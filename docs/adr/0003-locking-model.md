# 0003. Locking and leasing model

## Status

Accepted

**Amended 2026-10-17 by AMD-0003 (ratified 2026-10-18; see the ratification record at the end of the Amendment section).** See the Amendment section at the end of this file.

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

## Amendment — 2026-10-17 (AMD-0003: locality, Cowork write rule, settle bound, routing)

**RATIFIED 2026-10-18 (see the ratification record at the end of this section). Docs-only.** It changes no code. Dependent code and doc changes land in separate PRs that cite the merge commit of the *ratified* text. This amendment states **requirements and prohibitions**; mechanisms are settled in the implementing PRs, with tests. Sources: audit findings M01, M02, M38, the L0 evidence, and two independent adversarial reviews of earlier drafts. Companions: AMD-0004 (ADR-0004), AMD-0006 (ADR-0006). Where the hardened plan (PR #13) disagrees with this text, this text governs; see the plan's "Superseded by the amendments" section.

### A. Locality: how Cowork reaches the seam

The Context above calls Cowork a "possibly cloud-hosted surface". Evidence (2026-10-17):

- **Sessions recorded on this machine:** 529 (202 interactive, 327 scheduled), all local, all with `hostLoopMode: true`. (Local metadata cannot record cloud-hosted sessions, so this says nothing about any other machine.)
- **Seam sessions:** only 3 sessions selected the `Claude_Workspace` root (the most recent 2026-07-10, which also selected the AAOS repo); three others selected a subfolder or a different OneDrive root. The only recorded Code-to-Cowork round-trip is from 2026-07-10 (a seam file; not re-verifiable from this repo). Cross-surface locking has not been exercised since.
- **Probe result (interactive session):** a Linux container (Ubuntu 22.04.5, **Python 3.10.12**, UTC clock) sees the seam at `$HOME/mnt/AAOS` as a **FUSE** filesystem with NTFS-style inodes and 100 ns timestamps. Sizes and times of two files agreed with a host listing, so the mount reaches the host's NTFS folder through a **FUSE bridge**; the inode was not compared.

**Precondition.** The atomicity of exclusive create, `os.link` and `os.replace` is relied on only among processes running directly on the seam's NTFS volume (Code, including the scheduled Code fleet). For a Cowork session going through the FUSE bridge it is **UNVERIFIED** `[PENDING: V1 soak. The read-only probe identified the mechanism; it cannot show atomicity]`. The compensating control is unchanged: settle-then-rescan, conflict-copy detection, and fail-closed on ambiguity. The "cloud-hosted" description of Cowork in this ADR's Context and in the `agent-registry.json` cowork entry is superseded by this section.

**Rule R1 (decided: option (c), Gabriel, 2026-10-17).** Until the lift condition is met, a Cowork session MUST NOT run **any tool under `tools/` that is not on an explicit allowlist of read-only tools** (an **allowlist, not a denylist**: a tool is denied until it is classified and added). The allowlist is `validate.py`, `check-contract-drift.py` (its header states it never writes) and any other tool the classification test shows writes nothing to the seam. `publish-notion.py` writes nothing to the seam (it posts to Notion, `publish-notion.py` header) but is **not** on the allowlist until the test classifies it, because an allowlist is default-deny. `derive-heartbeat.py` and `project-notion.py` are **not** read-only tools in this sense: by default they write under the seam (`heartbeat.json` and `<project>/notion-projection.json`), so they stay off the allowlist, as do `create-task`, `lease-task`, `complete-task`, `fail-task`, `post-handoff`, `expire-leases`, `emit-event` and `publish-contracts`; the test, not this list, is authoritative. A conforming test enumerates `tools/*.py` and fails when a tool is on the allowlist but contains a seam write, or is neither allowlisted nor classified. Cowork works SAFE-BY-HAND: the three recipes in COWORK-BOOTSTRAP section 7, each of which creates only a new, randomly-suffixed file (once AMD-0004 section C lands, Recipe 2 additionally renames its own `.part` file without clobbering; R1 permits exactly that rename and nothing else). **While R1 holds, the bootstrap's Probe X (COWORK-BOOTSTRAP section 2) MUST resolve to `MODE = SAFE-BY-HAND` unconditionally**, whatever the probe command's exit code: the probe command (`validate.py` on the registry) is itself allowlisted and exits 0, which today selects `FULL-EXEC` (section 6) and would hand Cowork every seam-writing tool.

- **R1 is honour-system until wired.** Nothing in the code distinguishes surfaces, and Cowork reads the bootstrap and the seam's contracts mirror, not this ADR. R1 binds in practice only after COWORK-BOOTSTRAP sections 6 and 7 are edited and the mirror is republished. Whether scheduled Cowork tasks embed their own copy of the bootstrap is `UNVERIFIED` and is a checklist item for that edit.
- **Lift condition (all must hold; lifted only by a further explicit documentation change):** (1) the access mechanism is identified (**met**, 2026-10-17); (2) the V1 soak has passed with Cowork as a contender, against a non-live seam root. If the Cowork contender cannot be reproduced within the soak's two-session timebox, the condition is **not** met and R1 stays in force.

### B. Thresholds are unchanged; the settle must be shorter than the lease

- The ratified constants (45-minute TTL, 5-minute skew, 30-second settle) are **unchanged** and are defaults, not floors. The CLI rejects invalid and absurd values (sanity ceilings are implementation guards, not policy). `LEASE_TTL_CAP` (45) and `LEASE_EXPIRY_CEILING` (90) remain ADR-0006 D3's. **Recorded non-change (M03):** the 5-minute skew is not enforced as a floor on foreign-lease judging (ballot Q4, option A).
- **Requirement:** the requested settle MUST be strictly shorter than the lease TTL. The lock's `expires` is computed when the lock is staged (the Stage step of `acquire_lease`, ADR-0003's step 2, `_aaos_common.py` ~:870; not the exclusive create of step 3), before the step-4 settle, so a settle at least as long as the TTL leaves the holder asleep on an already-expired lock. This guard was **shipped before this text is ratified**, as a CLI input validation (PR #17); ratifying this amendment ratifies it retroactively. It is CLI-only: library callers of `acquire_lease` are not guarded, and a past `--now` can still mint an already-reclaimable lock.

### C. Routing is enforced at acquisition (M38)

Before **any lock I/O** at the start of acquisition, the acquirer MUST verify that (i) `holder` is an active agent in `agent-registry.json`, and (ii) if `task.owner_agent` is not `unassigned`, `holder` equals it, **with this exception while R1 holds (ballot, 2026-10-17):** a task whose `owner_agent` is `cowork` is also leasable by `claude-code`, acting on Cowork's return handoff, because no tool can advance a task without a held lease and Cowork is barred from every lease tool. An unreadable or invalid registry denies the acquisition, and the failure MUST be reported as a denial rather than an uncaught error.

- **Denial event.** A routing denial MUST be recorded as an event whose actor is `system` (never the unregistered holder, which AMD-0004 would quarantine). The `holder` is caller-supplied free text and may be attacker-controlled (persisting such text in events is exactly what the reason code avoids; AMD-0004 section D does not amend inbound-event validation or the reason vocabulary), so the event MUST NOT carry the offending holder string, or any other caller-supplied text, in `detail` or `refs`. It carries a **fixed reason code** in `refs.reason` and a fixed `detail` string chosen by the code, never interpolated from input. If the holder must be identifiable for forensics, it MAY additionally be recorded as a truncated hash, or as a value restricted to a short fixed charset (mechanism left to the implementation). The routing reason codes are defined here and are `RoutingHolderUnregistered`, `RoutingOwnerMismatch` and `RoutingRegistryUnavailable`. They join the enumerated `refs.reason` list that hardened-plan item **S0** (event schema: fixed reason vocabulary) introduces, together with the codes the plan lists for the drain and commit paths (`InboundInvalid`, `StateCommitFailed`, `StateCommitAbandoned`). `refs.reason` does not exist in `schemas/event.schema.json` on main (`refs` is closed to `task`, `handoff`, `lock`, `escalation`), and AMD-0004 as merged does not introduce it, so S0 owns the one schema change; until S0 lands the routing denial cannot be emitted as specified.
- **`to_agent` is deliberately not enforced here.** It is a handoff field; routing of handoffs belongs to inbound-trust validation.
- **Cutover note.** Before the routing check is enabled, census the existing tasks' `owner_agent` against the holders that actually lease them (the events and lock files give the usual holder per task), read-only, and list every pair that the new rule would deny. A task whose `owner_agent` differs from its usual holder (for example an `owner_agent` set at creation to the other surface) would become unleasable on the day the check ships; resolve each by reassignment, with Gabriel's approval, before the enabling merge.
- Takes effect only when the conforming code merges.

## Consequences of this amendment

| File | Conforming change | When |
|---|---|---|
| `docs/COWORK-BOOTSTRAP.md` sections 6 and 7 | Cap Cowork at SAFE-BY-HAND for every tool not on the R1 allowlist (R1); state the Cowork seam path as `$HOME/mnt/AAOS`; Recipe 2 writes a `.part` file and renames it (AMD-0004) | after ratification |
| Seam contracts mirror | Republish the edited bootstrap; check scheduled Cowork tasks for embedded copies | after the bootstrap edit |
| `agent-registry.json` cowork entry | Replace "cloud-hosted agent surface" with the verified description (local container, FUSE bridge to the host folder) | after ratification |
| A test over `tools/*.py` | Enumerate `tools/*.py` against the R1 allowlist; fail when an allowlisted tool contains a seam write, or a tool is neither allowlisted nor classified (R1) | with the bootstrap edit |
| `tools/_aaos_common.py` `acquire_lease`, `tools/validate.py` `load_registry` | Routing check before any lock I/O; wrap registry read errors so the result is a denial, not exit 1 | after ratification |
| `tools/lease-task.py` | Emit the routing denial as actor `system` (it currently emits denials with `actor=holder`) | with the routing code |
| `docs/AAOS-SPEC.md` section 7 | Cross-reference this amendment | after ratification |
| `README.md` (diagram node, ~line 39: "Cowork FULL-EXEC: runs tools", and the `FULL-EXEC` edge ~line 47) and `docs/OPERATIONS.md` (the `owner_agent` description, ~line 49; the "two writers race" paragraph, which must list a routing denial as a new `DENIED` cause) | Remove or qualify the FULL-EXEC description while R1 holds; document the routing denial and the cowork-owned-task exception | after ratification |
| `docs/COWORK-BOOTSTRAP.md` section 2 (Probe X; the `FULL-EXEC` branch of section 6) | Probe X yields `SAFE-BY-HAND` unconditionally while R1 holds (R1); no instruction to use `FULL-EXEC` tools in section 6 while R1 holds | with the bootstrap edit |
| ADR-0003 Context text "possibly cloud-hosted surface" | Mark superseded by section A at the point of use (the base text is not rewritten by this amendment); the same phrase in the README overview | after ratification |
| An explicit Code-side path to advance a `cowork`-owned task | `assert_lease_held` is called by `complete-task.py` (~:120) and `fail-task.py` (~:107), and `post-handoff.py` requires the lease (~:195), so no tool advances a task without a lease while Cowork is barred from every lease tool. Candidates: (1) the section C routing exception for `claude-code` while R1 holds (the interim this text adopts); (2) a reassign-owner tool; (3) a handoff-ingest tool that moves the task on Cowork's return handoff without a Cowork lease. **OPEN: whether (2) or (3) replaces (1), and what removes (1) at the R1 lift, is the author's decision.** | needs a decision before the routing code merges |

## Verification (additions)

1. Routing: an unregistered holder is denied before any lock file is touched (the incumbent's lock is byte-unchanged); a registered holder on a task owned by another agent is denied; `claude-code` may lease a `cowork`-owned task while R1 holds; an `unassigned` task is leasable by any registered holder; an unreadable registry yields a denial, not an uncaught error; the denial event's actor is `system`.
2. Settle bound: `--settle-sec` at or above `--ttl-min x 60` exits 2 (PR #17's tests).
3. R1: the classification test exists and fails both on an unclassified tool and on an allowlisted tool that contains a seam write; the bootstrap never instructs a seam-writing tool outside the three recipes.

### Ratification record
**Cross-PR ratification order: ratify PR #20 (AMD-0006), then PR #19 (AMD-0004), then PR #18 (AMD-0003).** The three amendments cross-reference each other (AMD-0003 depends on AMD-0006 for the lock-ownership requirements and cites AMD-0004 section C for the `.part` rename that R1 permits; the `refs.reason` enum it needs belongs to plan item S0, not to AMD-0004; AMD-0004 depends on AMD-0006 for the drain-lock primitive), so any other order leaves a ratified text citing an unratified one.

Ratified by Gabriel, 2026-10-18, scope: A, B, C, PR #18. Recorded by Claude on Gabriel's explicit in-chat instruction of 2026-10-18; this commit is the ratification act. Code PRs cite this commit.
