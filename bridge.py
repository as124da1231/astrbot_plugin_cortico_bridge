"""桥的传输层：WebSocket 服务端、握手、帧收发。

这一层只管「连接」和「帧」，不懂 QQ、不懂 Cortico 的语义。
上层通过两个回调把行为注入进来：
- on_send(conv_kind, conv_id, text, reply_to, platform, image, voice, umo, at)
  -> (ok, note, used_voice, uncertain, error_code)
- on_tool(name, args, turn, actor) -> (ok, text or component receipt)
"""

from __future__ import annotations

import asyncio
from uuid import uuid4
import contextlib
import hmac
import inspect
import json
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

from astrbot.api import logger

try:  # AstrBot 以包方式加载插件；单元测试把插件目录直接放进 sys.path
    from . import errors as codes
    from .message_segments import ComponentReceipt
except ImportError:  # pragma: no cover - 单文件加载（测试/脚本）
    import errors as codes
    from message_segments import ComponentReceipt

try:
    from websockets.asyncio.server import serve as ws_serve
    from websockets.exceptions import ConnectionClosed

    HAS_WS = True
except Exception:  # pragma: no cover - 缺依赖时插件仍要能加载
    ws_serve = None  # type: ignore[assignment]
    ConnectionClosed = Exception  # type: ignore[misc,assignment]
    HAS_WS = False

# (ok, 备注/中文说明, 是否用了语音, 结果是否未知, 错误码)
# 第 5 位只在失败时有值，取 errors.py 里的稳定 code；ack.error 只放它。
SendCallback = Callable[
    [str, str, str, str, str, "dict | None", bool, str, list[str]],
    Awaitable[tuple[bool, str, bool, bool, str]],
]
EventCallback = Callable[[str, dict], None]
# (工具名, 参数, 触发这一轮的消息 id, Coo 轮次的主人身份) -> (成功, 文本或组件回执)
ToolCallback = Callable[..., Awaitable[tuple[bool, str | ComponentReceipt]]]
# 握手后随 ready 一起下发的工具清单
ToolsProvider = Callable[[], dict]
# 语音能力：同步返回 bool，或返回 {ready, provider}（也可以 async）
TtsInfoProvider = Callable[[], Any]


def _s(value: Any) -> str:
    return "" if value is None else str(value)

def parse_umo(raw: str):
    """解析 platform:type:session；session 里允许有冒号。"""
    value = _s(raw).strip()
    if not value or len(value) > 512:
        return None
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    platform, msg_type, session = parts
    if not platform or not session:
        return None
    if msg_type not in ("GroupMessage", "FriendMessage"):
        return None
    kind = "group" if msg_type == "GroupMessage" else "private"
    return platform, kind, session, value


# 连进来之后才判的来源问题；空字符串表示放行。
# 浏览器发起的 WebSocket 一定会带 Origin，正常的 Coo 客户端（Node ws）不带。
# URL 里带凭据会进代理日志和错误串，和 DSH 桥的判据一致：见即拒。
def _header(headers, name: str) -> str:
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value:
            return _s(value)
    except Exception:  # noqa: BLE001 - 头部形状不对按没有处理
        pass
    try:
        for key, value in headers.items():
            if _s(key).lower() == name:
                return _s(value)
    except Exception:  # noqa: BLE001
        pass
    return ""


def connection_refusal(headers, path: str) -> str:
    if _header(headers, "origin").strip():
        return "origin"
    try:
        query = urlsplit(_s(path)).query
    except ValueError:
        return "query_token"
    if any(key.lower() in {"token", "access_token", "password", "auth", "authorization", "api_key", "apikey"}
           for key, _ in parse_qsl(query, keep_blank_values=True)):
        return "query_token"
    return ""


def bearer_token(headers) -> str:
    """从 Authorization 头里取 Bearer 凭据；没有或格式不对返回空。"""
    raw = _header(headers, "authorization").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def hello_version(frame: dict) -> int:
    """hello 里声明的协议版本；缺省按 1 处理，非法值返回 0。"""
    raw = frame.get("v", 1)
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return 0


def token_equal(a: str, b: str) -> bool:
    """定长比较 token；按 UTF-8 编码，非 ASCII 的自定义 token 也能比。"""
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


def is_loopback_host(host: str) -> bool:
    return str(host).strip().lower() in ("127.0.0.1", "localhost", "::1", "[::1]")


def looks_like_command(text: str, prefixes: list[str]) -> bool:
    """消息是不是 AstrBot 指令；命中 wake_prefix 就交给宿主，不进桥。"""
    value = _s(text).lstrip()
    if not value:
        return False
    return any(value.startswith(_s(p).strip()) for p in prefixes if _s(p).strip())


def matches_command(text: str, names) -> bool:
    """无前缀指令：首词或整句命中已注册的指令名。"""
    value = " ".join(_s(text).split())
    if not value or not names:
        return False
    return any(value == n or value.startswith(n + " ") for n in names if n)



