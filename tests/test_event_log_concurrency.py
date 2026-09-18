"""
tests/test_event_log_concurrency.py — Finding 1 regression test (adversarial
Phase 3 review): the event-log append race that silently dropped ~52% of
events at 8 concurrent writers x 60 events (reviewer's probe).

Root cause (pre-fix): _atomic_append_ndjson's mtime guard only covers the
*read*; between the read and the os.replace, two writers can each build
"old-content + own-line" from the same snapshot, and the later os.replace
clobbers the earlier writer's line. The append "succeeds" (no exception),
so no spool fallback ever triggers -- the event is just gone.

This test reproduces the reviewer's probe: N threads x M events each, all
calling aaos.emit_event for the *same* project concurrently, released by a
threading.Barrier so they genuinely overlap. Real clock (not injected
--now) is deliberate: this race is about actual file-I/O interleaving
timing, which injected time can't substitute for. After every writer
finishes, the spool is drained to completion and the test asserts the
total unique event_ids landed in the NDJSON == N*M exactly -- zero lost,
zero duplicated.

Pre-fix (current _atomic_append_ndjson-based design): RED -- measurable
event loss.
Post-fix (spool-as-commit-point + drain-lock design, PHASE3-DESIGN.md
section 1.10 REVISED): GREEN -- zero loss, by construction: every event is
durably committed to a uniquely-named pending file before drain is ever
attempted, so a drain race can at worst leave events pending for a later
drain, never lose them.

Run standalone:
    python -m unittest tests.test_event_log_concurrency -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

PROJECT = "ora-ops"
N_THREADS = 8
M_EVENTS = 30  # 8 x 30 = 240 events total -- well above the reviewer's 8x60=480 loss threshold,
# kept a bit smaller here only to keep the in-process thread test fast; still reliably
# reproduces the race against the pre-fix implementation.


class EventLogConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-concurrency-")
        self.root = Path(self._tmpdir)
        for sub in ("tasks", "handoffs", "locks/_archive", "events"):
            (self.root / "projects" / PROJECT / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_concurrent_writers_lose_zero_events(self):
        barrier = threading.Barrier(N_THREADS)
        errors = []
        errors_lock = threading.Lock()

        def worker(worker_idx):
            barrier.wait()
            for i in range(M_EVENTS):
                try:
                    aaos.emit_event(
                        "TaskCreated", "claude-code", PROJECT,
                        refs={"task": f"t-w{worker_idx}-{i}"},
                        root=str(self.root),
                    )
                except Exception as exc:  # pragma: no cover - captured for assertion
                    with errors_lock:
                        errors.append(exc)

        # OI-270 tripwire. This whole test runs in seconds and the drain
        # lock only goes stale after _DRAIN_LOCK_STALE_MINUTES, so NO lock
        # can legitimately be judged stale here: every attempt to archive
        # the drain lock is a live lock being stolen, i.e. a second drainer
        # being let in. Counted at os.replace (not by globbing _archive/)
        # because on Windows the steal usually dies on a sharing violation
        # and leaves no file behind, while on Linux it always succeeds --
        # which is why the resulting event loss only ever showed up on CI.
        drain_lock = str(aaos._drain_lock_path(PROJECT, root=str(self.root)))
        steal_attempts = []
        real_replace = os.replace

        def counting_replace(src, dst, *args, **kwargs):
            if str(src) == drain_lock:
                steal_attempts.append(str(dst))
            return real_replace(src, dst, *args, **kwargs)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(N_THREADS)]
        with mock.patch.object(aaos.os, "replace", counting_replace):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        self.assertEqual(errors, [], f"worker exceptions: {errors!r}")

        # Drain to completion -- fold in anything still pending (may take
        # more than one call if this run's drain-lock contention left some
        # events pending for a later drainer).
        for _ in range(50):
            if aaos.drain_event_spool(PROJECT, root=str(self.root)) == 0:
                break

        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        lines = (
            [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if log_path.exists() else []
        )
        event_ids = set()
        task_refs = set()
        for line in lines:
            record = json.loads(line)
            event_ids.add(record["event_id"])
            task_refs.add(record["refs"]["task"])

        expected = N_THREADS * M_EVENTS
        expected_task_refs = {f"t-w{w}-{i}" for w in range(N_THREADS) for i in range(M_EVENTS)}

        # Where did any missing event go? Distinguishes folded-then-clobbered
        # (file sits in _drained/) from quarantined (_corrupt/) from destroyed.
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        missing = expected_task_refs - task_refs
        in_drained = {
            json.loads(p.read_text(encoding="utf-8"))["refs"]["task"]
            for p in (spool_dir / "_drained").glob("*.json")
        }
        n_corrupt = len(list((spool_dir / "_corrupt").glob("*.json")))
        where = (
            f"{len(missing)} events missing from the NDJSON: {len(missing & in_drained)} of them sit in "
            f"_drained/ (folded, then clobbered by a second drainer), {n_corrupt} in _corrupt/"
        )

        self.assertEqual(
            steal_attempts, [],
            f"a live drain lock was judged stale and stolen {len(steal_attempts)}x -- mutual "
            f"exclusion of the NDJSON fold is broken (OI-270). {where}",
        )
        self.assertEqual(
            len(lines), expected,
            f"expected {expected} NDJSON lines, got {len(lines)} "
            f"({expected - len(lines)} events lost to the append race). {where}",
        )
        self.assertEqual(
            len(event_ids), expected,
            f"expected {expected} unique event_ids, got {len(event_ids)} (collision or duplication)",
        )
        self.assertEqual(
            task_refs, expected_task_refs,
            f"missing task refs: {expected_task_refs - task_refs!r}",
        )

        # Nothing should be left un-drained afterward.
        remaining = list(spool_dir.glob("*.json")) if spool_dir.is_dir() else []
        self.assertEqual(remaining, [], f"pending events left undrained: {remaining!r}")


class DrainLockMutualExclusionTests(unittest.TestCase):
    """OI-270 root cause, forced deterministically instead of left to CI's
    scheduler. The drain lock is the ONLY thing making the NDJSON's
    read-modify-replace fold single-writer, so the invariant under test is:
    a lock that is not stale is never archived, and never yields a second
    holder. "I could not read the lock" is not evidence the lock is stale."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-drainlock-")
        self.root = Path(self._tmpdir)
        (self.root / "projects" / PROJECT / "events").mkdir(parents=True, exist_ok=True)
        self.lock_path = aaos._drain_lock_path(PROJECT, root=str(self.root))

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _archived_locks(self):
        archive_dir = aaos._events_archive_dir(PROJECT, root=str(self.root))
        return list(archive_dir.glob("drainlock.*")) if archive_dir.is_dir() else []

    def test_lock_released_mid_check_is_not_stale_and_third_drainers_lock_survives(self):
        # A holds a FRESH lock. B's exclusive create fails against it; A
        # then finishes and releases; B's read finds no file. C acquires a
        # fresh lock before B acts on its verdict.
        held_by_a = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held_by_a)

        real_read = aaos._read_drain_lock_acquired
        state = {}

        def a_releases_then_c_acquires(lock_path):
            aaos._release_drain_lock(held_by_a)
            try:
                return real_read(lock_path)
            finally:
                aaos._read_drain_lock_acquired = real_read
                state["c"] = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
                state["c_bytes"] = self.lock_path.read_bytes()

        aaos._read_drain_lock_acquired = a_releases_then_c_acquires
        try:
            held_by_b = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        finally:
            aaos._read_drain_lock_acquired = real_read

        self.assertIsNotNone(state.get("c"), "fixture sanity: C must win the vacated lock")
        self.assertIsNone(held_by_b, "B acquired the drain lock while C holds it -- two drainers at once")
        self.assertEqual(self._archived_locks(), [], "a live, seconds-old lock was archived as 'stale'")
        self.assertEqual(self.lock_path.read_bytes(), state["c_bytes"], "C's live lock was displaced")

    def test_unreadable_lock_fails_closed_never_stolen(self):
        # A transient read failure (Windows: sharing violation / delete-
        # pending while the holder releases) says nothing about the lock's
        # age. Fail closed: skip this drain, leave the lock alone.
        held = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held)
        held_bytes = self.lock_path.read_bytes()

        real_read_text = Path.read_text

        def deny_lock_reads(path_self, *args, **kwargs):
            if Path(path_self) == self.lock_path:
                raise PermissionError(13, "simulated sharing violation")
            return real_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny_lock_reads):
            second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))

        self.assertIsNone(second, "an unreadable live lock was treated as stale and stolen")
        self.assertEqual(self._archived_locks(), [])
        self.assertEqual(self.lock_path.read_bytes(), held_bytes)

    def test_lock_that_stays_unreadable_is_still_reclaimed_by_age(self):
        # Failing closed on an unreadable lock must not become a permanent
        # wedge: a crashed drainer's lock that STAYS unreadable is judged by
        # the file's own mtime instead, and reclaimed once that is stale.
        held = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held)
        long_ago = self.lock_path.stat().st_mtime - 60 * (aaos._DRAIN_LOCK_STALE_MINUTES + 5)
        os.utime(self.lock_path, (long_ago, long_ago))

        real_read_text = Path.read_text

        def deny_lock_reads(path_self, *args, **kwargs):
            if Path(path_self) == self.lock_path:
                raise PermissionError(13, "simulated persistent unreadability")
            return real_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny_lock_reads):
            second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))

        self.assertIsNotNone(second, "an aged unreadable lock wedged draining forever")
        self.assertEqual(len(self._archived_locks()), 1, "stale lock must be archived, not deleted")

    def _deny_lock_reads(self):
        real_read_text = Path.read_text

        def deny(path_self, *args, **kwargs):
            if Path(path_self) == self.lock_path:
                raise PermissionError(13, "simulated sharing violation")
            return real_read_text(path_self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", deny)

    def test_unreadable_fresh_lock_fails_closed_even_under_a_far_future_injected_now(self):
        # REFUTE finding F2: mtime is a real filesystem time, so the
        # fallback must never be measured against an injected `now`.
        held = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held)
        far_future = datetime.now(timezone.utc) + timedelta(days=30)
        with self._deny_lock_reads():
            second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root), now=far_future)
        self.assertIsNone(second, "an injected clock made a seconds-old unreadable lock look stale")
        self.assertEqual(self._archived_locks(), [])

    def test_unreadable_lock_released_before_stat_is_not_stale_and_c_survives(self):
        # Same race as the first test, entered through the fallback: the
        # read fails, the holder releases before the stat, C acquires.
        held_by_a = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held_by_a)
        real_stat = os.stat
        state = {}

        def a_releases_then_c_acquires(path, *args, **kwargs):
            if str(path) != str(self.lock_path) or "c" in state:
                return real_stat(path, *args, **kwargs)
            aaos._release_drain_lock(held_by_a)
            try:
                return real_stat(path, *args, **kwargs)
            finally:
                state["c"] = None  # re-entrancy guard before C's own acquisition stats anything
                with mock.patch.object(aaos.os, "stat", real_stat):
                    state["c"] = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
                state["c_bytes"] = self.lock_path.read_bytes()

        real_read_text = Path.read_text
        calls = {"n": 0}

        def deny_first_lock_read(path_self, *args, **kwargs):
            if Path(path_self) == self.lock_path:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "simulated sharing violation")
            return real_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny_first_lock_read),                 mock.patch.object(aaos.os, "stat", a_releases_then_c_acquires):
            held_by_b = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))

        self.assertIsNotNone(state.get("c"), "fixture sanity: C must win the vacated lock")
        self.assertIsNone(held_by_b, "B acquired the drain lock while C holds it")
        self.assertEqual(self._archived_locks(), [], "C's live lock was archived")
        self.assertEqual(self.lock_path.read_bytes(), state["c_bytes"])

    def test_lock_neither_readable_nor_statable_fails_closed_and_says_so(self):
        held = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(held)
        real_stat = os.stat

        def deny_lock_stat(path, *args, **kwargs):
            if str(path) == str(self.lock_path):
                raise PermissionError(13, "simulated stat failure")
            return real_stat(path, *args, **kwargs)

        stderr = io.StringIO()
        with self._deny_lock_reads(), mock.patch.object(aaos.os, "stat", deny_lock_stat),                 contextlib.redirect_stderr(stderr):
            second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))

        self.assertIsNone(second, "a lock whose age is unknowable was stolen")
        self.assertEqual(self._archived_locks(), [])
        self.assertIn("neither read nor stat", stderr.getvalue(), "a skipped drain of this kind must not be silent")

    def test_non_utf8_lock_content_is_stale_not_an_escaping_exception(self):
        # Read-but-unparseable stays "stale" (unchanged contract): with
        # _exclusive_publish no live lock can have partial content.
        aaos._exclusive_publish(self.lock_path, bytes([0xFF, 0xFE]) + b" not utf-8 " + bytes([0xFF]))
        second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(second, "a corrupt lock wedged draining")
        self.assertEqual(len(self._archived_locks()), 1, "corrupt lock must be archived, not deleted")

    def test_valid_utf8_but_invalid_json_lock_content_is_stale_immediately(self):
        # REFUTE M12: readable-and-valid-UTF-8-but-not-JSON (or JSON missing
        # `acquired`) must be judged stale on this read, not merely once its
        # mtime ages past _DRAIN_LOCK_STALE_MINUTES -- a live lock with this
        # content can never legitimately exist (_try_acquire_drain_lock only
        # ever publishes well-formed JSON), so there is nothing to protect.
        aaos._exclusive_publish(self.lock_path, b"not json at all")
        second = aaos._try_acquire_drain_lock(PROJECT, root=str(self.root))
        self.assertIsNotNone(second, "invalid-JSON lock content wedged draining")
        self.assertEqual(len(self._archived_locks()), 1, "invalid-JSON lock must be archived, not deleted")

    def test_pending_file_vanishing_under_a_held_lock_is_reported_loudly(self):
        # A pending file disappearing while we hold the exclusive lock is
        # the fingerprint of a second drainer. Never a silent skip.
        def ev(i):
            return {
                "protocol_version": "1.0", "event_id": f"e-v-{i}", "type": "TaskCreated",
                "actor": "claude-code", "timestamp": "2026-09-18T00:00:00+00:00",
                "project_id": PROJECT, "refs": {"task": f"t-v-{i}"},
            }

        doomed = aaos.spool_event(ev(1), PROJECT, root=str(self.root))
        aaos.spool_event(ev(2), PROJECT, root=str(self.root))
        real_read_text = Path.read_text

        def vanish(path_self, *args, **kwargs):
            if Path(path_self) == doomed and doomed.exists():
                doomed.unlink()
            return real_read_text(path_self, *args, **kwargs)

        stderr = io.StringIO()
        with mock.patch.object(Path, "read_text", vanish), contextlib.redirect_stderr(stderr):
            drained = aaos.drain_event_spool(PROJECT, root=str(self.root))

        self.assertEqual(drained, 1, "the surviving pending file must still be folded")
        self.assertIn("vanished while the drain lock was held", stderr.getvalue())
        self.assertIn("e-v-2", aaos.events_log_path(PROJECT, root=str(self.root)).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
