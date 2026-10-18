"""tests/test_drain_lock_interleaving.py -- A0b characterization tests (plan section 11.2, item 3).

Audit findings: M01 (stale drain-lock takeover archives by PATH, so two stealers can both succeed and
lose committed events), M02 (expire-leases reclaim neither re-matches task.lease to the lock it judged
nor archives conditionally, so it can revert a live lease).

Each defect gets TWO kinds of test, per plan section 11.2:

  * an INVARIANT test, @known_defect on HEAD (tests/_known_defect.py: only a failed `invariant()` counts
    as 'defect present'; a failed precondition, an AttributeError or a changed signature is a real error). It asserts what the code must guarantee, and
    reaches the race through a deterministic hook at the judge-to-act boundary (a wrapper around the
    judging function, never a thread race, never polling). The fix PR (A1b for M01, A6c for M02) must
    flip it to a plain pass and remove the marker. An unexpected success fails the run.
  * a CHARACTERIZATION test of the current mechanism, passing on HEAD. A1b must rewrite or delete the
    os.replace one when it replaces the path-based archive.

Injection is path-filtered and one-shot: it fires only for the drain-lock path, only once, uses the
REAL functions for the rival's own steps (captured before patching), and is restored by mock's context
manager. Every test asserts that its hook actually fired, so none can pass vacuously.
All runs use a temp tree; DEFAULT_SEAM_ROOT is patched to a sentinel that must stay uncreated.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# `python -m unittest tests.test_drain_lock_interleaving` must work from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401  (puts tools/ on sys.path)
import _aaos_common as aaos
from _known_defect import invariant, known_defect
from test_queue_scripts import NOW, PROJECT, ScriptTestBase, _expired_lease, _iso, _live_lease

_REAL_REPLACE = os.replace
_REAL_PUBLISH = aaos._exclusive_publish


def _lock_bytes(pid: int, acquired: datetime) -> bytes:
    return json.dumps({"holder_pid": pid, "acquired": acquired.isoformat()}).encode("utf-8")


class DrainLockStealInterleavingTests(unittest.TestCase):
    RIVAL_PID = 222

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-drain-il-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = str(self.tmp)
        self.sentinel = self.tmp / "LIVE-DEFAULT-SENTINEL"
        p = mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(self.sentinel))
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.sentinel)})  # seam_root() reads this first
        e.start()
        self.addCleanup(e.stop)
        self.addCleanup(lambda: self.assertFalse(self.sentinel.exists(), "live default seam was resolved"))

        self.lock = aaos._drain_lock_path(PROJECT, root=self.root)
        self.archive = aaos._events_archive_dir(PROJECT, root=self.root)
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        # X: a genuinely stale lock left by a crashed drainer (2h old vs the stale threshold).
        self.lock.write_bytes(_lock_bytes(111, NOW - timedelta(hours=2)))

    def _rival_steals(self):
        """Drainer D, running to completion between B's staleness judgement and B's act: archive the
        stale lock X and publish its own LIVE lock Y at the canonical path. Real functions only."""
        _REAL_REPLACE(str(self.lock), str(self.archive / "rival-archived-X"))
        _REAL_PUBLISH(self.lock, _lock_bytes(self.RIVAL_PID, NOW))

    def _rival_lock_pid(self):
        return json.loads(self.lock.read_text(encoding="utf-8")).get("holder_pid") if self.lock.exists() else None

    # ---- M01 invariant ------------------------------------------------------------------------
    @known_defect("M01: two stealers can both hold the drain lock")
    def test_two_stealers_never_both_hold_the_drain_lock(self):
        """KNOWN DEFECT M01. B judges X stale; rival D then steals X and publishes live lock Y; B must
        notice and LOSE (return None, leave Y at the canonical path). Today B's path-based os.replace
        moves Y into the archive and B acquires too, so two drainers fold from different snapshots.
        A1b must flip this to a plain pass."""
        real_read = aaos._read_drain_lock_acquired
        fired = []

        def judge_then_rival_acts(p):
            result = real_read(p)  # B's judgement of X: stale
            if not fired and Path(p) == self.lock:
                fired.append(1)
                self._rival_steals()
            return result

        with mock.patch.object(aaos, "_read_drain_lock_acquired", judge_then_rival_acts):
            got = aaos._try_acquire_drain_lock(PROJECT, root=self.root, now=NOW)
        self.assertEqual(fired, [1], "hook never fired: test no longer reaches the judge-to-act boundary")
        invariant(got is None, "B acquired the drain lock while D's live lock Y existed (two holders)")
        invariant(self._rival_lock_pid() == self.RIVAL_PID, "D's live lock Y was removed from the canonical path")

    # ---- M01 characterization of the current os.replace window (A1b must rewrite or delete) -----
    def test_current_window_bs_path_based_replace_archives_the_rivals_live_lock(self):
        fired = []

        def replace_with_rival_first(src, dst, *a, **kw):
            is_steal = (not fired and Path(src) == self.lock and Path(dst).parent == self.archive
                        and not Path(dst).name.startswith("rival-"))
            if is_steal:
                fired.append(1)
                self._rival_steals()  # D acts just before B's replace executes
            return _REAL_REPLACE(src, dst, *a, **kw)

        with mock.patch.object(aaos.os, "replace", replace_with_rival_first):
            got = aaos._try_acquire_drain_lock(PROJECT, root=self.root, now=NOW)
        self.assertEqual(fired, [1], "hook never fired")
        self.assertEqual(got, self.lock, "characterization: B acquires despite D's live lock")
        self.assertEqual(self._rival_lock_pid(), os.getpid(), "B's lock replaced D's at the canonical path")
        archived = [json.loads(p.read_text(encoding="utf-8")).get("holder_pid") for p in self.archive.iterdir()]
        self.assertIn(self.RIVAL_PID, archived, "D's LIVE lock Y ended up in the archive (the M01 window)")

    # ---- M01 release: unconditional unlink ----------------------------------------------------
    @known_defect("M01: release unlinks by path and deletes a successor's lock")
    def test_release_never_deletes_a_successors_lock(self):
        """KNOWN DEFECT M01 (release half). B holds the drain lock but stalls past the stale
        threshold; D steals and publishes Y; B's finally-release unlinks by PATH and deletes Y.
        A1b's content-verified release must make this pass. A1b may change the release signature; it
        must update this call and flip the marker."""
        self.lock.unlink()
        got = aaos._try_acquire_drain_lock(PROJECT, root=self.root, now=NOW)
        self.assertEqual(got, self.lock)
        self._rival_steals_over_held_lock()
        aaos._release_drain_lock(got)
        invariant(self.lock.exists(), "B's release deleted the successor's live lock")
        invariant(self._rival_lock_pid() == self.RIVAL_PID, "the successor's lock content changed")

    def _rival_steals_over_held_lock(self):
        _REAL_REPLACE(str(self.lock), str(self.archive / "rival-archived-B"))
        _REAL_PUBLISH(self.lock, _lock_bytes(self.RIVAL_PID, NOW))


class SweepReclaimInterleavingTests(ScriptTestBase):
    """M02. expire-leases judges reclaimability from the LOCK it read, then loads the task and checks
    only that the task is still leased/in-progress. It never re-matches task.lease to the judged lock."""

    @known_defect("M02: sweep requeues a lease renewed after it was judged expired")
    def test_sweep_does_not_revert_a_lease_renewed_after_it_was_judged_expired(self):
        """KNOWN DEFECT M02. Between the sweep's read of the expired lock and its load_task, a new
        holder legitimately acquires the task (fresh lock + fresh task.lease). The sweep must notice
        the lease it judged is no longer the one on the task and skip. Today it requeues the NEW
        holder's live lease and archives the new lock. A6c must make this pass."""
        task_id = "t-sweep-race"
        expired = _expired_lease(holder="cowork", session="old-sess", now=NOW)
        self.seed_task(task_id, state="leased", lease=expired)
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**expired, "task_ref": task_id, "protocol_version": "1.0"})

        fresh = _live_lease(holder="claude-code", session="new-sess", now=NOW)
        real_load_task = aaos.load_task
        fired = []

        def load_after_renewal(tid, project, **kw):
            if not fired and tid == task_id:
                fired.append(1)
                self.seed_task(task_id, state="leased", lease=fresh)  # the new holder's committed lease
                aaos.save_json(lock_file, {**fresh, "task_ref": task_id, "protocol_version": "1.0"})
            return real_load_task(tid, project, **kw)

        with mock.patch.object(aaos, "load_task", load_after_renewal):
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(fired, [1], "hook never fired: sweep no longer loads the task after judging the lock")
        self.assertEqual(code, 0, out)
        task = self.reload_task(task_id)
        invariant(task["state"] == "leased", "sweep requeued a live lease it never judged")
        invariant(task.get("lease", {}).get("session") == "new-sess", "the new holder's lease was dropped")
        invariant(lock_file.exists(), "sweep archived the new holder's live lock")
        invariant(self.count_events("LeaseExpired") == 0, "a LeaseExpired event was emitted for a live lease")


if __name__ == "__main__":
    unittest.main()
