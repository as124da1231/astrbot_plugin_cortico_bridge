"""桥协议的稳定错误码表。

`ack.error` / `tool-ack.error` 只放这里的英文 code；给人看的中文说明放
`ack.note`。World 侧对未知 code 有兜底，但新增 code 必须先加到这里，
再由 P-B（deliver/tools）、P-C（main）和 World 消费，不允许各自造字符串。

这个模块刻意不依赖 astrbot / websockets，测试和文档脚本都能直接 import。
"""

from __future__ import annotations

# ---------- 发送参数校验 ----------
BAD_IMAGE = "bad_image"  # 图片字段格式不对 / base64 为空
IMAGE_TOO_LARGE = "image_too_large"  # 单张图片超过大小上限
IMAGE_BAD_TYPE = "image_bad_type"  # mime 不是 image/*
EMPTY_MESSAGE = "empty_message"  # 正文和图片都为空
VOICE_NEEDS_TEXT = "voice_needs_text"  # 语音消息没有正文
VOICE_IMAGE_CONFLICT = "voice_image_conflict"  # 语音和图片不能同条
VOICE_AT_CONFLICT = "voice_at_conflict"  # 语音和 @ 不能同条
BAD_AT = "bad_at"  # at 不是 QQ 号 / "all"，或超过上限
BAD_SESSION = "bad_session"  # 会话 kind/id 不合法
MISSING_UMO = "missing_umo"  # 发送必须有 UMO（旧 World 兼容失败）
INVALID_UMO = "invalid_umo"  # UMO 解析失败
UNKNOWN_UMO = "unknown_umo"  # 桥没见过这个会话
UMO_CONV_MISMATCH = "umo_conv_mismatch"  # UMO 与 conv 冲突

# ---------- 发送执行 ----------
BUSY = "busy"  # 并发帧超限，本帧没有执行
SEND_QUEUE_TIMEOUT = "send_queue_timeout"  # 同会话排队超时，确定没发
SEND_FAILED = "send_failed"  # 发送层异常，结果可能未知
SEND_TIMEOUT = "send_timeout"  # 平台发送超时，结果未知
PLATFORM_MISSING = "platform_missing"  # AstrBot 里找不到对应平台
TTS_UNAVAILABLE = "tts_unavailable"  # 语音不可用（试听禁回退）

# ---------- 门禁 ----------
RATE_LIMITED = "rate_limited"
DOOR_CLOSED = "door_closed"

# ---------- 工具帧身份（桥层直接拒绝，不进入 on_tool） ----------
MISSING_IDENTITY = "missing_identity"  # turn 和 actor 都没有
AMBIGUOUS_IDENTITY = "ambiguous_identity"  # turn 和 actor 同时存在
BAD_ACTOR = "bad_actor"  # actor 不是 {kind: owner, id: 非空}
TOOL_FAILED = "tool_failed"  # 工具回调抛错（原文只进日志）

ALL_CODES = frozenset(
    {
        BAD_IMAGE,
        IMAGE_TOO_LARGE,
        IMAGE_BAD_TYPE,
        EMPTY_MESSAGE,
        VOICE_NEEDS_TEXT,
        VOICE_IMAGE_CONFLICT,
        VOICE_AT_CONFLICT,
        BAD_AT,
        BAD_SESSION,
        MISSING_UMO,
        INVALID_UMO,
        UNKNOWN_UMO,
        UMO_CONV_MISMATCH,
        BUSY,
        SEND_QUEUE_TIMEOUT,
        SEND_FAILED,
        SEND_TIMEOUT,
        PLATFORM_MISSING,
        TTS_UNAVAILABLE,
        RATE_LIMITED,
        DOOR_CLOSED,
        MISSING_IDENTITY,
        AMBIGUOUS_IDENTITY,
        BAD_ACTOR,
        TOOL_FAILED,
    }
)

__all__ = sorted(ALL_CODES)
