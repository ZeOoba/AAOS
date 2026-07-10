"""
Tests for tools/project-notion.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/project-notion.py does not exist yet, so every test in this module is
expected to fail (via a FileNotFoundError raised while loading the module
under test in setUpModule) until tools/project-notion.py is implemented.
See docs/PHASE3-DESIGN.md section 4.2 for the spec this pins down.

The script's filename is hyphenated (not a valid Python identifier), so it
is loaded via importlib.util.spec_from_file_location rather than a normal
import statement.

Run with:  python -m unittest tests.test_project_notion -v   (from repo root)
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)


def _load_hyphenated_module(name: str, filename: str):
    path = TOOLS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pn = _load_hyphenated_module("project_notion", "project-notion.py")


def run_pn(*args):
    """Invoke project-notion.py's main() in-process, capturing stdout.
    Returns (exit_code, stdout_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pn.main(list(args))
    return code, buf.getvalue()


NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
NOW_ISO = NOW.isoformat()
PROJECT = "ora-ops"


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _make_task(task_id, *, state="queued", owner_agent="unassigned", priority=2, lease=None, title=None):
    task = {
        "protocol_version": "1.0",
        "id": task_id,
        "project_id": PROJECT,
        "title": title or f"Task {task_id}",
        "owner_agent": owner_agent,
        "state": state,
        "priority": priority,
        "inputs": [],
        "acceptance_criteria": ["it works"],
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    if lease is not None:
        task["lease"] = lease
    return task


def _make_event(event_id, event_type, task_id, timestamp, actor="claude-code"):
    return {
        "protocol_version": "1.0",
        "event_id": event_id,
        "type": event_type,
        "actor": actor,
        "timestamp": timestamp,
        "project_id": PROJECT,
        "refs": {"task": task_id},
    }


class BaseProjectNotionTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-pn-test-")
        self.root = Path(self._tmpdir)
        self._old_env = os.environ.get("AAOS_SEAM_ROOT")
        os.environ["AAOS_SEAM_ROOT"] = str(self.root)
        self.tasks_dir = self.root / "projects" / PROJECT / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AAOS_SEAM_ROOT", None)
        else:
            os.environ["AAOS_SEAM_ROOT"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_task(self, task_id, **kwargs) -> Path:
        task = _make_task(task_id, **kwargs)
        path = self.tasks_dir / f"{task_id}.json"
        path.write_text(json.dumps(task), encoding="utf-8")
        return path

    def _write_event_log(self, events) -> Path:
        path = aaos.events_log_path(PROJECT, root=str(self.root))
        os.makedirs(path.parent, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return path

    def _write_heartbeat(self, agents) -> Path:
        doc = {
            "protocol_version": "1.0",
            "derived_at": NOW_ISO,
            "agents": agents,
        }
        path = aaos.heartbeat_path(root=str(self.root))
        aaos.save_json(path, doc)
        return path

    def _out_path(self) -> Path:
        return self.root / "out-projection.json"


# ---------------------------------------------------------------------------
# Multi-task project -> correct rows
# ---------------------------------------------------------------------------

class MultiTaskRowsTests(BaseProjectNotionTest):
    def test_mix_of_states_and_leases_produce_correct_rows(self):
        self._write_task("t-a", state="queued", owner_agent="unassigned", priority=1, title="Task A")
        lease = {
            "holder": "cowork",
            "acquired": (NOW - timedelta(minutes=10)).isoformat(),
            "expires": (NOW + timedelta(minutes=35)).isoformat(),
            "session": "sess-1",
        }
        self._write_task("t-b", state="leased", owner_agent="cowork", priority=2, lease=lease, title="Task B")
        self._write_task("t-c", state="done", owner_agent="claude-code", priority=3, title="Task C")

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        rows = {r["task_id"]: r for r in doc["rows"]}
        self.assertEqual(set(rows), {"t-a", "t-b", "t-c"})

        self.assertEqual(rows["t-a"]["state"], "queued")
        self.assertEqual(rows["t-a"]["owner_agent"], "unassigned")
        self.assertEqual(rows["t-a"]["priority"], 1)
        self.assertIsNone(rows["t-a"]["lease"])
        self.assertEqual(rows["t-a"]["title"], "Task A")

        self.assertEqual(rows["t-b"]["state"], "leased")
        self.assertEqual(rows["t-b"]["lease"], {"holder": "cowork", "expires": lease["expires"]})

        self.assertEqual(rows["t-c"]["state"], "done")
        self.assertIsNone(rows["t-c"]["lease"])


# ---------------------------------------------------------------------------
# Latest-event-per-task selection
# ---------------------------------------------------------------------------

class LatestEventSelectionTests(BaseProjectNotionTest):
    def test_newest_of_three_events_is_picked(self):
        self._write_task("t-a", state="done")
        events = [
            _make_event("e-1", "TaskCreated", "t-a", (NOW - timedelta(hours=2)).isoformat()),
            _make_event("e-2", "TaskLeased", "t-a", (NOW - timedelta(hours=1)).isoformat()),
            _make_event("e-3", "Completed", "t-a", NOW.isoformat()),
        ]
        self._write_event_log(events)

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertEqual(row["last_event"], {"type": "Completed", "timestamp": NOW.isoformat()})

    def test_event_order_in_log_does_not_matter(self):
        # Write the newest event FIRST in the file to prove selection is by
        # timestamp value, not by NDJSON line order.
        self._write_task("t-a", state="done")
        events = [
            _make_event("e-3", "Completed", "t-a", NOW.isoformat()),
            _make_event("e-1", "TaskCreated", "t-a", (NOW - timedelta(hours=2)).isoformat()),
            _make_event("e-2", "TaskLeased", "t-a", (NOW - timedelta(hours=1)).isoformat()),
        ]
        self._write_event_log(events)

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertEqual(row["last_event"]["type"], "Completed")

    def test_task_with_no_events_has_null_last_event(self):
        self._write_task("t-a", state="queued")
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertIsNone(row["last_event"])


# ---------------------------------------------------------------------------
# Stale flag boundary
# ---------------------------------------------------------------------------

class StaleFlagBoundaryTests(BaseProjectNotionTest):
    def test_inputs_eight_days_old_is_stale(self):
        path = self._write_task("t-a", state="queued")
        _set_mtime(path, NOW - timedelta(days=8))

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(doc["generation"]["stale"])

    def test_inputs_one_hour_old_is_not_stale(self):
        path = self._write_task("t-a", state="queued")
        _set_mtime(path, NOW - timedelta(hours=1))

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(doc["generation"]["stale"])

    def test_generation_block_has_generated_at(self):
        self._write_task("t-a", state="queued")
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["generation"]["generated_at"], NOW_ISO)


# ---------------------------------------------------------------------------
# Malformed task file -> skipped[], others still projected
# ---------------------------------------------------------------------------

class MalformedTaskFileTests(BaseProjectNotionTest):
    def test_malformed_json_task_file_is_skipped_others_projected(self):
        self._write_task("t-good", state="queued")
        bad_path = self.tasks_dir / "t-bad.json"
        bad_path.write_text("{not valid json at all", encoding="utf-8")

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))

        task_ids = {r["task_id"] for r in doc["rows"]}
        self.assertEqual(task_ids, {"t-good"})

        skipped = doc["generation"]["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["file"], "t-bad.json")
        self.assertTrue(skipped[0]["reason"])

    def test_task_file_missing_required_field_is_skipped(self):
        self._write_task("t-good", state="queued")
        incomplete = {"protocol_version": "1.0", "id": "t-incomplete"}  # missing title/state/etc
        (self.tasks_dir / "t-incomplete.json").write_text(json.dumps(incomplete), encoding="utf-8")

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        task_ids = {r["task_id"] for r in doc["rows"]}
        self.assertEqual(task_ids, {"t-good"})
        skipped_files = {s["file"] for s in doc["generation"]["skipped"]}
        self.assertIn("t-incomplete.json", skipped_files)


# ---------------------------------------------------------------------------
# Empty project -> rows: []
# ---------------------------------------------------------------------------

class EmptyProjectTests(BaseProjectNotionTest):
    def test_no_tasks_yields_empty_rows(self):
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["rows"], [])

    def test_missing_tasks_dir_entirely_yields_empty_rows(self):
        shutil.rmtree(self.tasks_dir)
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["rows"], [])


