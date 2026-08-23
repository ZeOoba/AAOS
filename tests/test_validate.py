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

    def test_handoff_referencing_list_scheduled_tasks_is_rejected(self):
        """2026-08-22: registration/tooling flows are create_scheduled_task-only
        (register-scheduled-task skill doctrine) -- a handoff whose free-text
        fields describe USING list_scheduled_tasks as the registration
        mechanism (no verification-context word alongside it) must fail, not
        just print a WARN, so the violation can never ride along to the
        receiving agent."""
        target = FIXTURES_DIR / "h-invalid-registration-doctrine.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("REGISTRATION-DOCTRINE VIOLATION", out)
        self.assertIn("list_scheduled_tasks", out)

    def test_handoff_not_referencing_list_scheduled_tasks_is_unaffected(self):
        """Control case: the existing valid fixture has none of the offending
        text and must still pass cleanly (no false positive)."""
        target = FIXTURES_DIR / "h-valid-001.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn("REGISTRATION-DOCTRINE VIOLATION", out)

    def test_handoff_verifying_via_list_scheduled_tasks_is_not_flagged(self):
        """register-scheduled-task skill v2.13 makes list_scheduled_tasks a
        MANDATORY verification step ("no 'done' without evidence ... paste
        the returned entry") -- it is never itself prohibited. Only using it
        as the registration mechanism is. A handoff that says it verified via
        list_scheduled_tasks (the doctrine-compliant, expected phrasing) must
        pass cleanly -- a blanket 'flag every mention' check would
        false-positive on exactly this correct usage."""
        target = FIXTURES_DIR / "h-valid-with-verified-list-scheduled-tasks.json"
        code, out = run_validate(
            str(target),
            "--schema-dir", str(SCHEMA_DIR),
            "--registry", str(FIXTURE_REGISTRY),
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn("REGISTRATION-DOCTRINE VIOLATION", out)


class RegistrationDoctrineAdversarialTests(unittest.TestCase):
    """Direct unit tests against check_registration_doctrine(). Round 1
    (2026-08-22) fixed an independent review's M1 (unbounded sentence-
    splitting) and M2 (substring matching) findings on a "flag unless a
    verify word is nearby" design. Round 2 (same day, a second independent
    review via qodo on the opened PR) found that design was ALSO too broad
    in the other direction -- register-scheduled-task's own doctrine text
    legitimately mentions list_scheduled_tasks with no verify word nearby --
    and separately found the needle search itself was case-sensitive. Both
    are fixed by switching to a positive-evidence design: flag only when
    splice/hand-write language is nearby, case-insensitively."""

    def _handoff(self, **overrides):
        base = {
            "protocol_version": "1.0", "id": "h-adv-001", "project_id": "ora-ops",
            "task_ref": "t-adv-001", "from_agent": "claude-code", "to_agent": "cowork",
            "artifact_manifest": [], "context_summary": "", "required_next_action": "",
            "blockers": [], "posted_at": "2026-08-22T00:00:00-04:00",
        }
        base.update(overrides)
        return base

    def test_splice_language_outside_the_window_does_not_flag(self):
        """The window bound still applies to the positive-evidence regex:
        splice language further from the needle than
        _REGISTRATION_DOCTRINE_WINDOW chars must not anchor to it."""
        pad = "x" * (validate._REGISTRATION_DOCTRINE_WINDOW + 20)
        h = self._handoff(context_summary=(
            f"Spliced an unrelated config file earlier. {pad} "
            "Separately, list_scheduled_tasks confirmed the entry is live."
        ))
        errors = validate.check_registration_doctrine(h)
        self.assertEqual(errors, [], "splice language outside the window "
                                     "wrongly anchored to a distant mention")

    def test_splice_language_inside_the_window_flags(self):
        h = self._handoff(context_summary=(
            "Spliced the list_scheduled_tasks output directly into the registry."
        ))
        errors = validate.check_registration_doctrine(h)
        self.assertTrue(errors)

    def test_case_insensitive_needle_bypass_is_closed(self):
        """qodo finding (PR #3, round 2): _registration_doctrine_flagged_
        excerpts() originally used a case-sensitive str.find(), so
        'List_scheduled_tasks'/'LIST_SCHEDULED_TASKS' evaded the check
        entirely regardless of surrounding language."""
        for variant in ("List_scheduled_tasks", "LIST_SCHEDULED_TASKS",
                        "List_Scheduled_Tasks"):
            h = self._handoff(context_summary=(
                f"Splice the entry from {variant} output directly into the registry."
            ))
            errors = validate.check_registration_doctrine(h)
            self.assertTrue(errors, f"case variant {variant!r} bypassed the check")

    def test_doctrine_quoting_prose_without_splice_language_is_not_flagged(self):
        """qodo finding (PR #3, round 2): register-scheduled-task's own
        SKILL.md legitimately mentions list_scheduled_tasks with no
        verification word AND no splice language nearby -- e.g. its surface
        note on which sessions carry the MCP tools. A handoff that quotes
        or relays that doctrine text must not be rejected."""
        h = self._handoff(context_summary=(
            "Per doctrine: create_scheduled_task/update_scheduled_task/"
            "delete_scheduled_task/list_scheduled_tasks exist only in a "
            "session running natively on-device; a cloud-sandbox Cowork "
            "session never carries them."
        ))
        errors = validate.check_registration_doctrine(h)
        self.assertEqual(errors, [])

    def test_genuine_nearby_verify_word_still_passes(self):
        """Control: the doctrine-mandated 'verified via list_scheduled_tasks'
        phrasing (no splice language) must pass cleanly."""
        h = self._handoff(required_next_action=(
            "Verified via list_scheduled_tasks that the entry is live."
        ))
        errors = validate.check_registration_doctrine(h)
        self.assertEqual(errors, [])

    def test_two_bad_mentions_in_one_field_report_separately(self):
        """L2 alignment: multiple offending occurrences in one field must
        each produce their own finding, not collapse into one."""
        h = self._handoff(context_summary=(
            "Spliced via list_scheduled_tasks for the CC fleet. "
            + ("padding " * 30) +
            "Also spliced via list_scheduled_tasks for the LAMS fleet."
        ))
        errors = validate.check_registration_doctrine(h)
        self.assertEqual(len(errors), 2, errors)

    def test_blockers_entries_checked_with_same_window_logic(self):
        h = self._handoff(blockers=[
            "Splice list_scheduled_tasks output into the registry directly.",
            "Verified via list_scheduled_tasks that the fleet is clean.",
        ])
        errors = validate.check_registration_doctrine(h)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("'blockers'", errors[0])


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
