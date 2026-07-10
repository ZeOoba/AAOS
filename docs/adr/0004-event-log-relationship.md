# 0004. Event log relationship to memory/event_log.md

## Status

Accepted

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
