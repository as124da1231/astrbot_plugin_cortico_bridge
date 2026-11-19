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
        self.assertFalse(delivery.has_umo("default:FriendMessage:999999999"))

    def test_delivery_ttl(self):
        import time

        delivery = Delivery(None, lambda: {})
        umo = "default:GroupMessage:123456789"
        delivery.remember("default", "group", "123456789", umo)
        delivery._umos["default|group|123456789"] = (
            umo,
            time.time() - 31 * 24 * 3600,
        )
        self.assertFalse(delivery.has_umo(umo))
        self.assertEqual(delivery.known_conversations(), [])

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


if __name__ == "__main__":
    unittest.main()
