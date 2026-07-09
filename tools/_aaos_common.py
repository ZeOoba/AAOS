#!/usr/bin/env python3
"""
tools/_aaos_common.py — AAOS Phase 3 shared library.

Leading underscore keeps this out of the CLI namespace: every AAOS CLI
script does `import _aaos_common as aaos` (siblings in tools/, same
sys.path pattern as tests/test_validate.py). This module owns:

  1.1  match_write_scope (lifted from validate.py — single home, see below)
  1.2  result/error types (LeaseOutcome, LeaseResult, ReclaimDecision, exceptions)
  1.3  path helpers (pure construction, no I/O)
  1.4  atomic_write / load_json / save_json
  1.5  typed loaders/savers (load_task/save_task/load_handoff/save_handoff/
       load_lock/load_heartbeat/load_registry)
  1.6  validate_obj (reuses validate.py's validator machinery in-process)
  1.7  acquire_lease (ADR-0003's 6-step protocol)
  1.8  commit_state_change + assert_lease_held
  1.9  reclaim_decision + archive_lock
  1.10 emit_event + spool_event + drain_event_spool

See docs/PHASE3-DESIGN.md section 1 for the full spec. Every function
signature, dataclass, exception type, path convention, and threshold
default here is decided there — this module implements it, not improvises
around it.

Import-cycle note: this module never imports `validate` at top level (only
lazily, inside the two functions that need it: load_registry, validate_obj).
validate.py imports `match_write_scope` from this module at top level. That
one-directional dependency (validate.py depends on _aaos_common, never the
reverse, at module-load time) is exactly what design section 1.1 calls for
and keeps both import orders safe.
"""
from __future__ import annotations

import dataclasses
import enum
import fnmatch
import itertools
import json
import os
import posixpath
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SCHEMA_DIR = REPO_ROOT / "schemas"

# 0.1 Runtime state root = the seam, not the git tree.
DEFAULT_SEAM_ROOT = r"C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS"

PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# 1.1 Path-matching reuse — the single home for match_write_scope.
# ---------------------------------------------------------------------------

