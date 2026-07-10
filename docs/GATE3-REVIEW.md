<!-- Copy this file when closing out any phase gate (Gate 0 through Gate 5). One checklist item per exit criterion named in the master prompt's Phase N section — do not paraphrase the criteria away. -->

# Gate 3 Review — Coordination Layer

**Status:** PASS (after one fix-and-reverify cycle)
**Reviewed by:** independent opus-tier task reviewer (fresh read, no prior authorship) for the final whole-phase adversarial pass; orchestrating session completed the fixes and independently re-ran the reviewer's own reproduction probes rather than trusting the fix report alone.
**Date:** 2026-07-10

## What shipped

`tools/_aaos_common.py` (the shared library: atomic write-rename, typed task/handoff/lock/heartbeat I/O, the ADR-0003 6-step lease-acquire protocol with injectable time/thresholds, `commit_state_change`/`assert_lease_held`, `reclaim_decision`/`archive_lock`, `emit_event`/`spool_event`/`drain_event_spool`); seven CLI scripts (`create-task`, `lease-task`, `complete-task`, `fail-task`, `post-handoff`, `expire-leases`, `emit-event`); `derive-heartbeat.py` and `project-notion.py` as pure local-compute scripts (no MCP calls, per the Problem-A boundary in `docs/PHASE3-DESIGN.md`). 139 tests, full suite green.

## Exit criteria checklist