# 图片 base64 的上限；World 那边限制原始 6MB，编码后会更大一些。
MAX_IMAGE_B64 = 8 * 1024 * 1024
# 一帧里最多带多少图片数据（base64 总和）；两张 8MB 的图会把帧撑到 16MB，
# 对模型没有意义，内存也吃不消。
MAX_IMAGE_TOTAL_B64 = 10 * 1024 * 1024

# 工具回执的长度上限：再长就截断，别把上下文撑爆。
MAX_TOOL_RESULT = 4000

# 握手超时：连上后必须在这么久内发出合法的 hello，否则断开。
HELLO_TIMEOUT = 10.0

# 桥协议版本：World 在 hello 里声明，插件不认就断开，避免版本错配后「能连上但行为不对」。
PROTOCOL_VERSION = 3

# 同一个会话的发送排队上限：前面的语音/大图还没发完时，后面的别无限等。
SEND_QUEUE_TIMEOUT = 5.0

# 同时在跑的 send/tool 任务上限。每帧可能带 8MB 图片，异常客户端刷帧时
# 不能让内存无限涨；超限直接回 busy，不新建任务。
MAX_INFLIGHT = 32

# per-conv 发送锁的上限：锁不随连接清空，所以需要回收空闲锁。
# 只删没被持有的锁，正在排队/发送的锁跳过。
SEND_LOCK_MAX = 256

# 发送回执缓存：同一个 rid 重发时直接回上一次的结果，别把消息发两遍。
ACK_CACHE_TTL = 300.0
MESSAGE_RECEIPT_TIMEOUT = 2.0
ACK_CACHE_MAX = 200


def _parse_image(raw: Any) -> tuple[dict | None, str, str]:
    """校验 send 帧里的图片。

    返回 (图片, 错误码, 中文说明)；没有图片时三个值分别是 (None, "", "")。
    错误码来自 errors.py，调用方放进 ack.error，中文说明放进 ack.note。
    """
    if not raw:
        return None, "", ""
    if not isinstance(raw, dict):
        return None, codes.BAD_IMAGE, "图片字段格式不正确"
    mime = _s(raw.get("mime")) or "image/jpeg"
    b64 = _s(raw.get("base64")).strip()
    if not b64:
        return None, codes.BAD_IMAGE, "图片内容是空的"
    if len(b64) > MAX_IMAGE_B64:
        return (
            None,
            codes.IMAGE_TOO_LARGE,
            f"图片太大，超过 {MAX_IMAGE_B64 // (1024 * 1024)}MB",
        )
    if not mime.startswith("image/"):
        return None, codes.IMAGE_BAD_TYPE, f"不支持的图片类型 {mime}"
    return {"mime": mime, "base64": b64}, "", ""


#: 一条消息最多 @ 多少人；@全体 用 "all" 表示。
AT_MAX = 10


