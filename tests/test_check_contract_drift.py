"""
tests/test_check_contract_drift.py — tests for tools/check-contract-drift.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/check-contract-drift.py does not exist yet, so every test here fails
via a loader error from _tool_loader.load_tool until the tool is
implemented.

Purpose this tool serves (systematization candidate from the AAOS v1.0
build): Gate 4 and Gate 5's independent reviewers each had to manually
notice contracts/ provenance lag (Gate 5 Finding 4: PUBLISHED.json's
source_commit stamp lagged HEAD by two commits, content byte-identical,
only the stamp stale). This tool automates that check — and goes one step
further than a commit-stamp comparison alone by hash-comparing every
published file's actual bytes against what the git tree would produce, so
it also catches a hand-edit directly on the seam mirror (a Cowork
write-scope violation, since Cowork's write_scope never includes
contracts/) that a commit-only check would miss entirely.

Fixture pattern mirrors tests/test_publish_contracts.py: a fake source
repo tree monkeypatched onto the loaded module's REPO_ROOT, plus a fresh
temp seam root. --source-commit is used verbatim in most tests (same
documented choice as publish-contracts.py) to avoid needing a real git
repo per test; GitDerivationTests below exercises real `git` since it is
confirmed available in this dev environment.

Run with:  python -m unittest tests.test_check_contract_drift -v
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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tool_loader import load_tool  # noqa: E402

DRIFT_MODULE_FILENAME = "check-contract-drift.py"
PUBLISH_MODULE_FILENAME = "publish-contracts.py"

VALID_REGISTRY_FIXTURE = FIXTURES_DIR / "agent-registry.json"

REQUIRED_SCHEMA_FILES = [
    "agent-registry.schema.json",
    "event.schema.json",
    "handoff.schema.json",
    "heartbeat.schema.json",
    "task.schema.json",
]


def _build_fake_repo_tree(dest: Path, *, registry_src=VALID_REGISTRY_FIXTURE) -> None:
    (dest / "schemas").mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_SCHEMA_FILES:
        shutil.copyfile(REPO_ROOT / "schemas" / name, dest / "schemas" / name)

    shutil.copyfile(registry_src, dest / "agent-registry.json")

    (dest / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "docs" / "AAOS-SPEC.md", dest / "docs" / "AAOS-SPEC.md")
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


class _DriftTestBase(unittest.TestCase):
    """Base fixture: a fake source tree monkeypatched onto BOTH the
    publish-contracts module's REPO_ROOT and the check-contract-drift
    module's REPO_ROOT (they must agree — both are "this repo"), plus a
    fresh temp seam root. Provides self.publish(*args) to seed a real
    mirror via the actual publish-contracts.py tool (not a hand-rolled
    copy), so these tests exercise real cross-tool integration."""

    def setUp(self):
        self.publish_module = load_tool(PUBLISH_MODULE_FILENAME)
        self.drift_module = load_tool(DRIFT_MODULE_FILENAME)
        self._orig_publish_repo_root = self.publish_module.REPO_ROOT
        self._orig_drift_repo_root = self.drift_module.REPO_ROOT

        self._tmp = tempfile.mkdtemp(prefix="aaos-ccd-test-")
        self.fake_repo = Path(self._tmp) / "fake-repo"
        self.seam_root = Path(self._tmp) / "seam"
        self.fake_repo.mkdir(parents=True, exist_ok=True)
        self.seam_root.mkdir(parents=True, exist_ok=True)

        _build_fake_repo_tree(self.fake_repo)

        self.publish_module.REPO_ROOT = self.fake_repo
        self.drift_module.REPO_ROOT = self.fake_repo

    def tearDown(self):
        self.publish_module.REPO_ROOT = self._orig_publish_repo_root
        self.drift_module.REPO_ROOT = self._orig_drift_repo_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, module, *args):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = module.main(list(args))
        return code, buf_out.getvalue() + buf_err.getvalue()

    def publish(self, source_commit="deadbeef"):
        code, out = self._run(
            self.publish_module, "--seam-root", str(self.seam_root), "--source-commit", source_commit
        )
        self.assertEqual(code, 0, f"fixture setup: publish failed: {out}")
        return code, out

    def check_drift(self, source_commit="deadbeef", *extra_args):
        return self._run(
            self.drift_module, "--seam-root", str(self.seam_root), "--source-commit", source_commit, *extra_args
        )

    @property
    def contracts_dir(self) -> Path:
        return self.seam_root / "contracts"


class CleanMirrorTests(_DriftTestBase):
    def test_freshly_published_mirror_is_clean(self):
        self.publish(source_commit="deadbeef")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 0, out)
        self.assertIn("CLEAN", out)

    def test_clean_report_names_the_commit(self):
        self.publish(source_commit="deadbeef")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 0, out)
        self.assertIn("deadbeef", out)


class NotYetPublishedTests(_DriftTestBase):
    def test_no_contracts_dir_at_all_is_drift_not_crash(self):
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("PUBLISHED.json", out)

    def test_contracts_dir_exists_but_no_published_json(self):
        self.contracts_dir.mkdir(parents=True, exist_ok=True)
        (self.contracts_dir / "agent-registry.json").write_text("{}", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("PUBLISHED.json", out)


class MalformedPublishedJsonTests(_DriftTestBase):
    def test_corrupt_published_json_is_drift_not_crash(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "PUBLISHED.json").write_text("{not valid json", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("PUBLISHED.json", out)


class CommitDriftTests(_DriftTestBase):
    def test_stale_commit_stamp_detected(self):
        self.publish(source_commit="OLD_COMMIT_SHA")
        code, out = self.check_drift(source_commit="NEW_COMMIT_SHA")
        self.assertEqual(code, 1, out)
        self.assertIn("OLD_COMMIT_SHA", out)
        self.assertIn("NEW_COMMIT_SHA", out)

    def test_clean_commit_but_content_still_checked(self):
        # Same commit stamp, but the mirror content was hand-tampered --
        # commit match alone must not be treated as sufficient for CLEAN.
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "AAOS-SPEC.md").write_text("tampered content", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("AAOS-SPEC.md", out)


class ContentDriftTests(_DriftTestBase):
    def test_hand_edited_mirror_file_detected_even_with_matching_commit(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "schemas" / "task.schema.json").write_text(
            '{"tampered": true}', encoding="utf-8"
        )
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("task.schema.json", out)

    def test_missing_mirror_file_detected(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "agent-registry.json").unlink()
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("agent-registry.json", out)

    def test_extra_unexpected_file_in_mirror_detected(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "STRAY_FILE.txt").write_text("should not be here", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("STRAY_FILE.txt", out)

    def test_template_content_drift_detected(self):
        self.publish(source_commit="deadbeef")
        template_files = sorted(p for p in (self.contracts_dir / "templates").iterdir() if p.is_file())
        self.assertTrue(template_files, "fixture template dir unexpectedly empty")
        target = template_files[0]
        target.write_text("tampered template", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn(target.name, out)


class TypeMismatchTests(_DriftTestBase):
    """Adversarial-review Finding 1: a directory landing where a file is
    expected (either side) must be reported, never crash the tool."""

    def test_directory_in_place_of_mirror_file_is_drift_not_crash(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "agent-registry.json").unlink()
        (self.contracts_dir / "agent-registry.json").mkdir()
        (self.contracts_dir / "agent-registry.json" / "stray.txt").write_text("x", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("TYPE MISMATCH", out)
        self.assertIn("agent-registry.json", out)

    def test_directory_in_place_of_source_file_is_usage_error_not_crash(self):
        (self.fake_repo / "docs" / "AAOS-SPEC.md").unlink()
        (self.fake_repo / "docs" / "AAOS-SPEC.md").mkdir()
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 2, out)
        self.assertIn("AAOS-SPEC.md", out)


class MalformedPublishedJsonEncodingTests(_DriftTestBase):
    """Adversarial-review Finding 2 (non-UTF-8 bytes) and Finding 3
    (syntactically valid JSON, wrong top-level shape)."""

    def test_non_utf8_published_json_is_drift_not_crash(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "PUBLISHED.json").write_bytes(b"\xff\xfe\x00\x01not utf8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("PUBLISHED.json", out)

    def test_published_json_as_a_list_not_a_dict_is_drift_not_crash(self):
        self.publish(source_commit="deadbeef")
        (self.contracts_dir / "PUBLISHED.json").write_text("[1, 2, 3]", encoding="utf-8")
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 1, out)
        self.assertIn("source_commit", out)


class UsageErrorTests(_DriftTestBase):
    def test_unreachable_seam_root_exit_2(self):
        code, out = self._run(
            self.drift_module,
            "--seam-root", str(Path(self._tmp) / "does-not-exist"),
            "--source-commit", "deadbeef",
        )
        self.assertEqual(code, 2, out)

    def test_missing_required_source_file_exit_2(self):
        # A required repo-side file (used to compute the "current expected"
        # side of the comparison) is missing -- can't judge drift at all
        # without it, so this fails closed the same way publish-contracts.py
        # does for the identical precondition.
        (self.fake_repo / "docs" / "COWORK-BOOTSTRAP.md").unlink()
        code, out = self.check_drift(source_commit="deadbeef")
        self.assertEqual(code, 2, out)
        self.assertIn("COWORK-BOOTSTRAP.md", out)


class GitDerivationTests(unittest.TestCase):
    """Exercises real `git rev-parse HEAD` / `git status --porcelain`
    derivation against throwaway git repos, mirroring
    tests/test_publish_contracts.py's GitDerivationTests."""

    def setUp(self):
        self.publish_module = load_tool(PUBLISH_MODULE_FILENAME)
        self.drift_module = load_tool(DRIFT_MODULE_FILENAME)
        self._orig_publish_repo_root = self.publish_module.REPO_ROOT
        self._orig_drift_repo_root = self.drift_module.REPO_ROOT
        self._tmp = tempfile.mkdtemp(prefix="aaos-ccd-git-test-")
        self.seam_root = Path(self._tmp) / "seam"
        self.seam_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.publish_module.REPO_ROOT = self._orig_publish_repo_root
        self.drift_module.REPO_ROOT = self._orig_drift_repo_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, module, *args):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = module.main(list(args))
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

    def test_clean_tree_matches_after_publish(self):
        repo_dir = Path(self._tmp) / "clean-repo"
        repo_dir.mkdir()
        _build_fake_repo_tree(repo_dir)
        self._init_git_repo(repo_dir)

        self.publish_module.REPO_ROOT = repo_dir
        code, out = self._run(self.publish_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)

        self.drift_module.REPO_ROOT = repo_dir
        code, out = self._run(self.drift_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)
        self.assertIn("CLEAN", out)

    def test_new_commit_after_publish_is_drift(self):
        repo_dir = Path(self._tmp) / "advancing-repo"
        repo_dir.mkdir()
        _build_fake_repo_tree(repo_dir)
        old_sha = self._init_git_repo(repo_dir)

        self.publish_module.REPO_ROOT = repo_dir
        code, out = self._run(self.publish_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)

        (repo_dir / "docs" / "AAOS-SPEC.md").write_text("advanced content\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=str(repo_dir), check=True)

        self.drift_module.REPO_ROOT = repo_dir
        code, out = self._run(self.drift_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 1, out)
        self.assertIn(old_sha, out)

    def test_dirty_tree_after_clean_publish_is_drift(self):
        repo_dir = Path(self._tmp) / "dirtying-repo"
        repo_dir.mkdir()
        _build_fake_repo_tree(repo_dir)
        self._init_git_repo(repo_dir)

        self.publish_module.REPO_ROOT = repo_dir
        code, out = self._run(self.publish_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 0, out)

        (repo_dir / "UNTRACKED_NOTE.txt").write_text("dirtying\n", encoding="utf-8")

        self.drift_module.REPO_ROOT = repo_dir
        code, out = self._run(self.drift_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 1, out)
        self.assertIn("dirty", out.lower())

    def test_git_unavailable_not_a_repo_exit_2(self):
        not_a_repo = Path(self._tmp) / "not-a-repo"
        not_a_repo.mkdir()
        _build_fake_repo_tree(not_a_repo)

        self.drift_module.REPO_ROOT = not_a_repo
        code, out = self._run(self.drift_module, "--seam-root", str(self.seam_root))
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
