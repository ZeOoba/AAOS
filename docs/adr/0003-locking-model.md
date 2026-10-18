# 0003. Locking and leasing model

## Status

Accepted

**Amended 2026-10-17 by AMD-0003 (PROPOSED, pending ratification; not in force until merged).** See the Amendment section at the end of this file.

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


## Amendment — 2026-10-17 (AMD-0003: locality, settle bound, routing)

**Status of this amendment: PROPOSED. RED, docs-only. Not in force until Gabriel ratifies it and this PR merges.** It changes no code. The dependent code and doc changes land in separate PRs that cite this amendment's merge commit. Sources: audit findings M01, M02, M38 and the L0 locality findings in the hardened improvement plan (PR #13, section 13). Companion amendments: AMD-0004 (ADR-0004) and AMD-0006 (ADR-0006).

### A. Locality: a Cowork session writes through a VM mount, not the seam's NTFS volume

The Context above calls Cowork a "possibly cloud-hosted surface". Evidence gathered read-only on 2026-10-17 shows otherwise:

1. All 529 Cowork sessions recorded on this machine (202 interactive, 327 scheduled) run locally and carry a per-session VM process; none is cloud-hosted. (The newer cloud-scheduled mode cannot reach local paths and is out of scope.)
2. The only live Code-to-Cowork round-trip (2026-07-10) reports running "path-translated to this session's mount" inside a "Cowork sandbox".
3. Code stamps timestamps with a local UTC offset; Cowork stamps UTC (`Z`).

**Precondition (new, explicit).** The atomicity of `O_CREAT|O_EXCL` create, `os.link` publish and `os.replace` is relied on only among processes running **directly on the seam's local NTFS volume** (Code, including the scheduled Code fleet). For a Cowork VM session writing through its folder mount, those guarantees are **UNVERIFIED**. `[PENDING: Cowork probe L0 (mount type, stat/link semantics, clocks) and the V1 soak (writes, distinct project id)]`. The compensating control is unchanged and becomes load-bearing: settle-then-rescan with conflict-copy detection (Acquire protocol steps 4-5) and fail-closed on ambiguity.

**Proposed rule R1 (RATIFICATION QUESTION).** Until the pending items resolve in favour of the mount, a Cowork VM session MUST NOT run any tool that creates, reclaims or releases a lock or that drains the event spool: `lease-task`, `complete-task`, `fail-task`, `expire-leases`, and any `emit-event` call that drains. It works SAFE-BY-HAND only (handoffs, spool files, new tasks per COWORK-BOOTSTRAP section 7). This narrows COWORK-BOOTSTRAP section 6, which today grants `MODE = FULL-EXEC` "full parity with Code"; a conforming PR would cap Cowork at SAFE-BY-HAND for every lock-mutating operation.

Options for Gabriel:

| Option | Effect |
|---|---|
| **(c) R1 now, lift it when the probe and soak pass (recommended)** | Closes the unverified-atomicity exposure today; costs nothing while Cowork already runs SAFE-BY-HAND in practice (its only recorded session did). |
| (b) R1 permanently | Simplest trust model; forgoes Cowork leasing even if the mount proves atomic. |
| (a) Keep FULL-EXEC | No change; relies on conflict-copy detection alone for cross-surface correctness. |

### B. Thresholds are unchanged; the settle must be shorter than the lease (clarification)

- The ratified constants (45-minute TTL, 5-minute skew, 30-second settle) are **unchanged** and are defaults, not floors. The CLI rejects only invalid values (zero or negative TTL, negative skew, negative or non-finite settle) plus loose sanity ceilings that prevent arithmetic overflow; the ceilings are implementation guards, not policy. The lease-length cap (`LEASE_TTL_CAP` 45) and expiry ceiling (90) remain ADR-0006 D3's.
- **Recorded non-change (M03):** the ratified 5-minute skew is *not* enforced as a floor on foreign-lease judging; a caller may pass `--skew-min 0`. This was considered and declined (ballot Q4, option A).
- **New clarification:** Acquire protocol step 4 (settle) MUST last strictly less than the lease TTL. The lock's `expires` is stamped at step 3, before the settle, so a settle at least as long as the TTL leaves the holder asleep on an already-reclaimable lock, and the conflict path can then archive a legitimate reclaimer's live lock. The default (30 s against 45 min) is unaffected. Enforced at the CLI by PR #17 (merged).

### C. Routing is enforced at acquisition (M38)

`owner_agent` and `to_agent` are never enforced today: any holder string can lease any task. At Acquire protocol step 1 the acquirer MUST verify that (i) `holder` is an active agent in `agent-registry.json`, and (ii) if `task.owner_agent` is not `unassigned`, `holder` equals it. An unreadable or invalid registry denies the acquisition (fail-closed) with a `LeaseDenied` event carrying the reason. Takes effect only when the conforming code merges.

## Consequences of this amendment

| File | Conforming change | PR |
|---|---|---|
| `docs/COWORK-BOOTSTRAP.md` section 6 | Cap lock-mutating operations at SAFE-BY-HAND (R1, option c), with the lift condition | after ratification |
| `tools/_aaos_common.py` `acquire_lease` | Routing check at step 1 (section C) | after ratification |
| `tools/lease-task.py` | Reject `--settle-sec >= --ttl-min x 60` | PR #17 (merged) |
| `docs/AAOS-SPEC.md` section 7 | Cross-reference this amendment | after ratification |

## Verification (additions)

1. Routing: a holder not in the registry is denied; a registered holder on a task owned by another agent is denied; an `unassigned` task is leasable by any registered holder; an unreadable registry denies.
2. Settle bound: `--settle-sec` equal to or above `--ttl-min x 60` exits 2 (covered by PR #17's tests).
3. R1, once ratified: a documented check that the Cowork bootstrap never instructs a lock-mutating tool in SAFE-BY-HAND mode.
