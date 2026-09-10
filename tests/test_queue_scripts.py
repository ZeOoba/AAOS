"""
Tests for the seven AAOS Phase-3 CLI scripts: tools/create-task.py,
lease-task.py, complete-task.py, fail-task.py, post-handoff.py,
expire-leases.py, emit-event.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
none of tools/{create-task,lease-task,complete-task,fail-task,post-handoff,
expire-leases,emit-event}.py exist yet, so every test here is expected to
fail (via a loader error on _tool_loader.load_tool -- the target file does
not exist) until the corresponding tool is implemented. See
docs/PHASE3-DESIGN.md section 2 for the full per-script spec these tests
pin down, and section 5.2 for the minimum coverage bar.

Each tool is driven in-process via its `main(argv)` entry point (loaded
through tests/_tool_loader.py, since hyphenated filenames aren't
`import`able directly) -- mirroring the tests/test_validate.py pattern.
The subprocess-driven end-to-end lifecycle lives in its own file,
tests/test_e2e_lifecycle.py, per the design's explicit split.

Registry note: post-handoff write-scope tests use the REAL repo-root
agent-registry.json (tools/_aaos_common.load_registry()'s only source --
there is no --registry override on post-handoff.py's CLI, per its
literal design spec). claude-code's write_scope covers "C:/Users/Owner/
AAOS/**", so tests that need a handoff to pass write-scope validation
place their artifact file in a temp directory created *inside* the repo
tree (tempfile.mkdtemp(dir=REPO_ROOT)), cleaned up in tearDown. Tests that
need a write-scope *violation* instead place the artifact under the
per-test OS-tmp seam root, which is outside every registered agent's
write_scope.

Run with:  python -m unittest tests.test_queue_scripts -v   (from the repo root)
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

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tool_loader import load_tool  # noqa: E402

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PROJECT = "ora-ops"


def _iso(dt):
    return dt.isoformat()


def _make_task(task_id, project_id=PROJECT, *, state="queued", lease=None, now=NOW, **overrides):
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
    task.update(overrides)
    return task


def _live_lease(holder="claude-code", session="sess-1", now=NOW, ttl_min=45):
    return {
        "holder": holder,
        "session": session,
        "acquired": now.isoformat(),
        "expires": (now + timedelta(minutes=ttl_min)).isoformat(),
    }


def _expired_lease(holder="claude-code", session="sess-1", now=NOW):
    return {
        "holder": holder,
        "session": session,
        "acquired": (now - timedelta(hours=2)).isoformat(),
        "expires": (now - timedelta(hours=1)).isoformat(),
    }


class ScriptTestBase(unittest.TestCase):
    """Seeds a temp dir as the seam root (docs/PHASE3-DESIGN.md section 5)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-qtest-")
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

    def run_tool(self, filename, *args):
        module = load_tool(filename)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = module.main(list(args))
        return code, buf_out.getvalue() + buf_err.getvalue()

    def events(self):
        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        if not log_path.exists():
            return []
        return [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def event_types(self):
        return [e["type"] for e in self.events()]

    def count_events(self, event_type):
        return sum(1 for e in self.events() if e["type"] == event_type)

    def seed_task(self, task_id, **kwargs):
        task = _make_task(task_id, **kwargs)
        aaos.save_task(task, root=str(self.root))
        return task

    def reload_task(self, task_id):
        return aaos.load_task(task_id, PROJECT, root=str(self.root))


# ---------------------------------------------------------------------------
# 2.1 create-task.py
# ---------------------------------------------------------------------------

class CreateTaskHappyPathTests(ScriptTestBase):
    def test_slug_normalized_and_task_written_queued(self):
        code, out = self.run_tool(
            "create-task.py",
            "--title", "Do the thing", "--slug", "foo",
            "--accept", "it works", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-foo")
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["title"], "Do the thing")
        self.assertEqual(task["owner_agent"], "unassigned")
        self.assertEqual(task["priority"], 2)
        self.assertEqual(task["acceptance_criteria"], ["it works"])
        self.assertEqual(self.count_events("TaskCreated"), 1)

    def test_explicit_id_owner_priority_and_multiple_inputs(self):
        code, out = self.run_tool(
            "create-task.py",
            "--title", "Explicit id task", "--id", "t-explicit",
            "--accept", "a", "--accept", "b",
            "--owner", "claude-code", "--priority", "1",
            "--input", "label=Foo", "ref=bar", "kind=file",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-explicit")
        self.assertEqual(task["owner_agent"], "claude-code")
        self.assertEqual(task["priority"], 1)
        self.assertEqual(task["acceptance_criteria"], ["a", "b"])
        self.assertEqual(task["inputs"], [{"label": "Foo", "ref": "bar", "kind": "file"}])

    def test_suffix_timestamp_appends_utc_compact_timestamp(self):
        code, out = self.run_tool(
            "create-task.py",
            "--title", "Timestamped", "--slug", "sweep",
            "--accept", "x", "--suffix-timestamp", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        expected_suffix = NOW.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
        expected_id = f"t-sweep-{expected_suffix}"
        task = self.reload_task(expected_id)
        self.assertEqual(task["id"], expected_id)


class CreateTaskUsageErrorTests(ScriptTestBase):
    def test_missing_accept_exits_2(self):
        code, out = self.run_tool(
            "create-task.py",
            "--title", "No acceptance criteria", "--slug", "bad",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)
        self.assertFalse((self.root / "projects" / PROJECT / "tasks" / "t-bad.json").exists())

    def test_invalid_slug_pattern_exits_2(self):
        code, out = self.run_tool(
            "create-task.py",
            "--title", "Bad slug", "--slug", "Foo Bar!",
            "--accept", "x", "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)


class CreateTaskIdempotencyTests(ScriptTestBase):
    def test_identical_repost_is_noop_exit_0_no_reemit(self):
        args = (
            "--title", "Idempotent task", "--id", "t-idem",
            "--accept", "x", "--now", _iso(NOW),
        )
        code1, out1 = self.run_tool("create-task.py", *args)
        code2, out2 = self.run_tool("create-task.py", *args)
        self.assertEqual(code1, 0, out1)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.count_events("TaskCreated"), 1)

    def test_same_id_different_content_exits_3(self):
        self.run_tool(
            "create-task.py",
            "--title", "Original title", "--id", "t-conflict",
            "--accept", "x", "--now", _iso(NOW),
        )
        code, out = self.run_tool(
            "create-task.py",
            "--title", "Different title", "--id", "t-conflict",
            "--accept", "x", "--now", _iso(NOW),
        )
        self.assertEqual(code, 3, out)
        task = self.reload_task("t-conflict")
        self.assertEqual(task["title"], "Original title")
        self.assertEqual(self.count_events("TaskCreated"), 1)


# ---------------------------------------------------------------------------
# 2.2 lease-task.py
# ---------------------------------------------------------------------------

class LeaseTaskHappyPathTests(ScriptTestBase):
    def test_acquired_transitions_task_and_emits_task_leased(self):
        self.seed_task("t-lease-happy")
        code, out = self.run_tool(
            "lease-task.py", "t-lease-happy",
            "--holder", "claude-code", "--session", "sess-1",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-lease-happy")
        self.assertEqual(task["state"], "leased")
        self.assertEqual(task["lease"]["holder"], "claude-code")
        self.assertEqual(task["lease"]["session"], "sess-1")
        self.assertEqual(self.count_events("TaskLeased"), 1)

    def test_reclaimed_prior_emits_lease_expired_then_task_leased(self):
        self.seed_task("t-lease-reclaim", state="leased", lease=_expired_lease(holder="cowork", session="old"))
        lock_file = aaos.lock_path("t-lease-reclaim", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**_expired_lease(holder="cowork", session="old"),
                                    "task_ref": "t-lease-reclaim", "protocol_version": "1.0"})
        code, out = self.run_tool(
            "lease-task.py", "t-lease-reclaim",
            "--holder", "claude-code", "--session", "new-sess",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.event_types(), ["LeaseExpired", "TaskLeased"])
        task = self.reload_task("t-lease-reclaim")
        self.assertEqual(task["lease"]["holder"], "claude-code")


class LeaseTaskDeniedTests(ScriptTestBase):
    def test_live_incumbent_denies_exit_3(self):
        self.seed_task("t-lease-denied", state="leased", lease=_live_lease(holder="cowork", session="s1"))
        code, out = self.run_tool(
            "lease-task.py", "t-lease-denied",
            "--holder", "claude-code", "--session", "sess-2",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 3, out)
        self.assertEqual(self.count_events("LeaseDenied"), 1)
        task = self.reload_task("t-lease-denied")
        self.assertEqual(task["lease"]["holder"], "cowork")


class LeaseTaskContestedTests(ScriptTestBase):
    def test_conflict_copy_during_settle_window_contests_exit_4(self):
        self.seed_task("t-lease-contested")
        locks_dir = aaos.lock_path("t-lease-contested", PROJECT, root=str(self.root)).parent
        result = {}

        def _run():
            code, out = self.run_tool(
                "lease-task.py", "t-lease-contested",
                "--holder", "claude-code", "--session", "sess-1",
                "--settle-sec", "1", "--now", _iso(NOW),
            )
            result["code"] = code
            result["out"] = out

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.3)
        locks_dir.mkdir(parents=True, exist_ok=True)
        # A real OneDrive conflict-copy is a literal content duplicate of
        # the original lock file, so it carries the same task_ref
        # (Finding 2, adversarial review: _scan_for_conflicts now reads
        # task_ref to distinguish a genuine conflict-copy of THIS task
        # from an unrelated sibling task's own lock).
        (locks_dir / "t-lease-contested-FOREIGNHOST.lock").write_text(
            json.dumps({"task_ref": "t-lease-contested", "holder": "cowork", "session": "other"}),
            encoding="utf-8",
        )
        t.join(timeout=15)

        self.assertEqual(result.get("code"), 4, result.get("out"))
        self.assertIn("LeaseContested", self.event_types())
        task = self.reload_task("t-lease-contested")
        self.assertEqual(task["state"], "queued")


class LeaseTaskIdempotencyTests(ScriptTestBase):
    def test_rerun_same_holder_session_is_noop(self):
        self.seed_task("t-lease-idem", state="leased", lease=_live_lease(holder="claude-code", session="sess-1"))
        code, out = self.run_tool(
            "lease-task.py", "t-lease-idem",
            "--holder", "claude-code", "--session", "sess-1",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.count_events("TaskLeased"), 0)


# ---------------------------------------------------------------------------
# 2.3 complete-task.py
# ---------------------------------------------------------------------------

class CompleteTaskHappyPathTests(ScriptTestBase):
    def test_completes_drops_lease_records_evidence(self):
        self.seed_task("t-complete", state="leased", lease=_live_lease())
        code, out = self.run_tool(
            "complete-task.py", "t-complete",
            "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=proof", "ref=out.txt",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-complete")
        self.assertEqual(task["state"], "done")
        self.assertNotIn("lease", task)
        self.assertEqual(task["evidence"], [{"label": "proof", "ref": "out.txt"}])
        self.assertEqual(self.count_events("Completed"), 1)

    def test_archive_flag_performs_second_transition(self):
        self.seed_task("t-complete-archive", state="leased", lease=_live_lease())
        code, out = self.run_tool(
            "complete-task.py", "t-complete-archive",
            "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=proof", "ref=out.txt",
            "--archive", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-complete-archive")
        self.assertEqual(task["state"], "archived")
        self.assertEqual(self.event_types(), ["Completed", "Archived"])


class CompleteTaskLeaseLostTests(ScriptTestBase):
    def test_wrong_holder_exits_4_task_unchanged(self):
        self.seed_task("t-complete-lost", state="leased", lease=_live_lease(holder="claude-code", session="sess-1"))
        code, out = self.run_tool(
            "complete-task.py", "t-complete-lost",
            "--holder", "cowork", "--session", "sess-2",
            "--evidence", "label=proof", "ref=out.txt",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 4, out)
        task = self.reload_task("t-complete-lost")
        self.assertEqual(task["state"], "leased")
        self.assertEqual(self.count_events("Completed"), 0)


class CompleteTaskMissingEvidenceTests(ScriptTestBase):
    def test_no_evidence_exits_2_task_unchanged(self):
        self.seed_task("t-complete-noev", state="leased", lease=_live_lease())
        code, out = self.run_tool(
            "complete-task.py", "t-complete-noev",
            "--holder", "claude-code", "--session", "sess-1",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)
        task = self.reload_task("t-complete-noev")
        self.assertEqual(task["state"], "leased")
        self.assertEqual(self.count_events("Completed"), 0)


class CompleteTaskIdempotencyTests(ScriptTestBase):
    def test_already_done_reruns_noop_archive_still_applies(self):
        self.seed_task("t-complete-idem", state="leased", lease=_live_lease())
        args = (
            "t-complete-idem", "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=proof", "ref=out.txt", "--now", _iso(NOW),
        )
        code1, out1 = self.run_tool("complete-task.py", *args)
        self.assertEqual(code1, 0, out1)

        code2, out2 = self.run_tool("complete-task.py", *args)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.count_events("Completed"), 1)

        code3, out3 = self.run_tool(
            "complete-task.py", "t-complete-idem",
            "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=proof", "ref=out.txt",
            "--archive", "--now", _iso(NOW),
        )
        self.assertEqual(code3, 0, out3)
        self.assertEqual(self.count_events("Archived"), 1)

        code4, out4 = self.run_tool(
            "complete-task.py", "t-complete-idem",
            "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=proof", "ref=out.txt",
            "--archive", "--now", _iso(NOW),
        )
        self.assertEqual(code4, 0, out4)
        self.assertEqual(self.count_events("Archived"), 1)


# ---------------------------------------------------------------------------
# 2.4 fail-task.py
# ---------------------------------------------------------------------------

class FailTaskHappyPathTests(ScriptTestBase):
    def test_fails_drops_lease(self):
        self.seed_task("t-fail", state="leased", lease=_live_lease())
        code, out = self.run_tool(
            "fail-task.py", "t-fail",
            "--holder", "claude-code", "--session", "sess-1",
            "--reason", "boom", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-fail")
        self.assertEqual(task["state"], "failed")
        self.assertNotIn("lease", task)
        self.assertEqual(self.count_events("Failed"), 1)
        self.assertEqual(self.count_events("Escalated"), 0)


class FailTaskLeaseLostTests(ScriptTestBase):
    def test_wrong_holder_exits_4(self):
        self.seed_task("t-fail-lost", state="leased", lease=_live_lease(holder="claude-code", session="sess-1"))
        code, out = self.run_tool(
            "fail-task.py", "t-fail-lost",
            "--holder", "cowork", "--session", "sess-2",
            "--reason", "boom", "--now", _iso(NOW),
        )
        self.assertEqual(code, 4, out)
        task = self.reload_task("t-fail-lost")
        self.assertEqual(task["state"], "leased")


class FailTaskIdempotencyTests(ScriptTestBase):
    def test_already_failed_is_noop(self):
        self.seed_task("t-fail-idem", state="leased", lease=_live_lease())
        args = (
            "t-fail-idem", "--holder", "claude-code", "--session", "sess-1",
            "--reason", "boom", "--now", _iso(NOW),
        )
        code1, out1 = self.run_tool("fail-task.py", *args)
        self.assertEqual(code1, 0, out1)
        code2, out2 = self.run_tool("fail-task.py", *args)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.count_events("Failed"), 1)


class FailTaskDoubleFailureEscalationTests(ScriptTestBase):
    def test_prior_failed_in_ndjson_triggers_escalated(self):
        self.seed_task("t-fail-double", state="leased", lease=_live_lease())
        # Pre-seed one prior Failed event for this task (simulating an
        # earlier real failure) directly via the shared library, bypassing
        # fail-task.py, then re-lease and fail again through the CLI.
        aaos.emit_event(
            "Failed", "claude-code", PROJECT,
            refs={"task": "t-fail-double"}, now=NOW - timedelta(minutes=5), root=str(self.root),
        )
        code, out = self.run_tool(
            "fail-task.py", "t-fail-double",
            "--holder", "claude-code", "--session", "sess-1",
            "--reason", "boom again", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.count_events("Failed"), 2)
        self.assertEqual(self.count_events("Escalated"), 1)
        escalated = [e for e in self.events() if e["type"] == "Escalated"][0]
        self.assertEqual(escalated.get("detail"), "DoubleFailure")


# ---------------------------------------------------------------------------
# 2.5 post-handoff.py
# ---------------------------------------------------------------------------

class PostHandoffTestBase(ScriptTestBase):
    """Adds a temp directory *inside* the git tree (under REPO_ROOT) for artifacts
    that must pass real write-scope validation, plus a registry whose write_scope
    is DERIVED from REPO_ROOT at runtime.

    Why the derived registry (OI-263): the production agent-registry.json declares
    claude-code's write_scope as the literal absolute "C:/Users/Owner/AAOS/**". These
    tests previously validated against it directly, so "in scope" was true only while
    this checkout happened to sit at that exact path -- they asserted a directory
    layout, not match_write_scope()'s logic, and failed on any other machine or CI
    runner. Deriving the scope from REPO_ROOT makes the positive-path assertions test
    the matcher, and keeps the production registry untouched.
    """

    def setUp(self):
        super().setUp()
        self._artifacts_tmp = tempfile.mkdtemp(prefix="aaos-artifacts-", dir=str(REPO_ROOT))
        self._registry_path = Path(self._artifacts_tmp) / "agent-registry.json"
        self._registry_path.write_text(
            json.dumps(self._derive_registry()), encoding="utf-8"
        )

    @staticmethod
    def _derive_registry() -> dict:
        """The production registry with every absolute C:/Users/Owner/AAOS/** entry
        rebased onto this checkout's real location. Everything else is left alone so
        the out-of-scope cases still exercise a realistic registry."""
        registry = json.loads(
            (REPO_ROOT / "agent-registry.json").read_text(encoding="utf-8")
        )
        canonical = "C:/Users/Owner/AAOS/**"
        here = str(REPO_ROOT).replace("\\", "/").rstrip("/") + "/**"
        for agent in registry["agents"]:
            for field in ("write_scope", "reachable_paths"):
                if field in agent:
                    agent[field] = [
                        here if entry == canonical else entry for entry in agent[field]
                    ]
        return registry

    def run_tool(self, filename, *args):
        """Inject --registry for post-handoff.py so write-scope validation resolves
        against the derived registry above rather than the production one."""
        if filename == "post-handoff.py" and "--registry" not in args:
            args = (*args, "--registry", str(self._registry_path))
        return super().run_tool(filename, *args)

    def tearDown(self):
        shutil.rmtree(self._artifacts_tmp, ignore_errors=True)
        super().tearDown()

    def make_in_scope_artifact(self, name="evidence.txt", content=b"evidence content"):
        p = Path(self._artifacts_tmp) / name
        p.write_bytes(content)
        return str(p).replace("\\", "/")

    def make_out_of_scope_artifact(self, name="secret.txt", content=b"outside scope"):
        p = self.root / name
        p.write_bytes(content)
        return str(p).replace("\\", "/")


class PostHandoffHappyPathTests(PostHandoffTestBase):
    def test_posts_handoff_with_artifact_manifest(self):
        self.seed_task("t-handoff-happy")
        artifact = self.make_in_scope_artifact()
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-happy", "--task", "t-handoff-happy",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "confirm and close",
            "--summary", "did the thing",
            "--artifact", f"path={artifact}",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        handoff = aaos.load_handoff("h-happy", PROJECT, root=str(self.root))
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["artifact_manifest"][0]["path"], artifact)
        self.assertGreater(handoff["artifact_manifest"][0]["bytes"], 0)
        self.assertEqual(self.count_events("HandoffPosted"), 1)


class PostHandoffWriteScopeTests(PostHandoffTestBase):
    def test_artifact_outside_write_scope_exits_1_writes_nothing(self):
        self.seed_task("t-handoff-scope")
        artifact = self.make_out_of_scope_artifact()
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-scope-violation", "--task", "t-handoff-scope",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "n/a", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 1, out)
        self.assertIsNone(aaos.load_handoff("h-scope-violation", PROJECT, root=str(self.root)))
        self.assertEqual(self.count_events("HandoffPosted"), 0)

    def test_registry_override_is_honoured_not_ignored(self):
        """--registry must actually change the verdict (OI-263).

        Guards two failure modes at once: an override that argparse accepts and the
        code then ignores, and a regression back to validating against the production
        registry's machine-absolute write_scope. Same artifact, two registries, two
        verdicts -- so the assertion can only pass if the flag is really consulted.
        """
        self.seed_task("t-handoff-registry")
        artifact = self.make_in_scope_artifact()

        narrow = json.loads(self._registry_path.read_text(encoding="utf-8"))
        for agent in narrow["agents"]:
            if agent["id"] == "claude-code":
                agent["write_scope"] = ["https://nowhere.invalid/**"]
        narrow_path = Path(self._artifacts_tmp) / "narrow-registry.json"
        narrow_path.write_text(json.dumps(narrow), encoding="utf-8")

        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-registry-narrow", "--task", "t-handoff-registry",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "n/a", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--now", _iso(NOW),
            "--registry", str(narrow_path),
        )
        self.assertEqual(code, 1, out)
        self.assertIn("WRITE-SCOPE VIOLATION", out)

        # Identical call, default (derived) registry -> accepted.
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-registry-wide", "--task", "t-handoff-registry",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "n/a", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)

    def test_derived_registry_does_not_depend_on_checkout_location(self):
        """The derived scope must name THIS checkout, whatever path it sits at.

        The bug OI-263 filed was invisible precisely because it only manifested off
        one machine; this asserts the property directly rather than by running on CI.
        """
        derived = self._derive_registry()
        claude_code = next(a for a in derived["agents"] if a["id"] == "claude-code")
        here = str(REPO_ROOT).replace("\\", "/").rstrip("/") + "/**"
        self.assertIn(here, claude_code["write_scope"])
        self.assertNotIn(
            "C:/Users/Owner/AAOS/**",
            [s for s in claude_code["write_scope"] if s != here],
            "production absolute path survived derivation -- tests would re-couple to one machine",
        )


class PostHandoffMissingArtifactTests(PostHandoffTestBase):
    def test_artifact_missing_on_disk_exits_2(self):
        self.seed_task("t-handoff-missing")
        missing_path = str(Path(self._artifacts_tmp) / "does-not-exist.txt").replace("\\", "/")
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-missing-artifact", "--task", "t-handoff-missing",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "n/a", "--summary", "s",
            "--artifact", f"path={missing_path}",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 2, out)
        self.assertIsNone(aaos.load_handoff("h-missing-artifact", PROJECT, root=str(self.root)))


