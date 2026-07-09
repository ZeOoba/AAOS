"""
Tests for tools/validate.py.

Written FIRST per AAOS TDD doctrine: at the point this file is authored,
tools/validate.py does not exist yet, so every test in this module is
expected to fail (via ImportError at module load, surfaced by unittest as
a collection/setUp error) until tools/validate.py is implemented.

Run with:  python -m unittest tests.test_validate -v   (from the repo root)
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
SCHEMA_DIR = REPO_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_REGISTRY = FIXTURES_DIR / "agent-registry.json"

# Make tools/validate.py importable as a plain module (tools/ is not a
# package and does not need to be — this test inserts its directory onto
# sys.path directly so import resolution never depends on cwd).
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import validate  # noqa: E402  (import after sys.path manipulation, by design)


def run_validate(*args):
    """Invoke validate.main() in-process, capturing stdout.

    Returns (exit_code, stdout_text).
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = validate.main(list(args))
    return code, buf.getvalue()


class TaskValidationTests(unittest.TestCase):
    def test_valid_task_passes(self):
        target = FIXTURES_DIR / "t-valid-001.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)
        self.assertIn(str(target), out)

    def test_task_missing_required_field_is_rejected(self):
        target = FIXTURES_DIR / "t-invalid-missing-field.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertNotEqual(code, 0)
        self.assertIn("acceptance_criteria", out)

    def test_task_bad_enum_state_is_rejected(self):
        target = FIXTURES_DIR / "t-invalid-bad-state.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertNotEqual(code, 0)
        self.assertIn("state", out)


class HandoffValidationTests(unittest.TestCase):
    def test_valid_handoff_within_write_scope_passes(self):
        target = FIXTURES_DIR / "h-valid-001.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)

    def test_handoff_artifact_outside_write_scope_is_rejected(self):
        target = FIXTURES_DIR / "h-invalid-write-scope.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("WRITE-SCOPE VIOLATION", out)
        self.assertIn("C:/Users/Owner/other-repo/secret.json", out)
        self.assertIn("claude-code", out)

    def test_handoff_unknown_from_agent_is_rejected_as_write_scope_failure(self):
        target = FIXTURES_DIR / "h-invalid-unknown-agent.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("WRITE-SCOPE VIOLATION", out)
        self.assertIn("ghost-agent", out)


class WriteScopeTraversalTests(unittest.TestCase):
    """Finding 1: match_write_scope must canonicalize .. / . segments before
    glob-matching, so a directory-traversal path that merely *starts with*
    an in-scope prefix (but resolves outside it) is correctly rejected,
    while a traversal path that resolves back inside scope is still
    accepted."""

    def test_traversal_path_that_resolves_outside_scope_is_rejected(self):
        target = FIXTURES_DIR / "h-invalid-traversal-escape.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertNotEqual(code, 0, out)
        self.assertIn("WRITE-SCOPE VIOLATION", out)

    def test_traversal_path_that_still_resolves_inside_scope_is_accepted(self):
        target = FIXTURES_DIR / "h-valid-traversal-inside.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)


class RegistryUsageErrorTests(unittest.TestCase):
    """Finding 2: a missing or unreadable --registry is a usage error
    (exit 2), the same class as a missing target file or --schema-dir —
    not a WRITE-SCOPE VIOLATION (exit 1)."""

    def test_nonexistent_registry_path_exits_2(self):
        target = FIXTURES_DIR / "h-valid-001.json"
        missing_registry = FIXTURES_DIR / "does-not-exist-registry.json"
        self.assertFalse(missing_registry.exists())
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(missing_registry),
        )
        self.assertEqual(code, 2, out)
        self.assertIn("USAGE ERROR", out)
        self.assertNotIn("WRITE-SCOPE VIOLATION", out)

    def test_corrupt_registry_file_exits_2(self):
        target = FIXTURES_DIR / "h-valid-001.json"
        corrupt_registry = FIXTURES_DIR / "corrupt-registry.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(corrupt_registry),
        )
        self.assertEqual(code, 2, out)
        self.assertIn("USAGE ERROR", out)
        self.assertNotIn("WRITE-SCOPE VIOLATION", out)


class SuggestedNextOwnerTests(unittest.TestCase):
    """Finding 3: suggested_next_owner, when present, must be checked
    against the agent registry's ids, same as from_agent already is."""

    def test_valid_suggested_next_owner_passes(self):
        target = FIXTURES_DIR / "h-valid-suggested-next-owner.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)

    def test_unknown_suggested_next_owner_is_rejected(self):
        target = FIXTURES_DIR / "h-invalid-suggested-next-owner.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertNotEqual(code, 0, out)
        self.assertIn("suggested_next_owner", out)
        self.assertIn("ghost-agent", out)
        self.assertIn("not a registered agent id", out)


class ProtocolVersionTests(unittest.TestCase):
    def test_unsupported_major_version_is_rejected_distinctly(self):
        target = FIXTURES_DIR / "t-protocol-version-mismatch.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertNotEqual(code, 0)
        self.assertIn("protocol_version mismatch", out)
        self.assertIn("docs/MIGRATIONS.md", out)

    def test_non_dict_top_level_json_is_reported_as_schema_error_not_protocol_version(self):
        # Finding 4: a bare top-level JSON array has no 'protocol_version'
        # field to even read; that's a schema-shape problem ("not an
        # object"), not a protocol_version mismatch, and must be reported
        # with the dedicated, accurately-worded message.
        target = FIXTURES_DIR / "t-invalid-not-an-object.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertNotEqual(code, 0)
        self.assertIn("SCHEMA ERROR: top-level JSON value is not an object", out)
        self.assertNotIn("protocol_version mismatch", out)


class HeartbeatValidationTests(unittest.TestCase):
    def test_valid_heartbeat_passes(self):
        target = FIXTURES_DIR / "heartbeat.json"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertEqual(code, 0, out)
        self.assertIn("VALID:", out)


class EventLogValidationTests(unittest.TestCase):
    def test_ndjson_with_one_bad_line_reports_correct_line_number(self):
        target = FIXTURES_DIR / "events" / "sample-events.ndjson"
        code, out = run_validate(str(target), "--schema-dir", str(SCHEMA_DIR))
        self.assertNotEqual(code, 0)
        # Line 2 (1-based) is the deliberately invalid record.
        self.assertIn("line 2", out)
        self.assertNotIn("line 1:", out)
        self.assertNotIn("line 3:", out)


class SchemaInferenceTests(unittest.TestCase):
    def test_uninferable_filename_without_explicit_schema_is_usage_error(self):
        # A file with no recognizable naming convention and no --schema flag.
        target = FIXTURES_DIR / "t-valid-001.json"
        # Copy under a non-conforming name so we don't disturb the fixture
        # used elsewhere (which deliberately does follow the convention).
        mystery = FIXTURES_DIR / "mystery-file.json"
        mystery.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            code, out = run_validate(str(mystery), "--schema-dir", str(SCHEMA_DIR))
            self.assertEqual(code, 2, out)
        finally:
            mystery.unlink()

    def test_explicit_schema_flag_overrides_inference(self):
        target = FIXTURES_DIR / "t-valid-001.json"
        code, out = run_validate(
            str(target), "--schema-dir", str(SCHEMA_DIR), "--schema", "task"
        )
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
