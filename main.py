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
import secrets
import sys
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

from .bridge import (
    MAX_IMAGE_B64,
    MAX_IMAGE_TOTAL_B64,
    BridgeServer,
    is_loopback_host,
    looks_like_command,
    matches_command,
)
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

# 单条消息最多转述/打包几张图；正文与帧必须用同一个上限。
MAX_IMAGES = 2
# 对端不能读图、又没有视觉模型时，正文里要给出的明确原因。
_IMAGE_NO_VISION = "对端模型不能读图，且没有可用的视觉模型转述"


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    """面板/配置里的布尔值：`"false"`、`"0"`、`"off"` 都算 False。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _s(value).strip().lower()
    if not text:
        return default
    return text not in ("0", "false", "no", "off", "none")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _read_plugin_version() -> str:
    """从同目录 metadata.yaml 读版本，避免面板里再手写一份。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001 - 读不到就不显示版本
        pass
    return ""


PLUGIN_VERSION = _read_plugin_version()


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
            host=self._listen_host(),
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
        # 没有平台 message_id 的适配器：短窗口内防重复投递，不参与 120s 去重。
        self._fallback_seen: OrderedDict[str, float] = OrderedDict()
        # 退化消息 id 的进程内序号，保证同一纳秒也不撞。
        self._msg_seq = 0
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
        # AstrBot 已注册的指令名；用于识别「不带唤醒前缀」的指令。
        self._cmd_names: set[str] = set()
        self._cmd_names_at = 0.0
        # SSE 订阅者（每个打开面板的浏览器一个队列）
        self._subscribers: set[asyncio.Queue] = set()
        self.started_at = time.time()

        if HAS_WEB_API:
            for route, handler, methods, desc in (
                ("status", self.page_status, ["GET"], "桥的状态与配置"),
                ("groups", self.page_groups, ["GET", "POST"], "群聊接管设置"),
                ("private", self.page_private, ["GET", "POST"], "私聊接管设置"),
                ("tools", self.page_tools, ["GET", "POST"], "给 Cortico 用的工具设置"),
                ("voice", self.page_voice, ["GET", "POST"], "语音输入/输出设置"),
                ("voice/preview", self.page_voice_preview, ["POST"], "合成一段试听语音"),
                ("voice/test", self.page_voice_test, ["POST"], "把试听语音发到最近的会话"),
                ("events", self.page_events, ["GET"], "实时事件流（SSE）"),
                ("restart", self.page_restart, ["POST"], "重开监听"),
                ("token", self.page_token, ["GET", "POST"], "桥的访问 token"),
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

    def _remember_in(self, entry: dict) -> None:
        """入站消息/通知只在这一个地方进流水并广播，避免两处漂移。"""
        self._recent.append(entry)
        self._broadcast({"type": "message", **entry})

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

    def _listen_host(self) -> str:
        """监听地址缺省/留空一律回环；跨机必须显式写 0.0.0.0 并配 token。"""
        raw = self.config.get("listen_host", None)
        host = _s(raw).strip()
        if not host:
            logger.info(
                "Cortico Bridge: listen_host 未配置，按 127.0.0.1 启动；"
                "跨机请显式设 0.0.0.0 并配 token"
            )
            return "127.0.0.1"
        return host

    def _observe_groups(self) -> list[str]:
        """旧版群列表；只用于迁移，新代码用 group_allow。"""
        raw = self.config.get("observe_groups", [])
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _group_scope(self) -> str:
        """群范围：all / allowlist。旧配置按 observe_groups 是否为空推导。"""
        raw = self.config.get("group_scope")
        if raw in (None, ""):
            return "allowlist" if self._observe_groups() else "all"
        return "allowlist" if _s(raw).strip().lower() == "allowlist" else "all"

    def _group_allow(self) -> list[str]:
        """指定群列表；旧配置回退到 observe_groups。"""
        raw = self.config.get("group_allow")
        if raw is None:
            return self._observe_groups()
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _group_mode(self) -> str:
        """群消息范围：mention / all。旧配置按 observe_all 推导。"""
        raw = self.config.get("group_mode")
        if raw in (None, ""):
            return "all" if bool(self.config.get("observe_all", False)) else "mention"
        return "all" if _s(raw).strip().lower() == "all" else "mention"

    def _bridge_platforms(self) -> list[str]:
        """只接管这些 AstrBot 平台；其他平台（webchat/console/default1 等）完全不碰。"""
        raw = self.config.get("bridge_platforms", ["default"])
        if isinstance(raw, str):
            items = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        elif isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = []
        return items or ["default"]

    def _platform_allowed(self, event: AstrMessageEvent) -> bool:
        umo = self._umo_of(event)
        platform = umo.split(":", 1)[0] if ":" in umo else _s(event.get_platform_id())
        return platform in self._bridge_platforms()

    def _private_mode(self) -> str:
        raw = self.config.get("private_mode")
        # 旧配置没有这个键：保持原来“私聊全收”的行为，避免升级后突然断私聊。
        if raw in (None, ""):
            return "all"
        mode = _s(raw).strip().lower()
        return mode if mode in ("off", "allowlist", "all") else "all"

    def _private_allow(self) -> list[str]:
        raw = self.config.get("private_allow", [])
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _private_admin_bypass(self) -> bool:
        return bool(self.config.get("private_admin_bypass", True))

    def _private_deny(self) -> str:
        mode = _s(self.config.get("private_deny", "ignore")).strip().lower()
        return mode if mode in ("ignore", "astrbot") else "ignore"

    def _umo_of(self, event: AstrMessageEvent) -> str:
        return _s(getattr(event, "unified_msg_origin", "")).strip()

    def _normalize_scope_items(
        self, items: list[str], kind: str, platform: str | None = None
    ) -> list[str]:
        """把旧群号 / QQ 号归一化成 UMO；已经是 UMO 的原样保留。

        多平台时 page_* 可以传 payload 里的 `platform`（必须属于
        bridge_platforms）；不传才回退到第一个平台。
        """
        platforms = self._bridge_platforms() or ["default"]
        chosen = platform if platform in platforms else platforms[0]
        msg_type = "GroupMessage" if kind == "group" else "FriendMessage"
        out: list[str] = []
        for item in items:
            value = _s(item).strip()
            if not value:
                continue
            if ":" not in value:
                value = f"{chosen}:{msg_type}:{value}"
            if value not in out:
                out.append(value)
        return out

    def _token_value(self) -> str:
        """当前 token；空表示本机免密（只允许回环监听）。"""
        return _s(self.config.get("token", "")).strip()

    def _wake_prefixes(self) -> list[str]:
        """AstrBot 的指令前缀；读不到就按默认的 / 处理。"""
        try:
            cfg = self.context.get_config() or {}
            raw = cfg.get("wake_prefix")
        except Exception:  # noqa: BLE001
            raw = None
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, list):
            items = [str(x) for x in raw]
        else:
            items = ["/"]
        out = [p.strip() for p in items if p.strip()]
        return out or ["/"]

    def _raw_message_text(self, event: AstrMessageEvent) -> str:
        """唤醒检查会把 message_str 的前缀剥掉；原始组件里还留着 /。"""
        parts: list[str] = []
        try:
            for comp in event.get_messages():
                text = getattr(comp, "text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
        except Exception:  # noqa: BLE001
            return ""
        return "".join(parts)

    def _command_names(self) -> set[str]:
        """已注册的 AstrBot 指令名；30 秒缓存，读不到就退化成空前缀判断。"""
        now = time.time()
        if self._cmd_names and now - self._cmd_names_at < 30:
            return self._cmd_names
        names: set[str] = set()
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for handler in star_handlers_registry:
                for event_filter in getattr(handler, "event_filters", []) or []:
                    getter = getattr(event_filter, "get_complete_command_names", None)
                    if not callable(getter):
                        continue
                    try:
                        names.update(getter() or [])
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            names = set()
        self._cmd_names = names
        self._cmd_names_at = now
        return names

    def _looks_like_command(self, event: AstrMessageEvent) -> bool:
        prefixes = self._wake_prefixes()
        if looks_like_command(event.get_message_str(), prefixes):
            return True
        if looks_like_command(self._raw_message_text(event), prefixes):
            return True
        # 私聊默认不需要唤醒前缀；这时靠「首词命中已注册指令名」判断。
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        return matches_command(event.get_message_str(), self._command_names())

    def _matches_scope(self, event: AstrMessageEvent, items: list[str]) -> bool:
        """UMO 优先；旧配置里的群号/QQ 号继续兼容。"""
        if not items:
            return False
        umo = self._umo_of(event)
        if umo and umo in items:
            return True
        if event.is_private_chat():
            sid = _s(event.get_sender_id() or event.get_session_id())
        else:
            sid = _s(event.get_group_id())
        return bool(sid) and sid in items

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return True
        if self._group_scope() == "all":
            return True
        return self._matches_scope(event, self._group_allow())

    def _private_allowed(self, event: AstrMessageEvent) -> bool:
        if not event.is_private_chat():
            return True
        mode = self._private_mode()
        if mode == "off":
            return False
        if mode == "all":
            return True
        if self._private_admin_bypass() and self._is_admin(event):
            return True
        return self._matches_scope(event, self._private_allow())

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
        token = self._token_value()
        logger.info(
            "Cortico Bridge: 启动 —— enabled=%s, 监听=%s:%s, token=%s, "
            "B模式=%s, 群=%s, 称呼=%r"
            % (
                self.config.get("enabled", False),
                self.server.host,
                self.server.port,
                f"<{len(token)} chars>" if token else "<empty>",
                self._group_mode(),
                self._group_scope() + ":" + ",".join(self._group_allow()),
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

        if not self.config.get("enabled", False):
            # 没启用就不开端口：插件可以装着，但不接客。
            self.server.listening = False
            logger.info("Cortico Bridge: 未启用，不开放监听口")
            return

        await self.server.start()

    async def terminate(self):
        self.delivery.flush()
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

    async def _on_tool_call(
        self, name: str, args: dict, turn: str, actor: dict | None = None
    ) -> tuple[bool, str]:
        """执行一个 AstrBot 工具。

        QQ 轮次用触发消息的身份；Coo 对话轮次用连接页里指定的主人 QQ。
        """
        if not self.config.get("enabled", False):
            return False, "桥未启用，工具调用被拒绝"
        trusted = False
        if turn:
            hit = self._turns.get(turn)
            if hit is None:
                return False, "这一轮的消息已经过期，请重新说一次"
            stamp, event = hit
            if time.time() - stamp > 600:
                self._turns.pop(turn, None)
                return False, "这一轮的消息已经过期，请重新说一次"
            # T-001：只按 AstrBot 管理员名单判断；不信任 World 帧里的 role/owner。
            trusted = _s(event.get_sender_id()) in self._admin_ids()
            who = f"消息 {turn}"
        else:
            owner_id = ""
            if isinstance(actor, dict) and _s(actor.get("kind")) == "owner":
                owner_id = _s(actor.get("id")).strip()
            if not owner_id:
                return False, "这一轮没有 QQ 消息身份；要在这里调工具，先在 Coo 的连接设置里填主人 QQ"
            if owner_id not in self._admin_ids():
                return False, f"{owner_id} 不在 AstrBot 的管理员名单里"
            trusted = True
            platform = (self._bridge_platforms() or ["default"])[0]
            try:
                from .owner_event import build_owner_event

                event = build_owner_event(
                    self.context, platform, owner_id, self.server.self_id
                )
            except Exception as exc:  # noqa: BLE001
                return False, f"当前 AstrBot 版本不能为 Coo 轮次构造工具身份：{exc}"
            who = f"主人 {owner_id}"
        logger.info("Cortico Bridge: 执行工具 %s（来自 %s）" % (name, who))
        ok, text = await self.tools.execute(name, args, event, trusted=trusted)
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
        umo: str = "",
    ) -> tuple[bool, str, bool, bool, str]:
        if not self.config.get("enabled", False):
            return False, "桥未启用，发送门是关的", False, False, "door_closed"
        ok, note, used_voice, uncertain = await self.delivery.send(
            platform, conv_kind, conv_id, text, reply_to, image, voice, umo
        )
        if ok:
            self.stats["sent"] += 1
            if used_voice:
                self.stats["voices"] = self.stats.get("voices", 0) + 1
            if image is not None:
                self.stats["images"] = self.stats.get("images", 0) + 1
            self._remember_out(conv_kind, conv_id, text, image, used_voice, umo)
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
            # 结果未知和「确定没发」要分开说，否则模型会直接重发造成重复。
            prefix = "发送结果未知" if uncertain else "发送失败"
            logger.warning(f"Cortico Bridge: {prefix} conv={conv_kind}:{conv_id} —— {note}")
            self._broadcast(
                {
                    "type": "error",
                    "ts": _now_iso(),
                    "text": f"{prefix} {conv_kind}:{conv_id} —— {note}",
                }
            )
        return ok, note, used_voice, uncertain, ("" if ok else self.delivery.last_error_code)

    def _remember_out(
        self,
        conv_kind: str,
        conv_id: str,
        text: str,
        image: dict | None = None,
        voice: bool = False,
        umo: str = "",
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
            "umo": umo,
            "sender": "Coo",
            "text": ("[语音] " + text[:200]).strip() if voice else ("[图片] " + text[:200]).strip() if image else text[:200],
            "image": image is not None,
            "voice": voice,
        }
        self._remember_in(entry)

    # ---------- 消息进 Cortico ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-sys.maxsize)
    async def observe_all_messages(self, event: AstrMessageEvent):
        """最后一个跑：只标记这一轮归 Coo，不抢在其他插件前面 stop。

        指令、自己轮询的插件先执行；它们 stop 了，就不会走到 LLM 层。
        没人认领的消息由 on_llm_request 推给 Coo。
        通知类没有正文、进不了 LLM 阶段，所以仍然在这里直接推走。
        """
        if not self.config.get("enabled", False):
            return
        if not self._platform_allowed(event):
            umo = self._umo_of(event)
            platform = umo.split(":", 1)[0] if ":" in umo else _s(event.get_platform_id())
            logger.info(
                "Cortico Bridge: 跳过非接管平台 %s（bridge_platforms=%s）"
                % (platform, ",".join(self._bridge_platforms()))
            )
            return

        self._remember_self(event)
        if self._looks_like_command(event):
            logger.info(
                "Cortico Bridge: 指令交给 AstrBot —— %r", event.get_message_str()[:40]
            )
            return
        if event.is_private_chat() or self._group_allowed(event):
            logger.info(
                "Cortico Bridge: 事件进入 private=%s text=%r"
                % (event.is_private_chat(), event.get_message_str()[:60])
            )

        # 通知类事件先处理：撤回、入群、退群、戳一戳、上传文件。
        notice, ref = self._notice_text(event)
        if notice:
            # A-9/P-003：只有真的推给 Cortico 才拦下来；桥断开时放它继续走，
            # 其他插件仍能看到撤回/入群。范围过滤保留在 _push_notice 内部。
            pushed = await self._push_notice(event, notice, ref)
            if pushed:
                event.stop_event()
            return

        # 桥没连上时不要强制唤醒：否则 AstrBot 自己的模型会接管整个群，
        # Cortico 一断就开始刷屏。
        if not self.server.connected:
            return

        # 私聊和 B 模式群消息只做标记；真正推送留到 LLM 层。
        # 这样指令和「自己轮询」的插件可以先执行并 stop，Coo 不会抢在它们前面。
        if event.is_private_chat():
            if not self._private_allowed(event):
                if self._private_deny() == "ignore":
                    event.stop_event()
                return
            event.set_extra("_cortico_llm_owned", "private")
            return

        # A-1：先过群名单，再过 mention 门；别名与回复也算点名。
        # AstrBot 4.29.1 的 ProcessStage 在 activated handlers 全部执行完后才
        # 读 is_at_or_wake_command；本 handler priority=-sys.maxsize 最后执行，
        # 所以这里晚设置有效。真正起作用的是 is_at_or_wake_command，不是 is_wake。
        if not self._group_allowed(event):
            return
        if self._group_mode() == "mention":
            text = self._raw_message_text(event) or event.get_message_str()
            if not self._is_called_for_wake(event, text):
                return
            event.is_at_or_wake_command = True
            event.set_extra("_cortico_llm_owned", "group")
            # 事件层直接推送是主路径，不依赖管线顺序；成功的 _push_event 会
            # 先置 _cortico_pushed 再 stop，on_llm_request 不会二次推送。
            if event.is_stopped():
                return
            await self._push_event(event)
            return

        # all：仍走 LLM 路径，不在这里直接推。
        event.is_at_or_wake_command = True
        event.set_extra("_cortico_llm_owned", "group")

    @filter.on_llm_request(priority=sys.maxsize)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """把这一轮交给 Cortico，然后让 AstrBot 自己的模型退出。"""
        if not self.config.get("enabled", False):
            return
        if not self._platform_allowed(event):
            return
        if not self.server.connected:
            logger.info("Cortico Bridge: Cortico 没连上，本轮交给 AstrBot")
            return
        if not _s(event.get_extra("_cortico_llm_owned", "")).strip():
            return
        # 事件层已经直接推过（或已经停掉这一轮）就不再推第二次。
        if event.is_stopped() or bool(event.get_extra("_cortico_pushed", False)):
            return
        if self._looks_like_command(event):
            logger.info(
                "Cortico Bridge: 指令交给 AstrBot —— %r", event.get_message_str()[:40]
            )
            return
        await self._push_event(event)

    async def _push_event(
        self, event: AstrMessageEvent, text: str | None = None
    ) -> bool:
        """统一推送入口：策略、去重、图片、统计和会话记录只在这里做一次。"""
        if not self.server.connected:
            return False
        if not self._platform_allowed(event):
            return False

        if text is None:
            text = await self._message_text(event)
        if not text:
            return False

        conv_kind = "private" if event.is_private_chat() else "group"
        conv_id = (
            _s(event.get_group_id())
            if conv_kind == "group"
            else _s(event.get_sender_id() or event.get_session_id())
        )
        if not conv_id:
            return False

        self._remember_name(_s(event.get_sender_id()), _s(event.get_sender_name()))

        if conv_kind == "group":
            if not self._group_allowed(event):
                logger.debug(f"Cortico Bridge: 群 {conv_id} 不在接管名单里，本轮交给 AstrBot")
                return False
        elif not self._private_allowed(event):
            logger.debug(f"Cortico Bridge: 私聊 {conv_id} 不在允许名单里")
            if self._private_deny() == "ignore":
                event.stop_event()
            return False

        platform_id = _s(event.get_platform_id())
        is_admin = self._is_admin(event)
        self.delivery.remember(platform_id, conv_kind, conv_id, event.unified_msg_origin)

        msg_id = self._message_id(event)
        has_platform_id = self._has_platform_id(event)
        fallback_key = "" if has_platform_id else self._fallback_key(event)
        # 无平台 id 的适配器：3 秒内同一事件重复投递只处理一次，同文新消息
        # 因为时间/序号不同仍会进 Coo。
        if fallback_key and self._is_fallback_duplicate(fallback_key):
            self.stats["skipped"] += 1
            logger.debug(f"Cortico Bridge: 重复投递已跳过 key={fallback_key}")
            event.stop_event()
            return False
        if self._is_duplicate(msg_id):
            self.stats["skipped"] += 1
            logger.debug(f"Cortico Bridge: 重复消息已跳过 id={msg_id}")
            event.stop_event()
            return False

        conv_name = ""
        if conv_kind == "group":
            with contextlib.suppress(Exception):
                group = await event.get_group()
                conv_name = _s(getattr(group, "group_name", "") if group else "")

        frame = {
            "t": "msg",
            "channel": "qq",
            "id": msg_id,
            "umo": _s(event.unified_msg_origin).strip(),
            "ts": _now_iso(),
            "conv": {"kind": conv_kind, "id": conv_id, "name": conv_name},
            "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
            "text": text,
            "mentioned": self._is_called(event, text),
            "platform": platform_id,
            "role": "admin" if is_admin else "member",
            "owner": is_admin,
            "can": {"screenshot": is_admin},
        }

        images, image_errors, image_marks = await self._image_payloads(event)
        if images:
            frame["images"] = images
        if image_errors:
            frame["image_errors"] = image_errors
        if image_marks:
            frame["text"] = _apply_image_marks(text, image_marks)

        if not await self.server.push(frame):
            logger.warning("Cortico Bridge: 推送失败，本轮交给 AstrBot")
            if has_platform_id and msg_id:
                self._seen.pop(msg_id, None)
            elif fallback_key:
                self._fallback_seen.pop(fallback_key, None)
            return False

        # R1：push 成功后立刻置位，再写 UI/广播/日志/stop。后面任何一步抛错，
        # on_llm_request 都不会再推一次；push=False 时没有置位，允许它重试。
        event.set_extra("_cortico_pushed", True)
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
            "umo": frame.get("umo", ""),
            "sender": frame["sender"]["name"] or frame["sender"]["id"],
            "text": preview[:200],
            "mentioned": frame["mentioned"],
        }
        self._remember_in(entry)
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

        event.stop_event()
        if msg_id:
            self._turns[msg_id] = (time.time(), event)
            self._turns.move_to_end(msg_id)
            while len(self._turns) > 100:
                self._turns.popitem(last=False)
        return True

    # ---------- 小工具 ----------

    async def _message_text(self, event: AstrMessageEvent) -> str:
        """取消息正文。

        文字直接用；语音交给 AstrBot 的 STT provider 转写；图片按 `image_plan()`
        决定：native 只留占位，describe 换成视觉描述，不能做时给出明确原因。
        """
        parts: list[str] = []
        text = event.get_message_str().strip()
        if text:
            parts.append(text)

        plan = self.image_plan()
        image_index = 0
        for comp in event.get_messages():
            name = type(comp).__name__
            if name == "Record":
                spoken = await self._transcribe(
                    comp, _s(getattr(event, "unified_msg_origin", ""))
                )
                parts.append(f"[语音] {spoken}" if spoken else "[语音]")
                continue
            if name == "Image":
                slot = image_index
                image_index += 1
                if plan["native"]:
                    # 原图会随帧发过去，正文只留占位；取图失败由 image_marks 改写。
                    parts.append("[图片]")
                elif plan["describe"]:
                    if slot < MAX_IMAGES:
                        desc, reason = await self._describe_image(comp)
                        if desc:
                            parts.append(f"[图片：{desc}]")
                        elif reason:
                            parts.append(f"[图片|转述失败：{reason}]")
                        else:
                            parts.append(f"[图片|{_IMAGE_NO_VISION}]")
                    else:
                        parts.append("[图片|超出单次上限，未转述]")
                elif plan["reason"] == "peer_text_only":
                    parts.append(f"[图片|{_IMAGE_NO_VISION}]")
                else:
                    # off：不转述、不传原图，只保留占位。
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

    def image_plan(self, event: AstrMessageEvent | None = None) -> dict:
        """这一轮图片怎么处理：native（传原图）/ describe（转述）/ 都不做。

        返回 `{native, describe, reason}`。`_message_text` 与 `_image_payloads`
        共用同一份计划，避免一处丢图、一处又塞原图。`event` 预留，当前不读取。
        """
        mode = self._image_mode()
        if mode == "off":
            return {"native": False, "describe": False, "reason": "off"}

        if self._peer_images():
            if mode in ("auto", "image"):
                return {"native": True, "describe": False, "reason": ""}
            return {"native": False, "describe": True, "reason": "describe"}

        # 对端不能收图：auto/image 都不得传原图；有视觉模型就转述。
        if mode == "describe":
            return {"native": False, "describe": True, "reason": "describe"}
        if self._vision_provider_id():
            return {
                "native": False,
                "describe": True,
                "reason": "peer_text_only_describe",
            }
        return {"native": False, "describe": False, "reason": "peer_text_only"}

    def _should_describe(self) -> bool:
        """这一轮要不要花一次视觉调用把图转述成文字。"""
        return bool(self.image_plan().get("describe"))

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

    async def _describe_image(self, comp) -> tuple[str, str]:
        """把一张图变成一句话；返回 (描述, 失败原因)。

        失败不再静默退化成 `[图片]`：调用方用 reason 写 `[图片|转述失败：...]`，
        让模型知道这张图存在但没读到，而不是以为没有内容。
        """
        provider_id = self._vision_provider_id()
        if not provider_id:
            return "", "没有可用的视觉模型"

        source = await self._image_source(comp)
        if not source:
            return "", "取不到图片内容"

        key = source if source.startswith("http") else "sha1:" + hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()
        cached = self._image_cache.get(key)
        if cached is not None:
            self._image_cache.move_to_end(key)
            return cached, ""

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
            # 超时类的异常 str() 是空的；异常原文只进日志，不进正文。
            reason = str(exc).strip() or type(exc).__name__
            logger.warning(
                "Cortico Bridge: 识图失败 provider=%s —— %s" % (provider_id, reason)
            )
            return "", "视觉模型调用失败"

        if not desc:
            return "", "视觉模型返回了空描述"

        desc = " ".join(desc.split())[:200]
        self._image_cache[key] = desc
        while len(self._image_cache) > 200:
            self._image_cache.popitem(last=False)
        self.stats["vision"] = self.stats.get("vision", 0) + 1
        logger.info(
            "Cortico Bridge: 识图完成 provider=%s %d 字" % (provider_id, len(desc))
        )
        return desc, ""

    # ---------- 入站图片（对端能收图时才传） ----------

    async def _image_payloads(
        self, event: AstrMessageEvent
    ) -> tuple[list[dict], list[dict], list[str]]:
        """把消息里的图片打包进帧；返回 (图片, 失败清单, 每张图的占位标记)。

        只有 `image_plan()` 的 native 才打包原图；转述/忽略时返回空帧字段，
        并带上原因（`describe` / `peer_text_only`），不再静默丢掉。
        """
        plan = self.image_plan(event)
        images = [
            comp for comp in event.get_messages() if type(comp).__name__ == "Image"
        ]
        if not plan["native"]:
            if plan["describe"]:
                reason = "describe"
            elif plan["reason"] == "peer_text_only":
                reason = "peer_text_only"
            else:
                return [], [], []
            errors = [{"index": i, "reason": reason} for i in range(len(images))]
            return [], errors, []

        payloads: list[dict] = []
        errors: list[dict] = []
        marks: list[str] = []
        index = 0
        total_b64 = 0
        for comp in images:
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
            if len(payloads) >= MAX_IMAGES:
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
        if conv_kind == "group" and not self._group_allowed(event):
            return False
        if conv_kind == "private" and not self._private_allowed(event):
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
            "umo": _s(event.unified_msg_origin).strip(),
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
        entry = {
            "ts": frame["ts"],
            "dir": "in",
            "channel": "qq",
            "role": frame["role"],
            "owner": bool(frame.get("owner")),
            "notice": True,
            "conv": f"{conv_kind}:{conv_id}",
            "convName": conv_name,
            "umo": frame.get("umo", ""),
            "sender": "通知",
            "text": text[:200],
            "mentioned": False,
        }
        self._remember_in(entry)
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
            result = await asyncio.wait_for(provider.get_text(source), timeout=20)
            return _s(result).strip()
        except asyncio.TimeoutError:
            logger.warning("Cortico Bridge: 语音转写超过 20 秒，本条按无转写处理")
            return ""
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

    def _has_platform_id(self, event: AstrMessageEvent) -> bool:
        for attr in ("message_id", "id"):
            if getattr(event.message_obj, attr, None):
                return True
        return False

    def _component_signature(self, event: AstrMessageEvent) -> str:
        try:
            names = [type(comp).__name__ for comp in event.get_messages()]
        except Exception:  # noqa: BLE001
            return ""
        return ",".join(sorted(names))

    def _event_timestamp(self, event: AstrMessageEvent) -> int:
        """message_obj.time/timestamp 可能是 datetime、秒或毫秒；统一成 int。"""
        obj = getattr(event, "message_obj", None)
        for attr in ("time", "timestamp", "time_stamp"):
            value = getattr(obj, attr, None)
            if value is None:
                continue
            try:
                stamp = getattr(value, "timestamp", None)
                if callable(stamp):
                    value = stamp()
                return int(float(value))
            except (TypeError, ValueError, OverflowError):
                continue
        return 0

    def _fallback_key(self, event: AstrMessageEvent) -> str:
        """退化投递的稳定指纹：同一条事件重复投递会命中，同文新消息不会。"""
        raw = "|".join(
            (
                event.get_message_str(),
                _s(event.get_sender_id()),
                self._component_signature(event),
                _s(self._event_timestamp(event)),
            )
        )
        return "f" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _is_fallback_duplicate(self, key: str) -> bool:
        now = time.time()
        seen_at = self._fallback_seen.get(key)
        if seen_at is not None and now - seen_at < 3:
            return True
        self._fallback_seen[key] = now
        # 顺手清掉过期项；容量也封顶，长期运行不无限增长。
        while self._fallback_seen:
            first_key, first_at = next(iter(self._fallback_seen.items()))
            if now - first_at < 3 and len(self._fallback_seen) <= 500:
                break
            self._fallback_seen.pop(first_key, None)
        return False

    def _is_called_for_wake(self, event: AstrMessageEvent, text: str) -> bool:
        """mention 模式是否点名：@ 自己 / 回复自己 / 正文命中别名。

        `At(qq="all")`（@全体）不算点名，避免整群被唤醒；`_is_called` 写帧里的
        `mentioned` 用同一口径。
        """
        self_id = _s(event.get_self_id())
        with contextlib.suppress(Exception):
            for comp in event.get_messages():
                name = type(comp).__name__
                if name == "At":
                    qq = _s(getattr(comp, "qq", ""))
                    if qq and qq != "all" and qq == self_id:
                        return True
                elif name == "Reply" and self_id:
                    if _s(getattr(comp, "sender_id", "")) == self_id:
                        return True
        return any(a in text for a in self._aliases())

    def _is_called(self, event: AstrMessageEvent, text: str) -> bool:
        """有没有真的叫到名字；@ 自己、回复自己、正文里出现配置的称呼。"""
        return self._is_called_for_wake(event, text)

    def _message_id(self, event: AstrMessageEvent) -> str:
        # 真实平台 id 原样使用，按 120s 去重。
        for attr in ("message_id", "id"):
            value = getattr(event.message_obj, attr, None)
            if value:
                return _s(value)
        # 没有平台 id：生成唯一 id，同秒两条纯图片/语音也不会撞，可安全写 _turns。
        self._msg_seq += 1
        raw = "|".join(
            (
                event.get_message_str(),
                _s(event.get_sender_id()),
                str(time.time_ns()),
                str(self._msg_seq),
                self._component_signature(event),
            )
        )
        return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

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
                "version": PLUGIN_VERSION,
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
                "observeAll": self._group_mode() == "all",
                "groups": self._group_allow() if self._group_scope() == "allowlist" else [],
                "groupScope": self._group_scope(),
                "groupAllow": self._group_allow(),
                "groupMode": self._group_mode(),
                "bridgePlatforms": self._bridge_platforms(),
                "privateMode": self._private_mode(),
                "privateAllow": self._private_allow(),
                "privateAdminBypass": self._private_admin_bypass(),
                "privateDeny": self._private_deny(),
                "aliases": self._aliases(),
                "host": self.server.host,
                "port": self.server.port,
                "loopback": is_loopback_host(self.server.host),
                "originRefused": self.server.origin_refused,
                "queryTokenRefused": self.server.query_token_refused,
                "badTokenRefused": self.server.bad_token_refused,
                "versionRefused": self.server.version_refused,
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
            "listenError": self.server.listen_error,
            "originRefused": self.server.origin_refused,
            "queryTokenRefused": self.server.query_token_refused,
            "badTokenRefused": self.server.bad_token_refused,
            "versionRefused": self.server.version_refused,
            "stats": dict(self.stats),
            "recent": list(self._recent),
            "toolLog": list(self._tool_log),
            "conversations": self.delivery.known_conversations()[:20],
            "lastChannel": "qq" if last else "",
            "lastMessage": last,
            "replyWith": "astrbot_send_message" if last else "",
        }

    def _commit_config(self, changes: dict) -> bool:
        """先校验、写内存、save_config；失败逐键回滚，调用方无需再恢复。"""
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            logger.warning("Cortico Bridge: 当前 AstrBot 不支持保存插件配置")
            return False
        missing = object()
        old = {key: self.config.get(key, missing) for key in changes}
        self.config.update(changes)
        try:
            save()
            return True
        except Exception as exc:  # noqa: BLE001
            for key, value in old.items():
                if value is missing:
                    self.config.pop(key, None)
                else:
                    self.config[key] = value
            logger.warning(f"Cortico Bridge: 保存配置失败，已回滚 —— {exc}")
            return False

    async def page_groups(self):
        """群聊设置：范围、名单和消息范围。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求格式不正确。", status_code=400)
            groups = payload.get("groups", self._group_allow())
            if not isinstance(groups, list):
                return error_response("groups 必须是列表。", status_code=400)
            raw_scope = payload.get("scope")
            scope = (
                _s(raw_scope).strip().lower()
                if raw_scope is not None
                else ("allowlist" if groups else "all")
            )
            if scope not in ("all", "allowlist"):
                return error_response("scope 必须是 all / allowlist。", status_code=400)
            picked = []
            for item in groups:
                value = _s(item).strip()
                if value and value not in picked:
                    picked.append(value)
            platform = _s(payload.get("platform", "")).strip() or None
            picked = self._normalize_scope_items(picked, "group", platform)
            if scope == "allowlist" and not picked:
                return error_response("指定群模式至少要有一个群。", status_code=400)
            mode = _s(payload.get("mode", self._group_mode())).strip().lower()
            if mode not in ("mention", "all"):
                return error_response("mode 必须是 mention / all。", status_code=400)
            # 旧键只认裸群号；完整 UMO 是 platform:type:session，session 里可能
            # 还有冒号（如 123:456），不能用 rsplit 截最后一段。
            mirror = (
                [
                    value.split(":", 2)[2] if value.count(":") >= 2 else value
                    for value in picked
                ]
                if scope == "allowlist"
                else []
            )
            if not self._commit_config(
                {
                    "group_scope": scope,
                    "group_allow": picked,
                    "group_mode": mode,
                    "observe_groups": mirror,
                    "observe_all": mode == "all",
                }
            ):
                return error_response("保存配置失败。", status_code=500)
            return json_response(
                {"saved": True, "scope": scope, "groups": picked, "mode": mode}
            )
        return json_response(
            {
                "scope": self._group_scope(),
                "groups": self._group_allow(),
                "mode": self._group_mode(),
                # 旧字段保留给旧版 UI / 脚本。
                "observeAll": self._group_mode() == "all",
            }
        )

    async def page_private(self):
        """私聊设置：模式、UMO 白名单、管理员豁免、拒绝策略。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求格式不正确。", status_code=400)
            mode = _s(payload.get("mode", self._private_mode())).strip().lower()
            if mode not in ("off", "allowlist", "all"):
                return error_response("mode 必须是 off / allowlist / all。", status_code=400)
            deny = _s(payload.get("deny", self._private_deny())).strip().lower()
            if deny not in ("ignore", "astrbot"):
                return error_response("deny 必须是 ignore / astrbot。", status_code=400)
            allow_raw = payload.get("allow", self._private_allow())
            if not isinstance(allow_raw, list):
                return error_response("allow 必须是列表。", status_code=400)
            allow = []
            for item in allow_raw:
                value = _s(item).strip()
                if value and value not in allow:
                    allow.append(value)
            platform = _s(payload.get("platform", "")).strip() or None
            allow = self._normalize_scope_items(allow, "private", platform)
            admin_bypass = _as_bool(
                payload.get("adminBypass"), self._private_admin_bypass()
            )
            if not self._commit_config(
                {
                    "private_mode": mode,
                    "private_allow": allow,
                    "private_admin_bypass": admin_bypass,
                    "private_deny": deny,
                }
            ):
                return error_response("保存配置失败。", status_code=500)
            return json_response(
                {
                    "saved": True,
                    "mode": mode,
                    "allow": allow,
                    "adminBypass": admin_bypass,
                    "deny": deny,
                }
            )
        return json_response(
            {
                "mode": self._private_mode(),
                "allow": self._private_allow(),
                "adminBypass": self._private_admin_bypass(),
                "deny": self._private_deny(),
            }
        )

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
            if not self._commit_config({"tools_allow": picked}):
                return error_response("保存配置失败。", status_code=500)
            # 立刻把新清单推给 World，不用等重连。
            self.tools.catalog(force=True)
            await self.server.push(self.tools.frame())
            return json_response(
                {
                    "saved": True,
                    "selected": self.tools.allow_list(),
                    "granted": [
                        row.get("name", "")
                        for row in self.tools.enabled_catalog()
                        if row.get("name")
                    ],
                }
            )
        allow = self.tools.allow_list()
        return json_response(
            {
                "items": self.tools.catalog(force=True),
                "selected": allow,
                "all": "*" in allow,
                # 实际会下发给 World 的名字：`*` 的 read/write + 显式 exec。
                "granted": [
                    row.get("name", "")
                    for row in self.tools.enabled_catalog()
                    if row.get("name")
                ],
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
            changes: dict[str, Any] = {}
            if "enabled" in payload:
                changes["tts_enabled"] = _as_bool(payload.get("enabled"))
            if "max_chars" in payload:
                try:
                    limit = int(payload.get("max_chars"))
                except (TypeError, ValueError):
                    return error_response("max_chars 必须是整数。", status_code=400)
                changes["tts_max_chars"] = max(0, min(limit, 1000))
            if "stt_enabled" in payload:
                changes["stt_enabled"] = _as_bool(payload.get("stt_enabled"))
            changes.update(wanted_ids)
            if not self._commit_config(changes):
                return error_response("保存配置失败。", status_code=500)
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
        # A-2/P-002：试听也必须尊重总开关和 TTS 开关，不能绕过 door_closed。
        if not self.config.get("enabled", False):
            return error_response("桥未启用，试听被拒绝（door_closed）。", status_code=400)
        if not self._tts_enabled():
            return error_response(
                "TTS 没有在插件配置里打开（tts_unavailable），已取消试听。",
                status_code=400,
            )
        payload = await request.json(default={})
        text = _s(payload.get("text") if isinstance(payload, dict) else "").strip()
        if not text:
            return error_response("先写一句要读的话。", status_code=400)
        if len(text) > 300:
            return error_response("试听最多 300 字。", status_code=400)
        provider = await self._voice_provider("tts")
        if provider is None:
            return error_response(
                "TTS Provider 现在不可用（tts_unavailable），已取消试听。",
                status_code=400,
            )
        convs = self.delivery.known_conversations()
        if not convs:
            return error_response("还没有收到过 QQ 消息，先让群里说一句再试。", status_code=400)
        umo = _s(convs[0].get("umo") or convs[0].get("key"))
        if not umo:
            return error_response("最近的会话标识不完整。", status_code=400)
        # 试听失败只报错：allow_text_fallback=False 保证不会把文字发到最近会话。
        ok, note, used_voice, _uncertain = await self.delivery.send(
            "", "", "", text, "", None, True, umo, allow_text_fallback=False
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

    async def page_token(self):
        """查看 / 生成 / 清除桥的访问 token。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            action = _s(
                payload.get("action") if isinstance(payload, dict) else ""
            ).strip().lower()
            if action not in ("generate", "clear"):
                return error_response(
                    "action 必须是 generate / clear。", status_code=400
                )
            token = secrets.token_urlsafe(32) if action == "generate" else ""
            # A-3：save 成功后才动 server；保存失败时 config 已回滚，server 不变。
            if not self._commit_config({"token": token}):
                return error_response("保存配置失败。", status_code=500)
            self.server.token = token
            restarted = False
            if self.server.listening:
                await self.server.stop()
                restarted = await self.server.start()
            logger.info(
                "Cortico Bridge: 已%s访问 token"
                % ("生成新的" if action == "generate" else "清除")
            )
            return json_response(
                {
                    "token": token,
                    "tokenSet": bool(token),
                    "restarted": restarted,
                    "listening": self.server.listening,
                    "listenError": self.server.listen_error,
                }
            )
        return json_response(
            {
                "token": self._token_value(),
                "tokenSet": bool(self._token_value()),
                "loopback": is_loopback_host(self.server.host),
            }
        )

    async def page_restart(self):
        """重开监听：改完端口、token，或怀疑卡住时用。"""
        if not self.config.get("enabled", False):
            return error_response(
                "桥未启用，先打开「接管对话」再重开监听。", status_code=400
            )
        # 让「重开监听」真的按当前配置重新绑定，而不是继续用 init 时的旧值。
        self.server.host = self._listen_host()
        self.server.port = self._port()
        self.server.token = self._token_value()
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