class PostHandoffIdempotencyTests(PostHandoffTestBase):
    def test_identical_repost_is_noop_exit_0_no_reemit(self):
        self.seed_task("t-handoff-idem")
        artifact = self.make_in_scope_artifact()
        args = (
            "--id", "h-idem", "--task", "t-handoff-idem",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "confirm", "--summary", "s",
            "--artifact", f"path={artifact}", "--now", _iso(NOW),
        )
        code1, out1 = self.run_tool("post-handoff.py", *args)
        code2, out2 = self.run_tool("post-handoff.py", *args)
        self.assertEqual(code1, 0, out1)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.count_events("HandoffPosted"), 1)

    def test_write_once_conflict_exits_3(self):
        self.seed_task("t-handoff-conflict")
        artifact = self.make_in_scope_artifact()
        self.run_tool(
            "post-handoff.py",
            "--id", "h-conflict", "--task", "t-handoff-conflict",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "confirm", "--summary", "original summary",
            "--artifact", f"path={artifact}", "--now", _iso(NOW),
        )
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-conflict", "--task", "t-handoff-conflict",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "confirm", "--summary", "a materially different summary",
            "--artifact", f"path={artifact}", "--now", _iso(NOW),
        )
        self.assertEqual(code, 3, out)
        handoff = aaos.load_handoff("h-conflict", PROJECT, root=str(self.root))
        self.assertEqual(handoff["context_summary"], "original summary")
        self.assertEqual(self.count_events("HandoffPosted"), 1)


