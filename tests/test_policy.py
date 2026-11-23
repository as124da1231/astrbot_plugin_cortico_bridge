"""策略/配置/面板后端回归（A-1..A-9、T-001/T-004、UI-002/005/006/008）。

main.py 依赖较多 AstrBot 层；这里和 test_images 一样只 stub 到 import 需要的
部分，再用 object.__new__ 绕开插件完整初始化。
"""

import base64
import contextlib
import importlib.util
import json
import sys
import time
import types
import unittest
from collections import OrderedDict, deque
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
from plugin import owner_event  # noqa: E402


class FakeConfig(dict):
    def __init__(self, data=None, fail=False):
        super().__init__(data or {})
        self.fail = fail
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.fail:
            raise RuntimeError("disk full")


class At:
    def __init__(self, qq):
        self.qq = qq


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url="https://example.com/cat.png"):
        self.url = url

    async def convert_to_base64(self):
        return "iVBORw0KGgoAAAANSUhEUg"


class FakeEvent:
    def __init__(
        self,
        text="",
        messages=None,
        *,
        private=False,
        group_id="1",
        sender_id="100",
        self_id="999",
        message_id=None,
        timestamp=100,
        raw=None,
    ):
        self._text = text
        self._messages = messages if messages is not None else []
        self._private = private
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._extras = {}
        self._stopped = False
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.unified_msg_origin = (
            f"default:{'FriendMessage' if private else 'GroupMessage'}:{group_id}"
        )
        self.message_obj = types.SimpleNamespace(raw_message=raw, time=timestamp)
        if message_id is not None:
            self.message_obj.message_id = message_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return self._messages

    def is_private_chat(self):
        return self._private

    def get_group_id(self):
        return "" if self._private else self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return f"用户{self._sender_id}"

    def get_self_id(self):
        return self._self_id

    def get_platform_id(self):
        return "default"

    async def get_group(self):
        return types.SimpleNamespace(group_name="测试群")

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True


class FakeServer:
    mode = "full"
    def __init__(self, connected=True, push_ok=True):
        self.connected = connected
        self.push_ok = push_ok
        self.pushed = []
        self.capabilities = {"images": True}
        self.self_id = "999"
        self.host = "0.0.0.0"
        self.port = 17999
        self.token = "old-token"
        self.tool_owner = "111"
        self.listening = False
        self.listen_error = ""
        self.client_address = ""
        self.connected_at = time.time()
        self.last_frame_at = time.time()
        self.origin_refused = 0
        self.query_token_refused = 0
        self.bad_token_refused = 0
        self.version_refused = 0

    async def push(self, frame):
        self.pushed.append(frame)
        return self.push_ok

    async def push_ready(self):
        return True

    async def stop(self):
        self.listening = False

    async def start(self):
        self.listening = True
        return True


class FakeDelivery:
    def __init__(self, conversations=None, result=(True, "", True, False, "")):
        self.conversations = conversations or [
            {"key": "group:1", "umo": "default:GroupMessage:1"}
        ]
        self.result = result
        self.sent = []

    def known_conversations(self):
        return self.conversations

    def has_umo(self, umo):
        return any(row.get("umo") == umo for row in self.conversations)

    def remember(self, *args, **kwargs):
        return None

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return self.result


class FakeTools:
    def __init__(self, allow=None, catalog=None, enabled=None):
        self.allow = allow if allow is not None else ["*"]
        self.rows = catalog or []
        self.active_rows = enabled if enabled is not None else []
        self.executed = []
        self.frames = []

    def allow_list(self):
        return list(self.allow)

    def catalog(self, force=False):
        return list(self.rows)

    def enabled_catalog(self, force=False):
        return list(self.active_rows)

    def frame(self):
        return {"t": "tools", "items": list(self.active_rows)}

    async def execute(self, name, args, event, trusted=False):
        self.executed.append((name, args, trusted))
        return True, "ok"


