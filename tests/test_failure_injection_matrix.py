"""
tests/test_failure_injection_matrix.py — AAOS Phase 5 failure-injection
matrix: one test class per scenario (Scenario01 .. Scenario10), matching
the master prompt's own numbering 1:1 (docs/2026-07-09_AAOS_MasterPrompt_v02.md,
"PHASE 5 — VALIDATION & HARDENING" / "Failure-injection matrix (ten
scenarios; each scripted, executed, evidenced)") so the mapping between
this file and the master prompt is unambiguous.

See docs/PHASE5-DESIGN.md for the full coverage inventory (which of the
ten were already proven by existing tests vs. genuinely new) and the full
design for each of the four new scenarios (#2, #4, #8, #10).

Six scenarios (#1, #3, #5, #6, #7, #9) are already COVERED by existing,
already-green tests written in Phases 2-4. Per the design, those six get a
SHORT test here: a citation (exact existing test class/method named in the
docstring) plus a fresh confirming re-run, invoked via unittest's own
test-loading machinery (unittest.TestLoader + a private TestSuite/
TextTestRunner) so the real pass/fail output is captured as evidence --
these are NOT reimplementations and must not duplicate the real coverage.
Scenario #6 additionally builds its own protocol_version: "0.9" fixture
and runs it through tools/validate.py directly, per the design's explicit
instruction, so this file reads as a self-contained evidence report even
though the mechanism was proven elsewhere first.

Four scenarios (#2, #4, #8, #10) are genuinely NEW scripted scenarios,
built exactly as designed in PHASE5-DESIGN.md's "New scenario designs"
section. Their underlying mechanisms already exist from Phase 3 -- the
work here is proving each one explicitly with a dedicated scenario, not
writing new production code, EXCEPT where the rehearsal itself surfaced a
real gap:

  Scenario #4 (seam unavailable) surfaced a REAL GAP: create-task.py,
  post-handoff.py (no --blocker), and emit-event.py did not fail closed
  when pointed at a --seam-root that does not exist -- they silently
  auto-vivified a brand-new, empty parallel seam tree via atomic_write's
  own os.makedirs(path.parent, exist_ok=True) and SUCCEEDED. Fixed via
  tools/_aaos_common.py::require_seam_reachable(), now called as the
  first line of every seam-touching CLI script's main(). See
  docs/_task-reports/phase5-failure-matrix-report.md scenario #4 for the
  full before/after empirical evidence.

Run with:  python -m unittest tests.test_failure_injection_matrix -v
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
TESTS_DIR = Path(__file__).resolve().parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)
import validate  # noqa: E402  (tools/validate.py; scenarios #2, #6, #8, #10 drive it directly)
from _tool_loader import load_tool  # noqa: E402

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PROJECT = "ora-ops"


def _iso(dt):
    return dt.isoformat()


def _make_task(task_id, project_id=PROJECT, *, state="queued", lease=None, now=NOW, **overrides):
    task = {
        "protocol_version": "1.0",
        "id": task_id,
        "project_id": project_id,
        "title": f"Test task {task_id}",
        "owner_agent": "unassigned",
        "state": state,
        "priority": 2,
        "inputs": [],
        "acceptance_criteria": ["it works"],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    if lease is not None:
        task["lease"] = lease
    task.update(overrides)
    return task


def _run_existing(*loadables, verbosity=2):
    """Run one or more existing, already-green TestCase classes (or
    (TestCase, method_name) pairs, to select a single method out of a
    larger class) via unittest's OWN test-loading machinery -- this is
    literally "invoke them via unittest's test-loading machinery" per
    PHASE5-DESIGN.md's citation instruction for the six COVERED scenarios,
    not a hand-rolled reimplementation of what they check.

    Returns (result, output_text) -- output_text is the real `-v` output
    from a fresh TextTestRunner/TestResult, suitable for pasting as pass
    evidence in the Gate 5 report. Never touches the outer discovery run's
    own TestResult -- this is a fully isolated nested run.
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for item in loadables:
        if isinstance(item, tuple):
            case_cls, method_name = item
            suite.addTest(case_cls(method_name))
        else:
            suite.addTest(loader.loadTestsFromTestCase(item))
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=verbosity)
    result = runner.run(suite)
    return result, stream.getvalue()


