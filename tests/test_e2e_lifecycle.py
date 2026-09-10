"""
tests/test_e2e_lifecycle.py — Phase 3 Gate 3 end-to-end lifecycle test.

Written FIRST per AAOS TDD doctrine: fails until all seven tools/*.py CLI
scripts exist. Drives the REAL scripts via subprocess (not in-process) --
deliberately separate from tests/test_queue_scripts.py's in-process
per-script tests -- because the point of this test is to prove the actual
invocation path/exit codes/process boundaries a production sweep will use,
per docs/PHASE3-DESIGN.md section 5.3:

    create-task -> lease-task -> post-handoff -> complete-task --archive

with a fixed --now per step, asserting: exit 0 each step; final task state
"archived" with evidence and no lease; the handoff file exists and passes
tools/validate.py (run as a subprocess); the NDJSON event sequence is
EXACTLY TaskCreated, TaskLeased, HandoffPosted, Completed, Archived in
order; and the task file, and the NDJSON file itself, also pass
tools/validate.py.

Registry note (revised 2026-09-10, OI-263): the handoff's one artifact
lives in a temp directory created *inside* the git tree
(tempfile.mkdtemp(dir=REPO_ROOT)) so it falls within claude-code's
registered write_scope, cleaned up in tearDown regardless of outcome.
That scope is declared in the production registry as the literal absolute
"C:/Users/Owner/AAOS/**", so this only held on a checkout physically
located there. The test now passes --registry (a flag added to
post-handoff.py in the same change) pointing at
_tool_loader.derive_local_registry()'s rebased copy, which names THIS
checkout -- so the assertion tests write-scope matching rather than where
the repo happens to sit.

Run with:  python -m unittest tests.test_e2e_lifecycle -v   (from the repo root)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tool_loader import write_local_registry  # noqa: E402  (after sys.path setup, by design)

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PROJECT = "ora-ops"


def _iso(dt):
    return dt.isoformat()


def _run_tool(tool_name, args, env):
    cmd = [sys.executable, str(TOOLS_DIR / tool_name)] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _run_validate(target_path, registry_path=None):
    """validate.py on one state file.

    `registry_path` matters for the handoff: validate.py re-checks artifact
    write_scope against the registry, so validating a handoff whose artifact lives
    in this checkout needs the same derived registry post-handoff.py was given
    (OI-263). Task and event files carry no artifact paths, so they don't need it.
    """
    cmd = [sys.executable, str(TOOLS_DIR / "validate.py"), str(target_path)]
    if registry_path is not None:
        cmd += ["--registry", str(registry_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class E2ELifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-e2e-")
        self.root = Path(self._tmpdir)
        for sub in ("tasks", "handoffs", "locks/_archive", "events"):
            (self.root / "projects" / PROJECT / sub).mkdir(parents=True, exist_ok=True)

        self._artifacts_tmp = tempfile.mkdtemp(prefix="aaos-e2e-artifacts-", dir=str(REPO_ROOT))
        self._registry_path = write_local_registry(self._artifacts_tmp)

        self.env = dict(os.environ)
        self.env["AAOS_SEAM_ROOT"] = str(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        shutil.rmtree(self._artifacts_tmp, ignore_errors=True)

    def test_full_lifecycle_create_lease_handoff_complete_archive(self):
        task_id = "t-e2e-lifecycle"
        t0 = NOW
        t1 = NOW + timedelta(minutes=1)
        t2 = NOW + timedelta(minutes=2)
        t3 = NOW + timedelta(minutes=3)

        code, out, err = _run_tool(
            "create-task.py",
            ["--title", "E2E lifecycle task", "--id", task_id,
             "--accept", "it completes end to end", "--now", _iso(t0)],
            self.env,
        )
        self.assertEqual(code, 0, out + err)

        code, out, err = _run_tool(
            "lease-task.py",
            [task_id, "--holder", "claude-code", "--session", "sess-e2e",
             "--settle-sec", "0", "--now", _iso(t1)],
            self.env,
        )
        self.assertEqual(code, 0, out + err)

        artifact_path = Path(self._artifacts_tmp) / "evidence.txt"
        artifact_path.write_text("e2e evidence", encoding="utf-8")
        artifact_str = str(artifact_path).replace("\\", "/")

        code, out, err = _run_tool(
            "post-handoff.py",
            ["--id", "h-e2e-lifecycle", "--task", task_id,
             "--from", "claude-code", "--to", "cowork",
             "--next-action", "confirm and close",
             "--summary", "e2e lifecycle handoff",
             "--artifact", f"path={artifact_str}",
             "--registry", str(self._registry_path),
             "--now", _iso(t2)],
            self.env,
        )
        self.assertEqual(code, 0, out + err)

        code, out, err = _run_tool(
            "complete-task.py",
            [task_id, "--holder", "claude-code", "--session", "sess-e2e",
             "--evidence", "label=proof", "ref=evidence.txt",
             "--archive", "--now", _iso(t3)],
            self.env,
        )
        self.assertEqual(code, 0, out + err)

        # --- Final task state: archived, evidence present, no lease. ---
        task_path = self.root / "projects" / PROJECT / "tasks" / f"{task_id}.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertEqual(task["state"], "archived")
        self.assertNotIn("lease", task)
        self.assertTrue(task.get("evidence"))

        # --- Handoff file exists and re-validates. ---
        handoff_path = self.root / "projects" / PROJECT / "handoffs" / "h-e2e-lifecycle.json"
        self.assertTrue(handoff_path.exists())
        code, out, err = _run_validate(handoff_path, registry_path=self._registry_path)
        self.assertEqual(code, 0, out + err)

        # --- NDJSON event sequence is exactly this, in order. ---
        events_path = self.root / "projects" / PROJECT / "events" / "aaos-events.ndjson"
        lines = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(
            [e["type"] for e in lines],
            ["TaskCreated", "TaskLeased", "HandoffPosted", "Completed", "Archived"],
        )

        # --- Task file and the NDJSON file itself also re-validate. ---
        code, out, err = _run_validate(task_path)
        self.assertEqual(code, 0, out + err)

        code, out, err = _run_validate(events_path)
        self.assertEqual(code, 0, out + err)


if __name__ == "__main__":
    unittest.main()
