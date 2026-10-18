"""tests/test_windows_fs_semantics.py -- A0b characterization tests (plan section 11.2, items 1, 2, 4).

Audit findings: M32 (the os.link -> _exclusive_create fallback reopens the OI-264 zero-byte window,
untested) and M31 (partial: assert_lease_held decides from task.lease alone, never from the lock
file, which ADR-0006 D1 forbids).

Conventions, per plan section 11 (the repo runs `python -m unittest`, no pytest):
  * @known_defect (tests/_known_defect.py) documents a KNOWN DEFECT. If every pinned invariant holds
    the run FAILS, so the marker cannot silently outlive its fix; and unlike unittest.expectedFailure
    it does not swallow unrelated errors (a broken hook, a rename, a changed signature).
  * Plain tests are characterizations of CURRENT behaviour that must keep passing.
  * Everything runs in a temp tree. A guard patches DEFAULT_SEAM_ROOT to a sentinel inside the temp
    tree and fails the test if anything resolved (created) it; no test touches the live seam.

CI proves NTFS/Win32 (and Linux) semantics only. OneDrive guarantees stay UNVERIFIED until V1.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# `python -m unittest tests.test_windows_fs_semantics` must work from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401  (puts tools/ on sys.path)
import _aaos_common as aaos
from _known_defect import invariant, known_defect

PROJECT = "ora-ops"


class _SeamGuardedCase(unittest.TestCase):
    """Temp tree + a sentinel that proves the live default seam was never resolved."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-fs-sem-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.sentinel = self.tmp / "LIVE-DEFAULT-SENTINEL"
        patcher = mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(self.sentinel))
        patcher.start()
        self.addCleanup(patcher.stop)
        # seam_root() consults $AAOS_SEAM_ROOT before the default, so guard that route too.
        env_patcher = mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.sentinel)})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.addCleanup(self._assert_live_default_not_resolved)

    def _assert_live_default_not_resolved(self):
        self.assertFalse(self.sentinel.exists(), "a test resolved/created the live default seam root")


def _eperm(*_args, **_kwargs):
    raise OSError(errno.EPERM, "hardlinks refused (simulated)")


class ExclusiveLinkFallbackTests(_SeamGuardedCase):
    """M32. The fallback lives in _exclusive_link (tools/_aaos_common.py), not in _exclusive_publish."""

    def _staged_source(self, data: bytes) -> Path:
        src = self.tmp / "staged.sidecar"
        src.write_bytes(data)
        return src

    def test_hardlink_refused_falls_back_and_publishes_exact_bytes(self):
        data = b'{"holder":"a"}'
        src, dest = self._staged_source(data), self.tmp / "t.lock"
        with mock.patch.object(aaos.os, "link", _eperm):
            aaos._exclusive_link(src, dest, data)
        self.assertEqual(dest.read_bytes(), data)

    def test_collision_still_propagates_with_real_link(self):
        data = b"x"
        src, dest = self._staged_source(data), self.tmp / "t.lock"
        dest.write_bytes(b"incumbent")
        with self.assertRaises(FileExistsError):
            aaos._exclusive_link(src, dest, data)
        self.assertEqual(dest.read_bytes(), b"incumbent")

    def test_collision_propagates_through_the_fallback_too(self):
        # os.link refused, but dest already exists: _exclusive_create's O_EXCL must still say so.
        data = b"x"
        src, dest = self._staged_source(data), self.tmp / "t.lock"
        dest.write_bytes(b"incumbent")
        with mock.patch.object(aaos.os, "link", _eperm):
            with self.assertRaises(FileExistsError):
                aaos._exclusive_link(src, dest, data)
        self.assertEqual(dest.read_bytes(), b"incumbent")

    @known_defect("M32: the os.link fallback exposes an empty canonical file")
    def test_fallback_never_exposes_an_empty_canonical_file(self):
        """KNOWN DEFECT (M32 / OI-264): the fallback is O_EXCL create then a SEPARATE write, so the
        canonical name is visible with zero bytes in between. A deterministic hook (no polling reader
        thread, so it cannot flake) samples the destination at the instant of the write. The fix for
        M32 must make this pass; remove the marker with it."""
        data = b'{"holder":"a"}'
        src, dest = self._staged_source(data), self.tmp / "t.lock"
        real_write = os.write
        observed = []

        def sampling_write(fd, payload):
            try:
                if dest.exists():
                    observed.append(dest.stat().st_size)
            except OSError:
                pass
            return real_write(fd, payload)

        with mock.patch.object(aaos.os, "link", _eperm), mock.patch.object(aaos.os, "write", sampling_write):
            aaos._exclusive_link(src, dest, data)
        self.assertTrue(observed, "hook never fired: the test no longer reaches the create/write gap")
        invariant(0 not in observed, "canonical file was observable empty between create and write")


