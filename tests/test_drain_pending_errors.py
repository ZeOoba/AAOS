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
import os
import shutil
import sys
import tempfile
import time
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


def _age(path: Path, seconds: float) -> None:
    """Backdate a file's mtime by `seconds` (relative to the wall clock the grace check uses)."""
    t = time.time() - seconds
    os.utime(str(path), (t, t))


def _old(path: Path) -> None:
    """Age a file past the quarantine grace window, so a parse/decode failure is final."""
    _age(path, aaos.QUARANTINE_GRACE_SECONDS + 60)


class _PendingTestBase(unittest.TestCase):
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


class PendingReadErrors(_PendingTestBase):
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
        _old(self.spool / "0000-bad.json")  # past the grace window: a genuinely corrupt old file
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
        _old(self.spool / "0001-not-json.json")  # parse failures get a grace window (may be half-synced)
        # wrong-shape but complete JSON is quarantined at once, even when fresh:
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


class PendingReadReviewFixes(_PendingTestBase):
    """Review fixes on PR #21: read_events retries and flags, a truthful quarantine message that cannot
    abort the drain, a grace window before a half-synced file is quarantined, and BOM tolerance."""

    def test_read_events_retries_a_transient_permission_error_and_returns_the_event(self):
        path = aaos.spool_event(_event(1), PROJECT, root=self.root)
        fake, state = self._read_text_failing_for(path.name, times=1)
        err = io.StringIO()
        with mock.patch.object(Path, "read_text", fake), mock.patch.object(aaos.time, "sleep", lambda _s: None), \
                redirect_stderr(err):
            got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]
        self.assertGreaterEqual(state["n"], 1, "the simulated error never fired")
        self.assertEqual(got, [_event(1)["event_id"]], "a briefly locked pending event was silently dropped")

    def test_read_events_warns_on_a_persistently_unreadable_pending_file(self):
        path = aaos.spool_event(_event(1), PROJECT, root=self.root)
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        fake, state = self._read_text_failing_for(path.name, times=None)
        err = io.StringIO()
        with mock.patch.object(Path, "read_text", fake), mock.patch.object(aaos.time, "sleep", lambda _s: None), \
                redirect_stderr(err):
            got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]
        self.assertGreaterEqual(state["n"], 1, "the simulated error never fired")
        self.assertEqual(got, [_event(2)["event_id"]])
        self.assertIn("WARNING", err.getvalue())
        self.assertIn(path.name, err.getvalue(), "an unreadable pending file must be flagged, not skipped silently")

    def test_pending_read_retry_is_bounded_with_meaningful_backoff(self):
        path = aaos.spool_event(_event(1), PROJECT, root=self.root)
        fake, state = self._read_text_failing_for(path.name, times=None)
        sleeps = []
        with mock.patch.object(Path, "read_text", fake), mock.patch.object(aaos.time, "sleep", sleeps.append):
            status, _ = aaos._read_pending_text(path)
        self.assertEqual(status, "transient")
        self.assertEqual(state["n"], 3, "expected exactly 3 read attempts")
        self.assertEqual(sleeps, [0.05, 0.1])

    def test_quarantine_failure_message_is_truthful(self):
        bad = self.spool / "0000-bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        _old(bad)
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        real_replace = aaos.os.replace

        def replace(src, dst, *a, **k):
            if "_corrupt" in str(dst):
                raise PermissionError(13, "simulated: cannot move into _corrupt")
            return real_replace(src, dst, *a, **k)

        with mock.patch.object(aaos.os, "replace", replace):
            n, err = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(2)["event_id"]])
        self.assertTrue(bad.exists(), "the move failed, so the file must still be in pending/")
        self.assertNotIn("moved to", err, f"warning claims a move that did not happen: {err!r}")
        self.assertIn("could not move", err)
        self.assertIn("0000-bad.json", err)

    def test_a_corrupt_dir_makedirs_failure_does_not_abort_the_drain(self):
        bad = self.spool / "0000-bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        _old(bad)
        aaos.spool_event(_event(3), PROJECT, root=self.root)
        real_makedirs = aaos.os.makedirs

        def makedirs(path, *a, **k):
            if "_corrupt" in str(path):
                raise PermissionError(13, "simulated: cannot create _corrupt")
            return real_makedirs(path, *a, **k)

        with mock.patch.object(aaos.os, "makedirs", makedirs):
            n, err = self._drain()  # must not raise
        self.assertEqual(n, 1, "the valid event behind the bad file must still fold")
        self.assertEqual(self._ids_in_ndjson(), [_event(3)["event_id"]])
        self.assertTrue(bad.exists())
        self.assertIn("could not move", err)

    def test_a_fresh_truncated_multibyte_file_is_deferred_not_quarantined(self):
        full = json.dumps(dict(_event(1), detail="café"), ensure_ascii=False).encode("utf-8")
        half = self.spool / "0000-half.json"
        half.write_bytes(full[: full.index(b"\xc3") + 1])  # cut inside the 2-byte UTF-8 sequence
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        n, err = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(2)["event_id"]])
        self.assertTrue(half.exists(), "a fresh half-synced file must stay in pending/")
        self.assertEqual(self._quarantined(), [], "a fresh half-synced file was quarantined with no grace period")
        self.assertIn("0000-half.json", err)
        # the sync completes: the next drain folds it
        half.write_bytes(full)
        n2, _ = self._drain()
        self.assertEqual(n2, 1)
        self.assertEqual(sorted(self._ids_in_ndjson()), sorted([_event(1)["event_id"], _event(2)["event_id"]]))

    def test_a_fresh_truncated_ascii_json_file_is_deferred_not_quarantined(self):
        half = self.spool / "0000-half.json"
        half.write_text(json.dumps(_event(1))[:20], encoding="utf-8")
        n, err = self._drain()
        self.assertEqual(n, 0)
        self.assertTrue(half.exists())
        self.assertEqual(self._quarantined(), [])
        self.assertIn("0000-half.json", err)

    def test_a_file_just_inside_the_grace_window_is_still_deferred(self):
        half = self.spool / "0000-half.json"
        half.write_text("{not valid", encoding="utf-8")
        _age(half, aaos.QUARANTINE_GRACE_SECONDS - 60)
        self._drain()
        self.assertTrue(half.exists())
        self.assertEqual(self._quarantined(), [])

    def test_a_future_dated_unparseable_file_is_deferred(self):
        # Clock skew across machines: mtime up to the window in the FUTURE is still "fresh" (abs(age)).
        half = self.spool / "0000-half.json"
        half.write_text("{not valid", encoding="utf-8")
        _age(half, -300)
        self._drain()
        self.assertTrue(half.exists())
        self.assertEqual(self._quarantined(), [])

    def test_a_far_future_dated_unparseable_file_is_quarantined_not_deferred_forever(self):
        # A peer clock hours ahead must not park a bad file in pending/ indefinitely: abs(age) is outside
        # the window on either side, so it is quarantined like any other old bad file.
        bad = self.spool / "0000-bad.json"
        bad.write_text("{not valid", encoding="utf-8")
        _age(bad, -7200)
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        n, _ = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._quarantined(), ["0000-bad.json"])
        self.assertFalse(bad.exists())

    def test_an_unstatable_unparseable_file_is_deferred_never_quarantined_on_a_guess(self):
        half = self.spool / "0000-half.json"
        half.write_text("{not valid", encoding="utf-8")
        _old(half)  # old enough to quarantine if its age were known
        real_stat = Path.stat

        def stat(self_, *a, **k):
            # Fail only the grace check's own stat so listing (is_file) still works.
            if self_.name == "0000-half.json" and sys._getframe(1).f_code.co_name == "_within_quarantine_grace":
                raise PermissionError(13, "simulated: cannot stat")
            return real_stat(self_, *a, **k)

        with mock.patch.object(Path, "stat", stat):
            self._drain()
        self.assertTrue(half.exists())
        self.assertEqual(self._quarantined(), [])

    def test_an_old_truncated_multibyte_file_is_quarantined_and_does_not_wedge(self):
        full = json.dumps(dict(_event(1), detail="café"), ensure_ascii=False).encode("utf-8")
        half = self.spool / "0000-half.json"
        half.write_bytes(full[: full.index(b"\xc3") + 1])
        _old(half)
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        n, err = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(2)["event_id"]])
        self.assertEqual(self._quarantined(), ["0000-half.json"])
        self.assertIn("moved to", err)

    def test_a_bom_prefixed_pending_file_folds(self):
        (self.spool / "0000-bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(_event(1)).encode("utf-8"))
        n, _ = self._drain()
        self.assertEqual(n, 1)
        self.assertEqual(self._ids_in_ndjson(), [_event(1)["event_id"]])
        self.assertEqual(self._quarantined(), [])

    def test_read_events_accepts_a_bom_prefixed_pending_file(self):
        (self.spool / "0000-bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(_event(1)).encode("utf-8"))
        got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]
        self.assertEqual(got, [_event(1)["event_id"]])


if __name__ == "__main__":
    unittest.main()
