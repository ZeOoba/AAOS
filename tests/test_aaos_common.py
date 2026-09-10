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

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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

    def test_incumbent_lock_mid_create_denies_instead_of_raising(self):
        """A rival observed MID-CREATE is contended, not corrupt (OI-264).

        _exclusive_create() creates the lock with O_EXCL and writes its JSON as a
        separate syscall, so the loser of the create race can read a zero-byte file.
        Before this fix that surfaced as CorruptStateError escaping acquire_lease
        entirely -- so the loser produced NO outcome rather than DENIED, and a
        routine contended lease presented as damaged state. Deterministic here:
        the transient unparseable read is injected rather than raced for.
        """
        task_id = "t-midcreate"
        aaos.save_task(_make_task(task_id), root=str(self.root))

        first = aaos.acquire_lease(
            task_id, PROJECT, "claude-code", "sess-a",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(first.outcome, aaos.LeaseOutcome.ACQUIRED)

        real_load_lock = aaos.load_lock
        calls = {"n": 0}

        def flaky_load_lock(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise aaos.CorruptStateError("simulated zero-byte lock mid-create")
            return real_load_lock(*args, **kwargs)

        aaos.load_lock = flaky_load_lock
        try:
            second = aaos.acquire_lease(
                task_id, PROJECT, "cowork", "sess-b",
                now=NOW, settle_seconds=0, root=str(self.root),
            )
        finally:
            aaos.load_lock = real_load_lock

        self.assertEqual(second.outcome, aaos.LeaseOutcome.DENIED, second)
        self.assertGreaterEqual(
            calls["n"], 2, "the unparseable incumbent read was never retried"
        )

    def test_lock_is_never_observable_as_partially_written(self):
        """The create must be ATOMIC, not merely retried around (OI-264).

        This is the test for the actual fix. _exclusive_link() publishes the lock by
        hardlinking an already-fully-written sidecar, so the file appears complete or not
        at all. The previous implementation created with O_EXCL and wrote the JSON as a
        separate syscall, leaving it observable as zero bytes in between -- which
        _scan_for_conflicts() and expire-leases.py also read, and misread.

        Asserted structurally rather than by racing: a watcher thread hammers the lock
        path throughout the acquire and records every size it ever sees. Zero is a
        failure. Racing for it would make this test probabilistic, and a probabilistic
        guard on a concurrency primitive is how OI-264 survived in the first place.
        """
        task_id = "t-atomic-create"
        aaos.save_task(_make_task(task_id), root=str(self.root))
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        seen_sizes = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                try:
                    seen_sizes.append(lock_file.stat().st_size)
                except OSError:
                    pass  # not created yet, or already gone

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        try:
            result = aaos.acquire_lease(
                task_id, PROJECT, "claude-code", "sess-a",
                now=NOW, settle_seconds=0, root=str(self.root),
            )
        finally:
            stop.set()
            t.join(timeout=5)

        self.assertEqual(result.outcome, aaos.LeaseOutcome.ACQUIRED)
        self.assertTrue(seen_sizes, "watcher never observed the lock file at all")
        self.assertNotIn(
            0, seen_sizes,
            "lock file was observable as zero bytes -- the create is not atomic",
        )

    def test_unparseable_incumbent_read_is_actually_retried_with_a_wait(self):
        """The retry must WAIT, not busy-loop (OI-264 review finding).

        The first version of these tests asserted only the retry COUNT, so they passed
        with _LOCK_SETTLE_DELAY_SEC = 0 -- an implementation that fixes nothing. Duration
        is the only axis the safety net trades on, so it is the axis to assert.
        """
        task_id = "t-retry-waits"
        aaos.save_task(_make_task(task_id), root=str(self.root))
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"")  # durably unparseable -> exhausts the whole budget

        started = time.monotonic()
        with self.assertRaises(aaos.CorruptStateError):
            aaos.acquire_lease(
                task_id, PROJECT, "cowork", "sess-b",
                now=NOW, settle_seconds=0, root=str(self.root),
            )
        elapsed = time.monotonic() - started

        # ABSOLUTE floor, deliberately not derived from _LOCK_SETTLE_DELAY_SEC. Deriving
        # the expectation from the constant under test is what made the first version of
        # this assertion vacuous: with the constant mutated to 0 it asserted `elapsed >= 0`
        # and passed against an implementation that waits not at all. If someone lowers the
        # budget below this floor, this test SHOULD fail and force the conversation.
        MIN_REAL_WAIT_SEC = 0.04
        self.assertGreaterEqual(
            elapsed, MIN_REAL_WAIT_SEC,
            f"exhausting the retry budget took {elapsed:.4f}s, under the {MIN_REAL_WAIT_SEC}s "
            f"floor -- the retry is not actually waiting, so it fixes nothing "
            f"(_LOCK_SETTLE_ATTEMPTS={aaos._LOCK_SETTLE_ATTEMPTS}, "
            f"_LOCK_SETTLE_DELAY_SEC={aaos._LOCK_SETTLE_DELAY_SEC}).",
        )

    def test_incumbent_lock_permanently_unparseable_still_raises(self):
        """Retrying must NOT swallow real corruption (OI-264).

        The retry window exists for a lock that is briefly empty because a rival is
        mid-create. A lock that is STILL unparseable after that window is genuinely
        damaged, and must keep raising -- CorruptStateError is the signal
        docs/OPERATIONS.md's runbook acts on.
        """
        task_id = "t-corrupt-lock"
        aaos.save_task(_make_task(task_id), root=str(self.root))
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_bytes(b"")  # never becomes valid

        with self.assertRaises(aaos.CorruptStateError):
            aaos.acquire_lease(
                task_id, PROJECT, "cowork", "sess-b",
                now=NOW, settle_seconds=0, root=str(self.root),
            )


class AcquireLeaseContestedTests(BaseAaosTest):
    def test_injected_sleep_conflict_copy_yields_contested(self):
        task_id = "t-contested"
        aaos.save_task(_make_task(task_id), root=str(self.root))
        locks_dir = aaos.lock_path(task_id, PROJECT, root=str(self.root)).parent

        def fake_sleep(seconds):
            # A real OneDrive conflict-copy is a literal content duplicate
            # of the original lock file, so it carries the same task_ref
            # (Finding 2, adversarial review: _scan_for_conflicts now reads
            # task_ref to distinguish a genuine conflict-copy of THIS task
            # from an unrelated sibling task's own lock -- see
            # AcquireLeaseSiblingTaskIdConflictShapeTests).
            conflict = locks_dir / f"{task_id}-FOREIGNHOST.lock"
            conflict.write_text(
                json.dumps({"task_ref": task_id, "holder": "cowork", "session": "other"}),
                encoding="utf-8",
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


class AcquireLeaseSiblingTaskIdConflictShapeTests(BaseAaosTest):
    """Finding 2 (adversarial review): _matches_conflict_shape used a bare
    filename.startswith(task_id) check, so leasing "t-a" while an unrelated
    sibling task "t-a-b" holds ITS OWN live lock ("t-a-b.lock") was misread
    as a OneDrive conflict-copy of "t-a" (e.g. the real shape
    "t-a-<HOST>.lock") and falsely returned CONTESTED
    (reviewer-demonstrated: conflict_paths: ['t-a-b.lock']). Common with
    --suffix-timestamp ids where prefix-sibling task ids are routine."""

    def test_sibling_task_id_lock_does_not_falsely_contest(self):
        aaos.save_task(_make_task("t-a"), root=str(self.root))
        aaos.save_task(_make_task("t-a-b"), root=str(self.root))

        # t-a-b genuinely, legitimately holds its OWN live lock.
        sibling_result = aaos.acquire_lease(
            "t-a-b", PROJECT, "cowork", "sess-sibling",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(sibling_result.outcome, aaos.LeaseOutcome.ACQUIRED)

        # Leasing "t-a" must not be falsely CONTESTED by "t-a-b.lock".
        result = aaos.acquire_lease(
            "t-a", PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(
            result.outcome, aaos.LeaseOutcome.ACQUIRED,
            f"falsely CONTESTED by sibling task id's own lock: {result.conflict_paths!r}",
        )

    def test_true_conflict_copy_of_same_task_id_still_contests(self):
        aaos.save_task(_make_task("t-a"), root=str(self.root))
        locks_dir = aaos.lock_path("t-a", PROJECT, root=str(self.root)).parent
        locks_dir.mkdir(parents=True, exist_ok=True)
        # A genuine OneDrive conflict-copy of t-a's OWN lock (same
        # task_ref, foreign holder) must still be CONTESTED.
        aaos.save_json(locks_dir / "t-a-DESKTOP01.lock", {
            "protocol_version": "1.0", "task_ref": "t-a", "holder": "cowork",
            "session": "other-sess", "acquired": NOW.isoformat(),
            "expires": (NOW + timedelta(minutes=45)).isoformat(),
        })

        result = aaos.acquire_lease(
            "t-a", PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(result.outcome, aaos.LeaseOutcome.CONTESTED)
        self.assertTrue(any("DESKTOP01" in p for p in result.conflict_paths))

    def test_unparseable_conflict_shaped_file_still_fails_closed(self):
        aaos.save_task(_make_task("t-a"), root=str(self.root))
        locks_dir = aaos.lock_path("t-a", PROJECT, root=str(self.root)).parent
        locks_dir.mkdir(parents=True, exist_ok=True)
        (locks_dir / "t-a-DESKTOP01.lock").write_text("{not valid json", encoding="utf-8")

        result = aaos.acquire_lease(
            "t-a", PROJECT, "claude-code", "sess-1",
            now=NOW, settle_seconds=0, root=str(self.root),
        )
        self.assertEqual(result.outcome, aaos.LeaseOutcome.CONTESTED)


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


class LoadHeartbeatTests(BaseAaosTest):
    """Finding 6 (adversarial review): a corrupt heartbeat.json used to
    return None identically to an absent one, with zero signal. The
    fail-safe None-means-unknown return contract for CALLERS is unchanged
    (that's still correct); what changed is that the corrupt case now also
    prints a loud, visible warning distinguishing it from "never derived
    yet"."""

    def test_absent_heartbeat_returns_none_without_warning(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = aaos.load_heartbeat(root=str(self.root))
        self.assertIsNone(result)
        self.assertEqual(stderr.getvalue(), "")

    def test_corrupt_heartbeat_returns_none_with_loud_warning(self):
        hb_path = aaos.heartbeat_path(root=str(self.root))
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text("{not valid json", encoding="utf-8")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = aaos.load_heartbeat(root=str(self.root))

        self.assertIsNone(result)
        warning = stderr.getvalue()
        self.assertIn("WARNING", warning)
        self.assertIn("heartbeat.json", warning)
        self.assertIn(str(hb_path), warning)


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
    """REVISED (post-review, PHASE3-DESIGN.md section 1.10): the pending
    (spool) file write is now emit_event's commit point -- see
    tests/test_event_log_concurrency.py for the Finding-1 concurrency
    regression test that drove this redesign. These tests pin down the
    two halves of the new contract: (1) a drain failure after a successful
    commit must not lose the event, and (2) a commit (pending-write)
    failure is the only thing that raises EventCommitError."""

    def test_drain_failure_leaves_event_committed_pending_then_later_drain_folds_in_oldest_first(self):
        def _always_fail(*args, **kwargs):
            raise RuntimeError("simulated persistent drain-lock contention")

        original_drain = aaos.drain_event_spool
        aaos.drain_event_spool = _always_fail
        try:
            event_id_1 = aaos.emit_event(
                "TaskCreated", "claude-code", PROJECT,
                refs={"task": "t-a"}, now=NOW, root=str(self.root),
            )
        finally:
            aaos.drain_event_spool = original_drain

        # The event is durably committed to pending even though its own
        # drain attempt failed -- this is the load-bearing assertion of
        # the redesign: commit (pending write) succeeded, so nothing was
        # lost, even though it hasn't been folded into the NDJSON yet.
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        spooled_files = list(spool_dir.glob("*.json"))
        self.assertEqual(len(spooled_files), 1)

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        if log_path.exists():
            self.assertNotIn(event_id_1, log_path.read_text(encoding="utf-8"))

        # A later, successful emit_event drains BOTH the still-pending
        # event_id_1 and its own event_id_2, oldest-first.
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

    def test_pending_write_failure_raises_event_commit_error_nothing_written(self):
        def _boom(*args, **kwargs):
            raise OSError("simulated disk full")

        original_spool = aaos.spool_event
        aaos.spool_event = _boom
        try:
            with self.assertRaises(aaos.EventCommitError):
                aaos.emit_event(
                    "TaskCreated", "claude-code", PROJECT,
                    refs={"task": "t-x"}, now=NOW, root=str(self.root),
                )
        finally:
            aaos.spool_event = original_spool

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        self.assertFalse(log_path.exists())
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        remaining = list(spool_dir.glob("*.json")) if spool_dir.is_dir() else []
        self.assertEqual(remaining, [])


class DrainLockTests(BaseAaosTest):
    """Finding 1, point 2-4: the drain lock is skip-not-block on a live
    holder, and a stale (>5min or unparseable) lock is archived and
    reclaimed rather than wedging draining forever."""

    def test_held_drain_lock_causes_drain_to_skip_not_block(self):
        aaos.spool_event(
            {
                "protocol_version": "1.0", "event_id": "e-x-1", "type": "TaskCreated",
                "actor": "claude-code", "timestamp": NOW.isoformat(),
                "project_id": PROJECT, "refs": {"task": "t-x"},
            },
            PROJECT, root=str(self.root),
        )
        lock_path = aaos._drain_lock_path(PROJECT, root=str(self.root))
        aaos._exclusive_publish(
            lock_path,
            json.dumps({"holder_pid": 999999, "acquired": NOW.isoformat()}).encode("utf-8"),
        )

        drained = aaos.drain_event_spool(PROJECT, root=str(self.root), now=NOW)
        self.assertEqual(drained, 0)

        # Event is still safely pending; nothing was folded, no exception.
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        self.assertEqual(len(list(spool_dir.glob("*.json"))), 1)
        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        self.assertFalse(log_path.exists())

    def test_stale_drain_lock_is_archived_and_reclaimed(self):
        aaos.spool_event(
            {
                "protocol_version": "1.0", "event_id": "e-x-1", "type": "TaskCreated",
                "actor": "claude-code", "timestamp": NOW.isoformat(),
                "project_id": PROJECT, "refs": {"task": "t-x"},
            },
            PROJECT, root=str(self.root),
        )
        lock_path = aaos._drain_lock_path(PROJECT, root=str(self.root))
        stale_acquired = NOW - timedelta(minutes=10)
        aaos._exclusive_publish(
            lock_path,
            json.dumps({"holder_pid": 999999, "acquired": stale_acquired.isoformat()}).encode("utf-8"),
        )

        drained = aaos.drain_event_spool(PROJECT, root=str(self.root), now=NOW)
        self.assertEqual(drained, 1)

        archive_dir = aaos._events_archive_dir(PROJECT, root=str(self.root))
        archived = list(archive_dir.glob("drainlock.*"))
        self.assertEqual(len(archived), 1, "stale lock must be archived, not deleted")

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        self.assertIn("e-x-1", log_path.read_text(encoding="utf-8"))


class CorruptPendingFileTests(BaseAaosTest):
    """Finding 7: drain must not discard a malformed pending file by
    unlinking it (never-delete doctrine) -- move it to _corrupt/ instead,
    with a loud warning, and keep draining everything else."""

    def test_malformed_pending_file_moved_to_corrupt_not_deleted(self):
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        os.makedirs(spool_dir, exist_ok=True)
        bad = spool_dir / "00000000000000000-1-badbad.json"
        bad.write_text("{not valid json", encoding="utf-8")

        aaos.spool_event(
            {
                "protocol_version": "1.0", "event_id": "e-good-1", "type": "TaskCreated",
                "actor": "claude-code", "timestamp": NOW.isoformat(),
                "project_id": PROJECT, "refs": {"task": "t-good"},
            },
            PROJECT, root=str(self.root),
        )

        drained = aaos.drain_event_spool(PROJECT, root=str(self.root), now=NOW)
        self.assertEqual(drained, 1)  # only the good one is counted as drained

        self.assertFalse(bad.exists(), "corrupt file must be moved out of pending/, not left in place")
        corrupt_dir = aaos._pending_corrupt_dir(PROJECT, root=str(self.root))
        moved = list(corrupt_dir.glob("*.json"))
        self.assertEqual(len(moved), 1, "corrupt file must be moved, never unlinked")
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "{not valid json")

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        self.assertIn("e-good-1", log_path.read_text(encoding="utf-8"))


class EventIdUniquenessTests(BaseAaosTest):
    """Finding 1b: event_id = f"e-{iso}-{next(_EVENT_SEQ)}" collided across
    processes sharing an injected --now, because _EVENT_SEQ is a fresh
    itertools.count(1) per process. The fix folds in the real wall clock
    (time.time_ns(), independent of any injected `now`), so two "processes"
    -- simulated here by resetting the module-level counter, exactly what
    happens when a second `python tools/emit-event.py` invocation starts --
    still mint distinct ids for the same injected now."""

    def test_event_id_unique_when_sequence_counter_resets_under_same_injected_now(self):
        import itertools as _itertools

        aaos._EVENT_SEQ = _itertools.count(1)
        id_a = aaos.emit_event(
            "TaskCreated", "claude-code", PROJECT,
            refs={"task": "t-a"}, now=NOW, root=str(self.root),
        )
        aaos._EVENT_SEQ = _itertools.count(1)  # simulate a fresh process's counter
        id_b = aaos.emit_event(
            "TaskCreated", "claude-code", PROJECT,
            refs={"task": "t-b"}, now=NOW, root=str(self.root),
        )
        self.assertNotEqual(id_a, id_b, "event_id collided across a simulated cross-process counter reset")
        for event_id in (id_a, id_b):
            self.assertRegex(event_id, r"^e-[0-9TZ:.+\-]+-[0-9]+$")

    def test_many_ids_under_one_fixed_injected_now_are_unique(self):
        ids = {
            aaos.emit_event(
                "TaskCreated", "claude-code", PROJECT,
                refs={"task": f"t-{i}"}, now=NOW, root=str(self.root),
            )
            for i in range(20)
        }
        self.assertEqual(len(ids), 20)


class ReadEventsTests(BaseAaosTest):
    """Finding 1, point 6: readers must see committed-but-still-pending
    events, not just what has been drained into the NDJSON."""

    def test_includes_ndjson_and_pending_deduped_sorted_by_timestamp(self):
        aaos.emit_event(
            "TaskCreated", "claude-code", PROJECT,
            refs={"task": "t-a"}, now=NOW, root=str(self.root),
        )  # drains immediately -- lands in the NDJSON

        # Hold the drain lock so the next emit_event's own event is
        # committed to pending but can't be folded in yet.
        lock_path = aaos._drain_lock_path(PROJECT, root=str(self.root))
        aaos._exclusive_publish(
            lock_path,
            json.dumps({"holder_pid": 1, "acquired": NOW.isoformat()}, ensure_ascii=False).encode("utf-8"),
        )
        try:
            aaos.emit_event(
                "TaskLeased", "claude-code", PROJECT,
                refs={"task": "t-b"}, now=NOW + timedelta(minutes=1), root=str(self.root),
            )
        finally:
            os.unlink(lock_path)

        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        self.assertEqual(len(list(spool_dir.glob("*.json"))), 1, "second event should still be pending")

        events = aaos.read_events(PROJECT, root=str(self.root))
        self.assertEqual([e["type"] for e in events], ["TaskCreated", "TaskLeased"])

    def test_dedup_by_event_id_when_same_event_present_in_both_ndjson_and_pending(self):
        event = {
            "protocol_version": "1.0", "event_id": "e-dup-1", "type": "TaskCreated",
            "actor": "claude-code", "timestamp": NOW.isoformat(),
            "project_id": PROJECT, "refs": {"task": "t-dup"},
        }
        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        aaos.atomic_write(log_path, json.dumps(event) + "\n")
        aaos.spool_event(event, PROJECT, root=str(self.root))  # same event_id, still (not-yet-drained) pending

        events = aaos.read_events(PROJECT, root=str(self.root))
        self.assertEqual(len(events), 1)

    def test_empty_when_nothing_committed(self):
        self.assertEqual(aaos.read_events(PROJECT, root=str(self.root)), [])


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
