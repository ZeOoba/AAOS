"""
Regression tests for the 2026-10-03 lifecycle-hardening pass (branch
fix/lifecycle-hardening-2026-10-03):

  F01  post-handoff.py --blocker: requires --holder/--session, requires the
       task to be leased/in-progress, and asserts the lease BEFORE anything
       (handoff file, HandoffPosted event) is written.
  F02  lease-task.py / acquire_lease: a failure after the lock is created
       must not leave a queued task with a live lock; expire-leases.py
       archives an expired orphan lock whose task is no longer leased.
  F06  task/lock/handoff path builders validate their ids (traversal).
  F07  load_json rejects non-object JSON; CLIs map unexpected exceptions to
       a clean exit 1; a post-commit archive_lock OSError is non-fatal.

Every test runs against a per-test tmpdir seam (ScriptTestBase) -- never
the live seam.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_queue_scripts import (  # noqa: E402  (shared fixtures; no test methods are re-collected)
    NOW,
    PROJECT,
    PostHandoffTestBase,
    ScriptTestBase,
    _expired_lease,
    _iso,
    _live_lease,
    aaos,
    load_tool,
)


def _seed_lock(root, task_id, lease):
    lock_file = aaos.lock_path(task_id, PROJECT, root=str(root))
    aaos.save_json(lock_file, {**lease, "task_ref": task_id, "protocol_version": "1.0"})
    return lock_file


def _archived(root, task_id):
    return list(aaos.lock_archive_dir(PROJECT, root=str(root)).glob(f"{task_id}.*.lock"))


# ---------------------------------------------------------------------------
# F01 post-handoff.py --blocker
# ---------------------------------------------------------------------------

class PostHandoffBlockerHardeningTests(PostHandoffTestBase):
    def _post(self, handoff_id, task_id, *extra):
        artifact = self.make_in_scope_artifact()
        return self.run_tool(
            "post-handoff.py",
            "--id", handoff_id, "--task", task_id,
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "unblock me", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--blocker", "waiting on lender docs",
            *extra,
            "--now", _iso(NOW),
        )

    def test_non_holder_blocker_denied_and_writes_nothing(self):
        self.seed_task("t-f01-nonholder", state="leased", lease=_live_lease(holder="claude-code", session="sess-1"))
        code, out = self._post("h-f01-nonholder", "t-f01-nonholder", "--holder", "cowork", "--session", "other")
        self.assertEqual(code, 4, out)
        self.assertIsNone(aaos.load_handoff("h-f01-nonholder", PROJECT, root=str(self.root)))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.reload_task("t-f01-nonholder")["state"], "leased")

    def test_blocker_without_holder_session_is_usage_error_writes_nothing(self):
        self.seed_task("t-f01-nocreds", state="leased", lease=_live_lease())
        code, out = self._post("h-f01-nocreds", "t-f01-nocreds")
        self.assertEqual(code, 2, out)
        self.assertIsNone(aaos.load_handoff("h-f01-nocreds", PROJECT, root=str(self.root)))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.reload_task("t-f01-nocreds")["state"], "leased")

    def test_blocker_with_holder_only_is_usage_error(self):
        self.seed_task("t-f01-halfcreds", state="leased", lease=_live_lease())
        code, out = self._post("h-f01-halfcreds", "t-f01-halfcreds", "--holder", "claude-code")
        self.assertEqual(code, 2, out)
        self.assertIsNone(aaos.load_handoff("h-f01-halfcreds", PROJECT, root=str(self.root)))

    def test_blocker_on_done_task_refused_writes_nothing(self):
        self.seed_task("t-f01-done", state="done", evidence=[{"label": "p", "ref": "r"}])
        code, out = self._post("h-f01-done", "t-f01-done", "--holder", "claude-code", "--session", "sess-1")
        self.assertEqual(code, 3, out)
        self.assertIsNone(aaos.load_handoff("h-f01-done", PROJECT, root=str(self.root)))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.reload_task("t-f01-done")["state"], "done")

    def test_blocker_on_queued_task_refused(self):
        self.seed_task("t-f01-queued")
        code, out = self._post("h-f01-queued", "t-f01-queued", "--holder", "claude-code", "--session", "sess-1")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.reload_task("t-f01-queued")["state"], "queued")
        self.assertEqual(self.events(), [])

    def test_holder_blocker_still_works_and_rerun_is_idempotent(self):
        self.seed_task("t-f01-holder", state="in-progress", lease=_live_lease())
        args = ("--holder", "claude-code", "--session", "sess-1")
        code, out = self._post("h-f01-holder", "t-f01-holder", *args)
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-f01-holder")
        self.assertEqual(task["state"], "blocked")
        self.assertNotIn("lease", task)
        self.assertEqual(self.event_types(), ["HandoffPosted", "Blocked"])

        # Identical re-post after the task is blocked: no-op, not a refusal.
        code2, out2 = self._post("h-f01-holder", "t-f01-holder", *args)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.event_types(), ["HandoffPosted", "Blocked"])


# ---------------------------------------------------------------------------
# F02 lock left behind after a post-acquire failure
# ---------------------------------------------------------------------------

class LeaseTaskPostAcquireFailureTests(ScriptTestBase):
    def _run_with_patch(self, attr, replacement, *argv):
        module = load_tool("lease-task.py")
        original = getattr(module.aaos, attr)
        setattr(module.aaos, attr, replacement(original))
        try:
            return self.run_tool("lease-task.py", *argv)
        finally:
            setattr(module.aaos, attr, original)

    def _argv(self, task_id):
        return (task_id, "--holder", "claude-code", "--session", "sess-1", "--settle-sec", "0", "--now", _iso(NOW))

    def test_event_commit_failure_archives_the_new_lock(self):
        self.seed_task("t-f02-commit")

        def _fail(_orig):
            def _raise(*a, **kw):
                raise aaos.EventCommitError("simulated spool failure")
            return _raise

        code, out = self._run_with_patch("commit_state_change", _fail, *self._argv("t-f02-commit"))
        self.assertEqual(code, 5, out)
        self.assertEqual(self.reload_task("t-f02-commit")["state"], "queued")
        self.assertFalse(aaos.lock_path("t-f02-commit", PROJECT, root=str(self.root)).exists(),
                         "queued task must not be left with a live lock")
        self.assertEqual(len(_archived(self.root, "t-f02-commit")), 1)

    def test_schema_failure_archives_the_new_lock(self):
        self.seed_task("t-f02-schema")

        def _bad_task_schema(orig):
            def _validate(obj, schema_name, **kw):
                if schema_name == "task":
                    return ["simulated schema error"]
                return orig(obj, schema_name, **kw)
            return _validate

        code, out = self._run_with_patch("validate_obj", _bad_task_schema, *self._argv("t-f02-schema"))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.reload_task("t-f02-schema")["state"], "queued")
        self.assertFalse(aaos.lock_path("t-f02-schema", PROJECT, root=str(self.root)).exists())
        self.assertEqual(len(_archived(self.root, "t-f02-schema")), 1)
        self.assertEqual(self.count_events("TaskLeased"), 0)

    def test_cleanup_never_archives_a_lock_that_is_not_ours(self):
        """If the lock on disk was replaced by someone else's by the time we
        clean up, the cleanup must leave it alone."""
        self.seed_task("t-f02-foreign")
        lock_file = aaos.lock_path("t-f02-foreign", PROJECT, root=str(self.root))
        foreign = {**_live_lease(holder="cowork", session="other"), "task_ref": "t-f02-foreign",
                   "protocol_version": "1.0"}

        def _swap_then_fail(_orig):
            def _raise(*a, **kw):
                aaos.save_json(lock_file, foreign)
                raise aaos.EventCommitError("simulated")
            return _raise

        code, out = self._run_with_patch("commit_state_change", _swap_then_fail, *self._argv("t-f02-foreign"))
        self.assertEqual(code, 5, out)
        self.assertTrue(lock_file.exists())
        self.assertEqual(aaos.load_json(lock_file), foreign)


class AcquireLeasePostCreateFailureTests(ScriptTestBase):
    def test_rescan_failure_archives_own_lock_and_reraises(self):
        self.seed_task("t-f02-rescan")
        original = aaos._scan_for_conflicts

        def _boom(*a, **kw):
            raise OSError("simulated directory read failure")

        aaos._scan_for_conflicts = _boom
        try:
            with self.assertRaises(OSError):
                aaos.acquire_lease("t-f02-rescan", PROJECT, "claude-code", "sess-1",
                                   now=NOW, settle_seconds=0, root=str(self.root))
        finally:
            aaos._scan_for_conflicts = original
        self.assertFalse(aaos.lock_path("t-f02-rescan", PROJECT, root=str(self.root)).exists())
        self.assertEqual(len(_archived(self.root, "t-f02-rescan")), 1)


class ArchiveLockIfUnchangedRaceTests(ScriptTestBase):
    """The residual window inside archive_lock_if_unchanged: the lock is
    replaced between the re-read and the os.replace (simulated by swapping
    content inside archive_lock)."""

    def setUp(self):
        super().setUp()
        self.lock_file = _seed_lock(self.root, "t-f02-race", _expired_lease())
        self.expected = aaos.load_json(self.lock_file)
        self.foreign = {**_live_lease(holder="cowork", session="b"), "task_ref": "t-f02-race",
                        "protocol_version": "1.0"}
        self._orig = aaos.archive_lock

    def tearDown(self):
        aaos.archive_lock = self._orig
        super().tearDown()

    def _swap_inside_archive(self, third=None):
        def _archive(lock_arg, **kw):
            aaos.save_json(self.lock_file, self.foreign)  # B replaces our lock just before the move
            target = self._orig(lock_arg, **kw)
            if third is not None:
                aaos.save_json(self.lock_file, third)     # C publishes into the vacated path
            return target
        aaos.archive_lock = _archive

    def test_moved_foreign_lock_is_restored(self):
        self._swap_inside_archive()
        self.assertIsNone(aaos.archive_lock_if_unchanged(self.lock_file, self.expected, now=NOW))
        self.assertEqual(aaos.load_json(self.lock_file), self.foreign)
        self.assertEqual(_archived(self.root, "t-f02-race"), [])

    def test_unrestorable_race_escalates_and_never_clobbers(self):
        third = {**self.foreign, "holder": "claude-code", "session": "c"}
        self._swap_inside_archive(third=third)
        self.assertIsNone(aaos.archive_lock_if_unchanged(self.lock_file, self.expected, now=NOW))
        self.assertEqual(aaos.load_json(self.lock_file), third, "the newer lock must not be clobbered")
        archived = _archived(self.root, "t-f02-race")
        self.assertEqual(len(archived), 1)
        self.assertEqual(aaos.load_json(archived[0]), self.foreign)
        escalated = [e for e in self.events() if e["type"] == "Escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertTrue(escalated[0]["detail"].startswith("LockRestoreFailed"))


class ResolveNowUsageErrorTests(ScriptTestBase):
    def test_bad_now_is_usage_error_not_traceback(self):
        code, out = self.run_tool("lease-task.py", "t-x", "--holder", "a", "--session", "s", "--now", "garbage")
        self.assertEqual(code, 2, out)
        self.assertIn("USAGE ERROR", out)


class ExpireLeasesOrphanLockTests(ScriptTestBase):
    def test_expired_lock_on_queued_task_is_archived_as_orphan(self):
        self.seed_task("t-f02-orphan")  # queued, no lease
        lock_file = _seed_lock(self.root, "t-f02-orphan", _expired_lease())
        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)
        self.assertIn("ORPHAN", out)
        self.assertFalse(lock_file.exists())
        self.assertEqual(len(_archived(self.root, "t-f02-orphan")), 1)
        self.assertEqual(self.count_events("LeaseExpired"), 0)
        self.assertEqual(self.reload_task("t-f02-orphan")["state"], "queued")

    def test_orphan_lock_is_not_archived_when_replaced_since_read(self):
        """The lock read at the top of the sweep was expired; by the time the
        orphan branch runs, a new live lock (a lease-task mid-acquire) is in
        its place. It must not be archived."""
        self.seed_task("t-f02-replaced")
        lock_file = _seed_lock(self.root, "t-f02-replaced", _expired_lease())
        fresh = {**_live_lease(holder="cowork", session="new"), "task_ref": "t-f02-replaced",
                 "protocol_version": "1.0"}

        module = load_tool("expire-leases.py")
        original_load_task = module.aaos.load_task

        def _load_then_replace(*a, **kw):
            result = original_load_task(*a, **kw)
            aaos.save_json(lock_file, fresh)
            return result

        module.aaos.load_task = _load_then_replace
        try:
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        finally:
            module.aaos.load_task = original_load_task
        self.assertEqual(code, 0, out)
        self.assertTrue(lock_file.exists())
        self.assertEqual(aaos.load_json(lock_file), fresh)


# ---------------------------------------------------------------------------
# F06 id validation in path builders
# ---------------------------------------------------------------------------

class PathBuilderIdValidationTests(unittest.TestCase):
    BAD_TASK_IDS = ("../x", "t-x/../../y", "t-../../evil", "..\\x", "t-X", "x", "", "t-a\n", "t-a.json")

    def test_task_and_lock_paths_reject_bad_ids(self):
        for bad in self.BAD_TASK_IDS:
            with self.subTest(bad=bad):
                with self.assertRaises(aaos.UsageError):
                    aaos.task_path(bad, PROJECT, root="/tmp/seam")
                with self.assertRaises(aaos.UsageError):
                    aaos.lock_path(bad, PROJECT, root="/tmp/seam")

    def test_handoff_path_rejects_bad_ids(self):
        for bad in ("../x", "h-../../evil", "t-a", "h-a/b", "h-a\n", None):
            with self.subTest(bad=bad):
                with self.assertRaises(aaos.UsageError):
                    aaos.handoff_path(bad, PROJECT, root="/tmp/seam")

    def test_valid_ids_still_build(self):
        self.assertEqual(aaos.task_path("t-a-1", PROJECT, root="/s").name, "t-a-1.json")
        self.assertEqual(aaos.lock_path("t-a-1", PROJECT, root="/s").name, "t-a-1.lock")
        self.assertEqual(aaos.handoff_path("h-a-1", PROJECT, root="/s").name, "h-a-1.json")


class CliTraversalTests(ScriptTestBase):
    def test_lease_task_traversal_exits_2_and_writes_nothing(self):
        for bad in ("../x", "t-x/../../y"):
            with self.subTest(bad=bad):
                code, out = self.run_tool(
                    "lease-task.py", bad, "--holder", "claude-code", "--session", "s",
                    "--settle-sec", "0", "--now", _iso(NOW),
                )
                self.assertEqual(code, 2, out)
                self.assertIn("USAGE ERROR", out)
        self.assertEqual(self.events(), [])
        self.assertEqual(list((self.root / "projects" / PROJECT / "locks").glob("*.lock")), [])

    def test_complete_and_fail_task_traversal_exit_2(self):
        code, out = self.run_tool(
            "complete-task.py", "../x", "--holder", "a", "--session", "s",
            "--evidence", "label=p", "ref=r", "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)
        code, out = self.run_tool(
            "fail-task.py", "../x", "--holder", "a", "--session", "s", "--reason", "r", "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)


# ---------------------------------------------------------------------------
# F07 non-object JSON, catch-all exit mapping, post-commit archive OSError
# ---------------------------------------------------------------------------

class LoadJsonNonObjectTests(ScriptTestBase):
    def test_load_json_rejects_non_object(self):
        for payload in ("[]", "42", '"s"', "null"):
            with self.subTest(payload=payload):
                p = self.root / "x.json"
                p.write_text(payload, encoding="utf-8")
                with self.assertRaises(aaos.CorruptStateError):
                    aaos.load_json(p)

    def test_lifecycle_clis_exit_1_on_non_object_task_without_traceback(self):
        tasks_dir = self.root / "projects" / PROJECT / "tasks"
        (tasks_dir / "t-f07-list.json").write_text("[]", encoding="utf-8")
        cases = (
            ("lease-task.py", "t-f07-list", "--holder", "a", "--session", "s", "--settle-sec", "0"),
            ("complete-task.py", "t-f07-list", "--holder", "a", "--session", "s", "--evidence", "label=p", "ref=r"),
            ("fail-task.py", "t-f07-list", "--holder", "a", "--session", "s", "--reason", "r"),
        )
        for tool, *argv in cases:
            with self.subTest(tool=tool):
                code, out = self.run_tool(tool, *argv, "--now", _iso(NOW))
                self.assertEqual(code, 1, out)
                self.assertIn("VALIDATION FAILED", out)
                self.assertNotIn("Traceback", out)


class CatchAllExitMappingTests(PostHandoffTestBase):
    def _with_boom(self, tool, attr, *argv):
        module = load_tool(tool)
        original = getattr(module.aaos, attr)

        def _boom(*a, **kw):
            raise RuntimeError("simulated unexpected failure")

        setattr(module.aaos, attr, _boom)
        try:
            return self.run_tool(tool, *argv)
        finally:
            setattr(module.aaos, attr, original)

    def test_unexpected_exceptions_exit_1_cleanly(self):
        self.seed_task("t-f07-boom", state="leased", lease=_live_lease())
        artifact = self.make_in_scope_artifact()
        cases = (
            ("create-task.py", "require_seam_reachable", "--title", "x", "--accept", "y", "--id", "t-f07-new"),
            ("lease-task.py", "load_task", "t-f07-boom", "--holder", "a", "--session", "s"),
            ("complete-task.py", "load_task", "t-f07-boom", "--holder", "a", "--session", "s",
             "--evidence", "label=p", "ref=r"),
            ("fail-task.py", "load_task", "t-f07-boom", "--holder", "a", "--session", "s", "--reason", "r"),
            ("post-handoff.py", "validate_obj", "--id", "h-f07-boom", "--task", "t-f07-boom",
             "--from", "claude-code", "--to", "cowork", "--next-action", "n", "--summary", "s",
             "--artifact", f"path={artifact}"),
            ("emit-event.py", "emit_event", "--type", "TaskCreated", "--actor", "claude-code", "--task", "t-x"),
        )
        for tool, attr, *argv in cases:
            with self.subTest(tool=tool):
                code, out = self._with_boom(tool, attr, *argv, "--now", _iso(NOW))
                self.assertEqual(code, 1, out)
                self.assertIn("UNEXPECTED ERROR", out)


class PostCommitArchiveFailureTests(PostHandoffTestBase):
    """The terminal transition has committed; a lock that vanished before
    archive_lock ran (concurrent sweep) must not turn success into a crash."""

    def _with_vanishing_lock(self, tool, *argv):
        module = load_tool(tool)
        original = module.aaos.archive_lock

        def _delete_then_archive(lock_arg, **kw):
            try:
                Path(lock_arg).unlink()
            except OSError:
                pass
            return original(lock_arg, **kw)

        module.aaos.archive_lock = _delete_then_archive
        try:
            return self.run_tool(tool, *argv)
        finally:
            module.aaos.archive_lock = original

    def _seed_leased_with_lock(self, task_id):
        lease = _live_lease()
        self.seed_task(task_id, state="leased", lease=lease)
        _seed_lock(self.root, task_id, lease)

    def test_complete_task(self):
        self._seed_leased_with_lock("t-f07-complete")
        code, out = self._with_vanishing_lock(
            "complete-task.py", "t-f07-complete", "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=p", "ref=r", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.reload_task("t-f07-complete")["state"], "done")
        self.assertEqual(self.count_events("Completed"), 1)

    def test_fail_task(self):
        self._seed_leased_with_lock("t-f07-fail")
        code, out = self._with_vanishing_lock(
            "fail-task.py", "t-f07-fail", "--holder", "claude-code", "--session", "sess-1",
            "--reason", "r", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.reload_task("t-f07-fail")["state"], "failed")
        self.assertEqual(self.count_events("Failed"), 1)

    def test_post_handoff_blocker(self):
        self._seed_leased_with_lock("t-f07-block")
        artifact = self.make_in_scope_artifact()
        code, out = self._with_vanishing_lock(
            "post-handoff.py", "--id", "h-f07-block", "--task", "t-f07-block",
            "--from", "claude-code", "--to", "cowork", "--next-action", "n", "--summary", "s",
            "--artifact", f"path={artifact}", "--blocker", "b",
            "--holder", "claude-code", "--session", "sess-1", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.reload_task("t-f07-block")["state"], "blocked")
        self.assertEqual(self.event_types(), ["HandoffPosted", "Blocked"])


if __name__ == "__main__":
    unittest.main()
