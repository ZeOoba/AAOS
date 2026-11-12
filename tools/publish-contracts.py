#!/usr/bin/env python3
"""
tools/publish-contracts.py — publish the AAOS contracts (schemas, spec,
registry, templates) from the git tree to the seam's read-only contracts/
mirror.

Usage:
    python tools/publish-contracts.py [--seam-root <dir>] [--source-commit <sha>]

See docs/PHASE4-DESIGN.md section 4 for the full design this implements.

What gets copied (git tree REPO_ROOT -> <seam_root>/contracts/), each file
via _aaos_common.atomic_write -- never written in place:
    schemas/*.schema.json (all 5)          -> contracts/schemas/<name>.schema.json
    agent-registry.json                    -> contracts/agent-registry.json
    docs/AAOS-SPEC.md                      -> contracts/AAOS-SPEC.md
    docs/COWORK-BOOTSTRAP.md               -> contracts/COWORK-BOOTSTRAP.md
    templates/*                            -> contracts/templates/*
    PUBLISHED.json (written LAST, only on full success)
                                            -> contracts/PUBLISHED.json

NOT published (docs/PHASE4-DESIGN.md section 4): CODE-ADAPTER.md (no
Cowork consumer), OPERATIONS.md (its by-hand shared-state procedures are
exactly what Cowork must never attempt -- keeping it git-only removes the
temptation), NON-GOALS.md/V2-BACKLOG.md/adr/*/MIGRATIONS.md (design
rationale, not turn-execution inputs).

source_commit derivation: --source-commit, if given, is used VERBATIM (no
git call is made at all, and "dirty" is stamped false since dirtiness was
never checked in that path). Otherwise `git rev-parse HEAD` and
`git status --porcelain` are run via subprocess with cwd=REPO_ROOT; a
non-empty porcelain output stamps "<hash>-dirty" / dirty=true, a clean
tree stamps "<hash>" / dirty=false. If git is unavailable, the tree isn't
a git repo, or either command fails, and no --source-commit override was
given, this fails closed: print a usage error and publish nothing
(exit 2) -- never fabricate an unknown provenance.

Pre-publish sanity: agent-registry.json is loaded (parsed ourselves, not
just checked-for-existence) and validated via
_aaos_common.validate_obj(..., "agent-registry") BEFORE any file is
copied; a malformed registry must never reach the seam (exit 1, nothing
published).

Every required source file's existence is checked BEFORE any file is
copied, so a single missing file (e.g. docs/COWORK-BOOTSTRAP.md, which may
not exist yet if this script runs ahead of the sibling task that writes
it -- a normal, expected precondition, not a bug) aborts with a usage
error and publishes nothing -- never a partial mirror. Re-running once the
file exists just works: no special-casing needed beyond "the file exists
now".

PUBLISHED.json is written LAST, strictly after every other file has been
copied successfully, so its mere presence in contracts/ is itself the
"publish succeeded" signal a reader (Probe S / Probe G in
docs/PHASE4-DESIGN.md section 1.2) can trust.

Testing note: this script's source tree is always the module-level
REPO_ROOT below (a deliberate design decision -- there is no --repo-root
CLI flag; this script publishes THIS repo, not an arbitrary tree). Tests
that need to exercise the script against a fake/incomplete source tree do
so by monkeypatching this module's REPO_ROOT attribute
(module.REPO_ROOT = <fake tree>) before calling main(argv) -- every path
below is computed by reading the REPO_ROOT global at call time, never
captured once at import time, so this works. See
tests/test_publish_contracts.py's _FakeRepoTestBase.

Exit codes:
    0   success
    1   validation failure (agent-registry.json is malformed JSON or fails
        agent-registry.schema.json)
    2   usage error (git unavailable/failed and no --source-commit
        override given; a required source file is missing; --seam-root is
        unusable)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
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
        prog="publish-contracts.py",
        description="Publish the AAOS contracts (schemas, spec, registry, "
        "templates) from the git tree to the seam's contracts/ mirror.",
    )
    parser.add_argument(
        "--seam-root", default=None,
        help="Seam root override (default: AAOS_SEAM_ROOT env or "
        "_aaos_common.DEFAULT_SEAM_ROOT).",
    )
    parser.add_argument(
        "--source-commit", default=None,
        help="Use this commit sha verbatim instead of deriving it via git "
        "(no git call is made; 'dirty' is stamped false).",
    )
    return parser


def _derive_source_commit(source_commit_arg: str | None, repo_root: Path) -> tuple[str, bool]:
    """Returns (stamped_source_commit, dirty). Raises aaos.UsageError if
    derivation is required (no override given) and fails at any step."""
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


def _load_and_validate_registry(registry_src: Path) -> dict:
    """Load agent-registry.json's JSON content ourselves (not just a bare
    existence/parse check) and validate it against agent-registry.schema.json.

    Raises aaos.UsageError if the file is missing/unreadable (a
    missing-required-source-file usage error, exit 2 -- same class as any
    other absent required file). Raises aaos.CorruptStateError if it's
    unparseable JSON or fails schema validation (a validation failure,
    exit 1 -- distinct from a missing file)."""
    if not registry_src.exists():
        raise aaos.UsageError(f"missing required source file: {registry_src}")
    try:
        raw = registry_src.read_text(encoding="utf-8")
    except OSError as exc:
        raise aaos.UsageError(f"could not read {registry_src}: {exc}") from exc
    try:
        registry_obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise aaos.CorruptStateError(f"{registry_src} is not valid JSON: {exc}") from exc

    errors = aaos.validate_obj(registry_obj, "agent-registry")
    if errors:
        raise aaos.CorruptStateError(
            f"{registry_src} failed agent-registry schema validation: {errors}"
        )
    return registry_obj


def _build_copy_plan(repo_root: Path, contracts_dir: Path) -> list[tuple[Path, Path]]:
    """Returns [(src, dest), ...] for every non-PUBLISHED.json file this
    script copies. Does not check existence -- callers verify every src
    exists before copying anything (fail closed, publish nothing)."""
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
        for tf in sorted(p for p in templates_dir.iterdir() if p.is_file()):
            plan.append((tf, contracts_dir / "templates" / tf.name))
    else:
        # templates/ itself missing is a missing-required-source-file
        # condition; folding it into `plan` lets the single existence-check
        # pass below catch it uniformly with everything else.
        plan.append((templates_dir, contracts_dir / "templates"))

    return plan


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = REPO_ROOT  # re-read the module global at call time (tests monkeypatch it)

    try:
        seam_root = aaos.seam_root(args.seam_root)
    except Exception as exc:
        print(f"USAGE ERROR: bad --seam-root: {exc}", file=sys.stderr)
        return 2

    if seam_root.exists() and not seam_root.is_dir():
        print(
            f"USAGE ERROR: bad --seam-root: {seam_root} exists and is not a directory",
            file=sys.stderr,
        )
        return 2

    try:
        contracts_dir = aaos.guard_path(seam_root / "contracts", "contracts dir")
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        source_commit, dirty = _derive_source_commit(args.source_commit, repo_root)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        _load_and_validate_registry(repo_root / "agent-registry.json")
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2
    except aaos.CorruptStateError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1

    plan = _build_copy_plan(repo_root, contracts_dir)

    missing = [src for src, _dest in plan if not src.exists()]
    if missing:
        for src in missing:
            print(f"USAGE ERROR: missing required source file: {src}", file=sys.stderr)
        return 2

    # Invalidate any prior success signal BEFORE copying, not after: if a
    # write fails partway through the loop below, a stale-but-still-present
    # PUBLISHED.json would otherwise keep telling readers (Probe S) that a
    # (now Frankenstein, mixed old/new) mirror is trustworthy. Deleting it
    # first means a crash mid-publish always leaves NO success signal --
    # Probe S correctly fails closed instead of trusting stale bytes.
    published_path = contracts_dir / "PUBLISHED.json"
    if published_path.exists():
        published_path.unlink()

    for src, dest in plan:
        aaos.atomic_write(dest, src.read_bytes())

    published = {
        "protocol_version": "1.0",
        "source_commit": source_commit,
        "generated_at": datetime.now().astimezone().isoformat(),
        "dirty": dirty,
    }
    aaos.atomic_write(
        contracts_dir / "PUBLISHED.json",
        json.dumps(published, indent=2, ensure_ascii=False) + "\n",
    )

    print(
        f"PUBLISHED: {contracts_dir} ({len(plan)} file(s) + PUBLISHED.json; "
        f"source_commit={source_commit}, dirty={dirty})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
