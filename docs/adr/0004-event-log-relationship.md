# 0004. Event log relationship to memory/event_log.md

## Status

Accepted

**Amended 2026-10-17 by AMD-0004 (PROPOSED, pending ratification; not in force until merged).** See the Amendment section at the end of this file.

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

## Amendment — 2026-10-17 (AMD-0004: drain-lock steal, fold invariant, ordering, inbound validation, commit failure, milestone mirror)

**Status of this amendment: PROPOSED. RED, docs-only. Not ratified until the ratification record at the end of this section is filled in.** It changes no code; each conforming change lands in a separate PR that cites the merge commit of the *ratified* text. Sources: audit findings M01, M07, M08, M22, M24, M25 and the hardened improvement plan (PR #13). Companion amendments: AMD-0003 (ADR-0003) and AMD-0006 (ADR-0006). Revised after an independent adversarial review (the first draft's fold invariant, validation rule and commit-failure semantics were each refuted; the corrections are folded in below).

### A. The drain lock's stale takeover and release use the shared ownership-transfer primitive

The 2026-07-10 Revision above specifies an `O_EXCL` drain lock "with a staleness-based reclaim". The implemented reclaim archives whatever sits at the lock *path* (`os.replace`), so two drainers that judge the same lock stale can both take it, and the release is an unconditional unlink by path, so a stalled holder deletes its successor's lock. Ownership of the drain lock therefore transfers, and is released, by the **single primitive specified in AMD-0006** (an exclusively-created, expiring reclaim token serializing every mutator of an existing lock file; a content-verified swap; a content-verified release). That primitive is **not** a true compare-and-swap: AMD-0006 records its residual window (a holder that stalls past the token's expiry between its last verification and the swap) as an accepted, bounded risk. A holder that discovers its lock was replaced stops folding and leaves its spool files pending.

### B. Restated fold invariant, defined over what readers can see

"Exactly one drainer folds" is not achievable on a synced folder. The invariant is:

> **Every committed event is, at every instant, present in exactly one of: the NDJSON log, `pending/`, or (flagged) `_corrupt/`. `_drained/` holds only spool files whose events are already in the NDJSON; it is an audit copy and never a place an event can exist alone. Duplicates are removed by `event_id` at read time.**

Mechanism, separating what is current from what is new:

- *Current, unchanged:* a spool file is moved to `_drained/` only after the NDJSON `os.replace` succeeded.
- *New:* immediately before the final `os.replace` the drainer re-hashes the NDJSON and, if it changed since the read, aborts the fold and leaves the spool files pending. This **narrows** the stale-base window; it does not close it (a second drainer can list `pending/`, miss an event spooled afterwards, and replace from its stale base after the first drainer has already moved that event to `_drained/`).
- *New:* `read_events` reports every `_corrupt/` entry as flagged (count and names) instead of ignoring the folder, and a health check reconciles `_drained/` against the NDJSON by `event_id`. Any `_drained/` event missing from the NDJSON is the residual loss; repair is a supervised re-fold (copy the file back to `pending/`).
- *New requirement:* **a transient error never quarantines.** An `OSError` while reading, parsing or moving a pending file (a OneDrive or antivirus sharing violation) leaves the file in `pending/` to be retried at the next drain. Only a file that is readable and fails JSON or schema validation goes to `_corrupt/`. (Today a single transient error moves a valid event to `_corrupt/`, where no reader looks; reproduced during review. It is fixed first, by a separate test-first PR.)

### C. Ordering: `read_events` is the canonical ordered view

AAOS-SPEC section 8 says the log is "appended oldest-first". **File order is never time order**, within a drain batch or across batches: a batch is folded in file-name order, Code spool files are named `<time_ns>-<pid>-<uuid>.json`, and Cowork's SAFE-BY-HAND recipe names them `<sortable-timestamp>-cowork-<random>.json`, which sorts after every Code file, and the two surfaces may stamp different UTC offsets. The canonical ordered view is `read_events`, which de-duplicates by `event_id` and sorts by **parsed** timestamp (ties keep NDJSON-then-pending order; unparseable timestamps sort last). Consumers MUST use it and MUST NOT rely on file position. A conforming change fixes the Cowork filename convention in COWORK-BOOTSTRAP and tolerates a late earlier-timestamp arrival.

### D. Inbound events are validated, prospectively

