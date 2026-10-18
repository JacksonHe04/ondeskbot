import tempfile
import unittest
from pathlib import Path

from ondeskbot.capabilities import CapabilityRegistry
from ondeskbot.command_processor import CommandProcessor
from ondeskbot.identity import DeviceIdentity
from ondeskbot.runtime_state import RuntimeState


class CommandProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.identity = DeviceIdentity(
            str(Path(self.temp.name) / "identity.json"), b"chip")
        self.identity.apply_registration({
            "device_id": "device-1", "device_secret": "secret"
        })
        self.state = RuntimeState(now_provider=lambda: 1000)
        self.registry = CapabilityRegistry()
        self.calls = []
        self.registry.register(
            "test.read", lambda args: self.calls.append(args) or {"value": 1},
            "test")
        self.processor = CommandProcessor(
            self.identity, self.registry, self.state, now_provider=lambda: 1000)

    def tearDown(self):
        self.temp.cleanup()

    def command(self, request_id="r1"):
        return {"message_type": "command", "payload": {
            "request_id": request_id, "capability": "test.read", "args": {},
            "issued_at_ms": 900, "expires_at_ms": 2000,
        }}

    def test_duplicate_request_returns_cache_without_reexecution(self):
        first = self.processor.handle(self.command())
        second = self.processor.handle(self.command())
        self.assertTrue(first["payload"]["ok"])
        self.assertFalse(first["payload"]["cached"])
        self.assertTrue(second["payload"]["cached"])
        self.assertEqual(len(self.calls), 1)

    def test_expired_command_is_rejected(self):
        message = self.command("expired")
        message["payload"]["expires_at_ms"] = 999
        response = self.processor.handle(message)
        self.assertFalse(response["payload"]["ok"])
        self.assertEqual(response["payload"]["error"]["code"], "COMMAND_EXPIRED")


if __name__ == "__main__":
    unittest.main()
