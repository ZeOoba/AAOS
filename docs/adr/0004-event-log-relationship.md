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

## Amendment — 2026-10-17 (AMD-0004: drain-lock ownership, fold safety, ordering, milestone mirror)

**PROPOSED. RED, docs-only. Not in force until the ratification record at the end of this section is filled in; merging without it ratifies nothing.** It changes no code. Each conforming change lands in a separate PR that cites the merge commit of the *ratified* text, except the two PRs named below, which land *before* ratification and are ratified retroactively with it (as PR #17 was for AMD-0003 section B). This amendment states **requirements only**; mechanisms are designed in the implementing PRs and proven by tests. **Scope was deliberately cut:** inbound-event validation and commit-failure roll-forward are **not** amended here (section D). Where the hardened plan (PR #13) disagrees with this text, this text will govern once ratified; see the plan's "Superseded by the amendments" section (PR #22). Companions: AMD-0003 and AMD-0006, both merged and awaiting their ratification records.

### A. Drain-lock ownership

The 2026-07-10 Revision above specifies an `O_EXCL` drain lock "with a staleness-based reclaim". The implemented takeover archives whatever sits at the lock *path*, so two drainers that judge the same lock stale can both take it, and the release is an unconditional unlink by path, so a stalled holder deletes its successor's lock. **Requirements:** the drain lock's takeover and release MUST satisfy the ownership-transfer requirements of AMD-0006 (no unconditional action by path; content verified; every remaining window documented and detected). Before replacing the NDJSON, a drainer MUST confirm it still owns the drain lock, and aborts (leaving its spool files pending) if it does not. *Not implemented today.*

### B. Fold safety and visibility

"Exactly one drainer folds" is not achievable on a synced folder. The requirements are:

1. **Never absent.** A committed event MUST at all times be present in at least one of: the NDJSON, `pending/`, `_drained/`, or `_corrupt/`. The invariant is "at least one", never "exactly one": an event is in **both** the NDJSON and `pending/` between the NDJSON `os.replace` and the move to `_drained/`, and stays in both after a crash in that window. Duplicates collapse by `event_id` (the drain skips ids already in the NDJSON, and `read_events` keeps the first occurrence).
2. **Loss is a detected defect.** An event present only in `_drained/` is a loss. A health check MUST reconcile `_drained/` against the NDJSON by `event_id` **and content hash**, and treat two files with the same `event_id` but different content as a defect (the second would otherwise survive only in `_drained/`). Repair is a supervised re-fold. An event present only in `_corrupt/` is **not** a loss: it is a quarantined state that MUST be surfaced (requirement 3) and recovered by a supervised readmit.
3. **Readers do not miss committed events.** Every event committed before a `read_events` call begins MUST be returned, **including when a drain moves it from `pending/` into the NDJSON during the call** (a drain that folds between the NDJSON read and the `pending/` listing currently hides the event; reproduced on `main`). The only exception is a file that cannot be read: it MUST be reported as a flagged unreadable entry, never silently skipped. `read_events` MUST also surface `_corrupt/` entries as flagged. The mechanism is left to implementation.
4. **A transient error never quarantines.** An `OSError` while reading or moving a pending file leaves it in `pending/` for the next drain. Only a file that was read and is structurally bad (not valid UTF-8, not JSON, not an object, or no `event_id`) is quarantined. **PR #21** implements this and is a pre-ratification conforming change. A quarantined file is recovered by moving it back to `pending/` (a supervised `readmit` tool does not exist yet; the quarantine warning names the file). A file the OneDrive client has only partly synced under its final name on another device is a residual case recovered the same way.
5. **Narrowing the stale-base window.** Before replacing the NDJSON a drainer SHOULD confirm that the NDJSON is unchanged since it read it (for example by comparing a hash) and abort otherwise. This narrows the stale-base window; it does not close it, and requirement 2 covers what remains. *Not implemented today.*

### C. Ordering

AAOS-SPEC section 8 says the log is "appended oldest-first". **File order is never time order**, within a batch or across batches: a batch is folded in file-name order; Code spool files are named `<time_ns>-<pid>-<uuid>.json` while Cowork's recipe names them `<sortable-timestamp>-cowork-<random>.json`, which sort after every Code file; and the surfaces may stamp different UTC offsets. The canonical ordered view is `read_events` (de-duplicated by `event_id`, sorted by **parsed** timestamp, ties keep NDJSON-then-pending order, unparseable timestamps sort last). Consumers MUST use it and MUST NOT rely on file position.

- **Timestamps MUST parse identically on every Python that runs AAOS tools**, which includes Cowork's sandbox (measured 3.10.12), including a trailing `Z`. **PR #23** implements this and is a pre-ratification conforming change.
- **Spool file names.** Cowork's spool names MUST begin with the decimal epoch time in nanoseconds, as Code's do, so that file-name order agrees across surfaces. This is a COWORK-BOOTSTRAP Recipe 2 change.
- **A spool file MUST NOT be visible under its final name until it is complete.** Mechanism (a `.part` name, then a no-clobber rename of one's own file) is a bootstrap change; its atomicity over the FUSE bridge is `UNVERIFIED` (AMD-0003 section A), and a half-written file that does appear under the final name is handled by requirement B.4. The drain already ignores `.part` names. Orphan `.part` files MUST be listed by the health check after a bound. The bootstrap section 7 rule ("create only a brand-new file") and AMD-0003 R1 must be amended to allow renaming one's own `.part` file.

### D. Not amended here: inbound-event validation and commit-failure roll-forward

Two independent reviews found that requirements for (i) validating inbound events (the actor rule, the in-flight/cutover rule, per-checkout registry differences) and (ii) rolling forward a committed event whose state rename fails (the `lease-task` rollback after `TaskLeased` commits, replay preconditions, `detail` length, reason vocabulary) are **not yet satisfiable as written**. They are designed in the A1c and A2 implementation PRs, with the review's findings as design inputs (plan section 16), proven by tests, and proposed as a **separate amendment** once proven. Until then ADR-0004's existing text governs, including the Decision's statement that the "event committed, rename failed" case "is caught by the next validation/sweep pass". **No such pass exists today**; that gap is recorded here, not fixed.

### E. Recorded deviation: the milestone mirror writes the human side only (ballot Q8, option A, reason corrected)

The Decision above requires each `event_log.md` milestone to have a paired NDJSON milestone event carrying `event_log_ref`. No automated producer calls it, but `emit-event.py --event-log-ref` writes such an event through the ordinary spool-and-drain writer today. **Deviation:** the milestone roll-up writes only the `event_log.md` side (through `append-event-log.py`, citing the AAOS `event_id`/`task_ref`); an automated caller of `--event-log-ref` is deferred to V2 so this effort adds no new automated seam writer.

## Consequences of this amendment

| File | Conforming change |
|---|---|
| `tools/_aaos_common.py` | Drain-lock acquire and release per AMD-0006; owner check and hash check before the NDJSON replace (A, B.5); reader race and unreadable-file reporting in `read_events`; `_corrupt/` surfaced (B.3); transient-error rule (B.4, **PR #21**); timestamp parsing (C, **PR #23**) |
| Queue health check (planned B1; **does not exist today**) | `_drained/` reconciliation by id and content hash (B.2); orphan `.part` listing (C) |
| Supervised `readmit` (**does not exist today**) | Recovery of quarantined files (B.2, B.4) |
| `docs/COWORK-BOOTSTRAP.md` Recipe 2 and section 7 | Epoch-nanosecond name prefix; `.part` then no-clobber rename of one's own file; allow that rename in the section 7 rule (C) |
| `docs/OPERATIONS.md` | A single unreadable pending file is not "every drain failing" (PR #21) |
| `docs/AAOS-SPEC.md` section 8 | State the canonical ordered view (C); cross-reference this amendment |

## Verification (additions)

1. A forced interleaving (a rival steals the drain lock between judge and act) leaves exactly one holder (A). `tests/test_drain_lock_interleaving.py` pins this (the two M01 `@known_defect` markers: steal and release); it asserts holder count and lock content, and the A1b PR adds the event-presence assertion and removes those two markers.
2. `read_events` called while a drain folds between its two reads still returns the event (B.3); an unreadable pending file is reported as flagged, not skipped.
3. A transient `OSError` leaves a file pending and it folds on the next drain (B.4, PR #21); two files with the same `event_id` and different content are reported by the reconciliation (B.2).
4. Timestamps with a trailing `Z` parse and order identically on Python 3.10, 3.11 and 3.13 (C, PR #23).

### Ratification record
Pending. Filled in by a docs commit on Gabriel's instruction, replacing this block with `Ratified by Gabriel, <date>, scope: <sections>, PR #19` and the PROPOSED banner at the top of this file's Status section. Merging this PR without it ratifies nothing (as happened with AMD-0003 and AMD-0006, whose records are completed separately).