Events written by hand by a non-Code surface are folded today without schema, actor or trust validation. The drain MUST validate each pending file: schema, `event_id` shape, and `actor` in **the agent registry or the literal `system`** (the schema defines `system` for the sweep, and Code's own events use it; the aggregated escalation below is itself emitted as `system`). Rollout:

1. **Census (read-only).** The validator runs over a *copy* of the live log, `pending/` and `_drained/` and reports counts by reason. The copy requires Gabriel's in-turn approval. The census records an **inventory of the spool file names present at that moment**.
2. **Shadow mode.** Invalid events are tagged; one aggregated `Escalated` (`refs.reason` = `InboundInvalid`, the count in `detail`) is emitted per drain **after** the drain lock is released, built only from sanitized fixed fields; the events are still folded.
3. **Hard mode** applies only to spool files **not in the recorded inventory** (that is, first seen after the cutover). Files that were already pending at cutover are grandfathered: validated in shadow mode however late they are drained, so a legitimate event in flight is never rejected. A rejected file goes to `_corrupt/`, which `read_events` now surfaces as flagged (section B); a supervised `readmit` path moves it back to `pending/`.
4. If the registry is unreadable the drain **skips** (events stay pending); it never rejects on a read failure.

### E. A committed event whose state rename fails is rolled forward, not left as an unexplained divergence

The "one remaining hairline case" above (the event commits, the state rename fails) is specified as **roll-forward**, consistent with the event being the commit point:

- Before each retry, a lease-bearing caller (`complete-task`, `fail-task`, `post-handoff`) re-verifies its lease (against the lock file, per ADR-0006 D1) and that the state file is byte-unchanged; a caller with no lease (`create-task`, the `expire-leases` sweep) re-verifies only the state-file precondition (unchanged, or still absent for create). Failure aborts the retry. The retry budget is elapsed time, not a count.
- On exhaustion the staged temp file is **kept**, and an `Escalated` event is emitted with `refs.reason` = `StateCommitFailed` and `refs.event_id` = the committed event.
- The committed event is **not** marked superseded. The divergence is reported ("committed, unapplied") until a supervised replay applies the staged state, or an operator **abandons** the transaction, which emits `Escalated` with `refs.reason` = `StateCommitAbandoned`; only abandonment makes `fail-task` and the projection treat the event as superseded.
- **Schema.** `refs` is closed (`task`, `handoff`, `lock`, `escalation`). This section needs `refs.reason` (already ratified, ballot Q5-A) **and a new `refs.event_id` field, which is NOT yet ratified**; counts travel in `detail` as text, so no `count` field is needed. `[RATIFICATION REQUIRED: refs.event_id]`.

### F. Recorded deviation: the milestone mirror writes the human side only (ballot Q8, option A, reason corrected)

The Decision above requires each `event_log.md` milestone to have a paired NDJSON milestone event carrying `event_log_ref`. **No automated producer calls it**, but the means exist: `emit-event.py --event-log-ref` writes such an event through the ordinary spool-and-drain writer today. The earlier statement that a producer "adds a new writer to the seam" was wrong and is withdrawn. **Deviation, retained for a different reason:** the milestone roll-up writes only the `event_log.md` side (through `append-event-log.py`, citing the AAOS `event_id`/`task_ref`); wiring an automated caller of `emit-event.py --event-log-ref` is deferred to V2 so this effort adds no new automated seam writer. The link remains one-directional by reference.

## Consequences of this amendment

| File | Conforming change |
|---|---|
| `tools/_aaos_common.py` | Drain-lock acquire and release on the shared primitive (A); pre-replace re-hash guard, reconciliation, no quarantine on transient errors (B); validation hook and grandfathering inventory (D); retry, roll-forward and abandonment (E) |
| `tools/_aaos_common.py` `read_events` | Surface `_corrupt/` entries as flagged (B, D) |
| `tools/emit-event.py`, `tools/fail-task.py`, `tools/project-notion.py` | Honour `StateCommitFailed` / `StateCommitAbandoned` (E) |
| `schemas/event.schema.json`, `docs/MIGRATIONS.md`, `docs/COWORK-BOOTSTRAP.md` | Enumerated `refs.reason` vocabulary (`InboundInvalid`, `StateCommitFailed`, `StateCommitAbandoned`) and the new `refs.event_id` field; Cowork spool filename and bootstrap type-list sync (C, D, E) |
| `docs/AAOS-SPEC.md` section 8 | State the canonical ordered view (C); cross-reference this amendment |

## Verification (additions)

1. A forced interleaving (a rival steals the drain lock between the judge and the swap) leaves exactly one holder (A). The existing `tests/test_drain_lock_interleaving.py` pins this; it asserts holder count and lock content only. The A1b PR adds the event-union assertion (every event present in the NDJSON, `pending/` or flagged `_corrupt/`) and removes the `@known_defect` marker.
2. A simulated transient `OSError` on a pending file leaves it in `pending/` and it folds on the next drain (B).
3. A pre-existing Cowork fixture corpus folds with zero quarantines in shadow mode, including events with actor `system`; a legitimate event pending at cutover is not rejected when drained afterwards; a 10 KB hostile filename cannot abort a drain (D).
4. A simulated multi-second sharing violation on the state rename ends in `StateCommitFailed` with the staged file retained and the event not superseded; abandonment supersedes it (E).

### Ratification record
Pending. In a small commit made immediately before merge, this block is replaced with `Ratified by Gabriel, <date>, scope: <sections>, PR #19`, and the PROPOSED banner at the top of this file's Status section is replaced in the same commit. Section E's `refs.event_id` field is ratified separately and is not covered by ratifying the rest. Code PRs cite that merge commit.
