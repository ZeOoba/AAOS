# AAOS Spec — ORA Agent Coordination Layer

## 1. Purpose & Scope

AAOS (Agent-to-Agent Operating Substrate) is a brownfield coordination layer through which Claude Code, Cowork, and future agents at Oxford Realty Advisors (ORA) discover work, claim it, execute it, hand it off, and report completion. It is built on top of ORA's existing infrastructure — filesystem, Git, Notion — never parallel to it. Every design tradeoff in this document is resolved by one binding priority order, applied in this sequence: **correctness → integration with existing systems → maintainability → reliability → autonomy → speed**. Where two options trade off against each other, the higher-priority concern wins even at the cost of the lower ones.

Vendor-agnosticism and extensibility are achieved entirely through contracts — the five JSON schemas plus `agent-registry.json`'s `write_scope` field — rather than through any negotiation machinery, compatibility layer, or plugin runtime. A new agent joins AAOS by adding one registry entry; nothing else in the system needs to change.

## 2. Non-Goals

`docs/NON-GOALS.md` is authoritative; this section summarizes it. AAOS explicitly does not build: distributed or multi-machine execution; a message bus or microservices layer (filesystem, Git, and Notion are the only transport); a plugin marketplace (the agent registry is one small static file); a parallel quality-scoring engine (the existing 16-point QC Gate and repo gates remain the quality system); protocol-negotiation machinery (a single `protocol_version` field per schema plus `docs/MIGRATIONS.md` covers evolution); a second memory system; or a second scheduler. Any future proposal that would cross one of these lines is routed to `docs/V2-BACKLOG.md` rather than folded into this spec or implemented ad hoc.

## 3. Architecture

