"""tests/test_iso_z_portability.py -- a trailing 'Z' (UTC) must parse identically on every supported Python.

Cowork stamps its events in UTC ('2026-07-10T17:46:23.580832Z'). Its sandbox runs Python 3.10, and
datetime.fromisoformat() only learned to accept the 'Z' suffix in 3.11. On 3.10 that made
_parse_iso_dt raise, parse_cli_now reject a valid --now, and (worst) read_events SILENTLY mis-order
Cowork's events, because an unparseable timestamp sorts last and no error is raised. Found by the
L0 Cowork probe plus a 3.10 measurement (hardened plan, section 14).

These tests are interpreter-independent assertions: they pass on 3.11/3.13 before the fix and only
fail on 3.10, which is why CI also runs 3.10.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

PROJECT = "ora-ops"


class ParseZ(unittest.TestCase):
    def test_trailing_Z_is_utc_with_and_without_fractional_seconds(self):
        for text, expected in (
            ("2026-07-10T17:46:23.580832Z", datetime(2026, 7, 10, 17, 46, 23, 580832, tzinfo=timezone.utc)),
            ("2026-07-10T17:46:23Z", datetime(2026, 7, 10, 17, 46, 23, tzinfo=timezone.utc)),
        ):
            with self.subTest(text=text):
                got = aaos._parse_iso_dt(text)
                self.assertEqual(got, expected)
                self.assertEqual(got.utcoffset(), timedelta(0))

    def test_numeric_offsets_still_work(self):
        self.assertEqual(aaos._parse_iso_dt("2026-07-10T11:58:06.951866-04:00").utcoffset(), timedelta(hours=-4))
        self.assertEqual(aaos._parse_iso_dt("2026-07-10T17:46:23+00:00").utcoffset(), timedelta(0))

    def test_naive_and_malformed_values_are_still_rejected(self):
        for bad in ("2026-07-10T17:46:23", "2026-07-10", "Z", "", "2026-07-10T17:46:23+00:00Z",
                    "2026-07-10T17:46:23ZZ", "not-a-date", "2026-07-10Z",
                    "2026-07-10T17:46:23z"):  # lowercase z: rejected by 3.11+, so rejected everywhere
            with self.subTest(bad=bad):
                with self.assertRaises(aaos.CorruptStateError):
                    aaos._parse_iso_dt(bad)

    def test_error_message_names_the_original_value(self):
        with self.assertRaises(aaos.CorruptStateError) as ctx:
            aaos._parse_iso_dt("2026-07-10T17:46:23+00:00Z")
        self.assertIn("2026-07-10T17:46:23+00:00Z", str(ctx.exception))

    def test_parse_cli_now_accepts_Z(self):
        self.assertEqual(aaos.parse_cli_now("2026-07-10T17:46:23Z").utcoffset(), timedelta(0))


class ReadEventsOrdering(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-z-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = str(self.tmp)
        self.spool = aaos.event_spool_dir(PROJECT, root=self.root)
        self.spool.mkdir(parents=True)

    def _event(self, event_id, ts):
        return {"protocol_version": "1.0", "event_id": event_id, "type": "TaskCreated", "actor": "cowork",
                "timestamp": ts, "project_id": PROJECT, "refs": {"task": "t-x"}}

    def test_a_Z_stamped_event_is_ordered_by_the_clock_not_sorted_last(self):
        """09:00Z is EARLIER than 11:58-04:00 (= 15:58Z). Before the fix, on Python 3.10, the Z event
        was 'unparseable' and sorted last, silently reversing the two."""
        (self.spool / "a.json").write_text(json.dumps(self._event("e-first-by-clock-1", "2026-07-10T09:00:00Z")), encoding="utf-8")
        (self.spool / "b.json").write_text(json.dumps(self._event("e-second-by-clock-2", "2026-07-10T11:58:06-04:00")), encoding="utf-8")
        got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]
        self.assertEqual(got, ["e-first-by-clock-1", "e-second-by-clock-2"])


if __name__ == "__main__":
    unittest.main()
