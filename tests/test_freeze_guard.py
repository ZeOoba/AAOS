"""
tests/test_freeze_guard.py -- the in-code freeze sentinel (docs/adr/0007-freeze.md, addendum
2026-11-12; audit finding M-no-in-code-freeze-guard).

While AAOS is frozen, seam_root() / require_seam_reachable() must refuse the hard-coded live seam
(LIVE_SEAM_ROOT) unless AAOS_UNFREEZE=1, by every route that reaches it: the no-override fallback,
AAOS_SEAM_ROOT, an explicit --seam-root, a spelling variant, or a junction/symlink alias. Explicit
override roots that are NOT the live seam (the per-test tmp seams the whole suite uses) keep working.

No test here touches the live seam. Refusals of the literal live path are decided lexically before
any filesystem call; the alias test points LIVE_SEAM_ROOT at a decoy folder.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

REAL_LIVE = aaos.LIVE_SEAM_ROOT if hasattr(aaos, "LIVE_SEAM_ROOT") else aaos.DEFAULT_SEAM_ROOT


def _env_without(*names):
    env = {k: v for k, v in os.environ.items() if k not in names}
    return env


class FreezeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-freeze-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        # Default state for every test: frozen, no env seam.
        patcher = mock.patch.dict(os.environ, _env_without("AAOS_UNFREEZE", "AAOS_SEAM_ROOT"), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_frozen_refusal(self, fn):
        with self.assertRaises(aaos.UsageError) as ctx:
            fn()
        msg = str(ctx.exception)
        self.assertIn("FROZEN", msg)
        self.assertIn("0007", msg)
        self.assertIn("AAOS_UNFREEZE", msg)

    def test_live_constant_is_the_hard_coded_onedrive_seam(self):
        self.assertEqual(aaos.LIVE_SEAM_ROOT, aaos.DEFAULT_SEAM_ROOT)
        self.assertIn("Claude_Workspace/AAOS", aaos.LIVE_SEAM_ROOT)

    def test_fallback_to_live_default_is_refused(self):
        self._assert_frozen_refusal(lambda: aaos.seam_root())

    def test_require_seam_reachable_refuses_live_default(self):
        self._assert_frozen_refusal(lambda: aaos.require_seam_reachable())

    def test_explicit_override_equal_to_live_is_refused(self):
        self._assert_frozen_refusal(lambda: aaos.seam_root(REAL_LIVE))

    def test_env_var_pointing_at_live_is_refused(self):
        with mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": REAL_LIVE}):
            self._assert_frozen_refusal(lambda: aaos.seam_root())

    def test_spelling_variants_of_live_are_refused(self):
        variants = [REAL_LIVE + "/", REAL_LIVE.replace("/", "\\"), REAL_LIVE + "/projects/.."]
        if os.name == "nt":
            variants.append(REAL_LIVE.upper())
        for v in variants:
            with self.subTest(variant=v):
                self._assert_frozen_refusal(lambda v=v: aaos.seam_root(v))

    def test_path_builders_inherit_the_refusal(self):
        self._assert_frozen_refusal(lambda: aaos.project_dir("ora-ops"))
        self._assert_frozen_refusal(lambda: aaos.heartbeat_path())

    def test_unfreeze_sentinel_allows_live_default(self):
        with mock.patch.dict(os.environ, {"AAOS_UNFREEZE": "1"}):
            self.assertEqual(aaos.seam_root(), Path(aaos.DEFAULT_SEAM_ROOT))

    def test_unfreeze_must_be_exactly_1(self):
        for val in ("", "0", "true", "yes", " 1"):
            with self.subTest(val=val):
                with mock.patch.dict(os.environ, {"AAOS_UNFREEZE": val}):
                    self._assert_frozen_refusal(lambda: aaos.seam_root())

    def test_explicit_tmp_override_is_allowed(self):
        self.assertEqual(aaos.seam_root(str(self.tmp)), Path(str(self.tmp)))
        self.assertEqual(aaos.require_seam_reachable(str(self.tmp)), Path(str(self.tmp)))

    def test_env_override_elsewhere_is_allowed(self):
        with mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.tmp)}):
            self.assertEqual(aaos.seam_root(), Path(str(self.tmp)))
            self.assertEqual(aaos.require_seam_reachable(), Path(str(self.tmp)))

    def test_alias_to_live_is_refused(self):
        """A junction/symlink leading to the live seam is the same directory. LIVE_SEAM_ROOT is
        patched to a decoy so the real live seam is never touched."""
        decoy = self.tmp / "live-decoy"
        decoy.mkdir()
        link = self.tmp / "alias"
        try:
            os.symlink(str(decoy), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("cannot create a symlink on this host")
            proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(decoy)], capture_output=True)
            if proc.returncode != 0 or not link.exists():
                self.skipTest("cannot create a symlink or junction on this host")
        self.addCleanup(lambda: (os.rmdir(str(link)) if link.exists() else None))
        with mock.patch.object(aaos, "LIVE_SEAM_ROOT", str(decoy)):
            self._assert_frozen_refusal(lambda: aaos.seam_root(str(link)))
            self._assert_frozen_refusal(lambda: aaos.seam_root(str(decoy)))

    def test_patched_default_sentinel_is_not_mistaken_for_live(self):
        """Other suites patch DEFAULT_SEAM_ROOT to an in-tmp sentinel; that must not be refused."""
        sentinel = self.tmp / "sentinel"
        with mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(sentinel)):
            self.assertEqual(aaos.seam_root(), Path(str(sentinel)))


class FreezeGuardCliTests(unittest.TestCase):
    """Every seam CLI fails closed (exit 2, FROZEN on stderr) when it would fall back to the live
    seam. Run IN-PROCESS with both DEFAULT_SEAM_ROOT and LIVE_SEAM_ROOT patched to a decoy inside a
    tmp dir: a regression in the guard can then at worst touch the decoy, never the real seam."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-freeze-cli-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.decoy = self.tmp / "live-decoy"
        self.decoy.mkdir()
        for p in (
            mock.patch.dict(os.environ, _env_without("AAOS_UNFREEZE", "AAOS_SEAM_ROOT",
                                                     "AAOS_NOTION_TOKEN", "NOTION_TOKEN"), clear=True),
            mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(self.decoy)),
            mock.patch.object(aaos, "LIVE_SEAM_ROOT", str(self.decoy)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_cli_refuses_live_default(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        signals = self.tmp / "signals.json"
        signals.write_text('{"agents": []}', encoding="utf-8")
        cases = [
            ("create-task.py", "--id", "t-synthetic-freeze", "--title", "synthetic", "--accept", "x"),
            ("expire-leases.py", "--dry-run"),
            ("derive-heartbeat.py", "--signals", str(signals), "--now", "2026-11-12T00:00:00+00:00"),
            ("project-notion.py",),
            ("check-contract-drift.py",),
            ("publish-contracts.py",),
            ("publish-notion.py",),
        ]
        for name, *argv in cases:
            with self.subTest(tool=name):
                module = _tool_loader.load_tool(name)
                err, out = io.StringIO(), io.StringIO()
                with redirect_stderr(err), redirect_stdout(out), mock.patch("urllib.request.urlopen") as net:
                    code = module.main(list(argv))
                self.assertEqual(code, 2, out.getvalue() + err.getvalue())
                # Some CLIs report usage errors on stdout (pre-existing behaviour), so check both.
                self.assertIn("FROZEN", out.getvalue() + err.getvalue())
                net.assert_not_called()
        self.assertEqual(list(self.decoy.iterdir()), [], "nothing may be written under the live seam")


if __name__ == "__main__":
    unittest.main()
