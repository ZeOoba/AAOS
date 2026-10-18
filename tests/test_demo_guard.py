"""tests/test_demo_guard.py -- C0 / audit M52: executable demos under docs/ must not be able to
touch the live OneDrive seam by accident.

docs/_task-reports/phase4-dryrun-rehearsal.py writes and removes sentinel files inside whatever seam
it is pointed at. The refusal logic lives in docs/_task-reports/_demo_guard.py so it can be tested
IN-PROCESS against a decoy folder. No test here ever passes the real live seam path to the demo: a
regression in the guard must not be able to turn a test run into a write to the live seam.

Review findings addressed: the guard compared abspath+normcase only, so a junction/symlink/short-name
path to the live seam slipped past it; and the old refusal test executed the demo against the live path.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "docs" / "_task-reports"
DEMO = REPORTS_DIR / "phase4-dryrun-rehearsal.py"
sys.path.insert(0, str(REPORTS_DIR))

import _demo_guard  # noqa: E402


def _make_dir_link(link: Path, target: Path) -> bool:
    """Symlink, or a directory junction on Windows (no admin needed). False if neither works."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return proc.returncode == 0 and link.exists()
    return False


def _remove_link(link: Path) -> None:
    try:
        os.rmdir(str(link))  # removes a junction / directory symlink without touching its target
    except OSError:
        try:
            os.unlink(str(link))
        except OSError:
            pass


class DemoStaticGuard(unittest.TestCase):
    def setUp(self):
        self.src = DEMO.read_text(encoding="utf-8")

    def test_no_hardcoded_live_seam_literal(self):
        # Step 10 of the demo legitimately tests the *name* "Claude_Workspace" as scope logic, so only
        # the full live-path literal is banned.
        self.assertNotIn("OneDrive - Oxford", self.src)

    def test_no_hardcoded_repo_checkout_path(self):
        self.assertNotIn(r"C:\Users\Owner\AAOS", self.src)

    def test_requires_explicit_seam_root_and_imports_default(self):
        self.assertIn("--seam-root", self.src)
        self.assertIn("DEFAULT_SEAM_ROOT", self.src)

    def test_demo_wiring_is_exactly_one_resolve_seam_call_and_nothing_else_sets_SEAM(self):
        """Behavioural tests of resolve_seam() cannot see how the demo uses it, so pin the wiring: the
        one assignment is `SEAM = resolve_seam(sys.argv[1:], DEFAULT_SEAM_ROOT)`, it happens once, it
        precedes every path built under SEAM, and nothing swallows the refusal (mutation checks: a
        different live root, a later reassignment of SEAM, a try/except around the guard)."""
        assignments = re.findall(r"^SEAM\s*=.*$", self.src, flags=re.M)
        self.assertEqual(assignments, ["SEAM = resolve_seam(sys.argv[1:], DEFAULT_SEAM_ROOT)"])
        self.assertEqual(len(re.findall(r"\bSEAM\s*=(?!=)", self.src)), 1, "SEAM must be assigned exactly once")
        first_use = self.src.find("os.path.join(SEAM")
        self.assertGreater(first_use, 0)
        self.assertLess(self.src.find("SEAM = resolve_seam("), first_use)
        self.assertNotIn("except SystemExit", self.src)
        self.assertNotIn("except BaseException", self.src)
        self.assertNotRegex(self.src, r"refuse_live_seam\(")  # only resolve_seam() decides, in one place


class RefuseLiveSeam(unittest.TestCase):
    """The guard, in-process, with a decoy standing in for the live seam."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-guard-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.live = self.tmp / "live-decoy" / "AAOS"
        self.live.mkdir(parents=True)
        self.other = self.tmp / "copy" / "AAOS"
        self.other.mkdir(parents=True)

    def _refused(self, candidate):
        with self.assertRaises(SystemExit) as ctx:
            _demo_guard.refuse_live_seam(str(candidate), str(self.live))
        self.assertIn("REFUSED", str(ctx.exception.code))

    def test_the_live_root_itself_is_refused(self):
        self._refused(self.live)

    def test_spelling_variants_of_the_live_root_are_refused(self):
        self._refused(str(self.live) + os.sep)
        self._refused(self.live / ".." / "AAOS")
        self._refused(str(self.live).upper() if os.name == "nt" else self.live)

    def test_a_junction_or_symlink_to_the_live_root_is_refused(self):
        link = self.tmp / "alias"
        if not _make_dir_link(link, self.live):
            self.skipTest("cannot create a symlink or junction on this host")
        self.addCleanup(_remove_link, link)
        # Prove the test discriminates: the previous abspath+normcase comparison would have missed it.
        self.assertNotEqual(os.path.normcase(os.path.abspath(str(link))),
                            os.path.normcase(os.path.abspath(str(self.live))))
        self._refused(link)

    def test_a_different_directory_is_allowed(self):
        _demo_guard.refuse_live_seam(str(self.other), str(self.live))

    def test_nonexistent_paths_fall_back_to_a_path_comparison(self):
        ghost = self.tmp / "no-such" / "AAOS"
        with self.assertRaises(SystemExit):
            _demo_guard.refuse_live_seam(str(ghost), str(ghost))
        _demo_guard.refuse_live_seam(str(ghost), str(self.live))

    def test_real_default_root_constant_is_what_the_demo_compares_against(self):
        # Pure string check: nothing here touches the live seam.
        self.assertIn("DEFAULT_SEAM_ROOT", DEMO.read_text(encoding="utf-8"))
        self.assertTrue(aaos.DEFAULT_SEAM_ROOT)


class ResolveSeam(unittest.TestCase):
    """resolve_seam() in-process with a decoy standing in for the live seam: the demo's whole
    command-line behaviour, without ever running the demo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-resolve-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.live = self.tmp / "live-decoy"
        self.live.mkdir()
        self.copy = self.tmp / "copy"
        self.copy.mkdir()

    def test_missing_seam_root_exits_2_naming_the_flag(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            _demo_guard.resolve_seam([], str(self.live), environ={})
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--seam-root", err.getvalue())

    def test_live_root_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _demo_guard.resolve_seam(["--seam-root", str(self.live)], str(self.live), environ={})
        self.assertIn("REFUSED", str(ctx.exception.code))

    def test_a_copy_is_returned_unchanged(self):
        self.assertEqual(_demo_guard.resolve_seam(["--seam-root", str(self.copy)], str(self.live), environ={}),
                         str(self.copy))

    def test_seam_root_equal_to_the_env_seam_warns_but_is_allowed(self):
        err = io.StringIO()
        with redirect_stderr(err):
            got = _demo_guard.resolve_seam(["--seam-root", str(self.copy)], str(self.live),
                                           environ={"AAOS_SEAM_ROOT": str(self.copy)})
        self.assertEqual(got, str(self.copy))
        self.assertIn("AAOS_SEAM_ROOT", err.getvalue())

    def test_no_warning_when_env_points_elsewhere(self):
        err = io.StringIO()
        with redirect_stderr(err):
            _demo_guard.resolve_seam(["--seam-root", str(self.copy)], str(self.live),
                                     environ={"AAOS_SEAM_ROOT": str(self.live)})
        self.assertEqual(err.getvalue(), "")


class DemoRefusesWithoutSeamRoot(unittest.TestCase):
    def test_exits_nonzero_and_names_the_flag_before_doing_anything(self):
        proc = subprocess.run([sys.executable, str(DEMO)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--seam-root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
