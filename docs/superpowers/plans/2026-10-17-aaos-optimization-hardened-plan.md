> Produced by aaos-eval-plan-premortem run wf_c4449398-61e on 2026-10-17 (AAOS-grounded copy of cc-eval-plan-premortem). Extends docs/superpowers/specs/2026-10-17-aaos-optimization-design.md (D1-D6 stay ratified; every change is recorded in the section 1a delta table). Ballot Q1-Q9 ratified 2026-10-17 (all option A); Q10-Q15 await the L0 spike.

# AAOS Optimization: Hardened Improvement Plan (Phase 4)

Date: 2026-10-17 · Branch `claude/aaos-optimization-plan-27d345` · Baseline: worktree HEAD `957140d`. That commit is docs-only on top of `618f081`, where the audit ran (cc-audit-army `wf_e31ede0a-2a7`, 52 confirmed findings M01-M52).
Extends: the draft spec `docs/superpowers/specs/2026-10-17-aaos-optimization-design.md` (D1-D6 remain ratified except where section 1a records a change) and re-sequences the Phase-3 extended plan (A0-A6, B1-B6, C0, F0, V1, R0/DQ1-19).
Status: hardened and validated. Every critical and high pre-mortem objection is resolved (§9). The 14 post-hardening validation defects are resolved in §10-§12 (A0/A0.5 re-specified in §11; health feedback loop added in §12). Ballot Q1-Q9 ratified (§8). RED items and Q10-Q15 still wait for Gabriel (§8).

## 0. Ground rules (unchanged, restated so every PR inherits them)
- **Goal priority:** (1) no committed state or event is lost or corrupted, (2) close the Cowork-to-Code loop, (3) no confidential or unvalidated data crosses the seam or reaches Notion, (4) operability, (5) native capabilities, without breaking the file seam.
- **Ratified baselines, never contradicted silently:** ADR-0001 through ADR-0006 (ADR-0002 file-seam transport; ADR-0003 locking and the 30s settle; ADR-0004 event-first commit; ADR-0006 D1-D4) and NON-GOALS.md. Any change to one of them is a **separate RED, docs-only AMENDMENT PR**. It is ratified and merged on `main` *before* the dependent code PR opens, and the code PR cites the amendment's merge SHA. Ratification is never a success criterion inside a code PR.
- **Already done, not re-planned:** PR #4, #8, #10, #11 (OI-264, OI-270, blocker auth, orphan locks, id paths).
- **Engineering contract:**
  - Test-first: a failing test is committed before the fix.
  - The 267 baseline tests stay green.
  - REFUTE (adversarial reviewer, Opus 5.5 or higher, at least as strong as the implementer) is mandatory on every shared-state writer and on all egress code.
  - No code runs against the live seam; tests use temp seam trees.
  - Every PR can be reverted on its own.
  - Hard Invariants apply.
- **Tiers:**
  - GREEN: additive, read-only, no writer, no egress.
  - YELLOW: changes lock, lease, event or validation semantics, with deterministic tests.
  - RED: Notion egress, an ADR amendment, anything destructive or irreversible, a live-seam write, a new seam writer, or a blast-radius hook or settings file.

## 1. What changed from the draft (summary of structural fixes)
1. **The trust boundary now comes before the injection channel.** B3 is split. **B3a** (inbound validate plus untrusted tagging plus resolved-path scope, M25/M28) has no V1 dependency and hard-gates B2 and B5. Until B3a merges, B1, B2 and B5 are **metadata-only**: ids, from/to, state, age and counts, with no free text.
2. **One ownership-transfer primitive.** `archive_lock_if_unchanged` (`tools/_aaos_common.py:513-555`, which its own docstring calls "Not a compare-and-swap") is **banned on ownership-deciding paths**. A1a builds a single ADR-0006-D2-conforming primitive: an O_EXCL reclaim token held across read, judge and swap, an expiring token, the canonical path never vacated, and a content-verified release. The drain lock (A1b), `acquire_lease` and the sweep (A6) all consume it. It gets one deep REFUTE.
3. **A1's invariant is restated** so it is achievable and testable. It was "exactly one drainer folds". It is now: *no event is ever absent from the union NDJSON ∪ pending ∪ _drained; duplicates are deduped by event_id; the NDJSON is never replaced from a stale base* (pre-replace re-hash guard).
4. **Locality first.** A read-only **L0 locality spike** moves to the front: how do interactive and scheduled Cowork sessions actually reach `Claude_Workspace/AAOS`? The precondition "all seam writers share one NTFS volume" becomes a ratified ADR-0003 amendment before any CAS code is designed.
5. **Validation is prospective and starts in shadow mode.** Shadow mode tags, aggregates and escalates, but still folds. Hard rejection applies only to pending files drained after a recorded cutover. `read_events` flags invalid events and never drops them. A supervised census runs on a *copy* of the live log first, with explicit approval. A supervised `readmit` path exists.
6. **A YELLOW substrate cutover procedure** (§6) covers any PR that changes lock, drain or sweep behaviour:
   - forward-compatible filenames
   - a MIGRATIONS.md entry
   - pausing `ora-aaos-expire-leases` (supervised, in-turn approval) until every live tool path runs the new commit.
7. **The R0 monolith is split** into per-PR DQ gates (R0a now, R0b after L0). A0, A0.5, C0 and B1-metadata start immediately.
8. **The time parser and input floors come first** (A0.5), so A1 and A6 build on a single tz-aware parser and their REFUTE and mutation evidence stays valid.
9. **A6 is split** into a/b/c/d. There is a single archive owner (B4), and the A5 "no unlink" grep is scoped by helper.
10. **Goal 2 is described honestly.** B1, B2 and B5 are *notification*; the loop is still gated on a human. A supervised CC-fleet consumer (B7) is put to Gabriel as a DQ, measured by handoff-to-action latency.

### 1a. Delta table vs the draft spec (extends, does not overwrite)
| Draft item | Hardened change | Reason |
|---|---|---|
| D4: amend ADR-0006 for CAS | Implement ADR-0006 D1-D4 as already ratified; drain-lock CAS goes through AMD-0004 / AMD-0006 (docs-only, merged before the code PR) | ADR-0006 D2 already ratifies the compare-and-swap; the missing piece was implementation |
| A1: reuse `archive_lock_if_unchanged` | Banned on ownership-deciding paths; A1a builds one O_EXCL reclaim-token primitive | The helper's own docstring says it is not a compare-and-swap |
| A3: input bounds + heartbeat | Split: CLI floors and tz-aware `--now` go to A0.5 (ratified 30s settle and 5-min skew stay defaults, not floors, per Q4); heartbeat bounds stay in A3 | One tz-aware parser must exist before A1/A6 |
| A5: Windows CI, doc drift ships with A5 | Windows CI becomes A0a/A0b (tests before the CAS code); doc drift becomes C0 | Evidence must exist before the code that flips it |
| D3: defer all Tier C | M20, M21, M52, M35 pulled forward (C0, A7, A4a); M43/M44/M45/M16/M17/M19/M37/M42/M51 stay in V2-BACKLOG with reasons | Hard-delete (Invariant 6), live-seam demos, phantom seam |
| B3: one PR | Split into B3a (inbound trust) and B3b (execution-time backstop) | B2/B5 inject untrusted text and must wait for B3a |
| D2: read-only native surfaces | Kept; B2 gated on B3a; B5 scheduled digest added | A SessionStart surface alone does not close goal 2 |
| D5: windows-latest only | Kept, plus L0 locality spike and V1 OneDrive soak | windows-latest proves NTFS, not OneDrive |
| D1 / D6 | Kept unchanged (B1 and C0 run in parallel with Tier A, they have no shared-state writer) | |

## 2. Sequenced roadmap

