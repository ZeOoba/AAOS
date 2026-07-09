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
     fnmatch.fnmatch (documented choice — see match_write_scope() below)
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
import fnmatch
import json
import posixpath
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

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
    """
    norm_candidate = posixpath.normpath(candidate_path.replace("\\", "/"))
    for pattern in write_scope:
        if not isinstance(pattern, str):
            continue
        norm_pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(norm_candidate, norm_pattern):
            return True
    return False


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