class PostHandoffBlockerTests(PostHandoffTestBase):
    def test_nonempty_blocker_blocks_task_and_emits_blocked(self):
        self.seed_task("t-handoff-blocked", state="leased", lease=_live_lease())
        artifact = self.make_in_scope_artifact()
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-blocked", "--task", "t-handoff-blocked",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "unblock me", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--blocker", "waiting on lender docs",
            "--holder", "claude-code", "--session", "sess-1",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-handoff-blocked")
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["blocked_reason"], "waiting on lender docs")
        self.assertEqual(self.event_types(), ["HandoffPosted", "Blocked"])

    def test_blocking_drops_lease_and_archives_live_lock(self):
        # Gate-5 independent-review Issue 1 regression: a blocked task must
        # not keep carrying a lease (task.schema.json's own invariant is
        # "lease present iff state is leased or in-progress") -- otherwise
        # a dead agent's lock on a now-blocked task is unreclaimable
        # forever, since expire-leases.py only re-examines locks whose task
        # is leased/in-progress. This test seeds a REAL lock file on disk
        # (not just the task's `lease` field), matching what a genuine
        # lease-task.py call would have created, so the archive assertion
        # below actually exercises the fix rather than a no-op.
        lease = _live_lease()
        self.seed_task("t-handoff-block-drops-lease", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-handoff-block-drops-lease", PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(json.dumps({
            "protocol_version": "1.0",
            "task_ref": "t-handoff-block-drops-lease",
            "holder": lease["holder"], "session": lease["session"],
            "acquired": lease["acquired"], "expires": lease["expires"],
        }), encoding="utf-8")

        artifact = self.make_in_scope_artifact()
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-block-drops-lease", "--task", "t-handoff-block-drops-lease",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "unblock me", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--blocker", "waiting on lender docs",
            "--holder", "claude-code", "--session", "sess-1",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)

        task = self.reload_task("t-handoff-block-drops-lease")
        self.assertEqual(task["state"], "blocked")
        self.assertNotIn("lease", task, "blocked task must not carry a lease (schema invariant)")

        self.assertFalse(lock_file.exists(), "lock must no longer be live once the task is blocked")
        archived = list((lock_file.parent / "_archive").glob("t-handoff-block-drops-lease.*.lock"))
        self.assertEqual(len(archived), 1, "lock must be archived, not deleted, when blocking releases it")

        # And the property this whole fix exists for: expire-leases.py must
        # not (mis)report this as "already reclaimed by a concurrent
        # sweep" -- there is no live lock left to even consider.
        code2, out2 = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code2, 0, out2)
        self.assertNotIn("SKIP", out2)
        self.assertEqual(self.reload_task("t-handoff-block-drops-lease")["state"], "blocked")

    def test_blocker_with_lost_lease_exits_4(self):
        self.seed_task("t-handoff-blocked-lost", state="leased", lease=_live_lease(holder="claude-code", session="sess-1"))
        artifact = self.make_in_scope_artifact()
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-blocked-lost", "--task", "t-handoff-blocked-lost",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "unblock me", "--summary", "s",
            "--artifact", f"path={artifact}",
            "--blocker", "waiting on lender docs",
            "--holder", "cowork", "--session", "wrong-sess",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 4, out)
        task = self.reload_task("t-handoff-blocked-lost")
        self.assertEqual(task["state"], "leased")


