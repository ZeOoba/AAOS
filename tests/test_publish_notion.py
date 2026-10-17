"""
tests/test_publish_notion.py — tests for tools/publish-notion.py.

Written per AAOS TDD doctrine. Mirrors test_publish_contracts.py's style:
the hyphenated module is loaded via _tool_loader.load_tool, and anything
touching the network (urllib.request.urlopen) is monkeypatched rather than
hitting a real Notion API from a unit test.

build_notion_page_payload() is pure (no network, no clock read -- now_iso
is a parameter) and is tested directly with no mocking at all. publish()
is tested three ways: --dry-run (no token needed, no network call made),
a mocked successful POST, and a mocked HTTPError/URLError to confirm a
failed publish raises PublishError (exit 1 at the CLI layer) rather than
being swallowed -- Notion is Derived per ADR-0001, but a publish failure
must still be loud, never silent.

Run with:  python -m unittest tests.test_publish_notion -v
       or: python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tool_loader import load_tool  # noqa: E402

MODULE_FILENAME = "publish-notion.py"

SAMPLE_PAYLOAD = {
    "generation": {
        "generated_at": "2026-07-26T12:00:00+00:00",
        "sources": {},
        "stale": False,
        "skipped": [],
    },
    "rows": [
        {
            "task_id": "t-noi-crosscheck",
            "title": "Wire NOI cross-check into gate",
            "state": "in-progress",
            "owner_agent": "claude-code",
            "priority": 1,
            "lease": None,
            "last_event": None,
            "heartbeat_status": "fresh",
        },
    ],
}


class BuildPayloadTests(unittest.TestCase):
    """Pure classifier tests -- no network, no filesystem, no mocking."""

    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)

    def test_title_includes_project_and_timestamp(self):
        body = self.module.build_notion_page_payload(
            SAMPLE_PAYLOAD, "ora-ops", "32b6ecd8-9e59-81f2-b329-df3fd0e25062", "2026-07-26T12:00:00+00:00",
        )
        title = body["properties"]["title"]["title"][0]["text"]["content"]
        self.assertIn("ora-ops", title)
        self.assertIn("2026-07-26T12:00:00+00:00", title)

    def test_parent_is_the_given_page_id(self):
        body = self.module.build_notion_page_payload(SAMPLE_PAYLOAD, "ora-ops", "abc123", "2026-07-26T00:00:00")
        self.assertEqual(body["parent"], {"page_id": "abc123"})

    def test_table_has_header_row_plus_one_row_per_task(self):
        body = self.module.build_notion_page_payload(SAMPLE_PAYLOAD, "ora-ops", "abc123", "now")
        table_block = next(c for c in body["children"] if c["type"] == "table")
        self.assertTrue(table_block["table"]["has_column_header"])
        self.assertEqual(table_block["table"]["table_width"], 6)
        # header row + 1 data row
        self.assertEqual(len(table_block["table"]["children"]), 2)
        header_cells = table_block["table"]["children"][0]["table_row"]["cells"]
        self.assertEqual(header_cells[0][0]["text"]["content"], "Task ID")
        data_cells = table_block["table"]["children"][1]["table_row"]["cells"]
        self.assertEqual(data_cells[0][0]["text"]["content"], "t-noi-crosscheck")

    def test_empty_rows_still_produces_a_valid_header_only_table(self):
        empty = {"generation": SAMPLE_PAYLOAD["generation"], "rows": []}
        body = self.module.build_notion_page_payload(empty, "ora-ops", "abc123", "now")
        table_block = next(c for c in body["children"] if c["type"] == "table")
        self.assertEqual(len(table_block["table"]["children"]), 1)  # header row only

    def test_stale_flag_surfaces_in_summary_paragraph(self):
        stale_payload = {
            "generation": {**SAMPLE_PAYLOAD["generation"], "stale": True},
            "rows": [],
        }
        body = self.module.build_notion_page_payload(stale_payload, "ora-ops", "abc123", "now")
        paragraphs = [c for c in body["children"] if c["type"] == "paragraph"]
        joined = " ".join(p["paragraph"]["rich_text"][0]["text"]["content"] for p in paragraphs)
        self.assertIn("STALE", joined)

    def test_missing_generation_key_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.build_notion_page_payload({"rows": []}, "ora-ops", "abc123", "now")

    def test_missing_rows_key_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.build_notion_page_payload({"generation": {}}, "ora-ops", "abc123", "now")


class NormalizePageIdTests(unittest.TestCase):
    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)

    def test_bare_32_hex_is_dashed(self):
        got = self.module._normalize_page_id("32b6ecd89e5981f2b329df3fd0e25062")
        self.assertEqual(got, "32b6ecd8-9e59-81f2-b329-df3fd0e25062")

    def test_already_dashed_round_trips(self):
        got = self.module._normalize_page_id("32b6ecd8-9e59-81f2-b329-df3fd0e25062")
        self.assertEqual(got, "32b6ecd8-9e59-81f2-b329-df3fd0e25062")

    def test_wrong_length_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module._normalize_page_id("not-a-page-id")


class PublishTests(unittest.TestCase):
    """publish() tests: dry-run, mocked success, mocked failure."""

    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)
        self.tmpdir = Path(tempfile.mkdtemp())
        self.payload_path = self.tmpdir / "notion-projection.json"
        self.payload_path.write_text(json.dumps(SAMPLE_PAYLOAD), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_returns_body_without_network_or_token(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            body = self.module.publish(
                self.payload_path, project="ora-ops", parent_page_id="abc123",
                token=None, dry_run=True, now_iso="2026-07-26T00:00:00",
            )
        mock_urlopen.assert_not_called()
        self.assertEqual(body["parent"], {"page_id": "abc123"})

    def test_missing_payload_file_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.publish(
                self.tmpdir / "does-not-exist.json", project="ora-ops",
                parent_page_id="abc123", token=None, dry_run=True, now_iso="now",
            )

    def test_malformed_json_raises_usage_error(self):
        bad = self.tmpdir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.publish(
                bad, project="ora-ops", parent_page_id="abc123",
                token=None, dry_run=True, now_iso="now",
            )

    def test_missing_token_without_dry_run_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.publish(
                self.payload_path, project="ora-ops", parent_page_id="abc123",
                token=None, dry_run=False, now_iso="now",
            )

    def test_successful_post_sends_expected_request(self):
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return b"{}"

        def _fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            body = self.module.publish(
                self.payload_path, project="ora-ops", parent_page_id="abc123",
                token="ntn_faketoken", dry_run=False, now_iso="2026-07-26T00:00:00",
            )

        self.assertEqual(captured["url"], "https://api.notion.com/v1/pages")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"]["authorization"], "Bearer ntn_faketoken")
        self.assertEqual(captured["headers"]["notion-version"], "2022-06-28")
        self.assertEqual(captured["body"]["parent"], {"page_id": "abc123"})

    def test_http_error_raises_publish_error(self):
        def _raise_http_error(request, timeout=None):
            raise urllib.error.HTTPError(
                url="https://api.notion.com/v1/pages", code=401,
                msg="Unauthorized", hdrs=None, fp=io.BytesIO(b'{"message": "bad token"}'),
            )

        with patch("urllib.request.urlopen", side_effect=_raise_http_error):
            with self.assertRaises(self.module.PublishError):
                self.module.publish(
                    self.payload_path, project="ora-ops", parent_page_id="abc123",
                    token="ntn_badtoken", dry_run=False, now_iso="now",
                )

    def test_url_error_raises_publish_error(self):
        def _raise_url_error(request, timeout=None):
            raise urllib.error.URLError("network unreachable")

        with patch("urllib.request.urlopen", side_effect=_raise_url_error):
            with self.assertRaises(self.module.PublishError):
                self.module.publish(
                    self.payload_path, project="ora-ops", parent_page_id="abc123",
                    token="ntn_sometoken", dry_run=False, now_iso="now",
                )


class MainCliTests(unittest.TestCase):
    """CLI-layer exit-code tests."""

    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)
        self.tmpdir = Path(tempfile.mkdtemp())
        self.payload_path = self.tmpdir / "notion-projection.json"
        self.payload_path.write_text(json.dumps(SAMPLE_PAYLOAD), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_exits_zero_and_prints_body(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = self.module.main(["--in", str(self.payload_path), "--dry-run", "--now", "2026-07-26T00:00:00+00:00"])
        self.assertEqual(code, 0)
        printed = json.loads(buf.getvalue())
        self.assertIn("parent", printed)
        self.assertIn("2026-07-26T00:00:00+00:00", json.dumps(printed))

    def test_naive_or_garbage_now_exits_2_before_any_network_call(self):
        """Audit review finding: publish-notion is the egress tool, and --now becomes the page title
        timestamp. Like every other CLI it must reject a naive or unparseable value (exit 2), and it
        must do so BEFORE building a request or touching the network."""
        for bad in ("2026-07-26T00:00:00", "garbage", "2026-07-26"):
            with self.subTest(now=bad):
                buf = io.StringIO()
                with patch("urllib.request.urlopen") as fake_urlopen, patch("sys.stderr", buf):
                    with patch.dict("os.environ", {"NOTION_TOKEN": "ntn_x"}):
                        code = self.module.main(["--in", str(self.payload_path), "--now", bad])
                self.assertEqual(code, 2)
                self.assertIn("USAGE ERROR", buf.getvalue())
                self.assertIn("--now", buf.getvalue())
                fake_urlopen.assert_not_called()

    def test_missing_payload_exits_2(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = self.module.main(
                ["--in", str(self.tmpdir / "nope.json"), "--dry-run"]
            )
        self.assertEqual(code, 2)
        self.assertIn("USAGE ERROR", buf.getvalue())

    def test_bad_parent_page_id_exits_2(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = self.module.main(
                ["--in", str(self.payload_path), "--dry-run", "--parent-page-id", "nope"]
            )
        self.assertEqual(code, 2)

    def test_no_token_no_dry_run_exits_2(self):
        buf = io.StringIO()
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("NOTION_TOKEN", None)
            with patch("sys.stderr", buf):
                code = self.module.main(["--in", str(self.payload_path)])
        self.assertEqual(code, 2)

    def test_publish_failure_exits_1(self):
        def _raise_http_error(request, timeout=None):
            raise urllib.error.HTTPError(
                url="https://api.notion.com/v1/pages", code=500,
                msg="Server Error", hdrs=None, fp=io.BytesIO(b"{}"),
            )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=_raise_http_error):
            with patch.dict("os.environ", {"NOTION_TOKEN": "ntn_x"}):
                with patch("sys.stderr", buf):
                    code = self.module.main(["--in", str(self.payload_path)])
        self.assertEqual(code, 1)
        self.assertIn("PUBLISH FAILED", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
