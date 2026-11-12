"""
tests/test_publish_notion.py — tests for tools/publish-notion.py.

Written per AAOS TDD doctrine. The hyphenated module is loaded via _tool_loader.load_tool, and
anything touching the network (urllib.request.urlopen) is monkeypatched: no test makes a network
call.

Fail-closed contract (docs/adr/0007-freeze.md, addendum 2026-11-12):
  * dry-run is the default; a live POST needs --live (live=True), AAOS_NOTION_TOKEN, AAOS_HASH_KEY
    and, while frozen, AAOS_UNFREEZE=1. publish() reads the token and key from the environment
    itself; there is no token parameter, so a caller cannot hand it the generic NOTION_TOKEN.
  * task_id and title are always HMAC-SHA256'd with AAOS_HASH_KEY (an ephemeral random key on a
    dry run without one). owner_agent passes verbatim only if it is a registered agent id or
    "unassigned"; state / heartbeat_status / priority only if they are schema values; anything else
    is HMAC'd too. project must match PROJECT_ID_RE; generated_at and now_iso must be ISO-8601.

All fixtures are synthetic.

Run with:  python -m unittest tests.test_publish_notion -v
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
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

TEST_KEY = "synthetic-test-hash-key-0123456789"
NOW = "2026-07-26T12:00:00+00:00"
PARENT = "32b6ecd8-9e59-81f2-b329-df3fd0e25062"
SCRUB = ("AAOS_UNFREEZE", "AAOS_NOTION_TOKEN", "NOTION_TOKEN", "AAOS_HASH_KEY", "AAOS_SEAM_ROOT")
LIVE_ENV = {"AAOS_UNFREEZE": "1", "AAOS_NOTION_TOKEN": "ntn_aaos", "AAOS_HASH_KEY": TEST_KEY}
AGENTS = frozenset({"claude-code", "cowork"})


def _h(text: str, key: str = TEST_KEY) -> str:
    return "hmac:" + hmac.new(key.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


SAMPLE_PAYLOAD = {
    "generation": {
        "generated_at": "2026-07-26T12:00:00+00:00",
        "sources": {},
        "stale": False,
        "skipped": [],
    },
    "rows": [
        {
            "task_id": "t-synthetic-parcel-alpha",
            "title": "Synthetic Parcel Alpha LOI follow-up",
            "state": "in-progress",
            "owner_agent": "claude-code",
            "priority": 1,
            "lease": None,
            "last_event": None,
            "heartbeat_status": "fresh",
        },
    ],
}


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


class _EnvCase(unittest.TestCase):
    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)
        env = patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in SCRUB}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        net = patch("urllib.request.urlopen")
        self.urlopen = net.start()
        self.addCleanup(net.stop)
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.payload_path = self.tmpdir / "notion-projection.json"
        self.payload_path.write_text(json.dumps(SAMPLE_PAYLOAD), encoding="utf-8")

    def _build(self, payload=SAMPLE_PAYLOAD, project="ora-ops", now=NOW):
        return self.module.build_notion_page_payload(
            payload, project, "abc123", now, hash_key=TEST_KEY.encode("utf-8"), known_agents=AGENTS)

    def _cells(self, body, row=1):
        table = next(c for c in body["children"] if c["type"] == "table")
        return [c[0]["text"]["content"] for c in table["table"]["children"][row]["table_row"]["cells"]]


class BuildPayloadTests(_EnvCase):
    """Pure builder -- no network, no filesystem."""

    def test_title_includes_project_and_timestamp(self):
        body = self._build()
        title = body["properties"]["title"]["title"][0]["text"]["content"]
        self.assertIn("ora-ops", title)
        self.assertIn(NOW, title)

    def test_parent_is_the_given_page_id(self):
        self.assertEqual(self._build()["parent"], {"page_id": "abc123"})

    def test_table_has_header_row_plus_one_row_per_task(self):
        body = self._build()
        table_block = next(c for c in body["children"] if c["type"] == "table")
        self.assertTrue(table_block["table"]["has_column_header"])
        self.assertEqual(table_block["table"]["table_width"], 6)
        self.assertEqual(len(table_block["table"]["children"]), 2)
        self.assertEqual(self._cells(body, 0)[0], "Task ID (hmac)")
        self.assertEqual(self._cells(body)[0], _h("t-synthetic-parcel-alpha"))

    def test_free_text_cells_are_keyed_hashes_never_verbatim(self):
        body = self._build()
        blob = json.dumps(body)
        self.assertNotIn("Synthetic Parcel Alpha", blob)
        self.assertNotIn("synthetic-parcel-alpha", blob)
        cells = self._cells(body)
        self.assertEqual(cells[1], _h("Synthetic Parcel Alpha LOI follow-up"))
        # An unkeyed sha256 of the title (guessable by dictionary) must not appear.
        self.assertNotIn(hashlib.sha256(b"Synthetic Parcel Alpha LOI follow-up").hexdigest()[:12], blob)
        self.assertEqual(cells[2:], ["in-progress", "claude-code", "1", "fresh"])

    def test_hash_depends_on_key(self):
        a = self._cells(self._build())[1]
        b = self._cells(self.module.build_notion_page_payload(
            SAMPLE_PAYLOAD, "ora-ops", "abc123", NOW, hash_key=b"another-synthetic-key-0000000000",
            known_agents=AGENTS))[1]
        self.assertNotEqual(a, b)

    def test_unregistered_owner_and_off_schema_values_are_hashed(self):
        """C4: owner_agent is unvalidated free text at create-task; the other columns may be tampered."""
        row = dict(SAMPLE_PAYLOAD["rows"][0], owner_agent="Synthetic Deal Owner", state="Synthetic Deal",
                   heartbeat_status="Synthetic Deal", priority="Synthetic Deal")
        body = self._build({"generation": SAMPLE_PAYLOAD["generation"], "rows": [row]})
        self.assertNotIn("Synthetic Deal", json.dumps(body))
        cells = self._cells(body)
        self.assertEqual(cells[3], _h("Synthetic Deal Owner"))
        self.assertEqual(cells[2], _h("Synthetic Deal"))

    def test_unassigned_owner_passes(self):
        row = dict(SAMPLE_PAYLOAD["rows"][0], owner_agent="unassigned")
        self.assertEqual(self._cells(self._build({"generation": SAMPLE_PAYLOAD["generation"], "rows": [row]}))[3],
                         "unassigned")

    def test_default_known_agents_is_empty_so_owner_is_hashed(self):
        body = self.module.build_notion_page_payload(SAMPLE_PAYLOAD, "ora-ops", "abc123", NOW,
                                                     hash_key=TEST_KEY.encode("utf-8"))
        self.assertEqual(self._cells(body)[3], _h("claude-code"))

    def test_missing_title_is_left_empty_not_hashed(self):
        row = dict(SAMPLE_PAYLOAD["rows"][0], title=None)
        self.assertEqual(self._cells(self._build({"generation": SAMPLE_PAYLOAD["generation"], "rows": [row]}))[1], "")

    def test_bad_project_is_rejected(self):
        for bad in ("Synthetic Deal t-x", "../x", "ORA", ""):
            with self.subTest(project=bad):
                with self.assertRaises(self.module.aaos.UsageError):
                    self._build(project=bad)

    def test_bad_generated_at_is_rejected(self):
        for bad in ("Synthetic Deal", "2026-07-26", 5):
            with self.subTest(generated_at=bad):
                payload = {"generation": dict(SAMPLE_PAYLOAD["generation"], generated_at=bad), "rows": []}
                with self.assertRaises(self.module.aaos.UsageError):
                    self._build(payload)

    def test_bad_now_is_rejected(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self._build(now="Synthetic Deal")

    def test_empty_rows_still_produces_a_valid_header_only_table(self):
        body = self._build({"generation": SAMPLE_PAYLOAD["generation"], "rows": []})
        table_block = next(c for c in body["children"] if c["type"] == "table")
        self.assertEqual(len(table_block["table"]["children"]), 1)

    def test_stale_flag_surfaces_in_summary_paragraph(self):
        body = self._build({"generation": {**SAMPLE_PAYLOAD["generation"], "stale": True}, "rows": []})
        paragraphs = [c for c in body["children"] if c["type"] == "paragraph"]
        joined = " ".join(p["paragraph"]["rich_text"][0]["text"]["content"] for p in paragraphs)
        self.assertIn("STALE", joined)

    def test_missing_generation_key_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self._build({"rows": []})

    def test_missing_rows_key_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self._build({"generation": {}})


class NormalizePageIdTests(unittest.TestCase):
    def setUp(self):
        self.module = load_tool(MODULE_FILENAME)

    def test_bare_32_hex_is_dashed(self):
        self.assertEqual(self.module._normalize_page_id("32b6ecd89e5981f2b329df3fd0e25062"), PARENT)

    def test_already_dashed_round_trips(self):
        self.assertEqual(self.module._normalize_page_id(PARENT), PARENT)

    def test_wrong_length_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module._normalize_page_id("not-a-page-id")


class PublishTests(_EnvCase):
    """publish(): env-only credentials, freeze gate, mocked network."""

    def _publish(self, **kw):
        kw.setdefault("project", "ora-ops")
        kw.setdefault("parent_page_id", "abc123")
        kw.setdefault("now_iso", NOW)
        return self.module.publish(self.payload_path, **kw)

    def test_token_parameter_is_gone(self):
        """C3b: a caller can no longer pass a token (e.g. the generic NOTION_TOKEN) in."""
        with patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(TypeError):
                self._publish(token="ntn_generic", live=True)
        self.urlopen.assert_not_called()

    def test_dry_run_is_default_and_needs_no_env(self):
        body = self._publish()
        self.urlopen.assert_not_called()
        self.assertEqual(body["parent"], {"page_id": "abc123"})
        self.assertNotIn("Synthetic Parcel Alpha", json.dumps(body))

    def test_dry_run_without_key_uses_an_ephemeral_key(self):
        a = self._cells(self._publish())[1]
        b = self._cells(self._publish())[1]
        self.assertTrue(a.startswith("hmac:"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, _h("Synthetic Parcel Alpha LOI follow-up"))

    def test_dry_run_with_key_uses_it(self):
        with patch.dict(os.environ, {"AAOS_HASH_KEY": TEST_KEY}):
            self.assertEqual(self._cells(self._publish())[1], _h("Synthetic Parcel Alpha LOI follow-up"))

    def test_owner_validated_against_the_repo_registry(self):
        self.assertEqual(self._cells(self._publish())[3], "claude-code")

    def test_live_refused_while_frozen(self):
        env = dict(LIVE_ENV)
        env.pop("AAOS_UNFREEZE")
        with patch.dict(os.environ, env):
            with self.assertRaises(self.module.aaos.UsageError) as ctx:
                self._publish(live=True)
        self.assertIn("FROZEN", str(ctx.exception))
        self.urlopen.assert_not_called()

    def test_live_refuses_generic_token_only(self):
        with patch.dict(os.environ, {"AAOS_UNFREEZE": "1", "AAOS_HASH_KEY": TEST_KEY, "NOTION_TOKEN": "ntn_generic"}):
            with self.assertRaises(self.module.aaos.UsageError) as ctx:
                self._publish(live=True)
        self.assertIn("AAOS_NOTION_TOKEN", str(ctx.exception))
        self.urlopen.assert_not_called()

    def test_live_refused_without_hash_key(self):
        for key in (None, "", "short"):
            with self.subTest(key=key):
                env = dict(LIVE_ENV)
                env.pop("AAOS_HASH_KEY")
                if key is not None:
                    env["AAOS_HASH_KEY"] = key
                with patch.dict(os.environ, env):
                    with self.assertRaises(self.module.aaos.UsageError) as ctx:
                        self._publish(live=True)
                self.assertIn("AAOS_HASH_KEY", str(ctx.exception))
                self.urlopen.assert_not_called()

    def test_live_refuses_bad_project(self):
        with patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(self.module.aaos.UsageError):
                self._publish(live=True, project="Synthetic Deal t-x")
        self.urlopen.assert_not_called()

    def test_missing_payload_file_raises_usage_error(self):
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.publish(self.tmpdir / "nope.json", project="ora-ops", parent_page_id="abc123", now_iso=NOW)

    def test_malformed_json_raises_usage_error(self):
        bad = self.tmpdir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(self.module.aaos.UsageError):
            self.module.publish(bad, project="ora-ops", parent_page_id="abc123", now_iso=NOW)

    def test_successful_post_sends_expected_request(self):
        captured = {}

        def _fake(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        self.urlopen.side_effect = _fake
        with patch.dict(os.environ, {**LIVE_ENV, "AAOS_NOTION_TOKEN": " ntn_aaos\n", "NOTION_TOKEN": "ntn_generic"}):
            self._publish(live=True)
        self.assertEqual(captured["url"], "https://api.notion.com/v1/pages")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"]["authorization"], "Bearer ntn_aaos")
        self.assertEqual(captured["headers"]["notion-version"], "2022-06-28")
        self.assertEqual(captured["body"]["parent"], {"page_id": "abc123"})
        self.assertEqual(self._cells(captured["body"])[1], _h("Synthetic Parcel Alpha LOI follow-up"))

    def test_http_error_raises_publish_error(self):
        self.urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.notion.com/v1/pages", code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"message": "bad token"}'))
        with patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(self.module.PublishError):
                self._publish(live=True)

    def test_url_error_raises_publish_error(self):
        self.urlopen.side_effect = urllib.error.URLError("network unreachable")
        with patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(self.module.PublishError):
                self._publish(live=True)


class MainCliTests(_EnvCase):
    """CLI-layer exit codes. Scrubbed env, urlopen mocked."""

    def _main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env or {}), patch("sys.stdout", out), patch("sys.stderr", err):
            code = self.module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_exits_zero_and_prints_body(self):
        code, out, _ = self._main(["--in", str(self.payload_path), "--dry-run", "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn(NOW, json.dumps(json.loads(out)))

    def test_default_is_dry_run_even_with_every_credential_set(self):
        code, out, _ = self._main(["--in", str(self.payload_path), "--now", NOW], {**LIVE_ENV, "NOTION_TOKEN": "x"})
        self.assertEqual(code, 0)
        self.assertIn("parent", json.loads(out))
        self.urlopen.assert_not_called()

    def test_live_refused_while_frozen(self):
        env = dict(LIVE_ENV)
        env.pop("AAOS_UNFREEZE")
        code, _, err = self._main(["--in", str(self.payload_path), "--live"], env)
        self.assertEqual(code, 2)
        self.assertIn("FROZEN", err)
        self.assertIn("0007", err)
        self.urlopen.assert_not_called()

    def test_live_ignores_generic_notion_token(self):
        env = dict(LIVE_ENV, NOTION_TOKEN="ntn_generic")
        env.pop("AAOS_NOTION_TOKEN")
        code, _, err = self._main(["--in", str(self.payload_path), "--live"], env)
        self.assertEqual(code, 2)
        self.assertIn("AAOS_NOTION_TOKEN", err)
        self.urlopen.assert_not_called()

    def test_live_with_all_gates_posts_keyed_hash_payload(self):
        captured = {}

        def _fake(request, timeout=None):
            captured["auth"] = {k.lower(): v for k, v in request.header_items()}["authorization"]
            captured["data"] = request.data.decode("utf-8")
            return _Resp()

        self.urlopen.side_effect = _fake
        code, out, _ = self._main(["--in", str(self.payload_path), "--live", "--now", NOW],
                                  {**LIVE_ENV, "NOTION_TOKEN": "ntn_generic"})
        self.assertEqual(code, 0, out)
        self.assertEqual(captured["auth"], "Bearer ntn_aaos")
        self.assertNotIn("Synthetic Parcel Alpha", captured["data"])
        self.assertIn(_h("Synthetic Parcel Alpha LOI follow-up"), captured["data"])

    def test_bad_project_exits_2(self):
        code, _, err = self._main(["--in", str(self.payload_path), "--project", "Synthetic Deal"])
        self.assertEqual(code, 2)
        self.assertIn("USAGE ERROR", err)

    def test_live_and_dry_run_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--in", str(self.payload_path), "--live", "--dry-run"], LIVE_ENV)
        self.assertEqual(ctx.exception.code, 2)
        self.urlopen.assert_not_called()

    def test_naive_or_garbage_now_exits_2_before_any_network_call(self):
        for bad in ("2026-07-26T00:00:00", "garbage", "2026-07-26"):
            with self.subTest(now=bad):
                code, _, err = self._main(["--in", str(self.payload_path), "--live", "--now", bad], LIVE_ENV)
                self.assertEqual(code, 2)
                self.assertIn("USAGE ERROR", err)
                self.assertIn("--now", err)
                self.urlopen.assert_not_called()

    def test_missing_payload_exits_2(self):
        code, _, err = self._main(["--in", str(self.tmpdir / "nope.json"), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("USAGE ERROR", err)

    def test_bad_parent_page_id_exits_2(self):
        code, _, _ = self._main(["--in", str(self.payload_path), "--dry-run", "--parent-page-id", "nope"])
        self.assertEqual(code, 2)

    def test_live_without_token_exits_2(self):
        env = dict(LIVE_ENV)
        env.pop("AAOS_NOTION_TOKEN")
        code, _, err = self._main(["--in", str(self.payload_path), "--live"], env)
        self.assertEqual(code, 2)
        self.assertIn("AAOS_NOTION_TOKEN", err)
        self.urlopen.assert_not_called()

    def test_publish_failure_exits_1(self):
        self.urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.notion.com/v1/pages", code=500, msg="Server Error", hdrs=None, fp=io.BytesIO(b"{}"))
        code, _, err = self._main(["--in", str(self.payload_path), "--live"], LIVE_ENV)
        self.assertEqual(code, 1)
        self.assertIn("PUBLISH FAILED", err)


if __name__ == "__main__":
    unittest.main()
