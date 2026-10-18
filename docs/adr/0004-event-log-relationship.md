# 0004. Event log relationship to memory/event_log.md

## Status

Accepted

**Amended 2026-10-17 by AMD-0004 (PROPOSED, pending ratification; merging without the ratification record at the end of the Amendment section ratifies nothing).** See the Amendment section at the end of this file.

## Context

Two things must both be true simultaneously: AAOS needs a complete, machine-consumable event trail for its own operation, and, per the master prompt's own instruction, that trail must "link into event_log.md conventions rather than replacing them." The existing `memory/event_log.md` is a human-prose, newest-first, single-structural-writer log, appended to exclusively via `python ~/.claude/maintenance/append-event-log.py`; hand-editing or anchor-editing its header directly has caused three real collision incidents on record.

## Decision

**The machine log.** AAOS writes its own event trail to `projects/ora-ops/events/aaos-events.ndjson`, one JSON `event.json` object per line, appended oldest-first — the natural order for machine replay, and deliberately the opposite convention from the human-facing log, which serves a different reader. It is written only by a dedicated `emit-event.py` tool, which reuses the exact discipline already implemented in `append-event-log.py`: a modification-time-guarded read, then an append, then an atomic `os.replace`, with at most one retry on contention and then spooling to a per-writer spool file that a later drain step folds back in. This gives atomic write-rename semantics, append-only guarantees, and contention-safety — an explicit extension of the existing appender's mechanics, not a new I/O philosophy.

**Transaction ordering, with the event as the commit point.** For every state-mutating operation, the sequence is: (1) stage the new state to a `.tmp` file; (2) append the event — if this fails even after the one retry and spool fallback, discard the staged tmp file and report the operation as failed, failing closed, which matches the required Phase 5 hardening scenario for event-log append failure; (3) only then atomically rename the staged state file into place. No state file is ever renamed into place before its corresponding event has been durably logged. The one remaining hairline case — the event commits but the subsequent rename fails — is caught by the next validation/sweep pass as an event-without-matching-state divergence, and is escalated rather than silently ignored.

**Linkage to event_log.md without duplication.** The NDJSON file is the complete, authoritative record of every AAOS event. Only genuine milestones — a project completing, a version 1.0 release, a dead-agent escalation — get a short human-prose rollup appended to `memory/event_log.md`, using the existing `append-event-log.py --entry-file` interface, which produces the standard dated `## YYYY-MM-DD — ` header. Machine-level detail lives exclusively in the NDJSON stream; human-narrative milestones live exclusively in `event_log.md`; the full event stream is never duplicated into both places. The link between them is by reference only: each `event_log.md` milestone entry cites the specific AAOS `event_id`/`task_ref` it is summarizing, and the corresponding mirrored milestone event in the NDJSON carries an `event_log_ref` field pointing back.

## Consequences

AAOS never hand-edits `event_log.md` directly, avoiding the exact failure mode already on record, and it never floods that log with high-frequency lease/lock chatter — only genuinely narratable milestones reach the human-facing log.

## Revision — 2026-07-10 (Gate 3 adversarial review, Finding 1, CRITICAL)