def match_write_scope(candidate_path: str, write_scope) -> bool:
    """True if candidate_path is covered by at least one glob in write_scope.

    Uses fnmatch.fnmatch (not pathlib.PurePath.match) because write_scope
    globs use "**" to mean "this directory and everything under it,
    recursively" (see agent-registry.json, e.g. "C:/Users/Owner/AAOS/**").
    fnmatch treats "*" (and so "**") as matching any run of characters
    including path separators, which is exactly that recursive semantics;
    PurePath.match treats "**" as an ordinary single-segment "*" and would
    require multiple pattern segments to express the same thing.

    Before matching, candidate_path is canonicalized: normalized to forward
    slashes (this project always authors write_scope globs with forward
    slashes even for Windows paths) and then run through posixpath.normpath
    to lexically resolve "." / ".." segments and collapse redundant
    separators. This is load-bearing, not cosmetic: fnmatch.fnmatch is a
    pure string match with no notion of path semantics, so without
    canonicalizing first, a traversal path like
    "C:/Users/Owner/AAOS/../../Windows/System32/evil.dll" would satisfy the
    glob "C:/Users/Owner/AAOS/**" merely because it *starts with* that
    literal prefix, even though it resolves well outside the scoped
    directory. posixpath (rather than os.path) is used deliberately so this
    resolution is platform-independent — write_scope globs are an internal,
    always-forward-slash convention, not literal OS paths, and normpath's
    ".."-collapsing is purely lexical either way (no filesystem/symlink
    resolution occurs, nor is any needed here).

    Lifted verbatim from tools/validate.py per docs/PHASE3-DESIGN.md
    section 1.1 — this is now the single implementation; validate.py
    imports it from here.
    """
    norm_candidate = posixpath.normpath(candidate_path.replace("\\", "/"))
    for pattern in write_scope:
        if not isinstance(pattern, str):
            continue
        norm_pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(norm_candidate, norm_pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# 1.2 Result & error types
# ---------------------------------------------------------------------------

class LeaseOutcome(enum.Enum):
    ACQUIRED = "acquired"
    DENIED = "denied"          # task not leasable / live incumbent
    CONTESTED = "contested"    # settle-rescan found a foreign/conflict lock -> fail closed


@dataclasses.dataclass(frozen=True)
class LeaseResult:
    outcome: LeaseOutcome
    lock: dict | None = None
    expires: datetime | None = None
    reclaimed_prior: dict | None = None   # a prior EXPIRED lock we archived en route (caller emits LeaseExpired)
    conflict_paths: list[str] = ()        # offending filenames, on CONTESTED
    reason: str | None = None             # human-readable, for DENIED/CONTESTED


@dataclasses.dataclass(frozen=True)
class ReclaimDecision:
    reclaimable: bool
    classification: str        # "DeadAgent" | "ExpiredLapsed" | "" (not reclaimable)
    reason: str


class AaosError(Exception):
    """Base class for all AAOS shared-library errors."""


class UsageError(AaosError):
    """Bad args, missing file, unreadable/corrupt registry -> exit 2."""


class StateConflictError(AaosError):
    """Task not queued, write-once conflict, etc. -> exit 3."""


class LeaseLostError(AaosError):
    """Commit-time lease no longer ours -> exit 4."""


class CorruptStateError(AaosError):
    """Unparseable lock/task/expiry; fail-closed -> exit 1."""


class EventCommitError(AaosError):
    """emit_event could neither append nor spool -> exit 5."""


# ---------------------------------------------------------------------------
# 1.3 Paths — pure construction, no I/O, no directory creation.
# ---------------------------------------------------------------------------

def _validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise UsageError(
            f"invalid project_id: {project_id!r} "
            f"(must match {PROJECT_ID_RE.pattern!r}) — refusing to construct a "
            "path that could escape the seam"
        )
    return project_id


def seam_root(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("AAOS_SEAM_ROOT")
    if env:
        return Path(env)
    return Path(DEFAULT_SEAM_ROOT)


def project_dir(project_id: str, *, root: str | None = None) -> Path:
    _validate_project_id(project_id)
    return seam_root(root) / "projects" / project_id


def task_path(task_id, project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "tasks" / f"{task_id}.json"


def handoff_path(handoff_id, project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "handoffs" / f"{handoff_id}.json"


def lock_path(task_id, project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "locks" / f"{task_id}.lock"


def lock_archive_dir(project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "locks" / "_archive"


def events_log_path(project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "events" / "aaos-events.ndjson"


def event_spool_dir(project_id, *, root: str | None = None) -> Path:
    log_path = events_log_path(project_id, root=root)
    return log_path.parent / (log_path.name + ".pending")


def heartbeat_path(*, root: str | None = None) -> Path:
    return seam_root(root) / "heartbeat.json"


def registry_path() -> Path:
    return REPO_ROOT / "agent-registry.json"


# ---------------------------------------------------------------------------
# 1.4 Atomic I/O
# ---------------------------------------------------------------------------

def atomic_write(path: Path | str, content: str | bytes) -> None:
    """The ADR-0002 idiom, verbatim: create parent dirs; unique temp name
    path + '.tmp-' + str(os.getpid()); write bytes (UTF-8 if str); flush() +
    os.fsync(); os.replace(tmp, path); on any exception, best-effort unlink
    the temp and re-raise. Never writes `path` in place."""
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    tmp = Path(str(path) + ".tmp-" + str(os.getpid()))
    try:
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def load_json(path: Path | str) -> dict:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"could not read {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptStateError(f"unparseable JSON at {path}: {exc}") from exc


def save_json(path: Path | str, obj: dict) -> None:
    content = json.dumps(obj, indent=2, ensure_ascii=False)
    atomic_write(path, content)


# ---------------------------------------------------------------------------
# 1.5 Typed state loaders/savers
# ---------------------------------------------------------------------------

def load_task(task_id, project_id, *, root: str | None = None) -> dict:
    return load_json(task_path(task_id, project_id, root=root))


def save_task(task: dict, *, root: str | None = None) -> None:
    save_json(task_path(task["id"], task["project_id"], root=root), task)


def load_handoff(handoff_id, project_id, *, root: str | None = None) -> dict | None:
    path = handoff_path(handoff_id, project_id, root=root)
    if not path.exists():
        return None
    return load_json(path)


def save_handoff(handoff: dict, *, root: str | None = None) -> None:
    """WRITE-ONCE: an existing-and-different handoff of the same id raises
    StateConflictError; an existing-and-identical one is a silent no-op."""
    handoff_id = handoff["id"]
    project_id = handoff["project_id"]
    existing = load_handoff(handoff_id, project_id, root=root)
    if existing is None:
        save_json(handoff_path(handoff_id, project_id, root=root), handoff)
        return
    if existing == handoff:
        return
    raise StateConflictError(
        f"handoff {handoff_id} already exists with different content (write-once)"
    )


def load_lock(task_id, project_id, *, root: str | None = None) -> dict | None:
    path = lock_path(task_id, project_id, root=root)
    if not path.exists():
        return None
    return load_json(path)  # CorruptStateError propagates if unparseable


def load_heartbeat(*, root: str | None = None) -> dict | None:
    path = heartbeat_path(root=root)
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (UsageError, CorruptStateError):
        return None


def load_registry() -> dict:
    import validate as _validate  # lazy: see module docstring's cycle note
    return _validate.load_registry(registry_path())


# ---------------------------------------------------------------------------
# 1.6 Validation reuse
# ---------------------------------------------------------------------------

def validate_obj(obj: dict, schema_name: str, *, registry: dict | None = None) -> list[str]:
    import validate as _validate  # lazy: see module docstring's cycle note

    schema_path = SCHEMA_DIR / f"{schema_name}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    if schema_name == "handoff" and registry is None:
        registry = load_registry()

    return _validate.validate_instance(obj, validator, schema_name, registry, registry_path())


# ---------------------------------------------------------------------------
# 0.3 Injectable time
# ---------------------------------------------------------------------------

def _now(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now().astimezone()


def _parse_iso_dt(value) -> datetime:
    """Parse an ISO-8601 datetime string. Fail-closed CorruptStateError on
    missing/non-string/unparseable/naive values — never silently assumed
    UTC (design section 0.3)."""
    if not isinstance(value, str) or not value:
        raise CorruptStateError(f"missing or non-string datetime value: {value!r}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptStateError(f"unparseable datetime value {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise CorruptStateError(f"naive datetime not allowed (ambiguous timezone): {value!r}")
    return dt


# ---------------------------------------------------------------------------
# 1.9 Stale-lock reclamation (defined ahead of acquire_lease, which uses it)
# ---------------------------------------------------------------------------

def reclaim_decision(
    lock: dict, heartbeat_status: str | None, *, now: datetime | None = None, skew_minutes: int = 5
) -> ReclaimDecision:
    resolved_now = _now(now)
    expires_raw = lock.get("expires") if isinstance(lock, dict) else None
    try:
        expires = _parse_iso_dt(expires_raw)
    except CorruptStateError:
        return ReclaimDecision(
            reclaimable=False,
            classification="",
            reason="lock has a missing or unparseable 'expires'; fail-closed, not reclaimable",
        )

    grace_deadline = expires + timedelta(minutes=skew_minutes)
    if resolved_now <= grace_deadline:
        return ReclaimDecision(
            reclaimable=False,
            classification="",
            reason=f"lease live: now <= expires+{skew_minutes}m grace ({grace_deadline.isoformat()})",
        )

    if heartbeat_status in ("missed", "unknown", None):
        classification = "DeadAgent"
    else:
        classification = "ExpiredLapsed"
    return ReclaimDecision(
        reclaimable=True,
        classification=classification,
        reason=(
            f"lease expired past grace (now > expires+{skew_minutes}m); "
            f"heartbeat_status={heartbeat_status!r}"
        ),
    )


def archive_lock(lock_file: Path | str, *, now: datetime | None = None) -> Path:
    """os.replace to locks/_archive/<task_id>.<compact-ts>.lock; never deletes."""
    lock_file = Path(lock_file)
    resolved_now = _now(now)
    base = lock_file.name.split(".lock", 1)[0]
    compact_ts = resolved_now.strftime("%Y%m%dT%H%M%S%f")
    archive_dir = lock_file.parent / "_archive"
    os.makedirs(archive_dir, exist_ok=True)
    target = archive_dir / f"{base}.{compact_ts}.lock"
    if target.exists():
        target = archive_dir / f"{base}.{compact_ts}.{os.getpid()}.lock"
    os.replace(str(lock_file), str(target))
    return target


# ---------------------------------------------------------------------------
# 1.7 Lease acquisition — ADR-0003's 6-step protocol
# ---------------------------------------------------------------------------

_CONFLICT_SUFFIX_RE = re.compile(r"^(-[A-Za-z0-9_.-]+)?( \(\d+\))?\.lock$")


def _matches_conflict_shape(filename: str, task_id: str) -> bool:
    """True for the canonical <task_id>.lock as well as OneDrive
    conflict-copy shapes: <id>-<HOST>.lock, <id> (N).lock, <id>-<HOST> (N).lock."""
    if not filename.startswith(task_id):
        return False
    suffix = filename[len(task_id):]
    return bool(_CONFLICT_SUFFIX_RE.match(suffix))


def _scan_for_conflicts(locks_dir: Path, task_id: str, holder: str, session: str) -> list[str]:
    conflicts: list[str] = []
    if not locks_dir.is_dir():
        return conflicts
    canonical_name = f"{task_id}.lock"
    for entry in sorted(locks_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        name = entry.name
        if not _matches_conflict_shape(name, task_id):
            continue
        if name == canonical_name:
            try:
                data = load_json(entry)
            except AaosError:
                conflicts.append(str(entry))
                continue
            if data.get("holder") != holder or data.get("session") != session:
                conflicts.append(str(entry))
            continue
        # Any other matching filename is itself a conflict-copy shape.
        conflicts.append(str(entry))
    return conflicts


def _exclusive_create(path: Path, data: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def acquire_lease(
    task_id: str, project_id: str, holder: str, session: str, *,
    now: datetime | None = None,
    ttl_minutes: int = 45, skew_minutes: int = 5, settle_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    reclaim_expired: bool = True,
    root: str | None = None,
) -> LeaseResult:
    resolved_now = _now(now)

    # ---- 1. Precheck ----
    task = load_task(task_id, project_id, root=root)
    state = task.get("state")
    lease = task.get("lease")
    reclaimed_prior = None

    if state == "queued":
        pass  # proceed
    else:
        expired = False
        if lease is not None:
            try:
                lease_expires = _parse_iso_dt(lease.get("expires"))
                expired = resolved_now > lease_expires + timedelta(minutes=skew_minutes)
            except CorruptStateError:
                expired = False  # fail-closed: an unparseable lease is treated as live, not reclaimable
        if lease is not None and expired:
            if reclaim_expired:
                incumbent_file = lock_path(task_id, project_id, root=root)
                if incumbent_file.exists():
                    archive_lock(incumbent_file, now=resolved_now)
                reclaimed_prior = lease
                # proceed
            else:
                return LeaseResult(
                    outcome=LeaseOutcome.DENIED,
                    reason="expired lease present; run expire-leases",
                )
        else:
            return LeaseResult(
                outcome=LeaseOutcome.DENIED,
                reason=f"task state={state}, lease live",
            )

    # ---- 2. Stage ----
    final_lock_path = lock_path(task_id, project_id, root=root)
    acquired_iso = resolved_now.isoformat()
    expires_dt = resolved_now + timedelta(minutes=ttl_minutes)
    lock_dict = {
        "protocol_version": "1.0",
        "task_ref": task_id,
        "holder": holder,
        "acquired": acquired_iso,
        "expires": expires_dt.isoformat(),
        "session": session,
    }
    content = json.dumps(lock_dict, indent=2, ensure_ascii=False)
    content_bytes = content.encode("utf-8")

    pid = os.getpid()
    sidecar = final_lock_path.parent / f"{final_lock_path.name}.tmp-{holder}-{pid}"
    atomic_write(sidecar, content)

    def _cleanup_sidecar():
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass

    # ---- 3. Exclusive create ----
    os.makedirs(final_lock_path.parent, exist_ok=True)
    try:
        _exclusive_create(final_lock_path, content_bytes)
        _cleanup_sidecar()
    except FileExistsError:
        try:
            incumbent = load_lock(task_id, project_id, root=root)
        except CorruptStateError:
            _cleanup_sidecar()
            raise

        incumbent_live = True
        if incumbent is not None:
            try:
                incumbent_expires = _parse_iso_dt(incumbent.get("expires"))
                incumbent_live = resolved_now <= incumbent_expires + timedelta(minutes=skew_minutes)
            except CorruptStateError:
                incumbent_live = True  # fail-closed: don't steal an ambiguous incumbent

        if not incumbent_live:
            archive_lock(final_lock_path, now=resolved_now)
            try:
                _exclusive_create(final_lock_path, content_bytes)
                _cleanup_sidecar()
            except FileExistsError:
                archive_lock(sidecar, now=resolved_now)
                return LeaseResult(
                    outcome=LeaseOutcome.CONTESTED,
                    conflict_paths=[str(final_lock_path)],
                    reason="collision persisted after reclaim-and-retry",
                )
        else:
            _cleanup_sidecar()
            return LeaseResult(outcome=LeaseOutcome.DENIED, reason="incumbent lease live")

    # ---- 4. Settle ----
    sleep(settle_seconds)

    # ---- 5. Rescan ----
    conflict_paths = _scan_for_conflicts(final_lock_path.parent, task_id, holder, session)
    if conflict_paths:
        if final_lock_path.exists():
            archive_lock(final_lock_path, now=resolved_now)
        return LeaseResult(
            outcome=LeaseOutcome.CONTESTED,
            conflict_paths=conflict_paths,
            reason="conflict lock(s) found during settle rescan",
        )

    # ---- 6. Confirm ----
    return LeaseResult(
        outcome=LeaseOutcome.ACQUIRED,
        lock=lock_dict,
        expires=expires_dt,
        reclaimed_prior=reclaimed_prior,
    )


# ---------------------------------------------------------------------------
# 1.8 Commit transaction & the double-run backstop
# ---------------------------------------------------------------------------

def commit_state_change(new_state_obj: dict, state_path: Path | str, event: dict, *, root: str | None = None) -> str:
    state_path = Path(state_path)
    tmp = state_path.parent / f"{state_path.name}.tmp-{os.getpid()}"
    content = json.dumps(new_state_obj, indent=2, ensure_ascii=False)
    atomic_write(tmp, content)  # stages tmp — not yet in place at state_path

    try:
        event_id = emit_event(**event, root=root)
    except EventCommitError:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise

    os.replace(str(tmp), str(state_path))
    return event_id


def assert_lease_held(task: dict, holder: str, session: str, *, now: datetime | None = None, skew_minutes: int = 5) -> None:
    resolved_now = _now(now)
    lease = task.get("lease") if isinstance(task, dict) else None
    task_id = task.get("id") if isinstance(task, dict) else "?"
    if not lease:
        raise LeaseLostError(f"no lease present on task {task_id}")
    if lease.get("holder") != holder or lease.get("session") != session:
        raise LeaseLostError(
            f"task {task_id}: lease held by holder={lease.get('holder')!r} "
            f"session={lease.get('session')!r}, not {holder!r}/{session!r}"
        )
    expires = _parse_iso_dt(lease.get("expires"))  # CorruptStateError propagates if unparseable
    if resolved_now > expires + timedelta(minutes=skew_minutes):
        raise LeaseLostError(
            f"task {task_id}: lease expired at {expires.isoformat()} (+{skew_minutes}m grace)"
        )


# ---------------------------------------------------------------------------
# 1.10 Event emission — ADR-0004 discipline
# ---------------------------------------------------------------------------

_EVENT_SEQ = itertools.count(1)


def _atomic_append_ndjson(log_path: Path, line: str) -> None:
    """mtime-guarded read-all -> append line -> os.replace. Raises on
    mtime-changed-mid-read or any I/O error; caller decides retry/spool."""
    log_path = Path(log_path)
    os.makedirs(log_path.parent, exist_ok=True)
    if log_path.exists():
        st_before = os.stat(log_path).st_mtime_ns
        with open(log_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        if os.stat(log_path).st_mtime_ns != st_before:
            raise RuntimeError(f"event log changed mid-read (mtime guard tripped): {log_path}")
    else:
        content = ""
    if content and not content.endswith("\n"):
        content += "\n"
    new_content = content + line + "\n"

    tmp = log_path.parent / f"{log_path.name}.tmp-{os.getpid()}"
    atomic_write(tmp, new_content)
    os.replace(str(tmp), str(log_path))


def spool_event(event: dict, project_id, *, root: str | None = None) -> Path:
    spool_dir = event_spool_dir(project_id, root=root)
    os.makedirs(spool_dir, exist_ok=True)
    target = spool_dir / f"{time.time_ns()}-{os.getpid()}.json"
    atomic_write(target, json.dumps(event, ensure_ascii=False))
    return target


def drain_event_spool(project_id, *, root: str | None = None, attempts: int = 3) -> int:
    """Replay spooled events oldest-first into the NDJSON. Stops at the
    first one that still can't be appended (leaves it + the rest for next
    time). A malformed spooled file is discarded so it can't wedge the
    queue forever. Returns the count successfully drained."""
    spool_dir = event_spool_dir(project_id, root=root)
    if not spool_dir.is_dir():
        return 0

    log_path = events_log_path(project_id, root=root)
    names = sorted(p.name for p in spool_dir.iterdir() if p.is_file() and p.name.endswith(".json"))

    drained = 0
    for name in names:
        p = spool_dir / name
        try:
            event = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            try:
                p.unlink()
            except OSError:
                pass
            continue

        line = json.dumps(event, ensure_ascii=False)
        done = False
        for _attempt in range(attempts):
            try:
                _atomic_append_ndjson(log_path, line)
                done = True
                break
            except Exception:
                continue
        if not done:
            break  # still locked -- retry this + the rest on a later run

        try:
            p.unlink()
        except OSError:
            pass
        drained += 1

    return drained


def emit_event(
    type: str, actor: str, project_id: str,
    refs: dict | None = None, detail: str | None = None,
    event_log_ref: str | None = None, *,
    now: datetime | None = None, root: str | None = None,
) -> str:
    resolved_now = _now(now)

    # 1. Drain first.
    drain_event_spool(project_id, root=root)

    # 2. Build & validate.
    event = {
        "protocol_version": "1.0",
        "event_id": f"e-{resolved_now.isoformat()}-{next(_EVENT_SEQ)}",
        "type": type,
        "actor": actor,
        "timestamp": resolved_now.isoformat(),
        "project_id": project_id,
        "refs": refs or {},
    }
    if detail is not None:
        event["detail"] = detail
    if event_log_ref is not None:
        event["event_log_ref"] = event_log_ref

    errors = validate_obj(event, "event")
    if errors:
        raise EventCommitError(f"event failed schema validation, not appended: {errors}")

    log_path = events_log_path(project_id, root=root)
    line = json.dumps(event, ensure_ascii=False)

    # 3. Append with <=1 retry, then spool.
    appended = False
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            _atomic_append_ndjson(log_path, line)
            appended = True
            break
        except Exception as exc:
            last_exc = exc

    if not appended:
        try:
            spool_event(event, project_id, root=root)
        except Exception as spool_exc:
            # 4. EventCommitError only if both append and spool fail.
            raise EventCommitError(
                f"failed to append ({last_exc}) and failed to spool ({spool_exc})"
            ) from spool_exc

    return event["event_id"]