### Phase P0: no gate, start now
| ID | Title | Tier | Findings | Depends on |
|---|---|---|---|---|
| **A0a** | Windows CI leg, non-required (`continue-on-error`), plus inventory of baseline failures on Windows; `permissions: contents: read`; actions pinned by SHA (spec §11.1) | GREEN | M29, M30 (permissions + pins only; the `coverage --branch` floor is deferred to V2-BACKLOG) | none |
| **A0b** | Characterization tests as **unittest** (`expectedFailure`, `subTest`, `mock.patch`) for M01/M02 and the `_exclusive_link` fallback; deterministic three-caller test for `assert_lease_held` (spec §11.2) | GREEN | M32, M31 (partial: lock-vs-task.lease and forced interleave), M01/M02 as expectedFailure | A0a |
| *A0* | Umbrella name for A0a + A0b used by the dependency columns below | | | |
| **A0.5** | CLI input validators + tz-aware `--now` (spec §11.3) | YELLOW (validation; touches the scheduled sweep CLI, so the §6 verification run applies) | M03 (invalid-value rejection only), M43 (naive `--now` behaviour only; helper dedup stays deferred) | A0b |
| **C0** | Doc drift | GREEN | M47, M48, M49, M52 (demos get a live-path refusal banner) | none |
| **L0** | Locality spike (read-only, Gabriel-executed Cowork probe) | GREEN (observation only; no writes, no code) | precondition for M01/M02 design; M46 | none |

### Gate R0a: decisions Phase 1 needs (ballot in §8, items Q1-Q9)
### AMENDMENTS (RED, docs-only, each its own PR, each a surfaced AMENDMENT event)
- **AMD-0003:** the locality precondition (single NTFS volume for all seam writers, from the L0 outcome); the settle and skew floor semantics if Q4 changes them; the routing enforcement in `acquire_lease` (M38) if Q11 is accepted.
- **AMD-0004:**
  - drain-steal via the shared primitive
  - prospective inbound validation, with flag-not-drop for history
  - fold-order restatement: oldest-first *within a drain batch*; `read_events` is the canonical ordered view (amends AAOS-SPEC.md:84)
  - StateCommitFailed reconciliation semantics
  - the milestone-mirror deviation (F0 writes the event_log.md side only, if Q8 is accepted)
- **AMD-0006:**
  - the D2 primitive also governs the drain lock
  - an extended conforming-changes table: M02 sweep re-match, the line-884 conditional archive, the M03 skew floor on foreign-lease judging, the M04 expired same-holder fall-through
  - token filename convention
- **AMD-NG:** an ADR-0001/NON-GOALS note if Q12 (created_by scoping) is accepted.

### Phase P1: substrate hardening (YELLOW unless noted; every substrate PR follows the §6 cutover)
| ID | Title | Findings | Depends on |
|---|---|---|---|
| **S0** | Event schema: fixed reason vocabulary (`refs.reason` enum), MIGRATIONS entry, COWORK-BOOTSTRAP type-list sync | enables M08/M07/M23/M39 | A0, Q5 |
| **A1a** | Shared ownership-transfer primitive (additive, not wired in); module-level no-op interleave hook that only tests patch; a test proving the default is a no-op and that no env or config activates it | M01, M02 design | A0, A0.5, AMD-0006 merged, L0 answer |
| **A1b** | Drain lock adopts A1a + content-verified release (replaces the unconditional `os.unlink` at :1211) + pre-replace NDJSON re-hash guard | M01 | A1a, AMD-0004 merged |
| **A1c** | Inbound validation, **shadow mode**; aggregated, sanitized rejection events emitted *after* the drain-lock release; skew-window timestamp check before sort; supervised `aaos readmit` | M08, M22 | A1b, S0, census approval (Q6) |
| **A2** | `commit_state_change` retry: lease and state re-verified before each retry; elapsed-time budget; staged tmp retained for supervised replay; StateCommitFailed honoured by fail-task and the projection | M07, M06 (exclusive publish for the handoff write-once) | A0.5, S0, AMD-0004 |
| **A3** | Heartbeat bounds (NaN/Inf/negative/future); `derived_at` freshness; one aggregated "liveness unknown: heartbeat chain not running" escalation per sweep | M09, M10, M23 | A0.5, S0, Q2 |
| **A6a** | Trivial guards: line-884 conditional archive, M04 expiry-aware no-op | M04, (M02 part) | A0.5, AMD-0006 |
| **A6b** | `acquire_lease` and `assert_lease_held` decide from the lock (D1) via A1a; D4 mutation coverage | M02 / ADR-0006 D1-D2 | A1a, AMD-0006 |
| **A6c** | Sweep re-match plus D3 ceiling via A1a; M11 unparseable or naive `expires` → `_escalate_corrupt` | M02, M11, M33 (far-future TTL characterization) | A6b, S0 |
| **A7** | Scoped unlink guard (`_discard_own_temp()` + `_release_drain_lock` as the only allowlisted callers, each tested to touch only its own artifact); `publish-contracts.py:270` archives instead of deleting; contract-drift manifest single-sourced | M20, M21 | A1b (so the release path is final) |

### Phase P2: comms (notification), trust, egress
| ID | Title | Tier | Findings | Depends on |
|---|---|---|---|---|
| **B1** | `aaos inbox` / `aaos status` + health. **Metadata-only** (ids, from/to, state, age, counts). `validate.py` runs on every listed item and invalid items are marked. Import-graph test: never imports `archive_*`, `commit_state_change`, `emit_event` or `drain_event_spool`. Health checks: correct `_corrupt` path (`events/aaos-events.ndjson.pending/_corrupt/`, fixture on the real layout); `_drained` ⊆ NDJSON event_id reconciliation; OneDrive conflict copies of the NDJSON or drain lock in `events/` and `tasks/`; dehydrated or offline attributes; event-vs-state divergence (the ADR-0004 detector); Escalated events in the last window; log size and drain duration with a threshold. Cloud-provider errnos (WinError 362/389) map to exit 2. | GREEN | M36, M37, M16 (mitigation), M23 (escalation visibility) | A0b |
| **B1s** | `expire-leases` SWEEP summary line | YELLOW | M41 | A0b |
| **B3a** | Inbound trust: `validate.py` mandatory on inbound tasks and handoffs; untrusted-content tagging in the B1 renderer; resolved-path scope matching (casefold + junction resolution, Windows parity test); receiver artifact-hash verify | YELLOW | M25, M28, M26 | B1, A1c |
| **B2** | SessionStart `additionalContext`: metadata-only before B3a; after B3a, free text only inside explicit untrusted delimiters and only after the A4a matcher has screened it. Skipped when `agent_type` marks a subagent. FileChanged debounced and keyed to new handoff ids, not mtime. Explicit short timeout, fail-open. | YELLOW (blast-radius: project `.claude/settings.json`) | M37 | B1, B3a, A4a (matcher) |
| **A4a** | Notion egress gate: screen the **entire serialized payload** (every string, including task_id, project_id, owner and title) with the **same** matcher `confidentiality-guard.py` uses (imported, or vendored with a parity test); NFKC + casefold + separator collapse; titles **omitted or hashed by default** (verbatim is opt-in, Q7); exit 2 on any match; validator rejects new task_ids that match a roster term. Test decodes the captured body and walks every string; fixtures include diacritics, case, hyphen/underscore/space variants and split rich-text runs. REFUTE includes address, LLC-name and codename variants. | RED | M27, M35 (contract test against a real HTTP listener, not a patched urlopen) | A0b, Q7 |
| **B4** | Terminal-state exits (unblock/retry/archive) through `commit_state_change` with events; **sole owner of the archive transition** (flag in place, files stay in `tasks/` unless Q10 ratifies a move); every reader of `tasks/` enumerated (lease-task, expire-leases, complete-task no-op, fail-task, post-handoff, COWORK-BOOTSTRAP recipes) with a test that archived tasks are ignored; retryable `--blocker` (M39); break-glass only past expiry or with in-turn confirmation, via A1a + a LeaseContested/Escalated pair, REFUTE | YELLOW | M39, M40, M05, M50 | A2, A6c, S0 |
| **A4b** | Projection: age in title, STALE in title, correct staleness (newest input; true when there are no inputs), queue columns (expiry, age, verdict); terminal states excluded **by filter**; versioned child page plus a pointer block with a run-id marker; old pages archived, never deleted; live publish needs in-turn confirmation | RED (egress) | M12, M13, M14, M15 | A4a, B4 |
| **B5** | Daily digest to `ggoldberg@oxfordra.net` only: a one-line result **every day**, including "healthy, N checked"; exit 2 or a missed run is an alert; "unread" is derived purely from events (no cursor file); idempotency test; registered via `register-scheduled-task` as a **local-file CC-fleet** task, verified in automation-health-monitor on a supervised first run | YELLOW (internal send, pre-authorized by routing canon; registration supervised) | M36 (notification) | B1, B3a, A3, A1c, Q3 |
| **F0** | Health-loop metrics: opaque ids, counts and enum reasons only (no titles, detail or raw slugs; slugs hashed); A4a matcher fails closed on every proposal and digest; golden CI test with a roster fixture embedded in ids and details; event_log.md side only (per AMD-0004 / Q8) and declared a writer | YELLOW | M24 (ADR-0004 mirror, event_log.md side only per Q8) | B1, A4a, Q8 |

