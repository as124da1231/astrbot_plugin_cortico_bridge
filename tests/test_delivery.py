"""投递层回归：会话表容错（C-001/002/003/009）与附件降级（T-003/C-005/T-004）。

和 test_umo 一样，先把 astrbot 的少量依赖换成替身，再直接 import deliver。
"""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def debug(self, *args, **kwargs):
        self._add("debug", args)

    def info(self, *args, **kwargs):
        self._add("info", args)

    def warning(self, *args, **kwargs):
        self._add("warning", args)

    def error(self, *args, **kwargs):
        self._add("error", args)


logger = _Logger()

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logger
api.AstrBotConfig = dict
astrbot.api = api

event_mod = types.ModuleType("astrbot.api.event")


class _MessageChain:
    def __init__(self):
        self.chain = []

    def append(self, item):
        self.chain.append(item)


event_mod.MessageChain = _MessageChain
event_mod.AstrMessageEvent = object
event_mod.filter = types.SimpleNamespace()
api.event = event_mod

components = types.ModuleType("astrbot.api.message_components")
STATE = {"image_raise": False, "reply_raise": False, "record_raise": False}


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @classmethod
    def fromBase64(cls, _b64):
        if STATE["image_raise"]:
            raise ValueError("bad image")
        return cls()


class Record:
    @classmethod
    def fromFileSystem(cls, _path):
        if STATE["record_raise"]:
            raise ValueError("bad audio")
        return cls()


class Reply:
    def __init__(self, id):
        if STATE["reply_raise"]:
            raise ValueError("bad reply")
        self.id = id


class At:
    def __init__(self, qq, name=""):
        self.qq = qq
        self.name = name


components.Plain = Plain
components.Image = Image
components.Record = Record
components.Reply = Reply
components.At = At
api.message_components = components

star_mod = types.ModuleType("astrbot.api.star")
star_mod.Context = object
star_mod.Star = object
star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: "")
api.star = star_mod

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.message_components"] = components
sys.modules["astrbot.api.star"] = star_mod

from deliver import Delivery  # noqa: E402

import deliver as deliver_mod  # noqa: E402

deliver_mod.logger = logger

UMO = "default:GroupMessage:123456789"


class _Ctx:
    """记录每次 send_message；results 按顺序返回，exc 只在第一次抛。"""

    def __init__(self, results=None, exc=None):
        self.sent = []
        self.results = list(results) if results is not None else [True]
        self.exc = exc
        self.calls = 0

    async def send_message(self, umo, chain):
        self.calls += 1
        self.sent.append([type(item).__name__ for item in chain.chain])
        if self.exc is not None:
            error, self.exc = self.exc, None
            raise error
        return self.results.pop(0) if self.results else True


def _delivery(results=None, exc=None, config=None):
    ctx = _Ctx(results, exc)
    cfg = {"max_sends_per_minute": 0}
    cfg.update(config or {})
    delivery = Delivery(ctx, lambda: cfg)
    delivery.remember("default", "group", "123456789", UMO)
    return delivery, ctx


class RegistryToleranceTests(unittest.TestCase):
    def setUp(self):
        STATE.update(image_raise=False, reply_raise=False, record_raise=False)

    def _write(self, tmp, payload):
        path = Path(tmp) / "umos.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_bad_first_seen_does_not_break_startup(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "version": 2,
                    "umos": {
                        UMO: {
                            "umo": UMO,
                            "firstSeen": "bad",
                            "lastSeen": time.time(),
                        }
                    },
                },
            )
            delivery = Delivery(None, lambda: {}, data_dir=tmp)
            self.assertTrue(delivery.has_umo(UMO))

    def test_v2_empty_umos_still_reads_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "version": 2,
                    "umos": {},
                    "legacy": {"default|group|123456789": UMO},
                },
            )
            # _prune 会把指向不存在 umo 的 legacy 清掉；这里只看加载阶段。
            with mock.patch.object(Delivery, "_prune", lambda self: None):
                delivery = Delivery(None, lambda: {}, data_dir=tmp)
            self.assertEqual(
                delivery._legacy.get("default|group|123456789"), UMO
            )

    def test_string_version_two_is_read_as_v2(self):
        import time

        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "version": "2",
                    "umos": {
                        UMO: {
                            "umo": UMO,
                            "firstSeen": time.time(),
                            "lastSeen": time.time(),
                        }
                    },
                    "legacy": {"default|group|123456789": UMO},
                },
            )
            delivery = Delivery(None, lambda: {}, data_dir=tmp)
            self.assertTrue(delivery.has_umo(UMO))
            self.assertEqual(
                delivery.umo_for("default", "group", "123456789"), UMO
            )

    def test_bad_last_seen_in_memory_does_not_raise(self):
        delivery = Delivery(None, lambda: {})
        delivery._umos[UMO] = {
            "umo": UMO,
            "platform": "default",
            "kind": "group",
            "id": "123456789",
            "name": "",
            "firstSeen": 0,
            "lastSeen": "bad",
            "lastMessageId": "",
            "lastSender": "",
        }
        self.assertEqual(delivery.known_conversations(), [])
        self.assertFalse(delivery.has_umo(UMO))


