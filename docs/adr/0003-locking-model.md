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

**Status of this amendment: PROPOSED. RED, docs-only. Not ratified until the ratification record at the end of this section is filled in.** It changes no code. The dependent code and doc changes land in separate PRs that cite the merge commit of the *ratified* text. Sources: audit findings M01, M02, M38 and the L0 locality findings in the hardened improvement plan (PR #13, section 13). Companion amendments: AMD-0004 (ADR-0004) and AMD-0006 (ADR-0006). Revised after an independent adversarial review (12 of 17 claims in the first draft were refuted; the corrections are folded in below).

### A. Locality: Cowork sessions run in a per-session VM; the path by which they reach the seam is unverified

The Context above calls Cowork a "possibly cloud-hosted surface". Read-only evidence gathered 2026-10-17 from this machine's Cowork session metadata (structure only) shows otherwise:

1. All 529 recorded Cowork sessions (202 interactive, 327 scheduled) run locally with a per-session VM process; none is cloud-hosted. (The newer cloud-scheduled mode cannot reach local paths and is out of scope.)
2. All 529 also record `hostLoopMode: true`, which suggests the agent loop, and possibly its file operations, run on the host rather than inside the VM. **Whether Cowork file operations reach the seam through a VM mount or directly on the host is not established.**
3. Only 6 sessions ever listed `Claude_Workspace` as a selected folder; the most recent was created 2026-07-20. The last session that selected both `Claude_Workspace` and the AAOS repo, and the only recorded Code-to-Cowork round-trip, was 2026-07-10. Cross-surface locking has not been exercised since.
4. The 2026-07-10 handoff on the seam (quoted from the live seam, not re-verifiable from this repo) says its probe ran "path-translated to this session's mount" in a "Cowork sandbox". Cowork-stamped events seen on the seam use a `Z` suffix; the bootstrap's own example uses `-04:00`, so the UTC offset is not guaranteed either way.

**Precondition (new, explicit).** The atomicity of `O_CREAT|O_EXCL` create, `os.link` publish and `os.replace` is relied on only among processes running **directly on the seam's local NTFS volume** (Code, including the scheduled Code fleet). For a Cowork session those guarantees are **UNVERIFIED**. `[PENDING: Cowork probe L0 (mount type, link and stat semantics, clocks, and whether file operations run in the VM or on the host) and the V1 soak (writes, distinct project id)]`. The compensating control is unchanged and becomes load-bearing: settle-then-rescan with conflict-copy detection (Acquire protocol steps 4-5) and fail-closed on ambiguity. ADR-0001's and `agent-registry.json`'s "cloud-hosted" description of Cowork is superseded by this section (noted here; ADR-0001 is not edited).

**Rule R1 (decided: option (c), Gabriel, 2026-10-17).** Until the lift condition below is met, a Cowork session MUST NOT run **any tool under `tools/` that writes the seam** (creates, replaces, moves or deletes a file there). That includes `lease-task`, `complete-task`, `fail-task`, `expire-leases`, `post-handoff`, `create-task` and `emit-event` (every `emit_event` call drains the spool, so there is no non-draining path), and `publish-contracts`. Read-only tools (for example `validate.py`) remain allowed. Cowork works SAFE-BY-HAND: the three recipes in COWORK-BOOTSTRAP section 7 (post a return handoff, spool one event file, create one new task), each of which only creates a new, randomly-suffixed file. This narrows COWORK-BOOTSTRAP section 6, which today grants `MODE = FULL-EXEC` "full parity with Code".

**R1 is honour-system until it is wired.** Nothing in the code distinguishes surfaces (a Cowork session can pass any `--holder`), and Cowork reads the bootstrap and the seam's contracts mirror, not this ADR. R1 binds in practice only when (i) COWORK-BOOTSTRAP sections 6 and 7 are edited and (ii) the contracts mirror is republished. Until both are done R1 is a stated rule, not an enforced one.

**Lift condition.** R1 is lifted only by a further explicit documentation change, never automatically, and only when **both** hold:
1. the L0 probe has reported the mount type, file-identity and link semantics `[PENDING]`, and nothing in it contradicts atomic exclusive-create, `os.link` and rename-over-existing for Cowork's file operations; and
2. the V1 soak has passed with Cowork as a contender. If the Cowork contender cannot be reproduced within the soak's two-session timebox, this condition is **not** met and R1 stays in force.

Alternatives considered and not chosen: (b) R1 permanently; (a) keep FULL-EXEC and rely on conflict-copy detection alone.

### B. Thresholds are unchanged; the requested settle must be shorter than the lease (clarification)

- The ratified constants (45-minute TTL, 5-minute skew, 30-second settle) are **unchanged** and are defaults, not floors. The CLI rejects invalid values (zero or negative TTL, negative skew, negative or non-finite settle) plus loose sanity ceilings that prevent arithmetic overflow; the ceilings are implementation guards, not policy. `LEASE_TTL_CAP` (45) and `LEASE_EXPIRY_CEILING` (90) remain ADR-0006 D3's.
- **Recorded non-change (M03):** the ratified 5-minute skew is *not* enforced as a floor on foreign-lease judging; a caller may pass `--skew-min 0`. Considered and declined (ballot Q4, option A).
- **Clarification:** the Acquire protocol's `expires` is stamped at step 3, before the step-4 settle, so a requested settle at least as long as the TTL leaves the holder asleep on an already-expired lock (reclaimable once the 5-minute grace passes), and the conflict path could then archive a legitimate reclaimer's live lock. The requested settle MUST therefore be strictly shorter than the TTL. This is enforced **at the CLI only** (`lease-task`, PR #17, merged); library callers of `acquire_lease` are not guarded, and a past `--now` can still mint an already-reclaimable lock. The defaults (30 s against 45 min) are unaffected.

### C. Routing is enforced at acquisition (M38, ballot Q11-A)

`owner_agent` is never enforced today: any holder string can lease any task. Before **any lock I/O** at the start of the Acquire protocol (so an unregistered holder can never archive an incumbent's expired lock before being denied), the acquirer MUST verify that (i) `holder` is an active agent in `agent-registry.json`, and (ii) if `task.owner_agent` is not `unassigned`, `holder` equals it. An unreadable or invalid registry denies the acquisition (fail-closed).

- **Denial event.** A routing denial emits `LeaseDenied` with actor `system` and the offending holder and reason in `detail`/`refs.reason`, never with the offending holder as the event's `actor`, because AMD-0004 section D validates `actor` against the registry and would quarantine it.
- **`to_agent` is deliberately not enforced here.** It is a handoff field; routing of handoffs belongs to inbound-trust validation (B3a). Deferral recorded.
- **Interaction with R1 (ballot, 2026-10-17: handoff-only).** While R1 holds, a task with `owner_agent: cowork` is **not leasable by anyone**: Cowork is barred from lock tools and Claude Code is denied by routing. This is intended. Cowork acts on such tasks only through handoffs and spool files (how its one recorded session worked); the task state advances by Code-side action on Cowork's return handoff. The routing check exempts nothing.
- Takes effect only when the conforming code merges.

## Consequences of this amendment

| File | Conforming change | When |
|---|---|---|
| `docs/COWORK-BOOTSTRAP.md` sections 6 and 7 | Cap Cowork at SAFE-BY-HAND for every seam-writing tool (R1), state the lift condition | after ratification |
| Seam contracts mirror (`publish-contracts`) | Republish the edited bootstrap so Cowork actually reads R1 | after the bootstrap edit |
| `agent-registry.json` cowork entry | Replace "cloud-hosted agent surface" with the verified description (local, per-session VM) | after ratification; republish with the mirror |
| `tools/_aaos_common.py` `acquire_lease` | Routing check before any lock I/O (section C) | after ratification |
| `schemas/event.schema.json` | `refs.reason` (ballot Q5-A) so the routing denial can carry a reason | with the S0 schema PR |
| `tools/lease-task.py` | Reject `--settle-sec >= --ttl-min x 60` | PR #17 (merged) |
| `docs/AAOS-SPEC.md` section 7 | Cross-reference this amendment | after ratification |

## Verification (additions)

1. Routing: an unregistered holder is denied before any lock file is touched (assert the incumbent's lock is byte-unchanged); a registered holder on a task owned by another agent is denied; an `unassigned` task is leasable by any registered holder; an unreadable registry denies; the denial event's actor is `system`.
2. Settle bound: `--settle-sec` equal to or above `--ttl-min x 60` exits 2 (covered by PR #17's tests).
3. R1: a documented check that the bootstrap never instructs a seam-writing tool outside the three SAFE-BY-HAND recipes, and that FULL-EXEC is capped as above, until the lift condition is recorded as met.

### Ratification record
Pending. In a small commit made immediately before merge, this block is replaced with `Ratified by Gabriel, <date>, scope: <sections>, PR #18`, and the PROPOSED banner at the top of this file's Status section is replaced in the same commit. Code PRs cite that merge commit.