# ---------------------------------------------------------------------------
# 2.6 expire-leases.py
# ---------------------------------------------------------------------------

class ExpireLeasesReclaimTests(ScriptTestBase):
    def _seed_expired(self, task_id, holder="cowork"):
        lease = _expired_lease(holder=holder, session="old-sess", now=NOW)
        self.seed_task(task_id, state="leased", lease=lease)
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": task_id, "protocol_version": "1.0"})
        return lock_file

    def _seed_heartbeat(self, agent_id, status):
        aaos.save_json(aaos.heartbeat_path(root=str(self.root)), {
            "protocol_version": "1.0",
            "derived_at": _iso(NOW),
            "agents": [{
                "agent_id": agent_id, "last_run": _iso(NOW - timedelta(hours=20)),
                "source": ["test"], "status": status, "threshold_hours": 16,
            }],
        })

    def test_expired_missed_heartbeat_reclaims_requeues_and_escalates(self):
        lock_file = self._seed_expired("t-expire-reclaim", holder="cowork")
        self._seed_heartbeat("cowork", "missed")

        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)

        task = self.reload_task("t-expire-reclaim")
        self.assertEqual(task["state"], "queued")
        self.assertNotIn("lease", task)
        self.assertFalse(lock_file.exists())

        archive_dir = aaos.lock_archive_dir(PROJECT, root=str(self.root))
        self.assertEqual(len(list(archive_dir.glob("t-expire-reclaim.*.lock"))), 1)

        self.assertEqual(self.count_events("LeaseExpired"), 1)
        self.assertEqual(self.count_events("Escalated"), 1)
        escalated = [e for e in self.events() if e["type"] == "Escalated"][0]
        self.assertEqual(escalated.get("detail"), "DeadAgent")

    def test_expired_fresh_heartbeat_reclaims_without_escalation(self):
        self._seed_expired("t-expire-lapsed", holder="claude-code")
        self._seed_heartbeat("claude-code", "fresh")

        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)

        task = self.reload_task("t-expire-lapsed")
        self.assertEqual(task["state"], "queued")
        self.assertEqual(self.count_events("LeaseExpired"), 1)
        self.assertEqual(self.count_events("Escalated"), 0)

    def test_not_yet_expired_lock_untouched(self):
        lease = _live_lease(holder="claude-code", session="s1", now=NOW)
        self.seed_task("t-expire-live", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-expire-live", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-live", "protocol_version": "1.0"})

        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)

        task = self.reload_task("t-expire-live")
        self.assertEqual(task["state"], "leased")
        self.assertTrue(lock_file.exists())
        self.assertEqual(self.events(), [])


