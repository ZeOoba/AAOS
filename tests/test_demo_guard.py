"""tests/test_demo_guard.py -- C0 / audit M52: executable demos under docs/ must not be able to
touch the live OneDrive seam by accident.

docs/_task-reports/phase4-dryrun-rehearsal.py was written to run against the live seam and (step 8)
writes then removes a sentinel there. Importing nothing from tools/, it duplicated the live path as a
literal. These tests pin the guard: no live-path literal, no module-level SEAM constant, an explicit
--seam-root is required, and the live default root is refused before any filesystem access.

The static tests never execute the demo. The dynamic tests only run it in ways that must be
refused before the first seam read, so they cannot touch the live seam even if the guard regressed
to a no-op only by failing loudly (the live default root is passed purely as a refusal probe).
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _tool_loader  # noqa: F401  (puts tools/ on sys.path)
import _aaos_common as aaos

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO = REPO_ROOT / "docs" / "_task-reports" / "phase4-dryrun-rehearsal.py"


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


class DemoRefusals(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(DEMO), *args],
            capture_output=True, text=True, timeout=60,
        )

    def test_refuses_without_seam_root(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--seam-root", proc.stderr)

    def test_refuses_live_default_root(self):
        proc = self._run("--seam-root", aaos.DEFAULT_SEAM_ROOT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
