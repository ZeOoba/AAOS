> **Provenance note (added 2026-07-09 by the executing Code session):** this file was supplied inline in the chat instruction "Execute /mnt/user-data/outputs/2026-07-09_AAOS_MasterPrompt_v02.md [after moving it into the workspace]." That source path does not exist on this machine (`/mnt/user-data/outputs/` is a sandboxed-tool convention, not a local Windows path — verified via `ls`, confirmed absent). Since the full prompt text was pasted directly into the instruction, this file is a direct transcription of that pasted text (not a byte-copy of an actual source file, because none was reachable), staged here as the canonical spec input per the prompt's own Phase 2 instruction to place AAOS artifacts "under the Claude-system free working surface — Claude_Workspace or brain repo." No content below this line was altered from the original instruction.

---

0. MISSION

Design and implement the ORA Agent Coordination Layer — the coordination substrate through which Claude Code, Cowork, and future agents discover work, claim it, execute it, hand it off, and report completion — built on top of, not parallel to, the existing ORA infrastructure.

This is a brownfield integration project wearing an operating-system name. Treat it as such. The single most likely failure mode of this build is constructing a second copy of something that already exists. Every phase below is gated against that failure mode first.

Priority order (binding): correctness → integration with existing systems → maintainability → reliability → autonomy → speed.


1. HARD CONSTRAINTS (inherited — violations halt the build)


