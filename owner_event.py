"""为 Coo 对话轮次构造一个「主人私聊」事件。

AstrBot 的工具要求 `AstrMessageEvent`。Coo 轮次没有平台消息，这里按
`platform:FriendMessage:owner_id` 造一个最小事件，并把 `send()` 落到
主人的私聊；不借上一条 QQ 消息，也不伪造群上下文。

只在工具调用时导入 AstrBot 内部模块：旧版本缺类时由调用方转成一句人话。
"""

from __future__ import annotations

import time
import uuid


def build_owner_event(context, platform_id: str, owner_id: str, self_id: str = ""):
    """返回一个 role=admin 的主人私聊事件；构造失败直接抛错。"""
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType

    try:
        from astrbot.core.platform.platform_metadata import PlatformMetadata
    except Exception:  # pragma: no cover - 只兼容极旧版本
        PlatformMetadata = None  # type: ignore[assignment]

    meta = None
    inst = None
    get_inst = getattr(context, "get_platform_inst", None)
    if callable(get_inst):
        try:
            inst = get_inst(platform_id)
            if inst is not None and hasattr(inst, "meta"):
                meta = inst.meta()
        except Exception:  # noqa: BLE001 - 拿不到实例就退到最小元数据
            inst = None
            meta = None
    if meta is None:
        if PlatformMetadata is None:
            raise RuntimeError("这个 AstrBot 版本没有 PlatformMetadata")
        meta = PlatformMetadata(
            name="astrbot",
            description="AstrBot bridge",
            id=platform_id,
            support_proactive_message=True,
        )

    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = self_id or ""
    msg.session_id = owner_id
    msg.message_id = f"coo-{uuid.uuid4().hex}"
    msg.sender = MessageMember(user_id=owner_id, nickname="主人")
    msg.message = []
    msg.message_str = ""
    msg.raw_message = None
    msg.timestamp = int(time.time())

    class CooOwnerEvent(AstrMessageEvent):
        async def send(self, message) -> None:
            chain = getattr(message, "chain", None)
            if chain is None:
                chain = message
            if not isinstance(chain, MessageChain):
                chain = MessageChain(chain or [])
            await context.send_message(self.unified_msg_origin, chain)

    event = CooOwnerEvent("", msg, meta, owner_id)
    # 工具插件常直接走 event.bot（aiocqhttp 的 CQHttp 客户端）。
    # 合成事件没有平台回调，这里把真实平台实例的 bot 挂上。
    bot = getattr(inst, "bot", None) if inst is not None else None
    if bot is not None:
        event.bot = bot
    event.role = "admin"
    event.is_wake = True
    event.is_at_or_wake_command = True
    event.set_extra("coo_turn", True)
    event.set_extra("coo_owner", owner_id)
    return event
