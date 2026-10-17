"""tests/test_cli_input_bounds.py -- A0.5 (plan section 11.3): CLI input validators and tz-aware --now.

Audit findings: M03 (lease-task / expire-leases accept zero or negative --ttl-min, --skew-min,
--settle-sec; a negative --skew-min lets a contender reclaim a LIVE foreign lease) and M43 (behaviour
part only: seven copy-pasted _resolve_now() helpers accept a NAIVE --now, which is then compared
against timezone-aware datetimes; helper dedup itself stays deferred).

Only INVALID values are rejected. The ratified 30s settle and 5-minute skew stay defaults, never
floors (ballot Q4-A), so `--settle-sec 0` and `--skew-min 0` remain valid and the test suite's own use
of them keeps working. All runs use a per-test temp seam (ScriptTestBase sets AAOS_SEAM_ROOT).
"""
from __future__ import annotations

import ast
import contextlib
import io
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Make `python -m unittest tests.test_cli_input_bounds` work from the repo root, as the older test
# modules do: the sibling helpers (_tool_loader, test_queue_scripts) live in tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401  (puts tools/ on sys.path)
import _aaos_common as aaos
from _tool_loader import load_tool
from test_queue_scripts import NOW, PROJECT, ScriptTestBase, _iso, _live_lease

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
NAIVE_NOW = "2026-07-09T12:00:00"  # no offset: ambiguous timezone

# Minimal argv that gets each CLI past argparse so --now resolution is what is actually tested.
MINIMAL_ARGV = {
    "lease-task.py": ["t-x", "--holder", "h", "--session", "s"],
    "expire-leases.py": [],
    "complete-task.py": ["t-x", "--holder", "h", "--session", "s"],
    "create-task.py": ["--title", "T", "--slug", "x"],
    "emit-event.py": ["--drain-only"],
    "fail-task.py": ["t-x", "--holder", "h", "--session", "s", "--reason", "r"],
    "post-handoff.py": ["--id", "h-x", "--task", "t-x", "--from", "a", "--to", "b", "--next-action", "n"],
}


class _Invoker(ScriptTestBase):
    def invoke(self, tool, *args):
        """Like run_tool, but also captures argparse's SystemExit so exit 2 from a validator is visible."""
        module = load_tool(tool)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()


class ParseCliNowTests(unittest.TestCase):
    def test_empty_means_now_and_is_timezone_aware(self):
        got = aaos.parse_cli_now("")
        self.assertIsNotNone(got.tzinfo)
        self.assertIsNotNone(aaos.parse_cli_now(None).tzinfo)

    def test_aware_value_is_returned_unchanged(self):
        self.assertEqual(aaos.parse_cli_now("2026-07-09T12:00:00-04:00").utcoffset(), timedelta(hours=-4))
        self.assertEqual(aaos.parse_cli_now("2026-07-09T16:00:00+00:00").utcoffset(), timedelta(0))

    def test_naive_value_is_a_usage_error_naming_the_flag(self):
        with self.assertRaises(aaos.UsageError) as ctx:
            aaos.parse_cli_now(NAIVE_NOW)
        self.assertIn("--now", str(ctx.exception))

    def test_garbage_is_a_usage_error_not_corrupt_state(self):
        with self.assertRaises(aaos.UsageError):
            aaos.parse_cli_now("not-a-date")


class NaiveNowRejectedByEveryCli(_Invoker):
    def test_naive_now_exits_2_with_usage_error_for_all_seven_clis(self):
        for tool, argv in MINIMAL_ARGV.items():
            with self.subTest(tool=tool):
                code, out = self.invoke(tool, *argv, "--now", NAIVE_NOW)
                self.assertEqual(code, 2, out)
                self.assertIn("USAGE ERROR", out)
                self.assertIn("--now", out)

    def test_aware_now_still_accepted_positive_control(self):
        code, out = self.invoke("expire-leases.py", "--dry-run", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)


