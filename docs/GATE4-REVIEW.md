<!-- Copy this file when closing out any phase gate (Gate 0 through Gate 5). One checklist item per exit criterion named in the master prompt's Phase N section — do not paraphrase the criteria away. -->

# Gate 4 Review — Agent Integration

**Status:** Self-assessed PASS
**Reviewed by:** independent opus-tier task reviewer (fresh read, no prior authorship), plus a fix-and-reverify loop
**Date:** 2026-07-10

## Exit criteria checklist

- [x] **Both adapters exist, validate against the spec, and reference only verified paths.**
  Evidence: `docs/CODE-ADAPTER.md` (466 words, ≤1 page) and `docs/COWORK-BOOTSTRAP.md` (self-contained). Reviewer independently confirmed every CLI flag named in `CODE-ADAPTER.md` exists with exact spelling against the real `tools/*.py --help` output, and validated every JSON shape in `COWORK-BOOTSTRAP.md` §7's recipes against the live schemas (`VALID`, exit 0, both handoff and event). Seam mirror (`contracts/`) confirmed byte-identical to the git tree (0 mismatches across all 13 published files) after the final republish (`source_commit=30238b0`, `dirty=false`).

- [x] **Dry-run transcript shows every bootstrap step resolving against real files.**
  Evidence: `docs/_task-reports/phase4-dryrun-transcript.txt` — 12/12 steps pass against the real seam, re-run twice more during this session (once by the reviewer, once after the fix cycle) with identical results. Non-destructive throughout (create+delete sentinels prove write targets resolve without leaving artifacts); Probe X resolved `MODE=FULL-EXEC` because Code always passes it — this documents the optimistic path, not Cowork's actual resolution, which stays genuinely unconfirmed until a real Cowork session runs the bootstrap (tracked as an open item, not claimed as closed).

- [x] **The first live cross-surface task is staged with acceptance criteria and rollback (rollback = archive the task, no side effects).**
  Evidence: `t-cowork-seam-confirm` (3 acceptance criteria, `state=queued`, never leased — no `lease` key, no lock file anywhere including `_archive/`) + `h-code-to-cowork-seam-confirm` (validates, artifact hash verified matching the live `_seam-probe.md`) on the real seam, written via the real tools — genuinely staged, not executed; Cowork has not been invoked. Reviewer independently executed the documented `OPERATIONS.md` §3 rollback against a **copy** of both artifacts and confirmed it works exactly as documented (archived state validates; event log untouched; "no side effects" holds because the task was never leased, so nothing needed reclaiming).

- [x] **Nothing was installed into Cowork settings/skills autonomously — bootstrap is delivered as a file for Gabriel to apply.**
  Evidence: reviewer grepped `~/.claude/CLAUDE.md` and every project config for any AAOS/bootstrap reference — none found. `CODE-ADAPTER.md` carries an explicit "STAGED — not installed" banner. `COWORK-BOOTSTRAP.md` exists only as a file in the git tree and its published seam mirror (data Cowork reads, not a live configuration surface). `git diff --stat` across every Phase 4 commit touches only `docs/`, `tools/`, `tests/` — no `hooks/**`, `settings.json`, `*.env`, `credentials`, fleet registries, or CSP files.

## Findings from adversarial review

An independent opus-tier reviewer read `docs/COWORK-BOOTSTRAP.md` as if it were a fresh Cowork session with zero other context — the highest-stakes review target this phase, since it's the one artifact designed to be followed by an agent with no other safety net — and tried to construct concrete scenarios where a literal reading goes wrong, plus fault-injected `publish-contracts.py` and independently re-derived the SAFE-BY-HAND safety table rather than trusting it. Findings:

