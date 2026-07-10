# 0003. Locking and leasing model

## Status

Accepted

## Context

The existing `chip-claims.log` pattern — a timestamped claim with a staleness window and explicit terminal states — is the direct precedent for AAOS leasing, and it is proven in production; a race incident is on record where a chip launched on two surfaces and double-ran before this pattern existed. That pattern, however, lives on local disk and is Claude-Code-internal only. AAOS leases must cross the OneDrive seam between a local process (Code) and a possibly cloud-hosted surface (Cowork). OneDrive resolves simultaneous writes to the same file by producing conflict copies — for example `<name>-<HOSTNAME>.lock` or `<name> (1).lock` — rather than by honoring atomic-exclusive-create semantics across two different writing surfaces. This is the one genuinely new hard problem introduced by choosing the shared-folder architecture in ADR-0001, and it must be met head-on rather than assumed away.

## Decision

**Lock file format.** Locks live at `projects/ora-ops/locks/<task-id>.lock` as a JSON file so tooling can parse them:

```json
{ "protocol_version": "1.0", "task_ref": "t-...", "holder": "cowork",
  "acquired": "2026-07-09T14:00:00-04:00", "expires": "2026-07-09T14:45:00-04:00",
  "session": "cowork/<session-id>" }
```

**Acquire protocol (fail-closed).** Acquiring a lease follows a strict, ordered sequence:

1. Read the task's `task.json`; require that `state` is `queued`, or that any existing lease has already expired — otherwise abort cleanly and emit a `LeaseDenied` event.
2. Stage the lock to a uniquely-named temp file: `<task-id>.lock.tmp-<agent>-<pid>`.
3. Attempt an atomic exclusive create of the final lock path (`O_CREAT | O_EXCL | O_WRONLY`), write it, and close it. If this fails because the file already exists, read the incumbent lock: if it is expired past its grace period, proceed to reclaim it as described below; otherwise lose cleanly — emit `LeaseDenied` and exit.
4. Settle: wait 30 seconds to allow for OneDrive propagation.
5. Re-scan the `locks/` directory. If any other `<task-id>*.lock` file exists with a different holder, or any file matching an OneDrive conflict-copy naming pattern exists, the lease is CONTESTED: move this agent's own lock to `locks/_archive/`, emit a `LeaseContested` event, and fail closed so that neither agent proceeds, posting a "block"-severity human escalation row.
6. Otherwise the lease is confirmed: set `task.state` to `leased`, write the `task.lease` object, emit a `TaskLeased` event, and proceed with work.

**Thresholds.** Three thresholds govern the protocol, each chosen for a specific reason:

- *Lease TTL = 45 minutes.* The existing `chip-claims.log` pattern uses 60 minutes, but that covers parked chips awaiting pickup, whereas AAOS leases cover active work turns, which run shorter. 45 minutes comfortably covers the longest single Cowork skill run observed in this environment — batch lease-abstraction runs and monthly-close, both tens of minutes — while bounding dead-agent recovery time to under an hour. The lease is renewable: the holder can request a renewal before expiry, emitting a `LeaseRenewed` event, for genuinely long-running jobs.
- *Clock-skew grace = 5 minutes.* A lock only becomes reclaimable once `now > expires + 5 minutes`, protecting against clock drift between the two surfaces; erring toward waiting longer is the fail-safe direction.
- *Settle window = 30 seconds.* This is long enough to reliably catch a same-machine cross-surface conflict copy, at roughly 1% overhead against a 45-minute lease — an acceptable cost given that correctness outranks speed in the binding priority order.

**Stale-lock reclamation.** *(Corrected in the Phase 5 independent code review — this paragraph previously read "reclaimable only when BOTH expiry AND a missed/unknown heartbeat," which never matched the shipped implementation and would have kept a genuinely expired lease unreclaimable for up to 16 hours whenever the coarse cowork heartbeat still read `fresh`. The policy below is, and was always intended to be, expiry-primary.)* A lock is reclaimable on expiry alone: `now > expires + 5 minutes` grace, with no renewal (no `LeaseRenewed` event) on record — full stop. The holder's projected `heartbeat.json` status does not gate reclamation; it only sets the reclaim's *classification* for escalation purposes — `missed`/`unknown` → `DeadAgent` (escalate); `fresh`/`stale` → `ExpiredLapsed` (routine, no escalation). Reclaiming means moving the stale lock file to `locks/_archive/<task-id>.<timestamp>.lock` — never deleting it — resetting `task.state` back to `queued`, and emitting a `LeaseExpired` event (plus an `Escalated` event for the `DeadAgent` case). This can be performed by the scheduled `expire-leases` sweep or opportunistically by any agent that encounters an expired lock while working. See `docs/AAOS-SPEC.md` §7 and `docs/PHASE3-DESIGN.md` §1.9 for the full reasoning.

**Fail-closed on ambiguity.** If a lock's `expires` field is missing or unparseable, if the `locks/` directory cannot be fully read, or if any conflict-copy file is present, no agent may steal the lock or proceed — the correct behavior is to flag loudly and escalate to a human. Ambiguity always resolves to "do not proceed," never "assume fine."

## Consequences

This design extends the existing `chip-claims.log` pattern — timestamp, staleness window, explicit terminal states — with a settle-then-rescan step specifically for the cross-surface OneDrive boundary that the chip system never had to cross. It is explicitly framed as an extension of a proven pattern, not a new locking philosophy invented from scratch. The 30-second settle delay is the honest, acknowledged price of choosing the shared-folder architecture in ADR-0001.