- [x] **Full lifecycle demonstrated end-to-end on a synthetic task: created → leased → handoff → completed → archived, with a complete, valid event trail.**
  Evidence: run twice on the real seam (`Claude_Workspace\AAOS\projects\ora-ops\`) — once before the review (`t-gate3-demo`) and once fresh against the post-fix code (`t-gate3-postfix-demo`), since the event-emission internals changed materially during the fix cycle. Both produce the exact sequence `TaskCreated, TaskLeased, HandoffPosted, Completed, Archived`; both the tasks, the handoffs, and the full NDJSON event log pass `tools/validate.py` (exit 0) after the fix. The pre-fix artifacts were also re-validated against the post-fix schemas/validator and still pass — no schema drift.

- [x] **Lock contention test passes; stale-lock reclamation test passes; lease-expiry test passes.**
  Evidence: `AcquireLeaseSequentialCollisionTests`, `AcquireLeaseRaceTests` (threading + `Barrier`, exactly one ACQUIRED across repeated iterations), `AcquireLeaseContestedTests`, `AcquireLeaseExpiredReclaimTests`, `ReclaimDecisionTableTests`, `CorruptLockTests`, plus the `expire-leases.py` reclaim/dry-run/corrupt-lock test classes — all green in the 139-test suite. Beyond the unit suite, independently re-verified two of the review's own stress probes directly: (1) the reviewer's exact 8-process × 60-event concurrent-emission scenario — 480 expected, 480 landed, 0 lost, 0 duplicate ids; (2) the reviewer's exact sibling-task-id conflict-shape scenario (`t-a` vs `t-a-b` vs `t-z`) — `t-a` now acquires cleanly despite `t-a-b` holding its own live lock.

- [x] **Zero direct writes to fleet registries, hooks, or blast-radius files.**
  Evidence: `git diff --stat` across every Phase 3 commit touches only files under the AAOS repo — `tools/`, `tests/`, `docs/` (schemas and `agent-registry.json` were Phase 1/2 deliverables and were not touched in Phase 3). No hook, `settings.json`, `.env`, `*credentials*`, or scheduled-task registry file appears in any diff. At runtime, every script's default write target is the OneDrive `Claude_Workspace\AAOS\` seam (an intended coordination-state location, not a protected file); the two projection scripts' `--out` flags are explicit operator control, not autonomous writes elsewhere.

- [x] **Scheduled-task registration either completed via supervised tool with confirmation, or explicitly deferred with the command staged.**
  Evidence: `expire-leases.py` is built and locally tested as the candidate lease-sweep scheduled task per `docs/PHASE3-DESIGN.md` §2.6, but nothing in Phase 3 calls `create_scheduled_task` or touches either fleet's registry — registration is explicitly deferred to Phase 4/5 with in-turn confirmation, matching Hard Constraint 2 ("never splice scheduler registry JSON... registered ONLY via supervised `create_scheduled_task`").

## Findings from the final whole-phase adversarial review

The reviewer read all Phase-3 source fresh, re-ran the 119-test suite that existed pre-fix, and — critically — did not stop at reading the code: it wrote and ran 5 of its own adversarial probes against temp seam roots (concurrent event emission, concurrent sweep races, orphan-lock crash recovery, dry-run zero-writes) rather than accepting the design's stated guarantees on faith. Two probes broke the code on its own designed operating mode.

1. **[CRITICAL]** Event-log append race: the mtime-guarded read → append → `os.replace` scheme silently dropped events under concurrent emission — the reviewer's 8-process × 60-event probe lost >50% of events with no error surfaced anywhere. This directly threatened the "complete event trail" this gate requires, in exactly the concurrent Code+Cowork operation AAOS exists to support.
2. **[IMPORTANT]** The settle-rescan's conflict-copy detector flagged any filename merely *starting with* a task id as a conflict, so a sibling task whose id extended another's (`t-a-b` vs `t-a`, or any `--suffix-timestamp` id against its base) made the shorter-id task permanently un-leasable while the sibling held its own unrelated lock.
3. **[IMPORTANT]** Three related bugs in `expire-leases.py` under concurrent sweeps: a second sweep racing the same expired lock crashed with an uncaught `FileNotFoundError` at the archive step (the reclaim had already committed correctly, but the crash was a spurious scheduled-task failure); the crash could occur *before* the `DeadAgent` `Escalated` emission, silently dropping it; and `main()`'s exception handling only covered the library's own error types, so any other unexpected exception propagated as a raw traceback instead of a clean non-zero exit.
4. **[IMPORTANT]** `--dry-run`'s "zero writes" guarantee was violated whenever the event spool was non-empty (a leftover from a prior contended emit) — the startup drain ran unconditionally, before the dry-run check, so a preview run silently wrote to the durable event log.
5. **[MINOR]** `event_id` uniqueness relied on real-clock microsecond skew between processes; two processes emitting under an identical injected `now` (or, in principle, coincident real timestamps) could mint the same id, undermining dedup.
6. **[MINOR]** A corrupt/unreadable `heartbeat.json` returned `None` identically to a simply-absent one, with no signal distinguishing "never derived yet" from "degraded" — fail-safe, but silent.
7. **[NOTE]** A malformed file found during spool drain was discarded (deleted) rather than quarantined, in tension with Hard Constraint 1 ("never delete").
8. **[NOTE]** The double-run backstop (`assert_lease_held` immediately before `commit_state_change`) has a residual, non-CAS window between the check and the write; the reviewer assessed the realistic race as safe-if-ugly (duplicate terminal event, converges correctly) rather than corrupting, closed in practice by the expiry check itself.

## Resolutions

**Findings 1–4 and 6–7: FIXED**, each with a regression test reproducing the original failure, confirmed red before the fix and green after; Findings 1 and 2 additionally re-verified by independently re-running the reviewer's own exact probe scripts against the fixed code (not just the narrower automated tests) — see the evidence under the exit-criteria checklist above.

1. Redesigned the event-emission write path: spooling (unique-filename, collision-free by construction) is now the unconditional first step and the true commit point; folding the spool into the canonical NDJSON is a separate step serialized by an `O_EXCL` drain lock with staleness-based reclaim. Closes the race completely rather than narrowing the window. Revision documented in `docs/adr/0004-event-log-relationship.md` and `docs/PHASE3-DESIGN.md` §1.10.
2. Conflict-copy detection now disambiguates by the candidate lock's own `task_ref` field (already present in every lock, written by `acquire_lease`) rather than filename-prefix matching alone; a shape-match whose content names a different task is recognized as that task's own lock, not a conflict. `expire-leases.py` was aligned to resolve lock→task identity the same way.
3. `expire-leases.py`: added a pre-commit re-check that the loaded task is still actually leased (skip silently if a concurrent sweep already won); wrapped `archive_lock` in `try/except OSError` so an already-gone lock file doesn't crash the sweep, with the `DeadAgent` escalation moved to run unconditionally after that catch so it's never dropped by the race; added a final broad `except Exception` so no unexpected error escapes `main()` as a raw traceback.
4. Gated the startup `drain_event_spool()` call behind `if not args.dry_run`.
6. `load_heartbeat` now prints a loud `WARNING` to stderr when the file exists but fails to parse (the absent case remains silent, since it's the normal/expected "not derived yet" state) — the fail-safe `None` return contract for callers is unchanged.
7. Malformed spool files found during drain are now quarantined (moved to a dedicated subdir) rather than deleted.

**Finding 5: FIXED as part of Finding 1's redesign** — `event_id` generation now incorporates the process id and a random component alongside the timestamp, closing the cross-process collision case the concurrency fix itself would otherwise have made easier to trigger (many processes, same injected or coincident `now`).

**Finding 8: ACCEPTED WITH REASONING, not fixed** — documented as a residual-window note in `assert_lease_held`'s docstring rather than built around, since the reviewer's own analysis found the realistic outcome safe (a converging duplicate terminal event, never corruption), and the double-run risk this backstop exists to close is already closed for the scenario that actually matters (a lapsed holder attempting to write past its own expiry).

## Verdict

**PASS.** All four Gate 3 exit criteria hold with evidence; every design-review finding was fixed or explicitly accepted with reasoning — none silently dropped. The fix cycle itself was independently re-verified rather than taken on the fix report's word: the full 139-test suite was re-run by the orchestrating session, and the two most severe findings were each re-confirmed fixed by re-running the reviewer's own exact adversarial probe script against the corrected code, not a narrower substitute test. Per the master prompt's "hard gates, no skipping" doctrine, this session is stopping here — Phase 4 (Agent Integration: the Code adapter, `docs/COWORK-BOOTSTRAP.md`, the dry-run rehearsal, the first staged live cross-surface task) does not begin without review of this gate.