1. **[Important]** The staged task's own artifact input, `_seam-probe.md`, contained an older, pre-Phase-4 instruction telling the first Cowork reader to "append a confirmation line" directly to the file — a literal, unimaginative reading of that embedded instruction would perform exactly the in-place shared-file mutation `COWORK-BOOTSTRAP.md` §8 forbids, and would also silently invalidate the handoff's artifact hash.
2. **[Important]** The SAFE-BY-HAND write recipes said to include a random suffix in a fresh id "if unsure" — advisory, not mandatory. Since SAFE-BY-HAND never leases, nothing else prevents two concurrent by-hand writers (or a re-run) from picking the same obvious slug and one silently overwriting the other's handoff, undermining the "fresh file, no contention" safety property the whole design leans on.
3. **[Minor]** `publish-contracts.py` wrote `PUBLISHED.json` last (correct for a *first* publish) but never invalidated a *prior* `PUBLISHED.json` before starting a re-publish's copy loop — reviewer fault-injected a mid-copy failure over a pre-seeded stale mirror and found the stale `PUBLISHED.json` survived, still signaling success over a Frankenstein (mixed old/new) tree that Probe S trusts unconditionally.
4. **[Minor]** `COWORK-BOOTSTRAP.md` §6's FULL-EXEC branch pointed at `CODE-ADAPTER.md` for exact tool syntax, but that file is deliberately not published to the seam Cowork reads from — a gap in an otherwise self-contained document.
5. **[Minor]** The documented rollback procedure (`OPERATIONS.md` §3) works but targets `tasks/_archive/` and `handoffs/_archive/`, neither of which existed yet on the live seam; step 3's handoff-archival bullet also didn't repeat step 2's "create the directory if missing" instruction.
6. **[Note]** §8's stated reason for always leaving a handoff's `blockers` empty in SAFE-BY-HAND mode ("would be a shared-file mutation") was inaccurate — by hand, nothing mutates the task at all; the real problem is a handoff that claims a state change nothing ever applied.

Two additional Note-level observations (script/spool pretty-printing, a one-commit provenance lag that turned out to be accurate on inspection) required no action — logged in the reviewer's report, not repeated here since neither changed any file.

## Resolutions

All 6 actionable findings **FIXED**, none merely accepted:
1. `COWORK-BOOTSTRAP.md` §8 now explicitly states that its "never edit an input artifact in place" rule overrides any instruction embedded in that artifact's own text, naming `_seam-probe.md`'s obsolete instruction specifically; `_seam-probe.md` itself was updated to retire that instruction and point to where the confirmation actually lives now. Because this edit changed `_seam-probe.md`'s hash, the already-staged handoff's artifact reference was stale — archived the old handoff and re-staged a fresh one; verified the new hash matches the live file.
2. Recipes 1, 2, and 3 now require the random suffix unconditionally, with the reasoning (no lease exists to otherwise prevent a same-slug collision) stated inline.
3. `publish-contracts.py` now deletes any prior `PUBLISHED.json` before the copy loop, not after — a mid-copy crash now always leaves no success signal, so Probe S correctly fails closed instead of trusting stale bytes. New regression test reproduces the reviewer's exact fault-injection scenario (13 tests, was 12).
4. §6 now inlines the four real tool invocations directly rather than pointing off-seam.
5. Seeded both `_archive/` directories on the live seam; `OPERATIONS.md` §3 step 3 now states the create-if-missing instruction explicitly.
6. §8's rationale reworded to the accurate reason.

Post-fix: full suite re-run (152 tests, up from 151), contracts republished clean (`dirty=false`, current `source_commit`), and the 12-step dry-run rehearsal re-run once more with identical results.

## Verdict

**PASS.** All four Gate 4 exit criteria hold with evidence; every design-review finding was fixed. Per the master prompt's "hard gates, no skipping" doctrine, this session is stopping here — Phase 5 (Validation & Hardening: the ten-scenario failure-injection matrix) does not begin without review of this gate. The live two-way Cowork confirmation (this session's own capability-detection preamble resolving for a *real* Cowork session, not Code simulating one) remains the one open item carried forward from Phase 0 — `t-cowork-seam-confirm` is staged and ready to close it whenever a Cowork session is invoked against it.