class BaseFailureMatrixTest(unittest.TestCase):
    """Seeds a temp dir as the seam root -- the same convention as
    tests/test_aaos_common.py::BaseAaosTest and
    tests/test_queue_scripts.py::ScriptTestBase."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aaos-fim-")
        self.root = Path(self._tmpdir)
        self._old_env = os.environ.get("AAOS_SEAM_ROOT")
        os.environ["AAOS_SEAM_ROOT"] = str(self.root)
        for sub in ("tasks", "handoffs", "locks/_archive", "events"):
            (self.root / "projects" / PROJECT / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AAOS_SEAM_ROOT", None)
        else:
            os.environ["AAOS_SEAM_ROOT"] = self._old_env
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def run_tool(self, filename, *args):
        module = load_tool(filename)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = module.main(list(args))
        return code, buf_out.getvalue() + buf_err.getvalue()

    def run_validate(self, path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main([str(path)])
        return code, buf.getvalue()

    def seed_task(self, task_id, **kwargs):
        task = _make_task(task_id, **kwargs)
        aaos.save_task(task, root=str(self.root))
        return task

    def reload_task(self, task_id):
        return aaos.load_task(task_id, PROJECT, root=str(self.root))

    def events_raw(self):
        log_path = aaos.events_log_path(PROJECT, root=str(self.root))
        return log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    def events(self):
        return [json.loads(l) for l in self.events_raw().splitlines() if l.strip()]


# =============================================================================
# Scenario #1 -- COVERED. Cites tests/test_aaos_common.py::
# AcquireLeaseExpiredReclaimTests + tests/test_queue_scripts.py::
# ExpireLeasesReclaimTests.
# =============================================================================

class Scenario01AgentDiesMidLeaseTests(unittest.TestCase):
    """Scenario #1 (verbatim, master prompt): "Agent dies mid-lease ->
    lease expires -> task reclaimable, event trail correct."

    COVERED (PHASE5-DESIGN.md coverage inventory row #1) -- no new
    mechanism needed, only fresh pass evidence:
      - tests/test_aaos_common.py::AcquireLeaseExpiredReclaimTests
        (shared-library level: an expired incumbent lock is archived and a
        fresh lease acquired over it, reclaimed_prior surfaced correctly)
      - tests/test_queue_scripts.py::ExpireLeasesReclaimTests
        (CLI-sweep level: missed-heartbeat reclaim + DeadAgent escalation,
        fresh-heartbeat reclaim without escalation, not-yet-expired lock
        left completely untouched)
    """

    def test_existing_coverage_passes(self):
        import test_aaos_common
        import test_queue_scripts

        result, output = _run_existing(
            test_aaos_common.AcquireLeaseExpiredReclaimTests,
            test_queue_scripts.ExpireLeasesReclaimTests,
        )
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(result.testsRun, 4, output)


# =============================================================================
# Scenario #2 -- NEW. Corrupt task file; queue otherwise unaffected.
# =============================================================================

class Scenario02CorruptTaskFileTests(BaseFailureMatrixTest):
    """Scenario #2 (verbatim, master prompt): "Corrupt/hand-mangled task
    file -> validator blocks all operations on it, loud flag, queue
    otherwise unaffected."

    NEW (PHASE5-DESIGN.md #2). Existing coverage was partial:
    tests/test_aaos_common.py::CorruptLockTests covers corrupt LOCK files
    only, and tests/test_validate.py's malformed-JSON coverage is generic
    (one file at a time). No existing test demonstrated the specific
    narrative this scenario needs: one corrupt task file sitting among
    well-formed siblings, with the siblings completely unaffected and the
    corrupt one failing loudly (never a raw traceback) via both
    tools/validate.py AND tools/lease-task.py.
    """

    def _seed_three_tasks(self):
        self.seed_task("t-scn2-ok-a")
        self.seed_task("t-scn2-ok-b")
        corrupt_path = aaos.task_path("t-scn2-corrupt", PROJECT, root=str(self.root))
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_text(
            '{"protocol_version": "1.0", "id": "t-scn2-corrupt", "title": TRUNCATED',
            encoding="utf-8",
        )
        return corrupt_path

    def test_validate_passes_siblings_fails_loudly_on_corrupt(self):
        corrupt_path = self._seed_three_tasks()
        ok_a = aaos.task_path("t-scn2-ok-a", PROJECT, root=str(self.root))
        ok_b = aaos.task_path("t-scn2-ok-b", PROJECT, root=str(self.root))

        code_a, out_a = self.run_validate(ok_a)
        self.assertEqual(code_a, 0, out_a)
        self.assertIn("VALID:", out_a)

        code_b, out_b = self.run_validate(ok_b)
        self.assertEqual(code_b, 0, out_b)
        self.assertIn("VALID:", out_b)

        code_c, out_c = self.run_validate(corrupt_path)
        self.assertEqual(code_c, 1, out_c)
        self.assertIn("JSON PARSE ERROR", out_c)
        self.assertIn(str(corrupt_path), out_c)
        self.assertNotIn("Traceback", out_c)

    def test_lease_task_on_sibling_succeeds_corrupt_sibling_causes_zero_interference(self):
        self._seed_three_tasks()
        code, out = self.run_tool(
            "lease-task.py", "t-scn2-ok-a",
            "--holder", "claude-code", "--session", "sess-1",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        task = self.reload_task("t-scn2-ok-a")
        self.assertEqual(task["state"], "leased")
        self.assertEqual(task["lease"]["holder"], "claude-code")

    def test_lease_task_on_corrupt_file_fails_closed_not_raw_traceback(self):
        self._seed_three_tasks()
        code, out = self.run_tool(
            "lease-task.py", "t-scn2-corrupt",
            "--holder", "claude-code", "--session", "sess-1",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        # tools/lease-task.py catches aaos.CorruptStateError (raised by
        # _aaos_common.load_task -> load_json on the unparseable JSON) and
        # prints a clean, labeled message -- not a bare Python traceback.
        self.assertEqual(code, 1, out)
        self.assertIn("VALIDATION FAILED", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("JSONDecodeError", out)


# =============================================================================
# Scenario #3 -- COVERED. Cites tests/test_aaos_common.py::
# AcquireLeaseRaceTests + tests/test_queue_scripts.py::
# LeaseTaskContestedTests.
# =============================================================================

class Scenario03TwoWritersRaceTests(unittest.TestCase):
    """Scenario #3 (verbatim, master prompt): "Two writers race for one
    task -> exactly one wins; loser exits cleanly with event."

    COVERED (PHASE5-DESIGN.md coverage inventory row #3):
      - tests/test_aaos_common.py::AcquireLeaseRaceTests (a genuine
        threading.Barrier-synchronized race, 10 iterations, asserting
        exactly one ACQUIRED / one DENIED every time, never two ACQUIRED)
      - tests/test_queue_scripts.py::LeaseTaskContestedTests (the
        CLI-level settle-window conflict-copy race, asserting exit 4 /
        LeaseContested event / task reverted to queued)
    """

    def test_existing_coverage_passes(self):
        import test_aaos_common
        import test_queue_scripts

        result, output = _run_existing(
            test_aaos_common.AcquireLeaseRaceTests,
            test_queue_scripts.LeaseTaskContestedTests,
        )
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(result.testsRun, 2, output)


# =============================================================================
# Scenario #4 -- NEW. Seam unavailable.
# =============================================================================

class Scenario04SeamUnavailableTests(BaseFailureMatrixTest):
    """Scenario #4 (verbatim, master prompt): "Seam unavailable (rename
    the shared directory) -> every operation fails closed with a clear
    error; nothing half-written."

    NEW (PHASE5-DESIGN.md #4) -- and this rehearsal surfaced a REAL GAP.
    Empirically confirmed (see docs/_task-reports/phase5-failure-matrix-
    report.md scenario #4 for the raw before/after transcripts): pointed
    at a --seam-root that does not exist, create-task.py, post-handoff.py
    (no --blocker), and emit-event.py had no reason to READ anything at
    the seam before their first WRITE, so atomic_write's own
    os.makedirs(path.parent, exist_ok=True) silently auto-vivified a
    brand-new, empty parallel seam tree at that path and the operation
    SUCCEEDED (exit 0, task/handoff/event fully written) instead of
    failing closed. expire-leases.py printed "no locks directory; nothing
    to sweep" and returned exit 0 -- no side effect, but also not a
    failure signal. lease-task.py and complete-task.py already failed
    closed correctly (they read task state before writing anything).

    FIX: tools/_aaos_common.py::require_seam_reachable(root) -- raises
    UsageError (exit 2) if the seam root itself is not an existing
    directory -- is now called as the FIRST line inside every
    seam-touching CLI script's main() (all 7: create-task, lease-task,
    complete-task, fail-task, post-handoff, expire-leases, emit-event;
    the 5 named by this scenario plus the 2 siblings sharing the identical
    latent gap, fixed for consistency -- see the Phase 5 report for the
    scope note). This class is both the regression test for that fix and
    the evidence for the design's explicit "check this ordering" ask: NO
    directory (not even an empty shell) is created for any of the 5
    scripts below once require_seam_reachable runs before any I/O.
    """

    def setUp(self):
        super().setUp()
        # A path that does NOT exist, INSIDE the already-existing temp
        # root -- simulates the OneDrive sync folder being renamed/
        # unmounted (the parent tree is reachable; the seam itself is
        # not), exactly as PHASE5-DESIGN.md #4 specifies.
        self.phantom = self.root / "phantom-seam"
        self.assertFalse(self.phantom.exists())

    def _tree(self):
        if not self.phantom.exists():
            return ["<phantom-seam directory itself now exists>"]
        return sorted(str(p.relative_to(self.phantom)) for p in self.phantom.rglob("*"))

    def _assert_fails_closed_no_side_effects(self, filename, *args):
        code, out = self.run_tool(
            filename, *args, "--seam-root", str(self.phantom), "--now", _iso(NOW)
        )
        self.assertNotEqual(code, 0, f"{filename} did not fail closed: {out}")
        self.assertNotIn("Traceback", out, f"{filename}: {out}")
        self.assertIn("seam root unreachable", out, f"{filename}: {out}")
        self.assertFalse(
            self.phantom.exists(),
            f"{filename} left a filesystem side effect at an unreachable seam root: {self._tree()}",
        )
        return code, out

    def test_create_task_fails_closed_no_side_effects(self):
        self._assert_fails_closed_no_side_effects(
            "create-task.py",
            "--title", "phantom test", "--id", "t-scn4-phantom", "--accept", "x",
        )

    def test_lease_task_against_preseeded_id_seam_absent_fails_closed(self):
        # "pre-seeded task id, seam absent" per the design: we act as if
        # t-scn4-phantom were already created (we know its id) -- the
        # point under test is that the SEAM itself is unreachable, so
        # lease-task.py can't even read the task it's asked to lease.
        self._assert_fails_closed_no_side_effects(
            "lease-task.py", "t-scn4-phantom",
            "--holder", "claude-code", "--session", "sess-1", "--settle-sec", "0",
        )

    def test_post_handoff_fails_closed_no_side_effects(self):
        artifact = str(REPO_ROOT / "docs" / "MIGRATIONS.md").replace("\\", "/")
        self._assert_fails_closed_no_side_effects(
            "post-handoff.py",
            "--id", "h-scn4-phantom", "--task", "t-scn4-phantom",
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "n/a", "--summary", "s",
            "--artifact", f"path={artifact}",
        )

    def test_complete_task_fails_closed_no_side_effects(self):
        self._assert_fails_closed_no_side_effects(
            "complete-task.py", "t-scn4-phantom",
            "--holder", "claude-code", "--session", "sess-1",
            "--evidence", "label=x", "ref=y",
        )

    def test_expire_leases_fails_closed_no_side_effects(self):
        self._assert_fails_closed_no_side_effects("expire-leases.py")


# =============================================================================
# Scenario #5 -- COVERED. Cites tests/test_project_notion.py::
# StaleFlagBoundaryTests.
# =============================================================================

class Scenario05StaleNotionMirrorTests(unittest.TestCase):
    """Scenario #5 (verbatim, master prompt): "Stale Notion mirror (>7
    days) -> marked STALE, never presented as current."

    COVERED (PHASE5-DESIGN.md coverage inventory row #5):
      - tests/test_project_notion.py::StaleFlagBoundaryTests (8-days-old
        task file mtime -> generation.stale == true; 1-hour-old -> false;
        generation.generated_at is always stamped)
    """

    def test_existing_coverage_passes(self):
        import test_project_notion

        result, output = _run_existing(test_project_notion.StaleFlagBoundaryTests)
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(result.testsRun, 3, output)


# =============================================================================
# Scenario #6 -- COVERED. Cites tests/test_validate.py::
# ProtocolVersionTests, PLUS a direct re-demonstration per the design's
# explicit instruction.
# =============================================================================

class Scenario06ProtocolVersionMismatchTests(BaseFailureMatrixTest):
    """Scenario #6 (verbatim, master prompt): "Protocol version mismatch
    (fixture with protocol_version: 0.9) -> rejected with migration
    pointer."

    COVERED (PHASE5-DESIGN.md coverage inventory row #6) by
    tests/test_validate.py::ProtocolVersionTests. Per the design's
    explicit instruction for this scenario, ALSO directly construct a
    protocol_version: "0.9" fixture and run it through tools/validate.py
    here, asserting rejection + a docs/MIGRATIONS.md pointer -- exactly
    like ProtocolVersionTests already proves -- so this file is a
    self-contained, readable evidence report even though the mechanism
    was proven elsewhere first.
    """

    def test_protocol_version_0_9_is_rejected_with_migrations_pointer(self):
        fixture_path = self.root / "t-scn6-old-protocol.json"
        fixture_path.write_text(json.dumps({
            "protocol_version": "0.9",
            "id": "t-scn6-old-protocol",
            "project_id": PROJECT,
            "title": "Task declaring an unsupported (pre-1.0) protocol version",
            "owner_agent": "unassigned",
            "state": "queued",
            "priority": 2,
            "inputs": [],
            "acceptance_criteria": ["it works"],
            "created_at": _iso(NOW),
            "updated_at": _iso(NOW),
        }), encoding="utf-8")

        code, out = self.run_validate(fixture_path)
        self.assertEqual(code, 1, out)
        self.assertIn("protocol_version mismatch", out)
        self.assertIn("0.9", out)
        self.assertIn("docs/MIGRATIONS.md", out)

    def test_existing_coverage_still_passes(self):
        import test_validate

        result, output = _run_existing(test_validate.ProtocolVersionTests)
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(result.testsRun, 2, output)


# =============================================================================
# Scenario #7 -- COVERED. Cites tests/test_aaos_common.py::EmitEventTests
# ::test_commit_state_change_state_unchanged_on_event_failure.
# =============================================================================

class Scenario07EventLogAppendFailureTests(unittest.TestCase):
    """Scenario #7 (verbatim, master prompt): "Event-log append failure ->
    operation aborts (event write is part of the transaction, not
    best-effort)."

    COVERED (PHASE5-DESIGN.md coverage inventory row #7):
      - tests/test_aaos_common.py::EmitEventTests
        ::test_commit_state_change_state_unchanged_on_event_failure
        (directly proves the abort contract AT THE TRANSACTION LEVEL --
        commit_state_change stages a tmp state file, and if the event
        commit fails, the staged tmp is discarded and the LIVE state file
        is left byte-for-byte unchanged -- not just emit_event's own
        internal retry/spool behavior in isolation).
    """

    def test_existing_coverage_passes(self):
        import test_aaos_common

        result, output = _run_existing(
            (
                test_aaos_common.EmitEventTests,
                "test_commit_state_change_state_unchanged_on_event_failure",
            ),
        )
        self.assertTrue(result.wasSuccessful(), output)
        self.assertEqual(result.testsRun, 1, output)


# =============================================================================
# Scenario #8 -- NEW. Partial write / interrupted sync leaves the prior
# version intact.
# =============================================================================

class Scenario08InterruptedWriteTests(BaseFailureMatrixTest):
    """Scenario #8 (verbatim, master prompt): "Partial write / interrupted
    sync -> truncate a state file mid-content (simulating a killed write
    or OneDrive sync fault) -> validator rejects it, operations on it
    halt, and the atomic write-rename convention is shown to have left the
    prior version intact."

    NEW (PHASE5-DESIGN.md #8). This test deliberately does NOT call
    aaos.atomic_write for the interruption itself -- that would prove
    nothing, since atomic_write always completes its own os.replace on
    success (that is the entire point of the design). Instead it
    hand-constructs atomic_write's exact temp-file naming convention
    (path + ".tmp-" + str(os.getpid()), see tools/_aaos_common.py::
    atomic_write) and deliberately STOPS before the os.replace step,
    simulating a process killed -- or a OneDrive sync fault caught -- in
    the one gap atomic_write's own design makes uncorruptable: nothing
    ever reads or trusts the .tmp file; only the rename makes a new
    version live. This demonstrates the *absence* of a code path (no tool
    ever writes the live file in place), not code under test.
    """

    def test_killed_write_between_tmp_and_replace_leaves_v1_intact_and_valid(self):
        task_path = aaos.task_path("t-scn8-partial", PROJECT, root=str(self.root))
        self.seed_task("t-scn8-partial", title="v1 marker value")
        v1_bytes = task_path.read_bytes()

        pid = os.getpid()
        tmp_path = Path(str(task_path) + ".tmp-" + str(pid))
        truncated_v2 = '{"protocol_version": "1.0", "id": "t-scn8-partial", "title": "v2 TRUNC'
        tmp_path.write_bytes(truncated_v2.encode("utf-8"))
        # Deliberately NO os.replace(tmp_path, task_path) here -- this is
        # the simulated kill point.

        # (1) The live file is still v1, byte-for-byte.
        self.assertEqual(task_path.read_bytes(), v1_bytes)
        reloaded = self.reload_task("t-scn8-partial")
        self.assertEqual(reloaded["title"], "v1 marker value")

        # (2) It still validates cleanly.
        code, out = self.run_validate(task_path)
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)

        # (2b) The truncated v2 content itself is NOT valid JSON, so if it
        # had somehow become live, validate.py would have rejected it --
        # confirming this is a genuine "prior version survives" story, not
        # a vacuous one where v2 would have been fine anyway.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(tmp_path.read_text(encoding="utf-8"))
        tmp_validate_code, tmp_validate_out = self.run_validate(tmp_path)
        self.assertEqual(tmp_validate_code, 1, tmp_validate_out)

        # (3) The stray .tmp-* file is present and detectable -- exactly
        # the evidence a human/sweep could notice and clean up.
        stray = list(task_path.parent.glob(f"{task_path.name}.tmp-*"))
        self.assertEqual(stray, [tmp_path])
        self.assertIn("v2 TRUNC", tmp_path.read_text(encoding="utf-8"))


# =============================================================================
# Scenario #9 -- COVERED. Cites tests/test_queue_scripts.py::
# PostHandoffWriteScopeTests + tests/test_validate.py::
# WriteScopeTraversalTests.
# =============================================================================

class Scenario09WriteScopeViolationTests(unittest.TestCase):
    """Scenario #9 (verbatim, master prompt): "Write-scope violation -> a
    handoff manifest referencing a path outside the posting agent's
    write_scope -> validation fails, loud flag, task untouched."

    COVERED (PHASE5-DESIGN.md coverage inventory row #9) -- this is Gate
    2's own finding-and-fix, with dedicated regression coverage:
      - tests/test_queue_scripts.py::PostHandoffWriteScopeTests (CLI
        level: an out-of-scope artifact -> post-handoff.py exits 1,
        writes nothing, emits no HandoffPosted; plus, since OI-263, that
        --registry actually changes the verdict rather than being accepted
        and ignored, and that the derived scope names THIS checkout rather
        than a hard-coded absolute path)
      - tests/test_validate.py::WriteScopeTraversalTests (validator level:
        a "../.." traversal path that resolves OUTSIDE scope is rejected;
        one that resolves back INSIDE scope is correctly accepted)
    """

    def test_existing_coverage_passes(self):
        import test_queue_scripts
        import test_validate

        result, output = _run_existing(
            test_queue_scripts.PostHandoffWriteScopeTests,
            test_validate.WriteScopeTraversalTests,
        )
        self.assertTrue(result.wasSuccessful(), output)
        # Count guard against silent coverage LOSS -- raise it deliberately when you add
        # coverage, never lower it to make a run go green. 3 -> 5 on 2026-09-10 (OI-263).
        self.assertEqual(result.testsRun, 5, output)


# =============================================================================
# Scenario #10 -- NEW. Rollback drill on an in-flight (leased) task.
# =============================================================================

class Scenario10RollbackInFlightLeasedTaskTests(BaseFailureMatrixTest):
    """Scenario #10 (verbatim, master prompt): "Rollback drill: archive an
    in-flight task and restore pre-task state; verify no orphaned locks or
    events referencing live work."

    NEW (PHASE5-DESIGN.md #10). Gate 4's own rollback drill
    (docs/GATE4-REVIEW.md) exercised docs/OPERATIONS.md §3 against
    t-cowork-seam-confirm, which was NEVER leased (state=queued, no lock
    file anywhere). "In-flight" specifically means a task that IS
    currently leased -- a materially different case: the lock must also
    be relocated as part of the rollback, which the prior drill never
    exercised.

    This class leases a task FOR REAL via lease-task.py (--settle-sec 0 is
    a TEST-ONLY speed override -- not a claim about production timing;
    ADR-0003's real default settle window is 30s), confirms a genuine live
    lock file exists, posts a real handoff referencing the in-flight task
    (so the "no dangling reference" assertion below is a real check, not a
    vacuous one), and then executes docs/OPERATIONS.md §3's archive
    procedure EXACTLY as documented, by hand, against this specific leased
    task.
    """

    def setUp(self):
        super().setUp()
        self.task_id = "t-scn10-leased"
        self.seed_task(self.task_id)

        code, out = self.run_tool(
            "lease-task.py", self.task_id,
            "--holder", "claude-code", "--session", "sess-scn10",
            "--settle-sec", "0", "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.lock_path = aaos.lock_path(self.task_id, PROJECT, root=str(self.root))
        self.assertTrue(
            self.lock_path.exists(), "precondition: lease-task.py must leave a live lock file"
        )

        artifact = str(REPO_ROOT / "docs" / "MIGRATIONS.md").replace("\\", "/")
        code, out = self.run_tool(
            "post-handoff.py",
            "--id", "h-scn10-inflight", "--task", self.task_id,
            "--from", "claude-code", "--to", "cowork",
            "--next-action", "keep working", "--summary", "in-flight handoff",
            "--artifact", f"path={artifact}",
            "--now", _iso(NOW),
        )
        self.assertEqual(code, 0, out)
        self.handoff_path = aaos.handoff_path("h-scn10-inflight", PROJECT, root=str(self.root))
        self.assertTrue(self.handoff_path.exists())

        self.events_before = self.events_raw()

    def _run_operations_md_section3_archive_drill(self):
        """Executes docs/OPERATIONS.md §3 exactly as documented, by hand,
        against self.task_id:
          1. Set state="archived" on the LIVE task file via the atomic
             write-rename convention (before moving it, so the last
             live-directory copy reflects its final state).
          2. Move the task file to tasks/_archive/<id>.json.
          3. Move any lock file still present to locks/_archive/ (the
             in-flight case this scenario exists to prove); move any
             handoff(s) referencing this task_ref to handoffs/_archive/.
          4. Leave events/aaos-events.ndjson alone -- untouched, not even
             read, anywhere in this method.
          5. (validation happens in the test method itself, against the
             moved task file, per §3 step 5.)
        """
        project_root = aaos.project_dir(PROJECT, root=str(self.root))

        # --- Step 1 ---
        task = self.reload_task(self.task_id)
        archived_task = dict(task)
        archived_task["state"] = "archived"
        archived_task.pop("lease", None)
        archived_task["updated_at"] = _iso(NOW)
        live_task_path = aaos.task_path(self.task_id, PROJECT, root=str(self.root))
        tmp = live_task_path.parent / f"{live_task_path.name}.tmp"
        aaos.atomic_write(tmp, json.dumps(archived_task, indent=2, ensure_ascii=False))
        os.replace(str(tmp), str(live_task_path))

        # --- Step 2 ---
        archive_tasks_dir = project_root / "tasks" / "_archive"
        archive_tasks_dir.mkdir(parents=True, exist_ok=True)
        archived_task_path = archive_tasks_dir / f"{self.task_id}.json"
        os.replace(str(live_task_path), str(archived_task_path))

        # --- Step 3a: lock, if still present ---
        archive_locks_dir = project_root / "locks" / "_archive"
        archive_locks_dir.mkdir(parents=True, exist_ok=True)
        archived_lock_path = None
        if self.lock_path.exists():
            compact_ts = NOW.strftime("%Y%m%dT%H%M%S")
            archived_lock_path = archive_locks_dir / f"{self.task_id}.{compact_ts}.lock"
            os.replace(str(self.lock_path), str(archived_lock_path))

        # --- Step 3b: handoff(s) referencing this task_ref ---
        handoffs_dir = project_root / "handoffs"
        archive_handoffs_dir = handoffs_dir / "_archive"
        moved_handoffs = []
        if handoffs_dir.is_dir():
            for entry in sorted(handoffs_dir.glob("h-*.json")):
                data = json.loads(entry.read_text(encoding="utf-8"))
                if data.get("task_ref") == self.task_id:
                    archive_handoffs_dir.mkdir(parents=True, exist_ok=True)
                    target = archive_handoffs_dir / entry.name
                    os.replace(str(entry), str(target))
                    moved_handoffs.append(target)

        # --- Step 4: events/aaos-events.ndjson is deliberately never
        # touched anywhere in this method. ---

        return archived_task_path, archived_lock_path, moved_handoffs

    def test_rollback_drill_on_leased_task_all_four_assertions(self):
        archived_task_path, archived_lock_path, moved_handoffs = (
            self._run_operations_md_section3_archive_drill()
        )

        # (a) the archived task exists at its new path, validates, and is
        # genuinely state="archived" with the lease dropped.
        code, out = self.run_validate(archived_task_path)
        self.assertEqual(code, 0, out)
        archived_task = json.loads(archived_task_path.read_text(encoding="utf-8"))
        self.assertEqual(archived_task["state"], "archived")
        self.assertNotIn("lease", archived_task)

        # (b) the lock no longer exists at its live path -- it was MOVED
        # to locks/_archive/, never deleted, never left dangling live.
        self.assertFalse(self.lock_path.exists())
        self.assertIsNotNone(
            archived_lock_path, "precondition: the in-flight task must have had a live lock to relocate"
        )
        self.assertTrue(archived_lock_path.exists())

        # (c) the event log is untouched: identical content, same line
        # count, before and after -- no new event fabricated to describe
        # the manual archive (§3's own "leave the event log alone" rule;
        # the master prompt's "no orphaned events referencing live work"
        # is satisfied by history staying complete and unedited, not by
        # pretending nothing happened).
        events_after = self.events_raw()
        self.assertEqual(events_after, self.events_before)
        lines_before = [l for l in self.events_before.splitlines() if l.strip()]
        lines_after = [l for l in events_after.splitlines() if l.strip()]
        self.assertEqual(len(lines_before), len(lines_after))
        self.assertGreater(
            len(lines_before), 0, "precondition: TaskLeased/HandoffPosted must be on record"
        )
        types_on_record = {json.loads(l)["type"] for l in lines_before}
        self.assertIn("TaskLeased", types_on_record)

        # (d) no dangling reference anywhere else: the handoff that
        # referenced this task_ref was relocated WITH it (never left live
        # pointing at a now-missing task), and the live tasks/handoffs
        # directories no longer contain anything for this task id.
        self.assertEqual(len(moved_handoffs), 1)
        self.assertFalse(self.handoff_path.exists())

        live_tasks_dir = aaos.project_dir(PROJECT, root=str(self.root)) / "tasks"
        self.assertFalse((live_tasks_dir / f"{self.task_id}.json").exists())

        live_handoffs_dir = aaos.project_dir(PROJECT, root=str(self.root)) / "handoffs"
        for entry in live_handoffs_dir.glob("h-*.json"):
            data = json.loads(entry.read_text(encoding="utf-8"))
            self.assertNotEqual(
                data.get("task_ref"), self.task_id,
                f"dangling reference: {entry} still points at archived task {self.task_id}",
            )


if __name__ == "__main__":
    unittest.main()
