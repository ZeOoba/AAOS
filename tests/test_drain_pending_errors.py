"""tests/test_drain_pending_errors.py -- how the drain treats a pending event file it cannot read.

Found by an independent adversarial review (AMD-0004 section B). Before this fix:
  * ANY OSError other than FileNotFoundError while reading a pending file (a OneDrive or antivirus
    sharing violation is the everyday case) moved a perfectly valid event to pending/_corrupt/, where
    no reader looks, so the event vanished from every view.
  * A pending file that is not valid UTF-8 raised UnicodeDecodeError out of drain_event_spool and
    out of read_events. It sorts first, so one such file wedged every later drain and crashed every
    reader.

Contract tested here: a transient read error leaves the file in pending/ (bounded retry inside the
drain, then retried at the next drain); only a file that was READ and is genuinely bad (undecodable,
malformed JSON, not an object, no event_id) is quarantined; and one bad file never blocks the others
or crashes a reader. Temp trees only.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

PROJECT = "ora-ops"
_REAL_READ_TEXT = Path.read_text


def _event(n: int) -> dict:
    return {
        "protocol_version": "1.0", "event_id": f"e-2026-07-09T12:00:0{n}-04:00-{n}", "type": "TaskCreated",
        "actor": "system", "timestamp": f"2026-07-09T12:00:0{n}-04:00", "project_id": PROJECT,
        "refs": {"task": f"t-{n}"},
    }


class PendingReadErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-pending-err-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = str(self.tmp)
        self.spool = aaos.event_spool_dir(PROJECT, root=self.root)
        self.corrupt = aaos._pending_corrupt_dir(PROJECT, root=self.root)
        self.spool.mkdir(parents=True)

    def _drain(self):
        err = io.StringIO()
        with redirect_stderr(err):
            n = aaos.drain_event_spool(PROJECT, root=self.root)
        return n, err.getvalue()

    def _ids_in_ndjson(self):
        log = aaos.events_log_path(PROJECT, root=self.root)
        if not log.exists():
            return []
        return [json.loads(l)["event_id"] for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _quarantined(self):
        return sorted(p.name for p in self.corrupt.iterdir()) if self.corrupt.exists() else []

    def _read_text_failing_for(self, name, times):
        """Path.read_text that raises PermissionError for `name` the first `times` calls (None: always)."""
        state = {"n": 0}

        def read_text(path_self, *args, **kwargs):
            if path_self.name == name and (times is None or state["n"] < times):
                state["n"] += 1
                raise PermissionError(13, "used by another process (simulated sharing violation)")
            return _REAL_READ_TEXT(path_self, *args, **kwargs)

        return read_text, state

    def test_a_brief_sharing_violation_is_retried_inside_the_drain_and_the_event_folds(self):
        path = aaos.spool_event(_event(1), PROJECT, root=self.root)
        fake, state = self._read_text_failing_for(path.name, times=1)
        with mock.patch.object(Path, "read_text", fake), mock.patch.object(aaos.time, "sleep", lambda _s: None):
            n, _ = self._drain()
        self.assertGreaterEqual(state["n"], 1, "the simulated error never fired")
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(1)["event_id"]])
        self.assertEqual(self._quarantined(), [], "a valid event was quarantined on a transient error")

    def test_a_persistent_read_error_leaves_the_file_pending_and_it_folds_on_the_next_drain(self):
        path = aaos.spool_event(_event(1), PROJECT, root=self.root)
        fake, state = self._read_text_failing_for(path.name, times=None)
        with mock.patch.object(Path, "read_text", fake), mock.patch.object(aaos.time, "sleep", lambda _s: None):
            n, err = self._drain()
        self.assertGreaterEqual(state["n"], 1, "the simulated error never fired")
        self.assertEqual(n, 0)
        self.assertTrue(path.exists(), "the unreadable-for-now file left pending/")
        self.assertEqual(self._quarantined(), [], "a valid event was quarantined on a read error")
        self.assertEqual(self._ids_in_ndjson(), [])
        self.assertIn(path.name, err, "the stuck file must be named in a warning")
        # the sharing violation clears: the next drain folds it, exactly once
        n2, _ = self._drain()
        self.assertEqual(n2, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(1)["event_id"]])
        self.assertEqual([e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)], [_event(1)["event_id"]])

    def test_undecodable_bytes_are_quarantined_and_do_not_wedge_the_drain(self):
        (self.spool / "0000-bad.json").write_bytes(b"\xff\xfe\x00 this is not utf-8")
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        n, _ = self._drain()  # must not raise
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(2)["event_id"]])
        self.assertEqual(self._quarantined(), ["0000-bad.json"])

    def test_read_events_survives_an_undecodable_pending_file(self):
        (self.spool / "0000-bad.json").write_bytes(b"\xff\xfe\x00 this is not utf-8")
        aaos.spool_event(_event(3), PROJECT, root=self.root)
        got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]  # must not raise
        self.assertEqual(got, [_event(3)["event_id"]])

    def test_genuinely_malformed_files_are_still_quarantined(self):
        """Regression guard for the behaviour that must NOT change."""
        (self.spool / "0001-not-json.json").write_text("{not valid json", encoding="utf-8")
        (self.spool / "0002-not-object.json").write_text("[1, 2, 3]", encoding="utf-8")
        (self.spool / "0003-no-id.json").write_text(json.dumps({"type": "TaskCreated"}), encoding="utf-8")
        aaos.spool_event(_event(4), PROJECT, root=self.root)
        n, _ = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._quarantined(), ["0001-not-json.json", "0002-not-object.json", "0003-no-id.json"])
        self.assertEqual(self._ids_in_ndjson(), [_event(4)["event_id"]])

    def test_a_vanished_file_is_still_skipped_with_a_warning(self):
        path = aaos.spool_event(_event(5), PROJECT, root=self.root)
        real_listdir_target = path.name

        def read_text(path_self, *args, **kwargs):
            if path_self.name == real_listdir_target:
                raise FileNotFoundError(2, "gone")
            return _REAL_READ_TEXT(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            n, err = self._drain()
        self.assertEqual(n, 0)
        self.assertIn("vanished", err)
        self.assertEqual(self._quarantined(), [])


if __name__ == "__main__":
    unittest.main()