class ExclusivePublishGapTests(_SeamGuardedCase):
    def test_publish_has_no_fallback_oserror_propagates_and_temp_is_cleaned(self):
        """NEWLY DOCUMENTED GAP (plan 11.2): _exclusive_publish (drain lock) has no os.link fallback.
        On a filesystem that refuses hardlinks the drain lock can never be acquired. Decision on
        whether _exclusive_publish needs a fallback at all is deferred to the M32 fix (A1a)."""
        dest = self.tmp / "events" / "drain.lock"
        with mock.patch.object(aaos.os, "link", _eperm):
            with self.assertRaises(OSError) as ctx:
                aaos._exclusive_publish(dest, b"{}")
        self.assertNotIsInstance(ctx.exception, FileExistsError)
        self.assertFalse(dest.exists())
        self.assertEqual([p.name for p in dest.parent.iterdir()], [], "temp sidecar leaked")


class DrainLockReleaseRetryTests(_SeamGuardedCase):
    def test_release_retries_through_transient_sharing_violation(self):
        lock = self.tmp / "drain.lock"
        lock.write_text("{}", encoding="utf-8")
        real_unlink = os.unlink
        calls = {"n": 0}

        def flaky_unlink(path):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "used by another process (simulated)")
            return real_unlink(path)

        with mock.patch.object(aaos.os, "unlink", flaky_unlink), mock.patch.object(aaos.time, "sleep", lambda _s: None):
            aaos._release_drain_lock(lock)
        self.assertFalse(lock.exists())
        self.assertEqual(calls["n"], 3)


class AssertLeaseHeldCharacterization(_SeamGuardedCase):
    """M31 (partial). ADR-0006 D1 says lease ownership is decided from the LOCK file. Today
    assert_lease_held reads task.lease only and never opens a lock file (it cannot: it has no
    project id or root), so it returns normally while a lock names another holder. That is the
    defect, pinned as a @known_defect rather than as a passing test, so a green run does not read as
    endorsing it. A6b changes the signature: this test then raises TypeError (a real failure), which
    is the prompt to update the call and drop the decorator. The remainder of M31 (full ADR-0006
    verification suite, forced three-caller interleave) is carried by A6b's definition of done."""

    @known_defect("M31: assert_lease_held ignores the lock file (ADR-0006 D1)")
    def test_lock_file_naming_another_holder_must_fail_the_assertion(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        task = {
            "id": "t-lock-disagrees", "state": "leased",
            "lease": {"holder": "claude-code", "session": "sess-A",
                      "acquired": now.isoformat(), "expires": (now + timedelta(minutes=45)).isoformat()},
        }
        lock_dir = self.tmp / "projects" / PROJECT / "locks"
        lock_dir.mkdir(parents=True)
        (lock_dir / "t-lock-disagrees.lock").write_text(json.dumps({
            "task_ref": "t-lock-disagrees", "holder": "cowork", "session": "sess-B",
            "acquired": now.isoformat(), "expires": (now + timedelta(minutes=45)).isoformat(),
        }), encoding="utf-8")
        # Precondition: a fixed implementation that resolves the lock through the default seam (the
        # only route the current signature allows) MUST find the lock planted above. Point the default
        # resolution at this temp tree and prove it lands on the planted file; otherwise a correct fix
        # would read a different tree, see no lock, and the invariant below would hold or fail for the
        # wrong reason.
        with mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.tmp)}):
            planted = aaos.lock_path("t-lock-disagrees", PROJECT)
            self.assertEqual(planted, lock_dir / "t-lock-disagrees.lock")
            self.assertTrue(planted.exists(), "the planted lock is not where default resolution looks")
            raised = False
            try:
                aaos.assert_lease_held(task, "claude-code", "sess-A", now=now)
            except aaos.LeaseLostError:
                raised = True
        invariant(raised, "assert_lease_held returned normally although the lock file names another holder")


if __name__ == "__main__":
    unittest.main()
