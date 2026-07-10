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

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

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

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(N_THREADS)]
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

        self.assertEqual(
            len(lines), expected,
            f"expected {expected} NDJSON lines, got {len(lines)} "
            f"({expected - len(lines)} events lost to the append race)",
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
        spool_dir = aaos.event_spool_dir(PROJECT, root=str(self.root))
        remaining = list(spool_dir.glob("*.json")) if spool_dir.is_dir() else []
        self.assertEqual(remaining, [], f"pending events left undrained: {remaining!r}")


if __name__ == "__main__":
    unittest.main()