class SendFallbackTests(unittest.TestCase):
    def setUp(self):
        STATE.update(image_raise=False, reply_raise=False, record_raise=False)
        logger.records.clear()

    def test_construction_failure_without_text_is_not_uncertain(self):
        STATE["image_raise"] = True
        delivery, ctx = _delivery()
        ok, _note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "", image={"base64": "x"}, umo=UMO)
        )
        self.assertFalse(ok)
        self.assertFalse(uncertain)
        self.assertEqual(ctx.calls, 0)

    def test_construction_failure_with_text_retries_plain(self):
        STATE["image_raise"] = True
        delivery, ctx = _delivery()
        ok, note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", image={"base64": "x"}, umo=UMO)
        )
        self.assertTrue(ok)
        self.assertFalse(uncertain)
        self.assertIn("附件构造失败", note)
        self.assertEqual(ctx.calls, 1)
        self.assertEqual(ctx.sent[0], ["Plain"])

    def test_platform_false_retries_plain_without_attachment(self):
        delivery, ctx = _delivery(results=[False, True])
        ok, note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", image={"base64": "x"}, umo=UMO)
        )
        self.assertTrue(ok)
        self.assertFalse(uncertain)
        self.assertIn("附件未发出，已改发纯文字", note)
        self.assertEqual(ctx.calls, 2)
        self.assertIn("Image", ctx.sent[0])
        self.assertEqual(ctx.sent[1], ["Plain"])

    def test_unknown_exception_is_not_retried_and_is_uncertain(self):
        delivery, ctx = _delivery(
            exc=RuntimeError("https://api.example?key=SECRET")
        )
        ok, note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", umo=UMO)
        )
        self.assertFalse(ok)
        self.assertTrue(uncertain)
        self.assertEqual(delivery.last_error_code, "send_failed")
        self.assertNotIn("SECRET", note)
        self.assertEqual(ctx.calls, 1)
        self.assertTrue(any("SECRET" in text for _lvl, text in logger.records))

    def test_timeout_is_not_retried_and_is_uncertain(self):
        delivery, ctx = _delivery(exc=asyncio.TimeoutError())
        ok, note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", umo=UMO)
        )
        self.assertFalse(ok)
        self.assertTrue(uncertain)
        self.assertEqual(delivery.last_error_code, "send_timeout")
        self.assertIn("超时", note)
        self.assertEqual(ctx.calls, 1)

    def test_reply_construction_failure_is_not_an_attachment(self):
        STATE["reply_raise"] = True
        delivery, ctx = _delivery(results=[False])
        ok, note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", reply_to="m-1", umo=UMO)
        )
        self.assertFalse(ok)
        self.assertFalse(uncertain)
        self.assertIn("引用失败", note)
        self.assertEqual(ctx.calls, 1)  # 没有触发纯文字重试

    def test_tts_fallback_is_not_an_attachment(self):
        delivery, ctx = _delivery(
            results=[False], config={"tts_enabled": True}
        )
        ok, note, used_voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", voice=True, umo=UMO)
        )
        self.assertFalse(ok)
        self.assertFalse(used_voice)
        self.assertFalse(uncertain)
        self.assertIn("TTS", note)
        self.assertEqual(ctx.calls, 1)

    def test_voice_without_text_fallback_stops_before_send(self):
        delivery, ctx = _delivery(config={"tts_enabled": False})
        ok, _note, used_voice, uncertain = asyncio.run(
            delivery.send(
                "", "", "", "hello", voice=True, umo=UMO, allow_text_fallback=False
            )
        )
        self.assertFalse(ok)
        self.assertFalse(used_voice)
        self.assertFalse(uncertain)
        self.assertEqual(delivery.last_error_code, "tts_unavailable")
        self.assertEqual(ctx.calls, 0)

    def test_plain_send_still_works(self):
        delivery, ctx = _delivery()
        ok, _note, _voice, uncertain = asyncio.run(
            delivery.send("", "", "", "hello", umo=UMO)
        )
        self.assertTrue(ok)
        self.assertFalse(uncertain)
        self.assertEqual(ctx.sent, [["Plain"]])

    def test_group_mentions_are_added_before_text(self):
        delivery, ctx = _delivery()
        ok, note, _voice, _uncertain = asyncio.run(
            delivery.send("", "group", "123456789", "@你", umo=UMO, at=["10001", "10002"])
        )
        self.assertTrue(ok, note)
        self.assertEqual(ctx.sent, [["At", "At", "Plain"]])

    def test_all_mention_is_one_component(self):
        delivery, ctx = _delivery()
        ok, _note, _voice, _uncertain = asyncio.run(
            delivery.send("", "group", "123456789", "通知", umo=UMO, at=["all"])
        )
        self.assertTrue(ok)
        self.assertEqual(ctx.sent, [["At", "Plain"]])

    def test_private_mentions_are_ignored_with_note(self):
        private_umo = "default:FriendMessage:123456789"
        delivery, ctx = _delivery()
        delivery.remember("default", "private", "123456789", private_umo)
        ok, note, _voice, _uncertain = asyncio.run(
            delivery.send("", "private", "123456789", "hi", umo=private_umo, at=["10001"])
        )
        self.assertTrue(ok)
        self.assertEqual(ctx.sent, [["Plain"]])
        self.assertIn("@ 不生效", note)


if __name__ == "__main__":
    unittest.main()
