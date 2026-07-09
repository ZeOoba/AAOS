"""
Tests for tools/derive-heartbeat.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/derive-heartbeat.py does not exist yet, so every test in this module
is expected to fail (via a FileNotFoundError raised while loading the
module under test in setUpModule) until tools/derive-heartbeat.py is
implemented. See docs/PHASE3-DESIGN.md section 4.1 for the spec this pins
down.

The script's filename is hyphenated (not a valid Python identifier), so it
is loaded via importlib.util.spec_from_file_location rather than a normal
import statement.

Run with:  python -m unittest tests.test_derive_heartbeat -v   (from repo root)
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
from unittest import mock

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


dh = _load_hyphenated_module("derive_heartbeat", "derive-heartbeat.py")


def run_dh(*args):
    """Invoke derive-heartbeat.py's main() in-process, capturing stdout.
    Returns (exit_code, stdout_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = dh.main(list(args))
    return code, buf.getvalue()


NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
NOW_ISO = NOW.isoformat()

# A minimal two-agent registry fixture, mirroring the shape of the real
# agent-registry.json but fully under test control (aaos.load_registry()
# always reads REPO_ROOT/agent-registry.json with no override lever per
# design section 0.2 — so registry-dependent scenarios patch
# aaos.load_registry directly rather than pointing at a fixture file).
BASE_REGISTRY = {
    "protocol_version": "1.0",
    "generated_by": "test fixture",
    "agents": [
        {
            "id": "claude-code",
            "heartbeat_source": ["fleet-session-hook:session-state", "fleet-status.py:dual-fleet-health"],
            "active": True,
        },
        {
            "id": "cowork",
            "heartbeat_source": ["notion:35c6ecd8 last_edited_time", "automation-health-monitor:beacon-table"],
            "active": True,
        },
    ],
}


def _registry_with(agents_overrides):
    """Build a registry fixture from BASE_REGISTRY, applying per-id overrides."""
    reg = json.loads(json.dumps(BASE_REGISTRY))
    for agent in reg["agents"]:
        if agent["id"] in agents_overrides:
            agent.update(agents_overrides[agent["id"]])
    return reg


class BaseDeriveHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-dh-test-")
        self.root = Path(self._tmpdir)
        self._old_env = os.environ.get("AAOS_SEAM_ROOT")
        os.environ["AAOS_SEAM_ROOT"] = str(self.root)
        self._registry_patcher = mock.patch.object(aaos, "load_registry", return_value=BASE_REGISTRY)
        self._registry_patcher.start()

    def tearDown(self):
        self._registry_patcher.stop()
        if self._old_env is None:
            os.environ.pop("AAOS_SEAM_ROOT", None)
        else:
            os.environ["AAOS_SEAM_ROOT"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_signals(self, obj) -> Path:
        path = self.root / "signals.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def _out_path(self) -> Path:
        return self.root / "out-heartbeat.json"


# ---------------------------------------------------------------------------
# Status-banding math at the boundaries
# ---------------------------------------------------------------------------

class BandingBoundaryTests(BaseDeriveHeartbeatTest):
    """Boundary semantics pinned per design section 4.1: delta<=threshold ->
    fresh; delta<=2x threshold -> stale; delta>2x threshold -> missed. The
    boundary values themselves (exactly 1x and exactly 2x) fall on the
    "still fresh" / "still stale" side of the <= comparisons."""

    def _run_single_agent(self, last_run_dt, threshold_hours):
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": last_run_dt.isoformat() if last_run_dt else None,
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": threshold_hours,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        entry = next(a for a in doc["agents"] if a["agent_id"] == "claude-code")
        return entry

    def test_delta_exactly_at_1x_threshold_is_fresh(self):
        threshold = 16
        last_run = NOW - timedelta(hours=threshold)
        entry = self._run_single_agent(last_run, threshold)
        self.assertEqual(entry["status"], "fresh")

    def test_delta_just_over_1x_threshold_is_stale(self):
        threshold = 16
        last_run = NOW - timedelta(hours=threshold, minutes=1)
        entry = self._run_single_agent(last_run, threshold)
        self.assertEqual(entry["status"], "stale")

    def test_delta_exactly_at_2x_threshold_is_stale(self):
        threshold = 16
        last_run = NOW - timedelta(hours=2 * threshold)
        entry = self._run_single_agent(last_run, threshold)
        self.assertEqual(entry["status"], "stale")

    def test_delta_just_over_2x_threshold_is_missed(self):
        threshold = 16
        last_run = NOW - timedelta(hours=2 * threshold, minutes=1)
        entry = self._run_single_agent(last_run, threshold)
        self.assertEqual(entry["status"], "missed")

    def test_source_and_threshold_carried_through_verbatim(self):
        threshold = 4
        last_run = NOW - timedelta(hours=1)
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": last_run.isoformat(),
                    "source": ["fleet-session-hook:session-state", "custom:extra-signal"],
                    "threshold_hours": threshold,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        entry = next(a for a in doc["agents"] if a["agent_id"] == "claude-code")
        self.assertEqual(entry["source"], ["fleet-session-hook:session-state", "custom:extra-signal"])
        self.assertEqual(entry["threshold_hours"], threshold)


# ---------------------------------------------------------------------------
# null / missing last_run
# ---------------------------------------------------------------------------

class NullLastRunTests(BaseDeriveHeartbeatTest):
    def test_null_last_run_is_unknown(self):
        signals = {
            "agents": [
                {
                    "agent_id": "cowork",
                    "last_run": None,
                    "source": ["notion:35c6ecd8 last_edited_time"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        entry = next(a for a in doc["agents"] if a["agent_id"] == "cowork")
        self.assertEqual(entry["status"], "unknown")
        self.assertIsNone(entry["last_run"])


# ---------------------------------------------------------------------------
# Registry agent missing from signals
# ---------------------------------------------------------------------------

class RegistryAgentMissingFromSignalsTests(BaseDeriveHeartbeatTest):
    def test_registry_agent_absent_from_signals_is_unknown(self):
        # Only claude-code reported; cowork is a registered agent but has no
        # signal at all.
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": (NOW - timedelta(hours=1)).isoformat(),
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        agent_ids = {a["agent_id"] for a in doc["agents"]}
        self.assertIn("cowork", agent_ids)
        cowork_entry = next(a for a in doc["agents"] if a["agent_id"] == "cowork")
        self.assertEqual(cowork_entry["status"], "unknown")
        self.assertIsNone(cowork_entry["last_run"])
        # Source is carried from the registry's own heartbeat_source, never
        # invented.
        self.assertEqual(
            cowork_entry["source"],
            ["notion:35c6ecd8 last_edited_time", "automation-health-monitor:beacon-table"],
        )
        self.assertIsInstance(cowork_entry["threshold_hours"], (int, float))


# ---------------------------------------------------------------------------
# Inactive registry agent forced unknown
# ---------------------------------------------------------------------------

class InactiveAgentForcedUnknownTests(BaseDeriveHeartbeatTest):
    def test_inactive_agent_forced_unknown_even_with_fresh_signal(self):
        inactive_registry = _registry_with({"cowork": {"active": False}})
        self._registry_patcher.stop()
        self._registry_patcher = mock.patch.object(aaos, "load_registry", return_value=inactive_registry)
        self._registry_patcher.start()

        # cowork looks perfectly fresh per the raw signal math...
        signals = {
            "agents": [
                {
                    "agent_id": "cowork",
                    "last_run": (NOW - timedelta(minutes=5)).isoformat(),
                    "source": ["notion:35c6ecd8 last_edited_time"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        entry = next(a for a in doc["agents"] if a["agent_id"] == "cowork")
        # ...but is forced to "unknown" because the registry marks it inactive.
        self.assertEqual(entry["status"], "unknown")

    def test_inactive_agent_absent_from_signals_is_also_unknown(self):
        inactive_registry = _registry_with({"cowork": {"active": False}})
        self._registry_patcher.stop()
        self._registry_patcher = mock.patch.object(aaos, "load_registry", return_value=inactive_registry)
        self._registry_patcher.start()

        signals = {"agents": []}
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        entry = next(a for a in doc["agents"] if a["agent_id"] == "cowork")
        self.assertEqual(entry["status"], "unknown")


# ---------------------------------------------------------------------------
# Output validates against the schema
# ---------------------------------------------------------------------------

class SchemaValidityTests(BaseDeriveHeartbeatTest):
    def test_output_validates_against_heartbeat_schema(self):
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": (NOW - timedelta(hours=1)).isoformat(),
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        doc = json.loads(out.read_text(encoding="utf-8"))
        errors = aaos.validate_obj(doc, "heartbeat")
        self.assertEqual(errors, [])

    def test_schema_validation_failure_blocks_write(self):
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": (NOW - timedelta(hours=1)).isoformat(),
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        self.assertFalse(out.exists())
        with mock.patch.object(dh.aaos, "validate_obj", return_value=["SCHEMA ERROR: forced for test"]):
            code, output = run_dh(
                "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
                "--seam-root", str(self.root),
            )
        self.assertEqual(code, 1, output)
        self.assertFalse(out.exists())


# ---------------------------------------------------------------------------
# Malformed / unreadable signals -> exit 2
# ---------------------------------------------------------------------------

class MalformedSignalsTests(BaseDeriveHeartbeatTest):
    def test_nonexistent_signals_path_exits_2(self):
        missing = self.root / "does-not-exist.json"
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(missing), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(out.exists())

    def test_invalid_json_exits_2(self):
        path = self.root / "signals.json"
        path.write_text("{not valid json", encoding="utf-8")
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(out.exists())

    def test_missing_agents_key_exits_2(self):
        path = self._write_signals({"not_agents": []})
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(out.exists())

    def test_entry_missing_agent_id_exits_2(self):
        path = self._write_signals({"agents": [{"last_run": None, "source": ["x"], "threshold_hours": 1}]})
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(out.exists())

    def test_unparseable_last_run_exits_2(self):
        path = self._write_signals(
            {"agents": [{"agent_id": "claude-code", "last_run": "not-a-date", "source": ["x"], "threshold_hours": 1}]}
        )
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 2, output)
        self.assertFalse(out.exists())

    def test_stdin_signals_works(self):
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": (NOW - timedelta(hours=1)).isoformat(),
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": 16,
                }
            ]
        }
        out = self._out_path()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(signals))
        try:
            code, output = run_dh(
                "--signals", "-", "--out", str(out), "--now", NOW_ISO,
                "--seam-root", str(self.root),
            )
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0, output)
        self.assertTrue(out.exists())


# ---------------------------------------------------------------------------
# Atomic write — no .tmp-* residue
# ---------------------------------------------------------------------------

class AtomicWriteResidueTests(BaseDeriveHeartbeatTest):
    def test_output_lands_atomically_no_tmp_residue(self):
        signals = {
            "agents": [
                {
                    "agent_id": "claude-code",
                    "last_run": (NOW - timedelta(hours=1)).isoformat(),
                    "source": ["fleet-status.py:dual-fleet-health"],
                    "threshold_hours": 16,
                }
            ]
        }
        path = self._write_signals(signals)
        out = self._out_path()
        code, output = run_dh(
            "--signals", str(path), "--out", str(out), "--now", NOW_ISO,
            "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        leftovers = list(out.parent.glob(f"{out.name}.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_default_out_path_is_seam_heartbeat_json(self):
        signals = {"agents": []}
        path = self._write_signals(signals)
        code, output = run_dh(
            "--signals", str(path), "--now", NOW_ISO, "--seam-root", str(self.root),
        )
        self.assertEqual(code, 0, output)
        expected = aaos.heartbeat_path(root=str(self.root))
        self.assertTrue(expected.exists())
        self.assertIn(str(expected), output)


if __name__ == "__main__":
    unittest.main()
