"""Cortico Bridge —— 把 AstrBot 的 QQ 会话接到 Cortico 的人格上。

分工：
- AstrBot 负责 QQ 连接、账号、白名单与所有指令类插件。
- Cortico 负责「说什么」。本插件把消息推过去，把它的回复发出去。

模块划分：
- bridge.py   传输层（WS 服务端、握手、帧）
- deliver.py  投递层（umo 登记、发到 QQ、频率上限）
- main.py     策略层（谁该收到什么、什么时候拦管线、面板接口）

一轮往返：
    QQ 消息 → observer 放行（B 模式）→ on_llm_request 推 msg 帧 → stop_event
                                                  ↓
                              Cortico 想完，调 astrbot_send_message
                                                  ↓
                                        send 帧 → deliver → QQ → ack
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import contextlib
import hashlib
import json
import mimetypes
import os
import time
from collections import OrderedDict, deque
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 面板是可选能力：旧版 AstrBot 没有 astrbot.api.web，插件本身照样能跑。
    from astrbot.api.web import (
        error_response,
        json_response,
        request,
        stream_response,
    )

    HAS_WEB_API = True
except Exception:  # pragma: no cover - 只在旧版本上走到
    error_response = json_response = stream_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    HAS_WEB_API = False

from .bridge import MAX_IMAGE_B64, MAX_IMAGE_TOTAL_B64, BridgeServer
from .deliver import Delivery
from .tools import ToolRegistry

PLUGIN_NAME = "astrbot_plugin_cortico_bridge"

# 跨插件热重载保存当前监听实例；模块被重新导入时 builtins 还在。
_ACTIVE_SERVER_ATTR = "_cortico_bridge_active_server"

# 非文字消息的占位说法，避免 B 模式下丢掉上下文
COMPONENT_LABELS = {
    "Image": "图片",
    "Face": "表情",
    "Record": "语音",
    "Video": "视频",
    "File": "文件",
    "Forward": "转发消息",
    "Json": "卡片",
    "Poke": "戳一戳",
}


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _sniff_image_mime(b64: str) -> str:
    """从 base64 开头几个字节认出图片格式；认不出按 jpeg 算。"""
    head = b64[:16]
    if head.startswith("iVBORw0KGgo"):
        return "image/png"
    if head.startswith("R0lGOD"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/jpeg"


def _apply_image_marks(text: str, marks: list[str]) -> str:
    """把正文里的 `[图片]` 占位按顺序换成实际结果。

    传过去的写 `[图片]`，没取到的写 `[图片|没取到：原因]`，超出上限的写
    `[图片|超出单次上限，没传]`。这样正文里的占位数和真正给到模型的图一一对应。
    """
    out = text
    pos = 0
    for mark in marks:
        # 不能简单用 replace：第一个 mark 可能就是 `[图片]`，替换后下一轮会
        # 又命中同一个位置，后面的标记就整体前移了。
        idx = out.find("[图片]", pos)
        if idx < 0:
            break
        out = out[:idx] + mark + out[idx + len("[图片]"):]
        pos = idx + len(mark)
    return out


def json_dumps(obj: Any) -> str:
    """SSE 用：json 里不会有裸换行，所以可以直接塞进 data: 行。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class CorticoBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_cortico_bridge"))
        except Exception:  # noqa: BLE001 - 拿不到数据目录也要能跑，只是不落盘
            data_dir = ""
        self.delivery = Delivery(context, lambda: self.config, data_dir=data_dir)
        self.tools = ToolRegistry(context, lambda: self.config)
        self.server = BridgeServer(
            host=_s(self.config.get("listen_host", "0.0.0.0")) or "0.0.0.0",
            port=self._port(),
            token=_s(self.config.get("token", "")),
            on_send=self._on_send_from_cortico,
            on_event=self._on_server_event,
            admin_ids=self._admin_ids,
            on_tool=self._on_tool_call,
            tools_frame=self.tools.frame,
            tts_available=self._tts_info,
        )
        # 最近推送的消息，给面板看
        self._recent: deque[dict] = deque(maxlen=40)
        # 最近的工具调用（谁、成没成、返回了什么），给面板看
        self._tool_log: deque[dict] = deque(maxlen=12)
        # 消息 id → (时间, 事件)：工具用触发这一轮的那条消息的身份执行。
        self._turns: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        # 去重：同一条消息被管线重复处理时只推一次
        self._seen: OrderedDict[str, float] = OrderedDict()
        self.stats = {
            "pushed": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "vision": 0,
            "voices": 0,
            "images": 0,
        }
        # 图片描述缓存：同一张图（同 URL / 同内容）只问一次视觉模型。
        self._image_cache: OrderedDict[str, str] = OrderedDict()
        # 自动选中的视觉模型；换模型时只提示一次。
        self._vision_logged = ""
        # QQ 号 → 昵称；通知事件只给号码，靠这里补上人名。
        self._names: OrderedDict[str, str] = OrderedDict()
        # SSE 订阅者（每个打开面板的浏览器一个队列）
        self._subscribers: set[asyncio.Queue] = set()
        self.started_at = time.time()

        if HAS_WEB_API:
            for route, handler, methods, desc in (
                ("status", self.page_status, ["GET"], "桥的状态与配置"),
                ("tools", self.page_tools, ["GET", "POST"], "给 Cortico 用的工具设置"),
                ("voice", self.page_voice, ["GET", "POST"], "语音输入/输出设置"),
                ("voice/preview", self.page_voice_preview, ["POST"], "合成一段试听语音"),
                ("voice/test", self.page_voice_test, ["POST"], "把试听语音发到最近的会话"),
                ("events", self.page_events, ["GET"], "实时事件流（SSE）"),
                ("restart", self.page_restart, ["POST"], "重开监听"),
                ("reset", self.page_reset, ["POST"], "清空统计与最近消息"),
            ):
                context.register_web_api(
                    f"/{PLUGIN_NAME}/{route}", handler, methods, desc
                )
        else:
            logger.info(
                "Cortico Bridge: 当前 AstrBot 没有 astrbot.api.web，面板接口跳过（桥功能不受影响）"
            )

    # ---------- 实时事件广播 ----------

    def _broadcast(self, event: dict) -> None:
        """把一条事件推给所有打开的面板。队列满了就丢，绝不阻塞桥。"""
        if not self._subscribers:
            return
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 面板卡住了，丢掉最旧的一条
                with contextlib.suppress(Exception):
                    queue.get_nowait()
                with contextlib.suppress(Exception):
                    queue.put_nowait(event)

    def _on_server_event(self, kind: str, payload: dict) -> None:
        event = {"type": kind, "ts": _now_iso(), **payload}
        if kind == "connected":
            event["text"] = f"Cortico 已连接 {payload.get('address', '')}".strip()
        elif kind == "disconnected":
            event["text"] = "Cortico 已断开"
        elif kind == "listening":
            event["text"] = f"监听已开启 {payload.get('host')}:{payload.get('port')}"
        else:
            event["text"] = kind
        self._broadcast(event)

    # ---------- 配置读写 ----------

    def _port(self) -> int:
        try:
            return int(self.config.get("listen_port", 17999) or 17999)
        except (TypeError, ValueError):
            return 17999

    def _observe_groups(self) -> list[str]:
        """B 模式群列表。兼容三种存法：list、逗号字符串、空。"""
        raw = self.config.get("observe_groups", [])
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _aliases(self) -> list[str]:
        raw = self.config.get("name_aliases", "")
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _max_sends(self) -> int:
        try:
            return int(self.config.get("max_sends_per_minute", 30) or 0)
        except (TypeError, ValueError):
            return 30

    def _tts_enabled(self) -> bool:
        return bool(self.config.get("tts_enabled", False))

    async def _tts_info(self) -> dict:
        """给 ready 帧的语音能力：插件开关 + 实际能不能拿到 provider。"""
        if not self._tts_enabled():
            return {"ready": False, "provider": ""}
        info = await self._voice_info("tts")
        return {"ready": bool(info.get("id")), "provider": info.get("id", "")}

    def _tts_max_chars(self) -> int:
        try:
            return int(self.config.get("tts_max_chars", 120) or 0)
        except (TypeError, ValueError):
            return 120

    def _stt_enabled(self) -> bool:
        return bool(self.config.get("stt_enabled", False))

    def _find_provider(self, kind: str, provider_id: str):
        """按 id 从 provider 实例表里找；找不到返回 None。"""
        getter = getattr(self.context, f"get_all_{kind}_providers", None)
        if not callable(getter):
            return None
        try:
            providers = getter() or []
        except Exception:  # noqa: BLE001
            return None
        for provider in providers:
            try:
                if _s(getattr(provider.meta(), "id", "")) == provider_id:
                    return provider
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _voice_provider(self, kind: str):
        """kind = stt / tts；配置指定优先，其次才是 AstrBot 当前的 provider。

        配置了具体 id 却找不到时不再偷偷退回当前 provider：面板显示的是
        A、实际用 B 会让语音变得不可预期，宁可这一轮不用语音。
        """
        wanted = _s(self.config.get(f"{kind}_provider_id", "")).strip()
        if wanted:
            found = self._find_provider(kind, wanted)
            if found is None:
                logger.warning(
                    "Cortico Bridge: 配置的 %s provider %s 没有找到"
                    % (kind.upper(), wanted)
                )
            return found
        getter = getattr(self.context, f"get_using_{kind}_provider_async", None)
        if callable(getter):
            return await getter()
        sync = getattr(self.context, f"get_using_{kind}_provider", None)
        return sync() if callable(sync) else None

    async def _voice_info(self, kind: str) -> dict:
        """给面板的 provider 摘要：id / type / model，拿不到就是空。"""
        try:
            provider = await self._voice_provider(kind)
        except Exception as exc:  # noqa: BLE001
            return {"id": "", "type": "", "model": "", "error": str(exc)}
        if provider is None:
            return {"id": "", "type": "", "model": ""}
        return self._provider_summary(provider)

    def _provider_summary(self, provider) -> dict:
        try:
            meta = provider.meta()
            return {
                "id": _s(getattr(meta, "id", "")),
                "type": _s(getattr(meta, "type", "")),
                "model": _s(getattr(meta, "model", "")),
            }
        except Exception:  # noqa: BLE001
            return {"id": "", "type": "", "model": ""}

    def _voice_list(self, kind: str) -> list[dict]:
        """面板下拉用：这个类型的全部 provider 实例。"""
        getter = getattr(self.context, f"get_all_{kind}_providers", None)
        if not callable(getter):
            return []
        try:
            providers = getter() or []
        except Exception:  # noqa: BLE001
            return []
        out: list[dict] = []
        for provider in providers:
            info = self._provider_summary(provider)
            if info["id"]:
                out.append(info)
        return out

    def _admin_ids(self) -> list[str]:
        """AstrBot 全局管理员列表；这是「谁有最高权限」的唯一来源。

        Cortico 那边不单独维护一份名单：截图权限、主人身份都从这里派生。
        """
        try:
            cfg = self.context.get_config()
            raw = cfg.get("admins_id", []) if cfg is not None else []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cortico Bridge: 读管理员列表失败 —— {exc}")
            raw = []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = _s(event.get_sender_id())
        return bool(sender) and sender in self._admin_ids()

    def _remember_self(self, event: AstrMessageEvent) -> None:
        """记下 bot 自己的 QQ 号；ready 帧会带上，World 才知道在和谁说话。"""
        try:
            self_id = _s(event.get_self_id())
        except Exception:  # noqa: BLE001
            return
        if not self_id or self.server.self_id == self_id:
            return
        self.server.self_id = self_id
        if self.server.connected:
            # 握手时还没有事件，identity 是空的；第一次看到事件后补推一次。
            asyncio.create_task(self.server.push_ready())

    # ---------- 生命周期 ----------

    async def initialize(self):
        token = _s(self.config.get("token", ""))
        logger.info(
            "Cortico Bridge: 启动 —— enabled=%s, 监听=%s:%s, token=%s, "
            "B模式=%s, 群=%s, 称呼=%r"
            % (
                self.config.get("enabled", False),
                self.server.host,
                self.server.port,
                f"<{len(token)} chars>" if token else "<empty>",
                self.config.get("observe_all", False),
                self._observe_groups() or "（全部群）",
                self.config.get("name_aliases", ""),
            )
        )
        logger.info(
            "Cortico Bridge: 主人（AstrBot 管理员）= %s"
            % (self._admin_ids() or "（未配置，截图不会放行）")
        )
        logger.info("Cortico Bridge: %s" % self.tools.startup_summary())

        # 插件热重载时旧实例可能还占着端口；把上一任监听关掉再绑。
        # 模块会被 AstrBot 重新导入，所以放在 builtins 里跨实例传递。
        old = getattr(builtins, _ACTIVE_SERVER_ATTR, None)
        if old is not None and old is not self.server:
            logger.info("Cortico Bridge: 发现上一次实例的监听，先关掉")
            with contextlib.suppress(Exception):
                await old.stop()
        setattr(builtins, _ACTIVE_SERVER_ATTR, self.server)

        await self.server.start()

    async def terminate(self):
        await self.server.stop()
        if getattr(builtins, _ACTIVE_SERVER_ATTR, None) is self.server:
            with contextlib.suppress(Exception):
                delattr(builtins, _ACTIVE_SERVER_ATTR)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """所有插件加载完再推一次清单：启动那一刻别的插件可能还没注册工具。"""
        if not self.config.get("enabled", False) or not self.server.connected:
            return
        try:
            self.tools.catalog(force=True)
            await self.server.push(self.tools.frame())
            logger.info(
                "Cortico Bridge: 已按完整工具表重推清单（%d 个）"
                % len(self.tools.enabled_catalog(force=True))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 重推工具清单失败 —— {exc}")

    # ---------- 从 Cortico 来的工具调用 ----------

    async def _on_tool_call(self, name: str, args: dict, turn: str) -> tuple[bool, str]:
        """用「触发这一轮的那条消息」的身份执行工具。"""
        if not turn:
            return False, "缺少触发消息 id，无法确认身份"
        hit = self._turns.get(turn)
        if hit is None:
            return False, "这一轮的消息已经过期，请重新说一次"
        stamp, event = hit
        if time.time() - stamp > 600:
            self._turns.pop(turn, None)
            return False, "这一轮的消息已经过期，请重新说一次"
        logger.info("Cortico Bridge: 执行工具 %s（来自消息 %s）" % (name, turn))
        ok, text = await self.tools.execute(name, args, event)
        try:
            sender = _s(event.get_sender_name() or event.get_sender_id())
            kind = "private" if event.is_private_chat() else "group"
            conv_id = _s(
                event.get_sender_id()
                if event.is_private_chat()
                else event.get_group_id()
            )
        except Exception:  # noqa: BLE001
            sender, kind, conv_id = "", "", ""
        entry = {
            "ts": _now_iso(),
            "name": name,
            "ok": ok,
            "sender": sender,
            "kind": kind,
            "conv": conv_id,
            "text": text[:160],
        }
        self._tool_log.appendleft(entry)
        self._broadcast({"type": "tool", **entry})
        return ok, text

    # ---------- 从 Cortico 来的发送请求 ----------

    async def _on_send_from_cortico(
        self,
        conv_kind: str,
        conv_id: str,
        text: str,
        reply_to: str,
        platform: str,
        image: dict | None = None,
        voice: bool = False,
    ) -> tuple[bool, str, bool]:
        ok, note, used_voice = await self.delivery.send(
            platform, conv_kind, conv_id, text, reply_to, image, voice
        )
        if ok:
            self.stats["sent"] += 1
            if used_voice:
                self.stats["voices"] = self.stats.get("voices", 0) + 1
            if image is not None:
                self.stats["images"] = self.stats.get("images", 0) + 1
            self._remember_out(conv_kind, conv_id, text, image, used_voice)
            logger.info(
                "Cortico Bridge: 已发出 conv=%s:%s %s%s%s"
                % (
                    conv_kind,
                    conv_id,
                    "语音 " if used_voice else "",
                    "图片+文字 " if image is not None and text and not used_voice else "图片 " if image is not None else "",
                    f"{len(text)} 字" if text else "",
                )
            )
            if note:
                logger.info(f"Cortico Bridge: 语音回退 —— {note}")
        else:
            self.stats["failed"] += 1
            logger.warning(f"Cortico Bridge: 发送失败 conv={conv_kind}:{conv_id} —— {note}")
            self._broadcast(
                {
                    "type": "error",
                    "ts": _now_iso(),
                    "text": f"发送失败 {conv_kind}:{conv_id} —— {note}",
                }
            )
        return ok, note, used_voice

    def _remember_out(
        self,
        conv_kind: str,
        conv_id: str,
        text: str,
        image: dict | None = None,
        voice: bool = False,
    ) -> None:
        conv_key = f"{conv_kind}:{conv_id}"
        conv_name = next(
            (
                e.get("convName")
                for e in reversed(self._recent)
                if e.get("conv") == conv_key and e.get("convName")
            ),
            "",
        )
        entry = {
            "ts": _now_iso(),
            "dir": "out",
            "channel": "qq",
            "conv": conv_key,
            "convName": conv_name,
            "sender": "Coo",
            "text": ("[语音] " + text[:200]).strip() if voice else ("[图片] " + text[:200]).strip() if image else text[:200],
            "image": image is not None,
            "voice": voice,
        }
        self._recent.append(entry)
        self._broadcast({"type": "message", **entry})

    # ---------- 消息进 Cortico ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe_all_messages(self, event: AstrMessageEvent):
        """B 模式：让没有 @ 的群消息也通过唤醒检查。

        WakingCheckStage 在消息没被唤醒时会直接 stop_event()，那样永远到不了
        on_llm_request。这里注册一个总是通过 filter 的 handler：它通过就置
        is_wake=True；还要补 is_at_or_wake_command，ProcessStage 认的是后者。

        本函数只放行，不推送——推送统一在 on_llm_request，避免两处逻辑分叉。
        唯一例外是通知类事件（撤回/入群/戳一戳）：它们没有正文，进不了 LLM 阶段，
        所以在这里直接翻译并推走。
        """
        if not self.config.get("enabled", False):
            return

        self._remember_self(event)

        # 通知类事件先处理：撤回、入群、退群、戳一戳、上传文件。
        notice, ref = self._notice_text(event)
        if notice:
            # 只有真的推给 Cortico 才拦下来；不在接管名单、或桥没连上时
            # 放它继续走，别的插件还能看到撤回/入群。
            pushed = await self._push_notice(event, notice, ref)
            if pushed or self._notice_in_scope(event):
                event.stop_event()
            return

        if not self.config.get("observe_all", False):
            return

        # 桥没连上时不要强制唤醒：否则 AstrBot 自己的模型会接管整个群，
        # Cortico 一断就开始刷屏。
        if not self.server.connected:
            return

        groups = self._observe_groups()
        gid = _s(event.get_group_id())
        if groups and gid not in groups:
            return

        event.is_at_or_wake_command = True
        # 标记「这轮是我们硬唤醒的」：推送失败时宁可这一轮不说话，
        # 也不要掉回 AstrBot 的模型。
        event.set_extra("_cortico_forced_wake", True)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """把这一轮交给 Cortico，然后让 AstrBot 自己的模型退出。"""
        if not self.config.get("enabled", False):
            return

        # 这一轮是不是 B 模式硬唤醒的（群里没点名也被我们叫起来）。
        forced = bool(event.get_extra("_cortico_forced_wake", False))

        if not self.server.connected:
            if forced:
                # 硬唤醒的消息不能掉回 AstrBot 自己的模型，否则桥一断就刷屏。
                logger.info("Cortico Bridge: Cortico 没连上，本条不回复")
                event.stop_event()
            else:
                logger.info("Cortico Bridge: Cortico 没连上，本轮交给 AstrBot")
            return

        text = await self._message_text(event)
        if not text:
            return

        conv_kind = "private" if event.is_private_chat() else "group"
        conv_id = (
            _s(event.get_group_id())
            if conv_kind == "group"
            else _s(event.get_sender_id() or event.get_session_id())
        )
        if not conv_id:
            return

        # 顺手把「号 → 昵称」记下来，通知事件只有号码时用来补人名。
        self._remember_name(_s(event.get_sender_id()), _s(event.get_sender_name()))

        # 群名单对 A/B 两种模式都生效：名单外的群完全不接管，交回 AstrBot。
        # 否则 B 模式只认名单、A 模式却谁 @ 都接，行为会不一致。
        if conv_kind == "group":
            groups = self._observe_groups()
            if groups and conv_id not in groups:
                logger.debug(f"Cortico Bridge: 群 {conv_id} 不在接管名单里，本轮交给 AstrBot")
                return

        platform_id = _s(event.get_platform_id())
        is_admin = self._is_admin(event)
        self.delivery.remember(platform_id, conv_kind, conv_id, event.unified_msg_origin)

        msg_id = self._message_id(event)
        if self._is_duplicate(msg_id):
            self.stats["skipped"] += 1
            logger.debug(f"Cortico Bridge: 重复消息已跳过 id={msg_id}")
            # 仍然要拦住 AstrBot，避免它对同一条消息回两次
            event.stop_event()
            return

        conv_name = ""
        if conv_kind == "group":
            with contextlib.suppress(Exception):
                group = await event.get_group()
                conv_name = _s(getattr(group, "group_name", "") if group else "")

        frame = {
            "t": "msg",
            "channel": "qq",
            "id": msg_id,
            "ts": _now_iso(),
            "conv": {"kind": conv_kind, "id": conv_id, "name": conv_name},
            "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
            "text": text,
            "mentioned": self._is_called(event, text),
            "platform": platform_id,
            # role 只是 AstrBot 的工具权限；owner 才是「主人」。
            # 目前按约定沿用 AstrBot 管理员作为主人，两者值相同，但字段分开。
            "role": "admin" if is_admin else "member",
            "owner": is_admin,
            "can": {"screenshot": is_admin},
        }

        # 对端模型能读图时，原图随帧一起过去（否则只传上面那句描述）。
        images, image_errors, image_marks = await self._image_payloads(event)
        if images:
            frame["images"] = images
        if image_errors:
            frame["image_errors"] = image_errors
        if image_marks:
            # 让正文里的占位与真正传过去的图一一对应；没取到的写清原因。
            frame["text"] = _apply_image_marks(text, image_marks)

        if not await self.server.push(frame):
            logger.warning("Cortico Bridge: 推送失败，本轮交给 AstrBot")
            # 没推成功就别把它记成「已见过」，否则管线重试时会被当成重复丢掉。
            if msg_id:
                self._seen.pop(msg_id, None)
            if forced:
                event.stop_event()
            return

        self.stats["pushed"] += 1
        preview = _apply_image_marks(text, image_marks) if image_marks else text
        entry = {
            "ts": frame["ts"],
            "dir": "in",
            "channel": "qq",
            "role": frame["role"],
            "owner": bool(frame.get("owner")),
            "notice": False,
            "conv": f"{conv_kind}:{conv_id}",
            "convName": conv_name,
            "sender": frame["sender"]["name"] or frame["sender"]["id"],
            "text": preview[:200],
            "mentioned": frame["mentioned"],
        }
        self._recent.append(entry)
        self._broadcast({"type": "message", **entry})
        logger.info(
            "Cortico Bridge: 已推送 conv=%s:%s mentioned=%s role=%s owner=%s 图片=%d 没取到=%d"
            % (
                conv_kind,
                conv_id,
                frame["mentioned"],
                frame["role"],
                frame.get("owner"),
                len(frame.get("images") or []),
                len(frame.get("image_errors") or []),
            )
        )

        # call_event_hook 只看 is_stopped()，不看返回值，必须显式 stop。
        event.stop_event()

        # 记下「这一轮是谁说的」：之后 Cortico 调工具时要用这条消息的身份。
        if msg_id:
            self._turns[msg_id] = (time.time(), event)
            self._turns.move_to_end(msg_id)
            while len(self._turns) > 100:
                self._turns.popitem(last=False)

    # ---------- 小工具 ----------

    async def _message_text(self, event: AstrMessageEvent) -> str:
        """取消息正文。

        文字直接用；语音交给 AstrBot 的 STT provider 转写；图片交给 AstrBot 的
        视觉模型描述成文字——因为 Cortico 那边的语言模型不一定能读图。
        """
        parts: list[str] = []
        text = event.get_message_str().strip()
        if text:
            parts.append(text)

        described = 0
        for comp in event.get_messages():
            name = type(comp).__name__
            if name == "Record":
                spoken = await self._transcribe(
                    comp, _s(getattr(event, "unified_msg_origin", ""))
                )
                parts.append(f"[语音] {spoken}" if spoken else "[语音]")
                continue
            if name == "Image":
                # 只有转述模式才花视觉调用；传原图时正文只留占位。
                if described < 2 and self._should_describe():
                    parts.append(await self._describe_image(comp))
                    described += 1
                else:
                    parts.append("[图片]")
                continue
            label = COMPONENT_LABELS.get(name)
            if label and f"[{label}]" not in parts:
                parts.append(f"[{label}]")

        return " ".join(p for p in parts if p).strip()

    # ---------- 看图 ----------

    def _image_mode(self) -> str:
        """auto / image / describe / off；配置写错时按 auto。"""
        raw = _s(self.config.get("image_mode", "auto")).strip().lower()
        return raw if raw in ("auto", "image", "describe", "off") else "auto"

    def _peer_images(self) -> bool:
        """对端模型能不能直接读图（由 Cortico 在握手时自报）。"""
        return bool((self.server.capabilities or {}).get("images"))

    def _should_describe(self) -> bool:
        """这一轮要不要花一次视觉调用把图转述成文字。"""
        mode = self._image_mode()
        if mode == "describe":
            return True
        return mode == "auto" and not self._peer_images()

    def _vision_provider_id(self) -> str:
        """看图的模型：配置里指定优先，否则自动挑一个带 vision / VL 的。"""
        configured = _s(self.config.get("image_vision_provider", "")).strip()
        if configured:
            return configured
        try:
            for provider in self.context.get_all_providers():
                cfg = getattr(provider, "provider_config", {}) or {}
                pid = _s(cfg.get("id"))
                low = pid.lower()
                if "vision" in low or "/vl" in low or "-vl" in low:
                    if self._vision_logged != pid:
                        logger.info(f"Cortico Bridge: 自动选用视觉模型 {pid}")
                        self._vision_logged = pid
                    return pid
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cortico Bridge: 找视觉模型失败 —— {exc}")
        return ""

    async def _image_source(self, comp) -> str:
        """给视觉模型的图片来源：能用 URL 就用 URL，否则转成 data URI。"""
        url = _s(getattr(comp, "url", "")).strip()
        if url.startswith(("http://", "https://")):
            return url
        try:
            b64 = await comp.convert_to_base64()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cortico Bridge: 取图失败 —— {exc}")
            return ""
        if not b64:
            return ""
        return f"data:{_sniff_image_mime(b64)};base64,{b64}"

    async def _describe_image(self, comp) -> str:
        """把一张图变成一句话。失败、没配视觉模型都退化成 [图片]。"""
        provider_id = self._vision_provider_id()
        if not provider_id:
            return "[图片]"

        source = await self._image_source(comp)
        if not source:
            return "[图片]"

        key = source if source.startswith("http") else "sha1:" + hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()
        cached = self._image_cache.get(key)
        if cached is not None:
            self._image_cache.move_to_end(key)
            return f"[图片：{cached}]"

        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=(
                        "用一两句话客观描述这张图片里看得见的内容。"
                        "如果它是表情包或梗图，说明它的画面和文字。不要评价，不要猜情绪。"
                    ),
                    image_urls=[source],
                ),
                timeout=25,
            )
            desc = _s(getattr(resp, "completion_text", "")).strip()
        except Exception as exc:  # noqa: BLE001
            # 超时类的异常 str() 是空的，直接拼 exc 会得到「识图失败 —— 」。
            reason = str(exc).strip() or type(exc).__name__
            logger.warning(
                "Cortico Bridge: 识图失败 provider=%s —— %s" % (provider_id, reason)
            )
            return "[图片]"

        if not desc:
            return "[图片]"

        desc = " ".join(desc.split())[:200]
        self._image_cache[key] = desc
        while len(self._image_cache) > 200:
            self._image_cache.popitem(last=False)
        self.stats["vision"] = self.stats.get("vision", 0) + 1
        logger.info(
            "Cortico Bridge: 识图完成 provider=%s %d 字" % (provider_id, len(desc))
        )
        return f"[图片：{desc}]"

    # ---------- 入站图片（对端能收图时才传） ----------

    async def _image_payloads(
        self, event: AstrMessageEvent
    ) -> tuple[list[dict], list[dict], list[str]]:
        """把消息里的图片打包进帧；返回 (图片, 失败清单, 每张图的占位标记)。

        这一轮决定转述或忽略时返回空列表。取不到图不再静默跳过：
        失败原因带回给调用方，正文也要如实说明，免得模型以为看见了图。
        """
        mode = self._image_mode()
        if mode in ("describe", "off"):
            return [], [], []
        if mode == "auto" and not self._peer_images():
            return [], [], []
        payloads: list[dict] = []
        errors: list[dict] = []
        marks: list[str] = []
        index = 0
        total_b64 = 0
        for comp in event.get_messages():
            if type(comp).__name__ != "Image":
                continue
            slot = index
            index += 1
            # QQ 图片 CDN 偶尔会断连；隔一会儿重试通常就好。每次最多等 10 秒，
            # 免得一次卡死的下载把这一轮回复也拖住。
            b64 = ""
            last_error = ""
            for attempt in range(3):
                try:
                    b64 = await asyncio.wait_for(comp.convert_to_base64(), timeout=10)
                    break
                except asyncio.TimeoutError:
                    last_error = "下载超时"
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc).strip() or type(exc).__name__
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
            if not b64:
                reason = last_error or "空数据"
                errors.append({"index": slot, "reason": reason})
                marks.append(f"[图片|没取到：{reason}]")
                logger.warning(f"Cortico Bridge: 取图失败 —— {reason}")
                continue
            if len(b64) > MAX_IMAGE_B64:
                errors.append({"index": slot, "reason": "太大"})
                marks.append("[图片|没取到：太大]")
                logger.warning(
                    "Cortico Bridge: 图片太大 %d KB（上限 %d KB），没有传给 Cortico"
                    % (len(b64) // 1024, MAX_IMAGE_B64 // 1024)
                )
                continue
            if len(payloads) >= 2:
                errors.append({"index": slot, "reason": "超过单次上限"})
                marks.append("[图片|超出单次上限，没传]")
                continue
            if total_b64 + len(b64) > MAX_IMAGE_TOTAL_B64:
                errors.append({"index": slot, "reason": "超过单次总大小"})
                marks.append("[图片|超出单次总大小，没传]")
                continue
            payloads.append({"mime": _sniff_image_mime(b64), "base64": b64})
            total_b64 += len(b64)
            marks.append("[图片]")
        return payloads, errors, marks

    # ---------- 通知类事件（撤回 / 入群 / 戳一戳…） ----------

    def _remember_name(self, uid: str, name: str) -> None:
        """攒 QQ 号 → 昵称；通知事件只带号码，靠这张表补人名。"""
        if not uid or not name or name == uid:
            return
        self._names[uid] = name
        self._names.move_to_end(uid)
        while len(self._names) > 500:
            self._names.popitem(last=False)

    def _display(self, uid: str) -> str:
        """有昵称就显示昵称，没有就退回号码。"""
        if not uid:
            return ""
        return self._names.get(uid) or uid

    def _notice_text(self, event: AstrMessageEvent) -> tuple[str, str]:
        """把 OneBot 的 notice 翻译成一句人话；返回 (正文, 相关消息号)。

        AstrBot 原生已经把这些转成了事件，但正文是空的，信息只在
        message_obj.raw_message 里；Poke 会额外变成一个组件。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None or not hasattr(raw, "get"):
            return "", ""
        try:
            if _s(raw.get("post_type")) != "notice":
                return "", ""
            kind = _s(raw.get("notice_type"))
            sub = _s(raw.get("sub_type"))
            user = _s(raw.get("user_id"))
            target = _s(raw.get("target_id"))
            operator = _s(raw.get("operator_id"))
            message_id = _s(raw.get("message_id"))
        except Exception:  # noqa: BLE001
            return "", ""

        self_id = _s(event.get_self_id())
        # 通知只给号码，用攒下来的昵称补人话；戳到自己时说「你」。
        user_label = self._display(user)
        operator_label = self._display(operator)
        target_label = "你" if target and target == self_id else self._display(target)

        if kind == "group_recall":
            return f"[撤回] {operator_label or user_label} 撤回了一条消息", message_id
        if kind == "friend_recall":
            return "[撤回] 对方撤回了一条消息", message_id
        if kind == "group_increase":
            return f"[入群] {user_label} 加入了群", ""
        if kind == "group_decrease":
            why = "被踢出" if sub == "kick" else "退出"
            who = f"（操作人 {operator_label}）" if operator and operator != user else ""
            return f"[退群] {user_label} {why}了群{who}", ""
        if kind == "group_admin":
            return f"[管理] {user_label} " + ("成为管理员" if sub == "set" else "被取消管理员"), ""
        if kind == "notify" and sub == "poke":
            return f"[戳一戳] {user_label} 戳了 {target_label}", ""
        if kind == "group_upload":
            file_info = raw.get("file") or {}
            name = _s(file_info.get("name")) if hasattr(file_info, "get") else ""
            return f"[上传] {user_label} 上传了文件 {name}".strip(), ""
        return "", ""

    def _notice_in_scope(self, event: AstrMessageEvent) -> bool:
        """这条通知是不是归我们接管；私聊一律算，群聊看名单。"""
        if event.is_private_chat():
            return True
        groups = self._observe_groups()
        gid = _s(event.get_group_id())
        return not groups or gid in groups

    async def _push_notice(
        self, event: AstrMessageEvent, text: str, ref: str = ""
    ) -> bool:
        """通知不进 LLM，直接推给 Cortico 当上下文记着。

        返回是否真的推出去了；调用方只在 True 时 stop_event，免得
        接管名单外的通知也被我们吞掉。
        """
        if not self.server.connected:
            return False

        conv_kind = "private" if event.is_private_chat() else "group"
        conv_id = (
            _s(event.get_group_id())
            if conv_kind == "group"
            else _s(event.get_sender_id() or event.get_session_id())
        )
        if not conv_id:
            return False
        if conv_kind == "group":
            groups = self._observe_groups()
            if groups and conv_id not in groups:
                return False

        platform_id = _s(event.get_platform_id())
        self.delivery.remember(platform_id, conv_kind, conv_id, event.unified_msg_origin)

        # 通知事件本身不带群名，问一次（拿不到就算了，World 那边有缓存兜底）。
        conv_name = ""
        if conv_kind == "group":
            with contextlib.suppress(Exception):
                group = await event.get_group()
                conv_name = _s(getattr(group, "group_name", "") if group else "")

        frame = {
            "t": "msg",
            "kind": "notice",
            "channel": "qq",
            "id": "",
            "ref": ref,
            "ts": _now_iso(),
            "conv": {"kind": conv_kind, "id": conv_id, "name": conv_name},
            "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
            "text": text,
            "mentioned": False,
            "platform": platform_id,
            "role": "admin" if self._is_admin(event) else "member",
            "owner": self._is_admin(event),
            "can": {"screenshot": False},
        }
        if not await self.server.push(frame):
            return False

        self.stats["pushed"] += 1
        self._recent.append(
            {
                "ts": frame["ts"],
                "dir": "in",
                "channel": "qq",
                "role": frame["role"],
                "owner": bool(frame.get("owner")),
                "notice": True,
                "conv": f"{conv_kind}:{conv_id}",
                "convName": conv_name,
                "sender": "通知",
                "text": text[:200],
                "mentioned": False,
            }
        )
        logger.info(
            "Cortico Bridge: 已推送通知 conv=%s:%s %s" % (conv_kind, conv_id, text[:30])
        )
        return True

    async def _transcribe(self, comp, umo: str = "") -> str:
        """用 AstrBot 已配置的 STT provider 把语音转成文字；失败就算了。"""
        if not self._stt_enabled():
            return ""
        try:
            provider = await self._voice_provider("stt")
            if provider is None:
                return ""
            source = _s(getattr(comp, "url", "")) or _s(getattr(comp, "file", ""))
            if not source:
                return ""
            result = await provider.get_text(source)
            return _s(result).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Cortico Bridge: 语音转写失败 —— {exc}")
            return ""

    def _is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        seen_at = self._seen.get(msg_id)
        if seen_at is not None and now - seen_at < 120:
            return True
        self._seen[msg_id] = now
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    def _is_called(self, event: AstrMessageEvent, text: str) -> bool:
        """有没有真的叫到名字。

        只认三种可确认的事实：@ 了自己、引用了自己的消息、正文里出现配置的称呼。
        不能用 is_wake_up()——B 模式下它被我们强制成 True 了。
        """
        self_id = _s(event.get_self_id())
        with contextlib.suppress(Exception):
            for comp in event.get_messages():
                name = type(comp).__name__
                if name == "At":
                    qq = _s(getattr(comp, "qq", ""))
                    if qq and (qq == self_id or qq == "all"):
                        return True
                elif name == "Reply" and self_id:
                    if _s(getattr(comp, "sender_id", "")) == self_id:
                        return True
        return any(a in text for a in self._aliases())

    def _message_id(self, event: AstrMessageEvent) -> str:
        for attr in ("message_id", "id"):
            value = getattr(event.message_obj, attr, None)
            if value:
                return _s(value)
        return "h" + hashlib.sha1(
            (event.get_message_str() + event.get_sender_id()).encode("utf-8")
        ).hexdigest()[:16]

    # ---------- 面板接口 ----------

    def _uptime(self) -> int:
        return int(time.time() - self.started_at)

    async def page_status(self):
        now = time.time()
        connected_at = self.server.connected_at
        last_frame = self.server.last_frame_at
        last = self._recent[-1] if self._recent else None
        return json_response(
            {
                "connected": self.server.connected,
                "listening": self.server.listening,
                "listenError": self.server.listen_error,
                "client": self.server.client_address,
                "connectedFor": (
                    int(now - connected_at) if connected_at is not None else 0
                ),
                "lastFrameAgo": (
                    int(now - last_frame) if last_frame is not None else -1
                ),
                "uptime": self._uptime(),
                "enabled": bool(self.config.get("enabled", False)),
                "observeAll": bool(self.config.get("observe_all", False)),
                "groups": self._observe_groups(),
                "aliases": self._aliases(),
                "host": self.server.host,
                "port": self.server.port,
                "tokenSet": bool(_s(self.config.get("token", ""))),
                "maxSends": self._max_sends(),
                "admins": self._admin_ids(),
                "imageMode": self._image_mode(),
                "peerImages": self._peer_images(),
                "ttsEnabled": self._tts_enabled(),
                "ttsMaxChars": self._tts_max_chars(),
                "sttEnabled": self._stt_enabled(),
                "sttProvider": (await self._voice_info("stt"))["id"],
                "ttsProvider": (await self._voice_info("tts"))["id"],
                "toolCount": len(self.tools.enabled_catalog()),
                "stats": dict(self.stats),
                "toolLog": list(self._tool_log),
                "sseClients": len(self._subscribers),
                "conversations": self.delivery.known_conversations()[:20],
                # 渠道卡用：最近一条从哪来、这一轮该用哪个出口。
                "lastChannel": "qq" if last else "",
                "lastMessage": last,
                "replyWith": "astrbot_send_message" if last else "",
            }
        )

    async def page_events(self):
        """SSE：连上就推一条快照，之后有消息/连接变化就推。"""

        async def stream():
            queue: asyncio.Queue = asyncio.Queue(maxsize=200)
            self._subscribers.add(queue)
            try:
                yield f"data: {json_dumps({'type': 'hello', 'uptime': self._uptime()})}\n\n"
                yield f"data: {json_dumps(self._snapshot())}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json_dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        # 心跳：让代理和浏览器知道连接还活着
                        yield ": ping\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                self._subscribers.discard(queue)

        return stream_response(
            stream(),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    def _snapshot(self) -> dict:
        now = time.time()
        connected_at = self.server.connected_at
        last = self._recent[-1] if self._recent else None
        return {
            "type": "snapshot",
            "connected": self.server.connected,
            "listening": self.server.listening,
            "client": self.server.client_address,
            "connectedFor": int(now - connected_at) if connected_at else 0,
            "stats": dict(self.stats),
            "recent": list(self._recent),
            "toolLog": list(self._tool_log),
            "conversations": self.delivery.known_conversations()[:20],
            "lastChannel": "qq" if last else "",
            "lastMessage": last,
            "replyWith": "astrbot_send_message" if last else "",
        }

    async def page_tools(self):
        """工具设置：列出可调工具，读写同一个配置项 tools_allow。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            names = payload.get("tools") if isinstance(payload, dict) else None
            if not isinstance(names, list):
                return error_response("tools 必须是列表。", status_code=400)
            picked = []
            for item in names:
                name = _s(item).strip()
                if name and name not in picked:
                    picked.append(name)
            self.config["tools_allow"] = picked
            save = getattr(self.config, "save_config", None)
            if not callable(save):
                return error_response("当前 AstrBot 不支持保存插件配置。", status_code=500)
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                return error_response(f"保存失败：{exc}", status_code=500)
            # 立刻把新清单推给 World，不用等重连。
            self.tools.catalog(force=True)
            await self.server.push(self.tools.frame())
            return json_response({"saved": True, "selected": self.tools.allow_list()})
        allow = self.tools.allow_list()
        return json_response(
            {
                "items": self.tools.catalog(force=True),
                "selected": allow,
                "all": "*" in allow,
            }
        )

    async def page_voice(self):
        """语音设置：读写 provider 选择、TTS 开关与字数上限。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("参数格式不正确。", status_code=400)
            # 先把 provider 校验完，避免校验失败时内存里的开关已经被改了一半。
            wanted_ids: dict[str, str] = {}
            for kind in ("tts", "stt"):
                key = f"{kind}_provider_id"
                if key not in payload:
                    continue
                wanted = _s(payload.get(key)).strip()
                if wanted:
                    known = {_s(item.get("id")) for item in self._voice_list(kind)}
                    if wanted not in known:
                        return error_response(
                            f"没有找到 {kind.upper()} Provider「{wanted}」，"
                            "可能已经被删掉了。",
                            status_code=400,
                        )
                wanted_ids[key] = wanted
            if "enabled" in payload:
                self.config["tts_enabled"] = bool(payload.get("enabled"))
            if "max_chars" in payload:
                try:
                    limit = int(payload.get("max_chars"))
                except (TypeError, ValueError):
                    return error_response("max_chars 必须是整数。", status_code=400)
                self.config["tts_max_chars"] = max(0, min(limit, 1000))
            if "stt_enabled" in payload:
                self.config["stt_enabled"] = bool(payload.get("stt_enabled"))
            for key, wanted in wanted_ids.items():
                self.config[key] = wanted
            save = getattr(self.config, "save_config", None)
            if not callable(save):
                return error_response("当前 AstrBot 不支持保存插件配置。", status_code=500)
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                return error_response(f"保存失败：{exc}", status_code=500)
            # 语音配置变了，顺手刷新 World 那边的 tts 状态。
            with contextlib.suppress(Exception):
                await self.server.push_ready()
            return json_response(
                {
                    "saved": True,
                    "enabled": self._tts_enabled(),
                    "maxChars": self._tts_max_chars(),
                    "sttEnabled": self._stt_enabled(),
                    "ttsProviderId": _s(self.config.get("tts_provider_id", "")),
                    "sttProviderId": _s(self.config.get("stt_provider_id", "")),
                }
            )
        return json_response(
            {
                "stt": await self._voice_info("stt"),
                "tts": await self._voice_info("tts"),
                "providers": {
                    "tts": self._voice_list("tts"),
                    "stt": self._voice_list("stt"),
                },
                "enabled": self._tts_enabled(),
                "maxChars": self._tts_max_chars(),
                "sttEnabled": self._stt_enabled(),
                "ttsProviderId": _s(self.config.get("tts_provider_id", "")),
                "sttProviderId": _s(self.config.get("stt_provider_id", "")),
            }
        )

    async def page_voice_preview(self):
        """合成一段试听音频，返回 base64。"""
        payload = await request.json(default={})
        text = _s(payload.get("text") if isinstance(payload, dict) else "").strip()
        if not text:
            return error_response("先写一句要读的话。", status_code=400)
        if len(text) > 300:
            return error_response("试听最多 300 字。", status_code=400)
        provider = await self._voice_provider("tts")
        if provider is None:
            wanted = _s(self.config.get("tts_provider_id", "")).strip()
            if wanted:
                return error_response(
                    f"配置的 TTS Provider「{wanted}」现在不可用。", status_code=400
                )
            return error_response("AstrBot 里还没有可用的 TTS Provider。", status_code=400)
        path = ""
        try:
            try:
                path = _s(
                    await asyncio.wait_for(provider.get_audio(text), timeout=30)
                )
            except asyncio.TimeoutError:
                return error_response("TTS 超过 30 秒没有返回。", status_code=500)
            except Exception as exc:  # noqa: BLE001
                return error_response(f"TTS 失败：{exc}", status_code=500)
            if not path:
                return error_response("TTS 没有返回音频文件。", status_code=500)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception as exc:  # noqa: BLE001
                return error_response(f"读不到 TTS 音频：{exc}", status_code=500)
            if len(data) > 3 * 1024 * 1024:
                return error_response("TTS 音频超过 3MB。", status_code=500)
            mime = mimetypes.guess_type(path)[0] or "audio/mpeg"
            return json_response(
                {
                    "audio": base64.b64encode(data).decode("ascii"),
                    "mime": mime,
                    "chars": len(text),
                }
            )
        finally:
            # 试听文件读完就没用了；TTS provider 每次都会生成新文件，删掉不心疼。
            if path:
                with contextlib.suppress(Exception):
                    os.remove(path)

    async def page_voice_test(self):
        """把一段语音发到最近联系过的 QQ 会话，方便实机试听。"""
        payload = await request.json(default={})
        text = _s(payload.get("text") if isinstance(payload, dict) else "").strip()
        if not text:
            return error_response("先写一句要读的话。", status_code=400)
        if len(text) > 300:
            return error_response("试听最多 300 字。", status_code=400)
        convs = self.delivery.known_conversations()
        if not convs:
            return error_response("还没有收到过 QQ 消息，先让群里说一句再试。", status_code=400)
        parts = _s(convs[0].get("key")).split("|")
        if len(parts) != 3:
            return error_response("最近的会话标识不完整。", status_code=400)
        platform, kind, conv_id = parts
        ok, note, used_voice = await self.delivery.send(
            platform, kind, conv_id, text, "", None, True
        )
        if not ok:
            return error_response(note or "发送失败", status_code=500)
        return json_response(
            {
                "sent": True,
                "voice": used_voice,
                "note": note,
                "conv": convs[0].get("key", ""),
            }
        )

    async def page_restart(self):
        """重开监听：改完端口、token，或怀疑卡住时用。"""
        # 让「重开监听」真的按当前配置重新绑定，而不是继续用 init 时的旧值。
        self.server.host = _s(self.config.get("listen_host", "0.0.0.0")) or "0.0.0.0"
        self.server.port = self._port()
        self.server.token = _s(self.config.get("token", ""))
        try:
            await self.server.stop()
            ok = await self.server.start()
        except Exception as exc:  # noqa: BLE001
            return error_response(f"重开失败：{exc}", status_code=500)
        if not ok:
            return error_response(
                f"{self.server.host}:{self.server.port} 监听失败，端口可能被占用。",
                status_code=500,
            )
        self._broadcast(
            {
                "type": "info",
                "ts": _now_iso(),
                "text": f"已重开监听 {self.server.host}:{self.server.port}",
            }
        )
        return json_response({"restarted": True})

    async def page_reset(self):
        self.stats = {
            "pushed": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "vision": 0,
            "voices": 0,
            "images": 0,
        }
        self._recent.clear()
        self._tool_log.clear()
        self._seen.clear()
        return json_response({"reset": True})
