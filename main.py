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
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import time
from collections import OrderedDict, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import errors as codes

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
    parse_umo,
)
from .deliver import Delivery
from .message_segments import (
    LABELS,
    READ_COMPONENT_TOOL,
    ComponentReceipt,
    ComponentPage,
    annotate_images,
    audio_suffix,
    download_qq_media,
    event_segments,
    fetch_forward,
    forward_id,
    render_detail,
)
from .tools import ToolRegistry

PLUGIN_NAME = "astrbot_plugin_cortico_bridge"

# 跨插件热重载保存当前监听实例；模块被重新导入时 builtins 还在。
_ACTIVE_SERVER_ATTR = "_cortico_bridge_active_server"

# 非文字消息的占位说法，避免 B 模式下丢掉上下文
COMPONENT_LABELS = {
    kind: label for kind, label in LABELS.items() if kind not in ("plain", "at")
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
        if not mark:
            continue
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
            desktop_owner=self._desktop_owner,
            desktop_targets=self._desktop_targets,
            owner_umo=self._owner_umo,
            owner_dms=self._owner_dms,
            platform_routes=self._platform_routes,
            discord_target_allowed=self._target_allowed,
            tools_frame=self.tools.frame,
            tts_available=self._tts_info,
            conversations=self.delivery.known_conversations,
            mode=self._initial_bridge_mode(),
            save_mode=self._save_bridge_mode,
        )
        # 最近推送的消息，给面板看
        self._recent: deque[dict] = deque(maxlen=40)
        # 最近的工具调用（谁、成没成、返回了什么），给面板看
        self._tool_log: deque[dict] = deque(maxlen=12)
        # 消息 id → (时间, 事件)：工具用触发这一轮的那条消息的身份执行。
        self._turns: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        self._message_segments: dict[tuple[str, str], ComponentPage] = {}
        self._forward_pages: OrderedDict[tuple[str, str, str], ComponentPage] = OrderedDict()
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
        self._listener_lock = asyncio.Lock()
        self.started_at = time.time()

        if HAS_WEB_API:
            for route, handler, methods, desc in (
                ("status", self.page_status, ["GET"], "桥的状态与配置"),
                ("settings/connection", self.page_connection_settings, ["GET", "POST"], "监听与发送设置"),
                ("settings/qq", self.page_qq_settings, ["GET", "POST"], "QQ 实例与唤醒称呼"),
                ("settings/images", self.page_image_settings, ["GET", "POST"], "图片处理设置"),
                ("access", self.page_access, ["GET", "POST"], "模式审批与桌面授权"),
                ("groups", self.page_groups, ["GET", "POST"], "群聊接管设置"),
                ("private", self.page_private, ["GET", "POST"], "私聊接管设置"),
                ("tools", self.page_tools, ["GET", "POST"], "给 Cortico 用的工具设置"),
                ("tools/risks", self.page_tool_risks, ["GET", "POST"], "工具风险分类"),
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
        raw = self.config.get("listen_port", 17999)
        try:
            value = float(raw) if not isinstance(raw, bool) else 0.0
            if value.is_integer() and 1 <= value <= 65535:
                return int(value)
        except (TypeError, ValueError, OverflowError):
            pass
        logger.warning("Cortico Bridge: 监听端口无效，使用 17999")
        return 17999

    def _initial_bridge_mode(self) -> str:
        mode = self.config.get("bridge_mode")
        if mode in ("safe", "full"):
            return mode
        # 已启用的旧版部署继续使用原权限；新安装从主人私聊开始。
        return "full" if self.config.get("enabled") else "safe"

    def _owner_umo(self) -> str:
        owner = _s(self.config.get("owner_tool_qq")).strip()
        platforms = self._bridge_platforms()
        platform = _s(self.config.get("owner_platform")).strip()
        if not platform and len(platforms) == 1:
            platform = platforms[0]
        if (platform not in platforms or platform in self._discord_routes() or platform in self._discord_instances()
                or len(owner) > 20 or not owner.isascii() or not owner.isdigit()):
            return ""
        return f"{platform}:FriendMessage:{owner}"

    def _is_owner_private(self, event: AstrMessageEvent) -> bool:
        if self._is_discord(event):
            route = self._discord_routes().get(_s(event.get_platform_id()), {})
            umo = _s(route.get("owner_umo"))
            return bool(umo and event.is_private_chat() and self._umo_of(event) == umo
                        and _s(event.get_sender_id()) == route.get("owner_id"))
        owner_umo = self._owner_umo()
        return bool(owner_umo and event.is_private_chat() and self._umo_of(event) == owner_umo
                    and _s(event.get_sender_id()) == owner_umo.rsplit(":", 1)[1])

    def _event_allowed(self, event: AstrMessageEvent) -> bool:
        if self.server.mode == "safe":
            return self._is_owner_private(event) and (not self._is_discord(event) or self._platform_allowed(event))
        return self._platform_allowed(event) and (
            self._private_allowed(event) if event.is_private_chat() else self._group_allowed(event)
        )

    def _desktop_owner(self) -> str:
        owner_umo = self._owner_umo()
        return owner_umo.rsplit(":", 1)[1] if owner_umo and self.config.get("desktop_tools_enabled") is True else ""

    def _desktop_targets(self) -> list[str]:
        targets = self.config.get("desktop_send_targets", [])
        return [target for target in targets if isinstance(target, str)] if isinstance(targets, list) else []

    def _legacy_desktop_needs_review(self) -> bool:
        # 旧的独立令牌只用于识别待迁移配置，不能授权新版连接。
        return bool(self.config.get("owner_tool_token") and self.config.get("owner_tool_qq")
                    and not self.config.get("desktop_auth_reviewed"))

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
        raw = _s(self.config.get("group_scope")).strip().lower()
        if raw in ("", "inherit") or (raw == "all" and not self.config.get("group_allow")):
            return "allowlist" if self._observe_groups() else "all"
        return "allowlist" if raw == "allowlist" else "all"

    def _group_allow(self) -> list[str]:
        """指定群列表；旧配置回退到 observe_groups。"""
        raw = self.config.get("group_allow")
        if not raw and _s(self.config.get("group_scope")).strip().lower() != "allowlist":
            return self._observe_groups()
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in _s(raw).replace("，", ",").split(",") if x.strip()]

    def _group_mode(self) -> str:
        """群消息范围：mention / all。旧配置按 observe_all 推导。"""
        raw = self.config.get("group_mode")
        if raw in (None, "", "inherit"):
            return "all" if _as_bool(self.config.get("observe_all")) else "mention"
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

    def _configured_platforms(self) -> dict[str, dict]:
        try:
            platforms = (self.context.get_config() or {}).get("platform", [])
        except Exception:
            return {}
        if not isinstance(platforms, list):
            return {}
        return {
            str(item["id"]): item for item in platforms
            if isinstance(item, dict) and item.get("id")
        }

    def _discord_instances(self) -> dict[str, dict]:
        return {key: item for key, item in self._configured_platforms().items() if item.get("type") == "discord"}

    def _discord_routes(self) -> dict[str, dict]:
        raw = self.config.get("discord_routes")
        return {key: value for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}

    def _platform_routes(self) -> list[dict]:
        configured = self._configured_platforms()
        routes = self._discord_routes()
        discord = {key: item for key, item in configured.items() if item.get("type") == "discord"}
        instances = {**dict.fromkeys(routes, {"enable": False}), **discord}
        owner = self._owner_umo()
        return [
            {"id": platform, "channel": "qq", "enabled": configured[platform].get("enable") is not False,
             "ownerBound": owner.startswith(f"{platform}:")}
            for platform in self._bridge_platforms()
            if configured.get(platform, {}).get("type") == "aiocqhttp" and platform not in instances
        ] + [
            {"id": platform, "channel": "discord", "enabled": routes.get(platform, {}).get("enabled") is True and item.get("enable") is not False,
             "ownerBound": bool(routes.get(platform, {}).get("owner_umo"))}
            for platform, item in instances.items()
        ]

    @staticmethod
    def _is_discord(event: AstrMessageEvent) -> bool:
        platform_name = getattr(event, "get_platform_name", None)
        return callable(platform_name) and platform_name() == "discord"

    def _owner_dms(self) -> list[str]:
        return [self._owner_umo()] + [
            _s(route.get("owner_umo")) for platform, route in self._discord_routes().items()
            if self._discord_instance_enabled(platform)
        ]

    def _discord_instance_enabled(self, platform: str) -> bool:
        adapter = self._discord_instances().get(platform)
        return bool(adapter and adapter.get("enable") is not False
                    and self._discord_routes().get(platform, {}).get("enabled") is True)

    def _bind_discord_owner(self, event: AstrMessageEvent) -> bool:
        platform = _s(event.get_platform_id())
        routes = self._discord_routes()
        route = routes.get(platform, {})
        parsed = parse_umo(self._umo_of(event))
        if (not parsed or parsed[:2] != (platform, "private") or not event.is_private_chat()
                or not route.get("owner_id") or route.get("owner_umo")
                or _s(event.get_sender_id()) != route["owner_id"]):
            return False
        return self._commit_config({"discord_routes": {**routes, platform: {**route, "owner_umo": parsed[3]}}})

    def _platform_allowed(self, event: AstrMessageEvent) -> bool:
        umo = self._umo_of(event)
        platform = umo.split(":", 1)[0] if ":" in umo else _s(event.get_platform_id())
        if self._is_discord(event):
            return bool(getattr(self.server, "supports_multiple_platforms", False)
                        and platform == _s(event.get_platform_id())
                        and parse_umo(umo) and self._discord_instance_enabled(platform))
        if platform in self._discord_instances() or platform in self._discord_routes():
            return False
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
        if self._is_discord(event):
            route = self._discord_routes().get(_s(event.get_platform_id()), {})
            return self._umo_of(event) in route.get("channels", [])
        if self._group_scope() == "all":
            return True
        return self._matches_scope(event, self._group_allow())

    def _private_allowed(self, event: AstrMessageEvent) -> bool:
        if not event.is_private_chat():
            return True
        if self._is_discord(event):
            return self._is_owner_private(event)
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
            logger.debug("Cortico Bridge: 读取语音服务失败（%s）", type(exc).__name__)
            return {"id": "", "type": "", "model": "", "error": "无法读取语音服务"}
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
        if self.config.get("bridge_mode") not in ("safe", "full"):
            self._commit_config({"bridge_mode": self.server.mode})
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

    def _save_bridge_mode(self, mode: str) -> None:
        if not self._commit_config({"bridge_mode": mode}):
            raise ValueError("保存桥接模式失败")

    def _target_allowed(self, umo: str) -> bool:
        target = parse_umo(umo)
        if target and (target[0] in self._discord_instances() or target[0] in self._discord_routes()):
            platform, kind, _, _ = target
            if not self._discord_instance_enabled(platform) or not getattr(self.server, "supports_multiple_platforms", False):
                return False
            route = self._discord_routes()[platform]
            if kind == "private":
                return umo == route.get("owner_umo") and self.delivery.has_umo(umo)
            return self.server.mode == "full" and umo in route.get("channels", []) and self.delivery.has_umo(umo)
        if self.server.mode == "safe":
            return bool(umo and umo == self._owner_umo())
        if not target or not self.delivery.has_umo(umo):
            return False
        platform, kind, target_id, _ = target
        if platform not in self._bridge_platforms():
            return False
        if kind == "group":
            return self._group_scope() == "all" or bool({umo, target_id}.intersection(self._group_allow()))
        mode = self._private_mode()
        return mode == "all" or (mode == "allowlist" and (
            bool({umo, target_id}.intersection(self._private_allow())) or (
                self._private_admin_bypass() and target_id in self._admin_ids())))

    async def _on_tool_call(
        self, name: str, args: dict, turn: str, actor: dict | None = None, *, scope: str = ""
    ) -> tuple[bool, str | ComponentReceipt]:
        """执行一个 AstrBot 工具。

        QQ 轮次用触发消息的身份；Coo 桌面轮次用 AstrBot 管理页授权的主人 QQ。
        """
        if not self.config.get("enabled", False):
            return False, "桥未启用，工具调用被拒绝"
        trusted = False
        read_scope = _s(args.get("umo")).strip()
        if turn:
            key = (scope, turn)
            hit = self._turns.get(key)
            if hit is None:
                return False, "这一轮的消息已经过期，请重新说一次"
            stamp, event = hit
            if time.time() - stamp > 600:
                self._turns.pop(key, None)
                self._message_segments.pop(key, None)
                return False, "这一轮的消息已经过期，请重新说一次"
            if not self._event_allowed(event):
                return False, "触发该轮的 QQ 会话已停用"
            if read_scope and read_scope != scope and name in (READ_COMPONENT_TOOL, "cortico_authorize_history"):
                return False, "读取目标超出当前 QQ 会话"
            read_scope = scope
            # T-001：只按 AstrBot 管理员名单判断；不信任 World 帧里的 role/owner。
            trusted = not self._is_discord(event) and _s(event.get_sender_id()) in self._admin_ids()
            who = f"消息 {turn}"
        else:
            owner_id = self.server.tool_owner if actor == {"kind": "desktop"} and self.server.mode == "full" else ""
            if not owner_id:
                return False, "桌面对话未获得 AstrBot 的主人工具授权"
            if name == READ_COMPONENT_TOOL:
                if not self._target_allowed(read_scope):
                    return False, "该 QQ 会话未获授权"
                return await self._read_message_component(args, scope=read_scope)
            if name == "cortico_authorize_history":
                allowed = self._target_allowed(read_scope)
                return allowed, "" if allowed else "该 QQ 会话未获授权"
            trusted = True
            platform = self._owner_umo().split(":", 1)[0]
            try:
                from .owner_event import build_owner_event

                event = build_owner_event(
                    self.context, platform, owner_id, self.server.self_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Cortico Bridge: 主人工具身份构造失败（%s）", type(exc).__name__)
                return False, "主人事件构造失败，已跳过"
            who = f"主人 {owner_id}"
        if name == READ_COMPONENT_TOOL:
            return await self._read_message_component(args, scope=read_scope)
        if name == "cortico_authorize_history":
            return True, ""
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

    async def _read_message_component(self, args: dict, actor_event=None, *, scope: str = "") -> tuple[bool, str | ComponentReceipt]:
        message_id = _s(args.get("message_id"))
        path = args.get("path")
        if not isinstance(path, list) or not 1 <= len(path) <= 6 or any(
            type(index) is not int or not 1 <= index <= 120 for index in path
        ):
            return False, "组件位置无效"
        scope = scope or (self._umo_of(actor_event) if actor_event is not None else "")
        message_key = (scope, message_id)
        hit = self._turns.get(message_key)
        if not hit or time.time() - hit[0] > 600:
            return False, "桥端消息索引已过期，可继续查看 Coo 已保存的部分"
        source = hit[1]
        if actor_event is not None and _s(actor_event.unified_msg_origin) != _s(source.unified_msg_origin):
            return False, "这条组件属于其他 QQ 会话"
        if not self._event_allowed(source):
            return False, "消息所在会话已停用"
        page = self._message_segments.get(message_key)
        if not page:
            return False, "这条消息没有组件记录"
        segments, deferred_media = page.segments, page.media
        node_message_id = ""
        for depth, index in enumerate(path):
            if index > len(segments):
                return False, "该位置没有组件"
            segment = segments[index - 1]
            segments = segment.get("children", [])
            total = len(segments)
            if segment["type"] == "node":
                node_message_id = segment["fields"].get("id", "")
            reference = forward_id(segment)
            if reference and not segments:
                if segment["type"] == "json":
                    # JSON cards are user-editable. Ask the adapter about the QQ
                    # message already observed, never about a card-supplied ID.
                    reference = node_message_id if depth else _s(
                        getattr(source.message_obj, "message_id", "")
                        or getattr(source.message_obj, "id", "")
                    )
                if not reference or not hasattr(source, "bot"):
                    return False, "适配器没有提供可查看的转发内容"
                key = (scope, message_id, reference)
                if key not in self._forward_pages:
                    try:
                        self._forward_pages[key] = await fetch_forward(
                            source.bot, reference, source.get_self_id()
                        )
                    except Exception:  # noqa: BLE001
                        return False, "QQ 暂时取不到这条转发，可能已过期或适配器不支持"
                page = self._forward_pages[key]
                segments, total, deferred_media = page.segments, page.total, page.media
                self._forward_pages.move_to_end(key)
                while len(self._forward_pages) > 8:
                    self._forward_pages.popitem(last=False)

        if not segments:
            if media_url := deferred_media.get(id(segment)):
                if segment["type"] == "record":
                    return True, await self._read_voice(segment, media_url, source, file_only=args.get("audio_file") is True)
                return True, await self._read_deferred_media(segment, media_url, source)
            if segment["type"] == "record" and args.get("audio_file") is True:
                return False, "QQ 未提供这条语音的可读取原件"
            return True, render_detail(segment)
        start = args.get("offset", 0)
        start = max(0, start) if type(start) is int else 0
        if start >= len(segments):
            return True, "已读到本次可获取内容的末尾"
        lines = []
        size = 0
        for index, item in enumerate(segments[start:start + 8], start + 1):
            entry = render_detail(item, (*path, index))[:2300]
            if size + len(entry) > 3450:
                break
            lines.append(entry)
            size += len(entry) + 1
        seen = start + len(lines)
        footer = f"第 {start + 1}–{seen} 条，共 {total} 条"
        if seen < len(segments):
            footer += f"。下一页 offset={seen}"
        if len(segments) < total:
            footer += f"。QQ 内容读取上限为 {len(segments)} 条"
        return True, "\n".join([footer, *lines])

    async def _read_voice(self, segment: dict, url: str, source, *, file_only: bool) -> str | ComponentReceipt:
        attachment = (self.server.capabilities or {}).get("component_audio") is True
        if file_only and not attachment:
            return "[语音] 当前 Coo 未声明接收语音原件的能力。"
        if not attachment and not self._stt_enabled():
            return "[语音] AstrBot 当前未开启语音转写。"

        try:
            data = await download_qq_media(url, 6 * 1024 * 1024)
        except Exception:  # noqa: BLE001 - never return a signed QQ URL in the receipt
            return "[语音] QQ 音频暂时取不到，可能已过期或超过 6 MiB。"
        suffix = audio_suffix(data)
        mime = {".silk": "audio/silk", ".amr": "audio/amr", ".ogg": "audio/ogg",
                ".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(suffix)
        if not mime:
            return "[语音] QQ 返回了暂不支持的音频格式。"

        text = _s(segment["fields"].get("text")).strip()
        if not text and not file_only and self._stt_enabled():
            try:
                with tempfile.TemporaryDirectory(prefix="cortico-voice-") as directory:
                    path = Path(directory, f"voice{suffix}")
                    path.write_bytes(data)
                    text = await self._transcribe(SimpleNamespace(file=str(path)), _s(source.unified_msg_origin))
            except OSError:
                logger.warning("Cortico Bridge: 转写用的临时音频无法写入")
        if not attachment:
            return f"[语音] {text}" if text else "[语音] AstrBot 未返回转写。"
        note = "[语音] QQ 原件已下载。"
        if text:
            note += f" 转写：{text}"
        elif not file_only:
            note += " AstrBot 未返回转写。" if self._stt_enabled() else " AstrBot 当前未开启语音转写。"
        return ComponentReceipt(note, audio={"mime": mime, "base64": base64.b64encode(data).decode("ascii")})

    async def _read_deferred_media(self, segment: dict, url: str, source) -> str | ComponentReceipt:
        plan = self.image_plan(source)
        if not plan["native"] and not plan["describe"]:
            reason = _IMAGE_NO_VISION if plan["reason"] == "peer_text_only" else "已关闭图片处理"
            return f"[图片] {reason}。"
        if plan["native"] and not (self.server.capabilities or {}).get("component_media"):
            return "[图片] 当前 Coo 不支持从组件回执读取图片。"
        try:
            raw = await download_qq_media(url, 6 * 1024 * 1024)
            if not (raw.startswith((b"\xFF\xD8\xFF", b"\x89PNG\r\n\x1A\n", b"GIF87a", b"GIF89a"))
                    or raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
                return "[图片] QQ 返回了暂不支持的图片格式。"
            from astrbot.api.message_components import Image

            component = Image.fromBase64(base64.b64encode(raw).decode())
            if plan["describe"]:
                return await self._image_description_mark(component, 0)
            b64, montage, reason = await asyncio.wait_for(self._native_image(component, attempts=1), timeout=6)
        except Exception:  # noqa: BLE001
            return "[图片] QQ 原图暂时取不到，可能已过期。"
        if not b64:
            return f"[图片] {reason or 'QQ 暂未返回原图'}。"
        if len(b64) > MAX_IMAGE_B64:
            return "[图片] 原图超过读取大小上限。"
        return ComponentReceipt(
            "[图片：动图已按 3×3 帧拼图]" if montage else "[QQ 转发图片]",
            [{"mime": _sniff_image_mime(b64), "base64": b64}],
        )

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
        at: list[str] | None = None,
    ) -> tuple[bool, str, bool, bool, str]:
        if not self.config.get("enabled", False):
            return False, "桥未启用，发送门是关的", False, False, codes.DOOR_CLOSED
        if not self._target_allowed(umo):
            return False, "该 QQ 会话未获授权", False, False, "scope_denied"
        ok, note, used_voice, uncertain, error_code = await self.delivery.send(
            platform, conv_kind, conv_id, text, reply_to, image, voice, umo, at=at or [],
            may_dispatch=lambda: bool(self.config.get("enabled") and self._target_allowed(umo)),
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
        return ok, note, used_voice, uncertain, error_code

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
                if (e.get("umo") == umo if umo else e.get("conv") == conv_key) and e.get("convName")
            ),
            "",
        )
        entry = {
            "ts": _now_iso(),
            "dir": "out",
            "channel": "discord" if umo.split(":", 1)[0] in self._discord_instances() else "qq",
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
        discord = self._is_discord(event)
        if event.is_stopped():
            return
        if discord:
            slash_command = getattr(event, "is_slash_command", None)
            if callable(slash_command) and slash_command():
                return
        if discord and self._discord_instance_enabled(_s(event.get_platform_id())) and self._bind_discord_owner(event):
            return
        if not self._platform_allowed(event):
            umo = self._umo_of(event)
            platform = umo.split(":", 1)[0] if ":" in umo else _s(event.get_platform_id())
            logger.info(
                "Cortico Bridge: 跳过非接管平台 %s（bridge_platforms=%s）"
                % (platform, ",".join(self._bridge_platforms()))
            )
            return
        if self.server.mode == "safe" and not self._is_owner_private(event):
            return

        if not discord:
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
        notice, ref = self._notice_text(event) if not discord else ("", "")
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
            if not self._event_allowed(event):
                if not discord and self._private_deny() == "ignore":
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
        if discord or self._group_mode() == "mention":
            text = self._raw_message_text(event) or event.get_message_str()
            if not self._is_called_for_wake(event, text):
                return
            if not discord:
                # /llm 修改该 UMO 使用的配置；事件层直推会跳过 AstrBot 的 LLM 检查。
                config = self.context.get_config(umo=event.unified_msg_origin)
                if not config.get("provider_settings", {}).get("enable", True):
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
        if not self._platform_allowed(event) or (self.server.mode == "safe" and not self._is_owner_private(event)):
            return False

        conv_kind = "private" if event.is_private_chat() else "group"
        conv_id = (
            _s(event.get_group_id())
            if conv_kind == "group"
            else _s(event.get_sender_id() or event.get_session_id())
        )
        discord = self._is_discord(event)
        if discord:
            target = parse_umo(self._umo_of(event))
            if not target or target[1] != conv_kind:
                return False
            conv_id = target[2]
        if not conv_id:
            return False

        if not discord:
            self._remember_name(_s(event.get_sender_id()), _s(event.get_sender_name()))

        if conv_kind == "group":
            if not self._group_allowed(event):
                logger.debug(f"Cortico Bridge: 群 {conv_id} 不在接管名单里，本轮交给 AstrBot")
                return False
        elif not self._event_allowed(event):
            logger.debug(f"Cortico Bridge: 私聊 {conv_id} 不在允许名单里")
            if not discord and self._private_deny() == "ignore":
                event.stop_event()
            return False

        media_text: dict[int, str] = {}
        supports_segments = bool((self.server.capabilities or {}).get("message_segments"))
        quoted_images = [] if supports_segments else None
        quoted_records = [] if supports_segments else None
        deferred_media = {}
        if text is None:
            text = await self._message_text(event, media_text)
        segments = event_segments(
            event, self._raw_message(event), media_text, quoted_images, quoted_records,
            deferred_media if supports_segments else None,
        )
        if not text and not segments:
            return False
        if not text:
            text = " ".join(f"[{LABELS[part['type']]}]" for part in segments[:8])

        platform_id = _s(event.get_platform_id())
        is_admin = not discord and self._is_admin(event)
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
        seen_key = (self._umo_of(event), msg_id)
        if self._is_duplicate(seen_key):
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
            "channel": "discord" if discord else "qq",
            "id": msg_id,
            "umo": _s(event.unified_msg_origin).strip(),
            "ts": _now_iso(),
            "conv": {"kind": conv_kind, "id": conv_id, "name": conv_name},
            "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
            "text": text,
            "mentioned": self._is_called(event, text),
            "platform": platform_id,
            "role": "admin" if is_admin else "member",
            "owner": self._is_owner_private(event) if discord else is_admin,
            "can": {"screenshot": is_admin},
        }

        for fields, record in (quoted_records or [])[:2]:
            if fields.get("text"):
                continue
            if not self._stt_enabled():
                fields["media_state"] = "disabled"
                continue
            transcript = await self._transcribe(record, frame["umo"])
            if transcript:
                fields["text"] = transcript[:1600]
                fields["media_state"] = "described"

        images, image_errors, image_marks = await self._image_payloads(event, quoted_images or ())
        if images:
            frame["images"] = images
        if image_errors:
            frame["image_errors"] = image_errors
        if image_marks:
            direct_marks = image_marks[:len(image_marks) - len(quoted_images or ())]
            frame["text"] = _apply_image_marks(text, direct_marks)

        if segments and supports_segments:
            annotate_images(segments, images, image_errors, image_marks, enabled=self._image_mode() != "off")
            frame["segments"] = segments
            if self.image_plan(event)["native"] and any(part["type"] == "reply" for part in segments):
                frame["quote_image_history"] = True

        # A fast Coo turn can call back before push() has finished. Stage the
        # lookup with the frame and remove it if delivery failed.
        message_key = (frame["umo"], msg_id)
        self._turns[message_key] = (time.time(), event)
        self._message_segments[message_key] = ComponentPage(segments, len(segments), deferred_media)
        delivery = await self.server.offer_message(frame) if self._event_allowed(event) else "declined"
        if delivery == "declined":
            logger.warning("Cortico Bridge: 推送失败，本轮交给 AstrBot")
            self._turns.pop(message_key, None)
            self._message_segments.pop(message_key, None)
            if has_platform_id and msg_id:
                self._seen.pop(seen_key, None)
            elif fallback_key:
                self._fallback_seen.pop(fallback_key, None)
            return False

        # R1：push 成功后立刻置位，再写 UI/广播/日志/stop。后面任何一步抛错，
        # on_llm_request 都不会再推一次；push=False 时没有置位，允许它重试。
        event.set_extra("_cortico_pushed", True)
        self.stats["pushed"] += 1
        if delivery == "unknown":
            self.stats["delivery_unknown"] = self.stats.get("delivery_unknown", 0) + 1
            logger.warning("Cortico Bridge: Coo 接管回执超时，可能已接管，本轮不再由 AstrBot 回复")
        preview = frame["text"]
        entry = {
            "ts": frame["ts"],
            "dir": "in",
            "channel": frame["channel"],
            "role": frame["role"],
            "owner": bool(frame.get("owner")),
            "notice": False,
            "conv": f"{conv_kind}:{conv_id}",
            "convName": conv_name,
            "umo": frame.get("umo", ""),
            "sender": frame["sender"]["name"] or frame["sender"]["id"],
            "text": preview[:200],
            "mentioned": frame["mentioned"],
            "deliveryState": delivery,
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
        if message_key in self._turns:
            self._turns.move_to_end(message_key)
            while len(self._turns) > 100:
                oldest, _ = self._turns.popitem(last=False)
                self._message_segments.pop(oldest, None)
        return True

    # ---------- 小工具 ----------

    async def _message_text(self, event: AstrMessageEvent, media_text: dict[int, str] | None = None) -> str:
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
        for index, comp in enumerate(event.get_messages()):
            name = type(comp).__name__
            if name == "Record":
                spoken = await self._transcribe(
                    comp, _s(getattr(event, "unified_msg_origin", ""))
                )
                if spoken and media_text is not None:
                    media_text[index] = spoken
                parts.append(f"[语音] {spoken}" if spoken else "[语音]")
                continue
            if name == "Image":
                slot = image_index
                image_index += 1
                if plan["native"]:
                    # 原图会随帧发过去，正文只留占位；取图失败由 image_marks 改写。
                    parts.append("[图片]")
                elif plan["describe"]:
                    parts.append(await self._image_description_mark(comp, slot))
                elif plan["reason"] == "peer_text_only":
                    parts.append(f"[图片|{_IMAGE_NO_VISION}]")
                else:
                    # off：不转述、不传原图，只保留占位。
                    parts.append("[图片]")
                if media_text is not None:
                    media_text[index] = parts[-1]
                continue
            label = COMPONENT_LABELS.get(name.lower())
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

        # 显式 image = 强制原图。对端没声明能力也照发，由用户承担模型读不了的风险；
        # auto 才按对端能力保守降级。
        if mode == "image":
            if not self._peer_images():
                logger.debug(
                    "Cortico Bridge: image 模式强制发原图（对端未声明支持图片）"
                )
            return {"native": True, "describe": False, "reason": ""}

        if mode == "describe":
            return {"native": False, "describe": True, "reason": "describe"}

        if self._peer_images():
            return {"native": True, "describe": False, "reason": ""}
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
            logger.warning(
                "Cortico Bridge: 识图失败 provider=%s（%s）", provider_id, type(exc).__name__
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

    async def _image_description_mark(self, comp, slot: int) -> str:
        if slot >= MAX_IMAGES:
            return "[图片|超出单次上限，未转述]"
        desc, reason = await self._describe_image(comp)
        if desc:
            return f"[图片：{desc}]"
        if reason:
            return f"[图片|转述失败：{reason}]"
        return f"[图片|{_IMAGE_NO_VISION}]"

    def _image_max_size(self) -> int:
        """对齐 AstrBot 的 provider_settings.image_compress_options.max_size。"""
        try:
            from astrbot.core.utils.media_utils import (
                normalize_model_image_max_size,
            )

            config = self.context.get_config() or {}
            settings = config.get("provider_settings") or {}
            compression = settings.get("image_compress_options") or {}
            return normalize_model_image_max_size(compression.get("max_size"))
        except Exception:  # noqa: BLE001
            return 1280

    async def _prepare_native_image(self, comp) -> tuple[str, bool, str]:
        """用 AstrBot 自带机制处理原图，再给 Coo 的 LLM。

        - 动图（GIF/WebP 动画）→ 3×3 帧拼图，保留动作顺序；
        - 静图 → 最长边压到 AstrBot 配置的 max_size（默认 1280）；
        - 超过单图上限 → 返回“太大”，不回退原图。

        返回 (base64, 是否动图拼图, 失败原因)。AstrBot 不可用或处理失败时
        返回空串，调用方回退到旧的 convert_to_base64。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
            from astrbot.core.utils.media_utils import (
                ImageInputTooLargeError,
                prepare_model_image,
            )
        except Exception:  # noqa: BLE001
            return "", False, ""

        try:
            path = await comp.convert_to_file_path()
        except Exception:  # noqa: BLE001
            logger.debug("Cortico Bridge: 取本地图片失败", exc_info=True)
            return "", False, ""
        if not path:
            return "", False, ""

        try:
            prepared = await prepare_model_image(
                path,
                max_size=self._image_max_size(),
                output_dir=Path(get_astrbot_temp_path()),
            )
        except ImageInputTooLargeError:
            logger.warning("Cortico Bridge: 图片超过 AstrBot 单图上限，跳过")
            return "", False, "太大"
        except Exception:  # noqa: BLE001
            logger.debug("Cortico Bridge: 图片预处理失败，回退原图", exc_info=True)
            return "", False, ""
        if not prepared:
            return "", False, ""

        prepared_path, is_montage, needs_cleanup, _original = prepared
        try:
            data = Path(prepared_path).read_bytes()
        except Exception:  # noqa: BLE001
            logger.debug("Cortico Bridge: 读取处理后图片失败", exc_info=True)
            return "", False, ""
        finally:
            if needs_cleanup:
                with contextlib.suppress(OSError):
                    os.remove(prepared_path)
        return base64.b64encode(data).decode(), is_montage, ""

    async def _native_image(self, comp, attempts: int) -> tuple[str, bool, str]:
        b64, is_montage, reason = await self._prepare_native_image(comp)
        if b64 or reason:
            return b64, is_montage, reason
        for attempt in range(attempts):
            try:
                b64 = await asyncio.wait_for(comp.convert_to_base64(), timeout=10)
                return b64, is_montage, "" if b64 else reason
            except asyncio.TimeoutError:
                reason = "下载超时"
                logger.debug("Cortico Bridge: 取图超时（第 %d 次）", attempt + 1)
            except Exception:  # noqa: BLE001
                reason = "下载失败"
                logger.warning("Cortico Bridge: 取图失败（第 %d 次）", attempt + 1)
            if attempt + 1 < attempts:
                await asyncio.sleep(0.4 * (attempt + 1))
        return "", is_montage, reason

    async def _image_payloads(
        self, event: AstrMessageEvent, quoted=()
    ) -> tuple[list[dict], list[dict], list[str]]:
        """把消息里的图片打包进帧；返回 (图片, 失败清单, 每张图的占位标记)。

        只有 `image_plan()` 的 native 才打包原图；转述/忽略时返回空帧字段，
        并带上原因（`describe` / `peer_text_only`），不再静默丢掉。
        """
        plan = self.image_plan(event)
        images = [
            comp for comp in event.get_messages() if type(comp).__name__ == "Image"
        ]
        direct_count = len(images)
        images.extend(quoted)
        if not plan["native"]:
            if plan["describe"]:
                # 转述不算“没取到”：正文里已经有 [图片：描述]，不再往
                # image_errors 里塞，否则日志/模型会误以为这张图失败了。
                marks = [""] * direct_count if quoted else []
                for slot, comp in enumerate(quoted, direct_count):
                    marks.append(await self._image_description_mark(comp, slot))
                return [], [], marks
            if plan["reason"] == "peer_text_only":
                errors = [
                    {"index": i, "reason": "peer_text_only"} for i in range(len(images))
                ]
                marks = [""] * direct_count + [f"[图片|{_IMAGE_NO_VISION}]"] * len(quoted) if quoted else []
                return [], errors, marks
            marks = [""] * direct_count + ["[图片|已关闭图片处理]"] * len(quoted) if quoted else []
            return [], [], marks

        payloads: list[dict] = []
        errors: list[dict] = []
        marks: list[str] = []
        total_b64 = 0
        for slot, comp in enumerate(images):
            if len(payloads) >= MAX_IMAGES or slot >= direct_count + MAX_IMAGES:
                errors.append({"index": slot, "reason": "超过单次上限"})
                marks.append("[图片|超出单次上限，没传]")
                continue
            if slot < direct_count:
                b64, is_montage, last_error = await self._native_image(comp, attempts=3)
            else:
                # An expired quoted URL is common. Try briefly and allow Coo to use
                # an attachment already saved in the same conversation.
                try:
                    b64, is_montage, last_error = await asyncio.wait_for(
                        self._native_image(comp, attempts=1), timeout=6
                    )
                except asyncio.TimeoutError:
                    b64, is_montage, last_error = "", False, "引用图片下载超时"
            if not b64:
                reason = last_error or "空数据"
                errors.append({"index": slot, "reason": reason})
                marks.append(f"[图片|没取到：{reason}]")
                logger.warning("Cortico Bridge: 取图失败 —— %s" % reason)
                continue
            if len(b64) > MAX_IMAGE_B64:
                errors.append({"index": slot, "reason": "太大"})
                marks.append("[图片|没取到：太大]")
                logger.warning(
                    "Cortico Bridge: 图片太大 %d KB（上限 %d KB），没有传给 Cortico"
                    % (len(b64) // 1024, MAX_IMAGE_B64 // 1024)
                )
                continue
            if total_b64 + len(b64) > MAX_IMAGE_TOTAL_B64:
                errors.append({"index": slot, "reason": "超过单次总大小"})
                marks.append("[图片|超出单次总大小，没传]")
                continue
            payloads.append({"mime": _sniff_image_mime(b64), "base64": b64})
            total_b64 += len(b64)
            marks.append(
                "[图片：动图已按 3×3 帧拼图]" if is_montage else "[图片]"
            )
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
        if not self.server.connected or (self.server.mode == "safe" and not self._is_owner_private(event)):
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
            logger.debug("Cortico Bridge: 语音转写失败（%s）", type(exc).__name__)
            return ""

    def _is_duplicate(self, msg_id: str | tuple[str, str]) -> bool:
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
        """组件数量 + 顺序类型的稳定描述（同秒两条的上下文仍然一样）。"""
        try:
            comps = list(event.get_messages())
        except Exception:  # noqa: BLE001
            return ""
        names = [type(comp).__name__ for comp in comps]
        return f"{len(names)}:" + ",".join(names)

    def _component_refs(self, event: AstrMessageEvent) -> list[str]:
        """组件上能区分单次投递的媒体引用；base64 只取前 32 字符。"""
        try:
            comps = list(event.get_messages())
        except Exception:  # noqa: BLE001
            return []
        refs: list[str] = []
        for comp in comps:
            for attr in ("url", "file", "path", "filename"):
                value = getattr(comp, attr, None)
                if isinstance(value, str) and value:
                    refs.append(f"{attr}={value}")
            b64 = getattr(comp, "base64", None)
            if isinstance(b64, str) and b64:
                refs.append("base64=" + b64[:32])
        return refs

    def _raw_message(self, event: AstrMessageEvent) -> Any:
        """取适配器原始报文；不同 AstrBot 版本字段名不同，两个都兼容。"""
        obj = getattr(event, "message_obj", None)
        for attr in ("raw_message", "raw"):
            try:
                raw = getattr(obj, attr, None)
            except Exception:  # noqa: BLE001
                continue
            if raw is not None and callable(getattr(raw, "get", None)):
                return raw
        return None

    def _raw_image_refs(self, raw: Any) -> list[str]:
        """原始报文里图片段的 url/file/base64 前缀，用来区分同秒的不同图。"""
        if raw is None:
            return []
        try:
            segments = raw.get("message")
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(segments, list):
            return []
        refs: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if _s(segment.get("type")) != "image":
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                data = {}
            bits = []
            for key in ("url", "file", "path", "filename"):
                value = _s(data.get(key))
                if value:
                    bits.append(f"{key}={value}")
            b64 = _s(data.get("base64"))
            if b64:
                bits.append("base64=" + b64[:32])
            if bits:
                refs.append("&".join(bits))
        return refs

    def _fallback_event_nonce(self, event: AstrMessageEvent) -> str:
        """同一事件对象上的随机 nonce：重复观察命中，不同对象不会互相顶掉。"""
        try:
            nonce = event.get_extra("_cortico_fallback_nonce")
            if nonce:
                return _s(nonce)
            nonce = secrets.token_hex(8)
            event.set_extra("_cortico_fallback_nonce", nonce)
            return nonce
        except Exception:  # noqa: BLE001
            return ""

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
        """退化投递的稳定指纹：同一条事件重复投递会命中，同文新消息不会。

        指纹由「上下文」（正文/发送者/秒级时间/组件数量与类型序列）和
        「单次投递标识」（raw 里的 message_id、图片 url/file、base64 前 32
        字符）组成。没有任何投递标识时（纯文本且适配器不透出 message_id），
        退回到事件对象自己的 nonce：同秒同文的第二条是另一个对象，照样推送；
        同一事件在 3 秒内被重复观察仍只处理一次。

        限制：若适配器把同一条消息重新构造成新对象、且 raw 里连 message_id/
        图片引用都没有，两次投递在数据上完全不可区分，这种情况下不吞消息
        （宁可重复也不丢），窗口仍为 3 秒。
        """
        raw = self._raw_message(event)
        try:
            conv = _s(event.get_group_id())
        except Exception:  # noqa: BLE001
            conv = ""
        refs: list[str] = []
        if raw is not None:
            for key in ("message_id", "msg_id", "real_id"):
                try:
                    value = _s(raw.get(key))
                except Exception:  # noqa: BLE001
                    value = ""
                if value:
                    refs.append(f"raw_{key}={value}")
            refs.extend("raw_image=" + item for item in self._raw_image_refs(raw))
        refs.extend(self._component_refs(event))
        if not refs:
            refs.append("evt=" + self._fallback_event_nonce(event))
        raw_key = "|".join(
            (
                event.get_message_str(),
                _s(event.get_sender_id()),
                conv,
                self._component_signature(event),
                _s(self._event_timestamp(event)),
                *refs,
            )
        )
        return "f" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]

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
        discord = self._is_discord(event)
        with contextlib.suppress(Exception):
            native_mention = getattr(event, "is_mentioned", None)
            if discord and callable(native_mention) and native_mention():
                return True
            for comp in event.get_messages():
                name = type(comp).__name__
                if name == "At":
                    qq = _s(getattr(comp, "qq", ""))
                    if qq and qq != "all" and qq == self_id:
                        return True
                elif name == "Reply" and self_id and not discord:
                    if _s(getattr(comp, "sender_id", "")) == self_id:
                        return True
        return not discord and any(a in text for a in self._aliases())

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
        approval = self.server.pending_mode_approval()
        approval_scope = self._full_mode_scope() if approval else None
        return json_response(
            {
                "version": PLUGIN_VERSION,
                "connected": self.server.connected,
                "mode": self.server.mode,
                "modeApproval": approval,
                "modeApprovalScope": approval_scope,
                "modeApprovalScopeVersion": self._approval_scope_version(approval_scope) if approval_scope else "",
                "ownerBound": any(self._owner_dms()),
                "platformRoutes": self._platform_routes(),
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
                "lastChannel": last.get("channel", "qq") if last else "",
                "platformRoutes": self._platform_routes(),
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
            "mode": self.server.mode,
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
            "lastChannel": last.get("channel", "qq") if last else "",
            "platformRoutes": self._platform_routes(),
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
        permission_keys = {
            "owner_tool_qq", "owner_platform", "desktop_tools_enabled", "desktop_send_targets", "discord_routes",
            "bridge_platforms", "name_aliases", "group_scope", "group_allow", "group_mode", "observe_all", "observe_groups",
            "private_mode", "private_allow", "private_admin_bypass", "private_deny", "tools_allow", "tools_risks",
        }
        permissions_changed = any(key in permission_keys and old[key] != value for key, value in changes.items())
        if permissions_changed and self.server.operations_active:
            return False
        self.config.update(changes)
        try:
            save()
        except Exception as exc:  # noqa: BLE001
            for key, value in old.items():
                if value is missing:
                    self.config.pop(key, None)
                else:
                    self.config[key] = value
            logger.warning(f"Cortico Bridge: 保存配置失败，已回滚 —— {exc}")
            return False

        if permissions_changed:
            self.server.revise_permissions()
            if self.server.has_client:
                asyncio.create_task(self.server.push_ready())
        return True

    @staticmethod
    def _setting_items(raw: Any, label: str) -> list[str]:
        if not isinstance(raw, list) or len(raw) > 100 or any(
            not isinstance(item, str) or len(item) > 256 for item in raw
        ):
            raise ValueError(f"{label}最多 100 项，每项不超过 256 个字符。")
        return list(dict.fromkeys(item.strip() for item in raw if item.strip()))

    def _connection_settings(self) -> dict:
        return {
            "enabled": self.config.get("enabled") is True,
            "host": self._listen_host(), "port": self._port(), "maxSends": max(0, self._max_sends()),
            "tokenSet": bool(self._token_value()),
        }

    async def _bind_listener(self, host: str, port: int, token: str, enabled: bool) -> bool:
        await self.server.stop()
        self.server.host, self.server.port, self.server.token = host, port, token
        return not enabled or await self.server.start()

    @staticmethod
    def _settings_changed(payload: dict, current: dict, *keys: str) -> bool:
        expected = payload.get("expected")
        return expected is not None and expected != {key: current[key] for key in keys}

    async def page_connection_settings(self):
        if request is None or _s(request.method).upper() != "POST":
            return json_response(self._connection_settings())
        payload = await request.json(default={})
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return error_response("请选择是否接管对话。", status_code=400)
        enabled, port, max_sends = payload["enabled"], payload.get("port"), payload.get("maxSends")
        host = _s(payload.get("host")).strip()
        host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
                return error_response("监听地址须为 IP 或主机名。", status_code=400)
        if type(port) is not int or not 1 <= port <= 65535 or type(max_sends) is not int or max_sends < 0:
            return error_response("端口须为 1–65535，发送上限须为非负整数。", status_code=400)

        async with self._listener_lock:
            current = self._connection_settings()
            if self._settings_changed(payload, current, "enabled", "host", "port", "maxSends"):
                return error_response("连接设置已在别处修改，请重新打开控制台后再保存。", status_code=409)
            changes = dict(enabled=enabled, listen_host=host, listen_port=port, max_sends_per_minute=max_sends)
            rebind = (enabled, host, port) != (current["enabled"], current["host"], current["port"])
            if not rebind:
                if max_sends != current["maxSends"] and not self._commit_config(changes):
                    return error_response("保存发送上限失败。", status_code=500)
                return json_response(self._connection_settings())

            token = self._token_value()
            if enabled and not is_loopback_host(host) and not token:
                return error_response("跨机监听需要令牌。请先在本页生成令牌。", status_code=400)
            if payload.get("confirmed") is not True and (self.server.listening or enabled and not is_loopback_host(host)):
                return error_response("请确认修改监听会改变 Coo 的连接。", status_code=409)
            if self.server.operations_active:
                return error_response("桥正在处理消息，请稍后再改监听。", status_code=409)

            previous = (self.server.host, self.server.port, self.server.token, self.server.listening)
            try:
                if not await self._bind_listener(host, port, token, enabled):
                    raise RuntimeError("新地址或端口监听失败")
                if not self._commit_config(changes):
                    raise RuntimeError("保存连接设置失败")
            except (Exception, asyncio.CancelledError) as exc:
                try:
                    restored = await self._bind_listener(*previous)
                except Exception:
                    restored = False
                logger.warning(f"Cortico Bridge: 应用监听设置失败，旧监听恢复={restored}（{type(exc).__name__}）")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                message = "未能应用新设置，已恢复原监听。" if restored else "未能应用新设置，原监听也恢复失败。请检查地址和端口。"
                return error_response(message, status_code=500)
            return json_response(self._connection_settings())

    def _qq_settings(self) -> dict:
        return {
            "platforms": self._bridge_platforms(), "aliases": self._aliases(), "mode": self.server.mode,
            "instances": [
                {"id": key, "enabled": item.get("enable") is not False}
                for key, item in self._configured_platforms().items() if item.get("type") == "aiocqhttp"
            ],
        }

    async def page_qq_settings(self):
        if request is None or _s(request.method).upper() != "POST":
            return json_response(self._qq_settings())
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式不正确。", status_code=400)
        try:
            platforms = self._setting_items(payload.get("platforms"), "QQ 实例")
            aliases = self._setting_items(payload.get("aliases"), "唤醒称呼")
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        current = self._qq_settings()
        if self._settings_changed(payload, current, "platforms", "aliases"):
            return error_response("QQ 设置已在别处修改，请重新打开控制台后再保存。", status_code=409)
        added = set(platforms) - set(current["platforms"])
        available = {item["id"] for item in current["instances"]}
        if not platforms or added - available:
            return error_response("请至少保留一个 QQ 实例，新增项须从 AstrBot 已配置的实例中选择。", status_code=400)
        owner_platform = self._owner_umo().partition(":")[0]
        if owner_platform and owner_platform not in platforms:
            return error_response("请先调整主人私聊绑定，再移除此 QQ 实例。", status_code=409)
        if (added or self.server.mode == "full" and set(aliases) - set(current["aliases"])) and payload.get("confirmed") is not True:
            return error_response("请确认新增的实例或称呼可以触发 Coo。", status_code=409)
        changes = {"bridge_platforms": platforms, "name_aliases": aliases}
        if owner_platform and not self.config.get("owner_platform") and len(platforms) > 1:
            changes["owner_platform"] = owner_platform
        if not self._commit_config(changes):
            return error_response("保存设置失败。桥正在处理消息时请稍后再试。", status_code=409)
        return json_response(self._qq_settings())

    def _image_settings(self) -> dict:
        providers = []
        try:
            for provider in self.context.get_all_providers():
                key = _s((getattr(provider, "provider_config", None) or {}).get("id")).strip()
                if key and key not in providers:
                    providers.append(key)
        except Exception:
            logger.debug("Cortico Bridge: 未能读取视觉模型列表")
        return {"mode": self._image_mode(), "provider": _s(self.config.get("image_vision_provider")).strip(), "providers": providers}

    async def page_image_settings(self):
        if request is None or _s(request.method).upper() != "POST":
            return json_response(self._image_settings())
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("mode") not in ("auto", "image", "describe", "off"):
            return error_response("请选择有效的图片处理方式。", status_code=400)
        current = self._image_settings()
        if self._settings_changed(payload, current, "mode", "provider"):
            return error_response("图片设置已在别处修改，请重新打开控制台后再保存。", status_code=409)
        provider = payload.get("provider")
        if not isinstance(provider, str) or provider and provider not in current["providers"] and provider != current["provider"]:
            return error_response("请选择 AstrBot 已配置的模型，或使用自动选择。", status_code=400)
        if not self._commit_config({"image_mode": payload["mode"], "image_vision_provider": provider}):
            return error_response("保存图片设置失败。", status_code=500)
        return json_response(self._image_settings())

    async def page_tool_risks(self):
        posting = request is not None and _s(request.method).upper() == "POST"
        payload = await request.json(default={}) if posting else {}
        known = {row["name"] for row in self.tools.catalog(force=True) if row.get("name")}
        previous = self.tools.declared_risks()
        allow = self.tools.allow_list()
        auto_reviewed = "*" in allow
        if posting:
            if self.server.mode != "full":
                return error_response("工具风险分类只在完整模式下编辑。", status_code=403)
            try:
                entries = self._setting_items(payload.get("entries") if isinstance(payload, dict) else None, "工具风险分类")
                requested_auto = payload.get("autoReviewed")
                if type(requested_auto) is not bool:
                    raise ValueError("请选择是否自动授权已审查的读写工具。")
                current = {"entries": sorted(f"{k}:{v}" for k, v in previous.items()), "autoReviewed": auto_reviewed}
                if self._settings_changed(payload, current, "entries", "autoReviewed"):
                    return error_response("风险分类已在别处修改，请重新打开控制台后再保存。", status_code=409)
                risks = {}
                available = known | previous.keys()
                for entry in entries:
                    name, _, risk = entry.rpartition(":")
                    name, risk = name.strip(), risk.strip()
                    if name in risks or name not in available or risk not in ("read", "write", "exec"):
                        raise ValueError("请选择已知工具和有效风险等级，每个工具只配置一次。")
                    risks[name] = risk
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
            if risks != previous or requested_auto != auto_reviewed:
                if payload.get("confirmed") is not True:
                    return error_response("请确认已核对工具行为和通配授权会受到的影响。", status_code=409)
                changes = {"tools_risks": [f"{name}:{risk}" for name, risk in risks.items()]} if risks != previous else {}
                if requested_auto != auto_reviewed:
                    changes["tools_allow"] = ["*", *allow] if requested_auto else [name for name in allow if name != "*"]
                if not self._commit_config(changes):
                    return error_response("保存风险分类失败，请稍后再试。", status_code=409)
                self.tools.catalog(force=True)
                await self.server.push(self.tools.frame())
                previous, auto_reviewed = risks, requested_auto
        return json_response({"tools": sorted(known | previous.keys()), "risks": previous, "autoReviewed": auto_reviewed})

    def _full_mode_scope(self) -> dict:
        return {
            "platforms": self._bridge_platforms(),
            "groupScope": self._group_scope(),
            "groupAllow": self._group_allow(),
            "groupMode": self._group_mode(),
            "privateMode": self._private_mode(),
            "privateAllow": self._private_allow(),
            "privateAdminBypass": self._admin_ids() if self._private_admin_bypass() and self._private_mode() == "allowlist" else [],
            "tools": [{"name": tool["name"], "risk": tool.get("risk", "")}
                      for tool in self.tools.enabled_catalog() if tool.get("name")],
            "desktopOwner": self._desktop_owner() if self._desktop_owner() in self._admin_ids() else "",
            "desktopTargets": self._desktop_targets(),
            "discordRoutes": {
                platform: route for platform, route in self._discord_routes().items()
                if self._discord_instance_enabled(platform)
            },
        }

    @staticmethod
    def _approval_scope_version(scope: dict) -> str:
        return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    async def page_access(self):
        """AstrBot 管理后台是绑定主人、配置桌面权限和批准扩权的唯一入口。"""
        if request is not None and _s(request.method).upper() == "POST":
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求格式不正确。", status_code=400)
            action = payload.get("action")
            try:
                if action == "approve":
                    if payload.get("confirmed") is not True:
                        raise ValueError("请确认完整模式会让已放行的会话使用 Coo 主会话")
                    if payload.get("scopeVersion") != self._approval_scope_version(self._full_mode_scope()):
                        raise ValueError("开放范围已变化，请刷新后重新确认")
                    await self.server.approve_mode(_s(payload.get("approval")))
                elif action == "reject":
                    await self.server.reject_mode(_s(payload.get("approval")))
                elif action == "narrow":
                    await self.server.narrow_mode()
                elif action == "save-discord":
                    if not self._save_discord_route(payload):
                        return error_response("当前无法保存 Discord 接入配置。", status_code=409)
                elif action == "save":
                    if self.server.operations_active:
                        return error_response("桥正处理上一轮，请稍后保存授权。", status_code=409)
                    owner = _s(payload.get("ownerQQ")).strip()
                    platform = _s(payload.get("ownerPlatform")).strip()
                    if owner and (not owner.isascii() or not owner.isdigit() or len(owner) > 20):
                        raise ValueError("主人 QQ 应为 20 位以内的数字")
                    if owner and (platform not in self._bridge_platforms() or platform in self._discord_instances()):
                        raise ValueError("请选择已接入的 QQ 平台实例")
                    owner_changed = self._owner_umo() != (f"{platform}:FriendMessage:{owner}" if owner else "")
                    changes = {"owner_tool_qq": owner, "owner_platform": platform if owner else ""}
                    if self.server.mode == "full" and "desktopTools" in payload and "desktopTargets" in payload:
                        changes.update(self._desktop_config_changes(payload, owner, owner_changed))
                    elif owner_changed and self.config.get("desktop_tools_enabled") is True:
                        # 新绑定的主人需要单独获批桌面权限。已保存的发送目标仍保留。
                        changes["desktop_tools_enabled"] = False
                    if not self._commit_config(changes):
                        return error_response("保存授权失败。", status_code=500)
                else:
                    return error_response("未知授权操作。", status_code=400)
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        return json_response({
            "mode": self.server.mode,
            "modeApproval": self.server.pending_mode_approval(),
            "ownerQQ": _s(self.config.get("owner_tool_qq")).strip(),
            "ownerPlatform": _s(self.config.get("owner_platform")).strip() or self._owner_umo().split(":", 1)[0],
            "platforms": [p for p in self._bridge_platforms()
                          if p not in self._discord_routes() and p not in self._discord_instances()],
            "platformRoutes": [route for route in self._platform_routes()
                               if route["channel"] == "qq" or route["id"] in self._discord_instances()],
            "discordRoutes": self._discord_routes(),
            "desktopTools": self.config.get("desktop_tools_enabled") is True,
            "desktopTargets": self._desktop_targets(),
            "legacyDesktopNeedsReview": self._legacy_desktop_needs_review(),
        })

    def _save_discord_route(self, payload: dict) -> bool:
        platform = _s(payload.get("platform")).strip()
        if platform not in self._discord_instances():
            raise ValueError("请选择 AstrBot 已配置的 Discord 实例。")
        owner = _s(payload.get("ownerId")).strip()
        if owner and (not owner.isascii() or not owner.isdigit() or len(owner) > 20):
            raise ValueError("Discord 主人用户 ID 应为 20 位以内的数字。")
        channels = payload.get("channels")
        if not isinstance(channels, list) or len(channels) > 100:
            raise ValueError("频道白名单最多 100 条。")
        targets = []
        for item in channels:
            value = _s(item).strip()
            if value.isascii() and value.isdigit():
                value = f"{platform}:GroupMessage:{value}"
            parsed = parse_umo(value)
            if not parsed or parsed[:2] != (platform, "group"):
                raise ValueError("请填写所选 Discord 实例的频道 ID 或完整频道 UMO。")
            if parsed[3] not in targets:
                targets.append(parsed[3])
        routes = self._discord_routes()
        old = routes.get(platform, {})
        enabled = payload.get("enabled") is True
        opens_access = enabled and (old.get("enabled") is not True or owner != old.get("owner_id", "")
                                    or set(targets).difference(old.get("channels", [])))
        if self.server.mode == "full" and opens_access and payload.get("confirmed") is not True:
            raise ValueError("请确认开放的 Discord 会话将接入 Coo 主会话。")
        route = {"enabled": enabled, "owner_id": owner, "channels": targets,
                 "owner_umo": old.get("owner_umo", "") if owner and owner == old.get("owner_id") else ""}
        return self._commit_config({"discord_routes": {**routes, platform: route}})

    def _desktop_config_changes(self, payload: dict, owner: str, owner_changed: bool) -> dict:
        owner_tools = payload.get("desktopTools") is True
        if owner_tools and owner not in self._admin_ids():
            raise ValueError("桌面使用主人工具时，主人 QQ 须在 AstrBot 管理员名单里")
        targets = payload.get("desktopTargets")
        if not isinstance(targets, list) or len(targets) > 100:
            raise ValueError("桌面发送目标最多 100 条")
        allow = []
        platforms = set(self._bridge_platforms()) | self._discord_routes().keys()
        for item in targets:
            parsed = parse_umo(item) if isinstance(item, str) else None
            if not parsed or parsed[0] not in platforms:
                raise ValueError("桌面发送目标必须是已接入平台的完整会话 UMO")
            if parsed[3] not in allow:
                allow.append(parsed[3])
        if payload.get("confirmed") is not True and (
            (owner_tools and (self.config.get("desktop_tools_enabled") is not True or owner_changed))
            or set(allow).difference(self._desktop_targets())
        ):
            raise ValueError("请确认当前桥接令牌将获得新增的桌面权限")
        return {"desktop_tools_enabled": owner_tools, "desktop_send_targets": allow, "desktop_auth_reviewed": True}

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
                logger.warning("Cortico Bridge: TTS 试听失败（%s）", type(exc).__name__)
                return error_response("TTS 试听失败。", status_code=500)
            if not path:
                return error_response("TTS 没有返回音频文件。", status_code=500)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cortico Bridge: 读取 TTS 试听音频失败（%s）", type(exc).__name__)
                return error_response("读不到 TTS 试听音频。", status_code=500)
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
        conversation = next((conv for conv in self.delivery.known_conversations()
                             if self._target_allowed(_s(conv.get("umo")))), None)
        if conversation is None:
            message = "请先由已绑定的主人 QQ 发一条私聊。" if self.server.mode == "safe" else "还没有允许发送的 QQ 会话。"
            return error_response(message, status_code=400)
        umo = _s(conversation["umo"])
        # 试听失败只报错：allow_text_fallback=False 保证不会把文字发到最近会话。
        ok, note, used_voice, _uncertain, _error_code = await self.delivery.send(
            "", "", "", text, "", None, True, umo, allow_text_fallback=False,
            may_dispatch=lambda: bool(self.config.get("enabled") and self._target_allowed(umo)),
        )
        if not ok:
            return error_response(note or "发送失败", status_code=500)
        return json_response(
            {
                "sent": True,
                "voice": used_voice,
                "note": note,
                "conv": conversation.get("key", ""),
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
            async with self._listener_lock:
                # A-3：保存成功后才替换运行中的令牌。
                if not self._commit_config({"token": token}):
                    return error_response("保存配置失败。", status_code=500)
                self.server.token = token
                restarted = False
                if self.server.listening:
                    restarted = await self._bind_listener(self.server.host, self.server.port, token, True)
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
        async with self._listener_lock:
            if not self.config.get("enabled", False):
                return error_response("桥未启用，先打开「接管对话」再重开监听。", status_code=400)
            try:
                ok = await self._bind_listener(self._listen_host(), self._port(), self._token_value(), True)
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