class ExpireLeasesConcurrentSweepRaceTests(ScriptTestBase):
    """Finding 3 (adversarial review): a concurrent second sweep reclaiming
    the SAME lock hits archive_lock's os.replace on a lock file another
    sweep already moved -> uncaught FileNotFoundError (main() only caught
    aaos.AaosError, so it propagated as a raw traceback). Simulates the
    reviewer's exact race: after reclaim_decision says reclaimable but
    before archive_lock actually runs, the lock file vanishes out from
    under this sweep (as it would if a concurrent sweep had already
    archived it) -- via a wrapper around aaos.archive_lock that
    deletes-then-calls, per the task's own suggested repro."""

    def _patch_archive_lock_to_delete_first(self, module):
        original_archive_lock = module.aaos.archive_lock

        def _delete_then_archive(lock_arg, **kwargs):
            try:
                Path(lock_arg).unlink()
            except OSError:
                pass
            return original_archive_lock(lock_arg, **kwargs)

        module.aaos.archive_lock = _delete_then_archive
        return original_archive_lock

    def test_lock_vanishing_before_archive_does_not_crash_or_duplicate_lease_expired(self):
        lease = _expired_lease(holder="cowork", session="old-sess", now=NOW)
        self.seed_task("t-expire-race", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-expire-race", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-race", "protocol_version": "1.0"})

        module = load_tool("expire-leases.py")
        original_archive_lock = self._patch_archive_lock_to_delete_first(module)
        try:
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        finally:
            module.aaos.archive_lock = original_archive_lock

        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)

        task = self.reload_task("t-expire-race")
        self.assertEqual(task["state"], "queued")
        self.assertNotIn("lease", task)

        # Exactly one LeaseExpired -- the reclaim transaction's own
        # commit, which had already succeeded before the (simulated)
        # concurrent sweep's archive removed the lock file.
        self.assertEqual(self.count_events("LeaseExpired"), 1)

    def test_dead_agent_escalated_survives_archive_failure(self):
        lease = _expired_lease(holder="cowork", session="old-sess", now=NOW)
        self.seed_task("t-expire-race-dead", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-expire-race-dead", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-race-dead", "protocol_version": "1.0"})
        aaos.save_json(aaos.heartbeat_path(root=str(self.root)), {
            "protocol_version": "1.0",
            "derived_at": _iso(NOW),
            "agents": [{
                "agent_id": "cowork", "last_run": _iso(NOW - timedelta(hours=20)),
                "source": ["test"], "status": "missed", "threshold_hours": 16,
            }],
        })

        module = load_tool("expire-leases.py")
        original_archive_lock = self._patch_archive_lock_to_delete_first(module)
        try:
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        finally:
            module.aaos.archive_lock = original_archive_lock

        self.assertEqual(code, 0, out)
        self.assertEqual(self.count_events("LeaseExpired"), 1)
        # The DeadAgent Escalated must not be lost just because the lock
        # file itself could no longer be moved to _archive/.
        self.assertEqual(self.count_events("Escalated"), 1)
        escalated = [e for e in self.events() if e["type"] == "Escalated"][0]
        self.assertEqual(escalated.get("detail"), "DeadAgent")

    def test_lock_already_gone_before_transaction_starts_is_skipped_silently(self):
        """The (a) pre-check: if the lock is ALREADY gone by the time this
        sweep reaches the reclaim transaction (a fully-concurrent sweep
        already won), skip without starting a redundant reclaim at all --
        no LeaseExpired, no state churn, no crash. Simulates the narrow
        real window where a winning sweep's commit_state_change (task ->
        queued) has already landed but its archive_lock hasn't happened
        yet, by deleting the lock file as a side effect of THIS sweep's
        own initial read of it."""
        lease = _expired_lease(holder="cowork", session="old-sess", now=NOW)
        self.seed_task("t-expire-already-gone", state="queued")  # as if already reclaimed by "the winner"
        lock_file = aaos.lock_path("t-expire-already-gone", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-already-gone", "protocol_version": "1.0"})

        module = load_tool("expire-leases.py")
        original_load_json = module.aaos.load_json

        def _load_then_vanish(path, *a, **kw):
            result = original_load_json(path, *a, **kw)
            if str(path) == str(lock_file):
                try:
                    lock_file.unlink()
                except OSError:
                    pass
            return result

        module.aaos.load_json = _load_then_vanish
        try:
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        finally:
            module.aaos.load_json = original_load_json

        self.assertEqual(code, 0, out)
        self.assertEqual(self.count_events("LeaseExpired"), 0)
        task = self.reload_task("t-expire-already-gone")
        self.assertEqual(task["state"], "queued")

    def test_unexpected_exception_exits_nonzero_not_raw_traceback(self):
        """(d): main()'s outer catch used to only cover aaos.AaosError
        subclasses -- any other exception (this Finding's own raw
        FileNotFoundError included) propagated straight out of main(),
        crashing a scheduled sweep instead of exiting cleanly non-zero."""
        module = load_tool("expire-leases.py")
        original_load_heartbeat = module.aaos.load_heartbeat

        def _boom(*a, **kw):
            raise RuntimeError("simulated totally unexpected failure")

        module.aaos.load_heartbeat = _boom
        try:
            code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        finally:
            module.aaos.load_heartbeat = original_load_heartbeat

        self.assertNotEqual(code, 0)


class ExpireLeasesDryRunTests(ScriptTestBase):
    def test_dry_run_makes_zero_changes(self):
        lease = _expired_lease(holder="cowork", session="old", now=NOW)
        self.seed_task("t-expire-dryrun", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-expire-dryrun", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-dryrun", "protocol_version": "1.0"})

        code, out = self.run_tool("expire-leases.py", "--dry-run", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)
        self.assertIn("t-expire-dryrun", out)

        task = self.reload_task("t-expire-dryrun")
        self.assertEqual(task["state"], "leased")
        self.assertTrue(lock_file.exists())
        self.assertEqual(self.events(), [])

    def test_dry_run_with_nonempty_spool_still_makes_zero_writes(self):
        """Finding 4 (adversarial review): the OLD code drained the event
        spool unconditionally, before the --dry-run check -- so a dry-run
        preview with events already sitting in the spool (left over from a
        prior contended emit) silently wrote them into the durable NDJSON,
        breaking --dry-run's "zero writes" promise. This is the specific
        case the reviewer's probe broke; the empty-spool case above already
        passed even pre-fix."""
        lease = _expired_lease(holder="cowork", session="old", now=NOW)
        self.seed_task("t-expire-dryrun-spool", state="leased", lease=lease)
        lock_file = aaos.lock_path("t-expire-dryrun-spool", PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": "t-expire-dryrun-spool", "protocol_version": "1.0"})

        # Seed a pending spooled event, simulating a prior contended emit.
        spool_event = {
            "protocol_version": "1.0", "event_id": "e-2026-07-09T00:00:00Z-1",
            "type": "TaskCreated", "actor": "system", "project_id": PROJECT,
            "timestamp": _iso(NOW), "refs": {"task": "t-spooled-leftover"},
        }
        aaos.spool_event(spool_event, PROJECT, root=str(self.root))

        tree_before = sorted(
            (str(p.relative_to(self.root)), p.stat().st_mtime_ns)
            for p in self.root.rglob("*") if p.is_file()
        )

        code, out = self.run_tool("expire-leases.py", "--dry-run", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)

        tree_after = sorted(
            (str(p.relative_to(self.root)), p.stat().st_mtime_ns)
            for p in self.root.rglob("*") if p.is_file()
        )
        self.assertEqual(tree_before, tree_after, "dry-run must make ZERO filesystem writes, including draining")

        # The spooled event is still exactly where it was -- not folded in.
        ndjson_path = aaos.events_log_path(PROJECT, root=str(self.root))
        self.assertFalse(ndjson_path.exists())
        pending = list(aaos.event_spool_dir(PROJECT, root=str(self.root)).glob("*.json"))
        self.assertEqual(len(pending), 1)


class ExpireLeasesCorruptLockTests(ScriptTestBase):
    def test_corrupt_lock_fails_closed_skip_not_stolen(self):
        self.seed_task("t-expire-corrupt", state="queued")
        lock_file = aaos.lock_path("t-expire-corrupt", PROJECT, root=str(self.root))
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("{not valid json", encoding="utf-8")

        code, out = self.run_tool("expire-leases.py", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)

        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "{not valid json")
        task = self.reload_task("t-expire-corrupt")
        self.assertEqual(task["state"], "queued")

        self.assertEqual(self.count_events("Escalated"), 1)
        escalated = [e for e in self.events() if e["type"] == "Escalated"][0]
        self.assertEqual(escalated.get("detail"), "CorruptState")