class SeamUnavailableTests(BaseProjectNotionTest):
    def test_nonexistent_seam_root_fails_closed_not_empty_success(self):
        # Gate-5 review Minor #3: this tool's inputs (tasks/, events log,
        # heartbeat.json) are always seam-rooted -- a phantom seam must not
        # silently produce an empty-but-"successful" projection (rows:[],
        # exit 0), which would misrepresent an unreachable seam as a
        # genuinely empty, current project.
        phantom = self.root / "does-not-exist"
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(phantom),
        )
        self.assertEqual(code, 2, output)
        self.assertIn("seam root unreachable", output)
        self.assertFalse(out.exists(), "no projection file should be written for an unreachable seam")
        self.assertFalse(phantom.exists(), "the phantom seam root itself must not be auto-created")


# ---------------------------------------------------------------------------
# heartbeat.json absent -> every task's heartbeat_status = unknown
# ---------------------------------------------------------------------------

class HeartbeatAbsentTests(BaseProjectNotionTest):
    def test_heartbeat_absent_yields_unknown_for_all_tasks(self):
        self._write_task("t-a", state="queued", owner_agent="claude-code")
        lease = {
            "holder": "cowork",
            "acquired": NOW.isoformat(),
            "expires": (NOW + timedelta(minutes=45)).isoformat(),
            "session": "sess-1",
        }
        self._write_task("t-b", state="leased", owner_agent="cowork", lease=lease)

        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        for row in doc["rows"]:
            self.assertEqual(row["heartbeat_status"], "unknown")


