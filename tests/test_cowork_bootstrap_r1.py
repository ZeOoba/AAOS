"""tests/test_cowork_bootstrap_r1.py -- AMD-0003 rule R1 as it appears in docs/COWORK-BOOTSTRAP.md.

R1: a Cowork session MUST NOT run any tool under tools/ that writes the seam, because its file
operations reach the seam through a FUSE bridge whose atomicity for exclusive create, hard links and
rename is unverified (V1 soak pending). The ADR makes a classification test over tools/*.py, not a
hand-copied list, the authority. This module is that test, plus checks that the bootstrap says what
the ADR requires and no longer tells Cowork to run a barred tool.

Adding a tool to tools/ fails test_every_tool_is_classified until it is classified here (and, if it
writes the seam, named in the bootstrap's R1 paragraph).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
BOOTSTRAP = REPO_ROOT / "docs" / "COWORK-BOOTSTRAP.md"

# Tools that create, replace, move or delete a file on the seam (by default or by flag).
SEAM_WRITING = {
    "complete-task", "create-task", "derive-heartbeat", "emit-event", "expire-leases", "fail-task",
    "lease-task", "post-handoff", "project-notion", "publish-contracts",
}
# Tools that write nothing to the seam. (publish-notion sends a projection to Notion; it is not a seam write.)
SEAM_READ_ONLY = {"check-contract-drift", "publish-notion", "validate"}
# Importable library, not a tool.
LIBRARY = {"_aaos_common"}

WRITE_MARKERS = re.compile(
    r"atomic_write|save_json|save_task|save_handoff|commit_state_change|emit_event\(|spool_event|drain_event_spool"
)


def _sections(text: str) -> dict:
    parts = re.split(r"(?m)^## (§\d+)[^\n]*\n", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


class ToolClassification(unittest.TestCase):
    def test_every_tool_is_classified_exactly_once(self):
        found = {p.stem for p in TOOLS.glob("*.py")}
        classified = SEAM_WRITING | SEAM_READ_ONLY | LIBRARY
        self.assertEqual(found, classified, "unclassified or stale tool names; update the R1 classification")
        self.assertFalse(SEAM_WRITING & SEAM_READ_ONLY)

    def test_classification_matches_what_the_code_does(self):
        for name in sorted(SEAM_WRITING):
            with self.subTest(tool=name, expected="writes"):
                src = (TOOLS / f"{name}.py").read_text(encoding="utf-8")
                self.assertRegex(src, WRITE_MARKERS, f"{name} is classified seam-writing but shows no write call")
        for name in sorted(SEAM_READ_ONLY):
            with self.subTest(tool=name, expected="read-only"):
                src = (TOOLS / f"{name}.py").read_text(encoding="utf-8")
                self.assertNotRegex(src, WRITE_MARKERS, f"{name} is classified read-only but calls a seam writer")


class BootstrapStatesR1(unittest.TestCase):
    def setUp(self):
        self.text = BOOTSTRAP.read_text(encoding="utf-8")
        self.sec = _sections(self.text)

    def test_section_6_states_rule_R1_and_names_every_barred_tool(self):
        s6 = self.sec["§6"]
        self.assertIn("Rule R1", s6)
        for name in sorted(SEAM_WRITING):
            with self.subTest(tool=name):
                self.assertIn(name, s6)
        self.assertIn("never lift it yourself", s6)

    def test_no_command_line_instructs_a_barred_tool(self):
        barred = "|".join(re.escape(n) for n in sorted(SEAM_WRITING))
        pattern = re.compile(r"(?m)^\s*python\s+\S*tools/(" + barred + r")\.py")
        self.assertEqual(pattern.findall(self.text), [], "the bootstrap still instructs a seam-writing tool")

    def test_the_full_parity_claim_is_gone(self):
        self.assertNotIn("Full parity with Code", self.text)

    def test_sections_7_and_8_always_apply(self):
        self.assertNotIn("do not apply to you in this mode", self.text)

    def test_section_8_forbids_barred_tools_even_in_full_exec(self):
        self.assertRegex(self.sec["§8"], r"even when `MODE = FULL-EXEC`")

    def test_section_9_report_states_no_barred_tool_was_run(self):
        self.assertIn("seam-writing tool", self.sec["§9"])

    def test_cowork_seam_path_is_documented(self):
        self.assertIn("$HOME/mnt/AAOS", self.text)


if __name__ == "__main__":
    unittest.main()
