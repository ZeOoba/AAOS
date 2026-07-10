# AAOS Phase 4 — Agent Integration: Implementation Design

**Status:** implementation-level design, produced by an opus-tier planning pass before any Phase 4 file was written. This is the single source of truth for Phase 4's adapter content, the `publish-contracts` script, the first staged cross-surface task, and the dry-run checklist.

## 1. Problem C resolution — Cowork's code-execution capability

### 1.1 The core reasoning

Two facts from Phase 3 decide everything downstream:

1. **The safety guarantees are OS primitives, not conventions.** Exactly-one-winner leasing depends on `O_EXCL` atomic exclusive-create of a *predictable* filename (`locks/<task-id>.lock`); race-free event capture depends on writing a *uniquely-named* file that no other writer will ever pick (`…pending/<time_ns>-<pid>-<uuid>.json`). A model-facing "write this file" tool exposes neither `O_EXCL` nor an atomic compare-and-swap. Therefore **any hand-written mutation of a shared, predictably-named file cannot honestly claim the Phase-3-proven guarantee**, but **any hand-written creation of a fresh, uniquely-named file that nobody else will ever write is safe by construction** — there is no contention to lose.
2. **The tools live in the git tree.** To "run the real tools like Code," a Cowork-spawned process must be able to read `C:\Users\Owner\AAOS\tools\` and `\schemas\`. So the real capability test is not "can Cowork run Python" in the abstract — it is "can Cowork successfully invoke a real AAOS tool against the real seam." One probe settles the whole question.

This collapses the branch space: the **behavioral** fork is EXEC vs NO-EXEC; the git-tree-read probe only decides *where Cowork reads its contracts from*, not *what it may write*.

### 1.2 The capability-detection preamble (exact steps Cowork runs first)

Run in order, top of every Cowork turn, before any protocol action:

- **Probe S — Seam reachability (mandatory gate).** Attempt to read `C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS/contracts/PUBLISHED.json`. If absent, try `…/AAOS/_seam-probe.md` as a fallback sentinel. If **neither** is readable → **STOP, fail closed** (§3 of the bootstrap). If `PUBLISHED.json` reads but `protocol_version != "1.0"` → STOP (protocol mismatch).
- **Probe G — Contracts source.** Attempt a plain file-read of `C:/Users/Owner/AAOS/schemas/task.schema.json` (git tree). Success → `CONTRACTS_SOURCE = git-tree` (freshest, authoritative). Failure → `CONTRACTS_SOURCE = seam-mirror` (`…/AAOS/contracts/`). If Probe S found `PUBLISHED.json` but the seam `contracts/schemas/` are missing/malformed AND the git tree is unreadable → STOP (no usable schemas).
- **Probe X — Execution.** Attempt a **read-only** real-tool invocation:
  `python "C:/Users/Owner/AAOS/tools/validate.py" "C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS/contracts/PUBLISHED.json" --schema agent-registry` — *(decoupled from any specific demo task per the open item below — probing against the stable `PUBLISHED.json`/registry rather than a task that might later be archived/renamed)*.
  Exit 0 → `MODE = FULL-EXEC` (proves shell + git-tree-readable-by-that-process + jsonschema + `_aaos_common` all work, and it mutated nothing). Any failure, unavailability, or absence of a shell facility → `MODE = SAFE-BY-HAND`.

**Do not assume MODE.** The bootstrap requires Cowork to *state which mode and source it resolved to* in its turn report, so a human can see whether the optimistic or the restricted path ran.

### 1.3 Branch-by-branch behavior

- **`MODE = FULL-EXEC`** (Probe X passed): Cowork uses the real tools **exactly like Code** — `lease-task.py`, `complete-task.py`, `post-handoff.py`, `emit-event.py`, etc., with `--holder cowork --session <cowork-session-id>`. Problem C's hard parts evaporate; the full turn is available. Cowork still reads contracts from `CONTRACTS_SOURCE` for its own briefing but the tools use the git tree directly.
- **`MODE = SAFE-BY-HAND`, `CONTRACTS_SOURCE = git-tree`**: Cowork reads schemas/spec directly from `C:/Users/Owner/AAOS/` (freshest) but **cannot run scripts** → restricted to the safe subset (§1.4).
- **`MODE = SAFE-BY-HAND`, `CONTRACTS_SOURCE = seam-mirror`**: identical write behavior; Cowork reads schemas/spec from the seam `contracts/` mirror because the git tree is unreachable. This is the reason `publish-contracts` must exist.
- **Seam unreadable** (Probe S fails): fail closed, report, write nothing.

### 1.4 The safe-operation subset for the NO-EXEC case (the load-bearing answer)

Governing rule Cowork is taught verbatim: **you may READ anything; you may only WRITE by creating a brand-new file at an id/name you invent that no other writer will ever touch; you may never hand-mutate an existing shared file, and you may never hand-create a predictably-named file another agent also races for (a lock).** Schema conformance for every new file is self-checked against the schema copy from `CONTRACTS_SOURCE` before the write is treated as final; because these are fresh-id new files (never mutations), a schema mistake harms nothing shared — the worst case is Code's `validate.py` rejecting Cowork's file at pickup and flagging it, never silent corruption of live state. That asymmetry is *why* new-file-by-hand is acceptable without a live validator and mutate-by-hand is not.

Per-operation verdict:

| Operation | Verdict | Reasoning |
|---|---|---|
| **create-task** | **SAFE-BY-HAND** (new-file) | Writes `tasks/<id>.json` at a Cowork-invented, collision-proof id (uuid-suffixed) + drops a `TaskCreated` spool event. New unique file → no contention; no shared state to corrupt. Not used in the v1 first task (Code stages it), but mechanically safe. |
| **lease-task** | **UNSAFE-BY-HAND → SAFE-IF-SCRIPT-EXEC** | Requires `O_EXCL` create of the predictable `locks/<task-id>.lock` (exactly-one-winner) **and** in-place mutation of shared `task.json`. A hand-written lock is last-write-wins → double-lease, the precise corruption AAOS exists to prevent. Only safe via the real tool. |
| **complete-task** | **UNSAFE-BY-HAND → SAFE-IF-SCRIPT-EXEC** | In-place mutation of shared `task.json` (state→done, drop lease, add evidence) + `assert_lease_held` (which Cowork can't have legitimately satisfied by hand) + lock archive. Shared-file mutation → defer. |
| **fail-task** | **UNSAFE-BY-HAND → SAFE-IF-SCRIPT-EXEC** | Same shared-`task.json` in-place mutation + `Failed`/`Escalated` sequencing. Defer. |
| **post-handoff** | **SAFE-BY-HAND** (new-file), with two carve-outs | Writes `handoffs/<id>.json` write-once at a fresh id → no contention. **Conditions:** (a) `artifact_manifest` empty (Cowork can't reliably compute `sha256`/`bytes` in no-exec mode, and every listed path must be inside cowork's `write_scope`); (b) schema-conformant by self-check. **Carve-out:** the `--blocker` path also mutates the shared task to `blocked` in place — that half is UNSAFE-BY-HAND-DEFER. So: post a handoff yes; force a task-blocked transition no. Emit the paired `HandoffPosted` via a spool file (below). |
| **expire-leases** | **UNSAFE-BY-HAND → SAFE-IF-SCRIPT-EXEC** | A concurrency-critical sweep that mutates many `task.json` in place, archives locks, and serializes NDJSON writes behind the drain lock. Conceptually the fleet/Code's job, never Cowork's. Defer unconditionally. |
| **emit-event** | **SAFE-BY-HAND *only* via the spool file; UNSAFE via direct NDJSON append** | Dropping a uniquely-named `…/events/aaos-events.ndjson.pending/<sortable-ts>-cowork-<uuid>.json` **is** the designed durable commit point — unique filename by construction, zero contention, and Code's next drain folds it into the NDJSON under the `O_EXCL` drain lock. Cowork must **never** append to `aaos-events.ndjson` directly (that is Finding 1's concurrent-append race). Event JSON self-checked against `event.schema.json`; `event_id` minted as `e-<cowork-ISO-now>-<random digits>` (schema pattern `^e-[0-9TZ:.+-]+-[0-9]+$`, digits-only tail). |

Net: in NO-EXEC mode Cowork's toolbox is **read everything + create-task + post-handoff (empty manifest) + emit-event (spool only)** — read-heavy with new-file writes, exactly matching the master-prompt example's intended v1 scope. Everything that mutates shared state is deferred to a Code session or the fleet sweep.

---

## 2. `docs/CODE-ADAPTER.md` — content outline (git tree; ≤1 page; pointer-first)

Staged file, **not** auto-loaded. Sections:
- **Banner:** "STAGED — not installed."
- **What/why + pointers** to `AAOS-SPEC.md`, `PHASE3-DESIGN.md` §0.4, `OPERATIONS.md`.
- **Preconditions:** claude-code session, tools path, live seam path, `--holder claude-code --session <id>` on every mutating call.
- **The turn:** check queue → lease → execute → hand off (if passing on) → complete/fail, one command per step.
- **Fail-closed reminders:** never hand-edit the NDJSON; CONTESTED/DENIED is a stop not a loop; TTL 45min, no renew CLI in v1; evidence-before-done.
- **Escalation pointer** to OPERATIONS.md §2–4.

## 3. `docs/COWORK-BOOTSTRAP.md` — content outline (published to seam; fully self-contained)

- **§0** What this is / how you got it; do steps in order; fail closed on any step that fails.
- **§1** Constants: seam root, project dir, contracts dir, git tree (may be unreadable), your agent id `cowork`, your `write_scope`.
- **§2** Capability-detection preamble: Probes S, G, X verbatim; resolution → `MODE` + `CONTRACTS_SOURCE`; state both in the end-of-turn report.
- **§3** Seam-unreadable → stop, fail closed. Three failure classes (not-found / permission / malformed) each with the exact stop-and-report message template (internal only, never external).
- **§4** Read the contracts from `CONTRACTS_SOURCE`: `AAOS-SPEC.md`, `handoff.schema.json`, `event.schema.json`, `agent-registry.json`, `templates/handoff.template.json`.
- **§5** Read the queue: find the task/handoff addressed to `cowork`; §3-stop if missing/malformed.
- **§6** Execute the turn, branch on `MODE` (FULL-EXEC: run the real tools; SAFE-BY-HAND: §7 only).
- **§7** SAFE-BY-HAND write recipes: post a return handoff (exact field set, empty manifest, self-validate) + emit the paired event via the spool file only (never touch the NDJSON directly).
- **§8** What you must NOT do by hand, one honest reason each: locks, in-place task edits, lease/complete/fail/expire by hand, direct NDJSON append, in-place `_seam-probe.md` edits.
- **§9** Report back: `MODE`, `CONTRACTS_SOURCE`, what was read, what new files were written, any stop/blocker.

## 4. `publish-contracts.py` — script design

**Language: Python**, not the `.ps1` ADR-0002's illustrative tree named — every other tool is Python and reuses `_aaos_common.atomic_write` (the one write-rename idiom the whole codebase centralizes); a PowerShell script would reimplement it from scratch. ADR-0002 addendum records this as a naming reconciliation, not a silent deviation.

**Copies** (git tree → seam `contracts/`, each via `atomic_write`): `schemas/*.schema.json` (5), `agent-registry.json`, `AAOS-SPEC.md`, `COWORK-BOOTSTRAP.md`, `templates/*`, plus a `PUBLISHED.json` stamp (`protocol_version`, `source_commit`, `generated_at`).

**Not published:** `CODE-ADAPTER.md` (no Cowork consumer), `OPERATIONS.md` (its procedures are exactly the by-hand shared-state surgery §1.4/§8 forbid Cowork from doing — keep it git-only so it can't tempt an unsafe action), `NON-GOALS.md`/`V2-BACKLOG.md`/`adr/*`/`MIGRATIONS.md` (design rationale, not turn-execution inputs).

**`source_commit`:** `git rev-parse HEAD` via subprocess; if `git status --porcelain` shows a dirty tree, stamp `"<hash>-dirty"` + `"dirty": true` (publishing from an uncommitted tree is normal — never fabricate a clean-hash provenance that doesn't match the published bytes). `--source-commit <sha>` override; if git is unavailable and no override given, **exit non-zero, publish nothing** (fail closed on provenance, never stamp unknown).

**Pre-publish sanity:** validate `agent-registry.json` via `validate_obj(..., "agent-registry")` before copying — a malformed registry must never reach the seam. Idempotent; sole writer of `contracts/`.

## 5. The first live cross-surface task — staged, not executed

**Task:** close the still-open two-way seam confirmation (`_seam-probe.md` / BUILD-STATE item 1) — small, real, internal-only, reversible.

**Staged via the real tools** (Code, this session):
1. `create-task.py --id t-cowork-seam-confirm --owner cowork --priority 3 --title "Confirm two-way Code↔Cowork seam reachability" --accept "Cowork posts a return handoff from cowork→claude-code referencing this task" --accept "Cowork emits a HandoffPosted event for that return handoff" --accept "The return handoff validates against handoff.schema.json"`.
2. `post-handoff.py --id h-code-to-cowork-seam-confirm --task t-cowork-seam-confirm --from claude-code --to cowork --summary "..." --next-action "Post return handoff h-cowork-seam-confirm-ack + its HandoffPosted spool event" --artifact path="…/AAOS/_seam-probe.md"`.

**"Staged" means:** the task and handoff already exist on the seam, written via the real tools, awaiting a Cowork session to act — Cowork has not yet been invoked. (Not "just described in a doc" — that would leave the dry-run nothing real to check.)

**Cowork's eventual turn (SAFE-BY-HAND):** reads spec + handoff + `_seam-probe.md`; writes `h-cowork-seam-confirm-ack.json` (empty manifest, confirming both-direction read); drops a `HandoffPosted` spool event. Realized as `HandoffPosted`, not `Completed` — emitting `Completed` while `task.json` still reads `queued` would be a dishonest event/state mismatch Cowork cannot safely produce by hand.

**Rollback (Gate 4: "archive the task, no side effects"):** OPERATIONS.md §3, no new tooling — the task was never leased, so nothing to reclaim. Move `tasks/t-cowork-seam-confirm.json` → `_archive/`, move the handoff(s) → `_archive/`; the `TaskCreated`/`HandoffPosted` events stay in the NDJSON untouched (append-only history is "no side effects").

## 6. Dry-run rehearsal — the checklist

1. Probe S — read `contracts/PUBLISHED.json`; assert exists, parses, `protocol_version=="1.0"`.
2. Probe G — read `schemas/task.schema.json` from the git tree; record `CONTRACTS_SOURCE`.
3. Probe X — run `validate.py` against `contracts/PUBLISHED.json` (`--schema agent-registry`... see implementation note below); assert exit 0; record `MODE`.
4. Contracts read — open `AAOS-SPEC.md`, `handoff.schema.json`, `event.schema.json`, `agent-registry.json`; assert parse + `cowork` write_scope present.
5. Queue read — list `tasks/`; assert `t-cowork-seam-confirm.json` present, parses, `state=="queued"`, `owner_agent=="cowork"`.
6. Handoff read — open `h-code-to-cowork-seam-confirm.json`; assert parses, `to_agent`/`task_ref` correct; verify artifact path + hash on disk.
7. Input read — open `_seam-probe.md`; assert readable.
8. Draft return handoff — construct in memory, self-check against schema; assert `handoffs/` writable via a create+delete temp sentinel.
9. Draft event — construct in memory, self-check against schema; assert the pending dir writable via a create+delete temp sentinel.
10. write_scope check — confirm the draft only touches `Claude_Workspace/**`, no out-of-scope artifact.
11. Fail-closed rehearsal — point the reader at a missing/malformed `PUBLISHED.json`; confirm the §3 stop path triggers correctly; restore.
12. Cleanliness assertion — no live artifacts created, all temp sentinels removed, `t-cowork-seam-confirm` still `queued`.

Passing all twelve = Gate 4's "dry-run transcript shows every bootstrap step resolving against real files," first live task still staged, nothing installed into Cowork.

## Implementation notes carried forward
- Probe X's exact `validate.py` invocation needs a real target file: `PUBLISHED.json` doesn't itself have a dedicated schema (it's a `contracts/`-only stamp, not one of the five contracts). Simplest fix used in implementation: Probe X validates `contracts/agent-registry.json` with `--schema agent-registry` (published, stable, decoupled from the demo task) — updated in the bootstrap doc and the rehearsal accordingly.
- Cowork `--session` identity convention in FULL-EXEC: use the Cowork session id string as-is (matches how Code uses its own session id).
