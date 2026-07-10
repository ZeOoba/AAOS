# Phase 5 Failure-Injection Matrix — Consolidated Evidence Report

Master prompt reference: `docs/2026-07-09_AAOS_MasterPrompt_v02.md`, "PHASE 5
— VALIDATION & HARDENING" / "Failure-injection matrix (ten scenarios; each
scripted, executed, evidenced)". Design reference: `docs/PHASE5-DESIGN.md`.
Test file: `tests/test_failure_injection_matrix.py` (one `unittest.TestCase`
class per scenario, `Scenario01...` – `Scenario10...`, matching the master
prompt's own numbering 1:1).

All command output below is real, captured from this session's own test
runs (`python -m unittest ...`), not paraphrased.

---

## Scenario #1 — Agent dies mid-lease

> "Agent dies mid-lease → lease expires → task reclaimable, event trail
> correct."

**Verdict:** COVERED (no new mechanism this phase).

**Proven by:**
- `tests/test_aaos_common.py::AcquireLeaseExpiredReclaimTests` (shared-library level)
- `tests/test_queue_scripts.py::ExpireLeasesReclaimTests` (CLI-sweep level, 3 methods)
- Re-demonstrated here by `tests/test_failure_injection_matrix.py::Scenario01AgentDiesMidLeaseTests`, which invokes both existing classes via `unittest`'s own test-loading machinery (`unittest.TestLoader` + a private `TestSuite`/`TextTestRunner`) and asserts the result is a clean pass.

**Pass evidence** (from `python -m unittest tests.test_failure_injection_matrix -v`):
```
test_existing_coverage_passes (tests.test_failure_injection_matrix.Scenario01AgentDiesMidLeaseTests.test_existing_coverage_passes) ... ok
```
Internally this nested run reports (captured via the class's own assertion, `result.testsRun == 4`): `test_expired_incumbent_is_archived_and_new_lease_acquired` (1) + `ExpireLeasesReclaimTests`'s 3 methods (`test_expired_missed_heartbeat_reclaims_requeues_and_escalates`, `test_expired_fresh_heartbeat_reclaims_without_escalation`, `test_not_yet_expired_lock_untouched`) = 4 tests, all `ok`.

---

## Scenario #2 — Corrupt/hand-mangled task file

> "Corrupt/hand-mangled task file → validator blocks all operations on it,
> loud flag, queue otherwise unaffected."

**Verdict:** NEW scenario, built this phase.

**Why new:** existing coverage was partial — `tests/test_aaos_common.py::CorruptLockTests` covers corrupt **lock** files only, and `tests/test_validate.py`'s malformed-JSON coverage is one file at a time. No existing test demonstrated the specific narrative: one corrupt task file sitting among well-formed siblings, siblings completely unaffected, and the corrupt one failing loudly (never a raw traceback) via both `tools/validate.py` and `tools/lease-task.py`.

**Built:** `Scenario02CorruptTaskFileTests` (3 test methods) — seeds `t-scn2-ok-a`, `t-scn2-ok-b` (well-formed) and `t-scn2-corrupt` (hand-truncated invalid JSON) side by side.

**Pass evidence:**
```
test_validate_passes_siblings_fails_loudly_on_corrupt (...) ... ok
test_lease_task_on_sibling_succeeds_corrupt_sibling_causes_zero_interference (...) ... ok
test_lease_task_on_corrupt_file_fails_closed_not_raw_traceback (...) ... ok
```

Real observed output captured inside the test:
- `validate.py` against the two well-formed siblings: `VALID: ...\tasks\t-scn2-ok-a.json` / `t-scn2-ok-b.json`, exit 0.
- `validate.py` against the corrupt file: `JSON PARSE ERROR: ...\tasks\t-scn2-corrupt.json: Expecting value: line 1 column 62 (char 61)`, exit 1, no traceback.
- `lease-task.py t-scn2-ok-a ...`: `ACQUIRED: t-scn2-ok-a by claude-code/sess-1 (event ...)`, exit 0 — the corrupt sibling caused zero interference.
- `lease-task.py t-scn2-corrupt ...`: `VALIDATION FAILED: unparseable JSON at ...\tasks\t-scn2-corrupt.json: Expecting value: line 1 column 62 (char 61)`, exit 1 — `_aaos_common.load_task`'s `CorruptStateError` surfaced cleanly by `lease-task.py`'s own exception handler, never a bare Python traceback.

**Production code change:** none needed — the fail-closed path already existed from Phase 3 (`CorruptStateError` → `lease-task.py` exit 1). This scenario proves it explicitly; it did not surface a gap.

---

## Scenario #3 — Two writers race for one task

> "Two writers race for one task → exactly one wins; loser exits cleanly
> with event."

**Verdict:** COVERED.

**Proven by:**
- `tests/test_aaos_common.py::AcquireLeaseRaceTests` (genuine `threading.Barrier`-synchronized race, 10 iterations, exactly one `ACQUIRED`/one `DENIED` every time)
- `tests/test_queue_scripts.py::LeaseTaskContestedTests` (CLI-level settle-window conflict-copy race, exit 4 / `LeaseContested` event / task reverted to `queued`)

**Pass evidence:**
```
test_existing_coverage_passes (tests.test_failure_injection_matrix.Scenario03TwoWritersRaceTests.test_existing_coverage_passes) ... ok
```
Nested run: 2 tests, `result.wasSuccessful() == True`.

---

## Scenario #4 — Seam unavailable

> "Seam unavailable (rename the shared directory) → every operation fails
> closed with a clear error; nothing half-written."

**Verdict:** NEW scenario — **and this rehearsal surfaced a real gap in `tools/*.py`, which was fixed with its own regression test.**

### The gap, empirically confirmed

Before any fix, running each of the 5 named scripts against `--seam-root <path-that-does-not-exist>` gave:

| script | pre-fix exit | pre-fix behavior |
|---|---|---|
| `create-task.py` | **0** | `CREATED: t-phantom (event ...)` — silently created the entire seam tree (`projects/ora-ops/{tasks,events}` + the task file + the event) at the nonexistent path and **succeeded**. |
| `lease-task.py` | 2 | `USAGE ERROR: could not read ...\tasks\t-phantom.json` — failed closed correctly (it reads the task before writing anything). |
| `post-handoff.py` (no `--blocker`) | **0** | `POSTED: h-phantom (event ...)` — same silent auto-vivify-and-succeed pattern as `create-task.py`. |
| `complete-task.py` | 2 | `USAGE ERROR: could not read ...\tasks\t-phantom.json` — failed closed correctly. |
| `expire-leases.py` | **0** | `no locks directory; nothing to sweep` — no filesystem side effect, but also not a failure signal; a genuinely unreachable seam is indistinguishable from "this project just has no locks yet." |

Root cause: `create-task.py`, `post-handoff.py` (without a blocker), and (separately confirmed) `emit-event.py`'s normal path have no reason to **read** anything at the seam before their **first write** — so `tools/_aaos_common.py::atomic_write`'s own `os.makedirs(path.parent, exist_ok=True)` had no notion that a missing seam **root** is different from a missing subdirectory that's fine to auto-create on first use. Pointed at a renamed/unmounted OneDrive sync folder, these scripts didn't just leave an "empty directory shell" (the design doc's hedge) — they completed a full, successful write to a brand-new, wrong location.

Raw pre-fix repro (captured this session, `tools/create-task.py`):
```
=== create-task.py ===
exit: 0
CREATED: t-phantom (event e-2026-07-09T12:00:00-04:00-178370122606631380033361)
phantom exists after: True
tree: ['projects', 'projects\\ora-ops', 'projects\\ora-ops\\events', 'projects\\ora-ops\\events\\aaos-events.ndjson',
       'projects\\ora-ops\\events\\aaos-events.ndjson.pending', ...,
       'projects\\ora-ops\\tasks', 'projects\\ora-ops\\tasks\\t-phantom.json']
```

### The fix

Added `tools/_aaos_common.py::require_seam_reachable(root=None)`: raises `UsageError` (exit 2) if `seam_root(root)` is not an existing directory. Wired as the **first line inside `main()`'s `try:` block**, before any other seam I/O, in all 7 seam-touching CLI scripts: `create-task.py`, `lease-task.py`, `complete-task.py`, `fail-task.py`, `post-handoff.py`, `expire-leases.py`, `emit-event.py`. (The design named 5 scripts for this scenario's rehearsal; `fail-task.py` and `emit-event.py` share the identical latent gap — `fail-task.py` already happened to fail closed via its own pre-write read, same as `lease-task.py`/`complete-task.py`, but `emit-event.py`'s normal, non-`--drain-only` path had the same auto-vivify gap as `create-task.py`/`post-handoff.py`. Fixed for consistency rather than leaving two siblings with the same unfixed bug.)

Post-fix repro, all 7 scripts, same nonexistent `--seam-root`:
```
=== create-task.py ===       exit: 2   USAGE ERROR: seam root unreachable: '...\phantom-seam' does not exist or is not a directory. ...
=== lease-task.py ===        exit: 2   USAGE ERROR: seam root unreachable: ...
=== post-handoff.py ===      exit: 2   USAGE ERROR: seam root unreachable: ...
=== complete-task.py ===     exit: 2   USAGE ERROR: seam root unreachable: ...
=== expire-leases.py ===     exit: 2   USAGE ERROR: seam root unreachable: ...
=== fail-task.py ===         exit: 2   USAGE ERROR: seam root unreachable: ...
=== emit-event.py ===        exit: 2   USAGE ERROR: seam root unreachable: ...
```
Every run: `phantom exists after: False`, `tree: []` — literally nothing is created, not even an empty directory shell (the design's fallback-acceptable case). This is a stronger outcome than the design's hedge anticipated.

### Regression test + red/green cycle

`tests/test_failure_injection_matrix.py::Scenario04SeamUnavailableTests` (5 methods, one per named script) asserts: non-zero exit, `"seam root unreachable"` in the combined stdout+stderr, no `Traceback`, and — critically — that the phantom seam directory still does not exist on disk afterward.

**Red (pre-fix):** the fix was temporarily reverted via `git stash` (isolated to the 8 modified `tools/*.py` + `_aaos_common.py` files, the new test file untouched) and the class re-run:
```
test_complete_task_fails_closed_no_side_effects ... FAIL
test_create_task_fails_closed_no_side_effects ... FAIL
test_expire_leases_fails_closed_no_side_effects ... FAIL
test_lease_task_against_preseeded_id_seam_absent_fails_closed ... FAIL
test_post_handoff_fails_closed_no_side_effects ... FAIL

----------------------------------------------------------------------
Ran 5 tests in 0.099s

FAILED (failures=5)
```
Representative failure: `AssertionError: 0 == 0 : create-task.py did not fail closed: CREATED: t-scn4-phantom (event e-2026-07-09T12:00:00-04:00-...)`.
(`lease-task.py`/`complete-task.py` also "failed" this run only because the test additionally asserts the literal `"seam root unreachable"` message — pre-fix they already exited non-zero, just via a different, incidental `USAGE ERROR: could not read ...` path, not the intended uniform check.)

**Green (post-fix, `git stash pop` to restore):**
```
test_complete_task_fails_closed_no_side_effects (...) ... ok
test_create_task_fails_closed_no_side_effects (...) ... ok
test_expire_leases_fails_closed_no_side_effects (...) ... ok
test_lease_task_against_preseeded_id_seam_absent_fails_closed (...) ... ok
test_post_handoff_fails_closed_no_side_effects (...) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.077s

OK
```

**Scope note:** `docs/OPERATIONS.md` already carries an uncommitted §5 (added by a different, parallel Phase 5 work item — not part of this task's scope; not edited here) whose §5.4 entry ("Seam unavailable") already *describes* the fixed, uniform-exit-2 behavior as if it were always true. It was not true until this fix landed in this same session; the two now agree.

---

## Scenario #5 — Stale Notion mirror

> "Stale Notion mirror (>7 days) → marked STALE, never presented as
> current."

**Verdict:** COVERED.

**Proven by:** `tests/test_project_notion.py::StaleFlagBoundaryTests` (8-days-old task file mtime → `generation.stale == true`; 1-hour-old → `false`; `generation.generated_at` always stamped) — 3 methods.

**Pass evidence:**
```
test_existing_coverage_passes (tests.test_failure_injection_matrix.Scenario05StaleNotionMirrorTests.test_existing_coverage_passes) ... ok
```
Nested run: 3 tests, all `ok`.

---

## Scenario #6 — Protocol version mismatch

> "Protocol version mismatch (fixture with protocol_version: 0.9) →
> rejected with migration pointer."

**Verdict:** COVERED, plus a direct re-demonstration per the design's explicit instruction for this scenario.

**Proven by:** `tests/test_validate.py::ProtocolVersionTests` (2 methods) — re-run here — **and** a fresh, self-contained `protocol_version: "0.9"` fixture built and run through `tools/validate.py` directly inside this file.

**Pass evidence:**
```
test_protocol_version_0_9_is_rejected_with_migrations_pointer (...) ... ok
test_existing_coverage_still_passes (...) ... ok
```
Real `validate.py` output captured against the hand-built `t-scn6-old-protocol.json` (`"protocol_version": "0.9"`):
```
protocol_version mismatch: file declares protocol_version '0.9', but this tool only supports major version 1.x. See docs/MIGRATIONS.md for the migration process.
```
Exit 1. Assertions: message contains `"protocol_version mismatch"`, `"0.9"`, and `"docs/MIGRATIONS.md"` — all three present.

---

## Scenario #7 — Event-log append failure

> "Event-log append failure → operation aborts (event write is part of
> the transaction, not best-effort)."

**Verdict:** COVERED.

**Proven by:** `tests/test_aaos_common.py::EmitEventTests::test_commit_state_change_state_unchanged_on_event_failure` — proves the abort contract at the **transaction** level: `commit_state_change` stages a tmp state file, and when the injected event commit fails (`EventCommitError`), the staged tmp is discarded and the live state file is left byte-for-byte unchanged (not just `emit_event`'s own internal retry/spool behavior in isolation).

**Pass evidence:**
```
test_existing_coverage_passes (tests.test_failure_injection_matrix.Scenario07EventLogAppendFailureTests.test_existing_coverage_passes) ... ok
```
Nested run selects exactly this one method (via `unittest.TestLoader`'s single-method-by-name form) — `result.testsRun == 1`, `wasSuccessful() == True`.

---

## Scenario #8 — Partial write / interrupted sync

> "Partial write / interrupted sync → truncate a state file mid-content
> (simulating a killed write or OneDrive sync fault) → validator rejects
> it, operations on it halt, and the atomic write-rename convention is
> shown to have left the prior version intact."

**Verdict:** NEW scenario, built this phase.

**Built:** `Scenario08InterruptedWriteTests`. Per the design's explicit instruction, this test does **not** call `aaos.atomic_write` for the interruption (that would prove nothing, since `atomic_write` always completes its own `os.replace` on success). Instead it hand-constructs `atomic_write`'s exact temp-file naming convention (`path + ".tmp-" + str(os.getpid())`) and deliberately stops before `os.replace`, simulating a process killed — or a OneDrive sync fault — in the one gap the design makes uncorruptable: nothing ever reads or trusts the `.tmp` file; only the rename makes a new version live.

**Pass evidence:**
```
test_killed_write_between_tmp_and_replace_leaves_v1_intact_and_valid (...) ... ok
```
Assertions, all confirmed against real files on disk:
1. The live `task.json` is still v1 byte-for-byte (`task_path.read_bytes() == v1_bytes`).
2. `tools/validate.py` against the live file still returns `VALID: ...t-scn8-partial.json`, exit 0.
3. The truncated v2 content is itself confirmed genuinely-invalid JSON (`json.loads` raises `JSONDecodeError`, and `validate.py` against the `.tmp-*` file itself returns exit 1) — so this is a real "prior version survives a bad write" story, not a vacuous one where v2 would have been fine anyway.
4. The stray `.tmp-<pid>` file is present and detectable via `glob(f"{name}.tmp-*")` — exactly the evidence a human/sweep could notice and clean up.

**Production code change:** none — this scenario demonstrates the *absence* of a code path (no tool ever writes the live file in place), per the design's own framing, not code under test.

---

## Scenario #9 — Write-scope violation

> "Write-scope violation → a handoff manifest referencing a path outside
> the posting agent's write_scope → validation fails, loud flag, task
> untouched."

**Verdict:** COVERED — this is Gate 2's own finding-and-fix, with dedicated regression coverage already in place.

**Proven by:**
- `tests/test_queue_scripts.py::PostHandoffWriteScopeTests` (CLI level: out-of-scope artifact → `post-handoff.py` exits 1, writes nothing, emits no `HandoffPosted`)
- `tests/test_validate.py::WriteScopeTraversalTests` (validator level: a `../..` traversal path that resolves *outside* scope is rejected; one that resolves back *inside* scope is correctly accepted) — 2 methods

**Pass evidence:**
```
test_existing_coverage_passes (tests.test_failure_injection_matrix.Scenario09WriteScopeViolationTests.test_existing_coverage_passes) ... ok
```
Nested run: 3 tests (1 + 2), all `ok`.

---

## Scenario #10 — Rollback drill on an in-flight (leased) task

> "Rollback drill: archive an in-flight task and restore pre-task state;
> verify no orphaned locks or events referencing live work."

**Verdict:** NEW scenario, built this phase.

**Why new:** Gate 4's own rollback drill (`docs/GATE4-REVIEW.md`) exercised `docs/OPERATIONS.md` §3 against `t-cowork-seam-confirm`, which was **never leased** (`state=queued`, no lock file anywhere, including `_archive/`). "In-flight" specifically means a task that **is** currently leased — a materially different case that Gate 4 never exercised: the lock must also be relocated as part of the rollback.

**Built:** `Scenario10RollbackInFlightLeasedTaskTests`. Real setup: `lease-task.py` leases a fresh task for real (`--settle-sec 0` — a **test-only speed override**, not a claim about production timing; ADR-0003's real default settle window is 30s), confirming a genuine live lock file exists on disk; `post-handoff.py` then posts a real handoff referencing the in-flight task (`task_ref: t-scn10-leased`), so the "no dangling reference" assertion below is a real check, not a vacuous one. The test then executes `docs/OPERATIONS.md` §3's archive procedure **exactly as documented**, by hand, in the documented order: (1) set `state="archived"` on the live task via the atomic write-rename convention; (2) move the task file to `tasks/_archive/`; (3) move the still-present lock to `locks/_archive/`, and move the handoff referencing this task to `handoffs/_archive/`; (4) leave `events/aaos-events.ndjson` alone — never read or written anywhere in the drill method.

**Pass evidence:**
```
test_rollback_drill_on_leased_task_all_four_assertions (...) ... ok
```
All four of the design's assertions confirmed against real files on disk:
- **(a)** `tasks/_archive/t-scn10-leased.json` exists, `tools/validate.py` returns `VALID:` (exit 0) against it, `state == "archived"`, no `lease` key.
- **(b)** the lock no longer exists at its live path; it exists at `locks/_archive/t-scn10-leased.<timestamp>.lock` — moved, not deleted, not left dangling live.
- **(c)** the event log is byte-for-byte identical before and after (`events_after == events_before`), same line count, and the pre-existing `TaskLeased` event (from the real `lease-task.py` call) is confirmed still on record — no new event was fabricated to describe the manual archive, per §3's own "leave the event log alone" rule.
- **(d)** the handoff that referenced `t-scn10-leased` was relocated with it (no longer at its live path, present under `handoffs/_archive/`), the live `tasks/` directory no longer has an entry for this id, and a scan of every remaining live handoff confirms none still points at this now-archived task id — no dangling reference.

**Production code change:** none — `docs/OPERATIONS.md` §3's existing wording ("any lock file still present") was independently confirmed sufficient for the leased case; this drill is the proof, not a fix.

---

## Full suite

```
$ python -m unittest discover -s tests -p "test_*.py"
...
----------------------------------------------------------------------
Ran 169 tests in 10.969s

OK
```
152 pre-existing tests (Phases 1-4) + 17 new tests in `tests/test_failure_injection_matrix.py` = 169, all green. Breakdown of the 17: 6 covered-scenario citation classes (#1, #3, #5, #6, #7, #9) contribute 7 methods (1+1+1+2+1+1); 4 new-scenario classes (#2, #4, #8, #10) contribute 10 methods (3+5+1+1).

## Files touched this task

- `tests/test_failure_injection_matrix.py` (new)
- `tools/_aaos_common.py` — added `require_seam_reachable()`
- `tools/create-task.py`, `tools/lease-task.py`, `tools/complete-task.py`, `tools/fail-task.py`, `tools/post-handoff.py`, `tools/expire-leases.py`, `tools/emit-event.py` — each now calls `aaos.require_seam_reachable(args.seam_root)` as the first line inside `main()`'s `try:` block; `expire-leases.py` and `emit-event.py`'s docstrings' exit-code tables updated to mention the new exit-2 case.
- `docs/OPERATIONS.md` — **not touched by this task**; it already carried an uncommitted §5 from a separate, parallel Phase 5 work item at the time this session started (confirmed via `git diff`/`git log`); left as-is.