### Phase P3: supervised, RED
| ID | Title | Depends on |
|---|---|---|
| **V1** | OneDrive contention soak. `AAOS_SOAK=1` interlock: `seam_root()` raises if the resolved path is, or is under, `DEFAULT_SEAM_ROOT`, and every tool refuses to run without an explicit root. The soak uses a distinct project id (not `ora-ops`) and a soak-specific Cowork bootstrap. A live-seam listing hash is taken before and after. **Timebox 2 sessions**; the defined outcome "Cowork contender not reproducible" falls back to Code-only contenders. Gates production enablement of A1b/A6 *under multi-device sync* and B6; **does not gate B3a/B3b**. | A1b, A6c, L0, log-segmentation decision (Q13) |
| **B3b** | PreToolUse backstop. Signal = **process-local env var set by the adapter at lease time, plus a live `assert_lease_held` lock read** (A6b), never a seam marker. Scope cached in-process at lease time. The blast-radius denylist is **unconditional** (no `write_scope` can whitelist it). Cowork allowlists come from a repo-tracked Code-side policy file, never from task.json. Bash parsing is defense in depth; permission deny rules are added for those paths. Fail-closed only while a lease is held; kill-switch env var; short timeout; supervised break-glass documented in OPERATIONS. Extends the existing confidentiality-guard/autonomy-gate chain (ordering analysed) rather than running in parallel. Tests: hook crash → block only in AAOS-lease state; no lease → no effect. Sessions outside the repo get no backstop until Q14. | B3a, A6b, Q9 |
| **B6** | Cowork validated-writer plugin. RED build, success scoped to **interactive desktop Cowork only**; cloud-scheduled Cowork writes go to V2-BACKLOG with the reason (local MCP is desktop-only) | V1, B3a |
| **B7** | Supervised CC-fleet consumer that leases eligible claude-code tasks in a narrow allowlisted scope and posts the return handoff. Success = measured handoff-to-action latency | B3b, Q15 |

### Deferred (V2-BACKLOG, each with a reason)
- M43 helper dedup (beyond the A0.5 behaviour part), M44 and M45 (god-module split, per D3); M17, M19 perf; M42; M51.
- **M18 (fixed 30s settle on uncontended acquisitions): dropped, report-only.** The 30s settle is ratified (ADR-0003) and ADR-0006 leaves it out of scope; changing it is an ADR-0003 amendment with no safety gain, so it is not planned.
- **M34 (failure-injection matrix re-runs other classes and asserts hard-coded counts):** deferred to V2-BACKLOG; A0b's `expectedFailure` tests and A0.5's grep test assert invariants directly, so the matrix is not relied on. Reopen if the matrix is reused as a gate.
- **M16 log segmentation stays deferred only if Q13 says so.** The B1 conflict-copy and reconciliation detectors are its mitigation. Reopen trigger: NDJSON > 5 MB, p95 drain > 2 s, or any conflict copy detected.
- Cloud-scheduled Cowork writer (from B6).
- Event-emitting hooks (D6).

## 3. A1 family: invariants and tests (resolves M01 objections)
- **Primitive (A1a):**
  1. `O_EXCL`-create `<lock>.reclaim-token` (token carries holder, pid and expiry; a stale token is itself reclaimed only via the same O_EXCL rule).
  2. Re-read the lock bytes and compare them to the judged bytes; on mismatch, release the token and lose.
  3. Stage the sidecar and `os.replace` it onto the lock path (never vacated).
  4. Release the token.
