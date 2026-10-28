"""Unit tests for the server's pure helpers.

These cover the pieces that must hold without a paired Link
client or a Muse account: the bridge wake dialects and the
registry validator contract (docs/registry-sync.md). Run with:

    python3 -m unittest discover -s tests

The server module imports the MCP SDK (see requirements.txt);
the musegadget SDK is imported lazily inside the Link helpers,
so importing server.py here does not need a pairing.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class WakeFrameTests(unittest.TestCase):
    corr = "12345678-1234-1234-1234-123456789abc"
    path = Path("/tmp/bridge-queue/12345678-1234-1234-1234-123456789abc.json")

    def test_plain_frame_names_tool_and_files(self):
        frame = server._plain_wake_frame(
            "muse_chat_create", {"name": "Test"}, self.corr, self.path)
        self.assertIn("muse_chat_create", frame)
        self.assertIn(self.corr, frame)
        self.assertIn(str(self.path), frame)
        self.assertNotIn("§CANT", frame)

    def test_cant_frame_shape(self):
        frame = server._cant_wake_frame(
            "muse_chat_create", {"name": "Test"}, self.corr, self.path)
        lines = frame.splitlines()
        self.assertEqual(lines[0], "§CANT/1 REQ 12345678")
        self.assertIn("op=CRT", lines)
        self.assertEqual(lines[-1], "§/CANT 12345678")

    def test_dispatcher_follows_configuration(self):
        frame, fmt = server._wake_frame(
            "muse_chat_read", {"chat_id": "x", "limit": 5},
            self.corr, self.path)
        self.assertEqual(fmt, server._WAKE_FORMAT)
        self.assertTrue(frame)


def _registry(entries, total=None, registered=None, drafts=None):
    reg = sum(1 for e in entries if e.get("registered"))
    return {
        "type": "registry_sync_result",
        "entries": entries,
        "counts": {
            "enumerated_total": len(entries) if total is None else total,
            "registered": reg if registered is None else registered,
            "unregistered_drafts":
                len(entries) - reg if drafts is None else drafts,
        },
    }


class RegistryValidatorTests(unittest.TestCase):
    def test_valid_registry_passes(self):
        data = _registry([
            {"chat_id": "a", "registered": True},
            {"chat_id": "b", "registered": False},
        ])
        self.assertIsNone(server._validate_registry(data))

    def test_enumerated_total_must_equal_entry_count(self):
        # The producer defect this contract exists to catch:
        # counting something other than the entry list.
        data = _registry([{"chat_id": "a", "registered": True}], total=0)
        self.assertIn("enumerated_total", server._validate_registry(data))

    def test_duplicate_chat_id_rejected(self):
        data = _registry([
            {"chat_id": "a", "registered": True},
            {"chat_id": "a", "registered": False},
        ])
        self.assertIn("duplicate", server._validate_registry(data))


class RefusedFromTests(unittest.TestCase):
    def test_generic_operator_names_refused_by_default(self):
        self.assertIn("user", server._REFUSED_FROM)
        self.assertIn("owner", server._REFUSED_FROM)


if __name__ == "__main__":
    unittest.main()
