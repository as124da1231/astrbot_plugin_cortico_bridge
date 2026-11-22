"""Convert AstrBot and OneBot chains to the same bounded, ordered wire format."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from urllib.parse import urlsplit


LABELS = {
    "plain": "文字", "image": "图片", "record": "语音", "video": "视频",
    "file": "文件", "face": "表情", "at": "提及", "node": "转发条目",
    "nodes": "合并转发", "poke": "戳一戳", "reply": "引用", "forward": "合并转发",
    "rps": "猜拳", "dice": "骰子", "shake": "抖动", "share": "链接分享",
    "contact": "联系人分享", "location": "位置", "music": "音乐",
    "json": "卡片", "unknown": "未知组件",
}
MAX_DEPTH = 5
MAX_SEGMENTS = 120
MAX_FORWARD_NODES = 100
READ_COMPONENT_TOOL = "cortico_read_message_component"

@dataclass(slots=True)
class ComponentReceipt:
    text: str
    images: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class ComponentPage:
    segments: list[dict]
    total: int
    media: dict[int, str]


# Wire keys map to the field names used by typed components and raw OneBot segments.
_FIELDS = {
    "plain": {"text": ("text",)},
    "face": {"id": ("id",), "name": ("name",)},
    "at": {"id": ("qq", "id"), "name": ("name",)},
    "file": {"name": ("name", "file_name", "file"), "size": ("size", "file_size")},
    "record": {"text": ("text",), "duration": ("duration",)},
    "video": {"duration": ("duration",), "size": ("size", "file_size")},
    "image": {"summary": ("summary",), "width": ("width",), "height": ("height",)},
    "reply": {
        "id": ("id", "message_id"), "name": ("sender_nickname",),
        "sender": ("sender_id", "qq"), "time": ("time",), "text": ("message_str", "text"),
    },
    "node": {"id": ("id", "message_id"), "name": ("name", "nickname"), "sender": ("uin", "user_id"), "time": ("time",)},
    "forward": {"id": ("id", "message_id")},
    "poke": {"id": ("id",), "target": ("qq", "target_id"), "kind": ("_type", "type")},
    "rps": {"result": ("result", "value")},
    "dice": {"result": ("result", "value")},
    "share": {"title": ("title",), "text": ("content",), "url": ("url",)},
    "contact": {"id": ("id",), "kind": ("_type", "type"), "name": ("name",)},
    "location": {"lat": ("lat",), "lon": ("lon",), "title": ("title",), "text": ("content",)},
    "music": {"id": ("id",), "kind": ("_type", "type"), "title": ("title",), "text": ("content",), "url": ("url",)},
    "unknown": {"text": ("text", "summary", "alt"), "id": ("id", "emoji_id")},
}


def _read(obj, key):
    reader = getattr(obj, "get", None)
    return reader(key) if callable(reader) else getattr(obj, key, None)


def _scalar(value, limit=320):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value)[:limit]


def _first(obj, *names, limit=320):
    for name in names:
        value = _scalar(_read(obj, name), limit)
        if value:
            return value
    return ""


def _safe_link(value):
    try:
        url = urlsplit(value)
        if url.scheme in ("https", "http") and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ""


def _card_fields(raw):
    if isinstance(raw, str) and len(raw) <= 64_000:
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    slots = [raw]
    meta = raw.get("meta")
    if isinstance(meta, dict):
        slots.extend(value for value in list(meta.values())[:12] if isinstance(value, dict))
    fields = {}
    for slot in slots:
        for key, names in {
            "app": ("app", "view"), "title": ("title", "prompt"),
            "text": ("desc", "description", "summary"), "url": ("jumpUrl", "qqdocurl", "url"),
        }.items():
            value = _first(slot, *names)
            if key == "url":
                value = _safe_link(value)
            if value:
                fields.setdefault(key, value)
    if raw.get("app") == "com.tencent.multimsg":
        detail = meta.get("detail") if isinstance(meta, dict) else None
        if isinstance(detail, dict):
            fields["forward_id"] = _first(detail, "resid")
    return fields


def _nodes(value):
    if not isinstance(value, list):
        return []
    return [
        row if isinstance(row, dict) and row.get("type") == "node" else {
            "type": "node",
            "data": {
                "name": _first(_read(row, "sender"), "card", "nickname") or _first(row, "name", "nickname"),
                "uin": _first(_read(row, "sender"), "user_id") or _first(row, "uin", "user_id"),
                "id": _first(row, "message_id", "id"),
                "time": _read(row, "time"),
                "content": _read(row, "content") or _read(row, "message") or _read(row, "raw_message"),
            },
        }
        for row in value[:MAX_FORWARD_NODES]
    ]


def normalize_segments(
    chain, media_text=None, *, limit=MAX_SEGMENTS, quoted_images=None,
    quoted_records=None, deferred_media=None,
):
    remaining = limit
    direct_images = sum(type(item).__name__ == "Image" for item in chain) if quoted_images is not None else 0

    def visit(items, depth, inside_reply=False):
        nonlocal remaining
        if not isinstance(items, (list, tuple)) or depth > MAX_DEPTH:
            return []
        output = []
        image_slot = 0
        for index, item in enumerate(items):
            if remaining == 0:
                break
            remaining -= 1
            raw_type = _read(item, "type")
            raw_type = getattr(raw_type, "value", raw_type)
            name = (_scalar(raw_type, 48) or type(item).__name__).lower()
            kind = {
                "text": "plain", "atall": "at", "markdown": "plain", "voice": "record", "ptt": "record",
            }.get(name, name)
            data = item.get("data", item) if isinstance(item, dict) else item
            original = kind
            if kind not in LABELS:
                kind = "unknown"
            fields = {}
            for key, keys in _FIELDS.get(kind, {}).items():
                value = _first(data, *keys, limit=1600 if key == "text" else 320)
                if key == "url":
                    value = _safe_link(value)
                if kind == "file" and key == "name" and ("://" in value or value.startswith("base64:")):
                    value = ""
                if kind == "file" and key == "name":
                    value = value.replace("\\", "/").rsplit("/", 1)[-1]
                if value:
                    fields[key] = value
            if original != kind:
                fields["kind"] = original[:48]
            if name == "markdown":
                fields["text"] = _first(data, "markdown", "content", "text", limit=1600)
            if kind == "json":
                fields = _card_fields(_read(data, "data") or data)
            if kind == "at" and name == "atall":
                fields.setdefault("id", "all")
            if depth == 0 and kind in ("record", "image") and media_text and media_text.get(index):
                fields["text"] = media_text[index][:1600]
            if kind in ("image", "record"):
                fields["media_state"] = "described" if kind == "record" and fields.get("text") else "unavailable"
            elif kind in ("file", "video", "music"):
                fields["media_state"] = "metadata"
            if depth == 0 and kind == "image":
                fields["slot"] = str(image_slot)
                image_slot += 1
            elif inside_reply and kind == "image" and quoted_images is not None and type(item).__name__ == "Image":
                fields["slot"] = str(direct_images + len(quoted_images))
                quoted_images.append(item)
            child_key = {"reply": "chain", "node": "content", "nodes": "nodes", "forward": "content"}.get(kind)
            nested = _read(data, child_key) if child_key else None
            if not nested and kind == "node":
                nested = _read(data, "message")
            if kind == "forward" and isinstance(nested, list):
                nested = _nodes(nested)
            if isinstance(nested, str) and kind == "node":
                nested = [{"type": "text", "data": {"text": nested}}]
            children = visit(nested, depth + 1, kind == "reply") if isinstance(nested, (list, tuple)) else []
            segment = {"type": kind, "fields": fields}
            if inside_reply and kind == "record" and quoted_records is not None and type(item).__name__ == "Record":
                quoted_records.append((fields, item))
            if deferred_media is not None and kind in ("image", "record") and fields["media_state"] != "described":
                url = qq_media_url(_read(data, "url"))
                if url:
                    deferred_media[id(segment)] = url
                    fields["media_state"] = "deferred"
            if children:
                segment["children"] = children
            if isinstance(nested, (list, tuple)) and len(children) < len(nested):
                segment["truncated"] = True
            output.append(segment)
        return output

    return visit(chain, 0)


def event_segments(
    event, raw, media_text=None, quoted_images=None, quoted_records=None, deferred_media=None,
):
    chain = event.get_messages() or []
    typed = normalize_segments(
        chain, media_text, quoted_images=quoted_images, quoted_records=quoted_records, deferred_media=deferred_media,
    )
    original = _read(raw, "message")
    if not isinstance(original, list) or not original:
        return typed
    ordered = normalize_segments(original, deferred_media=deferred_media)
    # The adapter enriches quotes and mentions, but can drop failed files or merge
    # adjacent text. Raw order plus ID-based enrichment preserves both histories.
    enhanced = {
        (part["type"], part["fields"].get("id")): part
        for part in typed
        if part["type"] in ("reply", "at") and part["fields"].get("id")
    }
    for part in ordered:
        match = enhanced.get((part["type"], part["fields"].get("id")))
        if match:
            part["fields"].update(match["fields"])
            if "children" in match:
                part["children"] = match["children"]
            if match.get("truncated"):
                part["truncated"] = True
    for kind, keys in (("image", ("slot", "text")), ("record", ("text",))):
        targets = [part for part in ordered if part["type"] == kind]
        sources = [part for part in typed if part["type"] == kind]
        if len(targets) == len(sources):
            for target, source in zip(targets, sources, strict=True):
                for key in keys:
                    if value := source["fields"].get(key):
                        target["fields"][key] = value
                if kind == "record" and target["fields"].get("text"):
                    target["fields"]["media_state"] = "described"
        elif kind == "image":
            for target in targets:
                target["fields"].pop("slot", None)
    return ordered


def forward_id(segment):
    fields = segment["fields"]
    if segment["type"] == "forward":
        return fields.get("id")
    if segment["type"] == "json":
        return fields.get("forward_id")
    return None


def annotate_images(segments, images, failures, marks, *, enabled=True):
    failed = {item["index"] for item in failures}
    by_slot = {
        slot: index
        for index, slot in enumerate(i for i in range(len(marks)) if i not in failed)
        if index < len(images)
    }
    for segment in segments:
        _annotate_image_tree(segment, marks, by_slot, enabled)


def _annotate_image_tree(segment, marks, by_slot, enabled):
    fields = segment["fields"]
    slot = fields.get("slot")
    if slot is not None:
        slot = int(slot)
        if slot < len(marks) and marks[slot]:
            fields["text"] = marks[slot]
        if not enabled:
            fields["media_state"] = "disabled"
        elif fields.get("text", "").startswith("[图片："):
            fields["media_state"] = "described"
        if slot in by_slot:
            fields["attachment"] = str(by_slot[slot])
            fields["media_state"] = "attached"
    for child in segment.get("children", []):
        _annotate_image_tree(child, marks, by_slot, enabled)


def qq_media_url(value):
    """Only transport media from QQ CDN endpoints supplied by OneBot."""
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        known = host == "multimedia.nt.qq.com.cn" or host.endswith(".qpic.cn") or host.endswith(".gtimg.cn")
        if parsed.scheme == "https" and known and not parsed.username and not parsed.password and parsed.port in (None, 443):
            return value
    except ValueError:
        pass
    return ""


async def download_qq_media(url, max_bytes):
    """Read a bounded CDN response without forwarding credentials or redirects."""
    import aiohttp

    if not qq_media_url(url):
        raise ValueError("Unsupported media URL")
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=8), auto_decompress=False,
        headers={"Accept-Encoding": "identity"},
    ) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200 or (response.content_length or 0) > max_bytes:
                raise ValueError("QQ media is unavailable or too large")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(data) + len(chunk) > max_bytes:
                    raise ValueError("QQ media is too large")
                data.extend(chunk)
            if not data:
                raise ValueError("QQ media is empty")
            return bytes(data)


def audio_suffix(data):
    if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return ".silk"
    if data.startswith(b"#!AMR"):
        return ".amr"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"ID3") or len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return ".mp3"
    return ""


async def fetch_forward(bot, reference, bot_id):
    """Read a forward already present in an allowed message via its own bot."""
    routing = {"self_id": int(bot_id)} if str(bot_id).isdigit() else {}
    response = await asyncio.wait_for(
        bot.call_action("get_forward_msg", id=reference, message_id=reference, **routing),
        timeout=4,
    )
    if isinstance(response, dict):
        if response.get("status") == "failed":
            raise ValueError("adapter refused forward")
        response = response.get("data", response)
    if isinstance(response, dict):
        response = response.get("messages", response.get("message"))
    if not isinstance(response, list):
        raise ValueError("adapter returned no forward messages")
    # Normalize one node at a time so long first messages cannot eat later entries.
    media = {}
    segments = [normalize_segments([node], limit=32, deferred_media=media)[0] for node in _nodes(response)]
    return ComponentPage(segments, len(response), media)


def render_detail(segment, path=()):
    kind = segment["type"]
    fields = segment["fields"]
    ref = ".".join(map(str, path))
    prefix = f"{ref} " if ref else ""
    if kind == "plain":
        return prefix + fields.get("text", "")
    values = [
        f"{key}={value}" for key, value in fields.items()
        if key not in ("slot", "forward_id", "attachment", "media_state") and value not in ("", "0")
    ]
    head = prefix + f"[{LABELS[kind]}]" + (" " + " · ".join(values) if values else "")
    state = fields.get("media_state")
    if state == "deferred":
        head += "（打开此组件可读取媒体）"
    elif state == "metadata":
        head += "（可查看资料）"
    elif state == "disabled":
        head += "（媒体处理已关闭）"
    elif state == "unavailable" and not fields.get("text"):
        head += "（没有可读的媒体内容）"
    if kind == "forward" and not fields.get("id") and not segment.get("children"):
        head += "（QQ 没有提供可展开内容）"
    children = [
        render_detail(child, (*path, index) if path else ())
        for index, child in enumerate(segment.get("children", []), 1)
    ]
    if segment.get("truncated"):
        children.append("（部分内容超出读取上限）")
    return "\n".join([head, *children])
