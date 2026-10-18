"""Shared refusal guard for historical demos that write sentinel files into a seam (audit M52).

Kept in its own importable module so it can be unit-tested in-process against a decoy folder; the
demos themselves are hyphenated, straight-line scripts that cannot be imported safely.

The comparison resolves links: a directory junction, symlink, mapped/subst drive or 8.3 short name
that leads to the live seam is the SAME directory and must be refused. `os.path.samefile` answers that
when both paths exist; otherwise fall back to a realpath + case-folded comparison.
"""
from __future__ import annotations

import argparse
import os
import sys


def same_dir(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def refuse_live_seam(seam_root: str, live_root: str) -> None:
    """Exit with a REFUSED message if `seam_root` is (or resolves to) `live_root`."""
    if same_dir(seam_root, live_root):
        raise SystemExit(
            "REFUSED: --seam-root is (or resolves to) the live default seam root; this rehearsal writes "
            "sentinels. Pass a copy of the seam tree instead."
        )


def resolve_seam(argv, live_root: str, environ=None) -> str:
    """Parse the demo's command line and return the seam root to use, or exit.

    `--seam-root` is mandatory (argparse exits 2 without it) and must not be, or resolve to, the live
    root. The demo calls this once, before any path under the seam is built, so the wiring is testable
    in-process with a decoy `live_root`.

    If `--seam-root` equals `$AAOS_SEAM_ROOT` a warning is printed instead of refusing: that variable is
    how the tools select a seam, so it may legitimately point at the COPY being rehearsed (the test
    suite does exactly that), and a live seam at a non-default location cannot be told apart from it."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Phase-4 dry-run rehearsal (historical; needs a seam COPY).")
    parser.add_argument("--seam-root", required=True, help="root of a COPY of the seam tree (never the live seam)")
    args = parser.parse_args(argv)
    refuse_live_seam(args.seam_root, live_root)
    env_root = environ.get("AAOS_SEAM_ROOT")
    if env_root and same_dir(args.seam_root, env_root):
        print(
            "WARNING: --seam-root equals $AAOS_SEAM_ROOT, the seam the tools use in this shell. This "
            "rehearsal writes and removes sentinel files there; make sure it is a COPY, not the live seam.",
            file=sys.stderr,
        )
    return args.seam_root
