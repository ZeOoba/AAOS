"""Shared refusal guard for historical demos that write sentinel files into a seam (audit M52).

Kept in its own importable module so it can be unit-tested in-process against a decoy folder; the
demos themselves are hyphenated, straight-line scripts that cannot be imported safely.

The comparison resolves links: a directory junction, symlink, mapped/subst drive or 8.3 short name
that leads to the live seam is the SAME directory and must be refused. `os.path.samefile` answers that
when both paths exist; otherwise fall back to a realpath + case-folded comparison.
"""
from __future__ import annotations

import os


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