AAOS uses a shared-folder-as-bus architecture. `Claude_Workspace\AAOS\` — OneDrive-synced — is the live coordination-state seam, evidenced (not yet live-confirmed) as reachable by both Code and Cowork without any new connector; Phase 0 found strong circumstantial evidence (pre-existing Cowork-authored content already living in that tree) but could not complete a same-session two-way round trip, so this remains a Phase 4 re-check before anything depends on it in production (see `docs/adr/0001-architecture-selection.md`). Notion is kept strictly as a read-only Derived mirror of queue status, never the primary store: the Notion API has no compare-and-swap or file-lock primitive, and cannot support the atomic-lease guarantees this system requires. Treating Notion as authoritative would violate correctness, the first item in the binding priority order, to buy dashboard convenience — the wrong trade.

The codebase is split across two trees for a concrete environmental reason. The AAOS codebase — schemas, validator tooling, docs, ADRs — lives in a separate Git repository *outside* OneDrive, at `C:\Users\Owner\AAOS\`. Putting a `.git` repository inside a OneDrive-synced folder is a documented corruption and lock hazard in this environment; it is the same reason ORA's canonical `ora-skills` git clone is already kept outside the OneDrive-synced `Claude_Workspace` tree. The live runtime seam under `Claude_Workspace\AAOS\` therefore holds no `.git` directory at all — only a stamped, read-only `contracts/` mirror published from the git tree by `publish-contracts.ps1`, plus the actual live coordination state (tasks, handoffs, locks, events).

A rejected alternative — Notion-as-primary-store — is documented in full in `docs/adr/0001-architecture-selection.md`, including a fair accounting of where it would have won: native Cowork write ergonomics and a unified dashboard out of the box. It was rejected on correctness grounds, consistent with the priority order governing this whole spec, not because those advantages were illusory.

## 4. Directory Layout & Namespacing

```
Git repo (Source of Truth for code + contracts) — C:\Users\Owner\AAOS\  (outside OneDrive):
  schemas/*.schema.json (5)
  tools/  (validate, create-task, lease-task, complete-task, fail-task,
           post-handoff, expire-leases, emit-event, derive-heartbeat,
           project-notion, publish-contracts)
  tests/  templates/  agent-registry.json (canonical, 2 entries)
  docs/   AAOS-SPEC.md (this file), CAPABILITY-MAP.md, NON-GOALS.md,
          V2-BACKLOG.md, OPERATIONS.md, COWORK-BOOTSTRAP.md, MIGRATIONS.md,
          adr/0001-0005-*.md

Seam (OneDrive-synced runtime, NO git) — Claude_Workspace\AAOS\:
  contracts/          (DERIVED, read-only mirror published from the git tree)
  projects/ora-ops/
    tasks/<id>.json       (Source of Truth)
    handoffs/<id>.json    (Source of Truth, write-once)
    locks/<id>.lock       (Ephemeral)  locks/_archive/  (reclaimed, never deleted)
    events/aaos-events.ndjson  (Source of Truth, append-only)
  heartbeat.json       (Derived projection)
  state/BUILD-STATE.md (build continuity)
```

Every queue, handoff, lock, and event path carries a `projects/<project-id>/` segment. Exactly one project is seeded in v1: id `ora-ops` ("ORA Cross-Surface Operations"), the home for real Code↔Cowork handoffs. This is not speculative scaffolding — Phase 0 already surfaced live friction of exactly this kind, a cross-fleet scheduled-task toggle that got stuck because one surface's tooling could not reach the other fleet. No multi-project machinery is built for v1; the path convention alone means a second project can be added later with no migration required, only a new directory. Full rationale: `docs/adr/0002-transport-mechanics.md`.

## 5. State Model

AAOS classifies every file into exactly one of three state classes. **Source of Truth** is the one place a given fact is authored. **Derived** is a read-only, timestamped projection of a Source of Truth held elsewhere — always markable as stale relative to its origin. **Ephemeral** is meaningful only transiently, principally leases and locks.

Classification by file: `agent-registry.json` and all docs/ADRs/schemas are Source of Truth (Git, human-edited). `contracts/` is Derived — published by `publish-contracts.ps1`, never hand-edited. `task.json` and `handoff.json` are Source of Truth in the seam: `task.json` is single-writer-enforced by whoever currently holds its lease; `handoff.json` is write-once. `<task-id>.lock` is Ephemeral. `aaos-events.ndjson` is Source of Truth, append-only, with exactly one structural writer, `emit-event.py`. `heartbeat.json` is Derived — projected from the existing health-monitoring chain, never independently emitted. The Notion mirror is Derived. `memory/event_log.md` milestone rollups are Source of Truth for human narrative, while themselves being a Derived *summary* of the AAOS event stream — the two facts are not in tension because they answer different questions (what happened, in machine detail, vs. what mattered, in prose).

Two conventions are mandatory for every Source-of-Truth and Derived file, without exception. First, atomic write-rename: write to a `.tmp` path, then `os.replace()` into place — the exact idiom already in production in `~/.claude/hooks/fleet-session-hook.py`. No AAOS tool ever writes a state file in place. Second, single-writer per file, enforced either structurally (one script owns the file) or via the lease (a `task.json`'s writer is whoever currently holds its lease).

## 6. Schemas

Five schemas live at `schemas/*.schema.json`, JSON Schema draft 2020-12, each carrying a `protocol_version` field.

`agent-registry.json` is the static roster: id, surface, capabilities, reachable_paths, invocation_method, `write_scope`, heartbeat_source, active. `write_scope` is the ownership-boundary control, enforced by `tools/validate.py` against every handoff's artifact manifest — no agent can hand off a write against a path outside its declared scope.

`task.json` carries id, project_id, title, owner_agent, state (queued/leased/in-progress/blocked/done/failed/archived), priority, lease (holder/acquired/expires/session), inputs (pointers only, never embedded data), acceptance_criteria, evidence (required non-empty before state can transition to done), blocked_reason, and timestamps.

`handoff.json` carries from_agent, to_agent, task_ref, artifact_manifest (path, hash, and byte count per artifact — every path checked against the sender's write_scope, the colonization guard), context_summary (≤300 words, pointer-first rather than restating content), required_next_action, blockers (a non-empty list forces the task into blocked state), and suggested_next_owner (advisory only, never binding — routing authority stays with the registry, not with agent suggestion).

`event.json` is one record per line of the append-only NDJSON log: event_id, type (an enum of TaskCreated/TaskLeased/LeaseRenewed/LeaseExpired/LeaseDenied/LeaseContested/HandoffPosted/Blocked/Completed/Failed/HeartbeatMissed/Escalated/Archived), actor, timestamp, project_id, refs (pointers to the task/handoff/lock/escalation involved), detail, and an optional event_log_ref for milestones mirrored to `memory/event_log.md`.

`heartbeat.json` is a Derived, per-agent projection: agent_id, last_run (nullable), source (naming exactly which upstream signal it was read from, proving no new emitter exists), status (fresh/stale/missed/unknown — unknown is never treated as fresh), threshold_hours (always inherited from the upstream signal, never invented by AAOS).

Every field across all five schemas was assigned a named consumer during design — a specific tool, agent, or human process — so no speculative fields exist. Full field lists with types and required/optional status live in the schema files themselves; this section is a summary, not the reference.

## 7. Locking & Leasing

Acquiring a lease is a six-step protocol. (1) Check the task is queued, or its existing lease has already expired. (2) Stage the lock to a uniquely-named temp file. (3) Attempt an atomic exclusive create of the real lock path — on collision, either reclaim an already-expired incumbent or lose cleanly. (4) Wait a 30-second settle window, an allowance for OneDrive propagation delay. (5) Re-scan the locks directory for any other holder's lock or any OneDrive conflict-copy file; if found, the lease is CONTESTED — fail closed, archive this agent's own lock, and escalate. (6) Otherwise the lease is confirmed and work proceeds.

Three thresholds are fixed, each justified against the binding priority order. Lease TTL is **45 minutes, renewable before expiry** — long enough to cover the longest observed single-session job while bounding dead-agent recovery to under an hour. Clock-skew grace is **5 minutes**, protecting against drift between the two surfaces and erring toward waiting longer rather than reclaiming too early. The settle window is **30 seconds**, which catches cross-surface conflict copies at roughly 1% overhead against a 45-minute lease — a cost accepted because correctness outranks speed in the binding priority order.

Stale-lock reclamation triggers on hard expiry alone: the lock is past expiry-plus-skew with no renewal on record (a renewal would have bumped `expires` — renewal is the fine-grained liveness proof for active work). The holder's heartbeat does not gate reclamation; it sets only the escalation classification — missed/unknown classifies the reclaim as DeadAgent (additionally escalated), fresh/stale as ExpiredLapsed (routine, no escalation). Gating on the heartbeat would let the coarse 16-hour cowork beacon keep a genuinely expired lease un-reclaimable for hours, wedging the queue; the double-run risk of reclaiming from a live-but-lapsed holder is closed instead by the commit-time lease check every mutating script runs (`assert_lease_held`). *(Wording corrected in Phase 3 — this section originally required expiry AND missed-heartbeat, contradicting ADR-0003's "past its grace period with no renewal on record"; see PHASE3-DESIGN.md §1.9 for the resolution.)* Reclaiming always archives the old lock file — it is never deleted — and resets the task to queued.

The fail-closed rule is unconditional: any ambiguity in a lock file — unparseable expiry, an unreadable locks directory, a conflict copy present — means no agent may proceed. Flag loudly, escalate to a human, never assume fine. This protocol extends the existing `chip-claims.log` pattern already in use (timestamped claim, staleness window, explicit terminal states) rather than inventing a new locking philosophy from scratch. Full detail: `docs/adr/0003-locking-model.md`.

## 8. Event Log & Linkage

The complete machine-readable event trail is `projects/ora-ops/events/aaos-events.ndjson`, one `event.json` object per line, appended oldest-first, written only by `emit-event.py`. That tool reuses the exact atomic-append-with-retry-then-spool discipline already implemented in `~/.claude/maintenance/append-event-log.py`, rather than inventing a new append mechanism.

Transaction ordering is a firm rule: every mutating operation stages its new state, then appends the event — a failure here aborts the whole operation, since the event write is part of the transaction, never best-effort — and only then renames the staged state into place. The event append is the commit point of the operation, not an afterthought logged once the "real" write succeeds.

Linkage to `memory/event_log.md` is deliberately narrow: only genuine milestones — a project completing, a v1.0 release, a dead-agent escalation — get a short human-prose entry there, written via the existing `append-event-log.py --entry-file` interface and never hand-edited directly. The full event stream is never duplicated into `event_log.md`; the two logs stay linked by cross-reference instead — an `event_log_ref` field on the AAOS side, and an explicit event_id citation on the `event_log.md` side. Full detail: `docs/adr/0004-event-log-relationship.md`.

## 9. Heartbeat Derivation

`heartbeat.json` is a pure, read-only projection. `derive-heartbeat.py` never pings anything and never creates a new monitoring emitter, in keeping with the binding Non-Goal against a second monitoring system.

Two upstream sources feed it, one per surface. For `claude-code`: `fleet-session-hook.py` session state, plus `fleet-status.py` dual-fleet health. For `cowork`: the existing Notion heartbeat page's `last_edited_time`, plus the `automation-health-monitor` v1.6 beacon table.

Every threshold used — including the 16-hour cowork watchdog canary — is read from its upstream signal, never hardcoded independently by AAOS, so an upstream threshold change is honored automatically without an AAOS code change. The fail-closed default applies here too: an unreadable upstream signal produces status `unknown`, and `unknown` is never treated as equivalent to `fresh`. Full detail: `docs/adr/0005-heartbeat-derivation.md`.

## 10. Failure Semantics

Lease expiry is 45 minutes, renewable. Retry policy is a maximum of one automatic retry on a transient failure, then flag loudly rather than halting silently or retrying indefinitely — matching the existing firm-wide convention already applied to the Outlook COM email send path.

Dead-agent recovery runs as a sequence: an agent's heartbeat shows missed or unknown; its held leases pass their expiry-plus-skew grace; the expiry sweep archives the stale locks and resets the affected tasks to queued; an event is emitted; and a human-escalation row is posted naming the dead agent and the reclaimed tasks. There is explicitly no automatic cross-surface reassignment of a task to a different agent — that would violate the write_scope/routing model. `suggested_next_owner` remains advisory only, never binding.

The human-escalation row is posted as a Notion Ops Hub child page — riding the existing single email-send path if an alert is warranted, ggoldberg-only routing, no new send path introduced — and carries: escalation_id, raised_at, severity (info/warn/block), trigger (an enum including LeaseContested, DoubleFailure, DeadAgent, CorruptState, WriteScopeViolation, ProtocolMismatch, SeamUnavailable), project_id, task_ref, agents_involved, summary, evidence_ref, recommended_action, status (open/acknowledged/resolved), resolved_at/resolved_by.

## 11. Extensibility

A new agent joins AAOS by adding exactly one entry to `agent-registry.json`, with a `write_scope` declared — nothing else is required, and nothing else is built to support it. Protocol evolution is handled by a single `protocol_version` field per schema plus `docs/MIGRATIONS.md` as a manual, documented process; no compatibility-matrix machinery exists or is planned for v1. This is the entire extensibility story for this spec; anything beyond it belongs in `docs/V2-BACKLOG.md`, not here.

## 12. Operations & Glossary

Operational procedure — how a human inspects the queue, breaks a stuck lock, archives a task, or rolls back a protocol version — is a Phase 2 deliverable documented in `docs/OPERATIONS.md`, per the deliverables manifest in the master prompt.

**Glossary**

- **Source of Truth** — the one place a fact is authored.
- **Derived** — a read-only, timestamped projection of a Source of Truth, always stale-markable.
- **Ephemeral** — meaningful only transiently (locks, leases).
- **Lease** — a time-bounded exclusive claim on a task.
- **Handoff** — a structured, write-once artifact-and-context transfer between two named agents.