class HeartbeatPresentTests(BaseProjectNotionTest):
    def test_owner_agent_heartbeat_used_when_no_lease(self):
        self._write_task("t-a", state="queued", owner_agent="claude-code")
        self._write_heartbeat([
            {
                "agent_id": "claude-code",
                "last_run": NOW.isoformat(),
                "source": ["fleet-status.py:dual-fleet-health"],
                "status": "fresh",
                "threshold_hours": 16,
            }
        ])
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertEqual(row["heartbeat_status"], "fresh")

    def test_lease_holder_heartbeat_used_over_owner_agent(self):
        lease = {
            "holder": "cowork",
            "acquired": NOW.isoformat(),
            "expires": (NOW + timedelta(minutes=45)).isoformat(),
            "session": "sess-1",
        }
        # owner_agent is claude-code, but the ACTIVE holder is cowork -- the
        # holder's heartbeat should win.
        self._write_task("t-a", state="leased", owner_agent="claude-code", lease=lease)
        self._write_heartbeat([
            {
                "agent_id": "claude-code",
                "last_run": NOW.isoformat(),
                "source": ["fleet-status.py:dual-fleet-health"],
                "status": "fresh",
                "threshold_hours": 16,
            },
            {
                "agent_id": "cowork",
                "last_run": (NOW - timedelta(hours=20)).isoformat(),
                "source": ["notion:35c6ecd8 last_edited_time"],
                "status": "missed",
                "threshold_hours": 16,
            },
        ])
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertEqual(row["heartbeat_status"], "missed")

    def test_agent_id_not_in_heartbeat_is_unknown(self):
        self._write_task("t-a", state="queued", owner_agent="claude-code")
        self._write_heartbeat([
            {
                "agent_id": "cowork",
                "last_run": NOW.isoformat(),
                "source": ["notion:35c6ecd8 last_edited_time"],
                "status": "fresh",
                "threshold_hours": 16,
            },
        ])
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["task_id"] == "t-a")
        self.assertEqual(row["heartbeat_status"], "unknown")


# ---------------------------------------------------------------------------
# Atomic write — no .tmp-* residue, default out path
# ---------------------------------------------------------------------------

class AtomicWriteResidueTests(BaseProjectNotionTest):
    def test_output_lands_atomically_no_tmp_residue(self):
        self._write_task("t-a", state="queued")
        out = self._out_path()
        code, output = run_pn(
            "--project", PROJECT, "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        leftovers = list(out.parent.glob(f"{out.name}.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_default_out_path_is_seam_projects_project_notion_projection(self):
        self._write_task("t-a", state="queued")
        code, output = run_pn(
            "--project", PROJECT, "--now", NOW_ISO, "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        expected = self.root / "projects" / PROJECT / "notion-projection.json"
        self.assertTrue(expected.exists())
        self.assertIn(str(expected), output)

    def test_default_project_is_ora_ops(self):
        self._write_task("t-a", state="queued")
        code, output = run_pn("--now", NOW_ISO, "--seam-root", str(self.root))
        self.assertEqual(code, 0, output)
        expected = self.root / "projects" / "ora-ops" / "notion-projection.json"
        self.assertTrue(expected.exists())


if __name__ == "__main__":
    unittest.main()
