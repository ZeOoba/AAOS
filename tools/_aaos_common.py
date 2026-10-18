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

import argparse
import dataclasses
import enum
import fnmatch
import itertools
import json
import os
import posixpath
import re
import sys
import time
import uuid
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
# Mirror schemas/task.schema.json and handoff.schema.json "id" patterns. Used
# by the path builders below (fullmatch, so a trailing newline can't slip
# past "$") -- a raw CLI argument such as "../x" must never become a path.
TASK_ID_RE = re.compile(r"t-[a-z0-9-]+")
HANDOFF_ID_RE = re.compile(r"h-[a-z0-9-]+")


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
    """emit_event's durable commit failed -> exit 5.

    REVISED (post-review, see PHASE3-DESIGN.md section 1.10): the pending
    (spool) file write is now the commit point, so this is raised ONLY
    when that write itself fails (or the event fails schema validation
    before ever reaching it). A failure to *drain* pending events into
    the NDJSON afterward is non-fatal -- the event is already durably
    committed in pending and will be folded in by a later drain."""


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


def _validate_id(value, pattern: re.Pattern, kind: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise UsageError(
            f"invalid {kind}: {value!r} (must match {pattern.pattern!r}) -- "
            "refusing to construct a path that could escape the seam"
        )
    return value


def _validate_task_id(task_id) -> str:
    return _validate_id(task_id, TASK_ID_RE, "task id")


def _validate_handoff_id(handoff_id) -> str:
    return _validate_id(handoff_id, HANDOFF_ID_RE, "handoff id")


def seam_root(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("AAOS_SEAM_ROOT")
    if env:
        return Path(env)
    return Path(DEFAULT_SEAM_ROOT)


def require_seam_reachable(root: str | None = None) -> Path:
    """Fail closed with UsageError if the seam root itself does not exist
    as a directory. Every AAOS CLI script that touches seam-rooted state
    must call this FIRST, before any other seam read/write.

    Added in Phase 5 (failure-injection matrix, scenario #4 "seam
    unavailable"). Without this check, a script that has no other reason
    to read anything at the seam before it first writes (create-task.py;
    post-handoff.py with no --blocker; emit-event.py) would silently
    auto-vivify a brand-new, empty parallel seam tree and SUCCEED when
    pointed at a renamed/unmounted OneDrive sync folder, instead of
    failing closed the way every other AAOS operation does when its
    precondition isn't met -- atomic_write's os.makedirs(path.parent,
    exist_ok=True) has no notion that a missing seam ROOT is different
    from a missing subdirectory that's fine to auto-create on first use.
    Empirically confirmed via the Phase 5 rehearsal: see
    docs/_task-reports/phase5-failure-matrix-report.md, scenario #4.
    """
    resolved = seam_root(root)
    if not resolved.is_dir():
        raise UsageError(
            f"seam root unreachable: '{resolved}' does not exist or is not "
            "a directory. This usually means the OneDrive sync folder is "
            "unmounted or renamed, or --seam-root/AAOS_SEAM_ROOT is wrong "
            "-- fix the seam, don't work around this check."
        )
    return resolved


def project_dir(project_id: str, *, root: str | None = None) -> Path:
    _validate_project_id(project_id)
    return seam_root(root) / "projects" / project_id


def task_path(task_id, project_id, *, root: str | None = None) -> Path:
    _validate_task_id(task_id)
    return project_dir(project_id, root=root) / "tasks" / f"{task_id}.json"


def handoff_path(handoff_id, project_id, *, root: str | None = None) -> Path:
    _validate_handoff_id(handoff_id)
    return project_dir(project_id, root=root) / "handoffs" / f"{handoff_id}.json"


def lock_path(task_id, project_id, *, root: str | None = None) -> Path:
    _validate_task_id(task_id)
    return project_dir(project_id, root=root) / "locks" / f"{task_id}.lock"


def lock_archive_dir(project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "locks" / "_archive"


def events_log_path(project_id, *, root: str | None = None) -> Path:
    return project_dir(project_id, root=root) / "events" / "aaos-events.ndjson"


def event_spool_dir(project_id, *, root: str | None = None) -> Path:
    log_path = events_log_path(project_id, root=root)
    return log_path.parent / (log_path.name + ".pending")


def _drain_lock_path(project_id, *, root: str | None = None) -> Path:
    log_path = events_log_path(project_id, root=root)
    return log_path.parent / (log_path.name + ".drainlock")


def _events_archive_dir(project_id, *, root: str | None = None) -> Path:
    return events_log_path(project_id, root=root).parent / "_archive"


def _pending_drained_dir(project_id, *, root: str | None = None) -> Path:
    return event_spool_dir(project_id, root=root) / "_drained"


def _pending_corrupt_dir(project_id, *, root: str | None = None) -> Path:
    return event_spool_dir(project_id, root=root) / "_corrupt"


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
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptStateError(f"unparseable JSON at {path}: {exc}") from exc
    # Every AAOS state file (task, handoff, lock, heartbeat) is a JSON object;
    # callers immediately .get() on it. A list/scalar is corrupt state, not a
    # reason for an AttributeError traceback.
    if not isinstance(obj, dict):
        raise CorruptStateError(f"JSON at {path} is not an object (got {type(obj).__name__})")
    return obj


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
    """None if heartbeat.json is absent (normal/expected -- no warning) or
    unreadable/corrupt (Finding 6, adversarial review: this used to return
    None identically for both cases with no signal at all for the corrupt
    one. Callers already treat None as "unknown" correctly and fail safe on
    it -- that return contract is unchanged -- but a corrupt liveness
    projection is a real degradation worth a loud, visible warning rather
    than silently looking identical to "heartbeat was never derived yet.")
    """
    path = heartbeat_path(root=root)
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (UsageError, CorruptStateError) as exc:
        print(
            f"WARNING: heartbeat.json exists but is unreadable/corrupt "
            f"({path}): {exc}; treating all agents as heartbeat-unknown",
            file=sys.stderr,
        )
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
    # Python 3.10 (the interpreter inside Cowork's sandbox) rejects a trailing 'Z'; 3.11+ accepts it.
    # Cowork stamps UTC as '...Z', so rewrite it to '+00:00' so the common case parses the same on 3.10.
    # Only an uppercase 'Z' that directly follows a digit is rewritten, so what 3.11+ rejects (a
    # lowercase 'z', a doubled 'ZZ', a bare 'Z') stays rejected on 3.10 as well. This does NOT make 3.10
    # agree with 3.11+ in general: 3.10 still rejects fractions that are not 3 or 6 digits, offsets
    # without a colon (+0000, +00), the comma decimal, a space before Z, and basic/week formats (see
    # docs/MIGRATIONS.md, 'timestamps').
    text = value[:-1] + "+00:00" if value.endswith("Z") and value[-2:-1].isdigit() else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CorruptStateError(f"unparseable datetime value {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise CorruptStateError(f"naive datetime not allowed (ambiguous timezone): {value!r}")
    return dt


MIN_CLI_NOW_YEAR = 1970
MAX_CLI_NOW_YEAR = 9000


def parse_cli_now(now_str, flag: str = "--now") -> datetime:
    """Resolve a CLI `--now` value. Empty/None means the real clock (timezone-aware).

    Otherwise the value must be an ISO-8601 datetime WITH a UTC offset: a naive value is rejected
    with UsageError (exit 2) instead of being silently compared against timezone-aware datetimes
    (design section 0.3 -- never assume UTC). The seven queue CLIs share this single parse path;
    `_parse_iso_dt` is the only place `datetime.fromisoformat` is called."""
    if not now_str:
        return datetime.now().astimezone()
    try:
        value = _parse_iso_dt(now_str)
    except CorruptStateError as exc:
        raise UsageError(f"{flag}: {exc}") from exc
    # Sanity range: an extreme year makes the later `now +/- timedelta(...)` arithmetic overflow
    # (exit 1 'UNEXPECTED ERROR') instead of failing as a usage error.
    if not (MIN_CLI_NOW_YEAR <= value.year <= MAX_CLI_NOW_YEAR):
        raise UsageError(f"{flag}: year {value.year} is outside the supported range "
                         f"{MIN_CLI_NOW_YEAR}-{MAX_CLI_NOW_YEAR}")
    return value


# argparse `type=` validators for CLI numeric flags (audit M03). They reject INVALID values only; the
# ratified defaults (30s settle, 5-minute skew, 45-minute TTL) are defaults, never floors.
#
# The ceilings below are deliberately loose SANITY bounds, not policy: they keep absurd values from
# overflowing timedelta/sleep (exit 1 with a traceback-style error) or from holding a lock for years
# (a huge --settle-sec sleeps after the lock is created). The real lease-TTL ceiling is ADR-0006 D3
# (LEASE_TTL_CAP, not yet implemented) and will be tighter; it replaces MAX_CLI_MINUTES for --ttl-min.
MAX_CLI_MINUTES = 525_600   # one year, in minutes
MAX_CLI_SETTLE_SEC = 3600.0  # one hour


def _bounded_int(lo: int, hi: int, why_low: str):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer in [{lo}, {hi}], got {text!r}") from None
        if value < lo:
            raise argparse.ArgumentTypeError(f"must be >= {lo} ({why_low}), got {value}")
        if value > hi:
            raise argparse.ArgumentTypeError(f"must be <= {hi}, got {value}")
        return value
    return parse


# int >= 1 (a zero or negative TTL would mint an already-expired lease)
positive_int = _bounded_int(1, MAX_CLI_MINUTES, "a zero or negative TTL mints an already-expired lease")
# int >= 0 (a negative skew makes a LIVE foreign lease look expired and reclaimable)
nonneg_int = _bounded_int(0, MAX_CLI_MINUTES, "a negative skew makes a live lease look expired")


def finite_nonneg_float(text: str) -> float:
    """finite float in [0, MAX_CLI_SETTLE_SEC] (rejects negative, NaN, +/-Inf and absurdly long settles)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number in [0, {MAX_CLI_SETTLE_SEC:g}], got {text!r}") from None
    if value != value or value in (float("inf"), float("-inf")) or value < 0 or value > MAX_CLI_SETTLE_SEC:
        raise argparse.ArgumentTypeError(f"must be a finite number in [0, {MAX_CLI_SETTLE_SEC:g}], got {text!r}")
    return value


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


def archive_lock_if_unchanged(lock_file: Path | str, expected: dict, *, now: datetime | None = None) -> Path | None:
    """Archive `lock_file` only if its content still equals `expected`.

    For callers that read a lock, decided something about it, and now want to
    move it aside: the path may meanwhile hold a DIFFERENT lock (e.g. a
    lease-task that reclaimed the expired one and published its own), and a
    path-based archive_lock would then archive that live lock. Returns the
    archive path, or None if the lock is gone, no longer matches, or could
    not be moved (warned on stderr; never raises OSError, so it is safe in
    cleanup paths that must re-raise the original failure).

    Not a compare-and-swap -- a residual window remains between the re-read
    and the os.replace. It is mitigated, not closed: if the archived file's
    content is not `expected`, it is put back exclusively (os.link; on
    Windows, os.rename as the no-hardlink fallback -- neither ever clobbers
    a newer lock) and None is returned. If even that fails (a third lock
    now occupies the path, or no non-clobbering move exists), an Escalated
    event (detail "LockRestoreFailed") is emitted best-effort so a human
    can restore the archived lock by hand."""
    lock_file = Path(lock_file)
    try:
        current = load_json(lock_file)
    except (UsageError, CorruptStateError):
        return None  # gone or unreadable: nothing of ours to archive
    if current != expected:
        return None
    try:
        target = archive_lock(lock_file, now=now)
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"WARNING: could not archive lock {lock_file}: {exc}", file=sys.stderr)
        return None
    try:
        archived = load_json(target)
    except AaosError:
        archived = None
    if archived != expected:
        # Raced: we moved someone else's lock. Put it back without clobbering.
        _restore_archived_lock(target, lock_file, now=now)
        return None
    return target


def _restore_archived_lock(archived: Path, lock_file: Path, *, now: datetime | None = None) -> bool:
    """Move `archived` back to `lock_file` without ever replacing a lock that
    is there now. Returns True on success; on failure warns and emits a
    best-effort Escalated event. Never raises."""
    try:
        try:
            os.link(str(archived), str(lock_file))
        except FileExistsError:
            raise
        except OSError:
            if os.name != "nt":
                raise  # POSIX rename clobbers: no safe fallback
            os.rename(str(archived), str(lock_file))  # Windows rename refuses an existing dest
        else:
            try:
                os.unlink(str(archived))  # second name of the restored file; no data lost
            except OSError:
                pass  # harmless: a duplicate name left in _archive
        return True
    except OSError as exc:
        print(
            f"WARNING: archived a lock that changed under us and could not restore it "
            f"({archived} -> {lock_file}): {exc}",
            file=sys.stderr,
        )
        try:
            project_dir_ = lock_file.parent.parent
            emit_event(
                type="Escalated", actor="system", project_id=project_dir_.name,
                refs={"lock": str(lock_file)},
                detail=f"LockRestoreFailed: live lock left at {archived.name}"[:200],
                now=now, root=str(project_dir_.parent.parent),
            )
        except Exception as emit_exc:  # best-effort: never mask the caller's own path
            print(f"WARNING: could not emit LockRestoreFailed escalation: {emit_exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 1.7 Lease acquisition — ADR-0003's 6-step protocol
# ---------------------------------------------------------------------------

_CONFLICT_SUFFIX_RE = re.compile(r"^(-[A-Za-z0-9_.-]+)?( \(\d+\))?\.lock$")


def _matches_conflict_shape(filename: str, task_id: str) -> bool:
    """True for the canonical <task_id>.lock as well as OneDrive
    conflict-copy shapes: <id>-<HOST>.lock, <id> (N).lock, <id>-<HOST> (N).lock.

    This is a FILENAME-shape test only -- a positive match here is a
    candidate, not a confirmed conflict. A filename can match this shape
    purely because it starts with task_id followed by a hyphen (e.g.
    task_id="t-a", filename="t-a-b.lock") while actually being the
    canonical lock of an unrelated sibling task ("t-a-b"), not a
    conflict-copy of "t-a" at all. See _scan_for_conflicts, which resolves
    that ambiguity by reading the candidate's own task_ref (Finding 2,
    adversarial review)."""
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

        # Any OTHER filename matching the conflict-copy SHAPE is only a
        # genuine conflict if its own content confirms it: this is
        # actually a copy of THIS task's lock (task_ref == task_id) held
        # by someone else. Filename shape alone can't distinguish a real
        # OneDrive conflict-copy ("t-a-DESKTOP01.lock", content
        # task_ref="t-a") from an unrelated sibling task's own canonical
        # lock ("t-a-b.lock", content task_ref="t-a-b") -- read the
        # content to tell them apart (Finding 2, adversarial review: the
        # old code treated every shape-match here as an automatic
        # conflict, falsely CONTESTing "t-a" whenever sibling task "t-a-b"
        # merely held its own live lock).
        try:
            data = load_json(entry)
        except AaosError:
            conflicts.append(str(entry))  # unparseable -- fail closed, still a conflict
            continue

        if data.get("task_ref") != task_id:
            continue  # a different task's own lock -- not a conflict for us

        if data.get("holder") != holder or data.get("session") != session:
            conflicts.append(str(entry))
        # else: content confirms this conflict-copy-shaped file actually
        # IS our own lock (same task_ref, same holder/session) -- nothing
        # to fail closed on.
    return conflicts


def _exclusive_create(path: Path, data: bytes) -> None:
    """Create `path` exclusively. NOT atomic: the file exists, empty, between the
    O_EXCL create and the write. Prefer _exclusive_link() for lock files, which has no
    such window. Retained for callers with no staged sidecar to link from."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _exclusive_link(source: Path, dest: Path, data: bytes) -> None:
    """Atomically publish `source` (already fully written) at `dest`, exclusively.

    AAOS-SPEC.md section 7 step 3 calls this "an atomic exclusive create". Until 2026-09-10
    it was not: _exclusive_create() creates with O_EXCL and writes the JSON as a SEPARATE
    syscall, so the lock file was observable as zero bytes between the two. Every reader of
    that directory could see it -- the FileExistsError branch below, _scan_for_conflicts(),
    and expire-leases.py -- and each turned a routine contended lease into a corruption
    signal, a false CONTESTED, or a human escalation respectively (OI-264).

    os.link() is atomic and fails with FileExistsError on collision, so it preserves the
    exclusivity guarantee exactly while making the file appear complete or not at all. It
    also survives a crash mid-create: die before the link and there is no lock; die after
    and the lock is whole. A retry loop cannot fix that case at all.

    Falls back to _exclusive_create() only if the filesystem refuses hardlinks (OSError
    that is NOT FileExistsError -- a collision must still propagate). Verified working on
    this deployment's NTFS; the fallback exists because the seam is OneDrive-synced and
    hardlink support is a filesystem property we should not assume for all time.
    """
    try:
        os.link(str(source), str(dest))
    except FileExistsError:
        raise
    except OSError:
        _exclusive_create(dest, data)


#: Bounded re-read of an unparseable incumbent lock. This is a SAFETY NET, not the fix:
#: _exclusive_link() removes AAOS's own create-then-write window entirely, so no AAOS
#: writer can leave a zero-byte lock any more. What remains is a foreign or partially
#: synced file -- the seam is OneDrive-backed. Deliberately NOT sized against
#: AAOS-SPEC.md section 7's 30s propagation allowance: this runs on the DENIED path where a
#: caller is waiting, and a lock that is still unparseable after ~100ms is far more likely
#: to be genuinely damaged (e.g. a process killed between create and write by an older
#: build) than mid-flight. A slow foreign partial write still raises, by design.
_LOCK_SETTLE_ATTEMPTS = 5
_LOCK_SETTLE_DELAY_SEC = 0.025


def _load_incumbent_lock(task_id: str, project_id: str, *, root: str | None = None) -> dict | None:
    """load_lock(), tolerating a briefly-unparseable lock (OI-264).

    An incumbent lock that is unparseable for an instant means "a writer holds it and is
    mid-publish" -- contention, whose protocol answer is DENIED. Before this, the first
    unparseable read propagated CorruptStateError straight out of acquire_lease, so a
    contended lease surfaced as damaged state: outside the ACQUIRED/DENIED/CONTESTED
    contract in AAOS-SPEC.md section 7, and inviting the operator response for corruption
    (inspect/repair) instead of the correct one (back off and retry).

    A lock still unparseable after the window is genuinely damaged and still raises -- this
    widens no fail-open path, it only distinguishes transient from durable.

    Uses time.sleep directly rather than acquire_lease's injectable `sleep`: that parameter
    exists so tests can stub out the 30s protocol settle, and stubbing it must not silently
    disable this physical wait too.
    """
    for attempt in range(_LOCK_SETTLE_ATTEMPTS):
        try:
            return load_lock(task_id, project_id, root=root)
        except CorruptStateError:
            # Re-raise from inside the except on the final attempt, so correctness does
            # not depend on _LOCK_SETTLE_ATTEMPTS being >= 1. Binding the exception and
            # raising it after the loop would become `raise None` -> TypeError, escaping
            # the AaosError hierarchy every caller's handlers are built on, if anyone ever
            # tuned that constant to 0.
            if attempt == _LOCK_SETTLE_ATTEMPTS - 1:
                raise
            time.sleep(_LOCK_SETTLE_DELAY_SEC)
    raise CorruptStateError(  # unreachable while _LOCK_SETTLE_ATTEMPTS >= 1
        f"lock for {task_id} unreadable and retry loop did not execute "
        f"(_LOCK_SETTLE_ATTEMPTS={_LOCK_SETTLE_ATTEMPTS})"
    )


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
        _exclusive_link(sidecar, final_lock_path, content_bytes)
        _cleanup_sidecar()
    except FileExistsError:
        try:
            # Not load_lock() directly: the rival that just won the O_EXCL create may not
            # have written its JSON yet, and a zero-byte read here is contention, not
            # corruption (OI-264). Still raises if it stays unparseable.
            incumbent = _load_incumbent_lock(task_id, project_id, root=root)
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
            try:
                archive_lock(final_lock_path, now=resolved_now)
            except FileNotFoundError:
                pass  # already moved aside (e.g. expire-leases orphan sweep): just retry the link
            try:
                _exclusive_link(sidecar, final_lock_path, content_bytes)
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

    # From here on the canonical lock is ours. If settle/rescan blows up,
    # don't leave it behind: a queued task with a live lock is DENIED to
    # everyone until it expires (F02).
    try:
        # ---- 4. Settle ----
        sleep(settle_seconds)

        # ---- 5. Rescan ----
        conflict_paths = _scan_for_conflicts(final_lock_path.parent, task_id, holder, session)
    except BaseException:
        archive_lock_if_unchanged(final_lock_path, lock_dict, now=resolved_now)  # never raises OSError
        raise
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


def unexpected_error_exit(exc: BaseException) -> int:
    """Shared catch-all for the CLIs (mirrors expire-leases.py): anything that
    is not an AaosError becomes a one-line message and exit 1, never a raw
    traceback. Keeps every CLI inside the documented 0..5 exit contract."""
    print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def archive_lock_after_commit(lock_file: Path | str, *, now: datetime | None = None) -> None:
    """Post-commit lock release for complete/fail/post-handoff. The terminal
    transition already committed; a lock that vanished (concurrent sweep) or
    can't be moved is reported, not raised (mirrors expire-leases.py)."""
    lock_file = Path(lock_file)
    if not lock_file.exists():
        return
    try:
        archive_lock(lock_file, now=now)
    except OSError as exc:
        print(
            f"NOTE: lock {lock_file.name} already moved/removed or not archivable ({exc}); "
            "state change already committed, continuing"
        )


# ---------------------------------------------------------------------------
# 1.8 Commit transaction & the double-run backstop
# ---------------------------------------------------------------------------

def commit_state_change(new_state_obj: dict, state_path: Path | str, event: dict, *, root: str | None = None) -> str:
    """Stage new_state_obj to a tmp file, commit the event, then rename the
    tmp file into place. Ordering contract (unchanged conceptually post-
    review): the event is committed before the state is renamed into place.

    REVISED (post-review, PHASE3-DESIGN.md section 1.10): "committed" now
    means emit_event's durable pending-file write succeeded -- not that the
    event has necessarily been folded into the NDJSON yet (that fold/drain
    is best-effort and may happen slightly later). If emit_event raises
    EventCommitError (the pending write itself failed), the staged tmp
    state file is discarded and the state is left unchanged."""
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
    """Raise LeaseLostError unless task.lease is held by holder/session and
    not past its expiry+skew grace. The double-run backstop called
    immediately before commit_state_change in complete-task/fail-task/
    post-handoff.

    Residual-window note (Finding 8, adversarial review -- no code change,
    documented per the reviewer's request): this assert-then-replace
    sequence is NOT a compare-and-swap. There is a residual window between
    this check and the caller's subsequent commit_state_change during which
    another process could in principle also pass this same assert (e.g. two
    runs of the same script racing to complete the same task). In practice
    the realistic reclaim-vs-complete race -- a sweep reclaiming an expired
    lease while a live holder is still trying to complete it -- is already
    closed by the expiry check inside this function: a reclaim only
    proceeds once the lease is genuinely past expiry+skew, at which point
    this assert would itself raise LeaseLostError for the original holder.
    The residual case that remains is a *same-holder* double-run (e.g. a
    retried invocation, or an agent session accidentally started twice) --
    both invocations can pass this assert and both attempt the same
    terminal transition. This converges rather than corrupts: the second
    commit_state_change either produces an idempotent no-op (the caller
    scripts check terminal states before this assert) or, in the narrow
    window where both are mid-flight simultaneously, a duplicate terminal
    event (e.g. two Completed events) with a consistent final task state
    (last os.replace wins, and both writers were writing the same
    "done"/"failed" content). Safe-if-ugly; accepted."""
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
#
# REVISED (post-review): spool-as-commit-point + drain-lock model. See
# docs/PHASE3-DESIGN.md section 1.10 "REVISED (post-review)" for the full
# narrative; summary here for anyone reading only the code:
#
#   1. emit_event ALWAYS commits the event to a uniquely-named pending file
#      first (spool_event). That write is atomic and collision-free (see
#      the filename scheme below) and IS the durable commit -- if
#      everything after it fails, the event is still safely committed.
#   2. emit_event then attempts a best-effort drain: fold ALL pending files
#      (oldest-first) into the NDJSON, but only while holding an exclusive
#      drain lock (events/aaos-events.ndjson.drainlock, O_CREAT|O_EXCL,
#      the same primitive as task leases). If the lock is already held,
#      drain skips immediately -- it NEVER blocks/waits. The event stays
#      pending; a later drain (the next emit_event, an expire-leases sweep,
#      or a manual --drain-only) folds it in.
#   3. A drain lock whose `acquired` timestamp is older than 5 minutes (or
#      unparseable) is presumed abandoned by a crashed drainer: the next
#      drainer archives it (never deletes) and takes over.
#   4. Readers (project-notion.py, fail-task.py's DoubleFailure check) must
#      see pending-but-undrained events too -- see read_events() below.
# ---------------------------------------------------------------------------

_EVENT_SEQ = itertools.count(1)
_DRAIN_LOCK_STALE_MINUTES = 5


def spool_event(event: dict, project_id, *, root: str | None = None) -> Path:
    """Durably commit `event` to a uniquely-named pending file. This is the
    commit point (see module section header above): once this call
    returns, the event has happened, independent of whether/when it gets
    folded into the NDJSON.

    Filename is <time_ns>-<pid>-<uuid4 hex>.json. The time_ns-then-pid
    prefix preserves oldest-first sort order for draining (matching the
    pre-review scheme exactly); the uuid suffix is load-bearing, not
    decorative -- two threads in the *same* process can observe an
    identical time.time_ns() value (clock resolution is coarser than the
    GIL's context-switch granularity on some platforms), and without a
    per-call-unique suffix a second thread's atomic_write/os.replace would
    silently clobber the first thread's still-unread pending file, which is
    exactly the class of bug this whole redesign exists to eliminate."""
    spool_dir = event_spool_dir(project_id, root=root)
    os.makedirs(spool_dir, exist_ok=True)
    target = spool_dir / f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}.json"
    atomic_write(target, json.dumps(event, ensure_ascii=False))
    return target


def _exclusive_publish(path: Path, data: bytes) -> None:
    """Exclusive-create `path` (FileExistsError if it already exists) with
    `data` as its complete, already-durable content -- no partial-write
    window a concurrent reader can observe.

    Implementation note, found via this fix's own red/green TDD cycle (not
    theoretical): a plain os.open(O_CREAT|O_EXCL|O_WRONLY) followed by a
    separate write() -- the primitive _exclusive_create uses for task-lease
    locks elsewhere in this module -- has a narrow window between create
    and write during which a concurrent reader can observe the file as
    existing-but-empty/partial. Under the low contention task leases
    normally see (one lock per task) that has never been observed to
    matter; under the much higher contention the drain lock sees (every
    concurrent emit_event call across every task in a project races the
    SAME single lock), it was directly observed to matter: a reader would
    misread the in-flight write as an abandoned/stale lock and "steal" it,
    handing two callers the drain lock at once and reintroducing the exact
    unsynchronized-NDJSON-writer clobber (Finding 1) this mechanism exists
    to prevent.

    Fix: write the full content to a private temp file first (flushed +
    fsynced), then publish it under the real name via os.link, which is
    itself exclusive (raises FileExistsError if the destination already
    exists) but atomic-with-full-content -- there is no name under which
    the file is visible before its content is complete."""
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(str(tmp), str(path))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_drain_lock_acquired(lock_path: Path) -> datetime | None:
    """Parse a drain lock file's `acquired` field.

    Returns None ONLY when the lock's content was read and is unparseable
    -- callers treat that as stale, which is safe because
    _try_acquire_drain_lock publishes via _exclusive_publish: the file is
    never visible under its real name with partial content, so unparseable
    content is a genuinely corrupt/abandoned lock, not an in-flight write.

    Raises OSError when the lock could not be read at all. That is NOT
    evidence of staleness and must never be folded into the None/"stale"
    answer (OI-270): FileNotFoundError means the holder released between
    the caller's failed exclusive create and this read -- the single most
    routine event under contention -- and any other OSError (Windows
    sharing violation / delete-pending while the holder unlinks) means the
    lock's age is simply unknown. An earlier version returned None for
    both; the caller then "archived the stale lock" with a path-based
    os.replace that moved whatever was at the path by then -- a third
    drainer's LIVE lock -- and acquired too. Two drainers folding the
    NDJSON from the same snapshot is the lost-event race this whole
    mechanism exists to prevent."""
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return _parse_iso_dt(json.loads(raw).get("acquired"))
    except Exception:
        return None


def _try_acquire_drain_lock(project_id, *, root: str | None = None, now: datetime | None = None) -> Path | None:
    """Try to exclusively create the drain lock. Returns the lock path if
    acquired, or None if another drainer currently holds a live lock --
    this function NEVER blocks/waits on a live lock (design mandate: a
    skipped drain just leaves events safely pending for next time).

    A lock whose `acquired` timestamp is stale (>_DRAIN_LOCK_STALE_MINUTES
    old, or unparseable) is presumed abandoned by a crashed drainer: it is
    archived (os.replace, never deleted -- never-delete doctrine) and
    acquisition is retried once. A lock that is missing or unreadable by
    the time it is inspected is NOT stale -- see _read_drain_lock_acquired."""
    resolved_now = _now(now)
    lock_path = _drain_lock_path(project_id, root=root)
    content = json.dumps(
        {"holder_pid": os.getpid(), "acquired": resolved_now.isoformat()},
        ensure_ascii=False,
    ).encode("utf-8")

    os.makedirs(lock_path.parent, exist_ok=True)
    for _attempt in range(2):
        try:
            _exclusive_publish(lock_path, content)
            return lock_path
        except FileExistsError:
            stale_after = timedelta(minutes=_DRAIN_LOCK_STALE_MINUTES)
            try:
                acquired_at = _read_drain_lock_acquired(lock_path)
                is_stale = acquired_at is None or resolved_now - acquired_at > stale_after
            except FileNotFoundError:
                # Released between our create attempt and our read. Gone is
                # not stale: there is nothing to archive -- just compete for
                # the now-free lock on the next attempt (OI-270).
                continue
            except OSError:
                # Exists but unreadable, so its content can't be judged.
                # Fail closed on a fresh lock, but keep the unwedge path for
                # a crashed drainer's lock that STAYS unreadable: fall back
                # to the file's own mtime, which is fresh for any live lock.
                # mtime is a real filesystem time, so it is compared against
                # the real clock -- never `resolved_now`, which a caller may
                # have injected far from wall-clock.
                try:
                    lock_age = time.time() - os.stat(lock_path).st_mtime
                except FileNotFoundError:
                    continue  # released meanwhile: gone is not stale
                except OSError as exc:
                    print(
                        f"WARNING: drain lock {lock_path} can be neither read nor stat'ed ({exc}); "
                        "skipping this drain -- events stay pending until it clears",
                        file=sys.stderr,
                    )
                    return None
                is_stale = lock_age > stale_after.total_seconds()
            if not is_stale:
                return None  # a live drainer is active -- never steal, never block

            archive_dir = _events_archive_dir(project_id, root=root)
            os.makedirs(archive_dir, exist_ok=True)
            compact_ts = resolved_now.strftime("%Y%m%dT%H%M%S%f")
            target = archive_dir / f"drainlock.{compact_ts}"
            if target.exists():
                target = archive_dir / f"drainlock.{compact_ts}.{os.getpid()}"
            try:
                os.replace(str(lock_path), str(target))
            except FileNotFoundError:
                pass  # someone else already cleared it -- fall through and retry create
    return None


def _release_drain_lock(lock_path: Path) -> None:
    """Release (remove) a drain lock we hold, retrying briefly on a
    transient sharing violation.

    Found via this fix's own red/green TDD cycle: on Windows, a concurrent
    _read_drain_lock_acquired() call from another thread checking whether
    it's safe to steal a (still-live) lock can momentarily hold an open
    read handle on the exact same path this call is trying to unlink,
    raising PermissionError(13, "used by another process"). A single
    unretried failure here used to be swallowed silently, permanently
    orphaning the lock for the rest of the process's life (it's still
    fresh, so nothing else is willing to steal it either) -- draining
    would then wedge indefinitely with events stuck pending, which is
    exactly the kind of loss this whole mechanism exists to prevent. The
    retries are bounded (<=~50ms worst case) and Windows-transient sharing
    violations clear in low single-digit milliseconds in practice."""
    for attempt in range(10):
        try:
            os.unlink(str(lock_path))
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < 9:
                time.sleep(0.005)
    # Still couldn't remove it after retrying -- leave it in place rather
    # than raise (this runs in a `finally`). Its `acquired` timestamp is
    # still accurate, so the next drainer's staleness check behaves
    # correctly (treats it as live until it genuinely goes stale); no
    # event is ever at risk from a release failure, only drain latency.


# Bounded retry for reading one pending file: 3 attempts, sleeping 0.05s then 0.1s between them
# (<= 0.15s worst case per file). Long enough to ride out a typical antivirus / OneDrive sharing
# violation, short enough that a persistently locked file cannot stall a drain or a reader.
_PENDING_READ_BACKOFF = (0.05, 0.1)
_PENDING_READ_ATTEMPTS = len(_PENDING_READ_BACKOFF) + 1

# A pending file that fails to decode or parse may simply be half-synced (OneDrive delivers the
# bytes in pieces): treat that as transient -- leave it in pending/ and retry at the next drain --
# while its mtime is younger than this. Only a file still bad after the window is quarantined, so a
# genuinely corrupt file is quarantined eventually and never wedges the queue.
QUARANTINE_GRACE_SECONDS = 600


def _read_pending_text(p: Path):
    """Read one pending event file. Returns (status, value):
      ("ok", text)               -- readable (a leading UTF-8 BOM is accepted and stripped)
      ("vanished", None)         -- FileNotFoundError
      ("undecodable", exc)       -- the bytes are not valid UTF-8 (corrupt, or half-synced: the caller
                                    applies the QUARANTINE_GRACE_SECONDS window)
      ("transient", exc)         -- any other OSError that persisted through the bounded retry
    Shared by drain_event_spool and read_events so both ride out the same transient faults."""
    for attempt in range(_PENDING_READ_ATTEMPTS):
        try:
            return "ok", p.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return "vanished", None
        except UnicodeDecodeError as exc:
            return "undecodable", exc
        except OSError as exc:
            if attempt < _PENDING_READ_ATTEMPTS - 1:
                time.sleep(_PENDING_READ_BACKOFF[attempt])
                continue
            return "transient", exc


def _within_quarantine_grace(p: Path) -> bool:
    """True while a pending file's mtime is within QUARANTINE_GRACE_SECONDS of the wall clock (either
    side, to tolerate cross-machine clock skew on a synced file). The wall clock is deliberate: mtime
    is wall-clock time, so the drain's injectable `now` is not used here. If the file cannot be
    stat'ed its age is unknown, so it is treated as within grace (deferred with a warning, never
    quarantined on a guess)."""
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return True
    return abs(age) < QUARANTINE_GRACE_SECONDS


def _quarantine_pending(p: Path, name: str, corrupt_dir: Path, why) -> bool:
    """Move a pending file that WAS read and is genuinely bad to pending/_corrupt/ (never deleted).

    Never raises: a failure to create _corrupt/ or to move the file leaves it in pending/ (the next
    drain tries again) and must not abort the drain loop before the NDJSON write for the valid events
    behind it. The warning says truthfully which of the two happened. Returns True if moved."""
    target = corrupt_dir / name
    try:
        os.makedirs(corrupt_dir, exist_ok=True)
        os.replace(str(p), str(target))
    except OSError as move_exc:
        print(
            f"WARNING: malformed pending event file {name} could not move to {target} ({move_exc}); "
            f"left in pending/, will retry at the next drain (not folded, not lost): {why}",
            file=sys.stderr,
        )
        return False
    print(
        f"WARNING: malformed pending event file moved to {target} (not folded, not lost): {why}",
        file=sys.stderr,
    )
    return True


def _defer_or_quarantine_pending(p: Path, name: str, corrupt_dir: Path, why) -> None:
    """A decode/parse failure: defer while the file may still be syncing, quarantine once it is old."""
    if _within_quarantine_grace(p):
        print(
            f"WARNING: pending event file {name} is not yet valid ({why}); it may still be syncing, so it "
            f"stays in pending/ for the next drain (quarantined only if still bad after "
            f"{QUARANTINE_GRACE_SECONDS}s)",
            file=sys.stderr,
        )
        return
    _quarantine_pending(p, name, corrupt_dir, why)


def drain_event_spool(project_id, *, root: str | None = None, now: datetime | None = None) -> int:
    """Fold all committed-but-undrained pending events into the NDJSON.

    The pending directory IS the commit point (see spool_event); draining
    is a best-effort optimization, not a correctness requirement. If the
    drain lock is held by another drainer, this returns 0 immediately
    without touching anything (never blocks). Returns the number of
    pending files folded/moved in this call (0 if nothing was pending or
    the lock couldn't be acquired).

    Concurrency: mutual exclusion is the O_CREAT|O_EXCL drain lock (see
    _try_acquire_drain_lock) -- only the lock holder ever writes the
    NDJSON, so the old mtime-guarded-read-then-replace race (Finding 1)
    cannot recur: there is exactly one writer at a time by construction,
    not by optimistic detection.

    Dedup-by-event_id against the existing NDJSON content is deliberate,
    not just defensive: the merge below is two steps (write NDJSON, then
    move drained pending files to _drained/) that are not atomic as a
    pair. If a drainer crashes between them, the same pending files will
    be re-read by the next drain; without the dedup check their events
    would be duplicated into the NDJSON.

    Malformed pending files are moved to .../<log>.pending/_corrupt/ (never
    unlinked -- never-delete doctrine, Finding 7) with a loud stderr
    warning, rather than silently discarded."""
    spool_dir = event_spool_dir(project_id, root=root)
    if not spool_dir.is_dir():
        return 0

    def _list_pending():
        return sorted(p.name for p in spool_dir.iterdir() if p.is_file() and p.name.endswith(".json"))

    if not _list_pending():
        return 0

    lock_path = _try_acquire_drain_lock(project_id, root=root, now=now)
    if lock_path is None:
        return 0  # another drainer is active (or we lost the race) -- never block

    try:
        names = _list_pending()  # re-list under the lock: more may have landed since the check above
        if not names:
            return 0

        log_path = events_log_path(project_id, root=root)
        existing_content = ""
        existing_ids: set = set()
        if log_path.exists():
            existing_content = log_path.read_text(encoding="utf-8")
            for line in existing_content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_ids.add(json.loads(line).get("event_id"))
                except json.JSONDecodeError:
                    continue

        corrupt_dir = _pending_corrupt_dir(project_id, root=root)
        drained_dir = _pending_drained_dir(project_id, root=root)

        new_lines: list[str] = []
        to_move: list[tuple[Path, str]] = []

        for name in names:
            p = spool_dir / name
            status, raw = _read_pending_text(p)
            if status == "vanished":
                # Listed a moment ago, gone now. We hold the exclusive drain
                # lock, so nothing should be moving pending files out from
                # under us: this is the signature of a SECOND drainer, which
                # is how OI-270 lost events (the other drainer folds this
                # file, then one fold's os.replace clobbers the other's
                # lines). Nothing here can be folded, so skip it -- but
                # loudly, never as the benign timing quirk an earlier
                # comment here took it for.
                print(
                    f"WARNING: pending event file {name} vanished while the drain lock was held -- "
                    "another drainer is active despite the lock; verify its event reached the NDJSON",
                    file=sys.stderr,
                )
                continue
            if status == "transient":
                # A read error is NOT evidence the event is bad (a OneDrive or antivirus sharing
                # violation is the everyday cause). Quarantining it here used to move a valid event
                # to _corrupt/, which no reader looks at. Leave it in pending/ so the next drain retries it.
                # (read_events retries the same way and warns if it still cannot read it, so readers do
                # not see the event until it becomes readable; it is deferred, not lost.)
                print(
                    f"WARNING: pending event file {name} could not be read ({raw}); leaving it in "
                    "pending/ for the next drain (not folded, not lost)",
                    file=sys.stderr,
                )
                continue
            if status == "undecodable":
                # Possibly a half-synced file cut inside a multi-byte character: grace window applies.
                _defer_or_quarantine_pending(p, name, corrupt_dir, f"not valid UTF-8: {raw}")
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                # Possibly a half-synced (truncated) file: grace window applies.
                _defer_or_quarantine_pending(p, name, corrupt_dir, exc)
                continue
            if not isinstance(event, dict) or not event.get("event_id"):
                # Complete, parseable JSON of the wrong shape is not a sync artifact: quarantine now.
                _quarantine_pending(p, name, corrupt_dir, "pending event missing event_id or not an object")
                continue

            # A file deferred by an earlier drain (transient read error or grace window) folds here,
            # possibly after events with later timestamps, so the NDJSON is append-ordered, not
            # timestamp-ordered. That is expected: read_events sorts by parsed timestamp, and every
            # reader goes through read_events rather than raw NDJSON order.
            if event["event_id"] not in existing_ids:
                new_lines.append(json.dumps(event, ensure_ascii=False))
                existing_ids.add(event["event_id"])
            to_move.append((p, name))

        if new_lines:
            content = existing_content
            if content and not content.endswith("\n"):
                content += "\n"
            content += "\n".join(new_lines) + "\n"
            tmp = log_path.parent / f"{log_path.name}.tmp-{os.getpid()}"
            atomic_write(tmp, content)
            os.replace(str(tmp), str(log_path))

        if to_move:
            os.makedirs(drained_dir, exist_ok=True)
            for p, name in to_move:
                try:
                    os.replace(str(p), str(drained_dir / name))
                except OSError:
                    pass

        return len(to_move)
    finally:
        _release_drain_lock(lock_path)


def read_events(project_id, *, root: str | None = None) -> list[dict]:
    """The shared "what has actually happened" view: NDJSON lines plus all
    not-yet-drained pending events, deduped by event_id, sorted by parsed
    timestamp (stable -- ties preserve NDJSON-then-pending, oldest-pending-
    first order; entries with an unparseable/missing timestamp sort last,
    fail-closed, rather than crashing or being silently dropped).

    Every AAOS reader that needs "has event X happened for task Y" must go
    through this, not raw NDJSON parsing: an event that emit_event has
    durably committed to pending, but that hasn't been folded into the
    NDJSON yet (e.g. because another drainer held the lock), is still a
    real, already-happened event -- a reader that only looks at the NDJSON
    can miss it. Concretely: project-notion.py's latest-event-per-task and
    fail-task.py's prior-Failed DoubleFailure check."""
    events: list[dict] = []
    seen_ids: set = set()

    log_path = events_log_path(project_id, root=root)
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if event_id is not None:
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
            events.append(event)

    spool_dir = event_spool_dir(project_id, root=root)
    if spool_dir.is_dir():
        for name in sorted(p.name for p in spool_dir.iterdir() if p.is_file() and p.name.endswith(".json")):
            # Same bounded-retry reader as the drain: a briefly locked file (antivirus / OneDrive) must
            # not silently vanish from this view -- fail-task's DoubleFailure check depends on it.
            status, raw = _read_pending_text(spool_dir / name)
            if status == "vanished":
                # A concurrent drain folded it and moved it to _drained/ after the listing. Its event is in
                # the NDJSON now, but possibly after the NDJSON snapshot above was read (pre-existing
                # reader race, unchanged here); readers are not lock holders, so this is not an anomaly.
                continue
            if status == "transient":
                print(
                    f"WARNING: read_events could not read pending event file {name} ({raw}); this view "
                    "omits that event until the file becomes readable (not lost)",
                    file=sys.stderr,
                )
                continue
            if status == "undecodable":
                continue  # half-synced or corrupt; the drain owns the grace-window / quarantine decision
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if event_id is not None:
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
            events.append(event)

    def _sort_key(item):
        idx, event = item
        try:
            return (0, _parse_iso_dt(event.get("timestamp")).timestamp())
        except CorruptStateError:
            return (1, idx)

    indexed = sorted(enumerate(events), key=_sort_key)
    return [event for _, event in indexed]


def emit_event(
    type: str, actor: str, project_id: str,
    refs: dict | None = None, detail: str | None = None,
    event_log_ref: str | None = None, *,
    now: datetime | None = None, root: str | None = None,
) -> str:
    """Commit an event, then best-effort fold it (and anything else
    pending) into the NDJSON. See the module section header above and
    docs/PHASE3-DESIGN.md section 1.10 REVISED for the full design.

    EventCommitError is raised ONLY if the event fails schema validation,
    or if the durable pending-file commit itself fails -- never because of
    a drain failure (drain is best-effort; the event is already safely
    committed by the time drain is attempted)."""
    resolved_now = _now(now)

    # event_id: digits-only composite of wall-clock time_ns + pid + a
    # per-process monotonic counter (Finding 1b). time.time_ns() is the
    # REAL clock, independent of any injected `now` -- this is what fixes
    # the cross-process collision the old `next(_EVENT_SEQ)`-only scheme
    # had, since two processes given the same injected --now used to mint
    # identical ids. The trailing _EVENT_SEQ counter additionally guards
    # against two threads in the same process observing an identical
    # time_ns() value. Still matches schema pattern ^e-[0-9TZ:.+-]+-[0-9]+$
    # (digits-only after the final hyphen).
    event_id = f"e-{resolved_now.isoformat()}-{time.time_ns()}{os.getpid()}{next(_EVENT_SEQ)}"

    event = {
        "protocol_version": "1.0",
        "event_id": event_id,
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
        raise EventCommitError(f"event failed schema validation, not committed: {errors}")

    # Commit: this write IS the durability guarantee. If it fails, nothing
    # happened and the caller sees EventCommitError (exit 5).
    try:
        spool_event(event, project_id, root=root)
    except Exception as exc:
        raise EventCommitError(f"failed to commit event to pending spool: {exc}") from exc

    # Best-effort drain. Never raises: a drain failure here leaves the
    # event exactly where it already durably is (pending), for the next
    # successful drain to fold in. Still surfaced loudly on stderr so a
    # persistent drain problem isn't silent.
    try:
        drain_event_spool(project_id, root=root, now=resolved_now)
    except Exception as exc:
        print(f"WARNING: drain_event_spool failed (event already committed to pending): {exc}", file=sys.stderr)

    return event_id
