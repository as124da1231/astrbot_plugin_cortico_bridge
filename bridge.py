"""桥的传输层：WebSocket 服务端、握手、帧收发。

这一层只管「连接」和「帧」，不懂 QQ、不懂 Cortico 的语义。
上层通过两个回调把行为注入进来：
- on_send(conv_kind, conv_id, text, reply_to, platform) -> (ok, error)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed

    HAS_WS = True
except Exception:  # pragma: no cover - 缺依赖时插件仍要能加载
    ws_serve = None  # type: ignore[assignment]
    ConnectionClosed = Exception  # type: ignore[misc,assignment]
    HAS_WS = False

SendCallback = Callable[
    [str, str, str, str, str, "dict | None", bool],
    Awaitable[tuple[bool, str, bool]],
]
EventCallback = Callable[[str, dict], None]
# (工具名, 参数, 触发这一轮的消息 id) -> (成功, 文本)
ToolCallback = Callable[[str, dict, str], Awaitable[tuple[bool, str]]]
# 握手后随 ready 一起下发的工具清单
ToolsProvider = Callable[[], dict]
# 语音能力：同步返回 bool，或返回 {ready, provider}（也可以 async）
TtsInfoProvider = Callable[[], Any]


def _s(value: Any) -> str:
    return "" if value is None else str(value)


# 图片 base64 的上限；World 那边限制原始 6MB，编码后会更大一些。
MAX_IMAGE_B64 = 8 * 1024 * 1024
# 一帧里最多带多少图片数据（base64 总和）；两张 8MB 的图会把帧撑到 16MB，
# 对模型没有意义，内存也吃不消。
MAX_IMAGE_TOTAL_B64 = 10 * 1024 * 1024

# 工具回执的长度上限：再长就截断，别把上下文撑爆。
MAX_TOOL_RESULT = 4000

# 握手超时：连上后必须在这么久内发出合法的 hello，否则断开。
HELLO_TIMEOUT = 10.0


def _parse_image(raw: Any) -> tuple[dict | None, str]:
    """校验 send 帧里的图片。返回 (图片, 错误说明)；没有图片时返回 (None, "")。"""
    if not raw:
        return None, ""
    if not isinstance(raw, dict):
        return None, "图片字段格式不正确"
    mime = _s(raw.get("mime")) or "image/jpeg"
    b64 = _s(raw.get("base64")).strip()
    if not b64:
        return None, "图片内容是空的"
    if len(b64) > MAX_IMAGE_B64:
        return None, f"图片太大，超过 {MAX_IMAGE_B64 // (1024 * 1024)}MB"
    if not mime.startswith("image/"):
        return None, f"不支持的图片类型 {mime}"
    return {"mime": mime, "base64": b64}, ""


class BridgeServer:
    """一个只服务单个 Cortico 客户端的 WS 服务端。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        on_send: SendCallback,
        on_event: EventCallback | None = None,
        admin_ids: Callable[[], list[str]] | None = None,
        on_tool: ToolCallback | None = None,
        tools_frame: ToolsProvider | None = None,
        tts_available: TtsInfoProvider | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self._on_send = on_send
        self._on_event = on_event
        self._admin_ids = admin_ids or (lambda: [])
        self._on_tool = on_tool
        self._tools_frame = tools_frame
        self._tts_available = tts_available or (lambda: False)
        self._server = None
        self._client = None
        self._lock = asyncio.Lock()
        self._tick_task: asyncio.Task | None = None
        # 给状态面板看的信息
        self.client_address = ""
        self.connected_at: float | None = None
        self.last_frame_at: float | None = None
        self.listening = False
        # 监听失败的原因；面板直接显示，免得只看到「监听未开启」。
        self.listen_error = ""
        # 对端在 hello 里自报的能力；目前只有「能不能收图」。
        self.capabilities: dict = {}
        # 桥自报的身份；第一条 QQ 事件到达后由上层补上。
        self.self_id = ""
        self.self_name = ""

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # noqa: BLE001 - 面板回调坏了不能影响桥
            logger.debug("Cortico Bridge: 面板事件回调失败", exc_info=True)

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """启动监听。端口被占只记日志并返回 False，绝不抛错。"""
        if self.listening and self._server is not None:
            # 同一个实例被重复 initialize / 重开时，不要再去绑一次。
            return True
        if not HAS_WS:
            logger.error(
                "Cortico Bridge: 缺少 websockets 依赖，桥没有启动。请给 AstrBot 安装 websockets。"
            )
            self.listen_error = "缺少 websockets 依赖"
            return False
        try:
            self._server = await ws_serve(
                self._handle_client,
                self.host,
                self.port,
                max_size=16 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            )
        except OSError as exc:
            logger.error(f"Cortico Bridge: 无法监听 {self.host}:{self.port} —— {exc}")
            self._server = None
            self.listening = False
            self.listen_error = str(exc)
            return False
        self.listening = True
        self.listen_error = ""
        logger.info(f"Cortico Bridge: 已在 {self.host}:{self.port} 等待 Cortico 连接")
        self._emit("listening", host=self.host, port=self.port)
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())
        return True

    async def _tick_loop(self) -> None:
        """应用层心跳：光靠 WS ping 在某些代理/休眠后会停，任何一帧都能刷新对端活跃时间。"""
        while True:
            await asyncio.sleep(20)
            if self._client is None:
                continue
            try:
                await self._client.send(
                    json.dumps({"t": "tick", "ts": time.time()}, ensure_ascii=False)
                )
                self.last_frame_at = time.time()
            except Exception:  # noqa: BLE001 - 发不出去就等 close 接手
                logger.debug("Cortico Bridge: tick 发送失败", exc_info=True)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        self.listening = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(BaseException):
                await self._tick_task
            self._tick_task = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            logger.info("Cortico Bridge: 服务端已关闭")
        async with self._lock:
            client = self._client
            self._client = None
            self.connected_at = None
            self.client_address = ""
            self.capabilities = {}
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    @property
    def connected(self) -> bool:
        return self._client is not None

    # ---------- 帧 ----------

    async def push(self, obj: dict) -> bool:
        """往当前客户端发一帧；没连上时返回 False。"""
        client = self._client
        if client is None:
            return False
        try:
            await client.send(json.dumps(obj, ensure_ascii=False))
            self.last_frame_at = time.time()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 发送失败 —— {exc}")
            return False

    async def _send_to(self, websocket, obj: dict) -> bool:
        """把回执发回发起请求的那条连接。

        不能用 push：慢工具跑完时客户端可能已经重连，push 会把旧回执
        发给新连接。发不出去就算了，World 那边有超时兜底。
        """
        if websocket is None:
            return False
        try:
            await websocket.send(json.dumps(obj, ensure_ascii=False))
            if websocket is self._client:
                self.last_frame_at = time.time()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def notify(self, text: str, kind: str = "") -> None:
        """给 Cortico 推一条状态通知。"""
        frame: dict[str, Any] = {"t": "notice", "text": text}
        if kind:
            frame["kind"] = kind
        await self.push(frame)

    async def _tts_info(self) -> dict:
        """当前语音能力；查询失败按不可用，不影响握手。"""
        try:
            raw = self._tts_available()
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cortico Bridge: 查询 TTS 状态失败 —— {exc}")
            raw = None
        if isinstance(raw, dict):
            return {"ready": bool(raw.get("ready")), "provider": _s(raw.get("provider"))}
        return {"ready": bool(raw), "provider": ""}

    async def ready_frame(self) -> dict:
        return {
            "t": "ready",
            "channel": "qq",
            # 第一条 QQ 事件到达后由上层补上；早期为空。
            "self": {"id": self.self_id, "name": self.self_name},
            # 谁是 AstrBot 的管理员；当前约定这就是 QQ 上的「主人」。
            "admins": list(self._admin_ids()),
            # 桥这边能不能发语音，以及会用哪个 provider（World 可据此少试一次）。
            "tts": await self._tts_info(),
            "v": 1,
        }

    async def push_ready(self) -> None:
        """重推一次 ready：握手时 provider 可能还没就绪，语音设置变了也要刷新。"""
        if self._client is None:
            return
        await self.push(await self.ready_frame())

    async def _refresh_ready_later(self) -> None:
        await asyncio.sleep(2)
        if self._client is not None:
            await self.push_ready()

    async def _handle_client(self, websocket) -> None:
        remote = getattr(websocket, "remote_address", None)
        if isinstance(remote, (tuple, list)) and remote:
            address = f"{remote[0]}:{remote[1]}" if len(remote) > 1 else _s(remote[0])
        else:
            address = _s(remote)

        registered = False
        tasks: set[asyncio.Task] = set()
        send_lock = asyncio.Lock()

        def drop_task(task: asyncio.Task) -> None:
            tasks.discard(task)
            with contextlib.suppress(BaseException):
                task.exception()

        try:
            # 第一帧必须是 hello。握手前不把自己登记成当前客户端，也不推
            # 任何消息：否则谁连上监听端口都能收到整群 QQ 消息、发工具调用。
            try:
                first_raw = await asyncio.wait_for(
                    websocket.recv(), timeout=HELLO_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(f"Cortico Bridge: {address} 握手超时，已断开")
                with contextlib.suppress(Exception):
                    await websocket.close(code=4001, reason="hello timeout")
                return
            try:
                first = json.loads(first_raw)
            except (TypeError, ValueError):
                logger.warning(f"Cortico Bridge: {address} 第一帧不是 JSON，已断开")
                with contextlib.suppress(Exception):
                    await websocket.close(code=4002, reason="hello required")
                return
            if not isinstance(first, dict) or _s(first.get("t")) != "hello":
                logger.warning(f"Cortico Bridge: {address} 没有先握手，已断开")
                with contextlib.suppress(Exception):
                    await websocket.close(code=4002, reason="hello required")
                return
            if self.token and not hmac.compare_digest(
                _s(first.get("token")), self.token
            ):
                logger.warning(f"Cortico Bridge: {address} token 不匹配，已断开")
                with contextlib.suppress(Exception):
                    await websocket.close(code=4003, reason="bad token")
                return

            caps = first.get("capabilities")
            async with self._lock:
                old = self._client
                self._client = websocket
                self.client_address = address
                self.connected_at = time.time()
                self.last_frame_at = time.time()
                self.capabilities = caps if isinstance(caps, dict) else {}
                registered = True
            if old is not None and old is not websocket:
                # 新连接顶掉旧的：Cortico 重启时常见。
                logger.info("Cortico Bridge: 新连接替换了旧连接")
                with contextlib.suppress(Exception):
                    await old.close()
            logger.info(f"Cortico Bridge: Cortico 已连接 {address}")
            logger.info(
                "Cortico Bridge: 握手完成（对端能收图=%s）"
                % bool(self.capabilities.get("images"))
            )
            self._emit("connected", address=address)
            await self.push_ready()
            # 握手时 provider 可能还没就绪；2 秒后再推一次，覆盖这个窗口。
            asyncio.create_task(self._refresh_ready_later())
            # 握手完就把可用的工具清单推过去，World 侧据此拼环境提示词。
            if self._tools_frame is not None:
                try:
                    await self.push(self._tools_frame())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Cortico Bridge: 下发工具清单失败 —— {exc}")

            async for raw in websocket:
                if websocket is not self._client:
                    break
                self.last_frame_at = time.time()
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("Cortico Bridge: 收到非 JSON 帧，已忽略")
                    continue
                if not isinstance(frame, dict):
                    continue
                # send/tool 可能是慢操作（TTS、查网），派发出去，
                # 别让一次 30 秒的语音把后面的工具调用和回执全堵住。
                if _s(frame.get("t")) in ("send", "tool"):
                    task = asyncio.create_task(
                        self._serve_frame(websocket, frame, send_lock)
                    )
                    tasks.add(task)
                    task.add_done_callback(drop_task)
                else:
                    await self._on_frame(websocket, frame)
        except ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 连接异常 —— {exc}")
        finally:
            for task in list(tasks):
                task.cancel()
            if registered:
                async with self._lock:
                    if self._client is websocket:
                        self._client = None
                        self.connected_at = None
                        self.client_address = ""
                        self.capabilities = {}
                logger.info(
                    "Cortico Bridge: Cortico 已断开（code=%s reason=%s）"
                    % (getattr(websocket, "close_code", None), getattr(websocket, "close_reason", None))
                )
                self._emit("disconnected")

    async def _serve_frame(
        self, websocket, frame: dict, send_lock: asyncio.Lock
    ) -> None:
        kind = _s(frame.get("t"))
        if kind == "send":
            # 同一个连接的发送保持顺序；发送是聊天语义，不能乱序。
            async with send_lock:
                await self._handle_send(websocket, frame)
        elif kind == "tool":
            await self._handle_tool(websocket, frame)

    async def _handle_send(self, websocket, frame: dict) -> None:
        rid = _s(frame.get("rid"))
        conv = frame.get("conv")
        if not isinstance(conv, dict):
            conv = {}
        text = _s(frame.get("text"))
        reply_to = _s(frame.get("replyTo"))
        platform = _s(frame.get("platform"))
        conv_kind = _s(conv.get("kind"))
        conv_id = _s(conv.get("id"))
        voice = frame.get("voice") is True
        image, image_err = _parse_image(frame.get("image"))
        used_voice = False

        if image_err:
            ok, err = False, image_err
        elif not text and image is None:
            ok, err = False, "正文和图片都是空的"
        elif voice and not text.strip():
            ok, err = False, "语音需要正文"
        elif voice and image is not None:
            ok, err = False, "语音和图片不能是同一条消息"
        elif conv_kind not in ("group", "private") or not conv_id:
            ok, err = False, "会话参数不合法"
        else:
            ok, err, used_voice = await self._on_send(
                conv_kind, conv_id, text, reply_to, platform, image, voice
            )

        ack: dict[str, Any] = {
            "t": "ack",
            "rid": rid,
            "ok": ok,
            "voice": used_voice,
        }
        if not ok:
            ack["error"] = err
        elif err:
            ack["note"] = err
        await self._send_to(websocket, ack)

    async def _handle_tool(self, websocket, frame: dict) -> None:
        rid = _s(frame.get("rid"))
        name = _s(frame.get("name"))
        turn = _s(frame.get("turn"))
        raw_args = frame.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        if not name:
            ok, text = False, "缺少工具名"
        elif self._on_tool is None:
            ok, text = False, "桥没有配置工具执行"
        else:
            try:
                ok, text = await self._on_tool(name, args, turn)
            except Exception as exc:  # noqa: BLE001
                ok, text = False, str(exc).strip() or type(exc).__name__
        payload: dict[str, Any] = {"t": "tool-ack", "rid": rid, "ok": ok}
        payload["result" if ok else "error"] = text[:MAX_TOOL_RESULT]
        await self._send_to(websocket, payload)

    async def _on_frame(self, websocket, frame: dict) -> None:
        if websocket is not self._client:
            return
        kind = _s(frame.get("t"))

        if kind == "hello":
            # 同一连接重复握手：token 变了就断开，否则只刷新能力并重推 ready。
            token = _s(frame.get("token"))
            if self.token and not hmac.compare_digest(token, self.token):
                logger.warning("Cortico Bridge: token 不匹配，已断开该连接")
                with contextlib.suppress(Exception):
                    await websocket.close(code=4003, reason="bad token")
                return
            caps = frame.get("capabilities")
            if isinstance(caps, dict):
                self.capabilities = caps
            await self.push_ready()
            return

        logger.debug(f"Cortico Bridge: 未知帧类型 {kind}")