The "Decision" section above described the NDJSON write path as mtime-guarded-read → append → `os.replace`, with spooling as a fallback used only when that path collided. **This was proven unsafe under real concurrency** — an adversarial-review stress probe of 8 concurrent processes emitting 60 events each lost over half of them, because the mtime guard only covers the read half of the cycle; the write-back window (including `atomic_write`'s `fsync`) is unguarded, and two concurrent local `os.replace` calls on the same path can both "succeed" while one silently clobbers the other's line. This is worse, not better, over the OneDrive seam, since `os.replace` is not even cross-host-atomic there.

The mechanism was redesigned: **spooling is now the unconditional first step and the true commit point** for every event — a uniquely-named spool file (incorporating pid + a random component, collision-free by construction across any number of concurrent writers on any number of hosts) is durably written before the NDJSON is touched at all. Folding the spool into the canonical NDJSON is a second, independent step, serialized by an `O_EXCL`-acquired drain lock (with a staleness-based reclaim so a crashed drainer can't wedge future drains); a process that can't acquire the drain lock simply leaves its event safely spooled for the next successful drain, by itself or anyone else. This still honors this ADR's core commitments unchanged — one NDJSON per project, oldest-first, no duplication into `event_log.md`, the same by-reference linkage — it only replaces the specific write-path mechanics that turned out not to hold under concurrent access, which is the very thing this coordination layer exists to support. Full account: `docs/PHASE3-DESIGN.md` §1.10 and `docs/GATE3-REVIEW.md`.

## Amendment — 2026-10-17 (AMD-0004: drain lock, fold safety, ordering, inbound validation, commit failure, milestone mirror)

**PROPOSED. RED, docs-only. Not in force until the ratification record at the end of this section is filled in; merging without it ratifies nothing.** It changes no code. Each conforming change lands in a separate PR that cites the merge commit of the *ratified* text. This amendment states **requirements and prohibitions**; the mechanisms (grandfathering inventory, staging layout, drain internals) are designed in the implementing PRs and proven by tests, because two reviews refuted mechanism-level text written here. Sources: audit findings M01, M07, M08, M22, M24, M25 and two independent adversarial reviews. Companions: AMD-0003 (ADR-0003), AMD-0006 (ADR-0006). Where the hardened plan (PR #13) disagrees with this text, this text governs; see the plan's "Superseded by the amendments" section.

### A. Drain-lock ownership

The 2026-07-10 Revision above specifies an `O_EXCL` drain lock "with a staleness-based reclaim". The implemented takeover archives whatever sits at the lock *path*, so two drainers that judge the same lock stale can both take it, and the release is an unconditional unlink by path, so a stalled holder deletes its successor's lock. **Requirements:** the drain lock's takeover and release MUST satisfy the ownership-transfer requirements of AMD-0006 (no unconditional action by path; content verified; every remaining window documented and detected). Before replacing the NDJSON, a drainer MUST confirm it still owns the drain lock; if it does not, it aborts and leaves its spool files pending.

### B. Fold safety and visibility

"Exactly one drainer folds" is not achievable on a synced folder. The requirements are:

1. **Never absent.** A committed event MUST at all times be present in at least one of: the NDJSON, `pending/`, `_drained/`, or `_corrupt/` (flagged). During a fold an event is legitimately in more than one place (in the NDJSON and still in `pending/` until it is moved).
2. **Lost is a detected defect.** An event present only in `_drained/`, or only in `_corrupt/`, is a defect, not a state: a health check reconciles `_drained/` against the NDJSON by `event_id`, and repair is a supervised re-fold. `_drained/` is an audit copy of events already folded.
3. **Readers do not miss committed events.** `read_events` MUST NOT miss an event that is in `pending/` or the NDJSON for the whole duration of the call. (A drain that folds between its NDJSON read and its `pending/` listing currently hides the event; reproduced during review.) It MUST also surface `_corrupt/` entries as flagged. Mechanism is left to implementation.
4. **A transient error never quarantines.** An `OSError` while reading or moving a pending file leaves it in `pending/` for the next drain. Only a file that was read and is structurally bad (not valid UTF-8, not JSON, not an object, or no `event_id`) is quarantined, and `readmit` recovers it. (A fix for the first part is PR #21.) A file the OneDrive client has only partly synced under its final name on *another* device remains a residual case, recoverable through `readmit`.
5. **The pre-replace NDJSON re-hash narrows the stale-base window; it does not close it.** The window is covered by requirement 2.

### C. Ordering

AAOS-SPEC section 8 says the log is "appended oldest-first". **File order is never time order**, within a batch or across batches: a batch is folded in file-name order; Code spool files are named `<time_ns>-<pid>-<uuid>.json` while Cowork's recipe names them `<sortable-timestamp>-cowork-<random>.json`, which sort after every Code file; and the surfaces may stamp different UTC offsets. The canonical ordered view is `read_events` (de-duplicated by `event_id`, sorted by **parsed** timestamp, ties keep NDJSON-then-pending order, unparseable timestamps sort last). Consumers MUST use it and MUST NOT rely on file position. Timestamps MUST parse identically on every supported Python (a trailing `Z` included; PR #23).

### D. Inbound events are validated, prospectively

Requirements, with the mechanism designed in the A1c implementation PR:

1. A pending event MUST pass schema validation, `event_id` shape, and an `actor` in **the agent registry or the literal `system`**.
2. **Code's own events never fail the actor rule at drain time:** `emit_event` validates the actor *before* spooling (today `emit-event.py --actor` is free text), so a Code-written file cannot be quarantined after its state change has committed.
3. Rollout is a read-only census on a *copy* (needs Gabriel's in-turn approval), then **shadow mode** (tag, aggregate, still fold; one aggregated `Escalated` per drain, actor `system`, emitted after the drain lock is released), then **hard mode**.
4. **Hard mode MUST NOT reject an event that was legitimately in flight at cutover**, and the cutover MUST NOT depend on drain time. How in-flight files are identified (an inventory taken at the cutover step, keyed by name and content hash, stored durably) is the implementing PR's design, and it MUST show: no loop on `readmit`; a reused file name does not bypass validation; files spooled between the census and the cutover are handled.
5. A rejected file is quarantined and surfaced (requirement B.3); history is flagged, never dropped. An unreadable registry makes the drain **skip**, never reject.

### E. A committed event whose state rename fails is rolled forward

The "one remaining hairline case" above (the event commits, the state rename fails) is specified by requirements, consistent with the event being the commit point:

1. **The staged state MUST be durable, discoverable and replayable.** It MUST NOT use the `.tmp-<pid>` name that ordinary atomic writes reuse (a later writer with the same PID can overwrite it) and MUST NOT be a file that `OPERATIONS.md` tells operators is "safe to delete". A detector (the queue health check) MUST find staged files left by a process that died before it could report.
2. **Verification before retry.** Every caller of `commit_state_change` (including `lease-task`, `create-task`, and the `expire-leases` sweep, which hold different preconditions) re-verifies its own precondition before each retry: a lease-bearing caller its lease (against the lock file, ADR-0006 D1) and an unchanged state file; a caller without a lease its state-file precondition. Failure aborts the retry and is reported. The retry budget is elapsed time, not a count.
3. **Reporting.** On exhaustion, and on a precondition abort, an `Escalated` event is emitted with `refs.reason` = `StateCommitFailed`, `refs.task` naming the task, and the committed event's id and the staged file's name in `detail`. **No new schema field is required** (`refs` is closed).
4. **No silent supersession.** The committed event is not treated as superseded. The divergence is reported as "committed, unapplied" until a supervised replay applies the staged state, or an operator **abandons** the transaction, which emits `Escalated` with `refs.reason` = `StateCommitAbandoned`; only abandonment makes `fail-task` and the projection treat the event as superseded.

### F. Recorded deviation: the milestone mirror writes the human side only (ballot Q8, option A, reason corrected)

The Decision above requires each `event_log.md` milestone to have a paired NDJSON milestone event carrying `event_log_ref`. No automated producer calls it, but `emit-event.py --event-log-ref` writes such an event through the ordinary spool-and-drain writer today. **Deviation:** the milestone roll-up writes only the `event_log.md` side (through `append-event-log.py`, citing the AAOS `event_id`/`task_ref`); an automated caller of `--event-log-ref` is deferred to V2 so this effort adds no new automated seam writer.

## Consequences of this amendment

| File | Conforming change |
|---|---|
| `tools/_aaos_common.py` | Drain-lock acquire and release per AMD-0006 and A; owner check before the NDJSON replace (A); the B requirements (reader race, reconciliation, no transient quarantine); validation hook (D); retry, staging and abandonment (E) |
| `tools/_aaos_common.py` `read_events` | Never miss a committed event; surface `_corrupt/` (B) |
| `tools/emit-event.py` and `emit_event` | Validate `actor` before spooling (D.2) |
| `tools/lease-task.py`, `create-task.py`, `complete-task.py`, `fail-task.py`, `post-handoff.py`, `expire-leases.py`, `project-notion.py` | Per-caller precondition re-check and `StateCommitFailed` / `StateCommitAbandoned` handling (E) |
| `schemas/event.schema.json`, `docs/MIGRATIONS.md` | Enumerated `refs.reason` (`InboundInvalid`, `StateCommitFailed`, `StateCommitAbandoned`; ballot Q5-A). No other schema change |
| `docs/COWORK-BOOTSTRAP.md` Recipe 2 | Write the spool file under a `.part` name and rename it, so a drain never sees a partial file; the drain ignores `.part` names; fix the file-name convention (C) |
| `docs/OPERATIONS.md` | Staged state files are never deleted by hand (E.1); a single unreadable pending file is not "every drain failing" (PR #21) |
| `docs/AAOS-SPEC.md` section 8 | State the canonical ordered view (C); cross-reference this amendment |

## Verification (additions)

1. A forced interleaving (a rival steals the drain lock between judge and act) leaves exactly one holder (A). `tests/test_drain_lock_interleaving.py` pins this; it asserts holder count and lock content only, and the A1b PR adds the event-presence assertion and removes the `@known_defect` marker.
2. `read_events` called while a drain folds between its two reads still returns the event (B.3); a transient `OSError` leaves a file pending and it folds next drain (B.4).
3. A pre-existing Cowork fixture corpus folds with zero quarantines in shadow mode, including events with actor `system`; an event pending at cutover is not rejected when drained afterwards; a reused file name does not bypass validation (D).
4. A simulated multi-second sharing violation on the state rename ends in `StateCommitFailed` with the staged file discoverable by the health check and the event not superseded; abandonment supersedes it; a process killed mid-commit leaves a detectable staged file (E).

### Ratification record
Pending. In a small commit made immediately before merge, this block is replaced with `Ratified by Gabriel, <date>, scope: <sections>, PR #19`, and the PROPOSED banner at the top of this file's Status section is replaced in the same commit. Code PRs cite that merge commit.
