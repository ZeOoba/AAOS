"""tests/test_ndjson_line_robustness.py -- the drain and read_events must survive NDJSON lines that
parse as JSON but are not usable event objects (A1d-lite-1, audit finding #1 drain half).

Before this fix, drain_event_spool's existing-id scan did `existing_ids.add(json.loads(line).get(...))`
inside a try that caught only JSONDecodeError. A line such as `[]`, `1`, `"x"` or `null` raised
AttributeError, and a dict line whose event_id is unhashable (`{"event_id": []}`) raised TypeError.
Either aborted every drain before any write. emit_event swallows drain failures as a stderr WARNING,
so the wedge was silent: every later event stayed in pending/ forever. read_events had the same
TypeError on an unhashable event_id (`event_id in seen_ids`), which crashed every reader.

Contract tested here: such lines are skipped for dedup purposes only; the existing NDJSON content is
preserved verbatim (LF seed -- Path.read_text translates CRLF, which is pre-existing and out of scope);
new events fold exactly once; the pending file moves to _drained/; a second drain is a no-op; and
emit_event prints no drain-failure WARNING. Synthetic ids, temp trees only -- never the live seam.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

PROJECT = "ora-ops"

# Lines that are valid JSON but not usable event objects. All must be tolerated.
NON_OBJECT_LINES = ["[]", "1", '"x"', "null"]
UNHASHABLE_ID_LINES = ['{"event_id": []}', '{"event_id": {}}']


def _event(n: int) -> dict:
    return {
        "protocol_version": "1.0", "event_id": f"e-2026-07-09T12:00:0{n}-04:00-{n}", "type": "TaskCreated",
        "actor": "system", "timestamp": f"2026-07-09T12:00:0{n}-04:00", "project_id": PROJECT,
        "refs": {"task": f"t-{n}"},
    }


class _SeamBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-ndjson-robust-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = str(self.tmp)
        self.log = aaos.events_log_path(PROJECT, root=self.root)
        self.spool = aaos.event_spool_dir(PROJECT, root=self.root)
        self.drained = aaos._pending_drained_dir(PROJECT, root=self.root)
        self.log.parent.mkdir(parents=True)

    def _seed(self, extra_lines):
        lines = [json.dumps(_event(1))] + list(extra_lines)
        data = ("\n".join(lines) + "\n").encode("utf-8")  # LF seed
        self.log.write_bytes(data)
        return data

    def _ndjson_ids(self):
        ids = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                ids.append(obj.get("event_id"))
        return ids

    def _drain(self):
        err = io.StringIO()
        with redirect_stderr(err):
            n = aaos.drain_event_spool(PROJECT, root=self.root)
        return n, err.getvalue()


class DrainToleratesUnusableLines(_SeamBase):
    def _assert_drain_survives(self, bad_lines):
        seeded = self._seed(bad_lines)
        pending = aaos.spool_event(_event(2), PROJECT, root=self.root)

        n, _ = self._drain()  # at HEAD: AttributeError / TypeError escapes here

        self.assertEqual(n, 1)
        result = self.log.read_bytes()
        self.assertTrue(result.startswith(seeded), "existing NDJSON bytes were not preserved verbatim")
        self.assertEqual(self._ndjson_ids().count(_event(2)["event_id"]), 1, "new event not folded exactly once")
        self.assertFalse(pending.exists(), "pending file was not moved out of pending/")
        self.assertTrue((self.drained / pending.name).exists(), "pending file did not land in _drained/")

        # Second drain is a no-op: nothing pending, NDJSON unchanged.
        n2, _ = self._drain()
        self.assertEqual(n2, 0)
        self.assertEqual(self.log.read_bytes(), result)

    def test_non_object_lines(self):
        self._assert_drain_survives(NON_OBJECT_LINES)

    def test_dict_lines_with_unhashable_event_id(self):
        self._assert_drain_survives(UNHASHABLE_ID_LINES)

    def test_all_unusable_lines_together(self):
        self._assert_drain_survives(NON_OBJECT_LINES + UNHASHABLE_ID_LINES)

    def test_dedup_against_valid_lines_is_unchanged(self):
        # A pending event whose id is already in the NDJSON must still be skipped (not re-appended),
        # with unusable lines present around it.
        self._seed(NON_OBJECT_LINES + UNHASHABLE_ID_LINES)
        aaos.spool_event(_event(1), PROJECT, root=self.root)
        before = self.log.read_bytes()
        n, _ = self._drain()
        self.assertEqual(n, 1, "the duplicate pending file should still be moved to _drained/")
        self.assertEqual(self.log.read_bytes(), before, "a duplicate event was re-appended")

    def test_emit_event_prints_no_drain_failure_warning(self):
        self._seed(NON_OBJECT_LINES + UNHASHABLE_ID_LINES)
        err = io.StringIO()
        with redirect_stderr(err):
            event_id = aaos.emit_event("TaskCreated", "system", PROJECT, refs={"task": "t-9"}, root=self.root)
        self.assertNotIn("drain_event_spool failed", err.getvalue())
        self.assertIn(event_id, self._ndjson_ids())
        self.assertEqual([p for p in self.spool.iterdir() if p.is_file()], [], "event left in pending/")


class PendingFileWithUnhashableId(_SeamBase):
    """Same wedge on the pending side: a pending file {"event_id": [1]} passed the truthy event_id check,
    then raised TypeError at `not in existing_ids`, aborting every drain on every run. It is complete,
    parseable JSON of the wrong shape, so it is quarantined like a pending object with no event_id."""

    def _write_pending(self, name, obj):
        self.spool.mkdir(parents=True, exist_ok=True)
        path = self.spool / name
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def test_drain_quarantines_unhashable_pending_id_and_folds_the_rest(self):
        self._seed([])
        bad = dict(_event(3), event_id=[1])
        bad_path = self._write_pending("0000000000000000001-1-bad.json", bad)
        good_path = aaos.spool_event(_event(2), PROJECT, root=self.root)
        n, err = self._drain()
        self.assertEqual(n, 1)
        self.assertIn(_event(2)["event_id"], self._ndjson_ids())
        self.assertFalse(good_path.exists())
        self.assertFalse(bad_path.exists(), "unhashable-id pending file still wedging pending/")
        corrupt = aaos._pending_corrupt_dir(PROJECT, root=self.root)
        self.assertEqual(sorted(p.name for p in corrupt.iterdir()), [bad_path.name])

    def test_read_events_survives_unhashable_pending_id(self):
        self._seed([])
        self._write_pending("0000000000000000001-1-bad.json", dict(_event(3), event_id={"x": 1}))
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        err = io.StringIO()
        with redirect_stderr(err):
            ids = [e.get("event_id") for e in aaos.read_events(PROJECT, root=self.root)]
        self.assertEqual(ids.count(_event(1)["event_id"]), 1)
        self.assertEqual(ids.count(_event(2)["event_id"]), 1)


class ReadEventsToleratesUnusableLines(_SeamBase):
    def test_read_events_survives_non_object_and_unhashable_id_lines(self):
        self._seed(NON_OBJECT_LINES + UNHASHABLE_ID_LINES)
        aaos.spool_event(_event(2), PROJECT, root=self.root)
        events = aaos.read_events(PROJECT, root=self.root)  # at HEAD: TypeError on {"event_id": []}
        ids = [e.get("event_id") for e in events]
        self.assertEqual(ids.count(_event(1)["event_id"]), 1)
        self.assertEqual(ids.count(_event(2)["event_id"]), 1)
        # Non-object lines are never returned as events.
        self.assertTrue(all(isinstance(e, dict) for e in events))


if __name__ == "__main__":
    unittest.main()
