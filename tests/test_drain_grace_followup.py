"""tests/test_drain_grace_followup.py -- follow-ups from the independent refute pass on PR #21 (head 9a34860).

The refute pass found no safety defect; these cover its wording and visibility findings:
  * the grace-window deferral message must only claim "may still be syncing" for a file whose age is
    KNOWN to be young: a file that vanished before the age check, or whose age cannot be determined,
    gets its own truthful message (and an unknown age is still never quarantined on a guess);
  * read_events must not silently skip an undecodable / unparseable / non-object pending file: it warns,
    naming the file (AMD-0004 section B.3), while still returning every other event;
  * the docs must state the grace window's real limit: it keys on the file's modification time, which
    OneDrive preserves from the source machine, so a file synced late from another device looks old and
    is quarantined at once (recovered by moving it back to pending/).
Temp trees only.
"""
from __future__ import annotations

import io
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
from test_drain_pending_errors import _event, _old  # noqa: E402

PROJECT = "ora-ops"
REPO_ROOT = Path(__file__).resolve().parent.parent


class _SpoolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-grace-fu-"))
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

    def _quarantined(self):
        return sorted(p.name for p in self.corrupt.iterdir()) if self.corrupt.exists() else []


class GraceMessages(_SpoolCase):
    def _old_bad_file(self):
        bad = self.spool / "0000-bad.json"
        bad.write_text("{not valid", encoding="utf-8")
        _old(bad)  # old enough to quarantine if its age were known
        return bad

    def test_a_young_bad_file_says_it_may_still_be_syncing(self):
        young = self.spool / "0000-young.json"
        young.write_text("{not valid", encoding="utf-8")
        _, err = self._drain()
        self.assertIn("may still be syncing", err)
        self.assertTrue(young.exists())

    def test_a_file_that_vanishes_before_the_age_check_is_not_called_syncing(self):
        bad = self._old_bad_file()
        with mock.patch.object(aaos, "_pending_age", side_effect=FileNotFoundError(2, "gone")) as age:
            n, err = self._drain()
        self.assertTrue(age.called, "the age check was never reached: the test no longer exercises it")
        self.assertEqual(n, 0)
        self.assertIn("vanished", err)
        self.assertNotIn("may still be syncing", err)
        self.assertEqual(self._quarantined(), [])
        self.assertTrue(bad.exists())  # only the age check was simulated; nothing moved it

    def test_an_unknown_age_is_reported_as_unknown_and_never_quarantined_on_a_guess(self):
        bad = self._old_bad_file()
        with mock.patch.object(aaos, "_pending_age", side_effect=PermissionError(13, "cannot stat")) as age:
            n, err = self._drain()
        self.assertTrue(age.called)
        self.assertEqual(n, 0)
        self.assertIn("age cannot be determined", err)
        self.assertNotIn("may still be syncing", err)
        self.assertEqual(self._quarantined(), [])
        self.assertTrue(bad.exists())

    def test_an_old_bad_file_is_still_quarantined(self):
        bad = self._old_bad_file()
        n, err = self._drain()
        self.assertEqual(self._quarantined(), ["0000-bad.json"])
        self.assertFalse(bad.exists())
        self.assertIn("moved to", err)


class ReadEventsWarnsOnSkippedFiles(_SpoolCase):
    def _read(self):
        err = io.StringIO()
        with redirect_stderr(err):
            got = [e["event_id"] for e in aaos.read_events(PROJECT, root=self.root)]
        return got, err.getvalue()

    def test_an_undecodable_pending_file_is_named_in_a_warning_and_good_events_still_return(self):
        (self.spool / "0000-bad.json").write_bytes(b"\xff\xfe\x00 not utf-8")
        aaos.spool_event(_event(3), PROJECT, root=self.root)
        got, err = self._read()
        self.assertEqual(got, [_event(3)["event_id"]])
        self.assertIn("0000-bad.json", err)

    def test_a_non_json_and_a_non_object_pending_file_are_each_named(self):
        (self.spool / "0001-not-json.json").write_text("{not valid json", encoding="utf-8")
        (self.spool / "0002-not-object.json").write_text("[1, 2, 3]", encoding="utf-8")
        aaos.spool_event(_event(4), PROJECT, root=self.root)
        got, err = self._read()
        self.assertEqual(got, [_event(4)["event_id"]])
        self.assertIn("0001-not-json.json", err)
        self.assertIn("0002-not-object.json", err)

    def test_clean_reads_print_nothing(self):
        aaos.spool_event(_event(5), PROJECT, root=self.root)
        got, err = self._read()
        self.assertEqual(got, [_event(5)["event_id"]])
        self.assertEqual(err, "")


class DocsStateTheLimit(unittest.TestCase):
    PHRASE = "keeps its original modification time"

    def test_migrations_and_operations_state_the_grace_window_limit(self):
        for rel in ("docs/MIGRATIONS.md", "docs/OPERATIONS.md"):
            with self.subTest(doc=rel):
                self.assertIn(self.PHRASE, (REPO_ROOT / rel).read_text(encoding="utf-8"))

    def test_the_drain_diagram_shows_the_deferral(self):
        text = (REPO_ROOT / "docs" / "DIAGRAMS.md").read_text(encoding="utf-8")
        self.assertIn("grace window", text)


if __name__ == "__main__":
    unittest.main()
