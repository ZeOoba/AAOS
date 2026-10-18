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

    def test_extreme_years_are_usage_errors_not_overflow(self):
        """Review finding: `lease-task --now 9999-12-31T23:30:00+00:00` died with OverflowError (exit 1)
        and `expire-leases --now 0001-01-01... --skew-min 525600` likewise."""
        for tool, argv in (("lease-task.py", MINIMAL_ARGV["lease-task.py"]), ("expire-leases.py", ["--dry-run"])):
            for bad in ("9999-12-31T23:30:00+00:00", "0001-01-01T00:00:00+00:00", "1969-12-31T23:59:59+00:00"):
                with self.subTest(tool=tool, now=bad):
                    code, out = self.invoke(tool, *argv, "--now", bad)
                    self.assertEqual(code, 2, out)
                    self.assertIn("USAGE ERROR", out)
                    self.assertIn("supported range", out)

    def test_year_range_boundaries_are_accepted(self):
        for ok in ("1970-01-01T00:00:00+00:00", "9000-12-31T23:59:59+00:00"):
            with self.subTest(now=ok):
                self.assertEqual(aaos.parse_cli_now(ok).isoformat(), ok)

    def test_aware_now_still_accepted_positive_control(self):
        code, out = self.invoke("expire-leases.py", "--dry-run", "--now", _iso(NOW))
        self.assertEqual(code, 0, out)


class _FromisoformatSites(ast.NodeVisitor):
    """Collect EVERY reference to `fromisoformat` with its qualified enclosing scope ('<module>' for
    module-level code, 'Class.method' for methods). References, not just calls: `fi = d.fromisoformat`,
    `map(d.fromisoformat, xs)` and `getattr(d, 'fromisoformat')` would evade a Call-only scan."""

    def __init__(self):
        self.scope = []
        self.sites = []

    def _qual(self):
        return ".".join(self.scope) or "<module>"

    def _function(self, node):
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)  # defaults and annotations belong to the OUTER scope
        if node.returns:
            self.visit(node.returns)
        self.scope.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node):
        for e in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(e)
        self.scope.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    def visit_Attribute(self, node):
        if node.attr == "fromisoformat":
            self.sites.append((self._qual(), node.lineno))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if node.value == "fromisoformat":  # getattr(d, 'fromisoformat'); docstrings are longer strings
            self.sites.append((self._qual(), node.lineno))


class NoDirectFromisoformatOutsideTheParser(unittest.TestCase):
    def _scan(self, source, filename="x.py"):
        visitor = _FromisoformatSites()
        visitor.visit(ast.parse(source, filename=filename))
        return visitor.sites

    def test_fromisoformat_only_referenced_inside__parse_iso_dt(self):
        found = []
        for path in sorted(TOOLS_DIR.glob("*.py")):
            for scope, _line in self._scan(path.read_text(encoding="utf-8"), str(path)):
                found.append((path.name, scope))
        self.assertEqual(sorted(found), [("_aaos_common.py", "_parse_iso_dt")])

    def test_scanner_has_no_blind_spots(self):
        cases = {
            "module-level call": ("x = datetime.fromisoformat('2026-01-01')\n", ["<module>"]),
            "alias then call": ("fi = datetime.fromisoformat\nfi('2026-01-01')\n", ["<module>"]),
            "passed as a callable": ("list(map(datetime.fromisoformat, xs))\n", ["<module>"]),
            "getattr by name": ("getattr(datetime, 'fromisoformat')('2026-01-01')\n", ["<module>"]),
            "nested helper": ("def _parse_iso_dt(v):\n    def helper(s):\n        return d.fromisoformat(s)\n"
                              "    return helper(v)\n", ["_parse_iso_dt.helper"]),
            "same-named method is not the allowed function": (
                "class C:\n    def _parse_iso_dt(self, v):\n        return d.fromisoformat(v)\n", ["C._parse_iso_dt"]),
            "default argument belongs to the outer scope": (
                "def f(parse=datetime.fromisoformat):\n    return parse\n", ["<module>"]),
        }
        for label, (source, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual([scope for scope, _ in self._scan(source)], expected)

    def test_docstring_mentions_are_not_references(self):
        self.assertEqual(self._scan('def f():\n    """uses fromisoformat internally"""\n'), [])


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

    def test_settle_must_be_shorter_than_the_lease_it_settles(self):
        """The lock's expiry is stamped before the settle sleep, so a settle >= the TTL leaves the
        holder asleep while its own lock is already reclaimable (review finding)."""
        lock = self._seed_queued()
        for ttl, settle in (("1", "60"), ("1", "61"), ("45", "2700"), ("45", "3000")):
            with self.subTest(ttl=ttl, settle=settle):
                code, out = self.invoke("lease-task.py", *self._lease_argv(
                    "t-bounds", "--ttl-min", ttl, "--settle-sec", settle))
                self.assertEqual(code, 2, out)
                self.assertIn("--settle-sec", out)
                self.assertIn("--ttl-min", out)
                self.assertFalse(lock.exists())

    def test_settle_just_under_the_ttl_is_allowed(self):
        self._seed_queued("t-just-under")
        code, out = self.invoke("lease-task.py", *self._lease_argv(
            "t-just-under", "--ttl-min", "1", "--settle-sec", "0.2"))
        self.assertEqual(code, 0, out)

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
