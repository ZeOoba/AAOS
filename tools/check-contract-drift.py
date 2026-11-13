#!/usr/bin/env python3
"""
tools/check-contract-drift.py — read-only guard: has the seam's contracts/
mirror drifted from the git tree that published it?

Usage:
    python tools/check-contract-drift.py [--seam-root <dir>] [--source-commit <sha>]

Systematization candidate from the AAOS v1.0 build: Gate 4 and Gate 5's
independent reviewers each had to manually notice contracts/ provenance
lag (Gate 5 Finding 4: PUBLISHED.json's source_commit stamp lagged HEAD by
two commits — content byte-identical, only the stamp stale). This tool
automates that check, and adds a second, independent detection axis a
commit-stamp comparison alone would miss entirely: it hash-compares every
published file's actual bytes against what the git tree would currently
produce, so a hand-edit directly on the seam mirror (a write-scope
violation — no agent-registry.json entry's write_scope includes
contracts/, per docs/COWORK-BOOTSTRAP.md's read-only framing) is caught
even when the commit stamp still matches.

This tool never writes anything — it is purely diagnostic. To fix drift it
reports, re-run tools/publish-contracts.py.

What "current expected state" means: the same thing tools/publish-contracts.py
would compute right now if run against REPO_ROOT — same copy plan (5
schemas, agent-registry.json, docs/AAOS-SPEC.md, docs/COWORK-BOOTSTRAP.md,
templates/*), same git rev-parse HEAD / git status --porcelain derivation
(see _derive_source_commit below, deliberately duplicated from
publish-contracts.py rather than cross-imported: tools/*.py files are
hyphenated CLI scripts that only ever share logic through _aaos_common,
per this repo's existing convention — see tests/_tool_loader.py's
docstring). "Mirror state" means whatever currently sits under
<seam_root>/contracts/.

Testing note: like publish-contracts.py, this script's source tree is
always the module-level REPO_ROOT below (no --repo-root flag — this
script checks THIS repo against the seam, not an arbitrary tree). Tests
monkeypatch module.REPO_ROOT before calling main(argv); see
tests/test_check_contract_drift.py.

Findings reported (any one of these means DRIFT, exit 1):
  - contracts/PUBLISHED.json missing (mirror never published, or the
    contracts/ directory doesn't exist at all)
  - contracts/PUBLISHED.json present but not valid JSON, or missing its
    required "source_commit" key
  - PUBLISHED.json's source_commit does not exactly match the source
    commit currently derivable from the git tree (including a mismatched
    "-dirty" suffix)
  - a file the copy plan expects is missing from contracts/
  - a file present in contracts/ has different content (sha256 mismatch)
    from what the git tree currently holds
  - a file exists under contracts/ that the copy plan does not expect
    (PUBLISHED.json itself is exempt from this check)

Exit codes:
    0   CLEAN — mirror matches the git tree exactly, no findings
    1   DRIFT DETECTED — one or more findings above; see stdout for the
        itemized list
    2   USAGE ERROR — seam root unreachable, --seam-root bad, git
        unavailable/failed and no --source-commit override given, or a
        required repo-side source file is missing (can't determine the
        "current expected" side of the comparison at all)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import _aaos_common as aaos

REPO_ROOT = aaos.REPO_ROOT  # tests monkeypatch this attribute; see module docstring

REQUIRED_SCHEMA_FILES = [
    "agent-registry.schema.json",
    "event.schema.json",
    "handoff.schema.json",
    "heartbeat.schema.json",
    "task.schema.json",
]

REQUIRED_DOC_FILES = [
    "AAOS-SPEC.md",
    "COWORK-BOOTSTRAP.md",
]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-contract-drift.py",
        description="Read-only guard: compare the seam's contracts/ mirror "
        "against the git tree that should have published it.",
    )
    parser.add_argument(
        "--seam-root", default=None,
        help="Seam root override (default: AAOS_SEAM_ROOT env or "
        "_aaos_common.DEFAULT_SEAM_ROOT).",
    )
    parser.add_argument(
        "--source-commit", default=None,
        help="Use this commit sha verbatim as the 'current expected' "
        "value instead of deriving it via git (no git call is made; "
        "treated as a clean, non-dirty tree). Same escape hatch as "
        "publish-contracts.py --source-commit, for testing.",
    )
    return parser


def _derive_source_commit(source_commit_arg: str | None, repo_root: Path) -> tuple[str, bool]:
    """Returns (stamped_source_commit, dirty). Raises aaos.UsageError if
    derivation is required (no override given) and fails at any step.
    Deliberately identical logic to publish-contracts.py's
    _derive_source_commit — see module docstring for why this is
    duplicated rather than imported."""
    if source_commit_arg:
        return source_commit_arg, False

    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True,
        )
    except OSError as exc:
        raise aaos.UsageError(
            f"git is unavailable ({exc}) and no --source-commit override was given"
        ) from exc

    if rev.returncode != 0:
        raise aaos.UsageError(
            "git rev-parse HEAD failed (source tree unreadable or not a git "
            f"repo?) and no --source-commit override was given: {rev.stderr.strip()}"
        )
    sha = rev.stdout.strip()
    if not sha:
        raise aaos.UsageError(
            "git rev-parse HEAD returned empty output and no --source-commit "
            "override was given"
        )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if status.returncode != 0:
        raise aaos.UsageError(
            "git status --porcelain failed and no --source-commit override "
            f"was given: {status.stderr.strip()}"
        )
    dirty = bool(status.stdout.strip())
    stamped = f"{sha}-dirty" if dirty else sha
    return stamped, dirty


def _build_copy_plan(repo_root: Path, contracts_dir: Path) -> list[tuple[Path, Path]]:
    """Same plan publish-contracts.py's _build_copy_plan produces (minus
    PUBLISHED.json, which is compared separately). Does not check
    existence -- caller verifies every src exists before comparing
    anything (fail closed: can't judge drift without a source to compare
    against)."""
    schema_dir = repo_root / "schemas"
    docs_dir = repo_root / "docs"
    templates_dir = repo_root / "templates"

    plan: list[tuple[Path, Path]] = []
    for name in REQUIRED_SCHEMA_FILES:
        plan.append((schema_dir / name, contracts_dir / "schemas" / name))

    plan.append((repo_root / "agent-registry.json", contracts_dir / "agent-registry.json"))

    for name in REQUIRED_DOC_FILES:
        plan.append((docs_dir / name, contracts_dir / name))

    if templates_dir.is_dir():
        for tf in sorted(p for p in aaos.g_iterdir(templates_dir) if p.is_file()):
            plan.append((tf, contracts_dir / "templates" / tf.name))
    else:
        plan.append((templates_dir, contracts_dir / "templates"))

    return plan


def _sha256(path: Path) -> str:
    return hashlib.sha256(aaos.g_read_bytes(path)).hexdigest()


@aaos.freeze_exit
def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = REPO_ROOT  # re-read the module global at call time (tests monkeypatch it)

    try:
        seam_root = aaos.require_seam_reachable(args.seam_root)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        contracts_dir = aaos.guard_path(seam_root / "contracts", "contracts dir")
        if contracts_dir.is_dir():
            aaos.g_walk(contracts_dir)  # screen every mirror entry up front: a junction anywhere in it refuses the run
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        current_commit, current_dirty = _derive_source_commit(args.source_commit, repo_root)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    plan = _build_copy_plan(repo_root, contracts_dir)
    # is_file() (not exists()) so a source-side path that's a directory --
    # not just an absent one -- also fails closed here: we can't compute
    # the "current expected" side of the comparison from a directory
    # either, so it's the same class of usage error (adversarial-review
    # Finding 1).
    missing_sources = [src for src, _dest in plan if not src.is_file()]
    if missing_sources:
        for src in missing_sources:
            print(f"USAGE ERROR: missing required source file: {src}", file=sys.stderr)
        return 2

    published_path = contracts_dir / "PUBLISHED.json"
    if not published_path.exists():
        print(
            f"DRIFT DETECTED: {published_path} does not exist — contracts/ "
            "mirror has never been published, or the seam's contracts "
            "directory is gone. Run tools/publish-contracts.py."
        )
        return 1

    try:
        published_text = aaos.g_read_text(published_path)
    except (OSError, UnicodeDecodeError) as exc:
        # Finding 2 (adversarial review): a non-UTF-8 PUBLISHED.json (e.g.
        # a OneDrive conflict artifact, a botched manual edit) previously
        # raised UnicodeDecodeError uncaught. That's exactly the kind of
        # mirror corruption this tool exists to report as DRIFT, not crash on.
        print(f"DRIFT DETECTED: {published_path} could not be read as UTF-8: {exc}")
        return 1

    try:
        published = json.loads(published_text)
    except json.JSONDecodeError as exc:
        print(f"DRIFT DETECTED: {published_path} is not valid JSON: {exc}")
        return 1

    if not isinstance(published, dict) or "source_commit" not in published:
        print(
            f"DRIFT DETECTED: {published_path} is missing its required "
            "'source_commit' key — mirror provenance is untrustworthy."
        )
        return 1

    findings: list[str] = []

    published_commit = published.get("source_commit")
    if published_commit != current_commit:
        findings.append(
            f"COMMIT DRIFT: mirror stamped source_commit={published_commit!r}, "
            f"current tree is {current_commit!r} "
            f"(dirty={current_dirty}) — run tools/publish-contracts.py"
        )

    expected_dests = {dest for _src, dest in plan}
    expected_dests.add(published_path)

    for src, dest in plan:
        if not dest.exists():
            findings.append(f"MISSING IN MIRROR: {dest} (expected from {src})")
            continue
        if not dest.is_file():
            # Finding 1 (adversarial review): dest existed but wasn't a
            # regular file (e.g. a directory landed at that path -- a
            # realistic OneDrive partial-sync/conflict shape, not just
            # contrived tampering). _sha256 would previously crash here
            # uncaught; report it as a finding instead.
            findings.append(
                f"TYPE MISMATCH IN MIRROR: {dest} exists but is not a regular "
                f"file (expected a file matching {src})"
            )
            continue
        src_hash, dest_hash = _sha256(src), _sha256(dest)
        if src_hash != dest_hash:
            findings.append(
                f"CONTENT DRIFT: {dest} sha256={dest_hash[:12]}... does not "
                f"match git tree {src} sha256={src_hash[:12]}..."
            )

    if contracts_dir.is_dir():
        for path in sorted(aaos.g_walk(contracts_dir)):
            if path.is_file() and path not in expected_dests:
                findings.append(f"UNEXPECTED FILE IN MIRROR: {path}")

    if not findings:
        print(
            f"CLEAN: {contracts_dir} matches the git tree exactly "
            f"(source_commit={current_commit}, {len(plan)} file(s) verified)"
        )
        return 0

    print(f"DRIFT DETECTED: {len(findings)} finding(s) in {contracts_dir}")
    for finding in findings:
        print(f"  - {finding}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
