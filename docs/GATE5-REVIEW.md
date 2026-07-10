<!-- Copy this file when closing out any phase gate (Gate 0 through Gate 5). One checklist item per exit criterion named in the master prompt's Phase N section — do not paraphrase the criteria away. -->

# Gate 5 Review — Validation & Hardening (v1.0)

**Status:** Self-assessed PASS
**Reviewed by:** independent opus-tier reviewer via `superpowers:requesting-code-review` (fresh read, whole-codebase diff against the empty tree — distinct from every prior phase's custom adversarial-review pattern, run in addition to it per the master prompt's own naming of this specific skill for Gate 5), plus a fix-and-reverify loop
**Date:** 2026-07-10

## Exit criteria checklist

- [x] **All ten failure-injection scenarios executed with pass evidence.**
  Evidence: `docs/_task-reports/phase5-failure-matrix-report.md` — one section per scenario (1-10, matching the master prompt's own numbering), each with real command output. 6 scenarios (#1, #3, #5, #6, #7, #9) were already proven by Phase 2-4 tests and are re-demonstrated here with a fresh confirming run rather than silently assumed still-passing; 4 (#2, #4, #8, #10) are genuinely new scripted scenarios built this phase. Scenario #4's rehearsal found and fixed a real gap (below).

- [x] **Full suite green, typecheck 0, lint 0, independent local code review completed with findings resolved.**
  Evidence: `python -m unittest discover -s tests -p "test_*.py"` → **173/173 OK** (independently re-run by the orchestrating session after every fix, not just trusted from an implementer's report). This is a pure-Python codebase using only the standard library + `jsonschema`; no separate typecheck/lint tooling was introduced across any phase (consistent with the master prompt's own minimalism — no tooling beyond what each phase's task list names), so "0/0" is satisfied by there being no configured linter to fail, not by suppressing one. `superpowers:requesting-code-review` (the master prompt's explicitly-named Gate 5 requirement, run over the full codebase) returned 2 Important + 4 Minor findings, all fixed and independently re-verified — see below.

- [x] **`docs/OPERATIONS.md` updated with every failure mode's human-recovery procedure.**
  Evidence: `docs/OPERATIONS.md` §5 (Failure-mode quick reference) maps all ten master-prompt scenarios to a recovery action — 5 cross-references into the existing §1-4 procedures, 5 new paragraphs (corrupt task file, seam unavailable, event-log append failure/spool, partial write/`.tmp` cleanup, write-scope violation). The Gate 5 review additionally tightened §2 and §3 for accuracy against the shipped reclamation policy and the leased-task archive case (below).

- [x] **Final `BUILD-STATE.md` declares v1.0 complete; deliverables manifest written.**
  Evidence: `Claude_Workspace\AAOS\state\BUILD-STATE.md` (this session's final update) and `docs/DELIVERABLES-MANIFEST.md` (new, checks off the master prompt §5 deliverables list against what's actually in the repo, with evidence pointers).

- [x] **Session-end optimization bullets delivered per standing instruction.**
  Evidence: included in this session's final response to Gabriel (systematization candidates: which AAOS operations become skills or scheduled tasks), per the master prompt's "Autonomy" standing instruction.

## Findings from the independent code review

Run over the **entire codebase** (diff against git's empty-tree hash, not just Phase 5's changes) — the master prompt explicitly names `superpowers:requesting-code-review` as a Gate 5 requirement distinct from the project's own per-phase adversarial-review pattern (which had already found and fixed a Critical bug in 3 of the 4 prior phases). The reviewer independently re-verified the two most severe prior fixes with its own fresh probe scripts (the 8×60 concurrent-event-emission race and a 12-thread lock race) before reviewing new ground, and found no Critical issue — both hold under renewed adversarial pressure.

1. **[Important]** A blocked task's lease was never dropped by `post-handoff.py --blocker`, contradicting `task.schema.json`'s own invariant ("lease present iff leased/in-progress"). If the lease-holding agent died while a task was blocked, its lock was unreclaimable forever — `expire-leases.py` only re-examines locks whose task is `leased`/`in-progress` — and the sweep's skip message falsely asserted "already reclaimed by a concurrent sweep," a cause it had never actually verified.
2. **[Important]** Four locations still described the pre-Gate-3-fix reclamation policy ("reclaimable only when expiry AND a missed/unknown heartbeat"), contradicting the actual expiry-primary/heartbeat-classifies-only implementation: `docs/adr/0003-locking-model.md`, `docs/OPERATIONS.md` §2, and two **published** schema field descriptions (`heartbeat.schema.json`'s `status`, `agent-registry.schema.json`'s `active` — the latter also naming the wrong consumer entirely). `AAOS-SPEC.md` §7 had been corrected during the Gate 3 fix cycle; the correction was never propagated to these four.
3. **[Minor]** `project-notion.py` and `derive-heartbeat.py` were excluded from the Scenario-#4 `require_seam_reachable` fix applied to the 7 mutating scripts — both would auto-vivify a phantom seam tree and report success (`stale:false`, empty rows) instead of failing closed.
4. **[Minor]** The published `contracts/PUBLISHED.json` provenance stamp lagged HEAD by two commits (content was still byte-identical; only the stamp was stale).
5. **[Minor]** `post-handoff.py` can force a task to `blocked` without holding its lease — consistent with the documented design (any agent may flag an external blocker), left as-is with the intent now documented in the fix for Finding 1's code comment.
6. **[Minor]** `in-progress` is a reserved task state with no v1 producer; `OPERATIONS.md` §3 didn't tell an operator to drop a leased task's stale `lease` object when archiving it (the code path already did this correctly via the Scenario #10 drill; the doc didn't say to).

## Resolutions

All 6 findings **FIXED**, independently re-verified by the orchestrating session (not just the implementer's report):
1. `post-handoff.py`'s blocking transition now drops the task's `lease` and archives its lock in the same transaction. New regression test seeds a **real lock file on disk** (not just the task's `lease` field) and proves both the drop+archive and that the sweep no longer misfires afterward. Independently re-reproduced end-to-end outside the test suite: `state: blocked | has lease: False`, sweep exits 0 with no false diagnosis.
2. All four stale descriptions corrected to state the actual, shipped policy (expiry-primary; heartbeat only classifies DeadAgent vs. ExpiredLapsed for escalation purposes), each with an explicit note that this is a Gate 5 correction, not a silent rewrite of history — the original (wrong) wording and why it was wrong is preserved in the correction note itself.
3. `require_seam_reachable` extended to both projection scripts — unconditionally for `project-notion.py` (all its inputs are seam-rooted regardless of `--out`), gated only on the default-output-path case for `derive-heartbeat.py` (whose `--signals`/`--out` may legitimately point off-seam, per design). 4 new regression tests, including one confirming the off-seam `--out` override still works against an unreachable default seam root.
4. Contracts republished after every fix; final `PUBLISHED.json` stamp matches the release commit exactly (verified: `source_commit` == current HEAD, `dirty: false`).
5. Documented as intentional (code comment in the fix for Finding 1); no behavior change.
6. `task.schema.json`'s `state` field description now notes `in-progress` is reserved/unwritten in v1; `OPERATIONS.md` §3 now explicitly instructs dropping a leased task's `lease` object when archiving it.

Post-fix: full suite re-run (173 tests, up from 170 pre-fix, 152 pre-Phase-5), all 5 schemas re-validated against the JSON Schema 2020-12 meta-schema, and contracts republished with current, clean provenance.

## Verdict

**PASS.** All five Gate 5 exit criteria hold with evidence; every independent-review finding was fixed and independently re-verified, none merely accepted. This closes the master prompt's six-gate program. **v1.0 is complete** — see `docs/DELIVERABLES-MANIFEST.md` for the full deliverables checklist and `Claude_Workspace\AAOS\state\BUILD-STATE.md` for the final build-state record.

**Standing open item, unresolved by this gate and not resolvable from the Code surface:** the live Cowork-side seam confirmation (`t-cowork-seam-confirm`, staged since Phase 4) remains the one genuinely open loose end from Phase 0 — closing it requires an actual Cowork session, which this session confirmed it has no mechanism to invoke directly (checked: `computer-use`'s installed-apps list does not include Cowork/Claude Desktop; the `ccd_session_mgmt` tools reach other Claude Code sessions, not Cowork). This does not block v1.0 per Gate 4's own framing ("independent of whether Phase 5 has started") but is the natural next action for a human.
