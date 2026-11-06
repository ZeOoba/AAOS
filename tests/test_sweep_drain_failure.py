"""tests/test_sweep_drain_failure.py -- expire-leases keeps sweeping when the pre-sweep drain fails
(A1d-lite-2, audit finding #2).

Before this fix, expire-leases.py called aaos.drain_event_spool unguarded before the sweep. Any drain
exception (an unusable NDJSON line, a non-UTF-8 byte, a Windows PermissionError on os.replace) fell
through to main()'s catch-all and exited 1 with nothing reclaimed -- the only scheduled reclaimer
aborted on a best-effort path. emit_event already treats its own drain as best-effort
(_aaos_common.emit_event), and every per-lock emit in the sweep relies on that.

Contract tested here: a drain failure prints a WARNING and the sweep proceeds, reclaiming exactly as
it would have, then exits 6 ('swept, drain failed') instead of 0; aaos.UsageError (invalid --project) is NOT swallowed and still exits 2 with no WARNING;
--dry-run still never calls the drain.

Patching note: module.aaos is the shared _aaos_common module, so patching drain_event_spool also
redirects emit_event's internal drain. The raise-once cases fail only the pre-sweep call and delegate
later calls to the real drain, so the outcome can be compared with the unpatched baseline exactly.
The always-raise case leaves per-lock events in pending/, so it counts events through read_events
and asserts the pre-sweep WARNING line specifically, not a single WARNING. Temp seams only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_queue_scripts import (  # noqa: E402
    NOW, PROJECT, ScriptTestBase, _expired_lease, _iso, aaos, load_tool,
)

PRE_SWEEP_WARNING = "WARNING: pre-sweep drain_event_spool failed; sweeping anyway (events stay pending)"
# Distinct nonzero exit for "sweep completed, pre-sweep drain failed" (PHASE3-DESIGN 0.4): the reclaim
# still happens, but the scheduler and automation-health-monitor see a nonzero code, not a silent 0.
EXIT_SWEPT_DRAIN_FAILED = 6


def _undecodable():
    return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


class _SweepDrainBase(ScriptTestBase):
    TASK = "t-sweep-drain"

    def _seed_expired(self):
        lease = _expired_lease(holder="cowork", session="old-sess", now=NOW)
        self.seed_task(self.TASK, state="leased", lease=lease)
        lock_file = aaos.lock_path(self.TASK, PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": self.TASK, "protocol_version": "1.0"})
        return lock_file

    def _patch_drain(self, module, *, exc_factory, times):
        """Make module.aaos.drain_event_spool raise exc_factory() for the first `times` calls
        (None: every call), delegating to the real drain afterwards. Restored by addCleanup."""
        real = module.aaos.drain_event_spool
        state = {"calls": 0}

        def _drain(*args, **kwargs):
            state["calls"] += 1
            if times is None or state["calls"] <= times:
                raise exc_factory()
            return real(*args, **kwargs)

        module.aaos.drain_event_spool = _drain
        self.addCleanup(setattr, module.aaos, "drain_event_spool", real)
        return state

    def _all_event_types(self):
        return [e["type"] for e in aaos.read_events(PROJECT, root=str(self.root))]

    def _assert_reclaimed(self, lock_file):
        task = self.reload_task(self.TASK)
        self.assertEqual(task["state"], "queued")
        self.assertNotIn("lease", task)
        self.assertFalse(lock_file.exists())
        archive_dir = aaos.lock_archive_dir(PROJECT, root=str(self.root))
        self.assertEqual(len(list(archive_dir.glob(f"{self.TASK}.*.lock"))), 1)


class SweepSurvivesPreSweepDrainFailure(_SweepDrainBase):
    def _run_raise_once(self, exc_factory):
        lock_file = self._seed_expired()
        module = load_tool("expire-leases.py")
        state = self._patch_drain(module, exc_factory=exc_factory, times=1)
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertGreaterEqual(state["calls"], 1, "the pre-sweep drain was never called")
        self.assertEqual(code, EXIT_SWEPT_DRAIN_FAILED, out)  # pre-fix: 1, 'UNEXPECTED ERROR', nothing reclaimed
        self.assertIn(PRE_SWEEP_WARNING, out)
        self._assert_reclaimed(lock_file)
        # Identical to the unpatched baseline: one LeaseExpired, folded into the NDJSON.
        self.assertEqual(self.count_events("LeaseExpired"), 1)
        self.assertEqual(self._all_event_types().count("LeaseExpired"), 1)
        return out

    def test_runtime_error_in_pre_sweep_drain(self):
        out = self._run_raise_once(lambda: RuntimeError("simulated drain failure"))
        self.assertIn("RuntimeError: simulated drain failure", out)

    def test_unicode_decode_error_in_pre_sweep_drain(self):
        out = self._run_raise_once(_undecodable)
        self.assertIn("UnicodeDecodeError", out)

    def test_every_drain_failing_still_reclaims_and_events_stay_pending(self):
        lock_file = self._seed_expired()
        module = load_tool("expire-leases.py")
        self._patch_drain(module, exc_factory=lambda: RuntimeError("drain down"), times=None)
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, EXIT_SWEPT_DRAIN_FAILED, out)
        self.assertIn(PRE_SWEEP_WARNING, out)
        self._assert_reclaimed(lock_file)
        # The per-lock emit's own drain also failed (shared module), so the event is committed to
        # pending/ but not folded: count through read_events, never raw NDJSON.
        self.assertEqual(self._all_event_types().count("LeaseExpired"), 1)
        self.assertEqual(self.count_events("LeaseExpired"), 0)


class SweepSurvivesRealDrainFailure(_SweepDrainBase):
    """REFUTE (c): real failures through the real drain lock and its finally path -- drain_event_spool
    is NOT monkeypatched. The per-lock emit_event drains fail the same way, so the sweep's own events
    stay committed in pending/ (counted as files: read_events itself cannot read a non-UTF-8 NDJSON,
    which is exactly why the exit code must be nonzero)."""

    def _spool_one(self):
        aaos.spool_event({
            "protocol_version": "1.0", "event_id": "e-2026-07-09T12:00:00-04:00-1", "type": "TaskCreated",
            "actor": "system", "timestamp": "2026-07-09T12:00:00-04:00", "project_id": PROJECT,
            "refs": {"task": "t-other"},
        }, PROJECT, root=str(self.root))

    def _pending_files(self):
        spool = aaos.event_spool_dir(PROJECT, root=str(self.root))
        return sorted(p.name for p in spool.iterdir() if p.is_file() and p.name.endswith(".json"))

    def _assert_no_drain_lock(self):
        self.assertFalse(aaos._drain_lock_path(PROJECT, root=str(self.root)).exists(), "drain lock left behind")

    def test_non_utf8_ndjson(self):
        lock_file = self._seed_expired()
        log = aaos.events_log_path(PROJECT, root=str(self.root))
        log.write_bytes(b"\xff\n")
        self._spool_one()
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, EXIT_SWEPT_DRAIN_FAILED, out)
        self.assertIn(PRE_SWEEP_WARNING, out)
        self.assertIn("UnicodeDecodeError", out)
        self._assert_reclaimed(lock_file)
        self._assert_no_drain_lock()
        self.assertEqual(log.read_bytes(), b"\xff\n", "NDJSON must not be rewritten by a failed drain")
        # The pre-existing event plus the sweep's own events (LeaseExpired, and Escalated/DeadAgent since
        # no heartbeat is seeded) are still committed in pending/, not lost.
        spool = aaos.event_spool_dir(PROJECT, root=str(self.root))
        types = sorted(json.loads((spool / n).read_text(encoding="utf-8"))["type"] for n in self._pending_files())
        self.assertEqual(types, ["Escalated", "LeaseExpired", "TaskCreated"])

    def test_os_replace_permission_error_on_the_ndjson(self):
        lock_file = self._seed_expired()
        self._spool_one()
        log = str(aaos.events_log_path(PROJECT, root=str(self.root)))
        real_replace = aaos.os.replace
        calls = {"n": 0}

        def _replace(src, dst, *a, **kw):
            if str(dst) == log and calls["n"] == 0:  # only the pre-sweep drain's NDJSON swap
                calls["n"] += 1
                raise PermissionError(13, "simulated sharing violation")
            return real_replace(src, dst, *a, **kw)

        aaos.os.replace = _replace
        self.addCleanup(setattr, aaos.os, "replace", real_replace)
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(calls["n"], 1, "the simulated PermissionError never fired")
        self.assertEqual(code, EXIT_SWEPT_DRAIN_FAILED, out)
        self.assertIn(PRE_SWEEP_WARNING, out)
        self.assertIn("PermissionError", out)
        self._assert_reclaimed(lock_file)
        self._assert_no_drain_lock()
        # The later emit drain succeeded, so both events are folded and readable.
        types = self._all_event_types()
        self.assertEqual(types.count("LeaseExpired"), 1)
        self.assertEqual(types.count("TaskCreated"), 1)

    def test_clean_drain_still_exits_0(self):
        lock_file = self._seed_expired()
        self._spool_one()
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARNING", out)
        self._assert_reclaimed(lock_file)

    def test_drain_failure_with_no_locks_dir_still_signals(self):
        import shutil
        shutil.rmtree(str(aaos.project_dir(PROJECT, root=str(self.root)) / "locks"))
        aaos.events_log_path(PROJECT, root=str(self.root)).write_bytes(b"\xff\n")
        self._spool_one()
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, EXIT_SWEPT_DRAIN_FAILED, out)
        self.assertIn("nothing to sweep", out)


class SweepDrainGuardBoundaries(_SweepDrainBase):
    def test_dry_run_never_calls_the_drain(self):
        lock_file = self._seed_expired()
        module = load_tool("expire-leases.py")
        state = self._patch_drain(module, exc_factory=lambda: AssertionError("dry-run drained"), times=None)
        code, out = self.run_tool("expire-leases.py", "--dry-run", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)
        self.assertEqual(state["calls"], 0)
        self.assertNotIn("WARNING", out)
        self.assertTrue(lock_file.exists())
        self.assertEqual(self.reload_task(self.TASK)["state"], "leased")

    def test_invalid_project_still_exits_2_without_drain_warning(self):
        code, out = self.run_tool("expire-leases.py", "--project", "../escape", "--now", _iso(NOW))
        self.assertEqual(code, 2, out)
        self.assertIn("USAGE ERROR", out)
        self.assertNotIn("WARNING", out)

    def test_usage_error_raised_by_the_drain_is_not_swallowed(self):
        self._seed_expired()
        module = load_tool("expire-leases.py")
        self._patch_drain(module, exc_factory=lambda: aaos.UsageError("simulated bad project"), times=None)
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 2, out)
        self.assertNotIn(PRE_SWEEP_WARNING, out)
        self.assertEqual(self.reload_task(self.TASK)["state"], "leased", "swept despite a usage error")


if __name__ == "__main__":
    import unittest

    unittest.main()