Never delete — move to /archive/. Never overwrite any config (settings.json, CLAUDE.md, hooks, registry JSON) without reading it first.
Never splice scheduler registry JSON. Scheduled tasks are registered ONLY via supervised create_scheduled_task on the target surface. The AAOS may read both fleet registries; it may never write them directly.
Blast-radius files are out of scope for autonomous edits: hooks/**, *.env*, *credentials*, confidential-terms.json, settings.json, CSP/headers, financial-constant/governor files. If the design requires touching one, stop and ask with a specific diff proposal.
No external sends. The AAOS emits internal artifacts and Notion rows only. Any email pathway routes through the existing COM canon (send-via-outlook.ps1, ggoldberg-only routing) — the AAOS never introduces a new send path.
oxfordra.net repos and Netlify site are untouchable under this project. No exceptions.
Never fabricate state. If a registry, log, or config cannot be read, the correct output is "unreadable — here is what I tried," not a plausible reconstruction.
One fact, one home. No AAOS file may duplicate a fact whose source of record already exists (MEMORY.md, session context, fleet registries, Notion canon DBs, Global Operating Prompt). AAOS files hold pointers to sources of record, plus coordination state that has no other home.
Fail closed. Any lock, lease, schema validation, or gate that errors resolves to "do not proceed," never "assume fine."



2. NON-GOALS (explicit — do not build, do not design for)

Write these into docs/NON-GOALS.md in Phase 1 and defend them in every design review:


Distributed/multi-machine execution. All agents run on one Windows machine or Anthropic-hosted surfaces. No network transport, no remote workers, no containers.
Message buses, queues-as-services, microservices. The filesystem + Git + Notion are the transport. Full stop.
Plugin marketplace / dynamic capability negotiation. The agent registry is a static, hand-edited JSON file with ~2–5 entries. Capability "discovery" is reading that file.
Artifact quality-scoring engines. The existing 16-point QC Gate and repo gates are the quality system. Do not build a parallel grader.
Protocol version negotiation machinery. A single protocol_version field in every schema, plus a MIGRATIONS.md, is sufficient. No compatibility matrices.
A second memory system. Memory stays in MEMORY.md / event_log.md / session context. AAOS state files hold coordination state only (queue, leases, handoffs, heartbeats).
A second scheduler. The dual fleet (CC/MCP + LAMS/Cowork) remains the only scheduler. AAOS tasks that need recurrence are registered there via the supervised tool.


Anything on this list proposed later goes to docs/V2-BACKLOG.md with a one-line justification — it is not built now.

On vendor-agnosticism (the seed prompt's central demand): it is achieved entirely by the contracts, not by machinery. Any future agent that can read/write JSON at the seam and honor the five schemas can join by adding one registry entry with a write_scope. That sentence is the whole extensibility story; do not build more of one.


3. OPERATING DOCTRINE FOR THIS BUILD


Lane: This is an L3 /build program executed in phases; announce lane and dispatch per EOS doctrine at the start of every session.
Pre-flight failure analysis before every phase and every non-trivial step: name the top 2–3 failure modes (stale/misread input · wrong target · already-exists/already-done · hidden dependency · guardrailed surface) and verify each against ground truth before acting. Trust observation over memory.
TDD is mandatory for all executable code (validators, lock managers, queue operations, adapters): test written first, watched to fail, then implemented. Config/templates/docs exempt. Typecheck 0, lint 0, full suite green before any phase-exit claim.
Evidence before "done." Every completion claim carries command output. "It should work" is a defect.
ADR for every architectural choice in docs/adr/NNNN-title.md — including every "reuse existing system instead of building" decision. The reuse ADRs are the most important artifacts of Phase 0–1.
Smallest surgical change. When integrating with an existing file or system, the diff is minimal and the original is archived first.
Session continuity: this build spans multiple sessions. At the end of every session, write aaos/state/BUILD-STATE.md — current phase, gate status, open items, next action. At the start of every session, read it before anything else. Never re-derive build state from memory.



4. PHASE PLAN — HARD GATES, NO SKIPPING

Phases execute strictly in order. Each phase ends with a Gate Review: a written checklist with evidence per item. A failed gate item blocks the next phase — no partial credit, no "will fix in the next phase."


PHASE 0 — INVENTORY & RECONCILIATION (no design permitted until this gate passes)

Objective: Map every existing system that overlaps AAOS scope, so the design reuses rather than duplicates.

Tasks:


Enumerate and read (read-only): both fleet scheduled-task registries; MEMORY.md + memory directory index; event_log.md; the dead-man heartbeat chain; automation-health-monitor skill; Cowork Global Settings v1.0 (+ Amendment No. 1 if ratified); the Engineering OS spec (~/.claude/docs/superpowers/specs/2026-06-05-engineering-operating-system-design.md); memory/rules.md; hooks inventory; Notion Ops Hub schema (32b6ecd8); Claude_Workspace/01_Workspace/CLAUDE.md.
Produce docs/CAPABILITY-MAP.md: a table mapping every AAOS concept (registry, queue, handoff, heartbeat, event log, memory, governance, drift detection, observability) → the existing system that provides it, its file path/ID, its freshness, and one of three verdicts: REUSE / EXTEND / BUILD (BUILD requires an ADR proving no existing system can be extended).
Identify integration seams: exactly where Cowork can read/write shared state today (SharePoint? OneDrive Claude_Workspace? Git repo? Notion?). Verify each seam empirically — write a probe file from Code, confirm the path is one Cowork can actually reach per its documented mount points. Do not assume.
Identify conflicts: any place where two existing systems already disagree (e.g., stale mirrors, orphaned tasks). Log them; do not fix them in this phase.


Gate 0 exit criteria (all with evidence):


 Capability map complete; every AAOS concept has a verdict.
 ≥80% of concepts resolved REUSE or EXTEND, or an explicit written justification for why this environment is an exception.
 Integration seam empirically verified with a probe artifact both surfaces can reach.
 Zero writes performed to any existing registry, config, or memory file.



PHASE 1 — ARCHITECTURE & SPECIFICATION

Objective: A complete, bounded specification. Hard ceiling: the core spec (docs/AAOS-SPEC.md) ≤ 3,000 words excluding schemas. If it doesn't fit, the design is too big — cut per Non-Goals.

Tasks:


Compare exactly two candidate architectures (e.g., Git-repo-as-bus vs. shared-folder-as-bus with Notion mirror), score against the priority order in §0, select one, record the ADR. Do not compare five.
Define the contract-first schemas (JSON Schema, draft 2020-12), each with protocol_version:

agent-registry.json — static roster: agent id, surface, capabilities (enumerated strings), reachable paths, invocation method, write_scope (path globs the agent may write; the ownership-boundary control). Enforcement is validator-side: any handoff whose artifact manifest contains paths outside the posting agent's write_scope fails validation and is flagged loudly — agents cannot silently colonize each other's territory.
task.json — id, title, owner-agent, state (queued/leased/in-progress/blocked/done/failed/archived), priority, lease (holder, acquired, expires), inputs (pointers, never embedded data), acceptance criteria, evidence.
handoff.json — from-agent, to-agent, task ref, artifact manifest (paths + hashes), context summary (≤300 words, pointer-first), required next action, blockers (empty array permitted; non-empty forces task state blocked), suggested_next_owner (agent id from the registry — advisory routing, never binding).
event.json — append-only event record (TaskCreated/Leased/LeaseExpired/Completed/Failed/HandoffPosted/HeartbeatMissed), actor, timestamp, refs. Events append to a dedicated AAOS event log that links into event_log.md conventions rather than replacing them — per the Phase 0 verdict.
heartbeat.json — agent id, last-run, source (which existing dead-man/health signal it derives from). Heartbeats are derived from the existing health-monitor chain wherever possible, not independently emitted.



Define the state model: which files are Source of Truth vs. Derived vs. Ephemeral; where each lives; who may write each (single-writer rule per file wherever possible — locks only where genuinely needed). Two conventions are mandatory: (a) atomic write-rename — every AAOS state file is written to a temp path then renamed into place; no tool ever writes a state file in place (this is the partial-write / cloud-sync-corruption defense); (b) project-parameterized paths — all queue/handoff/state paths carry a project namespace segment (e.g., projects/<project-id>/tasks/), with exactly one project seeded now. Zero multi-project machinery is built; the path convention alone keeps a second project from ever requiring a migration.
Define the locking model: lock = atomic create of locks/<task-id>.lock containing holder + expiry; stale-lock reclamation rule; fail-closed on ambiguity.
Define failure semantics: lease expiry, retry counts (max 1 automatic retry, then flag loudly per firm canon), dead-agent recovery, and the human-escalation row format (Notion Ops Hub).
Write docs/NON-GOALS.md and docs/V2-BACKLOG.md.
Formal design review: re-read the spec adversarially against §1 constraints, §2 non-goals, and the Phase 0 capability map. Log every finding and resolution.


Gate 1 exit criteria:


 Spec ≤3,000 words; all five schemas validate against JSON Schema meta-schema (evidence: validator output).
 Every schema field justified — no field without a named consumer.
 Every REUSE/EXTEND verdict from Phase 0 reflected in the design; zero silent BUILDs.
 Design review findings all resolved or explicitly accepted with reasoning.
 ADRs recorded for: architecture selection, transport, locking, event-log relationship, heartbeat derivation.


---### PHASE 2 — FOUNDATION BUILD

Objective: The inert skeleton — structure, schemas, validators, templates. No moving parts yet.

Tasks:


Create the repository/directory structure per spec (under the Claude-system free working surface — Claude_Workspace or brain repo per Phase 0 seam verdict; never under non-Claude OneDrive business folders without permission).
Materialize all schemas; write validator tooling (tools/validate.(ps1|py)) that validates any AAOS file against its schema and exits non-zero on failure. TDD.
Author templates: task, handoff, ADR, gate-review checklist, BUILD-STATE.
Seed agent-registry.json with exactly two entries: claude-code and cowork. Nothing speculative.
Write docs/OPERATIONS.md: how a human inspects the queue, breaks a stuck lock, archives a task, rolls back a protocol version.
Git hygiene: repo initialized (or existing repo extended per ADR), .gitignore correct, initial commit, no secrets in tree (verified by scan — evidence required).


Gate 2 exit criteria:


 Full test suite green; validator rejects malformed fixtures and accepts valid ones (evidence: test output showing both directions).
 A hand-authored sample task + handoff round-trips through the validator.
 Secret scan clean.
 Structure matches spec exactly — diff of spec tree vs. actual tree is empty.



PHASE 3 — COORDINATION LAYER

Objective: The moving parts: queue operations, leasing, events, heartbeat derivation.

Tasks:


Implement queue operations as small, single-purpose scripts (create-task, lease-task, complete-task, fail-task, post-handoff, expire-leases, emit-event). Each: TDD, idempotent, fail-closed, appends an event.
Implement lock manager per spec; test contention explicitly (two simulated writers, one must lose cleanly).
Implement lease expiry sweep as a candidate scheduled task — defined and tested locally, but registered on the fleet ONLY via supervised create_scheduled_task, with in-turn confirmation before registration.
Wire heartbeat derivation: read the existing health-monitor / dead-man outputs and project them into heartbeat.json. Zero new monitoring emitters.
Wire the AAOS event log's linkage to event_log.md conventions per the Phase 1 ADR.
Notion projection (read-mostly): a script that mirrors queue status to an Ops Hub view. Mirror is Derived state, stamped with generation time; anything >7 days stale is marked STALE per firm canon.


Gate 3 exit criteria:


 Full lifecycle demonstrated end-to-end on a synthetic task: created → leased → handoff → completed → archived, with a complete, valid event trail (evidence: event log excerpt + validator pass).
 Lock contention test passes; stale-lock reclamation test passes; lease-expiry test passes.
 Zero direct writes to fleet registries, hooks, or blast-radius files (evidence: git diff scope + statement of paths touched).
 Scheduled-task registration either completed via supervised tool with confirmation, or explicitly deferred with the command staged.



PHASE 4 — AGENT INTEGRATION

Objective: Claude Code and Cowork both operate inside the protocol.

Tasks:


Code adapter: a CLAUDE.md/skill-level instruction block (staged as a file, not silently installed) telling Claude Code sessions how to check the queue, lease, execute, and hand off. Keep it ≤1 page; pointer-first.
Cowork bootstrap: a single, self-contained instruction document (docs/COWORK-BOOTSTRAP.md) that Gabriel pastes/loads into Cowork once. It must survive Cowork's constraints found in Phase 0 (reachable paths, no filesystem assumptions beyond the verified seam). It instructs Cowork to: read the spec → read the queue → lease → execute → post handoff + events. It must also instruct Cowork what to do when the seam is unreadable: report and stop (fail closed).
Dry-run rehearsal within Code alone: simulate a Cowork turn by hand-executing the bootstrap steps against the real seam, verifying every path resolves.
Produce one real cross-surface task for first live use (small, reversible, internal-only — e.g., "Cowork reads handoff X and posts a completion event"), staged but not executed.


Gate 4 exit criteria:


 Both adapters exist, validate against the spec, and reference only verified paths.
 Dry-run transcript shows every bootstrap step resolving against real files.
 The first live cross-surface task is staged with acceptance criteria and rollback (rollback = archive the task, no side effects).
 Nothing was installed into Cowork settings/skills autonomously — bootstrap is delivered as a file for Gabriel to apply.



PHASE 5 — VALIDATION & HARDENING

Objective: Prove failure behavior, not just success behavior.

Failure-injection matrix (ten scenarios; each scripted, executed, evidenced):


Agent dies mid-lease → lease expires → task reclaimable, event trail correct.
Corrupt/hand-mangled task file → validator blocks all operations on it, loud flag, queue otherwise unaffected.
Two writers race for one task → exactly one wins; loser exits cleanly with event.
Seam unavailable (rename the shared directory) → every operation fails closed with a clear error; nothing half-written.
Stale Notion mirror (>7 days) → marked STALE, never presented as current.
Protocol version mismatch (fixture with protocol_version: 0.9) → rejected with migration pointer.
Event-log append failure → operation aborts (event write is part of the transaction, not best-effort).
Partial write / interrupted sync → truncate a state file mid-content (simulating a killed write or OneDrive sync fault) → validator rejects it, operations on it halt, and the atomic write-rename convention is shown to have left the prior version intact.
Write-scope violation → a handoff manifest referencing a path outside the posting agent's write_scope → validation fails, loud flag, task untouched.
Rollback drill: archive an in-flight task and restore pre-task state; verify no orphaned locks or events referencing live work.


Gate 5 exit criteria:


 All ten scenarios executed with pass evidence.
 Full suite green, typecheck 0, lint 0, independent local code review (superpowers:requesting-code-review) completed with findings resolved.
 docs/OPERATIONS.md updated with every failure mode's human-recovery procedure.
 Final BUILD-STATE.md declares v1.0 complete; deliverables manifest written.
 Session-end optimization bullets delivered per standing instruction (systematization candidates: which AAOS operations become skills or scheduled tasks).



5. DELIVERABLES MANIFEST (what "done" produces)


docs/AAOS-SPEC.md (≤3,000 words) · docs/CAPABILITY-MAP.md · docs/NON-GOALS.md · docs/V2-BACKLOG.md · docs/OPERATIONS.md · docs/COWORK-BOOTSTRAP.md · docs/adr/*
schemas/*.json (5) + tools/validate.* + full test suite
Queue/lock/lease/event tooling with tests
agent-registry.json (2 entries) · templates · seeded example task + handoff
Gate-review records for all six gates, each with evidence
Staged (not auto-registered) scheduled-task definition for lease sweep
aaos/state/BUILD-STATE.md maintained throughout


6. STANDING SELF-CHECK (run at every gate)


Did anything built here duplicate a system found in Phase 0? If yes — halt, ADR, resolve.
Is any file both a source of truth and derived somewhere else? If yes — pick one home, archive the other.
Would another engineer, reading only the docs tree, be able to operate and extend this in two years? If no — the docs are the defect.
Is every "done" claim backed by pasted command output? If no — it is not done.
