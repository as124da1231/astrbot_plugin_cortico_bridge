"""图片决策回归（IMG-001/002/003）：peer × image_mode × 视觉 provider。

main.py 依赖较多 AstrBot 层，这里只 stub 到 import 和图片函数用得到的部分，
再用 object.__new__ 绕开插件的完整初始化。
"""

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _Logger()
api.AstrBotConfig = dict
astrbot.api = api

event_mod = types.ModuleType("astrbot.api.event")


class _MessageChain:
    def __init__(self):
        self.chain = []

    def append(self, item):
        self.chain.append(item)


class _Filter:
    class EventMessageType:
        ALL = "ALL"

    @staticmethod
    def on_astrbot_loaded(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def on_llm_request(*args, **kwargs):
        return lambda fn: fn


event_mod.MessageChain = _MessageChain
event_mod.AstrMessageEvent = object
event_mod.filter = _Filter()
api.event = event_mod

star_mod = types.ModuleType("astrbot.api.star")


class _Star:
    def __init__(self, context=None):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir(_name):
        raise RuntimeError("test has no data dir")


star_mod.Context = object
star_mod.Star = _Star
star_mod.StarTools = _StarTools
api.star = star_mod

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod

# 不依赖仓库目录名：把插件根目录注册为合成包 `plugin`
spec = importlib.util.spec_from_loader("plugin", None, is_package=True)
spec.submodule_search_locations = [str(ROOT)]
sys.modules.setdefault("plugin", importlib.util.module_from_spec(spec))

from plugin import main as main_mod  # noqa: E402

NO_VISION = "[图片|对端模型不能读图，且没有可用的视觉模型转述]"


class Image:
    def __init__(
        self,
        url="https://example.com/cat.png",
        *,
        error=None,
        data="iVBORw0KGgoAAAANSUhEUg",
    ):
        self.url = url
        self.error = error
        self.data = data

    async def convert_to_base64(self):
        if self.error is not None:
            raise self.error
        return self.data


class FakeEvent:
    def __init__(self, messages, text=""):
        self._messages = messages
        self._text = text
        self.unified_msg_origin = "default:GroupMessage:1"

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return self._messages


class FakeContext:
    def __init__(self, text="一只猫", error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(completion_text=self.text)


def _bridge(mode, peer, provider="vision-1", reply="一只猫", error=None):
    bridge = object.__new__(main_mod.CorticoBridge)
    bridge.config = {"image_mode": mode}
    bridge.server = types.SimpleNamespace(capabilities={"images": peer})
    bridge.context = FakeContext(reply, error)
    bridge._vision_logged = ""
    bridge._image_cache = OrderedDict()
    bridge.stats = {"vision": 0}
    bridge._vision_provider_id = lambda: provider
    return bridge


class ImagePlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_false_auto_describes(self):
        bridge = _bridge("auto", peer=False)
        plan = bridge.image_plan()
        self.assertFalse(plan["native"])
        self.assertTrue(plan["describe"])

        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertIn("一只猫", text)
        self.assertNotIn(NO_VISION, text)

        images, errors, marks = await bridge._image_payloads(
            FakeEvent([Image()])
        )
        self.assertEqual(images, [])
        # 转述成功不是“没取到”：errors 必须为空，否则日志/模型会误判。
        self.assertEqual(errors, [])
        self.assertEqual(marks, [])

    async def test_peer_false_auto_without_provider_explains(self):
        bridge = _bridge("auto", peer=False, provider=None)
        plan = bridge.image_plan()
        self.assertFalse(plan["native"])
        self.assertFalse(plan["describe"])
        self.assertEqual(plan["reason"], "peer_text_only")

        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertIn(NO_VISION, text)

        images, errors, _marks = await bridge._image_payloads(
            FakeEvent([Image()])
        )
        self.assertEqual(images, [])
        self.assertEqual(errors[0]["reason"], "peer_text_only")

    async def test_peer_false_image_mode_forces_native(self):
        # 显式 image = 强制原图：对端没声明能力也照发，由用户承担模型读不了的风险。
        bridge = _bridge("image", peer=False)
        plan = bridge.image_plan()
        self.assertTrue(plan["native"])
        self.assertFalse(plan["describe"])
        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertEqual(text, "[图片]")
        images, errors, _marks = await bridge._image_payloads(
            FakeEvent([Image()])
        )
        self.assertEqual(len(images), 1)
        self.assertEqual(errors, [])

    async def test_native_uses_astrbot_montage_and_marks_it(self):
        bridge = _bridge("image", peer=True, provider=None)

        async def fake_prepare(_comp):
            return "QUJD", True, ""

        bridge._prepare_native_image = fake_prepare
        images, errors, marks = await bridge._image_payloads(
            FakeEvent([Image()])
        )
        self.assertEqual(len(images), 1)
        self.assertEqual(errors, [])
        self.assertTrue(any("3×3" in m for m in marks))

    async def test_native_falls_back_when_prepare_unavailable(self):
        bridge = _bridge("image", peer=True, provider=None)

        async def fake_prepare(_comp):
            return "", False, ""

        bridge._prepare_native_image = fake_prepare
        images, errors, marks = await bridge._image_payloads(
            FakeEvent([Image()])
        )
        self.assertEqual(len(images), 1)
        self.assertEqual(errors, [])
        self.assertIn("[图片]", marks)

    async def test_peer_true_auto_and_image_send_native(self):
        for mode in ("auto", "image"):
            with self.subTest(mode=mode):
                bridge = _bridge(mode, peer=True, provider=None)
                plan = bridge.image_plan()
                self.assertTrue(plan["native"])
                self.assertFalse(plan["describe"])

                text = await bridge._message_text(FakeEvent([Image()]))
                self.assertEqual(text, "[图片]")

                images, errors, _marks = await bridge._image_payloads(
                    FakeEvent([Image()])
                )
                self.assertEqual(len(images), 1)
                self.assertEqual(errors, [])
                self.assertEqual(bridge.context.calls, [])

    async def test_describe_mode_always_describes(self):
        bridge = _bridge("describe", peer=True)
        plan = bridge.image_plan()
        self.assertFalse(plan["native"])
        self.assertTrue(plan["describe"])
        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertIn("一只猫", text)

    async def test_off_mode_does_nothing(self):
        for peer in (True, False):
            with self.subTest(peer=peer):
                bridge = _bridge("off", peer=peer)
                plan = bridge.image_plan()
                self.assertEqual(
                    plan, {"native": False, "describe": False, "reason": "off"}
                )
                text = await bridge._message_text(FakeEvent([Image()]))
                self.assertEqual(text, "[图片]")
                images, errors, marks = await bridge._image_payloads(
                    FakeEvent([Image()])
                )
                self.assertEqual((images, errors, marks), ([], [], []))

    async def test_provider_error_has_explicit_reason(self):
        bridge = _bridge(
            "describe", peer=True, error=RuntimeError("SECRET endpoint")
        )
        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertIn("转述失败", text)
        self.assertIn("视觉模型调用失败", text)
        self.assertNotIn("SECRET", text)
        self.assertNotEqual(text, "[图片]")

    async def test_empty_provider_reply_has_explicit_reason(self):
        bridge = _bridge("describe", peer=True, reply="")
        text = await bridge._message_text(FakeEvent([Image()]))
        self.assertIn("转述失败", text)

    async def test_third_image_is_marked_as_over_limit(self):
        bridge = _bridge("describe", peer=True)
        event = FakeEvent(
            [
                Image("https://example.com/1.png"),
                Image("https://example.com/2.png"),
                Image("https://example.com/3.png"),
            ]
        )
        text = await bridge._message_text(event)
        self.assertIn("超出单次上限，未转述", text)
        self.assertEqual(len(bridge.context.calls), 2)

    async def test_download_error_reason_is_fixed_and_redacted(self):
        bridge = _bridge("image", peer=True)
        secret = "SECRET https://cdn.example.com/cat.png?sig=topsecret"
        event = FakeEvent([Image(error=RuntimeError(secret))])
        with mock.patch.object(main_mod.asyncio, "sleep", new=mock.AsyncMock()):
            images, errors, marks = await bridge._image_payloads(event)
            text = await bridge._message_text(event)
        body = main_mod._apply_image_marks(text, marks)

        self.assertEqual(images, [])
        self.assertEqual(errors, [{"index": 0, "reason": "下载失败"}])
        self.assertEqual(marks, ["[图片|没取到：下载失败]"])
        self.assertIn("没取到：下载失败", body)
        blob = json.dumps(
            {"body": body, "errors": errors, "marks": marks}, ensure_ascii=False
        )
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("example.com", blob)

    async def test_download_timeout_reason_is_fixed(self):
        bridge = _bridge("image", peer=True)
        event = FakeEvent([Image(error=asyncio.TimeoutError())])
        with mock.patch.object(main_mod.asyncio, "sleep", new=mock.AsyncMock()):
            _images, errors, marks = await bridge._image_payloads(event)
        self.assertEqual(errors, [{"index": 0, "reason": "下载超时"}])
        self.assertEqual(marks, ["[图片|没取到：下载超时]"])

    async def test_empty_image_data_reason_is_fixed(self):
        bridge = _bridge("image", peer=True)
        _images, errors, marks = await bridge._image_payloads(
            FakeEvent([Image(data="")])
        )
        self.assertEqual(errors, [{"index": 0, "reason": "空数据"}])
        self.assertEqual(marks, ["[图片|没取到：空数据]"])


if __name__ == "__main__":
    unittest.main()
