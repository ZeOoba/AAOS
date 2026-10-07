# AAOS v1.0 — Deliverables Manifest

Checks off the master prompt's §5 "Deliverables Manifest" verbatim against what is actually in the repo at `C:\Users\Owner\AAOS\`, HEAD as of this writing. Every item includes an evidence pointer — this document does not assert completion without one, consistent with the project's own "evidence before claim" doctrine applied to itself.

## `docs/AAOS-SPEC.md (≤3,000 words) · docs/CAPABILITY-MAP.md · docs/NON-GOALS.md · docs/V2-BACKLOG.md · docs/OPERATIONS.md · docs/COWORK-BOOTSTRAP.md · docs/adr/*`

- [x] `docs/AAOS-SPEC.md` — **2,289 words** (`wc -w`), under the 3,000-word ceiling.
- [x] `docs/CAPABILITY-MAP.md` — Phase 0's 15-row REUSE/EXTEND/BUILD table, 80% REUSE+EXTEND (Gate 0 threshold), with a Phase 1 update note pointing to its corrections.
- [x] `docs/NON-GOALS.md` — all 7 master-prompt non-goals, each with a capability-map tie-in.
- [x] `docs/V2-BACKLOG.md` — 6 items, each with a stated deferral reason (not a restatement of the Non-Goals list).
- [x] `docs/OPERATIONS.md` — 5 sections: inspect the queue, break a stuck lock, archive a task, roll back a protocol version, and a §5 failure-mode quick reference mapping all 10 Phase 5 scenarios to a recovery action.
- [x] `docs/COWORK-BOOTSTRAP.md` — self-contained, published to the seam `contracts/` mirror; a 3-probe capability-detection preamble, explicit fail-closed stop-and-report protocol, and the SAFE-BY-HAND write recipes with an explicit forbidden-actions list and reasons.
- [x] `docs/adr/0001-architecture-selection.md` … `0005-heartbeat-derivation.md` — all 5 at v1.0 (ADR-0006 was added later), each Accepted, each with Context/Decision/Consequences; ADR-0003 and 0001 carry Gate 3/5-review correction notes where their original text was found stale.

## `schemas/*.json (5) + tools/validate.* + full test suite`

- [x] `schemas/{agent-registry,task,handoff,event,heartbeat}.schema.json` — all 5, JSON Schema draft 2020-12, re-validated against the meta-schema this session (`Draft202012Validator.check_schema`, all 5 `OK`).
- [x] `tools/validate.py` — the validator CLI (schema inference, `protocol_version` gating, write-scope colonization guard).
- [x] Full test suite — **173 tests, all green at v1.0 (historical; 267 as of origin/main 12ae2e4, recounted 2026-10-06)** (`python -m unittest discover -s tests -p "test_*.py"`, independently re-run this session after every fix).

## `Queue/lock/lease/event tooling with tests`

