# AAOS diagrams

Companion to the architecture diagram in the README. Each diagram was checked against the code on `origin/main` 12ae2e4 (2026-10-03) on 2026-10-06. Where a defect is still open it is labelled. The lock/projection split is ADR-0006, which is not implemented.

## 1. Task lifecycle: write ordering and every exit

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent (Code or Cowork)
    participant CT as create-task.py
    participant LT as lease-task.py
    participant C as _aaos_common
    participant L as locks/<id>.lock
    participant T as tasks/<id>.json
    participant S as events .pending/
    participant N as aaos-events.ndjson
    participant CP as complete-task.py
    participant EX as expire-leases.py (weekly)

    A->>CT: --title --accept
    CT->>T: atomic_write (exit 2/3 on usage/conflict)
    CT->>S: TaskCreated (commit_state_change)

    A->>LT: task_id --holder --session
    LT->>T: load (state=queued?)
    LT->>C: acquire_lease (45m TTL, 5m skew)
    C->>T: step 1: read task.lease (PROJECTION used for reclaim - ADR-0006 defect)
    C->>L: step 2-3: stage sidecar, os.link exclusive create
    alt link collides
        C->>L: read incumbent (short retry)
        C-->>LT: DENIED (exit 3) -> emit LeaseDenied
    end
    C->>C: step 4: sleep 30s settle (blocks the caller)
    C->>L: step 5: rescan for conflict copies
    alt conflict found
        C->>L: archive own lock
        C-->>LT: CONTESTED (exit 4) -> emit LeaseContested
    end
    C-->>LT: ACQUIRED
    LT->>T: stage tmp, validate, THEN emit TaskLeased, THEN os.replace
    LT->>S: TaskLeased (spool = commit point)
    S-->>N: drain (best effort, drain lock)
    Note over LT,L: if anything fails after step 3, lease-task archives its own lock (archive_lock_if_unchanged)

    A->>CP: task_id --evidence
    CP->>T: load; assert_lease_held reads task.lease only
    CP->>T: stage done state, validate
    CP->>S: Completed event
    CP->>T: os.replace
    CP->>L: archive_lock_after_commit (after the commit)

    Note over A,T: NO renew path: lease lasts 45 min then is reclaimable
    EX->>L: scan; reclaim_decision on lock expiry
    EX->>T: reset to queued, emit LeaseExpired (+Escalated if DeadAgent)
    EX->>L: archive; an expired lock whose task holds no lease is an ORPHAN LOCK, archived with no event
```

## 2. Event commit and drain pipeline

The spool file in `events/aaos-events.ndjson.pending/` is the durable commit point of an event. The NDJSON is a folded view written by a best-effort drain.

```mermaid
flowchart TB
    EMIT["emit_event()\nbuild event_id, validate_obj(event)"] -->|invalid| ERR5["EventCommitError (exit 5)"]
    EMIT --> SPOOL["spool_event: atomic_write\n<time_ns>-<pid>-<uuid>.json\nDURABLE COMMIT POINT"]
    SPOOL -->|write fails| ERR5
    SPOOL --> DRAIN{"drain_event_spool\n(best effort, never blocks)"}
    DRAIN -->|"no pending"| DONE["return event_id"]
    DRAIN --> LOCK{"_try_acquire_drain_lock\nos.link publish .drainlock"}
    LOCK -->|"held and < 5 min old"| DONE
    LOCK -->|"collision: read acquired"| STALE{"_read_drain_lock_acquired"}
    STALE -->|"FileNotFound: released, not stale (OI-270 fix)"| LOCK
    STALE -->|"unreadable content, or acquired > 5 min ago"| STEAL2["archive stale lock, retry create"]
    LOCK -->|acquired| FOLD["read WHOLE ndjson + all pending\ndedupe by event_id"]
    STEAL2 --> FOLD
    FOLD --> WRITE["atomic_write whole NDJSON (O(N) per emit)\nthen move pending -> _drained/"]
    WRITE --> REL["_release_drain_lock\n(10 x 5ms retry, silent give-up)"]
    REL --> DONE
    FOLD -->|"bad pending file, older than the grace window (600s)"| CORRUPT["move to _corrupt/ + stderr warning"]
    FOLD -->|"bad pending file, younger than the grace window"| DEFER["stay in pending/ + stderr warning,\nretry next drain"]

    subgraph READERS
      RE["read_events(): NDJSON + pending, dedupe,\nsort by timestamp\n(project-notion, fail-task DoubleFailure)"]
    end
    WRITE -.-> RE
    SPOOL -.-> RE
```

## 3. Lease authority (lock file) vs projection (`task.lease`)

This is the dual source of truth that ADR-0006 identifies as the root cause of the open lease-ownership defects (OI-265, OI-266). The target state in the dashed box is not implemented.

```mermaid
flowchart LR
    subgraph AUTH["AUTHORITY (decides who wins)"]
        LOCK["locks/<id>.lock\nwritten at step 3, before 30s settle\nby acquire_lease via os.link"]
    end
    subgraph PROJ["PROJECTION (written >=30s later)"]
        LEASE["task.lease\nwritten by lease-task.py after step 6\n(copies lock.expires)"]
    end

    LT["lease-task.py"] -->|"1. os.link create"| LOCK
    LT -->|"2. after settle: copy holder/expires"| LEASE

    ACQ1["acquire_lease step 1\n(_aaos_common.py:661-687)"] -->|"READS (wrong source)"| LEASE
    ASSERT["assert_lease_held\n(:837-851) double-run backstop"] -->|"READS (wrong source)"| LEASE
    CT["complete / fail / post-handoff"] --> ASSERT
    RECL["reclaim_decision\n(:431-464) used by expire-leases"] -->|"reads (correct source)"| LOCK

    ACQ1 -. "disagreement window on EVERY acquisition" .- LOCK
    ASSERT -. "passes for loser during window" .- LOCK

    TARGET["ADR-0006 target: D1 lock sole authority,\nD2 CAS reclaim, assert_lease_held reads lock"]:::future
    classDef future stroke-dasharray: 4 3
    LOCK --> TARGET
```

## 4. Derived projection chain (heartbeat to Notion)

`signals.json` has no producer, and the chain has no scheduled caller. The diagram shows the designed flow.

```mermaid
flowchart LR
    UP["Upstream health sources\n(4 named in registry heartbeat_source)"] --> FETCH["Agent-session fetch\nUNBUILT / manual"]
    FETCH -->|"signals.json"| DH["derive-heartbeat.py\nfresh <=T, stale <=2T, missed >2T,\nunknown if null (fail-closed)"]
    REG["agent-registry.json (git tree)"] --> DH
    DH -->|"atomic write"| HB["heartbeat.json"]
    HB --> EXP["expire-leases.py\n(escalation class only)"]
    HB --> PN["project-notion.py"]
    TASKS["tasks/*.json"] --> PN
    EV["read_events: ndjson + pending"] --> PN
    PN -->|"stale = newest input mtime > 7d\n(mtime unreliable on OneDrive)"| PROJ["notion-projection.json"]
    PROJ --> PUB["publish-notion.py\nNOTION_TOKEN, 30s timeout,\nno retry/backoff, no idempotency"]
    PUB -->|"POST /v1/pages: NEW page each run"| NOTION["Notion Ops Hub (dated child pages)"]
    PUB -. "failure: exit 1, never a queue failure" .-> NONE["(no alerting on failure)"]
```