def _parse_at(raw: Any) -> tuple[list[str], str, str]:
    """校验 send 帧里的 at（群 @）。

    返回 (QQ 号列表, 错误码, 中文说明)；没有 at 时三个值是 ([], "", "")。
    只接受平台数字用户 ID 或 "all"；去重，@全体时只保留 "all"。
    """
    if raw is None:
        return [], "", ""
    if not isinstance(raw, list):
        return [], codes.BAD_AT, "at 必须是平台用户 ID 数组"
    if len(raw) > AT_MAX:
        return [], codes.BAD_AT, f"一条消息最多 @ {AT_MAX} 人"
    out: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            return [], codes.BAD_AT, "at 里只能是平台用户 ID 或 all"
        value = str(item).strip()
        if value == "all":
            out.append("all")
            continue
        if not (5 <= len(value) <= 20) or not value.isascii() or not value.isdigit():
            return [], codes.BAD_AT, f"「{value}」不是有效用户 ID，at 只接受数字"
        out.append(value)
    if "all" in out:
        return ["all"], "", ""
    return list(dict.fromkeys(out)), "", ""


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
        desktop_owner: Callable[[], str] | None = None,
        desktop_targets: Callable[[], list[str]] | None = None,
        owner_umo: Callable[[], str] | None = None,
        owner_dms: Callable[[], list[str]] | None = None,
        platform_routes: Callable[[], list[dict]] | None = None,
        target_allowed: Callable[[str], bool] | None = None,
        scope_mode: Callable[[str], str] | None = None,
        instance_managed: Callable[[], bool] | None = None,
        discord_target_allowed: Callable[[str], bool] | None = None,
        tools_frame: ToolsProvider | None = None,
        tts_available: TtsInfoProvider | None = None,
        conversations: Callable[[], list[dict]] | None = None,
        mode: str = "full",
        save_mode: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self._on_send = on_send
        self._on_event = on_event
        self._admin_ids = admin_ids or (lambda: [])
        self._on_tool = on_tool
        self._desktop_owner = desktop_owner or (lambda: "")
        self._desktop_targets = desktop_targets or (lambda: [])
        self._owner_umo = owner_umo or (lambda: "")
        self._owner_dms = owner_dms or (lambda: [self._owner_umo()])
        self._platform_routes = platform_routes or (lambda: [])
        self._target_allowed = target_allowed or discord_target_allowed or (lambda _umo: True)
        self._scope_mode = scope_mode or (lambda _umo: self.mode)
        self._instance_managed = instance_managed or (lambda: False)
        self._tools_frame = tools_frame
        self._tts_available = tts_available or (lambda: False)
        self._conversations = conversations or (lambda: [])
        self.mode = "safe" if mode == "safe" else "full"
        self._save_mode = save_mode
        self._mode_revision = uuid4().hex
        self._mode_ready = False
        self._pending_full: tuple[str, int, str, float] | None = None
        self._turn_scopes: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._active_requests: set[tuple[int, str]] = set()
        self._server = None
        self._client = None
        self._lock = asyncio.Lock()
        # per-conv 发送锁：跨连接保留（重连不能把同群的顺序打乱），空闲时回收。
        self._send_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._acks: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self._tick_task: asyncio.Task | None = None
        # 连接代次：每注册一个新客户端 +1。旧连接上还没跑完的慢任务
        # 完成后不能把 ack 写进新连接的缓存，否则 rid 从 a-1 重新开始时
        # 第一条消息会「回 ok 但没发」。
        self._epoch = 0
        # 正在跑的 send/tool 任务数；上限见 MAX_INFLIGHT。
        self._inflight = 0
        self._message_receipts: dict[str, tuple[int, str, asyncio.Future[bool]]] = {}
        # 给状态面板看的信息
        self.client_address = ""
        self.connected_at: float | None = None
        self.last_frame_at: float | None = None
        self.listening = False
        # 监听失败的原因；面板直接显示，免得只看到「监听未开启」。
        self.listen_error = ""
        # 来源门计数：带 Origin 的浏览器连接、URL 里带 token 的连接，各自记一笔。
        self.origin_refused = 0
        self.query_token_refused = 0
        # 握手门计数：token 不对、协议版本不认。
        self.bad_token_refused = 0
        self.version_refused = 0
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
        # 非回环地址等于把 QQ 消息和发送能力放到网络上。没有 token 就不开这扇门，
        # 不再只打一行警告：警告挡不住扫描器。
        if not is_loopback_host(self.host) and not self.token:
            self.listening = False
            self.listen_error = "非回环监听必须设置 token"
            logger.error(
                "Cortico Bridge: 监听在 %s 但没有设置 token，拒绝启动。"
                "只给本机用请填 127.0.0.1；需要跨机时先设置 token。" % self.host
            )
            return False
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
        except (OSError, ValueError, OverflowError) as exc:
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
                    json.dumps({
                        "t": "tick", "ts": time.time(),
                        "modeApprovalPending": bool(self.pending_mode_approval()),
                        **({"platformRoutes": self._platform_routes()} if self.supports_multiple_platforms else {}),
                    }, ensure_ascii=False)
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
        return self._client is not None and (self._save_mode is None or self._mode_ready)

    @property
    def has_client(self) -> bool:
        return self._client is not None

    @property
    def operations_active(self) -> bool:
        return self._inflight > 0

    # ---------- 帧 ----------

    async def offer_message(self, frame: dict) -> str:
        """只在明确拒收时交还 AstrBot。提交后没等到回执，可能已经接管。"""
        if not self.connected:
            return "declined"
        if self.capabilities.get("message_delivery_ack") is not True:
            return "accepted" if await self.push(frame) else "declined"
        if len(self._message_receipts) >= 64:
            return "declined"
        delivery_id = uuid4().hex
        receipt = asyncio.get_running_loop().create_future()
        self._message_receipts[delivery_id] = (self._epoch, self._mode_revision, receipt)
        try:
            if not await self.push({**frame, "deliveryId": delivery_id}):
                return "declined"
            accepted = await asyncio.wait_for(receipt, timeout=MESSAGE_RECEIPT_TIMEOUT)
            if not accepted:
                self._turn_scopes.pop((_s(frame.get("umo")), _s(frame.get("id"))), None)
            return "accepted" if accepted else "declined"
        except asyncio.TimeoutError:
            return "unknown"
        finally:
            self._message_receipts.pop(delivery_id, None)

    async def push(self, obj: dict) -> bool:
        """往当前客户端发一帧；没连上时返回 False。"""
        client = self._client
        if client is None:
            return False
        if obj.get("t") == "msg" and obj.get("channel") == "discord" and not self.supports_multiple_platforms:
            return False
        if self._save_mode and obj.get("t") == "tools" and (not self._mode_ready or self.mode != "full"):
            return False
        key = None
        if self._save_mode is not None and obj.get("t") == "msg":
            if not self._mode_ready:
                return False
            if self._scope_mode(_s(obj.get("umo"))) == "safe" and obj.get("umo") not in self._owner_dms():
                return False
            obj = {**obj, "modeRevision": self._mode_revision}
            if obj.get("kind") != "notice":
                key = (_s(obj.get("umo")), _s(obj.get("id")))
                self._turn_scopes[key] = time.monotonic()
                self._turn_scopes.move_to_end(key)
                while len(self._turn_scopes) > 500:
                    self._turn_scopes.popitem(last=False)
        try:
            await client.send(json.dumps(obj, ensure_ascii=False))
            self.last_frame_at = time.time()
            return True
        except Exception as exc:  # noqa: BLE001
            if key is not None:
                self._turn_scopes.pop(key, None)
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

    @property
    def supports_multiple_platforms(self) -> bool:
        return self.capabilities.get("multi_platform") is True

    async def ready_frame(self) -> dict:
        routes = self._platform_routes()
        by_id = {route["id"]: route for route in routes}
        visible_conversations = []
        for item in self._conversations() if self.mode == "full" else []:
            umo = item.get("umo") or ""
            route = by_id.get(umo.split(":", 1)[0])
            if self._instance_managed() and (not self._target_allowed(umo) or self._scope_mode(umo) != "full"):
                continue
            if route is not None:
                channel = route["channel"]
                if channel == "discord" and (not self.supports_multiple_platforms or not self._target_allowed(umo)):
                    continue
                if channel not in ("qq", "discord") and self.capabilities.get("instance_policies") is not True:
                    continue
                item = {**item, "channel": channel}
            visible_conversations.append(item)
        return {
            "t": "ready",
            "channel": "qq",
            # 第一条 QQ 事件到达后由上层补上；早期为空。
            "self": {"id": self.self_id, "name": self.self_name},
            # 谁是 AstrBot 的管理员；当前约定这就是 QQ 上的「主人」。
            "admins": list(self._admin_ids()) if self.token and self.mode == "full" else [],
            "ownerTools": self.mode == "full" and bool(self.tool_owner),
            "desktopSend": self.mode == "full" and self._has_credentials(self._client) and bool(self._desktop_targets()),
            "ownerBound": bool(any(self._owner_dms()) if self.supports_multiple_platforms else self._owner_umo()),
            "modeApprovalPending": bool(self.pending_mode_approval()),
            **({"instancePolicies": True} if self._instance_managed() else {}),
            # 桥这边能不能发语音，以及会用哪个 provider（World 可据此少试一次）。
            "tts": await self._tts_info(),
            # 桥记得的会话（含重启前恢复的），World 用它来支持主动发送。
            "conversations": visible_conversations[:20],
            **({"platformRoutes": routes} if self.supports_multiple_platforms else {}),
            "v": PROTOCOL_VERSION,
            **({"mode": self.mode, "modeRevision": self._mode_revision} if self._save_mode else {}),
        }

    @property
    def tool_owner(self) -> str:
        return self._authorized_owner(self._client)

    def _authorized_owner(self, websocket) -> str:
        owner_id = self._desktop_owner()
        return owner_id if self._has_credentials(websocket) and owner_id in self._admin_ids() else ""

    def _has_credentials(self, websocket) -> bool:
        headers = getattr(getattr(websocket, "request", None), "headers", None)
        presented = bearer_token(headers)
        return bool(self.token and presented and token_equal(presented, self.token))

    async def push_ready(self) -> None:
        """重推一次 ready：握手时 provider 可能还没就绪，语音设置变了也要刷新。"""
        if self._client is None:
            return
        await self.push(await self.ready_frame())

    async def _refresh_ready_later(self) -> None:
        await asyncio.sleep(2)
        if self._client is not None:
            await self.push_ready()

    def pending_mode_approval(self) -> str:
        request = self._pending_full
        if request is not None:
            rid, epoch, revision, deadline = request
            if self._client is not None and epoch == self._epoch and revision == self._mode_revision and time.monotonic() < deadline:
                return rid
            self._pending_full = None
        return ""

    def revise_permissions(self) -> None:
        self._pending_full = None
        self._mode_ready = False
        self._mode_revision = uuid4().hex
        self._turn_scopes.clear()
        self._acks.clear()

    def _apply_mode(self, mode: str) -> None:
        if self.operations_active:
            raise ValueError("桥正在执行上一轮，请稍后确认")
        if self._save_mode is None:
            raise ValueError("当前桥无法保存模式")
        self._save_mode(mode)
        self.mode = mode
        self.revise_permissions()

    async def approve_mode(self, request: str) -> None:
        if not request or request != self.pending_mode_approval():
            raise ValueError("切换请求已过期或权限配置已更新，请在 Coo 重新发起")
        self._apply_mode("full")
        await self.push_ready()

    async def reject_mode(self, request: str) -> None:
        if not request or request != self.pending_mode_approval():
            raise ValueError("切换请求已过期或权限配置已更新")
        self._pending_full = None
        await self.push_ready()

    async def narrow_mode(self) -> None:
        if self.mode == "full":
            self._apply_mode("safe")
        else:
            self._pending_full = None
        await self.push_ready()

    def _refuse_connection(self, reason: str, address: str) -> None:
        if reason == "origin":
            self.origin_refused += 1
            logger.warning(
                f"Cortico Bridge: {address} 带了 Origin 头，按浏览器页面拒绝（桥没有网页客户端）"
            )
        else:
            self.query_token_refused += 1
            logger.warning(
                f"Cortico Bridge: {address} 把 token 放在 URL 查询里，已拒绝（凭据不能进 URL）"
            )

    async def _authorize(self, websocket, headers, frame: dict, address: str) -> bool:
        """凭据只从 WebSocket Upgrade 请求头获取。"""
        presented = bearer_token(headers)
        if "token" in frame or (self.token and (not presented or not token_equal(presented, self.token))):
            self.bad_token_refused += 1
            logger.warning(f"Cortico Bridge: {address} token 不匹配，已断开")
            with contextlib.suppress(Exception):
                await websocket.close(code=4003, reason="bad token")
            return False
        return True

    async def _check_hello(self, websocket, headers, frame: dict, address: str) -> bool:
        """初次握手和同一连接上的重复 hello 共用同一套校验。

        - token：只接受 `Authorization: Bearer` 请求头。
        - 版本：重复 hello 也要重查，不能靠第一次握手的结果。
        不通过时按原因 close（4003/4004），返回 False。
        """
        version = hello_version(frame)
        if version != PROTOCOL_VERSION:
            self.version_refused += 1
            logger.warning(
                f"Cortico Bridge: {address} 协议版本不认（收到 {_s(frame.get('v')) or '缺省'}，"
                f"本插件支持 {PROTOCOL_VERSION}），已断开"
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=4004, reason="unsupported protocol version")
            return False
        return await self._authorize(websocket, headers, frame, address)

    async def _handle_client(self, websocket) -> None:
        remote = getattr(websocket, "remote_address", None)
        if isinstance(remote, (tuple, list)) and remote:
            address = f"{remote[0]}:{remote[1]}" if len(remote) > 1 else _s(remote[0])
        else:
            address = _s(remote)

        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", None)
        refusal = connection_refusal(
            headers,
            _s(getattr(request, "path", "")),
        )
        if refusal:
            self._refuse_connection(refusal, address)
            with contextlib.suppress(Exception):
                await websocket.close(
                    code=4403 if refusal == "origin" else 4401,
                    reason="origin refused" if refusal == "origin" else "query token refused",
                )
            return

        registered = False
        epoch = self._epoch
        tasks: set[asyncio.Task] = set()

        def drop_task(task: asyncio.Task) -> None:
            tasks.discard(task)
            # done callback 一定会跑（包括创建后立刻被取消的任务），
            # 用它归还并发额度，避免连接关闭时计数泄漏。
            self._inflight = max(0, self._inflight - 1)
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
            if not await self._check_hello(websocket, headers, first, address):
                return

            caps = first.get("capabilities")
            async with self._lock:
                old = self._client
                self._client = websocket
                self.client_address = address
                self.connected_at = time.time()
                self.last_frame_at = time.time()
                self.capabilities = caps if isinstance(caps, dict) else {}
                # 新连接意味着对端可能重启过，rid 会从头开始；旧 ack 不能
                # 带过来，否则第一条 a-1 可能拿到上一次的结果。发送锁不随
                # 连接清空：旧任务可能还持有它，清了会让同群发送并发。
                self._acks.clear()
                self._mode_ready = False
                self._pending_full = None
                self._turn_scopes.clear()
                self._epoch += 1
                epoch = self._epoch
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
                # 但并发数必须有上限：每帧可能带 8MB 图片，超限直接回 busy，
                # 不新建任务（也就不占内存）。
                kind = _s(frame.get("t"))
                if kind in ("send", "tool"):
                    rid = _s(frame.get("rid"))
                    if self._save_mode and (not rid or not self._mode_ready or frame.get("modeRevision") != self._mode_revision):
                        await self._reject_request(websocket, frame, "mode_unsynced", "模式未同步或本轮已结束")
                        continue
                    if (epoch, rid) in self._active_requests:
                        continue
                    if self._inflight >= MAX_INFLIGHT:
                        await self._reject_busy(websocket, frame)
                        continue
                    self._inflight += 1
                    self._active_requests.add((epoch, rid))
                    task = asyncio.create_task(
                        self._serve_frame(websocket, frame, epoch)
                    )
                    tasks.add(task)
                    task.add_done_callback(drop_task)
                    task.add_done_callback(lambda _, key=(epoch, rid): self._active_requests.discard(key))
                else:
                    await self._on_frame(websocket, frame, headers)
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
                    "Cortico Bridge: Cortico 已断开（code=%s）" % getattr(websocket, "close_code", None)
                )
                self._emit("disconnected")

    async def _reject_busy(self, websocket, frame: dict) -> None:
        """并发超限：本帧没有执行，回 busy。不进 ack 缓存（结果不该被重放）。"""
        kind = _s(frame.get("t"))
        rid = _s(frame.get("rid"))
        if kind == "tool":
            payload: dict[str, Any] = {
                "t": "tool-ack",
                "rid": rid,
                "ok": False,
                "error": codes.BUSY,
            }
        else:
            payload = {
                "t": "ack",
                "rid": rid,
                "ok": False,
                "voice": False,
                "error": codes.BUSY,
                "note": "桥正忙，本条没有发出",
            }
        await self._send_to(websocket, payload)

    async def _reject_request(self, websocket, frame: dict, error: str, note: str) -> None:
        await self._send_to(websocket, {
            "t": "tool-ack" if frame.get("t") == "tool" else "ack",
            "rid": _s(frame.get("rid")), "ok": False, "error": error, "note": note,
        })

    def _authorize_turn(self, websocket, frame: dict, umo: str, *, sending: bool = False) -> str:
        if self._save_mode is None:
            return ""
        turn = _s(frame.get("turn"))
        actor = frame.get("actor")
        if turn and actor is None:
            stamp = self._turn_scopes.get((umo, turn))
            if stamp is not None and time.monotonic() - stamp <= 600:
                return ""
        elif not turn and self.mode == "full" and actor == {"kind": "desktop"}:
            if sending:
                if (self._has_credentials(websocket) and self._scope_mode(umo) == "full"
                        and umo in self._desktop_targets()):
                    return ""
            elif self._authorized_owner(websocket):
                return ""
        return "会话、消息或操作者未获本轮授权"

    async def _serve_frame(self, websocket, frame: dict, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = self._epoch
        kind = _s(frame.get("t"))
        if kind == "send":
            await self._handle_send(websocket, frame, epoch)
        elif kind == "tool":
            await self._handle_tool(websocket, frame, epoch)

    def _cached_ack(self, rid: str, epoch: int | None = None) -> dict | None:
        if not rid:
            return None
        if epoch is not None and epoch != self._epoch:
            return None
        hit = self._acks.get(rid)
        if hit is None:
            return None
        ts, ack = hit
        if time.time() - ts > ACK_CACHE_TTL:
            self._acks.pop(rid, None)
            return None
        return dict(ack)

    def _remember_ack(self, rid: str, ack: dict, epoch: int | None = None) -> None:
        if not rid:
            return
        if epoch is not None and epoch != self._epoch:
            # 旧连接上跑完的慢任务：回执只属于旧连接，不能污染新连接的缓存。
            logger.debug(
                "Cortico Bridge: 旧连接（代次 %s）的回执已丢弃（当前代次 %s）",
                epoch,
                self._epoch,
            )
            return
        now = time.time()
        self._acks[rid] = (now, dict(ack))
        self._acks.move_to_end(rid)
        while len(self._acks) > ACK_CACHE_MAX:
            self._acks.popitem(last=False)
        for key in list(self._acks):
            ts, _ = self._acks[key]
            if now - ts <= ACK_CACHE_TTL:
                break
            self._acks.pop(key, None)

    def _lock_for(self, key: str) -> asyncio.Lock:
        """取某个会话的发送锁；锁表按 LRU 回收空闲项，绝不删正在用的锁。"""
        lock = self._send_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[key] = lock
        else:
            self._send_locks.move_to_end(key)
        if len(self._send_locks) > SEND_LOCK_MAX:
            for old_key in list(self._send_locks):
                if len(self._send_locks) <= SEND_LOCK_MAX:
                    break
                old = self._send_locks[old_key]
                if old_key == key or old.locked():
                    continue
                self._send_locks.pop(old_key, None)
        return lock

    async def _handle_send(self, websocket, frame: dict, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = self._epoch
        if epoch != self._epoch:
            # 这条帧来自已经被替换的旧连接；不执行，也不写新缓存。
            logger.debug("Cortico Bridge: 旧连接发来的 send 帧已忽略")
            return
        rid = _s(frame.get("rid"))
        # 同一个 rid 重发：直接回上一次的结果，避免同一条消息发两遍。
        cached = self._cached_ack(rid, epoch)
        if cached is not None:
            await self._send_to(websocket, cached)
            return

        conv = frame.get("conv")
        if not isinstance(conv, dict):
            conv = {}
        text = _s(frame.get("text"))
        reply_to = _s(frame.get("replyTo"))
        platform = _s(frame.get("platform"))
        # UMO 是 AstrBot 的统一会话键；旧客户端没有时退回 conv。
        umo = _s(frame.get("umo")).strip()
        conv_kind = _s(conv.get("kind"))
        conv_id = _s(conv.get("id"))
        voice = frame.get("voice") is True
        at, at_code, at_note = _parse_at(frame.get("at"))

        # UMO 是权威键；有 UMO 时从它派生 kind/id，并拒绝与 conv 冲突。
        umo_code = ""
        if umo:
            parsed = parse_umo(umo)
            if parsed is None:
                umo_code = codes.INVALID_UMO
            else:
                p_platform, p_kind, p_id, p_umo = parsed
                if conv_kind and conv_id and (conv_kind != p_kind or conv_id != p_id):
                    umo_code = codes.UMO_CONV_MISMATCH
                else:
                    platform = p_platform or platform
                    conv_kind, conv_id, umo = p_kind, p_id, p_umo
        channel = next((route["channel"] for route in self._platform_routes() if route["id"] == platform), "qq")
        if channel == "discord":
            # 被引用的文字可能含有全体或角色提及，避免原样转发触发通知。
            text = re.sub(r"@(?=everyone\b|here\b|&[0-9]+>)", "@\u200b", text)
        image, image_code, image_note = _parse_image(frame.get("image"))
        used_voice = False
        uncertain = False
        # code 进 ack.error（只放英文稳定码），err 进 ack.note（中文说明）。
        code = ""
        err = ""

        if image_code:
            ok, code, err = False, image_code, image_note
        elif at_code:
            ok, code, err = False, at_code, at_note
        elif umo_code:
            ok, code = False, umo_code
            err = (
                "UMO 格式不正确"
                if umo_code == codes.INVALID_UMO
                else "UMO 与会话参数不一致"
            )
        elif not text and image is None:
            ok, code, err = False, codes.EMPTY_MESSAGE, "正文和图片都是空的"
        elif voice and not text.strip():
            ok, code, err = False, codes.VOICE_NEEDS_TEXT, "语音需要正文"
        elif voice and image is not None:
            ok, code, err = False, codes.VOICE_IMAGE_CONFLICT, "语音和图片不能是同一条消息"
        elif voice and at:
            ok, code, err = False, codes.VOICE_AT_CONFLICT, "语音和 @ 不能是同一条消息"
        elif channel != "qq" and "all" in at:
            ok, code, err = False, "scope_denied", "该平台只允许点名具体成员"
        elif conv_kind not in ("group", "private") or not conv_id:
            ok, code, err = False, codes.BAD_SESSION, "会话参数不合法"
        elif self._save_mode and self._scope_mode(umo) == "safe" and (conv_kind != "private" or image is not None or at):
            ok, code, err = False, "scope_denied", "安全模式仅允许回复已授权的主人私聊"
        elif (denied := self._authorize_turn(websocket, frame, umo, sending=True)):
            ok, code, err = False, "scope_denied", denied
        elif self._save_mode and reply_to and (umo, reply_to) not in self._turn_scopes:
            ok, code, err = False, "scope_denied", "引用须使用本会话中已核实的消息"
        else:
            # 同一个会话的发送串行，保证聊天顺序；不同会话互不堵。
            key = umo or f"{conv_kind}:{conv_id}"
            lock = self._lock_for(key)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=SEND_QUEUE_TIMEOUT)
            except asyncio.TimeoutError:
                ok, code, err = (
                    False,
                    codes.SEND_QUEUE_TIMEOUT,
                    f"发送排队超过 {SEND_QUEUE_TIMEOUT:.0f} 秒，本条没有发出",
                )
            else:
                try:
                    denied = self._authorize_turn(websocket, frame, umo, sending=True)
                    if epoch != self._epoch or (self._save_mode and frame.get("modeRevision") != self._mode_revision) or denied:
                        ok, note, used_voice, uncertain, raw_code = False, denied or "桥已重新连接", False, False, "scope_denied"
                    else:
                        ok, note, used_voice, uncertain, raw_code = await self._on_send(
                            conv_kind, conv_id, text, reply_to, platform, image, voice, umo, at,
                            **({"turn": _s(frame.get("turn"))} if self._instance_managed() else {}),
                        )
                    code = _s(raw_code).strip()
                    err = _s(note)
                except Exception as exc:  # noqa: BLE001 - 发送层异常也要回执
                    logger.warning("Cortico Bridge: 发送层异常（%s）", type(exc).__name__)
                    ok, err, used_voice, uncertain, code = (
                        False,
                        "发送失败，详情见插件日志",
                        False,
                        True,
                        codes.SEND_FAILED,
                    )
                finally:
                    lock.release()

        ack: dict[str, Any] = {
            "t": "ack",
            "rid": rid,
            "ok": ok,
            "voice": used_voice,
        }
        if not ok:
            # error 只放 code；没有 code 的旧回调一律归为 send_failed。
            ack["error"] = code or codes.SEND_FAILED
            if err and err != ack["error"]:
                ack["note"] = err
        elif err:
            ack["note"] = err
        if uncertain:
            ack["uncertain"] = True
        self._remember_ack(rid, ack, epoch)
        await self._send_to(websocket, ack)

    async def _handle_tool(self, websocket, frame: dict, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = self._epoch
        if epoch != self._epoch:
            logger.debug("Cortico Bridge: 旧连接发来的 tool 帧已忽略")
            return
        rid = _s(frame.get("rid"))
        cached = self._cached_ack(rid, epoch)
        if cached is not None:
            await self._send_to(websocket, cached)
            return
        name = _s(frame.get("name"))
        turn = _s(frame.get("turn"))
        raw_args = frame.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        raw_actor = frame.get("actor")
        actor = raw_actor if isinstance(raw_actor, dict) else None
        has_actor = raw_actor is not None

        # 纵深防御：身份在桥层先校验一遍，非法帧不调用 on_tool。
        identity_code = ""
        if not turn and not has_actor:
            identity_code = codes.MISSING_IDENTITY
        elif turn and has_actor:
            identity_code = codes.AMBIGUOUS_IDENTITY
        elif has_actor and actor != {"kind": "desktop"}:
            identity_code = codes.BAD_ACTOR
        elif actor is not None and not self._authorized_owner(websocket):
            identity_code = codes.OWNER_AUTH_REQUIRED
        elif self._authorize_turn(websocket, frame, _s(frame.get("umo"))):
            identity_code = "scope_denied"
        elif self._save_mode and (self._scope_mode(_s(frame.get("umo"))) if turn else self.mode) == "safe" and name not in ("cortico_read_message_component", "cortico_authorize_history"):
            identity_code = "scope_denied"

        if identity_code:
            payload = {"t": "tool-ack", "rid": rid, "ok": False, "error": identity_code}
            if identity_code == codes.OWNER_AUTH_REQUIRED:
                payload["note"] = "请在 AstrBot 管理页授权桌面对话使用主人 QQ 和工具。"
            await self._send_to(websocket, payload)
            return

        code = ""
        note = ""
        if not name:
            ok, text = False, "缺少工具名"
        elif self._on_tool is None:
            ok, text = False, "桥没有配置工具执行"
        else:
            try:
                scope = {"scope": _s(frame.get("umo"))} if self._save_mode else {}
                ok, text = await self._on_tool(name, args, turn, actor, **scope)
            except Exception as exc:  # noqa: BLE001 - 异常原文可能包含媒体地址或凭据
                logger.error("Cortico Bridge: 工具执行失败 name=%s（%s）", name, type(exc).__name__)
                ok, text = False, ""
                # errors.py 扩容前先在本层保留稳定 code；World 对未知 code 有兜底。
                code = codes.TOOL_FAILED
                note = "工具执行失败，详情见插件日志"
        payload: dict[str, Any] = {"t": "tool-ack", "rid": rid, "ok": ok}
        if ok:
            if isinstance(text, ComponentReceipt):
                payload["result"] = text.text[:MAX_TOOL_RESULT]
                if text.images and self.capabilities.get("component_media"):
                    payload["images"] = text.images
                if text.audio is not None and self.capabilities.get("component_audio") is True:
                    payload["audio"] = text.audio
            else:
                payload["result"] = text[:MAX_TOOL_RESULT]
        else:
            # ack.error 只放稳定 code；业务原因（权限/白名单/超时）放 note。
            payload["error"] = code or codes.TOOL_FAILED
            detail = note or text.strip()
            if detail:
                payload["note"] = detail[:MAX_TOOL_RESULT]
        if "audio" not in payload:
            self._remember_ack(rid, payload, epoch)
        await self._send_to(websocket, payload)

    async def _on_frame(self, websocket, frame: dict, headers=None) -> None:
        if websocket is not self._client:
            return
        kind = _s(frame.get("t"))

        if kind == "msg-ack":
            pending = self._message_receipts.get(_s(frame.get("deliveryId")))
            accepted = frame.get("accepted")
            if pending is not None and pending[:2] == (self._epoch, self._mode_revision) and isinstance(accepted, bool):
                receipt = pending[2]
                if not receipt.done():
                    receipt.set_result(accepted)
            return

        if self._save_mode and kind == "mode-accept":
            if frame.get("modeRevision") != self._mode_revision:
                await self.push_ready()
                return
            self._mode_ready = True
            await self._send_to(websocket, {
                "t": "mode-status", "mode": self.mode, "modeRevision": self._mode_revision,
                "active": True, "rid": _s(frame.get("rid")), "pending": bool(self.pending_mode_approval()),
            })
            if self._mode_ready and self.mode == "full" and self._tools_frame is not None:
                await self.push(self._tools_frame())
            self._emit("mode", mode=self.mode, synced=self._mode_ready)
            return

        if self._save_mode and kind == "mode-change":
            rid = _s(frame.get("rid"))
            mode = frame.get("mode")
            error = ""
            pending = False
            if not rid or not self._mode_ready or frame.get("modeRevision") != self._mode_revision or mode not in ("full", "safe"):
                error = "模式状态已变化，请刷新"
            elif self._instance_managed():
                error = "请在 AstrBot 的「实例接入」中选择实例并切换模式"
            elif mode == "full" and self.mode == "safe":
                if not self.pending_mode_approval():
                    self._pending_full = (uuid4().hex, self._epoch, self._mode_revision, time.monotonic() + 180)
                pending = True
                self._emit("mode", mode=self.mode, synced=self._mode_ready)
            elif mode != self.mode:
                try:
                    self._apply_mode(mode)
                except ValueError as exc:
                    error = str(exc)
                except Exception:
                    error = "AstrBot 保存模式失败"
            await self._send_to(websocket, {
                "t": "mode-prepare", "rid": rid, "error": error, "pending": pending,
                "mode": self.mode, "modeRevision": self._mode_revision,
            })
            return

        if kind == "hello":
            # 重复握手沿用请求头鉴权及协议版本，成功后只刷新能力并重推 ready。
            address = self.client_address
            if not await self._check_hello(websocket, headers, frame, address):
                return
            caps = frame.get("capabilities")
            if isinstance(caps, dict):
                self.capabilities = caps
            await self.push_ready()
            return

        logger.debug(f"Cortico Bridge: 未知帧类型 {kind}")