class FakeContext:
    def __init__(self, admins=None):
        self.admins = admins if admins is not None else ["111"]

    def get_config(self):
        return {"admins_id": list(self.admins), "wake_prefix": "/"}


class FakeRequest:
    def __init__(self, method, payload):
        self.method = method
        self.payload = payload

    async def json(self, default=None):
        return self.payload


DEFAULTS = {
    "enabled": True,
    "group_scope": "all",
    "group_mode": "mention",
    "group_allow": [],
    "bridge_platforms": ["default"],
    "private_mode": "all",
    "private_deny": "ignore",
    "name_aliases": ["Coo", "库"],
    "image_mode": "auto",
    "tts_enabled": False,
    "token": "secret",
    "tools_allow": ["*"],
}


def make_bridge(
    config=None,
    *,
    connected=True,
    push_ok=True,
    delivery=None,
    tools=None,
    context=None,
):
    bridge = object.__new__(main_mod.CorticoBridge)
    data = dict(DEFAULTS)
    if config:
        data.update(config)
    bridge.config = FakeConfig(data)
    bridge.context = context or FakeContext()
    bridge.server = FakeServer(connected=connected, push_ok=push_ok)
    bridge.delivery = delivery or FakeDelivery()
    bridge.tools = tools or FakeTools()
    bridge._recent = deque(maxlen=40)
    bridge._tool_log = deque(maxlen=12)
    bridge._turns = OrderedDict()
    bridge._message_segments = {}
    bridge._forward_pages = OrderedDict()
    bridge._seen = OrderedDict()
    bridge._fallback_seen = OrderedDict()
    bridge._msg_seq = 0
    bridge._subscribers = set()
    bridge._names = OrderedDict()
    bridge._image_cache = OrderedDict()
    bridge._vision_logged = ""
    bridge._cmd_names = set()
    bridge._cmd_names_at = 0.0
    bridge.started_at = time.time()
    bridge.stats = {
        "pushed": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "vision": 0,
        "voices": 0,
        "images": 0,
    }
    return bridge


@contextlib.contextmanager
def fake_request(method, payload=None):
    old = main_mod.request
    main_mod.request = FakeRequest(method, payload if payload is not None else {})
    try:
        yield
    finally:
        main_mod.request = old


@contextlib.contextmanager
def fake_responses():
    old_json, old_error = main_mod.json_response, main_mod.error_response
    main_mod.json_response = lambda data, **kwargs: {"status": 200, "data": data}
    main_mod.error_response = lambda msg, status_code=400, **kwargs: {
        "status": status_code,
        "error": msg,
    }
    try:
        yield
    finally:
        main_mod.json_response, main_mod.error_response = old_json, old_error


class MentionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_at_self_pushes_once_and_llm_does_not_repeat(self):
        bridge = make_bridge()
        event = FakeEvent(
            "你好", messages=[At("999"), Plain("你好")], sender_id="100"
        )
        await bridge.observe_all_messages(event)

        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")
        self.assertTrue(event.get_extra("_cortico_pushed"))
        self.assertTrue(event.is_stopped())
        self.assertTrue(bridge.server.pushed[0]["mentioned"])

        # 直接推送成功后，LLM 层不再推第二次。
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_mention_alias_wakes(self):
        bridge = make_bridge()
        event = FakeEvent("库，在吗", messages=[Plain("库，在吗")])
        await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")

    async def test_mention_unrelated_text_is_not_owned(self):
        bridge = make_bridge()
        event = FakeEvent("今天天气不错", messages=[Plain("今天天气不错")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))

    async def test_mention_group_not_allowed_does_not_wake(self):
        bridge = make_bridge({"group_scope": "allowlist", "group_allow": ["other"]})
        event = FakeEvent("你好", messages=[At("999")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertFalse(event.is_at_or_wake_command)

    async def test_at_all_is_not_wake_and_mentioned_false(self):
        bridge = make_bridge()
        event = FakeEvent("大家好", messages=[At("all")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertFalse(bridge._is_called(event, "大家好"))

    async def test_all_mode_stays_on_llm_path(self):
        bridge = make_bridge({"group_mode": "all"})
        event = FakeEvent("随便聊聊", messages=[Plain("随便聊聊")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")

        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_disconnected_bridge_does_not_set_owned(self):
        bridge = make_bridge(connected=False)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        await bridge.observe_all_messages(event)
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

    async def test_command_is_left_to_astrbot(self):
        bridge = make_bridge()
        event = FakeEvent("/help", messages=[Plain("/help")])
        await bridge.observe_all_messages(event)
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

    async def test_push_failure_allows_llm_retry_once(self):
        bridge = make_bridge(push_ok=False)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertFalse(event.get_extra("_cortico_pushed"))
        self.assertFalse(event.is_stopped())

        bridge.server.push_ok = True
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertTrue(event.get_extra("_cortico_pushed"))

    async def test_push_success_survives_ui_record_failure(self):
        bridge = make_bridge()

        class ExplodingRecent(deque):
            def append(self, item):
                raise RuntimeError("ui boom")

        bridge._recent = ExplodingRecent(maxlen=40)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        with self.assertRaises(RuntimeError):
            await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertTrue(event.get_extra("_cortico_pushed"))

        # 置位发生在 UI 记录之前，所以 LLM 层不会补推。
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)


class NoticePolicyTests(unittest.IsolatedAsyncioTestCase):
    def _event(self):
        raw = {
            "post_type": "notice",
            "notice_type": "group_recall",
            "user_id": "100",
            "operator_id": "100",
            "message_id": "9",
        }
        return FakeEvent("", raw=raw, group_id="1")

    async def test_disconnected_notice_is_not_stopped(self):
        bridge = make_bridge(connected=False)
        event = self._event()
        await bridge.observe_all_messages(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(bridge.server.pushed, [])

    async def test_connected_notice_is_pushed_and_stopped(self):
        bridge = make_bridge()
        event = self._event()
        await bridge.observe_all_messages(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(len(bridge._recent), 1)


class MessageIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_ids_are_unique_for_same_text(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        ids = {bridge._message_id(event) for _ in range(5)}
        self.assertEqual(len(ids), 5)

    async def test_fallback_dedupes_only_within_three_seconds(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        key = bridge._fallback_key(event)
        self.assertFalse(bridge._is_fallback_duplicate(key))
        self.assertTrue(bridge._is_fallback_duplicate(key))

        later = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=130)
        self.assertFalse(bridge._is_fallback_duplicate(bridge._fallback_key(later)))

    async def test_same_second_same_text_two_messages_both_push(self):
        bridge = make_bridge()
        # 同一秒、同一发送者、同一句正文：没有 message_id 也必须是两条。
        first = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        second = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertEqual(
            len({frame["id"] for frame in bridge.server.pushed}), 2
        )

    async def test_same_second_two_different_images_both_push(self):
        bridge = make_bridge()
        first = FakeEvent(
            "", messages=[Image("https://example.com/a.png")], timestamp=100
        )
        second = FakeEvent(
            "", messages=[Image("https://example.com/b.png")], timestamp=100
        )
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertEqual(
            len({frame["id"] for frame in bridge.server.pushed}), 2
        )

    async def test_same_event_redelivery_within_three_seconds_pushes_once(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        await bridge._push_event(event)
        await bridge._push_event(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(bridge.stats["skipped"], 1)

    async def test_raw_message_id_dedupes_across_event_objects(self):
        bridge = make_bridge()
        raw = {"message_id": "m-42", "message": []}
        first = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100, raw=raw)
        second = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100, raw=raw)
        self.assertEqual(bridge._fallback_key(first), bridge._fallback_key(second))
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_real_platform_id_still_dedupes(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", message_id="m1")
        self.assertTrue(bridge._has_platform_id(event))
        self.assertFalse(bridge._is_duplicate("m1"))
        self.assertTrue(bridge._is_duplicate("m1"))


class CommitConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_save_failure_rolls_back(self):
        bridge = make_bridge()
        bridge.config.fail = True
        before = dict(bridge.config)
        with fake_responses(), fake_request(
            "POST", {"scope": "allowlist", "groups": ["123"], "mode": "mention"}
        ):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 500)
        self.assertEqual(dict(bridge.config), before)

    async def test_voice_invalid_max_chars_does_not_touch_tts_enabled(self):
        bridge = make_bridge({"tts_enabled": False})
        with fake_responses(), fake_request(
            "POST", {"enabled": True, "max_chars": "abc"}
        ):
            result = await bridge.page_voice()
        self.assertEqual(result["status"], 400)
        self.assertFalse(bridge.config["tts_enabled"])

    async def test_token_save_failure_keeps_server_token(self):
        bridge = make_bridge()
        bridge.server.token = "old-token"
        bridge.config.fail = True
        with fake_responses(), fake_request("POST", {"action": "generate"}):
            result = await bridge.page_token()
        self.assertEqual(result["status"], 500)
        self.assertEqual(bridge.server.token, "old-token")
        self.assertEqual(bridge.config["token"], "secret")

    async def test_groups_mirror_keeps_session_colon(self):
        bridge = make_bridge()
        with fake_responses(), fake_request(
            "POST",
            {
                "scope": "allowlist",
                "groups": ["default:GroupMessage:123:456"],
                "mode": "mention",
            },
        ):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.config["group_allow"], ["default:GroupMessage:123:456"])
        self.assertEqual(bridge.config["observe_groups"], ["123:456"])

    async def test_groups_without_payload_groups_preserves_old_list(self):
        bridge = make_bridge()
        bridge.config["group_allow"] = ["default:GroupMessage:1"]
        with fake_responses(), fake_request("POST", {"scope": "all", "mode": "mention"}):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.config["group_allow"], ["default:GroupMessage:1"])
        self.assertEqual(result["data"]["groups"], ["default:GroupMessage:1"])

    async def test_normalize_scope_accepts_platform(self):
        bridge = make_bridge({"bridge_platforms": ["default", "qq2"]})
        out = bridge._normalize_scope_items(["123"], "group", "qq2")
        self.assertEqual(out, ["qq2:GroupMessage:123"])


class MessageComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_file_is_returned_without_transcription(self):
        bridge = make_bridge({"stt_enabled": False})
        bridge.server.capabilities.update(message_segments=True, component_audio=True)
        source_url = "https://multimedia.nt.qq.com.cn/voice?token=fixture"
        event = FakeEvent("", message_id="voice", raw={"message": [
            {"type": "record", "data": {"url": source_url}},
        ]})
        voice = b"\x02#!SILK_V3\x00\x00"
        bridge._transcribe = mock.AsyncMock()
        self.assertTrue(await bridge._push_event(event))
        with mock.patch.object(main_mod, "download_qq_media", new_callable=mock.AsyncMock, return_value=voice) as download:
            ok, result = await bridge._on_tool_call(
                main_mod.READ_COMPONENT_TOOL, {"message_id": "voice", "path": [1]},
                "voice", scope=event.unified_msg_origin,
            )
        self.assertTrue(ok)
        download.assert_awaited_once_with(source_url, 6 * 1024 * 1024)
        bridge._transcribe.assert_not_awaited()
        self.assertIn("未开启语音转写", result.text)
        self.assertNotIn(source_url, result.text)
        self.assertEqual(result.audio["mime"], "audio/silk")
        self.assertEqual(base64.b64decode(result.audio["base64"]), voice)

    async def test_voice_file_only_preserves_a_transcript_without_new_transcription(self):
        bridge = make_bridge({"stt_enabled": True})
        bridge.server.capabilities["component_audio"] = True
        bridge._transcribe = mock.AsyncMock()
        event = FakeEvent("声音")
        with mock.patch.object(main_mod, "download_qq_media", new_callable=mock.AsyncMock, return_value=b"#!AMR\nfixture"):
            result = await bridge._read_voice({"fields": {"text": "已经转写"}}, "fixture", event, file_only=True)
        self.assertIn("已经转写", result.text)
        self.assertEqual(result.audio["mime"], "audio/amr")
        bridge._transcribe.assert_not_awaited()

    async def test_forward_media_uses_the_selected_path_without_exposing_urls(self):
        bridge = make_bridge()
        bridge.server.capabilities.update(message_segments=True, component_media=True)
        url = "https://multimedia.nt.qq.com.cn/download?token=fixture"
        event = FakeEvent("看看这段", message_id="outer", raw={"message": [
            {"type": "forward", "data": {"id": "forward"}},
        ]})
        event.bot = types.SimpleNamespace(call_action=mock.AsyncMock(return_value={"messages": [{
            "sender": {"nickname": "转发作者"},
            "message": [
                {"type": "image", "data": {"url": url}},
                {"type": "record", "data": {"url": "http://127.0.0.1/private"}},
            ],
        }]}))
        bridge._read_deferred_media = mock.AsyncMock(return_value=main_mod.ComponentReceipt(
            "[图片]", [{"mime": "image/png", "base64": "AQID"}]
        ))
        await bridge._push_event(event)
        async def read(path):
            return await bridge._on_tool_call(
                main_mod.READ_COMPONENT_TOOL, {"message_id": "outer", "path": path}, "outer", scope=event.unified_msg_origin
            )

        ok, page = await read([1])
        self.assertTrue(ok)
        self.assertIn("1.1.1 [图片]", page)
        self.assertIn("1.1.2 [语音]", page)
        self.assertNotIn(url, page)
        self.assertNotIn("127.0.0.1", page)
        bridge._read_deferred_media.assert_not_called()
        ok, image = await read([1, 1, 1])
        self.assertTrue(ok)
        self.assertEqual(image.images[0]["base64"], "AQID")
        self.assertEqual(bridge._read_deferred_media.await_args.args[1], url)
        ok, audio = await read([1, 1, 2])
        self.assertTrue(ok)
        self.assertIn("没有可读的媒体内容", audio)
        self.assertEqual(bridge._read_deferred_media.await_count, 1)

    async def test_quoted_record_provides_transcript_in_its_own_component(self):
        class Record:
            pass

        bridge = make_bridge({"stt_enabled": True})
        bridge.server.capabilities["message_segments"] = True
        bridge._transcribe = mock.AsyncMock(return_value="原语音的内容")
        quote = Reply("100")
        quote.id = "earlier"
        voice = Record()
        quote.chain = [Plain("之前"), voice]
        event = FakeEvent("这段语音", messages=[quote, Plain("这段语音")])
        self.assertTrue(await bridge._push_event(event))
        audio = bridge.server.pushed[0]["segments"][0]["children"][1]
        self.assertEqual(audio["type"], "record")
        self.assertEqual(audio["fields"]["text"], "原语音的内容")
        self.assertEqual(audio["fields"]["media_state"], "described")

    async def test_quoted_image_uses_delivery_plan_and_attachment_slot(self):
        for mode in ("image", "describe", "off"):
            with self.subTest(mode=mode):
                bridge = make_bridge({"image_mode": mode})
                bridge.server.capabilities.update(images=True, message_segments=True)
                bridge._describe_image = mock.AsyncMock(return_value=("识别到图片里的猫", ""))
                quote = Reply("100")
                quote.id = "earlier"
                quote.chain = [Image("https://example.com/quoted.png")]
                event = FakeEvent("看看这张", messages=[quote, Plain("看看这张")], raw={"message": [
                    {"type": "reply", "data": {"id": "earlier"}},
                    {"type": "text", "data": {"text": "看看这张"}},
                ]})
                self.assertTrue(await bridge._push_event(event))
                packet = bridge.server.pushed[0]
                fields = packet["segments"][0]["children"][0]["fields"]
                if mode == "image":
                    self.assertEqual(fields["attachment"], "0")
                    self.assertEqual(packet["images"][0]["mime"], "image/png")
                    self.assertTrue(packet["quote_image_history"])
                else:
                    self.assertNotIn("images", packet)
                    self.assertNotIn("quote_image_history", packet)
                    self.assertNotIn("attachment", fields)
                    self.assertIn("图片里的猫" if mode == "describe" else "已关闭", fields["text"])

    async def test_image_slots_keep_direct_image_first_and_quote_rest(self):
        bridge = make_bridge({"image_mode": "image"})
        bridge.server.capabilities.update(images=True, message_segments=True)
        direct = Image("https://example.com/direct.png")
        quote = Reply("100")
        quote.id = "earlier"
        quote.chain = [Image("https://example.com/first.png"), Image("https://example.com/second.png")]
        event = FakeEvent("Coo 看图", messages=[quote, direct, Plain("Coo 看图")])
        self.assertTrue(await bridge._push_event(event))
        packet = bridge.server.pushed[0]
        self.assertEqual(len(packet["images"]), 2)
        self.assertEqual(packet["segments"][1]["fields"]["attachment"], "0")
        children = packet["segments"][0]["children"]
        self.assertEqual(children[0]["fields"]["attachment"], "1")
        self.assertNotIn("attachment", children[1]["fields"])
        self.assertIn("上限", children[1]["fields"]["text"])

    async def test_all_component_kinds_and_forward_scope(self):
        from importlib import import_module

        components = import_module(f"{main_mod.__package__}.message_segments")
        from unittest.mock import AsyncMock

        bridge = make_bridge()
        bridge.server.capabilities["message_segments"] = True
        chain = [
            {"type": kind, "data": (
                {"id": "forward-123"} if kind == "forward" else {"text": "fixture"}
            )}
            for kind in components.LABELS
        ]
        event = FakeEvent("fixture", message_id="outer", raw={"message": chain})
        event.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={
            "messages": [{
                "sender": {"nickname": "转发人", "user_id": 42},
                "message": [{"type": "text", "data": {"text": "转发正文"}}],
            }] * 9,
        }))
        self.assertTrue(await bridge._push_event(event))
        packet = bridge.server.pushed[0]
        self.assertEqual([part["type"] for part in packet["segments"]], list(components.LABELS))

        args = {"message_id": "outer", "path": [list(components.LABELS).index("forward") + 1]}
        other = FakeEvent("other", group_id="2", message_id="other")
        self.assertTrue(await bridge._push_event(other))
        denied, _ = await bridge._on_tool_call(components.READ_COMPONENT_TOOL, args, "other", scope=other.unified_msg_origin)
        self.assertFalse(denied)
        event.bot.call_action.assert_not_called()

        ok, result = await bridge._on_tool_call(components.READ_COMPONENT_TOOL, args, "outer", scope=event.unified_msg_origin)
        self.assertTrue(ok)
        self.assertIn("转发正文", result)
        self.assertIn("offset=8", result)
        ok, result = await bridge._on_tool_call(
            components.READ_COMPONENT_TOOL, {**args, "offset": 8}, "outer", scope=event.unified_msg_origin
        )
        self.assertTrue(ok)
        self.assertIn("第 9–9 条", result)
        event.bot.call_action.assert_awaited_once_with(
            "get_forward_msg", id="forward-123", message_id="forward-123", self_id=999
        )

    async def test_join_original_order_with_resolved_quote_and_failed_files(self):
        bridge = make_bridge()
        bridge.server.capabilities["message_segments"] = True
        quote = types.SimpleNamespace(
            type="Reply", id="42", sender_nickname="引用作者", chain=[Plain("旧正文")],
        )
        event = FakeEvent("当前正文", messages=[quote, Plain("当前正文")], raw={"message": [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "file", "data": {"name": r"C:\private\示例.zip", "url": "https://example.com/token"}},
            {"type": "text", "data": {"text": "当前正文"}},
            {"type": "new_type", "data": {"alt": "未知组件"}},
        ]})
        self.assertTrue(await bridge._push_event(event))
        parts = bridge.server.pushed[0]["segments"]
        self.assertEqual([part["type"] for part in parts], ["reply", "file", "plain", "unknown"])
        self.assertEqual(parts[0]["children"][0]["fields"]["text"], "旧正文")
        self.assertEqual(parts[0]["fields"]["name"], "引用作者")
        self.assertEqual(parts[1]["fields"], {"name": "示例.zip", "media_state": "metadata"})
        self.assertEqual(parts[3]["fields"]["kind"], "new_type")


class ListenHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_listen_host_defaults_to_loopback(self):
        bridge = make_bridge()
        self.assertEqual(bridge._listen_host(), "127.0.0.1")

    async def test_explicit_listen_host_is_kept(self):
        bridge = make_bridge({"listen_host": "0.0.0.0"})
        self.assertEqual(bridge._listen_host(), "0.0.0.0")

    async def test_page_restart_uses_loopback_default(self):
        bridge = make_bridge({"listen_host": ""})
        bridge.server.host = "0.0.0.0"
        with fake_responses():
            result = await bridge.page_restart()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.server.host, "127.0.0.1")


class VoiceTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_disabled_does_not_send_any_message(self):
        bridge = make_bridge({"tts_enabled": False})
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 400)
        self.assertIn("tts_unavailable", result["error"])
        self.assertEqual(bridge.delivery.sent, [])

    async def test_voice_failure_never_falls_back_to_text(self):
        bridge = make_bridge({"tts_enabled": True})
        bridge.delivery = FakeDelivery(result=(False, "TTS 失败", False, False, "tts_unavailable"))

        async def provider(_kind):
            return object()

        bridge._voice_provider = provider
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 500)
        self.assertEqual(len(bridge.delivery.sent), 1)
        self.assertFalse(bridge.delivery.sent[0][1]["allow_text_fallback"])
        self.assertIn("TTS 失败", result["error"])


class TrustedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_admin_without_tool_authorization_is_not_trusted(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        bridge.server.tool_owner = ""
        event = FakeEvent(sender_id="111")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        await bridge._on_tool_call("test_tool", {}, "t1", scope=event.unified_msg_origin)
        self.assertFalse(bridge.tools.executed[-1][2])

    async def test_qq_admin_sender_is_trusted(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="111")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        ok, _text = await bridge._on_tool_call("run_shell", {}, "t1", scope=event.unified_msg_origin)
        self.assertTrue(ok)
        self.assertEqual(bridge.tools.executed[-1][2], True)

    async def test_qq_member_sender_is_not_trusted(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="222")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        await bridge._on_tool_call("run_shell", {}, "t1", scope=event.unified_msg_origin)
        self.assertEqual(bridge.tools.executed[-1][2], False)

    async def test_owner_actor_must_be_in_admins(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="111")
        with mock.patch.object(owner_event, "build_owner_event", return_value=event):
            await bridge._on_tool_call(
                "run_shell", {}, "", {"kind": "owner", "id": "111"}
            )
        self.assertEqual(bridge.tools.executed[-1][2], True)

    async def test_owner_event_construction_failure_does_not_leak_exception(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        secret = "SECRET https://cdn.example.com/hook?sig=topsecret"
        with mock.patch.object(
            owner_event,
            "build_owner_event",
            side_effect=RuntimeError(secret),
        ):
            ok, text = await bridge._on_tool_call(
                "run_shell", {}, "", {"kind": "owner", "id": "111"}
            )
        self.assertFalse(ok)
        self.assertIn("主人事件构造失败", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("example.com", text)
        self.assertEqual(bridge.tools.executed, [])

    async def test_world_role_owner_fields_are_ignored(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        ok, text = await bridge._on_tool_call(
            "run_shell",
            {},
            "",
            {"kind": "owner", "id": "222", "role": "admin", "owner": True},
        )
        self.assertFalse(ok)
        self.assertIn("不在 AstrBot 的管理员名单", text)
        self.assertEqual(bridge.tools.executed, [])


class SendPassThroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_five_tuple_with_code_and_note(self):
        bridge = make_bridge()
        bridge.delivery = FakeDelivery(
            result=(False, "发送失败，详情见插件日志", False, False, "send_failed")
        )
        result = await bridge._on_send_from_cortico(
            "group", "1", "hi", "", "default", umo="default:GroupMessage:1"
        )
        self.assertEqual(len(result), 5)
        self.assertFalse(result[0])
        self.assertEqual(result[1], "发送失败，详情见插件日志")
        self.assertEqual(result[4], "send_failed")


class ConfigCompatibilityTests(unittest.TestCase):
    def test_schema_defaults_preserve_legacy_group_policy(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = {key: value["default"] for key, value in schema.items()}
        bridge = make_bridge({**defaults, "observe_groups": ["123456"], "observe_all": True})
        self.assertEqual(bridge._group_scope(), "allowlist")
        self.assertEqual(bridge._group_allow(), ["123456"])
        self.assertEqual(bridge._group_mode(), "all")

    def test_already_injected_scope_does_not_widen_legacy_allowlist(self):
        bridge = make_bridge({"group_scope": "all", "group_allow": [], "observe_groups": ["123456"]})
        self.assertEqual(bridge._group_scope(), "allowlist")
        self.assertEqual(bridge._group_allow(), ["123456"])
        bridge.config.update({"group_scope": "allowlist", "group_allow": ["456789"]})
        self.assertEqual(bridge._group_allow(), ["456789"])

    def test_invalid_port_uses_default(self):
        bridge = make_bridge()
        for port in (None, 0, True, 1.5, -1, 65536, float("inf")):
            with self.subTest(port=port):
                bridge.config["listen_port"] = port
                self.assertEqual(bridge._port(), 17999)
        bridge.config["listen_port"] = "18000.0"
        self.assertEqual(bridge._port(), 18000)


class PageBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_has_token_set_but_no_plaintext(self):
        bridge = make_bridge({"token": "secret"})
        bridge.server.host = "127.0.0.1"

        async def voice_info(_kind):
            return {"id": ""}

        bridge._voice_info = voice_info
        with fake_responses():
            result = await bridge.page_status()
        data = result["data"]
        self.assertNotIn("token", data)
        self.assertTrue(data["tokenSet"])
        self.assertEqual(list(data).count("listening"), 1)

    async def test_token_get_still_returns_plaintext(self):
        bridge = make_bridge({"token": "secret"})
        bridge.server.host = "127.0.0.1"
        with fake_responses():
            result = await bridge.page_token()
        self.assertEqual(result["data"]["token"], "secret")
        self.assertTrue(result["data"]["tokenSet"])

    async def test_tools_returns_granted_without_exec_under_star(self):
        rows = [
            {"name": "read_tool", "risk": "read", "active": True},
            {"name": "exec_tool", "risk": "exec", "active": True},
        ]
        tools = FakeTools(
            allow=["*"], catalog=rows, enabled=[rows[0]]
        )
        bridge = make_bridge(tools=tools)
        with fake_responses():
            result = await bridge.page_tools()
        data = result["data"]
        self.assertTrue(data["all"])
        self.assertEqual(data["granted"], ["read_tool"])
        self.assertNotIn("exec_tool", data["granted"])
