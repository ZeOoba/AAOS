#!/usr/bin/env python3
"""
tools/validate.py — validate an AAOS state file against its JSON Schema.

Usage:
    python tools/validate.py <path-to-json-file> [--schema-dir <dir>]
                                                   [--schema <name>]
                                                   [--registry <path>]

Behavior (see docs/adr/0002-transport-mechanics.md for the directory layout
this schema-inference table is derived from, and docs/AAOS-SPEC.md §6
(Schemas) for background on why write_scope is load-bearing):

  1. Schema inference from filename (unless --schema is given explicitly):
       agent-registry.json                          -> agent-registry.schema.json
       heartbeat.json                                -> heartbeat.schema.json
       name starts with "t-", or under a tasks/ dir  -> task.schema.json
       name starts with "h-", or under a handoffs/   -> handoff.schema.json
       name ends with ".ndjson", or under an events/ -> event.schema.json
                                                          (validated as a log:
                                                           one record per
                                                           non-empty line)
     If none of these match and --schema was not given, this is a usage
     error (exit 2) — the tool never guesses.

  2. protocol_version major-version check. Runs BEFORE full schema
     validation and is reported with a distinct "protocol_version mismatch"
     message that points to docs/MIGRATIONS.md, rather than being folded
     into a generic schema error. Only the major version component is
     compared against SUPPORTED_MAJOR_VERSION, so this file does not need
     an edit for every minor protocol bump.

  3. Full JSON Schema validation via jsonschema.Draft202012Validator,
     reporting every error found (not just the first), each annotated with
     a JS-like path (e.g. "agents[0].id").

  4. write_scope enforcement — ONLY for files validated against
     handoff.schema.json. Every artifact_manifest[].path must fall inside
     at least one of the handoff's from_agent's write_scope globs, as
     declared in the agent registry (default: agent-registry.json at the
     repo root; override with --registry). Glob matching uses
     fnmatch.fnmatch (documented choice — see match_write_scope() in
     _aaos_common.py, its single home since the Phase 3 lift)
     after normalizing both the candidate path and the glob pattern to
     forward slashes, since write_scope globs in this project are always
     authored with forward slashes (e.g. "C:/Users/Owner/AAOS/**") even on
     Windows — and after lexically resolving "." / ".." segments in the
     candidate path, so a directory-traversal path cannot pass the check
     merely by starting with an in-scope literal prefix while actually
     resolving outside it. A from_agent that does not exist in the registry
     at all is itself a write-scope failure, not a silent skip; so is a
     present-but-unrecognized suggested_next_owner (advisory for routing
     purposes only — that does not exempt it from being a real agent id).
     The registry file itself is loaded once, eagerly, for any handoff
     validation; if it is missing or unreadable/corrupt, that is a usage
     error (exit 2), not a write-scope violation.

Exit codes:
    0   file is valid
    1   file is invalid (protocol_version mismatch, schema errors, and/or
        a write-scope violation)
    2   usage error (schema could not be inferred, file/schema/registry
        not found or unreadable, etc.) — distinct from a validation failure
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from _aaos_common import match_write_scope  # single home (Phase 3 lift, PHASE3-DESIGN.md §1.1)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_SCHEMA_DIR = REPO_ROOT / "schemas"
# Spec'd default is the literal path C:\Users\Owner\AAOS\agent-registry.json;
# REPO_ROOT resolves to exactly that directory in this checkout, so this is
# equivalent while staying correct if the repo is ever relocated in lockstep
# with tools/validate.py.
DEFAULT_REGISTRY_PATH = REPO_ROOT / "agent-registry.json"

SUPPORTED_MAJOR_VERSION = "1"

SCHEMA_FILES = {
    "agent-registry": "agent-registry.schema.json",
    "task": "task.schema.json",
    "handoff": "handoff.schema.json",
    "event": "event.schema.json",
    "heartbeat": "heartbeat.schema.json",
}

MIGRATIONS_POINTER = "docs/MIGRATIONS.md"


def infer_schema_name(path: Path) -> str | None:
    """Infer which AAOS schema a file should be validated against, from its
    filename and containing directory names alone. Returns None if no rule
    matches (caller must then treat this as a usage error, never a guess).
    """
    name = path.name
    dir_parts_lower = {p.lower() for p in path.parts[:-1]}

    if name == "agent-registry.json":
        return "agent-registry"
    if name == "heartbeat.json":
        return "heartbeat"
    if name.startswith("t-") or "tasks" in dir_parts_lower:
        return "task"
    if name.startswith("h-") or "handoffs" in dir_parts_lower:
        return "handoff"
    if name.endswith(".ndjson") or "events" in dir_parts_lower:
        return "event"
    return None


def format_json_path(absolute_path) -> str:
    """Render a jsonschema error's absolute_path deque as a JS-like path,
    e.g. deque(['agents', 0, 'id']) -> "agents[0].id". Empty path -> "<root>".
    """
    parts = list(absolute_path)
    if not parts:
        return "<root>"
    out = []
    for i, part in enumerate(parts):
        if isinstance(part, int):
            out.append(f"[{part}]")
        else:
            out.append(str(part) if i == 0 else f".{part}")
    return "".join(out)


def check_protocol_version(instance: dict) -> str | None:
    """Return an error message if instance's protocol_version is missing or
    its major version isn't SUPPORTED_MAJOR_VERSION; None if it's fine.
    Deliberately reads only this one field, independent of whether the rest
    of the instance is schema-valid.

    Callers guarantee `instance` is already a dict — the "top-level JSON
    value is not an object" case is a distinct schema-shape problem, not a
    protocol_version problem, and is caught earlier by validate_instance
    before this function is ever called.
    """
    pv = instance.get("protocol_version")
    if not isinstance(pv, str) or not pv:
        return (
            "protocol_version mismatch: 'protocol_version' field is missing "
            f"or not a string. See {MIGRATIONS_POINTER}."
        )
    major = pv.split(".", 1)[0]
    if major != SUPPORTED_MAJOR_VERSION:
        return (
            f"protocol_version mismatch: file declares protocol_version "
            f"'{pv}', but this tool only supports major version "
            f"{SUPPORTED_MAJOR_VERSION}.x. See {MIGRATIONS_POINTER} for the "
            "migration process."
        )
    return None


def load_registry(registry_path: Path):
    """Load and return the agent registry JSON. Raises OSError/JSONDecodeError
    on failure — the caller (main(), for the only schema that needs a
    registry: "handoff") turns that into a usage error (exit 2)."""
    with open(registry_path, encoding="utf-8") as fh:
        return json.load(fh)


# match_write_scope lives in _aaos_common (imported above) — lifted there in
# Phase 3 per PHASE3-DESIGN.md §1.1 so exactly one traversal-safe
# implementation exists. Its full rationale docstring (fnmatch-over-PurePath,
# posixpath.normpath canonicalization as the Gate-2 traversal-bypass fix)
# moved with it.


def check_write_scope(handoff: dict, registry, registry_path: Path) -> list[str]:
    """Return a list of WRITE-SCOPE VIOLATION error strings (empty if none)
    for a handoff-shaped instance. Only called when the target file's
    inferred/explicit schema is "handoff".

    `registry` is the already-loaded agent-registry JSON. Loading the
    registry file itself (and any OSError/JSONDecodeError that implies) is
    the caller's responsibility, handled as a usage error (exit 2) before
    this function is ever invoked — a missing/unreadable registry is an
    environment problem, not a validation-content problem, so it must not
    be folded into a WRITE-SCOPE VIOLATION here.
    """
    errors: list[str] = []

    from_agent = handoff.get("from_agent")
    if not isinstance(from_agent, str) or not from_agent:
        errors.append(
            "WRITE-SCOPE VIOLATION: handoff has no valid 'from_agent' field "
            "to check write_scope against"
        )
        return errors

    agents = registry.get("agents", []) if isinstance(registry, dict) else []
    agent = next(
        (a for a in agents if isinstance(a, dict) and a.get("id") == from_agent),
        None,
    )
    if agent is None:
        errors.append(
            f"WRITE-SCOPE VIOLATION: from_agent '{from_agent}' does not "
            f"exist in the agent registry at '{registry_path}'"
        )
        return errors

    write_scope = agent.get("write_scope", [])
    if not isinstance(write_scope, list):
        write_scope = []

    manifest = handoff.get("artifact_manifest", [])
    if isinstance(manifest, list):
        for item in manifest:
            if not isinstance(item, dict):
                continue
            art_path = item.get("path")
            if not isinstance(art_path, str):
                continue
            if not match_write_scope(art_path, write_scope):
                errors.append(
                    f"WRITE-SCOPE VIOLATION: path '{art_path}' is outside "
                    f"agent '{from_agent}'s declared write_scope"
                )

    # suggested_next_owner is optional and, per handoff.schema.json, purely
    # advisory for ROUTING (AAOS never auto-assigns off of it) — but that
    # does not license a garbage value. A schema-valid string that names no
    # registered agent is a referential data-quality error, same class as
    # from_agent naming a nonexistent agent above, so it is checked here
    # against the same registry and reported the same way.
    suggested_next_owner = handoff.get("suggested_next_owner")
    if isinstance(suggested_next_owner, str) and suggested_next_owner:
        known_ids = {a.get("id") for a in agents if isinstance(a, dict)}
        if suggested_next_owner not in known_ids:
            errors.append(
                f"WRITE-SCOPE VIOLATION: suggested_next_owner "
                f"'{suggested_next_owner}' is not a registered agent id"
            )

    return errors


# register-scheduled-task skill (v2.13, the live install under
# local-agent-mode-sessions/skills-plugin/.../skills/register-scheduled-task)
# makes list_scheduled_tasks a MANDATORY verification step ("no 'done' without
# evidence ... paste the returned entry") -- it is never itself prohibited.
# What is actually prohibited is using it (or its output) AS the registration
# mechanism: registration is create_scheduled_task/update_scheduled_task-only,
# never a hand-written/JSON-splice write.
#
# POSITIVE-EVIDENCE design (independent review round 2, 2026-08-22, "doctrine
# check too narrow"): an earlier version flagged any occurrence of the needle
# with no "verification word" nearby. That is too broad in the other
# direction -- register-scheduled-task's own SKILL.md legitimately mentions
# list_scheduled_tasks with NO verification word anywhere nearby (e.g. its
# surface note on which sessions carry the MCP tools, or the PATH-
# DISAMBIGUATOR note on picking the right fleet's entry) -- a handoff that
# accurately quotes or relays that doctrine text would have been wrongly
# rejected. Requiring actual splice/hand-write language as positive evidence
# of the PROHIBITED mechanism -- the doctrine's own term of art, per
# CLAUDE.md ("never JSON-splice") and this skill ("JSON-splice registration
# is PROHIBITED") -- closes that gap: only flag when the text describes
# doing the forbidden thing, not merely mentioning the tool.
#
# Word-boundary matched, NOT substring (independent review round 1, M2): a
# bare substring test on a verify-word list let "checklist"/"confirmation"
# falsely suppress a real finding -- moot now that suppression is gone, but
# the same discipline applies to this positive-evidence regex too (so a
# word like "splicer" as a name wouldn't spuriously anchor on "splice").
_SPLICE_ACTION_RE = re.compile(
    r"\bsplic(?:e|es|ed|ing)\b|\bhand[- ]?(?:writ(?:e|es|ten|ing)|edit(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
# Chars scanned on each side of a needle occurrence (independent review round
# 1, M1): an earlier sentence-splitting implementation let a signal word
# anywhere in an unbounded comma-joined run-on (or a field with no terminal
# punctuation at all) apply to the whole field. A fixed window bounds how
# far the splice-action language may sit from the actual mention and still
# count as describing THAT mention's mechanism.
_REGISTRATION_DOCTRINE_WINDOW = 80


def _registration_doctrine_flagged_excerpts(value: str, needle: str) -> list[str]:
    """Excerpts (needle +/- _REGISTRATION_DOCTRINE_WINDOW chars) around each
    occurrence of `needle` in `value` that ALSO carries splice/hand-write
    language in that same window -- positive evidence that list_scheduled_
    tasks is being described as the write mechanism, not merely mentioned.

    Case-insensitive on both the needle and the action regex (independent
    review round 2, "case-sensitive needle bypass": the original used
    value.find(), which would miss 'List_scheduled_tasks'/
    'LIST_SCHEDULED_TASKS'). Excerpts are still sliced from the ORIGINAL
    (not lowercased) text so error messages preserve real casing. Pure."""
    out = []
    low = value.lower()
    needle_low = needle.lower()
    start = 0
    while True:
        idx = low.find(needle_low, start)
        if idx == -1:
            break
        lo = max(0, idx - _REGISTRATION_DOCTRINE_WINDOW)
        hi = min(len(value), idx + len(needle) + _REGISTRATION_DOCTRINE_WINDOW)
        window = value[lo:hi]
        if _SPLICE_ACTION_RE.search(window):
            out.append(window.strip())
        start = idx + len(needle_low)
    return out


def check_registration_doctrine(handoff: dict) -> list[str]:
    """Return REGISTRATION-DOCTRINE VIOLATION error strings (empty if none)
    for a handoff-shaped instance whose free-text fields describe using
    'list_scheduled_tasks' AS the registration/write mechanism (splice/
    hand-write language nearby -- see _registration_doctrine_flagged_
    excerpts), not merely mentioning the tool.

    WHY (2026-08-22): scheduled-task registration doctrine (register-
    scheduled-task skill) is create_scheduled_task-only -- never a
    list_scheduled_tasks-driven JSON-splice. That rule was prose-only, so a
    handoff brief could carry the violation forward to its receiving agent
    with nothing to catch it. Checked fields: context_summary,
    required_next_action, blockers -- the free-text fields a handoff author
    would actually write instructions into. A path inside artifact_manifest
    is not checked; a real file path containing that substring is not the
    failure mode this guards against.

    NOT flagged: a bare mention of list_scheduled_tasks with no splice/
    hand-write language nearby (e.g. "verified via list_scheduled_tasks",
    or doctrine-quoting prose like "list_scheduled_tasks is unavailable in
    a cloud-sandbox session") -- neither describes doing the prohibited
    thing. One error per offending occurrence, not per field/entry -- a
    field with two bad mentions must not read as a single finding."""
    errors: list[str] = []
    needle = "list_scheduled_tasks"

    for field in ("context_summary", "required_next_action"):
        value = handoff.get(field)
        if isinstance(value, str):
            for excerpt in _registration_doctrine_flagged_excerpts(value, needle):
                errors.append(
                    f"REGISTRATION-DOCTRINE VIOLATION: '{field}' references "
                    f"'{needle}' as a splice/hand-write mechanism ({excerpt!r}) "
                    "-- registration/tooling flows are create_scheduled_task-"
                    "only, never a list_scheduled_tasks-driven JSON-splice "
                    "(register-scheduled-task skill doctrine)."
                )

    blockers = handoff.get("blockers")
    if isinstance(blockers, list):
        for b in blockers:
            if not isinstance(b, str):
                continue
            for excerpt in _registration_doctrine_flagged_excerpts(b, needle):
                errors.append(
                    f"REGISTRATION-DOCTRINE VIOLATION: 'blockers' entry "
                    f"references '{needle}' as a splice/hand-write mechanism "
                    f"({excerpt!r}) -- registration/tooling flows are "
                    "create_scheduled_task-only, never a list_scheduled_tasks-"
                    "driven JSON-splice (register-scheduled-task skill doctrine)."
                )

    return errors


def validate_instance(
    instance, validator: Draft202012Validator, schema_name: str, registry, registry_path: Path
) -> list[str]:
    """Validate one parsed JSON record against the given schema/rules.
    Returns a flat list of error strings (empty list means valid)."""
    errors: list[str] = []

    # A non-dict top-level value (e.g. a bare JSON array) is caught first,
    # with its own accurately-worded message. This must run BEFORE the
    # protocol_version check: protocol_version is a field read off a dict,
    # and "this isn't a dict at all" is a distinct schema-shape problem, not
    # a protocol_version mismatch — reporting it as the latter would be
    # misleading about what's actually wrong.
    if not isinstance(instance, dict):
        errors.append("SCHEMA ERROR: top-level JSON value is not an object")
        return errors

    pv_error = check_protocol_version(instance)
    if pv_error:
        # protocol_version is checked, and reported, before full schema
        # validation runs — a mismatched protocol is not a "generic schema
        # error" even though the same const constraint would also flag it.
        errors.append(pv_error)
        return errors

    for err in validator.iter_errors(instance):
        path_str = format_json_path(err.absolute_path)
        errors.append(f"SCHEMA ERROR at {path_str}: {err.message}")

    if schema_name == "handoff":
        errors.extend(check_write_scope(instance, registry, registry_path))
        errors.extend(check_registration_doctrine(instance))

    return errors


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate an AAOS state file against its JSON Schema.",
    )
    parser.add_argument("path", help="Path to the JSON (or .ndjson) file to validate.")
    parser.add_argument(
        "--schema-dir",
        default=None,
        help="Directory containing the AAOS schema files "
        "(default: schemas/ next to this script's own location).",
    )
    parser.add_argument(
        "--schema",
        choices=sorted(SCHEMA_FILES),
        default=None,
        help="Explicitly select the schema instead of inferring it from the filename.",
    )
    parser.add_argument(
        "--registry",
        default=None,
        help="Path to agent-registry.json used for write_scope enforcement "
        f"(default: {DEFAULT_REGISTRY_PATH}).",
    )
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    target = Path(args.path)
    schema_dir = Path(args.schema_dir) if args.schema_dir else DEFAULT_SCHEMA_DIR
    registry_path = Path(args.registry) if args.registry else DEFAULT_REGISTRY_PATH

    schema_name = args.schema or infer_schema_name(target)
    if schema_name is None:
        print(
            f"USAGE ERROR: cannot infer a schema for '{target}' from its "
            "filename; pass --schema explicitly (one of: "
            f"{', '.join(sorted(SCHEMA_FILES))})."
        )
        return 2

    if not target.exists():
        print(f"USAGE ERROR: file not found: {target}")
        return 2

    schema_path = schema_dir / SCHEMA_FILES[schema_name]
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"USAGE ERROR: could not load schema '{schema_path}': {exc}")
        return 2

    validator = Draft202012Validator(schema)

    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"USAGE ERROR: could not read '{target}': {exc}")
        return 2

    # The agent registry is only needed for handoff write_scope enforcement,
    # so it's only loaded (and only required to exist/parse) for that
    # schema — this preserves existing behavior for task/heartbeat/event/
    # agent-registry files, which never touch --registry at all. A missing
    # or unreadable/corrupt registry is a usage/environment problem (the
    # same class as a missing target file or --schema-dir), so it exits 2
    # here rather than being folded into a WRITE-SCOPE VIOLATION (exit 1).
    registry = None
    if schema_name == "handoff":
        try:
            registry = load_registry(registry_path)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"USAGE ERROR: could not load agent registry '{registry_path}': {exc}")
            return 2

    if schema_name == "event":
        return _validate_ndjson(target, raw_text, validator, schema_name, registry, registry_path)
    return _validate_single(target, raw_text, validator, schema_name, registry, registry_path)


def _validate_single(target, raw_text, validator, schema_name, registry, registry_path) -> int:
    try:
        instance = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"JSON PARSE ERROR: {target}: {exc}")
        return 1

    errors = validate_instance(instance, validator, schema_name, registry, registry_path)
    if errors:
        for e in errors:
            print(e)
        return 1

    print(f"VALID: {target}")
    return 0


def _validate_ndjson(target, raw_text, validator, schema_name, registry, registry_path) -> int:
    all_errors: list[str] = []
    saw_any_line = False

    for lineno, line in enumerate(raw_text.splitlines(), start=1):
        if not line.strip():
            continue
        saw_any_line = True
        try:
            instance = json.loads(line)
        except json.JSONDecodeError as exc:
            all_errors.append(f"line {lineno}: JSON PARSE ERROR: {exc}")
            continue
        for e in validate_instance(instance, validator, schema_name, registry, registry_path):
            all_errors.append(f"line {lineno}: {e}")

    if not saw_any_line:
        print(f"USAGE ERROR: '{target}' has no non-empty lines to validate")
        return 2

    if all_errors:
        for e in all_errors:
            print(e)
        return 1

    print(f"VALID: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