class _FromisoformatSites(ast.NodeVisitor):
    """Collect every `<x>.fromisoformat(...)` call with its innermost enclosing function name
    ('<module>' for module-level code, so a bare module-level call cannot hide)."""

    def __init__(self):
        self.scope = ["<module>"]
        self.sites = []

    def _enter(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "fromisoformat":
            self.sites.append((self.scope[-1], node.lineno))
        self.generic_visit(node)


class NoDirectFromisoformatOutsideTheParser(unittest.TestCase):
    def _scan(self, source, filename="x.py"):
        visitor = _FromisoformatSites()
        visitor.visit(ast.parse(source, filename=filename))
        return visitor.sites

    def test_fromisoformat_only_called_inside__parse_iso_dt(self):
        found = []
        for path in sorted(TOOLS_DIR.glob("*.py")):
            for fn, _line in self._scan(path.read_text(encoding="utf-8"), str(path)):
                found.append((path.name, fn))
        self.assertEqual(sorted(found), [("_aaos_common.py", "_parse_iso_dt")])

    def test_scanner_sees_module_level_and_nested_calls(self):
        """The guard must not have blind spots: module-level calls and calls inside a nested helper
        are reported against the right scope."""
        module_level = "from datetime import datetime\nx = datetime.fromisoformat('2026-01-01')\n"
        self.assertEqual([fn for fn, _ in self._scan(module_level)], ["<module>"])
        nested = ("def _parse_iso_dt(v):\n    def helper(s):\n        return datetime.fromisoformat(s)\n"
                  "    return helper(v)\n")
        self.assertEqual([fn for fn, _ in self._scan(nested)], ["helper"])


class LeaseTaskFlagBounds(_Invoker):
    def _seed_queued(self, task_id="t-bounds"):
        self.seed_task(task_id, state="queued")
        return aaos.lock_path(task_id, PROJECT, root=str(self.root))

    def _lease_argv(self, task_id, *extra):
        return (task_id, "--holder", "claude-code", "--session", "sess-new", "--now", _iso(NOW), *extra)

    def test_zero_and_negative_ttl_rejected_before_any_side_effect(self):
        lock = self._seed_queued()
        for bad in ("0", "-1"):
            with self.subTest(ttl=bad):
                code, out = self.invoke("lease-task.py", *self._lease_argv("t-bounds", "--ttl-min", bad))
                self.assertEqual(code, 2, out)
                self.assertIn("--ttl-min", out)
                self.assertFalse(lock.exists())
                self.assertEqual(self.reload_task("t-bounds")["state"], "queued")

    def test_negative_nan_inf_settle_rejected(self):
        lock = self._seed_queued()
        for bad in ("-1", "nan", "inf"):
            with self.subTest(settle=bad):
                code, out = self.invoke("lease-task.py", *self._lease_argv("t-bounds", "--settle-sec", bad))
                self.assertEqual(code, 2, out)
                self.assertIn("--settle-sec", out)
                self.assertFalse(lock.exists())

    def test_negative_skew_cannot_reclaim_a_live_foreign_lease(self):
        """M03 attack: a lease with 3 minutes left is LIVE under the default 5-minute skew, but with
        --skew-min -5 it reads as expired and a contender reclaims it. The flag must be rejected and
        the incumbent's task and lock left byte-for-byte unchanged."""
        task_id = "t-live-foreign"
        lease = _live_lease(holder="cowork", session="owner-sess", now=NOW - timedelta(minutes=42), ttl_min=45)
        self.seed_task(task_id, state="leased", lease=lease)
        lock_file = aaos.lock_path(task_id, PROJECT, root=str(self.root))
        aaos.save_json(lock_file, {**lease, "task_ref": task_id, "protocol_version": "1.0"})
        task_file = aaos.task_path(task_id, PROJECT, root=str(self.root))
        before = (lock_file.read_bytes(), task_file.read_bytes())

        code, out = self.invoke("lease-task.py", *self._lease_argv(task_id, "--skew-min", "-5", "--settle-sec", "0"))

        self.assertEqual(code, 2, out)
        self.assertIn("--skew-min", out)
        self.assertEqual((lock_file.read_bytes(), task_file.read_bytes()), before)

    def test_positive_controls_zero_settle_zero_skew_and_defaults_still_work(self):
        self._seed_queued("t-ok-1")
        code, out = self.invoke("lease-task.py", *self._lease_argv("t-ok-1", "--settle-sec", "0", "--skew-min", "0"))
        self.assertEqual(code, 0, out)
        self._seed_queued("t-ok-2")
        code, out = self.invoke("lease-task.py", *self._lease_argv("t-ok-2", "--settle-sec", "0"))
        self.assertEqual(code, 0, out)

    def test_absurdly_large_values_are_usage_errors_not_overflow_or_year_long_sleeps(self):
        """Review finding: with only lower bounds, `--ttl-min 99999999999999` died with an
        OverflowError (exit 1 'UNEXPECTED ERROR'), `--settle-sec 1e19` likewise, and a finite
        `--settle-sec 1e9` slept for decades while holding the task's lock. These are sanity ceilings
        (MAX_CLI_MINUTES, MAX_CLI_SETTLE_SEC), not the ADR-0006 D3 lease cap."""
        lock = self._seed_queued()
        too_big = [
            ("--ttl-min", "99999999999999"), ("--ttl-min", str(aaos.MAX_CLI_MINUTES + 1)),
            ("--skew-min", "99999999999999"), ("--skew-min", str(aaos.MAX_CLI_MINUTES + 1)),
            ("--settle-sec", "1e9"), ("--settle-sec", "1e19"),
            ("--settle-sec", str(aaos.MAX_CLI_SETTLE_SEC + 1)),
        ]
        for flag, value in too_big:
            with self.subTest(flag=flag, value=value):
                extra = [flag, value] if flag == "--settle-sec" else [flag, value, "--settle-sec", "0"]
                code, out = self.invoke("lease-task.py", *self._lease_argv("t-bounds", *extra))
                self.assertEqual(code, 2, out)
                self.assertIn(flag, out)
                self.assertFalse(lock.exists())
                self.assertEqual(self.reload_task("t-bounds")["state"], "queued")

    def test_the_ceilings_themselves_are_accepted(self):
        self._seed_queued("t-edge")
        code, out = self.invoke("lease-task.py", *self._lease_argv(
            "t-edge", "--ttl-min", str(aaos.MAX_CLI_MINUTES), "--skew-min", str(aaos.MAX_CLI_MINUTES),
            "--settle-sec", "0"))
        self.assertEqual(code, 0, out)
        self.assertEqual(aaos.finite_nonneg_float(str(aaos.MAX_CLI_SETTLE_SEC)), aaos.MAX_CLI_SETTLE_SEC)


class ExpireLeasesFlagBounds(_Invoker):
    def test_negative_skew_rejected(self):
        code, out = self.invoke("expire-leases.py", "--dry-run", "--skew-min", "-1", "--now", _iso(NOW))
        self.assertEqual(code, 2, out)
        self.assertIn("--skew-min", out)

    def test_absurdly_large_skew_rejected(self):
        for value in ("99999999999999", str(aaos.MAX_CLI_MINUTES + 1)):
            with self.subTest(value=value):
                code, out = self.invoke("expire-leases.py", "--dry-run", "--skew-min", value, "--now", _iso(NOW))
                self.assertEqual(code, 2, out)
                self.assertIn("--skew-min", out)

    def test_zero_skew_still_valid(self):
        code, out = self.invoke("expire-leases.py", "--dry-run", "--skew-min", "0", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
