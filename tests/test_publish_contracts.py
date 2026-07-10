"""
tests/test_publish_contracts.py — tests for tools/publish-contracts.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/publish-contracts.py does not exist yet, so every test here is
expected to fail (via a loader error from _tool_loader.load_tool -- the
target file does not exist) until the tool is implemented. See
docs/PHASE4-DESIGN.md section 4 for the full script design these tests
pin down.

Source-tree fixture / design choice, documented per the task's "your
choice, document it" latitude: publish-contracts.py's CLI deliberately
has no --repo-root flag (docs/PHASE4-DESIGN.md section 4: this script
publishes THIS repo, not an arbitrary tree -- that is a considered design
decision, not an oversight). To exercise it against fake/incomplete
source trees (a missing docs/COWORK-BOOTSTRAP.md, a malformed
agent-registry.json, a dirty git working tree), these tests monkeypatch
the loaded module's REPO_ROOT attribute before calling main(argv) --
every path the script touches is computed by re-reading the REPO_ROOT
module global at call time (never captured once at import time), so
reassigning module.REPO_ROOT before each call works. See
_FakeRepoTestBase below.

--source-commit is passed explicitly in every test that isn't
specifically exercising git derivation, to avoid needing a real
throwaway git repo per test (the documented choice offered by the task).
The git-dependent behaviors (dirty-tree "-dirty" stamping, clean-tree
no-suffix stamping, git-unavailable fail-closed) ARE exercised against
real throwaway git repos in GitDerivationTests below, since `git` is
confirmed available in this dev environment (git version 2.55.0).

Run with:  python -m unittest tests.test_publish_contracts -v
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tool_loader import load_tool  # noqa: E402

MODULE_FILENAME = "publish-contracts.py"

VALID_REGISTRY_FIXTURE = FIXTURES_DIR / "agent-registry.json"
CORRUPT_REGISTRY_FIXTURE = FIXTURES_DIR / "corrupt-registry.json"

REQUIRED_SCHEMA_FILES = [
    "agent-registry.schema.json",
    "event.schema.json",
    "handoff.schema.json",
    "heartbeat.schema.json",
    "task.schema.json",
]


def _build_fake_repo_tree(dest: Path, *, include_bootstrap=True, registry_src=VALID_REGISTRY_FIXTURE) -> None:
    """Populate `dest` with a minimal-but-complete AAOS source tree:
    schemas/*.schema.json (copied from the real repo -- schema DEFINITIONS
    aren't what's under test here, and validate_obj always loads schemas
    from _aaos_common.SCHEMA_DIR regardless of this script's own REPO_ROOT,
    so reusing the real ones is both convenient and correct),
    agent-registry.json (from the given fixture), docs/AAOS-SPEC.md
    (copied from the real repo), docs/COWORK-BOOTSTRAP.md (a small
    synthetic placeholder -- this file doesn't exist in the real repo yet,
    per PHASE4-DESIGN.md's sibling-task note), and templates/* (copied
    from the real repo).
    """
    (dest / "schemas").mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_SCHEMA_FILES:
        shutil.copyfile(REPO_ROOT / "schemas" / name, dest / "schemas" / name)

    shutil.copyfile(registry_src, dest / "agent-registry.json")

    (dest / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "docs" / "AAOS-SPEC.md", dest / "docs" / "AAOS-SPEC.md")
    if include_bootstrap:
        (dest / "docs" / "COWORK-BOOTSTRAP.md").write_text(
            "# Cowork Bootstrap (test fixture)\n\nSynthetic placeholder for tests.\n",
            encoding="utf-8",
        )

    templates_src = REPO_ROOT / "templates"
    templates_dest = dest / "templates"
    templates_dest.mkdir(parents=True, exist_ok=True)
    for f in templates_src.iterdir():
        if f.is_file():
            shutil.copyfile(f, templates_dest / f.name)


class _FakeRepoTestBase(unittest.TestCase):
    """Base fixture: a fake source tree (self.fake_repo) monkeypatched onto
    the loaded publish-contracts module's REPO_ROOT, plus a fresh temp seam
    root (self.seam_root). Subclasses may override `include_bootstrap` /
    `registry_src` class attrs to build an intentionally-broken tree."""

    include_bootstrap = True
    registry_src = VALID_REGISTRY_FIXTURE

    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)
        self._orig_repo_root = self.module.REPO_ROOT

        self._tmp = tempfile.mkdtemp(prefix="aaos-pc-test-")
        self.fake_repo = Path(self._tmp) / "fake-repo"
        self.seam_root = Path(self._tmp) / "seam"
        self.fake_repo.mkdir(parents=True, exist_ok=True)
        self.seam_root.mkdir(parents=True, exist_ok=True)

        _build_fake_repo_tree(
            self.fake_repo, include_bootstrap=self.include_bootstrap, registry_src=self.registry_src
        )

        self.module.REPO_ROOT = self.fake_repo

    def tearDown(self):
        self.module.REPO_ROOT = self._orig_repo_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def run_tool(self, *args):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = self.module.main(list(args))
        return code, buf_out.getvalue() + buf_err.getvalue()

    @property
    def contracts_dir(self) -> Path:
        return self.seam_root / "contracts"


class HappyPathTests(_FakeRepoTestBase):
    def test_exit_0_and_all_files_copied_byte_identical(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 0, out)

        for name in REQUIRED_SCHEMA_FILES:
            src = self.fake_repo / "schemas" / name
            dest = self.contracts_dir / "schemas" / name
            self.assertTrue(dest.exists(), f"{dest} missing")
            self.assertEqual(dest.read_bytes(), src.read_bytes(), f"{name} content mismatch")

        self.assertEqual(
            (self.contracts_dir / "agent-registry.json").read_bytes(),
            (self.fake_repo / "agent-registry.json").read_bytes(),
        )
        self.assertEqual(
            (self.contracts_dir / "AAOS-SPEC.md").read_bytes(),
            (self.fake_repo / "docs" / "AAOS-SPEC.md").read_bytes(),
        )
        self.assertEqual(
            (self.contracts_dir / "COWORK-BOOTSTRAP.md").read_bytes(),
            (self.fake_repo / "docs" / "COWORK-BOOTSTRAP.md").read_bytes(),
        )

    def test_templates_all_land_under_contracts_templates(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 0, out)

        src_names = sorted(p.name for p in (self.fake_repo / "templates").iterdir() if p.is_file())
        self.assertTrue(src_names, "fixture template dir unexpectedly empty")
        dest_dir = self.contracts_dir / "templates"
        dest_names = sorted(p.name for p in dest_dir.iterdir() if p.is_file())
        self.assertEqual(src_names, dest_names)
        for name in src_names:
            self.assertEqual(
                (dest_dir / name).read_bytes(),
                (self.fake_repo / "templates" / name).read_bytes(),
            )

    def test_published_json_shape(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 0, out)

        published = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))
        self.assertEqual(published["protocol_version"], "1.0")
        self.assertEqual(published["source_commit"], "deadbeef")
        self.assertIs(published["dirty"], False)
        self.assertIsInstance(published["generated_at"], str)
        datetime.fromisoformat(published["generated_at"])  # must parse as ISO-8601

    def test_idempotent_rerun_byte_identical_except_generated_at(self):
        code1, out1 = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code1, 0, out1)
        published_1 = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))

        time.sleep(0.05)

        code2, out2 = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code2, 0, out2)
        published_2 = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))

        self.assertNotEqual(published_1["generated_at"], published_2["generated_at"])
        self.assertEqual(published_1["source_commit"], published_2["source_commit"])
        self.assertEqual(published_1["dirty"], published_2["dirty"])
        self.assertEqual(published_1["protocol_version"], published_2["protocol_version"])

        for name in REQUIRED_SCHEMA_FILES:
            self.assertEqual(
                (self.contracts_dir / "schemas" / name).read_bytes(),
                (self.fake_repo / "schemas" / name).read_bytes(),
            )
        self.assertEqual(
            (self.contracts_dir / "agent-registry.json").read_bytes(),
            (self.fake_repo / "agent-registry.json").read_bytes(),
        )


class MidCopyFaultTests(_FakeRepoTestBase):
    """Phase 4 adversarial-review Finding 3 (Minor): a re-publish over an
    existing (stale) contracts/ that fails partway through the copy loop
    must NOT leave the stale PUBLISHED.json in place still signaling
    success -- Probe S (docs/COWORK-BOOTSTRAP.md) trusts PUBLISHED.json's
    mere presence unconditionally, so a stale-but-present stamp over a
    Frankenstein (mixed old/new) mirror would be silently trusted."""

    def test_mid_copy_failure_invalidates_stale_published_json(self):
        # First, a real successful publish -- establishes a "stale prior"
        # PUBLISHED.json plus stale prior file contents to detect leftover
        # staleness against, exactly like a real re-publish scenario.
        code, out = self.run_tool(
            "--seam-root", str(self.seam_root), "--source-commit", "STALE_PRIOR_COMMIT"
        )
        self.assertEqual(code, 0, out)
        prior_published = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))
        self.assertEqual(prior_published["source_commit"], "STALE_PRIOR_COMMIT")

        # Now change a source file (so a successful re-publish WOULD differ
        # from what's on the seam) and inject a failure partway through the
        # copy loop by making atomic_write raise on its 3rd call.
        (self.fake_repo / "agent-registry.json").write_text(
            (self.fake_repo / "agent-registry.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        original_atomic_write = self.module.aaos.atomic_write
        call_count = {"n": 0}

        def flaky_atomic_write(path, content):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise PermissionError("simulated mid-copy fault (Finding 3 regression test)")
            return original_atomic_write(path, content)

        self.module.aaos.atomic_write = flaky_atomic_write
        try:
            with self.assertRaises(PermissionError):
                self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "FRESH_COMMIT")
        finally:
            self.module.aaos.atomic_write = original_atomic_write

        # The fix under test: PUBLISHED.json must be gone, not stale-but-
        # present. A gone PUBLISHED.json is what makes Probe S correctly
        # fail closed (docs/COWORK-BOOTSTRAP.md §2/§3) instead of trusting a
        # mixed old/new mirror.
        self.assertFalse(
            (self.contracts_dir / "PUBLISHED.json").exists(),
            "PUBLISHED.json survived a mid-copy crash -- it would still tell "
            "Probe S the (possibly Frankenstein) mirror is trustworthy",
        )


class MissingSourceFileTests(_FakeRepoTestBase):
    include_bootstrap = False  # simulate this task running before the sibling task writes COWORK-BOOTSTRAP.md

    def test_missing_bootstrap_doc_exit_2_publishes_nothing(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 2, out)
        self.assertIn("COWORK-BOOTSTRAP.md", out)
        self.assertFalse((self.contracts_dir / "PUBLISHED.json").exists())
        # Fail-closed: nothing at all should have been copied, not even the
        # files that DID exist and would otherwise have been fine.
        self.assertFalse(self.contracts_dir.exists())

    def test_rerunning_after_file_appears_just_works(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 2, out)

        (self.fake_repo / "docs" / "COWORK-BOOTSTRAP.md").write_text(
            "# Cowork Bootstrap (now exists)\n", encoding="utf-8"
        )

        code2, out2 = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code2, 0, out2)
        self.assertTrue((self.contracts_dir / "PUBLISHED.json").exists())


class MalformedRegistryJsonTests(_FakeRepoTestBase):
    registry_src = CORRUPT_REGISTRY_FIXTURE

    def test_invalid_json_registry_exit_1_publishes_nothing(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 1, out)
        self.assertFalse((self.contracts_dir / "PUBLISHED.json").exists())
        self.assertFalse(self.contracts_dir.exists())


class SchemaInvalidRegistryTests(_FakeRepoTestBase):
    def setUp(self):
        super().setUp()
        # Overwrite the (already-copied, valid-JSON) registry with JSON
        # that parses fine but fails agent-registry.schema.json (missing
        # the required "agents" field).
        (self.fake_repo / "agent-registry.json").write_text(
            json.dumps({"protocol_version": "1.0"}), encoding="utf-8"
        )

    def test_schema_invalid_registry_exit_1_publishes_nothing(self):
        code, out = self.run_tool("--seam-root", str(self.seam_root), "--source-commit", "deadbeef")
        self.assertEqual(code, 1, out)
        self.assertFalse((self.contracts_dir / "PUBLISHED.json").exists())
        self.assertFalse(self.contracts_dir.exists())


class SourceCommitOverrideTests(_FakeRepoTestBase):
    def test_override_used_verbatim_no_git_call_no_dirty_suffix(self):
        # fake_repo here is NOT a git repo at all -- if the script made any
        # git call despite the override, that call would fail (not a git
        # repo) and this test would see a non-zero exit instead of 0.
        code, out = self.run_tool(
            "--seam-root", str(self.seam_root), "--source-commit", "my-custom-sha-value"
        )
        self.assertEqual(code, 0, out)
        published = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))
        self.assertEqual(published["source_commit"], "my-custom-sha-value")
        self.assertIs(published["dirty"], False)


class GitDerivationTests(unittest.TestCase):
    """Exercises real `git rev-parse HEAD` / `git status --porcelain`
    derivation against throwaway git repos -- git is confirmed available
    in this dev environment (git version 2.55.0.windows.2)."""

    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)
        self._orig_repo_root = self.module.REPO_ROOT
        self._tmp = tempfile.mkdtemp(prefix="aaos-pc-git-test-")
        self.seam_root = Path(self._tmp) / "seam"
        self.seam_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.module.REPO_ROOT = self._orig_repo_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def run_tool(self, *args):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = self.module.main(list(args))
        return code, buf_out.getvalue() + buf_err.getvalue()

    @property
    def contracts_dir(self) -> Path:
        return self.seam_root / "contracts"

    def _init_git_repo(self, repo_dir: Path) -> str:
        subprocess.run(["git", "init", "-q"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "config", "user.name", "AAOS Test"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo_dir), check=True)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def test_clean_tree_no_dirty_suffix(self):
        repo_dir = Path(self._tmp) / "clean-repo"
        repo_dir.mkdir()
        _build_fake_repo_tree(repo_dir)
        sha = self._init_git_repo(repo_dir)

        self.module.REPO_ROOT = repo_dir
        code, out = self.run_tool("--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)

        published = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))
        self.assertEqual(published["source_commit"], sha)
        self.assertIs(published["dirty"], False)

    def test_dirty_tree_stamps_dirty_suffix(self):
        repo_dir = Path(self._tmp) / "dirty-repo"
        repo_dir.mkdir()
        _build_fake_repo_tree(repo_dir)
        sha = self._init_git_repo(repo_dir)

        # Dirty the tree with an untracked file (git status --porcelain
        # reports untracked files too -- no need to modify a tracked one).
        (repo_dir / "UNTRACKED_NOTE.txt").write_text("dirtying the tree\n", encoding="utf-8")

        self.module.REPO_ROOT = repo_dir
        code, out = self.run_tool("--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)

        published = json.loads((self.contracts_dir / "PUBLISHED.json").read_text(encoding="utf-8"))
        self.assertEqual(published["source_commit"], f"{sha}-dirty")
        self.assertIs(published["dirty"], True)

    def test_git_unavailable_not_a_repo_exit_2_publishes_nothing(self):
        not_a_repo = Path(self._tmp) / "not-a-repo"
        not_a_repo.mkdir()
        _build_fake_repo_tree(not_a_repo)  # fully populated but NOT a git repo

        self.module.REPO_ROOT = not_a_repo
        code, out = self.run_tool("--seam-root", str(self.seam_root))
        self.assertEqual(code, 2, out)
        self.assertFalse(self.contracts_dir.exists())


if __name__ == "__main__":
    unittest.main()
