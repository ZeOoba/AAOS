# 0001. Architecture selection: Shared-folder-as-bus with Notion read-only mirror

## Status

Accepted

## Context

AAOS needs cross-surface (Code↔Cowork) work-item state that supports atomic lease/ownership semantics and fail-closed contention handling. The candidate integration seam between the two surfaces is the OneDrive-synced `Claude_Workspace\` tree. Phase 0 gathered strong circumstantial evidence for this seam — pre-existing Cowork-authored content (Artifact-tool output, scheduled-task folders, a Cowork-editable skills clone) already lives there — but explicitly could not complete a live, same-day two-way round trip from a single Code session (see `CAPABILITY-MAP.md` §3: "evidenced-but-not-live-confirmed"). This ADR proceeds on that evidence, not on a false claim of live verification (Hard Constraint 6: never fabricate state); the seam's actual read/write behavior from a real Cowork session is re-checked at Phase 4's dry-run and first-live-task gates before anything depends on it in production. Cowork has no git access, and putting a `.git` repository inside a OneDrive-synced folder is a documented corruption/lock hazard in this environment — it is precisely why the canonical `ora-skills` git clone was deliberately kept outside the OneDrive `Claude_Workspace` tree. Notion is reachable from Cowork via its native MCP connector, but it is non-atomic: it offers no compare-and-swap, no file lock, no `O_EXCL`, and no temp-then-rename primitive, and it is rate-limited and network-dependent. Every AAOS decision is bound to the same priority order: correctness → integration with existing systems → maintainability → reliability → autonomy → speed.

Two candidates were compared.

**Candidate A — Shared-folder-as-bus.** `Claude_Workspace\AAOS\` (OneDrive-synced) holds all live coordination state — queue, locks, handoffs, events, and the heartbeat projection — as plain JSON files, written via atomic write-rename and protected by filesystem exclusive-create locks. Notion is kept as a read-only Derived mirror of queue status. A separate git repository located outside OneDrive, at `C:\Users\Owner\AAOS\`, versions the AAOS codebase — schemas, validators, tools, and docs — but never the live coordination state.

**Candidate B — Notion-as-primary-store.** Notion databases, extending the existing "Cowork Session Ledger" database, serve as the primary task/queue state, with the filesystem relegated to a secondary cache.

The genuine case for B deserves to be stated before it is rejected. First, B uses Cowork's most reliable, native write path — a first-class MCP connector — rather than requiring Cowork to reliably write to a synced folder. Second, it unifies the state store with the human-facing dashboard, since Notion is already the Ops Hub; this is arguably a purer application of "one fact, one home," with no projection or mirroring step required. Third, it is cloud-hosted and durable, and would survive a local disk loss.

B nonetheless loses, in strict priority order. On correctness, the disqualifying criterion, the Notion API offers no compare-and-swap or file-lock primitive: two agents can both PATCH a page's status to "leased" and both succeed silently, with last-write-wins semantics. A real lease with exactly one winner is not implementable on Notion, and the mandatory convention that every state file be written via atomic write-rename has no Notion analogue. On integration, the comparison is closer to a wash but still resolves against B: making Notion primary would abandon five existing extension precedents already established in Phase 0 — the `chip-claims.log` locking pattern, the `append-event-log.py` atomic-append discipline, the `fleet-session-hook.py` atomic write-rename idiom, and the existing probe/gate validator philosophy — and would force Code to mutate state over a rate-limited network, amounting to a large and avoidable new build of Notion-native machinery. On reliability, Notion is network-dependent and rate-limited, and a failed PATCH is ambiguous — it cannot be determined whether it committed — which cannot fail closed cleanly, unlike deterministic local filesystem operations. Finally, B directly contradicts the master prompt's own framing of Notion as "read-mostly … Derived state."

## Decision

Adopt Candidate A. Live coordination state is stored as plain JSON under `Claude_Workspace\AAOS\`, using atomic write-rename and `O_EXCL`-based filesystem leases, hardened for OneDrive sync behavior as detailed in ADR-0003. The AAOS codebase is versioned in a git repository outside OneDrive, at `C:\Users\Owner\AAOS\`, following the existing `ora-skills` precedent of keeping canonical git clones off the OneDrive-synced tree. Notion remains a read-only Derived mirror of queue status only — it is never the primary store. Handoffs formalize the existing chip/`spawn_task` claim-lock and self-contained-context discipline, extended across the Code↔Cowork boundary that the chip system does not cross today.

**On the repo location versus the master prompt's "Claude_Workspace or brain repo" wording (Phase 1 design-review finding):** the master prompt's Phase 2 task names two candidate homes for the git tree. A dedicated `C:\Users\Owner\AAOS\` repo was chosen over the brain repo (`oxford-command-center`) deliberately: the brain repo is a live, actively-deployed React/Hono application with its own CI, its own `$0`-tolerance reconciliation gate, and a RAM-sensitive local dev-server lifecycle entirely unrelated to agent coordination — folding an unrelated codebase into it would blur its scope and its gate, and would make AAOS's git history and blast radius inseparable from the brain's. A dedicated repo keeps "one fact, one home" for both codebases and is a smaller, more surgical footprint than growing the brain repo's remit. `Claude_Workspace` itself was never a candidate for the *codebase* (only for the *live seam*) per the OneDrive/`.git` corruption hazard already cited above.

## Consequences

On the positive side, real atomic leases become achievable, the design reuses and extends five already-proven mechanisms rather than inventing new ones, and Notion's strength in human observability is retained through the mirror. The cost is that this introduces one genuinely new engineering problem — OneDrive cross-surface sync latency and conflict-copy behavior — which is addressed directly in ADR-0003 via a settle-then-rescan protocol that fails closed on any ambiguity. It also means contracts (schemas, spec) must be explicitly published from the git tree into the OneDrive seam so that Cowork can read them, as described in ADR-0002's `contracts/` mirror.

| Criterion (priority order) | A — Shared-folder-as-bus | B — Notion-as-primary |
|---|---|---|
| Correctness (atomic lease, fail-closed) | 5 — O_EXCL + write-rename give real compare-and-swap | 1 — no lock/CAS primitive; leases can't have one winner |
| Integration with existing systems | 5 — extends 5 precedents; Notion kept in its existing mirror role | 3 — great for Cowork; abandons filesystem precedents, forces network mutations from Code |
| Maintainability | 4 — plain JSON, git-versioned code, editor-inspectable | 3 — state split across Notion properties + JSON cache; two-system reconciliation |
| Reliability | 4 — local, deterministic; OneDrive latency is the one risk (mitigated in ADR-0003) | 2 — network-dependent, rate-limited, ambiguous partial writes |
| Autonomy | 4 — agents self-serve on files, no network required | 4 — Cowork self-serves via native Notion tools |
| Speed | 3 — ~30s settle per lease; local I/O otherwise fast | 3 — network latency per operation |
| **Verdict** | **SELECTED** | Rejected — fails at priority #1 (correctness) |
