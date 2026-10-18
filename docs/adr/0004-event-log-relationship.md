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

**Status of this amendment: PROPOSED. RED, docs-only. Not in force until Gabriel ratifies it and this PR merges.** It changes no code; each conforming change lands in a separate PR that cites this amendment's merge commit. Sources: audit findings M01, M07, M08, M22, M24, M25 and the hardened improvement plan (PR #13). Companion amendments: AMD-0003 (ADR-0003) and AMD-0006 (ADR-0006).

### A. The drain lock's stale takeover is a compare-and-swap, not an archive by path

The 2026-07-10 Revision above specifies an `O_EXCL` drain lock "with a staleness-based reclaim". The implemented reclaim archives whatever sits at the lock *path* (`os.replace`), so two drainers that both judge the same lock stale can both take it: the second moves the first's **live** lock aside, and both fold from different snapshots. Ownership of the drain lock therefore transfers by the **same single compare-and-swap primitive** ADR-0006 D2 defines for lease locks (see AMD-0006): an exclusively-created, expiring reclaim token held across read, judge and swap; the canonical lock path never vacated; and a release that is **content-verified** (a holder never deletes a successor's lock). A holder that discovers its lock was replaced stops folding and leaves its spool files pending.

### B. Restated fold invariant

The invariant "exactly one drainer folds" is not achievable on a synced folder and is replaced by one that is testable:

> **No event is ever absent from the union of the NDJSON log, `pending/` and `_drained/`; duplicates are removed by `event_id`; and the NDJSON is never replaced from a stale base.**

Mechanism: immediately before the final `os.replace` the drainer re-hashes the NDJSON; if it changed since the read, it aborts the fold and leaves the spool files pending. Spool files are moved to `_drained/` only after the replace succeeds.

### C. Ordering: `read_events` is the canonical ordered view

AAOS-SPEC section 8 says the log is "appended oldest-first". Within one drain batch events are folded oldest-first, but **file order across batches is not time order**: Code spool files are named `<time_ns>-<pid>-<uuid>.json` while Cowork's SAFE-BY-HAND recipe names them `<sortable-timestamp>-cowork-<random>.json`, which sort after every Code file, and the two surfaces stamp different UTC offsets. The canonical ordered view is `read_events`, which de-duplicates by `event_id` and sorts by **parsed** timestamp (ties keep NDJSON-then-pending order; unparseable timestamps sort last). Consumers MUST use it and MUST NOT rely on file position. A conforming change fixes the Cowork filename convention in COWORK-BOOTSTRAP and tolerates a late earlier-timestamp arrival.

### D. Inbound events are validated, prospectively

Events written by hand by a non-Code surface are folded today without schema, actor or trust validation. The drain MUST validate each pending file (schema, `actor` in the agent registry, `event_id` shape) with this rollout:

1. **Census (read-only).** The validator runs over a *copy* of the live log, `pending/` and `_drained/` and reports counts by reason. The copy requires Gabriel's in-turn approval.
2. **Shadow mode.** Invalid events are tagged, one aggregated `Escalated(reason=InboundInvalid, count=N)` is emitted per drain **after** the drain lock is released, built only from sanitized fixed fields, and the events are still folded.
3. **Hard mode** applies only to pending files drained after a **recorded cutover timestamp**. Anything before it is **flagged, never dropped**: `read_events` never filters, and rejected files go to `_corrupt/` with a supervised `readmit` path.
4. If the registry is unreadable the drain **skips** (events stay pending); it never rejects on a read failure.

### E. A committed event whose state rename fails is compensated, not left as a divergence

The "one remaining hairline case" above (event committed, state rename fails) is now specified. Before each retry the committer re-verifies that its lease is still held and that the state file is byte-unchanged (otherwise it aborts, which removes the lost-update hazard). The retry budget is elapsed time, not a count. On exhaustion the staged temp file is **kept for supervised replay** and `Escalated(reason=StateCommitFailed, refs.event_id=<the committed event>)` is emitted; `fail-task` and the projection treat the referenced event as superseded when deriving state, and the queue health check reports any remaining event-versus-state divergence as needs-action.

### F. Recorded deviation: the milestone mirror writes the human side only (ballot Q8, option A)

The Decision above requires each `event_log.md` milestone to have a paired NDJSON milestone event carrying `event_log_ref`. No producer exists, and adding one adds a new writer to the seam. **Deviation, with its reason:** the milestone roll-up writes only the `event_log.md` side (through `append-event-log.py`, citing the AAOS `event_id`/`task_ref`); the paired NDJSON event and `event_log_ref` field are deferred to V2. The link remains one-directional by reference.

## Consequences of this amendment

| File | Conforming change |
|---|---|
| `tools/_aaos_common.py` | Drain-lock acquire/release on the shared primitive (A); pre-replace re-hash guard (B); validation hook (D); commit retry and compensation (E) |
| `tools/emit-event.py`, `tools/fail-task.py`, `tools/project-notion.py` | Honour the `StateCommitFailed` supersession (E) |
| `schemas/event.schema.json`, `docs/MIGRATIONS.md`, `docs/COWORK-BOOTSTRAP.md` | Enumerated `refs.reason` vocabulary (`InboundInvalid`, `StateCommitFailed`), Cowork spool filename and bootstrap type-list sync (C, D, E) |
| `docs/AAOS-SPEC.md` section 8 | State the canonical ordered view (C); cross-reference this amendment |

## Verification (additions)

1. A forced interleaving (a rival steals the drain lock between the judge and the swap) leaves exactly one holder and every event present in the union (A, B). This is the invariant test pinned by `tests/test_drain_lock_interleaving.py`; its `@known_defect` marker must be removed by the PR that fixes it.
2. A pre-existing Cowork fixture corpus folds with zero quarantines in shadow mode; a 10 KB hostile filename cannot abort a drain (D).
3. A simulated multi-second sharing violation on the state rename ends in `StateCommitFailed` with the staged file retained (E).