- [x] `tools/_aaos_common.py` — the shared library: atomic write-rename, the 6-step lease-acquire protocol, `commit_state_change`/`assert_lease_held`, `reclaim_decision`/`archive_lock`, `emit_event`/spool/drain (spool-as-commit-point + `O_EXCL` drain-lock, redesigned in the Gate 3 fix cycle to close a Critical concurrent-append race).
- [x] `tools/{create,lease,complete,fail}-task.py`, `tools/post-handoff.py`, `tools/expire-leases.py`, `tools/emit-event.py` — all 7, each with dedicated tests in `tests/test_queue_scripts.py` plus the subprocess-driven E2E lifecycle in `tests/test_e2e_lifecycle.py`.
- [x] `tools/derive-heartbeat.py`, `tools/project-notion.py` — the MCP-boundary-respecting projection scripts (consume externally-fetched signals / local seam files only, per Phase 3's "Problem A" resolution).
- [x] `tools/publish-contracts.py` — the contracts-mirror publisher (Phase 4), fail-closed on provenance/missing-source/malformed-registry.

## `agent-registry.json (2 entries) · templates · seeded example task + handoff`

- [x] `agent-registry.json` — 2 entries (`claude-code`, `cowork`), every field grounded in Phase 0 evidence (tech.md's connector inventory, the capability map's Cowork-reachability findings), not speculative.
- [x] `templates/{task,handoff}.template.json`, `templates/{adr,gate-review,BUILD-STATE}.template.md` — all 5.
- [x] `examples/t-sample-001.json`, `examples/h-sample-001.json` — hand-authored, validate against their schemas (Gate 2's round-trip evidence).

## `Gate-review records for all six gates, each with evidence`

- [x] Gate 0 — `docs/CAPABILITY-MAP.md` (conditional pass; seam evidenced-but-not-live-confirmed, carried forward as the one standing open item through every subsequent gate).
- [x] Gate 1 — `docs/PHASE1-DESIGN-REVIEW.md` (2 SHOULD-FIX + 4 MINOR/NOTE, all resolved).
- [x] Gate 2 — `docs/GATE2-REVIEW.md` (5 findings incl. 1 Critical write_scope traversal bypass, all fixed).
- [x] Gate 3 — `docs/GATE3-REVIEW.md` (8 findings incl. 1 Critical event-log concurrent-append race, all fixed, independently re-verified against the reviewer's own reproduction probes).
- [x] Gate 4 — `docs/GATE4-REVIEW.md` (6 findings incl. 2 Important — a probe-artifact's obsolete embedded instruction, a should-be-mandatory collision-avoidance rule — all fixed).
- [x] Gate 5 — `docs/GATE5-REVIEW.md` (6 findings incl. 2 Important — blocked-task lease never dropped, 4 stale reclamation-policy descriptions — all fixed, independently re-reproduced end-to-end outside the test suite).

**None of the six gates received an explicit Gabriel sign-off before the next phase's instruction arrived** — each was presented and the session proceeded on the user's next message. Flagged consistently in every `BUILD-STATE.md` update across all five sessions, never silently assumed as approval.

## `Staged (not auto-registered) scheduled-task definition for lease sweep`

- [x] `tools/expire-leases.py` — built, tested (reclaim/dry-run/corrupt-lock/concurrent-sweep-race coverage), and run for real against the live seam multiple times this session. **Never registered** via `create_scheduled_task` on either fleet — staged-with-the-command-known, per Gate 3's own exit criterion ("explicitly deferred with the command staged"), consistent with the master prompt's supervised-registration-with-in-turn-confirmation requirement, which was never sought.

## `aaos/state/BUILD-STATE.md maintained throughout`

- [x] `Claude_Workspace\AAOS\state\BUILD-STATE.md` — updated at the end of every phase session (Phase 0 through Phase 5), each update naming the gate status, what was done, open items, and the next action, per the master prompt's own "read it before anything else, never re-derive from memory" session-continuity rule.

---

## Standing open items (not deliverables gaps — explicitly out of this project's reach)

1. **Live Cowork-side seam confirmation.** `t-cowork-seam-confirm` has been staged since Phase 4 and remains so — closing this requires an actual Cowork session, which no tool available to this Code session can invoke (checked directly: `computer-use`'s installed-apps list has no Cowork/Claude Desktop entry; `ccd_session_mgmt` reaches other Code sessions, not Cowork).
2. **The signal-fetch half of `derive-heartbeat.py`** — confirmed-deferred to a future agent-session call site per Phase 3's Problem-A resolution (plain Python cannot call MCP tools); not a v1.0 gap, a stated architectural boundary. (The Notion-write half of `project-notion.py`'s output was the other side of this item — see below, now closed.)

**Closed since v1.0:**
- `tools/publish-notion.py` (built in its own worktree, request approved in-turn) is the "future agent-session call site" this section's original item 3 deferred for `project-notion.py`'s output — it POSTs the projection payload as a dated child page under the Notion Ops Hub (`32b6ecd8…`), per `docs/CAPABILITY-MAP.md` item 3's REUSE guidance (existing child-page convention, no parallel database). Stdlib-only HTTP (no new dependency), pure-builder/thin-IO-wrapper split matching this repo's house style, 22 unit tests (dry-run, mocked success, mocked HTTPError/URLError). Still never touches tasks/locks/events — read-only against the local projection, write-only against Notion, matching ADR-0001 (Notion stays Derived, never primary).
- **Scheduled-task registration of `expire-leases.py`** — this section's original item 2 was already stale when read on 2026-07-26: `list_scheduled_tasks` shows `ora-aaos-expire-leases` live on the CC/MCP fleet since 2026-07-10 (cadence halved 6×→3×/day on 2026-07-16 per Fleet Ledger v2 ruling C2; last successful run 2026-07-26T17:12 ET, cron `12 7,13,19 * * *`). This doc's claim that registration was "never attempted... never requested" was never corrected after the fact — a live-registry check should be the standing practice before citing this file's open-items list as current, not just reading it.

**v1.0 is complete.** All six gates self-assessed PASS with evidence; all master-prompt deliverables present; 173/173 tests green at v1.0 (267 on origin/main as of 2026-10-06); every adversarial/independent-review finding across all five phases (4 Critical, several Important, many Minor) resolved and independently re-verified, not merely accepted.
