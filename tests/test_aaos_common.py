"""
Tests for tools/_aaos_common.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/_aaos_common.py does not exist yet, so every test in this module is
expected to fail (via ImportError at module load, surfaced by unittest as a
collection/setUp error) until tools/_aaos_common.py is implemented. See
docs/PHASE3-DESIGN.md section 1 for the full spec these tests pin down.

Run with:  python -m unittest tests.test_aaos_common -v   (from the repo root)
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
SCHEMA_DIR = REPO_ROOT / "schemas"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

# Fixed injected "now" throughout -- matches the ADR examples' -04:00 offset
# convention and removes all real-time dependence from the suite.
NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PROJECT = "ora-ops"


def _make_task(task_id, project_id=PROJECT, *, state="queued", lease=None, now=NOW):
    task = {
        "protocol_version": "1.0",
        "id": task_id,
        "project_id": project_id,
        "title": f"Test task {task_id}",
        "owner_agent": "unassigned",
        "state": state,
        "priority": 2,
        "inputs": [],
        "acceptance_criteria": ["it works"],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    if lease is not None:
        task["lease"] = lease
    return task


def _make_handoff(**overrides):
    base = {
        "protocol_version": "1.0",
        "id": "h-x",
        "project_id": PROJECT,
        "task_ref": "t-x",
        "from_agent": "claude-code",
        "to_agent": "cowork",
        "artifact_manifest": [],
        "context_summary": "summary",
        "required_next_action": "do it",
        "blockers": [],
        "posted_at": NOW.isoformat(),
    }
    base.update(overrides)
    return base


class BaseAaosTest(unittest.TestCase):
    """Seeds a temp dir as the seam root, per docs/PHASE3-DESIGN.md section 5:
    'AAOS_SEAM_ROOT -> tmp_path, seed projects/ora-ops/{tasks,handoffs,
    locks/_archive,events}'. Sets the env var too, so both the env-var path
    and the explicit root= kwarg get exercised across the suite."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-test-")
        self.root = Path(self._tmpdir)
        self._old_env = os.environ.get("AAOS_SEAM_ROOT")
        os.environ["AAOS_SEAM_ROOT"] = str(self.root)
        for sub in ("tasks", "handoffs", "locks/_archive", "events"):
            (self.root / "projects" / PROJECT / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AAOS_SEAM_ROOT", None)
        else:
            os.environ["AAOS_SEAM_ROOT"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1.4 atomic_write
# ---------------------------------------------------------------------------

class AtomicWriteTests(BaseAaosTest):
    def test_writes_content_creates_parent_dirs_no_tmp_residue(self):
        target = self.root / "nested" / "dir" / "file.json"
        aaos.atomic_write(target, '{"a": 1}')
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')
        leftovers = list(target.parent.glob(f"{target.name}.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_write_failure_leaves_no_partial_final_file(self):
        target = self.root / "will-fail.json"
        original_fsync = os.fsync

        def _boom(fd):
            raise OSError("simulated fsync failure")

        os.fsync = _boom
        try:
            with self.assertRaises(OSError):
                aaos.atomic_write(target, "content")
        finally:
            os.fsync = original_fsync
        self.assertFalse(target.exists())
        leftovers = list(self.root.glob(f"{target.name}.tmp-*"))
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# 1.3 Path helpers
# ---------------------------------------------------------------------------

class PathHelperTests(BaseAaosTest):
    def test_paths_are_correct(self):
        root = str(self.root)
        base = self.root / "projects" / PROJECT
        self.assertEqual(aaos.task_path("t-a", PROJECT, root=root), base / "tasks" / "t-a.json")
        self.assertEqual(aaos.handoff_path("h-a", PROJECT, root=root), base / "handoffs" / "h-a.json")
        self.assertEqual(aaos.lock_path("t-a", PROJECT, root=root), base / "locks" / "t-a.lock")
        self.assertEqual(aaos.lock_archive_dir(PROJECT, root=root), base / "locks" / "_archive")
        self.assertEqual(
            aaos.events_log_path(PROJECT, root=root), base / "events" / "aaos-events.ndjson"
        )
        self.assertEqual(
            aaos.event_spool_dir(PROJECT, root=root),
            base / "events" / "aaos-events.ndjson.pending",
        )
        self.assertEqual(aaos.heartbeat_path(root=root), self.root / "heartbeat.json")
        self.assertEqual(aaos.registry_path(), aaos.REPO_ROOT / "agent-registry.json")

    def test_malicious_project_id_dotdot_is_rejected(self):
        with self.assertRaises(aaos.UsageError):
            aaos.project_dir("../escape", root=str(self.root))

    def test_malicious_project_id_slash_is_rejected(self):
        with self.assertRaises(aaos.UsageError):
            aaos.project_dir("Foo/Bar", root=str(self.root))

    def test_root_override_takes_precedence_over_env_var(self):
        # setUp already pointed AAOS_SEAM_ROOT at self.root; an explicit
        # root= kwarg must win over it.
        other = self.root / "other-root"
        p = aaos.task_path("t-a", PROJECT, root=str(other))
        self.assertTrue(str(p).startswith(str(other)))

    def test_env_var_used_when_no_override_given(self):
        p = aaos.task_path("t-a", PROJECT)
        self.assertTrue(str(p).startswith(str(self.root)))


# ---------------------------------------------------------------------------
# 1.5 save_handoff write-once semantics
# ---------------------------------------------------------------------------

class SaveHandoffWriteOnceTests(BaseAaosTest):
    def test_second_identical_write_is_noop(self):
        h = _make_handoff()
        aaos.save_handoff(h, root=str(self.root))
        aaos.save_handoff(dict(h), root=str(self.root))
        path = aaos.handoff_path("h-x", PROJECT, root=str(self.root))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["id"], "h-x")

    def test_second_different_write_raises_state_conflict(self):
        h = _make_handoff()
        aaos.save_handoff(h, root=str(self.root))
        h2 = _make_handoff(context_summary="a materially different summary")
        with self.assertRaises(aaos.StateConflictError):
            aaos.save_handoff(h2, root=str(self.root))


# ---------------------------------------------------------------------------
# 1.7 acquire_lease
# ---------------------------------------------------------------------------

class AcquireLeaseHappyPathTests(BaseAaosTest):
    def test_acquired_lock_file_has_correct_holder_and_expires(self):
        aaos.save_task(_make_task("t-happy"), root=str(self.root))
        result = aaos.acquire_lease(
            "t-happy", PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(result.outcome, aaos.LeaseOutcome.ACQUIRED)
        self.assertEqual(result.lock["holder"], "claude-code")
        self.assertEqual(result.lock["session"], "sess-1")
        self.assertEqual(result.expires, NOW + timedelta(minutes=45))

        lock_file = aaos.lock_path("t-happy", PROJECT, root=str(self.root))
        self.assertTrue(lock_file.exists())
        on_disk = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["holder"], "claude-code")
        self.assertEqual(on_disk["session"], "sess-1")
        self.assertEqual(on_disk["expires"], result.expires.isoformat())

        leftovers = list(lock_file.parent.glob(f"{lock_file.name}.tmp-*"))
        self.assertEqual(leftovers, [])


class AcquireLeaseSequentialCollisionTests(BaseAaosTest):
    def test_second_caller_denied_exactly_one_lock_file_remains(self):
        aaos.save_task(_make_task("t-seq"), root=str(self.root))
        r1 = aaos.acquire_lease(
            "t-seq", PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(r1.outcome, aaos.LeaseOutcome.ACQUIRED)

        r2 = aaos.acquire_lease(
            "t-seq", PROJECT, "cowork", "sess-2",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(r2.outcome, aaos.LeaseOutcome.DENIED)

        locks_dir = aaos.lock_path("t-seq", PROJECT, root=str(self.root)).parent
        lock_files = [p for p in locks_dir.glob("t-seq*.lock") if p.is_file()]
        self.assertEqual(len(lock_files), 1)


class AcquireLeaseRaceTests(BaseAaosTest):
    def test_genuine_race_exactly_one_acquired_never_two(self):
        for i in range(10):
            task_id = f"t-race-{i}"
            aaos.save_task(_make_task(task_id), root=str(self.root))
            barrier = threading.Barrier(2)
            outcomes = []
            outcomes_lock = threading.Lock()

            def worker(holder, session):
                barrier.wait()
                r = aaos.acquire_lease(
                    task_id, PROJECT, holder, session,
                    now=NOW, settle_seconds=0, root=str(self.root),
                )
                with outcomes_lock:
                    outcomes.append(r.outcome)

            t1 = threading.Thread(target=worker, args=("claude-code", "sess-a"))
            t2 = threading.Thread(target=worker, args=("cowork", "sess-b"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(
                sorted(o.value for o in outcomes),
                sorted([aaos.LeaseOutcome.ACQUIRED.value, aaos.LeaseOutcome.DENIED.value]),
                f"iteration {i}: outcomes were {outcomes!r}",
            )


class AcquireLeaseContestedTests(BaseAaosTest):
    def test_injected_sleep_conflict_copy_yields_contested(self):
        task_id = "t-contested"
        aaos.save_task(_make_task(task_id), root=str(self.root))
        locks_dir = aaos.lock_path(task_id, PROJECT, root=str(self.root)).parent

        def fake_sleep(seconds):
            conflict = locks_dir / f"{task_id}-FOREIGNHOST.lock"
            conflict.write_text(
                json.dumps({"holder": "cowork", "session": "other"}), encoding="utf-8"
            )

        result = aaos.acquire_lease(
            task_id, PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=1, sleep=fake_sleep, root=str(self.root),
        )
        self.assertEqual(result.outcome, aaos.LeaseOutcome.CONTESTED)
        self.assertTrue(any("FOREIGNHOST" in p for p in result.conflict_paths))

        own_lock = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        self.assertFalse(own_lock.exists())

        archive_dir = aaos.lock_archive_dir(PROJECT, root=str(self.root))
        archived = list(archive_dir.glob(f"{task_id}.*.lock"))
        self.assertEqual(len(archived), 1)

        reloaded = aaos.load_task(task_id, PROJECT, root=str(self.root))
        self.assertEqual(reloaded["state"], "queued")


class AcquireLeaseExpiredReclaimTests(BaseAaosTest):
    def test_expired_incumbent_is_archived_and_new_lease_acquired(self):
        task_id = "t-reclaim"
        prior_lease = {
            "holder": "cowork",
            "session": "old-sess",
            "acquired": (NOW - timedelta(hours=2)).isoformat(),
            "expires": (NOW - timedelta(hours=1)).isoformat(),
        }
        aaos.save_task(_make_task(task_id, state="leased", lease=prior_lease), root=str(self.root))
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        aaos.save_json(
            lock_file,
            {**prior_lease, "task_ref": task_id, "protocol_version": "1.0"},
        )

        result = aaos.acquire_lease(
            task_id, PROJECT, "claude-code", "new-sess",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(result.outcome, aaos.LeaseOutcome.ACQUIRED)
        self.assertIsNotNone(result.reclaimed_prior)
        self.assertEqual(result.reclaimed_prior["holder"], "cowork")

        archive_dir = aaos.lock_archive_dir(PROJECT, root=str(self.root))
        archived = list(archive_dir.glob(f"{task_id}.*.lock"))
        self.assertEqual(len(archived), 1)

        new_lock = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(new_lock["holder"], "claude-code")


# ---------------------------------------------------------------------------
# 1.9 reclaim_decision / archive_lock / corrupt-lock handling
# ---------------------------------------------------------------------------

class ReclaimDecisionTableTests(BaseAaosTest):
    def test_reclaim_decision_matrix(self):
        cases = [
            # (expires_offset_minutes, heartbeat_status, expected_reclaimable, expected_classification)
            (+60, "fresh", False, ""),
            (+60, "stale", False, ""),
            (+60, "missed", False, ""),
            (+60, "unknown", False, ""),
            (+60, None, False, ""),
            (-60, "fresh", True, "ExpiredLapsed"),
            (-60, "stale", True, "ExpiredLapsed"),
            (-60, "missed", True, "DeadAgent"),
            (-60, "unknown", True, "DeadAgent"),
            (-60, None, True, "DeadAgent"),
        ]
        for offset, heartbeat, expected_reclaimable, expected_classification in cases:
            with self.subTest(offset=offset, heartbeat=heartbeat):
                lock = {"expires": (NOW + timedelta(minutes=offset)).isoformat()}
                decision = aaos.reclaim_decision(lock, heartbeat, now=NOW, skew_minutes=5)
                self.assertEqual(decision.reclaimable, expected_reclaimable)
                self.assertEqual(decision.classification, expected_classification)

    def test_reclaim_decision_just_inside_skew_grace_is_not_reclaimable(self):
        # expires 3 minutes ago, skew grace 5 minutes -> still within grace, not reclaimable.
        lock = {"expires": (NOW - timedelta(minutes=3)).isoformat()}
        decision = aaos.reclaim_decision(lock, "missed", now=NOW, skew_minutes=5)
        self.assertFalse(decision.reclaimable)
        self.assertEqual(decision.classification, "")


class CorruptLockTests(BaseAaosTest):
    def test_load_lock_raises_corrupt_state_on_unparseable_json(self):
        lock_file = aaos.lock_path("t-corrupt", PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(aaos.CorruptStateError):
            aaos.load_lock("t-corrupt", PROJECT, root=str(self.root))

    def test_reclaim_decision_missing_expiry_not_reclaimable(self):
        decision = aaos.reclaim_decision({}, "fresh", now=NOW)
        self.assertFalse(decision.reclaimable)
        self.assertEqual(decision.classification, "")

    def test_reclaim_decision_unparseable_expiry_not_reclaimable(self):
        decision = aaos.reclaim_decision({"expires": "not-a-date"}, "fresh", now=NOW)
        self.assertFalse(decision.reclaimable)
        self.assertEqual(decision.classification, "")


# ---------------------------------------------------------------------------
# 1.10 emit_event / spool / drain, and the commit_state_change backstop
# ---------------------------------------------------------------------------

class EmitEventTests(BaseAaosTest):
    def test_emit_event_appends_schema_valid_ndjson_line(self):
        event_id = aaos.emit_event(
            "TaskCreated", "claude-code", PROJECT,
            refs={"task": "t-x"}, now=NOW, root=str(self.root),
        )
        self.assertRegex(event_id, r"^e-[0-9TZ:.+\-]+-[0-9]+$")

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event_id"], event_id)

        schema = json.loads((SCHEMA_DIR / "event.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(record))
        self.assertEqual(errors, [], errors)

    def test_commit_state_change_state_unchanged_on_event_failure(self):
        state_path = self.root / "projects" / PROJECT / "tasks" / "t-x.json"
        aaos.atomic_write(state_path, json.dumps({"marker": "original"}))

        def _boom(**kwargs):
            raise aaos.EventCommitError("simulated event-commit failure")

        original_emit = aaos.emit_event
        aaos.emit_event = _boom
        try:
            with self.assertRaises(aaos.EventCommitError):
                aaos.commit_state_change(
                    {"marker": "new"}, state_path,
                    {"type": "Completed", "actor": "claude-code", "project_id": PROJECT},
                    root=str(self.root),
                )
        finally:
            aaos.emit_event = original_emit

        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), {"marker": "original"})
        leftovers = list(state_path.parent.glob(f"{state_path.name}.tmp-*"))
        self.assertEqual(leftovers, [])


class EventSpoolTests(BaseAaosTest):
    def test_append_failure_twice_spools_then_later_drain_folds_in_oldest_first(self):
        def _always_fail(*args, **kwargs):
            raise RuntimeError("simulated persistent append lock")

        original_append = aaos._atomic_append_ndjson
        aaos._atomic_append_ndjson = _always_fail
        try:
            event_id_1 = aaos.emit_event(
                "TaskCreated", "claude-code", PROJECT,
                refs={"task": "t-a"}, now=NOW, root=str(self.root),
            )
        finally:
            aaos._atomic_append_ndjson = original_append

        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        spooled_files = list(spool_dir.glob("*.json"))
        self.assertEqual(len(spooled_files), 1)

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        if log_path.exists():
            self.assertNotIn(event_id_1, log_path.read_text(encoding="utf-8"))

        event_id_2 = aaos.emit_event(
            "TaskLeased", "claude-code", PROJECT,
            refs={"task": "t-a"}, now=NOW + timedelta(seconds=1), root=str(self.root),
        )

        lines = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        ids = [e["event_id"] for e in lines]
        self.assertEqual(ids, [event_id_1, event_id_2])
        self.assertEqual(list(spool_dir.glob("*.json")), [])


# ---------------------------------------------------------------------------
# 1.8 assert_lease_held
# ---------------------------------------------------------------------------

class AssertLeaseHeldTests(BaseAaosTest):
    def _task_with_lease(self, holder="claude-code", session="sess-1", expires_offset_min=30):
        return {
            "id": "t-x",
            "project_id": PROJECT,
            "lease": {
                "holder": holder,
                "session": session,
                "acquired": NOW.isoformat(),
                "expires": (NOW + timedelta(minutes=expires_offset_min)).isoformat(),
            },
        }

    def test_passes_for_live_holder(self):
        task = self._task_with_lease()
        aaos.assert_lease_held(task, "claude-code", "sess-1", now=NOW)  # must not raise

    def test_wrong_holder_raises_lease_lost(self):
        task = self._task_with_lease()
        with self.assertRaises(aaos.LeaseLostError):
            aaos.assert_lease_held(task, "cowork", "sess-1", now=NOW)

    def test_wrong_session_raises_lease_lost(self):
        task = self._task_with_lease()
        with self.assertRaises(aaos.LeaseLostError):
            aaos.assert_lease_held(task, "claude-code", "sess-WRONG", now=NOW)

    def test_expired_lease_raises_lease_lost(self):
        task = self._task_with_lease(expires_offset_min=-30)
        with self.assertRaises(aaos.LeaseLostError):
            aaos.assert_lease_held(task, "claude-code", "sess-1", now=NOW, skew_minutes=5)


if __name__ == "__main__":
    unittest.main()
