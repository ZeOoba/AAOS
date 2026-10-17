# docs/ index

Live documents are maintained against the current code. History documents record how v1.0 was built and
reviewed; they are not updated when the code moves, so never cite one as current state.

## Live
| Document | Use |
|---|---|
| AAOS-SPEC.md | The protocol specification |
| OPERATIONS.md | Day-to-day runbook, with a symptom-to-command table |
| CODE-ADAPTER.md, COWORK-BOOTSTRAP.md | Per-surface adapter instructions |
| CAPABILITY-MAP.md | What each surface can and cannot do |
| NON-GOALS.md, V2-BACKLOG.md | Scope boundaries and deferred work |
| MIGRATIONS.md | Protocol and CLI behaviour changes, newest first |
| DIAGRAMS.md | Architecture diagrams |
| adr/ | Ratified decisions (ADR-0001 to ADR-0006); an ADR wins over any other doc |

## History (do not treat as current)
| Document | What it is |
|---|---|
| 2026-07-09_AAOS_MasterPrompt_v02.md | The original build prompt |
| PHASE1-DESIGN-REVIEW.md, PHASE3-DESIGN.md, PHASE4-DESIGN.md, PHASE5-DESIGN.md | Phase design records |
| GATE2-REVIEW.md to GATE5-REVIEW.md | Phase gate reviews |
| DELIVERABLES-MANIFEST.md | v1.0 deliverables checklist (partly annotated as historical) |
| _task-reports/ | Per-task reports and transcripts; the `.py` there is a guarded historical rehearsal |

## Single sources
- **Test count:** recorded once, in README.md section 4 (Setup & run). CI is the live source of truth.
- **Sweep schedule:** `list_scheduled_tasks` (live registry). History: the `ora-aaos-expire-leases` sweep ran 3x/day until 2026-08-05, 1x/day until 2026-09-13, then weekly (Mondays 07:20 ET, cron `20 7 * * 1`).
