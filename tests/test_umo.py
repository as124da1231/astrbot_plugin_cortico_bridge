import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# bridge.py / deliver.py 只依赖 astrbot.api.logger；测试时给一个静默 logger。
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

# deliver.send 在函数内 import 消息组件；测试给一套最小替身。
components = types.ModuleType("astrbot.api.message_components")


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    @classmethod
    def fromBase64(cls, _b64):
        return cls()


class _Record:
    @classmethod
    def fromFileSystem(cls, _path):
        return cls()


class _Reply:
    def __init__(self, id):
        self.id = id


class _At:
    def __init__(self, qq, name=""):
        self.qq = qq
        self.name = name


components.Plain = _Plain
components.Image = _Image
components.Record = _Record
components.Reply = _Reply
components.At = _At

event_mod = types.ModuleType("astrbot.api.event")


class _MessageChain:
    def __init__(self):
        self.chain = []

    def append(self, item):
        self.chain.append(item)


event_mod.MessageChain = _MessageChain
api.message_components = components
api.event = event_mod
sys.modules.setdefault("astrbot.api.message_components", components)
sys.modules.setdefault("astrbot.api.event", event_mod)


class _SendContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True

import bridge  # noqa: E402
from deliver import Delivery  # noqa: E402


class UmoContractTests(unittest.TestCase):
    def test_shared_vectors(self):
        cases = json.loads(
            (ROOT / "shared" / "umo-vectors.json").read_text(encoding="utf-8")
        )
        for case in cases:
            parsed = bridge.parse_umo(case["in"])
            with self.subTest(case=case["in"]):
                if not case["ok"]:
                    self.assertIsNone(parsed)
                    continue
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed[0], case["platform"])
                self.assertEqual(parsed[1], case["kind"])
                self.assertEqual(parsed[2], case["id"])

    def test_delivery_known_umo_and_legacy_mapping(self):
        delivery = Delivery(None, lambda: {})
        umo = "default:FriendMessage:100000001"
        delivery.remember("default", "private", "100000001", umo)
        self.assertTrue(delivery.has_umo(umo))
        self.assertEqual(delivery.umo_for("default", "private", "100000001"), umo)
        self.assertFalse(delivery.has_umo("default:FriendMessage:123456788"))

    def test_delivery_ttl(self):
        import time

        delivery = Delivery(None, lambda: {})
        umo = "default:GroupMessage:123456789"
        delivery.remember("default", "group", "123456789", umo)
        delivery._umos[umo]["lastSeen"] = time.time() - 31 * 24 * 3600
        self.assertFalse(delivery.has_umo(umo))
        self.assertEqual(delivery.known_conversations(), [])

    def test_v1_registry_migrates_to_v2_and_backs_up(self):
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "umos.json"
            path.write_text(
                json.dumps(
                    {
                        "default|group|123456789": {
                            "umo": "default:GroupMessage:123456789",
                            "ts": time.time(),
                        }
                    }
                ),
                encoding="utf-8",
            )
            delivery = Delivery(None, lambda: {}, data_dir=tmp)
            self.assertTrue(delivery.has_umo("default:GroupMessage:123456789"))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("version"), 2)
            self.assertIn("default:GroupMessage:123456789", data.get("umos", {}))
            self.assertTrue((Path(tmp) / "umos.v1.json").exists())

    def test_v1_registry_stays_intact_until_backup_succeeds(self):
        import tempfile
        import time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "umos.json"
            backup = Path(tmp) / "umos.v1.json"
            umo = "default:GroupMessage:123456789"
            original = json.dumps({"default|group|123456789": [umo, time.time()]})
            path.write_text(original, encoding="utf-8")

            def fail_during_copy(_source, destination):
                Path(destination).write_bytes(b"partial")
                raise OSError("disk full")

            with patch("deliver.shutil.copyfile", side_effect=fail_during_copy):
                delivery = Delivery(None, lambda: {}, data_dir=tmp)
                self.assertTrue(delivery.has_umo(umo))
                delivery.flush()
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                self.assertEqual(list(Path(tmp).iterdir()), [path])

            delivery.flush()
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertIn(umo, saved["umos"])

    def test_send_without_umo_is_refused(self):
        import asyncio

        delivery = Delivery(None, lambda: {})
        ok, _note, _voice, _uncertain, error_code = asyncio.run(
            delivery.send("default", "group", "123456789", "hi")
        )
        self.assertFalse(ok)
        self.assertEqual(error_code, "missing_umo")

    def test_send_with_unknown_umo_is_refused(self):
        import asyncio

        delivery = Delivery(None, lambda: {})
        ok, _note, _voice, _uncertain, error_code = asyncio.run(
            delivery.send("", "", "", "hi", umo="default:GroupMessage:999")
        )
        self.assertFalse(ok)
        self.assertEqual(error_code, "unknown_umo")

    def test_send_with_umo_and_missing_conv_uses_umo(self):
        import asyncio

        ctx = _SendContext()
        delivery = Delivery(ctx, lambda: {"max_sends_per_minute": 0})
        umo = "default:FriendMessage:100000001"
        delivery.remember("default", "private", "100000001", umo)

        ok, _note, _voice, _uncertain, error_code = asyncio.run(
            delivery.send("", "", "", "hi", umo=umo)
        )
        self.assertTrue(ok)
        self.assertEqual(ctx.sent[-1][0], umo)

    def test_send_with_umo_ignores_conflicting_conv(self):
        import asyncio

        ctx = _SendContext()
        delivery = Delivery(ctx, lambda: {"max_sends_per_minute": 0})
        umo = "default:FriendMessage:100000001"
        delivery.remember("default", "private", "100000001", umo)

        # conv 写成别的群：交付层以 umo 为准；帧层是否拒绝由 bridge 负责。
        ok, _note, _voice, _uncertain, error_code = asyncio.run(
            delivery.send("default", "group", "999999", "hi", umo=umo)
        )
        self.assertTrue(ok)
        self.assertEqual(ctx.sent[-1][0], umo)

    def test_registry_cap_evicts_oldest(self):
        delivery = Delivery(None, lambda: {})
        for i in range(501):
            umo = f"default:GroupMessage:{i}"
            delivery._umos[umo] = {
                "umo": umo,
                "platform": "default",
                "kind": "group",
                "id": str(i),
                "name": "",
                "firstSeen": float(i),
                "lastSeen": float(i),
                "lastMessageId": "",
                "lastSender": "",
            }
            delivery._legacy[f"default|group|{i}"] = umo

        delivery._prune()

        self.assertEqual(len(delivery._umos), 500)
        self.assertNotIn("default:GroupMessage:0", delivery._umos)
        self.assertNotIn("default|group|0", delivery._legacy)
        self.assertIn("default:GroupMessage:500", delivery._umos)

    def test_delivery_flush_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            delivery = Delivery(None, lambda: {}, data_dir=tmp)
            umo = "default:GroupMessage:123456789"
            delivery.remember("default", "group", "123456789", umo)
            delivery.flush()

            restored = Delivery(None, lambda: {}, data_dir=tmp)
            self.assertTrue(restored.has_umo(umo))
            self.assertEqual(restored.umo_for("default", "group", "123456789"), umo)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def _server(self, host: str, token: str = ""):
        async def on_send(*_args, **_kwargs):
            return True, "", False, False

        return bridge.BridgeServer(host=host, port=0, token=token, on_send=on_send)

    class _Ws:
        def __init__(self):
            self.closed: list[tuple[int, str]] = []

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.closed.append((code, reason))

    class _FakeWs(_Ws):
        def __init__(self, frames, headers=None, path: str = "/"):
            super().__init__()
            self._frames = list(frames)
            self.request = types.SimpleNamespace(headers=headers or {}, path=path)
            self.remote_address = ("127.0.0.1", 4321)
            self.sent: list[str] = []

        async def recv(self):
            return self._frames.pop(0)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._frames:
                return self._frames.pop(0)
            raise StopAsyncIteration

        async def send(self, data) -> None:
            self.sent.append(data)

    async def test_non_loopback_without_token_refuses_to_bind(self):
        server = await self._server("0.0.0.0")
        started = await server.start()
        self.assertFalse(started)
        self.assertFalse(server.listening)
        self.assertIn("token", server.listen_error)

    async def test_loopback_without_token_is_not_refused_for_token(self):
        server = await self._server("127.0.0.1")
        try:
            await server.start()
            self.assertNotIn("token", server.listen_error)
        finally:
            await server.stop()

    def test_browser_origin_is_refused(self):
        self.assertEqual(
            bridge.connection_refusal({"origin": "http://127.0.0.1:3000"}, "/"),
            "origin",
        )
        self.assertEqual(
            bridge.connection_refusal({"Origin": "https://evil.example"}, "/"),
            "origin",
        )

    def test_query_token_is_refused(self):
        self.assertEqual(
            bridge.connection_refusal({}, "/?token=abc"), "query_token"
        )
        self.assertEqual(
            bridge.connection_refusal({}, "/?access_token=abc"), "query_token"
        )

    def test_native_client_passes(self):
        self.assertEqual(bridge.connection_refusal({}, "/"), "")
        self.assertEqual(bridge.connection_refusal(None, "/ws"), "")

    def test_bearer_token_is_read_from_header(self):
        self.assertEqual(
            bridge.bearer_token({"authorization": "Bearer abc123"}), "abc123"
        )
        self.assertEqual(
            bridge.bearer_token({"Authorization": "bearer   spaced"}), "spaced"
        )
        self.assertEqual(bridge.bearer_token({"authorization": "Basic abc"}), "")
        self.assertEqual(bridge.bearer_token({"authorization": ""}), "")
        self.assertEqual(bridge.bearer_token(None), "")

    def test_hello_version(self):
        self.assertEqual(bridge.hello_version({}), 1)
        self.assertEqual(bridge.hello_version({"v": 1}), 1)
        self.assertEqual(bridge.hello_version({"v": "1"}), 1)
        self.assertEqual(bridge.hello_version({"v": 2}), 2)
        self.assertEqual(bridge.hello_version({"v": True}), 0)
        self.assertEqual(bridge.hello_version({"v": "x"}), 0)
        self.assertEqual(bridge.hello_version({"v": None}), 0)

    def test_token_equal_handles_non_ascii(self):
        self.assertTrue(bridge.token_equal("口令", "口令"))
        self.assertFalse(bridge.token_equal("口令", "口令2"))
        self.assertFalse(bridge.token_equal("", "口令"))

    async def test_authorize_prefers_header(self):
        server = await self._server("127.0.0.1", token="s3cret")
        ws = self._Ws()
        ok = await server._authorize(
            ws, {"authorization": "Bearer s3cret"}, {"t": "hello"}, "1.2.3.4:1"
        )
        self.assertTrue(ok)
        self.assertEqual(ws.closed, [])

    async def test_authorize_rejects_header_frame_mismatch(self):
        server = await self._server("127.0.0.1", token="s3cret")
        ws = self._Ws()
        ok = await server._authorize(
            ws,
            {"authorization": "Bearer s3cret"},
            {"t": "hello", "token": "other"},
            "1.2.3.4:1",
        )
        self.assertFalse(ok)
        self.assertEqual(ws.closed, [(4003, "bad token")])

    async def test_authorize_rejects_frame_token(self):
        server = await self._server("127.0.0.1", token="s3cret")
        ws = self._Ws()
        ok = await server._authorize(
            ws, None, {"t": "hello", "token": "s3cret"}, "1.2.3.4:1"
        )
        self.assertFalse(ok)
        self.assertEqual(ws.closed, [(4003, "bad token")])

    async def test_authorize_rejects_wrong_token(self):
        server = await self._server("127.0.0.1", token="s3cret")
        ws = self._Ws()
        ok = await server._authorize(
            ws, {"authorization": "Bearer nope"}, {"t": "hello"}, "1.2.3.4:1"
        )
        self.assertFalse(ok)
        self.assertEqual(ws.closed, [(4003, "bad token")])
        self.assertEqual(server.bad_token_refused, 1)

    async def test_unknown_protocol_version_is_refused(self):
        server = await self._server("127.0.0.1")
        ws = self._FakeWs([json.dumps({"t": "hello", "v": 99})])
        await server._handle_client(ws)
        self.assertEqual(ws.closed, [(4004, "unsupported protocol version")])
        self.assertEqual(server.version_refused, 1)

    async def test_header_token_handshake_registers_client(self):
        server = await self._server("127.0.0.1", token="s3cret")

        async def no_refresh():
            return None

        server._refresh_ready_later = no_refresh
        ws = self._FakeWs(
            [json.dumps({"t": "hello", "v": bridge.PROTOCOL_VERSION})],
            headers={"authorization": "Bearer s3cret"},
        )
        await server._handle_client(ws)
        self.assertEqual(ws.closed, [])
        self.assertTrue(any('"ready"' in frame for frame in ws.sent))

    def test_command_prefix_is_detected(self):
        self.assertTrue(bridge.looks_like_command("/help", ["/"]))
        self.assertTrue(bridge.looks_like_command("  /help", ["/"]))
        self.assertFalse(bridge.looks_like_command("你好", ["/"]))
        self.assertFalse(bridge.looks_like_command("", ["/"]))
        self.assertTrue(bridge.looks_like_command("!help", ["!"]))
        self.assertFalse(bridge.looks_like_command("/help", []))

    def test_prefixless_command_matches_registered_name(self):
        self.assertTrue(bridge.matches_command("help", {"help"}))
        self.assertTrue(bridge.matches_command("help me", {"help"}))
        self.assertTrue(bridge.matches_command("  帮助   菜单 ", {"帮助 菜单"}))
        self.assertFalse(bridge.matches_command("hello", {"help"}))
        self.assertFalse(bridge.matches_command("", {"help"}))
        self.assertFalse(bridge.matches_command("help", set()))


if __name__ == "__main__":
    unittest.main()
