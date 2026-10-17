# AAOS Optimization — Design Spec

Date: 2026-10-17 · Branch: `claude/aaos-optimization-plan-27d345` · Status: draft for pre-mortem
Inputs: cc-audit-army run `wf_e31ede0a-2a7` (52 confirmed: 0 critical / 2 high / 23 medium / 27 low); Claude Code 2.1.302 + Cowork capability baseline (2026-10-17).

## Goal
Make the AAOS coordination substrate safe under concurrent writers on Windows/OneDrive, then close the Cowork↔Code communication loop, without changing the file-seam transport (ADR-0002).

## Decisions (ratified in brainstorming)
| # | Decision | Choice |
|---|----------|--------|
| D1 | Plan thrust | Harden core first (Tier A), then comms (Tier B) |
| D2 | Native capability use | Keep OneDrive file seam as source of truth; add read-only native surfaces |
| D3 | Tier C hygiene | Defer; only doc drift (M47–M49) ships now; rest to `V2-BACKLOG.md` |
| D4 | Delivery | Amend ADR-0006 for CAS semantics; one PR per finding group, each with adversarial REFUTE |
| D5 | Windows verification | `windows-latest` CI job + injected sharing-violation tests; no self-hosted runner |
| D6 | Event-emitting hooks (SubagentStop/TaskCompleted) | Deferred until Tier A is proven; Tier B hooks are read-only |

## Non-goals
Cloud-scheduled Cowork sweep (cloud tasks cannot reach local OneDrive paths); event-emitting hooks; god-module split; CLI helper dedup; log rotation (M16/M17/M19 → backlog); any change to `oxfordra.net` or the confidential roster.

## Tier A — Harden the core (PRs A1–A5, in order)
- **A1 Drain-lock steal CAS** (M01): steal only if lock bytes still match the bytes judged stale (reuse `archive_lock_if_unchanged`, `tools/_aaos_common.py:513`); judge content staleness on the real clock. Test: deterministic two-stealer interleaving. ADR-0006 amendment lands here.
- **A2 Sweep reclaim CAS** (M02, M11 unparseable-expires wedge, M04 expired same-holder no-op, M06 handoff write-once TOCTOU, M07 commit-replace retry): `expire-leases.py` re-matches `task.lease` to the judged lock and archives conditionally; naive/unparseable `expires` is escalated, not skipped; `lease-task` treats an expired own lease as expired.
- **A3 Input bounds** (M03 flag bounds, M09/M10 heartbeat, M23 DeadAgent noise): reject zero/negative `--ttl-min/--skew-min/--settle-sec`; heartbeat rejects NaN/Inf/negative and future `last_run`; consumers check `derived_at`; DeadAgent classification handles absent heartbeat (M23).
- **A4 Notion egress gate** (M27, M12, M13, M14, M15): `publish-notion.py` fails closed against `confidential-terms.json` (redact/omit titles on match); page title carries projection age; STALE flag moves to the title; stale computed from newest input correctly and true when no inputs. RED-tier: REFUTE mandatory.
- **A5 Windows/OneDrive CI** (M29, M31, M32, M30): `windows-latest` job; tests for the `os.link`→`_exclusive_create` OSError fallback and ADR-0006 "exactly one wins"; CI `permissions:` block, SHA-pinned actions.
- Doc drift ships with A5: M47 (stale expire-leases entry), M48 (single live test-count source), M49 (OPERATIONS symptom table).

## Tier B — Communication layer (PRs B1–B4, after A1–A5 merged)
- **B1 Inbox/status CLI** (M36, M37, M32): read-only `aaos inbox` / `aaos status` listing queue, leases, stuck tasks, and handoffs addressed to `claude-code`; defines what a return handoff does to its task.
- **B2 Native read-only surfaces** (D2): SessionStart hook injects B1 summary as `additionalContext` (`onFailure` fail-open, never blocks); optional `FileChanged` watch on the seam; an `/aaos` skill wrapping B1.
- **B3 Inbound trust** (M08, M25, M26, M22, M38, M28): schema + actor + `write_scope` validation on every Cowork-written spool event/task; receivers verify artifact hashes; fix Cowork spool filename sort order; enforce `owner_agent`/`to_agent` routing.
- **B4 Terminal-state exits and structured reports** (M39, M40, M50, M05): event-committing exit from blocked/failed (replaces hand-edit runbook); retryable post-handoff `--blocker`; machine-checkable Cowork report/`context_summary` shape.

## Verification & guardrails
- Test-first per PR; baseline 267 tests must stay green; new tests per finding.
- REFUTE pass (adversarial-reviewer, model ≥ Opus) on A1, A2, A4 and B3 (shared-state writers / egress).
- Each PR independently revertible; A-series gated on prior merge.
- No code executed against the live seam; tests use temp seam trees.

## Risks to examine in pre-mortem
CAS semantics on OneDrive (non-atomic sync, sharing violations); ADR-0006 amendment conflicting with ratified 30s settle; B3 validation rejecting legitimate Cowork spool events already in flight; hook surfacing adding latency to SessionStart; Windows CI flakiness masking real races.