# ---------------------------------------------------------------------------
# 2.7 emit-event.py
# ---------------------------------------------------------------------------

class EmitEventHappyPathTests(ScriptTestBase):
    def test_emits_schema_valid_event(self):
        code, out = self.run_tool(
            "emit-event.py", "--type", "TaskCreated", "--actor", "claude-code",
            "--task", "t-x", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.count_events("TaskCreated"), 1)
        event = self.events()[0]
        self.assertEqual(event["refs"]["task"], "t-x")


class EmitEventNonIdempotentTests(ScriptTestBase):
    def test_repeated_identical_calls_each_mint_a_new_event(self):
        args = ("--type", "TaskCreated", "--actor", "claude-code", "--task", "t-x", "--now", _iso(NOW))
        code1, out1 = self.run_tool("emit-event.py", *args)
        code2, out2 = self.run_tool("emit-event.py", *args)
        self.assertEqual(code1, 0, out1)
        self.assertEqual(code2, 0, out2)
        self.assertEqual(self.count_events("TaskCreated"), 2)
        ids = [e["event_id"] for e in self.events()]
        self.assertEqual(len(set(ids)), 2)


class EmitEventDrainOnlyTests(ScriptTestBase):
    def test_drain_only_folds_spooled_events_into_ndjson(self):
        aaos.spool_event(
            {
                "protocol_version": "1.0",
                "event_id": "e-spooled-1",
                "type": "TaskCreated",
                "actor": "claude-code",
                "timestamp": _iso(NOW),
                "project_id": PROJECT,
                "refs": {"task": "t-spooled"},
            },
            PROJECT, root=str(self.root),
        )
        code, out = self.run_tool("emit-event.py", "--drain-only", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        self.assertEqual(list(spool_dir.glob("*.json")), [])
        self.assertEqual(self.count_events("TaskCreated"), 1)


class EmitEventUsageErrorTests(ScriptTestBase):
    def test_missing_type_and_actor_exits_2(self):
        code, out = self.run_tool("emit-event.py", "--now", _iso(NOW))
        self.assertEqual(code, 2, out)
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