- **Release (A1b):** content-verified. The holder renames its lock to the archive, compares, and restores without clobbering if it is not its own. A stolen-and-replaced holder therefore never deletes the new holder's lock.
- **Forward-compatible names:** token and sidecar files end in `.reclaim-token` / `.swap-<pid>`. Never `.lock` or `.json`. A test runs the pre-change sweep enumeration predicate (copied fixture) and asserts these names are ignored.
- **Pre-replace guard:** re-hash the NDJSON immediately before `os.replace`. If it changed since the read, abort the fold and leave the spool pending.
- **Tests:**
  - Interleavings injected at **every** os call in steal, swap and release (enumerated list in the test module), including a third caller inside the former vacate gap (ADR-0006 Verification #1) on both the drain and lease paths.
  - Hypothesis-style property test asserting the union invariant.
  - D4 mutation: each ownership-deciding comparison mutated must fail at least one test.
- **Claim boundary:** `windows-latest` proves NTFS/Win32 semantics only. Conflict copies, cross-surface exclusivity and hardlinks in a synced folder stay **UNVERIFIED** until V1.

## 4. A1c validation rollout (resolves goal-1 vs goal-3 objections)
1. **Census.** A supervised, read-only run of the new validator over a *copy* of the live NDJSON, `pending/` and `_drained/` (copy requires approval, Q6). It reports counts by reason. No code reads the live tree.
2. **Shadow mode.** At least one soak window. Events are tagged; one aggregated `Escalated(reason=InboundInvalid, count=N)` is emitted per drain *after* the lock is released, built only from sanitized fixed fields. The events are still folded.
3. **Hard mode** applies only to pending files drained after the recorded cutover timestamp. Pre-cutover history is **flagged, never dropped**, by `read_events`, fail-task's DoubleFailure count and the projection.
4. **Registry unreadable:** the drain skips (events stay pending); `read_events` never filters.
5. **COWORK-BOOTSTRAP §7** is updated in the same PR. Tests:
   - the bootstrap's literal example event passes;
   - a pre-A1 Cowork fixture corpus folds with zero quarantines;
   - a 10 KB hostile filename cannot abort a drain;
   - a late earlier-timestamp arrival is tolerated and counted.

## 5. A2 compensation semantics
- Before each retry, re-verify that the lease is held and the state file is byte-unchanged; abort if not. This removes the lost-update hazard.
- The retry budget is elapsed time (tested against simulated multi-second sharing violations), not a count.
- On exhaustion, the staged tmp is kept for supervised replay and `Escalated(reason=StateCommitFailed, refs.event_id=<compensated>)` is emitted. fail-task and project-notion treat the referenced event as superseded for state derivation. The B1 divergence detector reports any remaining mismatch as needs-action.
- A0b lands the unittest `expectedFailure` clobber test first.

## 6. YELLOW substrate deploy procedure (record in OPERATIONS.md, used by A1b, A1c, A2, A6*, B4)
1. Forward-compatible filenames, or an enumeration-tolerance change merged one PR earlier.
2. A `docs/MIGRATIONS.md` entry (protocol note, cutover timestamp).
3. Pause `ora-aaos-expire-leases` via `register-scheduled-task` / `update_scheduled_task`, **with in-turn approval**, from merge until the main checkout and the scheduled-task tool path are confirmed on the new commit (`git rev-parse` on each).
4. Resume, and verify the next sweep run in automation-health-monitor.
5. Rollback = revert the PR and repeat steps 3-4.

## 7. Engineering OS notes
- Lanes:
  - A0 is L3 `/build` (tests + CI).
  - A0.5 is L2-style behaviour fix via `/build` with TDD.
  - AMD-* are docs-only RED.
- Dispatch: implementer Opus 5.5 for A1a/A1b/A2/A6*/A4a/B3*. REFUTE reviewer is Opus 5.5 or higher, independent of the implementer.
- Merge gate: a bot check is never a verdict. Blast-radius PRs (B2, B3b settings and hooks) need an adversarial subagent pass.

## 8. HUMAN-RATIFICATION-REQUIRED backlog and decision ballot
**Status 2026-10-17: Q1-Q9 ratified (option A each). Q10-Q15 remain open until the L0 spike returns.** Ratifying Q1-Q9 does not ratify the RED items listed at the end of this section; each still needs its own in-turn confirmation.
Each item is multiple choice with the recommended option first; Gabriel answers in chat. R0a = Q1-Q9 (needed for Phase 1). R0b = Q10-Q15 (after L0).

**Q1 (sequencing): first implementation PRs**
**RATIFIED 2026-10-17: option (A): A0 Windows CI + tests, then A0.5 input floors.**
- (A) A0 Windows CI + tests, then A0.5 input floors **(Recommended)**: the tests that prove the CAS exist before the CAS code; no ADR touched.
- (B) A1 drain CAS directly: fastest on M01, but designed before the L0 locality answer and the ADR amendments, which the pre-mortem rejected.
- (C) A4a Notion gate first: closes egress sooner, but it is RED and needs Q7 before it can start.

**Q2 (DQ5): heartbeat-absent behaviour**
**RATIFIED 2026-10-17: option (A): one aggregated liveness-unknown escalation per sweep.**
- (A) One aggregated "liveness unknown" escalation per sweep, surfaced in B1 and B5 **(Recommended)**: no false positives or false negatives, and it has a reader.
- (B) Classify as NoSignal silently: quieter, but a truly dead holder is never escalated.
- (C) Keep per-reclaim DeadAgent: status quo, 100% false-positive noise.

**Q3 (DQ10): digest policy**
**RATIFIED 2026-10-17: option (A): one-line daily digest including healthy days.**
- (A) A one-line daily result every day, including "healthy, N checked"; a missed run or exit 2 alerts **(Recommended)**: dead-man coverage.
- (B) Send only on breach: less mail, but silence is indistinguishable from a dead task.
- (C) No digest; B1 on demand only: no loop closure at all.

**Q4: ratified settle and skew floors in CLI input validation**
**RATIFIED 2026-10-17: option (A): reject invalid values only; ratified 30s/5-min stay defaults.**
- (A) Reject only invalid values (ttl ≥ 1, skew ≥ 0, settle ≥ 0); the ratified 30s and 5min stay defaults, not floors **(Recommended)**: closes M03's negative-skew attack with no ADR-0003 amendment, and tests keep `--settle-sec 0`.
- (B) Enforce the ratified values as floors on foreign-lease judging (5-min skew floor): stronger, but it is an ADR-0003/0006 amendment and breaks fast tests without a test-only override.
- (C) No floors: leaves M03 open.

**Q5: event-schema extension for reasons**
**RATIFIED 2026-10-17: option (A): enumerated `refs.reason` event field (S0).**
- (A) Add an enumerated `refs.reason` field, with a MIGRATIONS entry and COWORK-BOOTSTRAP sync (S0) **(Recommended)**: one vocabulary, closed schema preserved.
- (B) Encode reasons in `detail` (max 200 characters): no schema change, but free-text drift and an injection surface.
- (C) New event types per reason: fine-grained, but every PR edits the enum.

**Q6: census copy of the live NDJSON, pending/ and _drained/ (read-only, copied to scratch, deleted to `/archive/` after)**
**RATIFIED 2026-10-17: option (A): one supervised read-only census copy before A1c (the copy itself still needs in-turn confirmation when A1c starts).**
- (A) Approve one supervised copy before A1c **(Recommended)**: sizes the false-positive rate before anything is quarantined.
- (B) Skip the census and rely on shadow mode only: no live-data copy, but sizing comes only after deploy.
- (C) Neither: unbounded risk of quarantining legitimate in-flight events.

**Q7 (DQ7): Notion title policy (RED egress)**
**RATIFIED 2026-10-17: option (A): omit/hash titles by default, verbatim opt-in per project.**
- (A) Omit or hash titles by default; verbatim only per-project opt-in; full-payload screen fails closed **(Recommended)**: a gap in roster coverage cannot leak anything.
- (B) Verbatim titles with the roster screen and `[CONFIDENTIAL]` substitution: more readable, but aliases, addresses and LLC names escape substring matching.
- (C) Pause Notion publish until B4/A4b: zero egress risk, but no projection.

**Q8: ADR-0004 milestone mirror (event_log_ref)**
**RATIFIED 2026-10-17: option (A): recorded deviation: event_log.md side only, NDJSON mirror deferred to V2.**
- (A) Record a surfaced deviation: F0 writes only the event_log.md side; the paired NDJSON milestone is deferred to V2 with its reason **(Recommended)**: no new seam write.
- (B) Route the mirror event through the normal emit path now: full ADR-0004 compliance, but a new seam writer that needs its own REFUTE.
- (C) Drop the mirror requirement from ADR-0004: simplest, but it is an amendment removing an audit link.

**Q9 (DQ14): backstop scope for B3b**
**RATIFIED 2026-10-17: option (A): project-scoped lease-gated hook; global settings untouched.**
- (A) Project-scoped hook, active only while the session holds a live lease (adapter env var + lock read); global settings untouched **(Recommended)**: no self-lockout, no blast-radius change to `~/.claude`.
- (B) Also install globally via a supervised `~/.claude/settings.json` change: covers seam work from other cwds, but touches blast-radius config.
- (C) No hook; rely on B3a validation only: less machinery, but no execution-time defense.

**Q10 (DQ6): archive semantics (R0b)**
- (A) `archived` flag in place; files stay in `tasks/`; the projection filters terminal states; B4 is the sole owner **(Recommended)**: no reader breakage, no two-step move.
- (B) Move to `tasks/_archive/` with a crash-recovery test and every reader updated: a cleaner directory, but a new non-atomic transition plus reader churn across the tools and Cowork recipes.
- (C) No archive: unbounded projection (M24 open).

**Q11 (DQ8): routing enforcement, owner_agent/to_agent (R0b; ADR-0003 amendment if accepted)**
- (A) Enforce in `acquire_lease` against the registry after AMD-0003 merges; a registry-unreadable lease acquisition fails closed **(Recommended)**: closes M38.
- (B) Warn-only (Escalated on mismatch): no ADR change, but spoofed holders proceed.
- (C) Leave unenforced.

**Q12 (DQ9): created_by scoping for Cowork-authored tasks (R0b; ADR-0001/NON-GOALS note if accepted)**
- (A) Cowork-authored tasks run under a repo-tracked Code-side allowlist policy, never their own `write_scope`; blast-radius paths unconditionally denied **(Recommended)**: closes M25 escalation of scope.
- (B) Trust `write_scope` after B3a schema validation: simpler, but seam-controlled scope.
- (C) Forbid Cowork-authored claude-code tasks entirely: safest, but kills the loop.

**Q13: NDJSON log segmentation (M16) before V1**
- (A) Defer, with B1 conflict-copy and reconciliation detectors plus size and drain-duration thresholds as mitigation and a stated reopen trigger **(Recommended)**: avoids redesigning the fold mid-hardening.
- (B) Build immutable dated segments before V1, so the soak measures the production design: more correct, but touches the fold again (an ADR-0004 amendment).
- (C) Defer with no detector: silent-loss risk unmeasured.

**Q14: L0 / V1 Cowork leg**
- (A) Gabriel runs a one-time desktop Cowork probe in a scratch folder (read-only listing of how it reaches the seam); if it is not reproducible within the 2-session timebox, proceed with Code-only contenders **(Recommended)**: bounded, unblocks design.
- (B) Skip the Cowork leg entirely: faster, but the cross-surface exclusivity assumption is never tested.
- (C) Block A1 until a full Cowork contention soak passes: maximal evidence, unbounded stall.

**Q15: B7 autonomous consumer (goal 2)**
- (A) Approve design-only now; build after B3b, scoped to an allowlisted task class, success measured by handoff-to-action latency **(Recommended)**: honest path to a closed loop.
- (B) Accept notification-only (B1/B2/B5) and record the human-gated loop in V2-BACKLOG: lowest risk, but goal 2 is not met.
- (C) Build now on B3a alone: fastest, but it executes seam-authored work without the execution-time backstop.

RED items needing Gabriel's ratification: AMD-0003, AMD-0004, AMD-0006, AMD-NG, A4a, A4b live publish, V1, B3b, B6, B7, and any pause or resume of `ora-aaos-expire-leases`.

## 9. Objection resolution ledger
| Objection | Severity | Resolution |
|---|---|---|
| B1/B2/B5 injection before trust boundary | critical | B3a split out and made a hard dependency of B2/B5; metadata-only until then; subagents skipped; A4a matcher applied to injected text (§2 P2) |
| Drain/NDJSON fold on OneDrive, locality settled too late | critical | L0 at P0; AMD-0003 locality precondition before A1a; B1 conflict-copy + `_drained`⊆NDJSON reconciliation; A1 design gated on L0 |
| `archive_lock_if_unchanged` as CAS | critical | Banned on ownership paths; A1a D2 primitive; third-caller-in-gap tests on drain and lease paths (§3) |
| B3 gated on V1 | high | V1 removed from B3a/B3b dependencies; V1 timeboxed with a Code-only fallback; gates only A1b/A6 multi-device enablement and B6 |
| Merge = deploy into multi-writer seam | high | §6 cutover procedure, forward-compatible names, MIGRATIONS entry, supervised sweep pause |
| read_events retroactive filtering / historical events | high | Flag-not-drop for pre-cutover; census on a copy (Q6); hard mode only for new drains; bootstrap literal-example test (§4) |
| A6 silent ADR-0006 amendment; D4 mismatch | high | AMD-0006 with the extended conforming table, merged before A6*; D4 reconciled (amendment is its own PR, not "lands in A1") |
| A1 tiered YELLOW while carrying an ADR change | high | Every ADR change is a separate RED docs PR, ratified first; code cites the SHA; ballot lists every ADR touched |
| A1 shadow/readmit/registry-unreadable | high | Shadow mode, `aaos readmit`, registry-unreadable → drain skips, zero-quarantine corpus test |
| A4a task_id/project_id bypass | high | Full-payload screen, shared matcher, normalization, task_id validator rule |
| B2 before B3 | high | Same as the critical item above |
| B3 scope semantics | high | Unconditional denylist; repo-tracked policy; process-local marker; permission deny rules |
| V1 soak seam-root guard | high | `AAOS_SOAK` interlock in `seam_root()`; distinct project id; soak bootstrap; listing-hash diff; RED |
| F0 confidential persistence | high | Opaque ids, hashed slugs, fail-closed matcher on outputs, golden test; declared a writer |
| Goal 2 claimed via notification | high | Relabelled; B7 consumer DQ (Q15); latency metric |
| A2 retry lost-update / non-honoured compensation | high | Re-verify before each retry; divergence detector in B1; consumers honour StateCommitFailed (§5) |
| A1 quarantine un-commits events | high | AMD-0004 amendment; prospective validation; paired state ref in the rejection event |
| Two CAS primitives | medium | One primitive (A1a) |
| Time parsing after A1/A6 | medium | A0.5 first; `_parse_iso_dt` already tz-aware at `_aaos_common.py:443`; CLIs call one shared `parse_cli_now` |
| A4b/B4 archive overlap | medium | B4 sole owner; A4b filters only; reader enumeration test |
| A5 unlink grep | medium (x3) | A7 scoped guard via `_discard_own_temp()` + release helper; `publish-contracts.py:270` archives |
| New escalation reasons vs closed schema / re-entrancy | medium | S0 vocabulary; sanitized aggregated events emitted after lock release; hostile-filename test |
| B5 dependencies / silent failure | medium (x2) | Depends on A3, A1c, B3a; daily heartbeat line; event-derived "unread"; local-file CC-fleet verification |
| B3 marker undefined / FileChanged churn | medium (x2) | Env var + lease read; debounce keyed to handoff ids |
| B3 onFailure:block lockout | medium | Lease-only scope, in-process scope cache, timeout, kill-switch, break-glass |
| Files-On-Demand | medium | Pinned-folder precondition checked in B1; errno → exit 2; hook timeout tested with a blocking-read fixture |
| A0 "real proof" overclaim | medium | Claim restated as NTFS-only; OneDrive guarantees UNVERIFIED until V1 |
| Full-NDJSON rewrite / rotation deferral | medium | Q13 ballot plus a size and drain-duration metric with a reopen trigger |
| Priority-1 detection of already-lost events | medium | B1 reconciliation + conflict-copy detectors |
| B3 hook vs existing guard chain | medium | Extend the chain; RED + REFUTE |
| Routing/created_by/skew-floor silent ADR changes | medium | AMD-0003 / AMD-NG tied to Q4, Q11, Q12 |
| A6 too large | medium | Split a/b/c/d with separate REFUTE and revert boundaries |
| A0 injection points tiered GREEN | medium | Injection hook lives in A1a (YELLOW), as a module-level no-op patched only by tests; A0 uses monkeypatching of `os.*` only |
| A4b idempotency/never-delete | medium | Versioned child page + pointer + run-id; archive old pages |
| A3 NoSignal false negatives | medium | Q2(A) aggregated "liveness unknown" |
| R0 single ballot | medium/low | Split into R0a/R0b; per-PR DQ gates; A0/A0.5/C0/B1 ungated |
| A4a second matcher | medium | Import or vendor with a parity test; hash/omit by default |
| A2 bundling M43 | low | M43 stays deferred; A0.5 adds one shared CLI parse call only |
| Fold order criterion (x2) | low | Restated per batch; AMD-0004 amends SPEC:84; late-arrival test |
| B1 GREEN with writer path | low | SWEEP line split to B1s; import-graph test |
| B6 tier and NON-GOALS fit | low | RED build; desktop-only scope; cloud writes to V2 |
| F0 `_corrupt` path / ADR-0004 mirror | low | Path fixed with a real-layout fixture; Q8 |


## 11. A0 / A0.5 PR specifications (corrected; resolves validation defects 1-9, 10, 11, 14)
Shared rules for A0a, A0b and A0.5:
- **Runner is unittest.** CI runs `python -m unittest discover -s tests -p "test_*.py"` and `requirements.txt` holds only `jsonschema`. No pytest, no new dependency, no change to how the 267 baseline tests run or to the `testsRun` count wrappers in `tests/test_failure_injection_matrix.py`. Use `@unittest.expectedFailure` (an unexpected success fails the run, which is the strict behaviour wanted), `subTest` for parametrization, `unittest.mock.patch` for `os.*` injection, and a `setUp`/`addCleanup` guard that patches `seam_root`/`DEFAULT_SEAM_ROOT` and fails the test if the live default seam is ever resolved.
- **Finding ids are M-ids from `confirmed-findings.json`**, never audit rank numbers. (The first draft of A0/A0.5 mixed them; the same slip was also corrected in the §2 rows for A1c, A3, A6c, A7, B1, B1s, B2, A4a, A4b and F0.)

### 11.1 A0a: Windows CI leg (GREEN, one small PR)
- `.github/workflows/ci.yml`: matrix `os: [ubuntu-latest, windows-latest]` x `python-version: ['3.11', '3.13']` (the existing 2x2 versions are kept; a 3.13-only Windows leg is acceptable if runtime is a concern). The Windows legs run with `continue-on-error: true` and are **not a required check**. Add top-level `permissions: contents: read`; pin `actions/checkout` and `actions/setup-python` by full commit SHA with a tag comment.
- Inventory: the PR body lists every baseline test that fails on Windows, each with a per-test reason and a filed issue. **No blanket marking.** A test is marked `expectedFailure` on Windows only with its own issue and reason. The Windows legs become required only after they are green.
- Not in this PR: a `coverage --branch` floor (M30 remainder). Written into V2-BACKLOG with the reason: it needs a coverage dependency and a baseline measurement first.
- Revert = revert one PR.

### 11.2 A0b: characterization tests (GREEN; `tests/` only plus one OPERATIONS.md paragraph)
New modules `tests/test_windows_fs_semantics.py` and `tests/test_drain_lock_interleaving.py`, all unittest, all in temp seam roots.
1. **`_exclusive_link` fallback (M32).** The fallback lives in `_exclusive_link` (`tools/_aaos_common.py:704-708`), which on a non-`FileExistsError` `OSError` calls `_exclusive_create` (`:669`), whose docstring documents a zero-byte window. Tests:
   - Patch `os.link` to raise `EPERM`: assert the dest is published with the exact bytes, and a `FileExistsError` collision still propagates (passes on HEAD).
   - Zero-byte window: a deterministic hook between the O_EXCL create and the write (no polling reader thread, so it cannot flake) observes the dest at length 0. Marked `@expectedFailure` against the invariant "never observable empty"; this documents the defect and flips when M32 is fixed.
   - Characterize `_exclusive_publish` (`:1052-1090`) raising `OSError` when hardlinks are refused (the drain lock could then never be acquired). Recorded as a newly documented gap, with a one-line decision note on whether `_exclusive_publish` needs a fallback at all.
2. **M31 partial.** Deterministic three-caller interleave through `assert_lease_held` with `task.lease` disagreeing with the lock file, forced by `os.*` patching rather than an N-thread barrier (barriers are probabilistic, the weakness M31 names). The remainder of M31 (full ADR-0006 verification suite) is carried by A6b and listed in the A6b DoD.
3. **M01 / M02, two tests each (resolves defect 4 and 11).**
   - *Invariant test (implementation-independent):* two real `drain_event_spool` calls driven through a seam hook at the judge-to-swap boundary; assert the union `NDJSON ∪ pending ∪ _drained` holds every event_id and a successor's lock survives. `@expectedFailure` on HEAD; **A1b's DoD requires flipping it to a plain pass**.
   - *Characterization test of the current window:* a **path-filtered, reentrancy-guarded** wrapper around `os.replace`: it fires only when `src` is the drain-lock path and `dst` is under `events/_archive`, nested calls (the fold, `atomic_write`, state commits) use the real function, it is restored in `finally`, and the test asserts both the hook fire count and the call-site filter. **A1b must rewrite or remove this test.**
   - Same pair for the unconditional release (`os.unlink` at `:1211`) deleting a successor's lock, and for the M02 sweep re-match.
4. `docs/OPERATIONS.md`: one paragraph: CI proves NTFS/Win32 semantics only; OneDrive guarantees are UNVERIFIED until V1.
- Written-first evidence: the M01/M02 tests are committed first and shown failing on HEAD (run without the marker, output in the PR body); then marked. Tests expected to pass on HEAD (the `EPERM` publish/collision cases) are asserted to pass on both OSes; a Windows-only failure is a newly discovered defect, filed as an `expectedFailure` plus an issue, never patched in this PR.
- REFUTE focus: the hook actually fired (assert it); no `expectedFailure` can pass for the wrong reason (assert the specific invariant, not "raises"); the live seam is never resolved.
- Size: roughly 250-400 test lines plus a paragraph; one review.

### 11.3 A0.5: CLI input validators and tz-aware `--now` (YELLOW, validation only)
- `tools/_aaos_common.py`: add `parse_cli_now(now_str, flag="--now")`: empty means `datetime.now().astimezone()`; otherwise call the existing tz-aware `_parse_iso_dt` (`:443`) and re-raise `CorruptStateError` as `UsageError`. No other helper consolidation (M43 dedup stays deferred).
- The seven CLIs' `_resolve_now` bodies become a call to `parse_cli_now`. **`expire-leases.py` is an explicit change, not a one-liner:** `_resolve_now(args.now)` is called at `:104`, outside the `try` whose `except aaos.UsageError` (`:232`) returns exit 2, so a naive `--now` would escape as a traceback with exit 1. Move the now-resolution inside the `try` (or catch `UsageError` around it).
- argparse type validators: `lease-task --ttl-min` int >= 1; `lease-task` and `expire-leases --skew-min` int >= 0; `lease-task --settle-sec` finite float >= 0. Only invalid values are rejected; the ratified 30s settle and 5-min skew stay defaults (Q4-A), so there is no ADR-0003/0006 amendment and `--settle-sec 0` keeps working in tests. The `ttl >= 1` floor sits alongside the ADR-0006 D3 ceiling (`LEASE_TTL_CAP`, in the conforming-changes table) without implementing it; the validator must stay compatible with that future cap.
- `docs/MIGRATIONS.md` entry: "CLI rejects naive `--now` and zero/negative TTL, negative skew/settle, NaN/Inf from <commit>".
- Findings: **M03** (invalid values; the negative-skew attack on a live foreign lease) and **M43 (behaviour part only)**. M11 stays owned by A6c; M33 (far-future TTL characterization) is added to A6c only when its test exists.
- Tests (written first, failing on HEAD), unittest via the existing `_tool_loader` subprocess pattern on a temp seam:
  - naive `--now` through each of the 7 CLIs (`subTest`): exit 2 and `USAGE ERROR` on stderr;
  - `lease-task --skew-min -5`: exit 2 and a live foreign lock fixture is byte-unchanged and no lock file is created;
  - `--ttl-min 0` and `-1`; `--settle-sec -1`, `nan`, `inf`: exit 2 (argparse's own usage output asserted separately, since it is not `UsageError` text);
  - positive controls: aware `--now`, `--settle-sec 0`, `--skew-min 0`, defaults behave as before.
- REFUTE focus: no path reaches `datetime.fromisoformat` outside `_parse_iso_dt` (a grep test); no argparse default or env var bypasses a validator; and whether exit 2 breaks a documented recipe or the sweep command line.
- **Cutover (defect 14):** A0.5 changes the `ora-aaos-expire-leases` sweep CLI contract, so it follows §6 in reduced form. Before merge: attach the read-only grep of `COWORK-BOOTSTRAP.md`, `OPERATIONS.md`, `CODE-ADAPTER.md` and the scheduled task's `SKILL.md` for `--now`, `--skew-min`, `--ttl-min`, `--settle-sec`. After merge: the MIGRATIONS entry plus **one supervised verification run of the sweep** is a done-criterion. Pause/resume of the task is required only if the grep finds a non-default invocation.
- Depends on A0b. Size: about seven call sites plus roughly 30 lines of helpers, plus tests; one review.

## 12. Substrate-health feedback loop (design only; resolves validation defect 13)
Closed loop: **measure -> threshold -> proposal addressed to Gabriel -> acknowledgement or closure**. It never auto-applies anything and never builds or registers a task in this plan; B1 supplies the measurements and B5 the delivery.
| Metric (read-only, computed from the event log and B1 health checks) | Window | Initial threshold (proposal; tuned from the A1c census and the first soak) |
|---|---|---|
| Lease reclaims per day | 7 days | > 3x the trailing 4-week median, or any day with > 5 |
| Aggregated "liveness unknown" escalations (Q2-A) | per sweep | any two consecutive sweeps |
| Drain steals / contested acquisitions | 7 days | any steal; > 2 contested acquisitions |
| Shadow-mode `InboundInvalid` rejections (A1c) | per drain | any rejection after cutover; > 0 persisting 3 days |
| `_drained` vs NDJSON event_id reconciliation gap (B1) | each B1 run | any non-zero gap |
| OneDrive conflict copies in `events/` or `tasks/` (B1) | each B1 run | any |
| NDJSON size / p95 drain duration | daily | > 5 MB or > 2 s (the M16 reopen trigger) |
- **Computed by:** B1 (read-only, metadata-only; never imports a writer). **Delivered by:** the B5 daily digest to `ggoldberg@oxfordra.net` only.
- **Breach output:** a proposal line in the digest naming the metric, value, threshold, and the plan item it would reopen (for example, M16 segmentation, or reopening the CAS design). It is addressed to Gabriel; applying anything stays a supervised action.
- **Closure state:** a breach is recorded as an event (`Escalated`, enumerated reason, counts only). It stays "open" in the next digests until a later B1 run shows the metric back under threshold or Gabriel records a ratified waiver in V2-BACKLOG. Unacknowledged breaches repeat daily.
- **Confidentiality:** telemetry holds opaque ids, counts and enum reasons only (no titles, `detail`, or raw slugs; slugs hashed). The A4a matcher fails closed on every digest and proposal (F0 golden test).
- **Done-definition for building it later:** a breach fixture produces a digest proposal; a cleared fixture closes it; a second run is idempotent.


### 10a. Resolution map (added after Gabriel ratified Q1-Q9)
| Defect (addendum order) | Resolved in |
|---|---|
| High: A0 test aimed at wrong code / wrong HEAD expectation (also the duplicate "M31/M32 fallback describes code that does not exist") | §11.2 item 1 |
| High: finding-id traceability (also the duplicate "wrong ids in A0.5") | §2 row corrections, §11 shared rules, §11.3 |
| Medium + High: pytest vs unittest (two entries) | §11 shared rules |
| Medium: `expectedFailure` tied to `os.replace` at `:1187` / process-wide patch (two entries) | §11.2 item 3 |
| Medium: Windows baseline has never run | §11.1 (non-required leg first, inventory, per-test reasons) |
| Medium: A0 drops parts of M30/M31 | §2 A0a/A0b rows, §11.1, §11.2 item 2 |
| Medium: "supersedes" the draft spec | Header, §1a delta table |
| Medium: no visible closed-loop feedback | §12 |
| Low: `expire-leases` `_resolve_now` outside the `try` | §11.3 |
| Low: A0.5 skips the cutover procedure | §11.3 cutover bullet |

All 14 entries below remain as the historical record.


---

## 10. Validation addendum (post-hardening, 14 defects: 5 high / 7 medium / 2 low)

Apply these corrections to A0 / A0.5 before execution. Several duplicate each other (pytest vs unittest, finding-ID mapping, os.link target).

- **HIGH**: A0 test (a) is aimed at the wrong code and its expected HEAD result is wrong. _exclusive_publish (tools/_aaos_common.py:1052-1090) has no os.link fallback: an OSError propagates after the tmp file is unlinked. The only fallback is in _exclusive_link (:684-708). It falls back to _exclusive_create (:669), whose own docstring says the file 'exists, empty, between the O_EXCL create and the write'.
  - Fix: Retarget test (a) at _exclusive_link, the real M32 seam (:704-708). Change the expectation to what the M32 fix calls for: patch os.link to raise EPERM, assert that DENIED/FileExistsError semantics hold, and assert that a collision still propagates. If a 'never zero-byte' assertion is kept, it is a known defect and must be a strict expected-failure (or documented as accepted), not 'expected to pass on HEAD'. Separately, characterize _exclusive_publish raising OSError when hardlinks are refused (the drain lock then can never be acquired) as a newly documented gap. Replace the reader thread that polls the path with a deterministic hook between create and write, so the test cannot flake.
- **HIGH**: Finding-ID traceability mixes AUDIT-REPORT rank numbers with M-ids. A0.5 claims 'M21 (naive --now)', but M21 is contracts-drift/manifest-dup; naive --now is rank #21, which is M43. It claims 'M34 (far-future/negative TTL wedge)', but M34 is meta-test count guards; the TTL wedge is rank #34. A0 attributes the os.link fallback test to M31, but that is M32. M31 (assert_lease_held against a lock file, forced three-caller interleaving) is claimed but no test in A0 covers it.
  - Fix: Remap every addressesFindings entry to M-ids from confirmed-findings.json. A0.5: M03, M43 (behaviour part only), and the M-id behind rank #10 (naive 'expires' never escalated, input side). Drop M21 and M34 from A0.5. Add the far-future TTL characterization (rank #34's M-id) only if it is actually tested. Note that its upper cap is ADR-0006 D3 and stays out of scope. A0: list M32 for the fallback test, and either add the M31 tests (task.lease vs lock-file disagreement, deterministic three-caller interleave through assert_lease_held) or drop M31.
- **MEDIUM**: A0 and A0.5 are written for pytest (pytest.mark.xfail(strict=True), parametrize, monkeypatch, guard fixture), but the repo runs `python -m unittest discover` and requirements.txt contains only jsonschema. Switching runners is not '~15 lines of YAML' and would change how the 267 baseline tests run, including the testsRun count-wrappers in test_failure_injection_matrix.py.
  - Fix: Write the new tests as unittest. Use @unittest.expectedFailure: since Python 3.4 an unexpected success makes wasSuccessful() False, which matches strict-xfail. Use subTest for parametrization, unittest.mock.patch for os.* injection, and a setUp/addCleanup guard that patches seam_root/DEFAULT_SEAM_ROOT and fails the test if the live default is resolved. Alternatively, scope the pytest adoption explicitly as its own GREEN PR before A0.
- **MEDIUM**: The M01 strict-xfail is tied to the current os.replace call at :1187. Patching os.replace inside _aaos_common patches the global os module, which also intercepts atomic_write (:315), D's own steal and fold replaces (:1313, :1330, :1351) and the harness. Once A1b replaces the path-based os.replace with the reclaim-token protocol, the 'hook fired' assertion fails. That still counts as an expected failure, so the marker never flips and the promise that 'strict-xfail forces removal' does not hold.
  - Fix: Make the wrapper path-filtered: it fires only when src == the drain lock path and dst is under events/_archive, and it is reentrancy-guarded so D's nested acquisition calls the real os.replace. Split the test in two. (1) An implementation-independent invariant test drives two real drain_event_spool calls through a seam hook placed at the judge-to-swap boundary, and asserts that the union NDJSON ∪ pending ∪ _drained holds every event_id and that a successor's lock survives. (2) A separate characterization test of the current os.replace window, which A1b is required to delete. State in A1b's DoD that it must rewrite or remove test 2, and must flip test 1 to a plain pass.
- **MEDIUM**: The A0 gate says the full suite (267 + new) is green on windows-latest, and also says any windows failure becomes a strict-xfail plus an issue and is never patched. The 267 baseline has never run on Windows: M29 notes no platform-aware tests, and the OI-270 comments say behaviour diverges. Baseline failures would force the A0 PR to mark existing tests xfail, which is not additive, masks regressions, and can push A0 beyond one review.
  - Fix: Land the windows-latest leg first as continue-on-error, or as a non-required check, in a minimal GREEN PR, and inventory the baseline failures. Make it required only once green. Any baseline test that has to be marked xfail on Windows needs its own issue and per-test reason, and is listed in the PR body; no blanket marking. Keep the 3.11/3.13 versions as a 2x2 matrix, and keep a 3.13-only Windows leg if runtime is a concern.
- **LOW**: A0.5 understates the call-site change. In expire-leases.py, _resolve_now is called at :104, outside the try block whose `except aaos.UsageError` (:232) returns exit 2. A UsageError raised by a naive --now therefore escapes as a traceback with exit 1. Turning the body into a one-line call to parse_cli_now does not meet the test's 'exit 2 for all 7 CLIs'. A0.5 also edits lease-task --ttl-min, which ADR-0006's conforming-changes table already assigns (the LEASE_TTL_CAP plus catch-all row), and does not cross-reference it.
  - Fix: Move expire-leases' now-resolution inside the try, or catch UsageError around it, and list that as an explicit change rather than a one-liner. Have the parametrized test assert exit 2 and 'USAGE ERROR' on stderr for each CLI. Also add an argparse-validator case asserting argparse's own exit 2 and stderr usage, since that output is not UsageError text. In the PR, note that the ttl>=1 floor sits alongside the ADR-0006 D3 ttl cap without implementing it, and that the A0.5 validator must stay compatible with the future LEASE_TTL_CAP.
- **HIGH**: A0/A0.5 tests are written for pytest, but CI runs unittest and pytest is not installed
  - Fix: A0 and A0.5 depend on pytest.mark.xfail(strict=True), monkeypatch fixtures and parametrize. .github/workflows/ci.yml:34-37 runs `python -m unittest discover`, and requirements.txt lists only jsonschema. As written, the new test modules fail at import and turn CI red, and the strict-xfail mechanism never runs. Either write them in unittest form (unittest.expectedFailure, where an unexpected success fails the run; subTest; unittest.mock.patch), or add an explicit sub-step to A0: add pytest to requirements, switch the CI command, and prove the 267-test baseline count is unchanged under the new runner.
- **HIGH**: The A0 M31/M32 fallback test describes code that does not exist and wrongly expects a pass on HEAD
  - Fix: _exclusive_publish (tools/_aaos_common.py:1052-1091) has no OSError fallback: os.link raises and the error propagates. _exclusive_create (:669) never calls os.link. The fallback lives in _exclusive_link (:703-708), and it calls _exclusive_create, whose own docstring says it has a zero-byte window. M32 says exactly this: the fallback reopens the OI-264 window. A 'never observe zero-byte' reader assertion against that fallback should fail (or flake) on HEAD, not pass. Retarget the test to _exclusive_link. Make the zero-byte-window case a strict expected-failure, as M01/M02 are, and assert the DENIED/FileExistsError semantics separately, per the M32 fix text. Separately, decide whether _exclusive_publish needs a fallback at all, and record that decision.
- **HIGH**: Wrong finding IDs in A0.5 break the 52-finding coverage accounting
  - Fix: A0.5 claims M21 for 'naive --now', but M21 is the contracts-drift / PUBLISHED.json hard-delete finding in check-contract-drift.py and publish-contracts.py. It claims M34 for the 'far-future/negative TTL wedge', but M34 is the failure-injection meta-test count guards. Naive --now is M43 (dup CLI helpers) and M11 (unparseable/naive expires silent wedge). Relabel A0.5 to M03, M43 (behaviour part) and M11 (input side). Then re-run the 52-finding cover-or-defer matrix: M21 and M34 currently read as covered when they are not, and M11 is double-claimed with A2 or A6.
- **MEDIUM**: A0 claims to address M30 and M31 but drops parts of their remedies
  - Fix: M30's fix includes a coverage --branch floor, and A0 says 'no coverage gate yet'. M31's fix asks for task.lease vs lock-file disagreement (assert_lease_held) tests and a FORCED three-caller interleaving. A0 instead adds an N=8 thread-barrier test, which is still probabilistic, the exact weakness M31 calls out. Mark M30 and M31 as PARTIAL in addressesFindings, name the PR that carries the remainder or defer it explicitly to V2-BACKLOG, and replace the barrier test with a deterministic forced interleaving using os.* patching.
- **MEDIUM**: The A0 interleaving injection patches os.replace process-wide
  - Fix: In _aaos_common, os.replace is the global os module attribute. The same function is used by the fold (:1313, :1330, :1351, :1357), state commits (:953) and atomic_write (:315), not only the steal at :1187. Patching it unconditionally perturbs every call and can make the strict-xfail pass or fail for the wrong reason. Make the patched replace path-filtered: it fires only when src is the drain-lock path and dst is under the events _archive. Restore it in a finally block, and assert both the hook fire count and the call-site filter.
- **MEDIUM**: The plan says it supersedes the draft spec instead of extending it, and silently reverses the draft's A1 primitive choice
  - Fix: The header reads 'Supersedes: the draft spec (D1-D6)'. The lens requires extending the draft, not overwriting it. The draft A1 says to reuse archive_lock_if_unchanged; the hardened plan bans that helper on ownership paths. The ban is technically justified (its docstring says 'Not a compare-and-swap'), but it should be recorded as an explicit delta against the draft. Reword the header to 'Extends ... D1-D6 remain ratified', and add a delta table (draft item, hardened change, reason) for each changed item. Examples: the A1 primitive, A3 becoming A0.5 with floors kept as defaults, and A5 Windows CI moving to A0, which contradicts the draft's 'doc drift ships with A5' ordering.
- **MEDIUM**: No closed-loop health feedback is visible in the deliverable
  - Fix: Neither tierATargets nor the plan head nor residualRisks describes the measure -> threshold -> stop-for-Gabriel proposal loop that the Phase-3 flywheelDesign required. The only traces are 'automation-health-monitor is the detector' and M16 size/drain-duration thresholds that 'only trigger a reopen', both detect-only or write-only. Add a concrete section that defines: metrics (lease reclaim rate, DeadAgent escalations, drain contention or steals, shadow-mode rejected inbound events, the B1 _drained reconciliation gap), each with a numeric threshold and window; who computes them (read-only); the breach output (a proposal artifact or event addressed to Gabriel that never auto-applies); and an acknowledgement or closure state, so the loop closes. Also state that the telemetry never persists confidential terms.
- **LOW**: A0.5 changes the scheduled sweep's CLI contract but skips the YELLOW cutover procedure
  - Fix: A0.5 modifies expire-leases.py, the ora-aaos-expire-leases sweep, to newly reject inputs with exit 2. §6 applies the cutover procedure to any PR changing sweep behaviour. A0.5 waives it on the strength of a pre-flight grep that has not been run yet. Either attach the grep evidence of the scheduled task's actual command line before classifying the cutover as not required, or keep the MIGRATIONS entry plus a supervised verification run of the sweep after merge as a done-criterion.

## 13. L0 locality findings (read-only, 2026-10-17)
Evidence gathered without any write to the seam or any Cowork session: the live seam's own artifacts from the one past Cowork round-trip (2026-07-10), and structure-only fields (no titles or content) of this machine's Cowork session metadata.

**Established**
1. **Cowork does not run on the seam's NTFS volume directly. It runs locally in a per-session VM.** All 529 recorded local Cowork sessions (202 interactive, 327 scheduled) carry a `vmProcessName` and `hostLoopMode: true`; none is cloud-hosted. This reverses the draft precondition "all seam writers share one NTFS volume": Code writes natively on NTFS, Cowork writes through a VM folder mount of the OneDrive-synced host folder.
2. **The seam was reached through a mount.** The 2026-07-10 session mounted `Claude_Workspace` and `C:\Users\Owner\AAOS`, and its own handoff says Probe X ran "path-translated to this session's mount" inside a "Cowork sandbox" (a session-local pip environment). Result: `MODE=SAFE-BY-HAND`, `CONTRACTS_SOURCE=git-tree`.
3. **Scheduled Cowork sessions are local too** (327 of 327 have a VM process and a local session file), consistent with the standing note that both fleets fire only while the Desktop app is open. This is the pre-October-6 local mode; the newer cloud-scheduled mode cannot reach local paths and is out of scope (B6).
4. **The Desktop app classifies OneDrive folders as `cloud-sync`** (vs `local`) in `resolvedFolderKinds`, so the mount layer knows the folder is sync-managed.
5. **Cowork stamps UTC (`Z`), Code stamps a local offset** (seen in `aaos-events.ndjson`). Both are timezone-aware, so correctness holds, but any filename or lexical-ordering assumption across surfaces is unsafe (M22).
6. **The Cowork leg is stale.** Only 6 of 529 sessions ever mounted `Claude_Workspace`; the last AAOS-seam session was 2026-07-10. Nothing since has exercised cross-surface locking.

**Not established (cannot be read from metadata)**
- The mount mechanism (virtiofs, 9p, or SMB-like) and whether `O_EXCL` create, `os.link`, and rename-over-existing are atomic and immediately visible across it.
- Whether a Cowork write is visible at the host path synchronously, and in what order relative to OneDrive sync.

These need either a live read-only Cowork probe (mount type, `stat` inode and link count, clocks) or the V1 soak (writes, distinct project id).

**Design consequences**
- AMD-0003's locality precondition is rewritten: the CAS primitive (A1a) must be correct for **Code on NTFS vs Cowork over a VM mount**, and may not assume NTFS atomicity on the Cowork side. Until V1 says otherwise, treat Cowork as an untrusted-atomicity writer: it must never hold the drain lock or a lease-reclaim token. It writes spool files and handoffs only, which is already the Recipe 1/2 contract.
- B1's conflict-copy and `_drained` reconciliation detectors matter more: cross-surface sync is the likeliest source of conflict copies.
- The Q14 recommendation strengthens: option A (one desktop Cowork probe), with Code-only contenders as the fallback.
