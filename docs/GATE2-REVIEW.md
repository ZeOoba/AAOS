<!-- Copy this file when closing out any phase gate (Gate 0 through Gate 5). One checklist item per exit criterion named in the master prompt's Phase N section — do not paraphrase the criteria away. -->

# Gate 2 Review — Foundation Build

**Status:** Self-assessed PASS
**Reviewed by:** independent sonnet-tier task reviewer (fresh read, no prior authorship), plus a fix-and-reverify loop
**Date:** 2026-07-09

## Exit criteria checklist

- [x] **Full test suite green; validator rejects malformed fixtures and accepts valid ones (evidence: test output showing both directions).**
  Evidence: `python -m unittest discover -s tests -p "test_*.py" -v` → **18/18 OK**, independently re-run by the orchestrating session (not just the implementer's own report). Covers both directions per schema: valid task/handoff/heartbeat/event-log fixtures pass; malformed fixtures (missing required field, bad enum value, wrong `protocol_version` major, write-scope violation, unknown `from_agent`, unknown `suggested_next_owner`, non-object top-level JSON, corrupt/missing registry) are all rejected with distinct, correctly-worded errors and the spec'd exit codes (1 for content violations, 2 for usage/environment errors).

- [x] **A hand-authored sample task + handoff round-trips through the validator.**
  Evidence: `examples/t-sample-001.json` and `examples/h-sample-001.json`, hand-authored (not template boilerplate), both re-validated independently by the orchestrating session against the real production `agent-registry.json`: `VALID: examples\t-sample-001.json` (exit 0), `VALID: examples\h-sample-001.json` (exit 0).

- [x] **Secret scan clean.**
  Evidence: `grep -rliE "api[_-]?key|secret|password|BEGIN (RSA|OPENSSH|PRIVATE) KEY|ak_[a-z0-9]{10,}|xox[baprs]-|sk-[a-zA-Z0-9]{20,}" .` (excluding `.git/`) over the full tree, run by the orchestrating session before staging. 4 hits, all inspected individually and confirmed false positives: two are prose describing the "secret scan" process step itself (in the master-prompt transcription and ADR-0002), two are a test fixture (`h-invalid-write-scope.json`) and its assertion in `test_validate.py` using the literal filename `secret.json` as an example out-of-write-scope path string, not an actual secret value. No API keys, tokens, or credentials of any kind in the tree.

- [x] **Structure matches spec exactly — diff of spec tree vs. actual tree is empty.**
  Evidence, with an honest caveat: ADR-0002's directory-layout block illustrates the AAOS repo's **full, eventual, all-5-phases** shape, not Phase 2's scope alone. Comparing against what Phase 2 specifically was scoped to produce: `schemas/*.schema.json` (5/5, exact match), `templates/{task,handoff}.template.json` + `templates/{adr,gate-review,BUILD-STATE}.template.md` (5/5, exact match), `agent-registry.json` (present, 2 entries), `tools/validate.py` (present; `validate.ps1` was not also written — the master prompt's own wording is `validate.(ps1|py)`, i.e. either, not both), `docs/{AAOS-SPEC,NON-GOALS,V2-BACKLOG,MIGRATIONS,OPERATIONS}.md` + `docs/adr/0001-0005-*.md` (all present), `.gitignore` (present). Correctly **absent** (out of Phase 2's scope, deferred to later phases per the ADR-0002 illustration itself): the 10 Phase-3 queue/lock/event/heartbeat/Notion-projection scripts under `tools/` (create-task.py, lease-task.py, etc.) and `docs/COWORK-BOOTSTRAP.md` (Phase 4). Justified additions not shown in the illustrative ADR-0002 tree: `docs/CAPABILITY-MAP.md` and `docs/2026-07-09_AAOS_MasterPrompt_v02.md` (relocated from Phase 0, per ADR-0001/0002's own two-tree decision), `docs/PHASE1-DESIGN-REVIEW.md` and this file (gate-review audit trail — the deliverables manifest names "gate-review records for all six gates" as a deliverable, so these belong), `examples/` (the concrete artifact proving the hand-authored-round-trip criterion above), `archive/_validate_all.py` (a Phase-1 one-off script preserved per Hard Constraint 1's never-delete rule, not part of the live system), `tests/` contents (fixtures + test suite, an implementation necessity the illustrative tree didn't itemize). No unexplained file exists anywhere in the tree.

## Findings from adversarial review

An independent sonnet-tier reviewer read `tools/validate.py`, its tests, the templates, and the round-trip examples fresh, ran the existing suite itself, and additionally executed 7 adversarial probes not in the original test suite (Windows-backslash-vs-forward-slash glob matching, directory-traversal escape, corrupt/unparseable JSON input, NDJSON line-number accuracy, registry-load failure handling, and a doc-vs-implementation drift check on every write_scope-adjacent field). Findings:

1. **[Critical]** `match_write_scope`'s "colonization guard" was bypassable via a `../` path-traversal artifact path — `fnmatch` matched on the literal string prefix without resolving `..` segments, so a handoff could declare an artifact path that lexically started inside an agent's `write_scope` but actually resolved outside it (e.g. into `C:/Windows/System32/`), and the validator wrongly accepted it.
2. **[Important]** A missing or corrupt `--registry` file exited 1 (folded into a generic write-scope-violation message) instead of the spec's own exit-2 usage-error convention, inconsistent with how a missing target file or `--schema-dir` were already handled correctly.
3. **[Important]** `handoff.schema.json`'s own field description promises "`validate.py` checks [`suggested_next_owner`] is a real id," but the original implementation never referenced that field at all — a documentation/implementation drift on a schema-promised consumer contract.
4. **[Minor]** A non-dict top-level JSON value was mislabeled as a `protocol_version` mismatch instead of its own accurately-worded "not a JSON object" error.
5. **[Minor]** The module docstring pointed to the wrong ADR for write_scope's rationale (0003, about locking, instead of the actual source in `AAOS-SPEC.md` §6).

## Resolutions

All 5 findings **FIXED**, none merely accepted:
1. Fixed via `posixpath.normpath` canonicalization of the artifact path (resolving `.`/`..` segments) before glob-matching, chosen over `os.path` specifically to stay platform-independent given the project's forward-slash write_scope convention. New regression tests cover both the traversal-out case (now correctly rejected) and a traversal-that-still-resolves-inside case (still correctly accepted, proving the fix isn't overly broad).
2. Fixed: registry loading moved to `main()`, `OSError`/`JSONDecodeError` on load now exits 2 with a `USAGE ERROR:` prefix, matching the file's other usage-error paths. New regression tests cover both a missing and a corrupt registry file.
3. Fixed: `suggested_next_owner`, when present, is now checked against the loaded registry's agent ids and fails validation (distinct message) if unrecognized. New regression tests cover both a valid and an unknown value.
4. Fixed: dict-type check now runs before the `protocol_version` check, restoring the correct, previously-dead error branch.
5. Fixed: docstring now points to `docs/AAOS-SPEC.md` §6.

Post-fix, both the full suite (now 18 tests, up from 11) and the two Critical/Important repro commands were independently re-run by the orchestrating session (not just the fix-agent's own report) and confirmed green / fixed before this gate was called closed.

## Verdict

**PASS.** All four Gate 2 exit criteria hold with pasted evidence; every design-review finding was fixed (none needed to fall back to "accepted with reasoning"). Per the master prompt's "hard gates, no skipping" doctrine, this session is stopping here — Phase 3 (Coordination Layer: queue operations, lock manager, lease-expiry sweep, heartbeat derivation, Notion projection) does not begin without review of this gate.
