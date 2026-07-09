# AAOS Phase 1 — Formal Design Review: Findings & Resolutions

**Reviewer:** independent opus-tier agent, fresh read (no prior authorship of the design), per the master prompt's Phase 1 requirement: "Formal design review: re-read the spec adversarially against §1 constraints, §2 non-goals, and the Phase 0 capability map. Log every finding and resolution."

**Scope reviewed:** `docs/AAOS-SPEC.md`, all 5 ADRs, all 5 schemas, `docs/NON-GOALS.md`, `docs/V2-BACKLOG.md`, `docs/MIGRATIONS.md`, against the master prompt's Hard Constraints (§1), Non-Goals (§2), and `docs/CAPABILITY-MAP.md`.

**Overall verdict from the reviewer:** no BLOCKING (Hard-Constraint-halting) issue found. Two SHOULD-FIX findings, four MINOR/NOTE findings. All Gate 1 checklist items passed on the reviewer's own independently-re-run evidence (word count, meta-schema validation).

## Findings and resolutions

### 1. SHOULD-FIX — Agent-registry `id`/`capabilities` enums contradicted the "add one entry, nothing else changes" extensibility thesis
**Finding:** `agent-registry.schema.json` hard-coded `id: enum [claude-code, cowork]` and a closed `capabilities` enum. A third agent's registry entry would be rejected by the schema itself, contradicting the spec's own repeated claim (§1, §11) that onboarding a new agent requires only one registry entry, and contradicting Non-Goal #3's "~2–5 entries" framing (unreachable past 2 without a schema edit).
**Resolution: FIXED.** `id` is now an open pattern-constrained string (`^[a-z][a-z0-9-]*$`); `capabilities` items are likewise open pattern strings instead of a closed enum. Membership/existence validation moves to `tools/validate.py`'s registry-membership check (a Phase 3 tool), consistent with how every *other* agent-id reference in the schemas (`owner_agent`, `from_agent`, `to_agent`, `suggested_next_owner`) was already validator-checked rather than schema-enum-locked. Verified: all 5 schemas still pass `Draft202012Validator.check_schema` after the fix (re-run, evidence below).

### 2. SHOULD-FIX — "Verified" language overstated the Phase 0 seam status
**Finding:** ADR-0001, `AAOS-SPEC.md` §3, and `NON-GOALS.md` #1 all described the Code↔Cowork integration seam as "verified," when `CAPABILITY-MAP.md` §3 explicitly scored it "evidenced-but-not-live-confirmed" (strong circumstantial evidence, but no completed two-way round trip from a single Code session — Hard Constraint 6 forbids fabricating state as settled).
**Resolution: FIXED.** All three files reworded to "evidenced (not yet live-confirmed)" / "candidate integration seam," with an explicit pointer to the Phase 4 dry-run and first-live-task gates as where this gets genuinely re-checked before anything depends on it in production. `_seam-probe.md` (written in Phase 0) remains the live artifact a future Cowork session confirms.

### 3. MINOR — Escalation row is a structured artifact with no schema (accepted with reasoning)
**Finding:** The §10 human-escalation row (13 fields, referenced from `event.json`'s `refs.escalation`) is as structured as any of the five contract-first schemas but has no `escalation.schema.json`, so `tools/validate.py` can't check it.
**Resolution: ACCEPTED, not fixed, with reasoning.** The master prompt named exactly five schemas; escalation rows are a Notion-native, human-facing artifact rather than agent-to-agent coordination state, so leaving it outside the JSON-Schema-checked contract set is a deliberate scope choice, not an oversight. Logged as V2-Backlog item 6 ("A sixth `escalation.schema.json`") so the option is preserved if escalation volume ever makes this painful in practice.

### 4. MINOR — Git-tree location deviates from the master prompt's Phase 2 wording without reconciliation (accepted with reasoning, now documented)
**Finding:** The master prompt's Phase 2 task names "Claude_Workspace or brain repo" as the two candidate homes for the AAOS codebase; the design chose a third option, a dedicated repo at `C:\Users\Owner\AAOS\`, without explaining the deviation.
**Resolution: FIXED (documentation gap closed).** Added a paragraph to ADR-0001's Decision section explaining why the brain repo (`oxford-command-center`) was rejected as the codebase home: it's a live, actively-deployed application with its own CI/reconciliation gate/RAM-sensitive dev-server lifecycle unrelated to agent coordination, and folding AAOS into it would blur both codebases' scope and blast radius. No Hard Constraint is violated — `C:\Users\Owner\AAOS\` is not a OneDrive business folder and not a blast-radius path.

### 5. MINOR — `heartbeat_source`/heartbeat `source` were single strings but ADR-0005 specifies two upstream sources per agent
**Finding:** ADR-0005 defines two upstream signals per agent (e.g. cowork: Notion `last_edited_time` *plus* the automation-health-monitor beacon table), but both schema fields were `type: string`, forcing an undocumented compound-string convention on `derive-heartbeat.py`'s eventual implementer.
**Resolution: FIXED.** Both `agent-registry.schema.json`'s `heartbeat_source` and `heartbeat.schema.json`'s `agents[].source` changed to `array of string, minItems 1`, with descriptions citing the two-source case explicitly. Re-validated against the meta-schema.

### 6. NOTE — `event_id` regex silently excluded positive UTC offsets
**Finding:** The pattern `^e-[0-9TZ:.-]+-[0-9]+$` matches `Z` and negative offsets (`-04:00`) but not `+HH:MM`, an undocumented constraint.
**Resolution: FIXED.** Added `+` to the character class: `^e-[0-9TZ:.+-]+-[0-9]+$`. Re-validated against the meta-schema.

### 7. NOTE — Vague `agents` array consumer description; stale Phase-0 path narrative in the capability map
**Finding:** `agent-registry.schema.json`'s top-level `agents` array description read "Consumer: all AAOS tooling" — borderline-generic versus every other field's named-consumer bar. Separately, `CAPABILITY-MAP.md` still narrated the docs as living under the (now superseded) `Claude_Workspace\AAOS\docs\` path.
**Resolution: FIXED (both).** The `agents` array description now names concrete consumers (`tools/validate.py` registry-membership checks, `lease-task.py`/`post-handoff.py` agent resolution, `COWORK-BOOTSTRAP.md` dry-run, `project-notion.py` row labels) — this was addressed as part of the Finding #1 fix to the same block. `CAPABILITY-MAP.md` got a top-of-file "Phase 1 update note" pointing to the new docs location and to this review log, without rewriting the historical Phase 0 snapshot itself.

## Re-verification evidence after fixes

```
$ python -c "import json, jsonschema
for f in ['agent-registry.schema.json','heartbeat.schema.json','event.schema.json','task.schema.json','handoff.schema.json']:
    s = json.load(open(r'C:\Users\Owner\AAOS\schemas\%s' % f, encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(s)
    print('OK:', f)
print('ALL_OK')"
OK: agent-registry.schema.json
OK: heartbeat.schema.json
OK: event.schema.json
OK: task.schema.json
OK: handoff.schema.json
ALL_OK

$ wc -w C:/Users/Owner/AAOS/docs/AAOS-SPEC.md
2234 (up from 2190 pre-fix; still comfortably under the 3,000-word ceiling)
```

## Gate 1 status after resolution

All five Gate 1 exit criteria pass; both SHOULD-FIX findings are resolved (not merely accepted); all MINOR/NOTE findings are either fixed or explicitly accepted with documented reasoning, satisfying "design review findings all resolved or explicitly accepted with reasoning."
