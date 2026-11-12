"""
tests/test_freeze_guard.py -- the in-code freeze sentinel (docs/adr/0007-freeze.md, addendum
2026-11-12; audit findings M-no-in-code-freeze-guard and review items C1a/C1b/C1c/C2/C5).

While AAOS is frozen, any path that is, or lies under, the live seam (LIVE_SEAM_ROOT) is refused
unless AAOS_UNFREEZE=1 -- whether it arrives as a seam root (default fallback, AAOS_SEAM_ROOT,
--seam-root) or as a file path a CLI reads or writes (--out, --in, --signals, --summary-file,
--artifact, --registry, --schema-dir, validate.py's positional path). Spelling tricks (case,
separators, "..", trailing dot/space, NTFS stream suffixes, \\\\?\\ / \\\\.\\ / UNC admin-share
prefixes, 8.3 short names, junctions/symlinks) are refused too. Roots that are not the live seam
(the tmp seams the whole suite uses) keep working.

SAFETY: every test here points BOTH LIVE_SEAM_ROOT and DEFAULT_SEAM_ROOT at a decoy folder inside a
tmp dir. No test ever passes the real live seam path to the guard or a CLI, so even a broken guard
cannot make a test run touch the live seam.
"""
from __future__ import annotations

import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

B = "\\"
SCRUB = ("AAOS_UNFREEZE", "AAOS_SEAM_ROOT", "AAOS_NOTION_TOKEN", "NOTION_TOKEN", "AAOS_HASH_KEY")


def _env_without(*names):
    return {k: v for k, v in os.environ.items() if k not in names}


def _short_path(p: str) -> str | None:
    if os.name != "nt":
        return None
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value if n and buf.value.lower() != str(p).lower() else None


def _make_dir_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return proc.returncode == 0 and link.exists()
    return False


class _DecoyCase(unittest.TestCase):
    """tmp dir with a decoy live seam (a folder whose name has a space, so it has an 8.3 alias)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aaos-freeze-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.decoy = self.tmp / "decoy Long Name Dir" / "AAOS"
        (self.decoy / "projects" / "ora-ops" / "tasks").mkdir(parents=True)
        self.other = self.tmp / "other-seam"
        (self.other / "projects" / "ora-ops" / "tasks").mkdir(parents=True)
        for p in (
            mock.patch.dict(os.environ, _env_without(*SCRUB), clear=True),
            mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(self.decoy)),
            mock.patch.object(aaos, "LIVE_SEAM_ROOT", str(self.decoy)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _snapshot(self):
        return sorted(str(p.relative_to(self.decoy)) for p in self.decoy.rglob("*"))


class FreezeGuardTests(_DecoyCase):
    def _refused(self, fn):
        with self.assertRaises(aaos.UsageError) as ctx:
            fn()
        msg = str(ctx.exception)
        self.assertIn("FROZEN", msg)
        self.assertIn("0007", msg)
        self.assertIn("AAOS_UNFREEZE", msg)

    def test_live_constant_is_the_hard_coded_onedrive_seam(self):
        # String-only check against the unpatched module source value.
        import importlib
        src = Path(aaos.__file__).read_text(encoding="utf-8")
        self.assertIn("LIVE_SEAM_ROOT = DEFAULT_SEAM_ROOT", src)
        self.assertIn('Claude_Workspace/AAOS"', src)
        del importlib

    def test_fallback_to_live_default_is_refused(self):
        self._refused(lambda: aaos.seam_root())

    def test_require_seam_reachable_refuses_live_default(self):
        self._refused(lambda: aaos.require_seam_reachable())

    def test_explicit_override_equal_to_live_is_refused(self):
        self._refused(lambda: aaos.seam_root(str(self.decoy)))

    def test_env_var_pointing_at_live_is_refused(self):
        with mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.decoy)}):
            self._refused(lambda: aaos.seam_root())

    def test_child_paths_of_live_are_refused(self):
        """C1c: containment, not equality."""
        for p in (self.decoy / "projects", self.decoy / "projects" / "ora-ops" / "heartbeat.json",
                  self.decoy / "no-such-dir" / "x.json"):
            with self.subTest(p=str(p)):
                self._refused(lambda p=p: aaos.seam_root(str(p)))
                self._refused(lambda p=p: aaos.refuse_frozen_live_path(str(p), "--out"))

    def test_sibling_with_shared_prefix_is_allowed(self):
        sib = Path(str(self.decoy) + "-copy")
        sib.mkdir()
        self.assertEqual(aaos.seam_root(str(sib)), sib)

    def test_spelling_variants_of_live_are_refused(self):
        d = str(self.decoy)
        variants = [d + B, d + "/", d + B + "projects" + B + "..", d.replace(B, "/")]
        if os.name == "nt":
            variants += [d.upper(), d + ".", d + " ", d + "::$INDEX_ALLOCATION"]
        for v in variants:
            with self.subTest(variant=v):
                self._refused(lambda v=v: aaos.seam_root(v))

    @unittest.skipUnless(os.name == "nt", "Windows path prefixes")
    def test_device_and_unc_prefixes_are_refused(self):
        """C1b: \\\\?\\, \\\\.\\, \\\\?\\UNC\\ and admin-share UNC spellings of the live seam."""
        d = str(self.decoy)
        drive, rest = d[0], d[2:]
        variants = [
            B + B + "?" + B + d,
            B + B + "." + B + d,
            B + B + "localhost" + B + drive + "$" + rest,
            B + B + "127.0.0.1" + B + drive + "$" + rest,
            B + B + "?" + B + "UNC" + B + "localhost" + B + drive + "$" + rest,
            "//?/" + d.replace(B, "/"),
            B + B + "?" + B + d + B + "projects",
        ]
        for v in variants:
            with self.subTest(variant=v):
                self._refused(lambda v=v: aaos.seam_root(v))

    def test_short_name_alias_is_refused(self):
        short = _short_path(str(self.decoy))
        if not short:
            self.skipTest("no 8.3 short name available on this host")
        self._refused(lambda: aaos.seam_root(short))

    def test_junction_or_symlink_alias_is_refused(self):
        link = self.tmp / "alias"
        if not _make_dir_link(link, self.decoy):
            self.skipTest("cannot create a symlink or junction on this host")
        self.addCleanup(lambda: os.rmdir(str(link)) if link.exists() else None)
        self._refused(lambda: aaos.seam_root(str(link)))
        self._refused(lambda: aaos.refuse_frozen_live_path(str(link / "heartbeat.json"), "--out"))

    def test_path_builders_inherit_the_refusal(self):
        self._refused(lambda: aaos.project_dir("ora-ops"))
        self._refused(lambda: aaos.heartbeat_path())

    def test_unfreeze_sentinel_allows_live(self):
        with mock.patch.dict(os.environ, {"AAOS_UNFREEZE": "1"}):
            self.assertEqual(aaos.seam_root(), Path(str(self.decoy)))
            aaos.refuse_frozen_live_path(str(self.decoy / "heartbeat.json"), "--out")

    def test_unfreeze_must_be_exactly_1(self):
        for val in ("", "0", "true", "yes", " 1"):
            with self.subTest(val=val):
                with mock.patch.dict(os.environ, {"AAOS_UNFREEZE": val}):
                    self._refused(lambda: aaos.seam_root())

    def test_explicit_tmp_override_is_allowed(self):
        self.assertEqual(aaos.seam_root(str(self.other)), Path(str(self.other)))
        self.assertEqual(aaos.require_seam_reachable(str(self.other)), Path(str(self.other)))
        aaos.refuse_frozen_live_path(str(self.other / "heartbeat.json"), "--out")

    def test_env_override_elsewhere_is_allowed(self):
        with mock.patch.dict(os.environ, {"AAOS_SEAM_ROOT": str(self.other)}):
            self.assertEqual(aaos.seam_root(), Path(str(self.other)))
            self.assertEqual(aaos.require_seam_reachable(), Path(str(self.other)))

    def test_patched_default_sentinel_is_not_mistaken_for_live(self):
        """Other suites patch DEFAULT_SEAM_ROOT alone to an in-tmp sentinel; that must not be refused."""
        sentinel = self.tmp / "sentinel"
        with mock.patch.object(aaos, "DEFAULT_SEAM_ROOT", str(sentinel)):
            self.assertEqual(aaos.seam_root(), Path(str(sentinel)))

    def test_nothing_was_written_under_the_decoy(self):
        before = self._snapshot()
        for fn in (aaos.seam_root, aaos.require_seam_reachable):
            with self.assertRaises(aaos.UsageError):
                fn()
        self.assertEqual(self._snapshot(), before)


class FreezeGuardCliTests(_DecoyCase):
    """Every seam CLI, and every file-path argument, fails closed (exit 2, FROZEN in its output)
    when it would touch the live seam. In-process, decoy-patched: a regression can at worst touch
    the decoy, and the decoy's contents are asserted unchanged."""

    def setUp(self):
        super().setUp()
        self.signals = self.tmp / "signals.json"
        self.signals.write_text('{"agents": []}', encoding="utf-8")
        # Seed files INSIDE the decoy so "refused" is about the freeze, not a missing file.
        self.decoy_file = self.decoy / "projects" / "ora-ops" / "notion-projection.json"
        self.decoy_file.write_text('{"generation": {}, "rows": []}', encoding="utf-8")
        self.decoy_task = self.decoy / "projects" / "ora-ops" / "tasks" / "t-synthetic.json"
        self.decoy_task.write_text("{}", encoding="utf-8")
        self.decoy_sig = self.decoy / "signals.json"
        self.decoy_sig.write_text('{"agents": []}', encoding="utf-8")
        self.decoy_txt = self.decoy / "summary.txt"
        self.decoy_txt.write_text("synthetic", encoding="utf-8")
        self.baseline = self._snapshot()

    def _run(self, name, *argv):
        module = _tool_loader.load_tool(name)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out), mock.patch("urllib.request.urlopen") as net:
            code = module.main(list(argv))
        net.assert_not_called()
        return code, out.getvalue() + err.getvalue()

    def _assert_refused(self, name, *argv):
        with self.subTest(tool=name, argv=argv[-2:]):
            code, output = self._run(name, *argv)
            self.assertEqual(code, 2, output)
            self.assertIn("FROZEN", output)
            self.assertEqual(self._snapshot(), self.baseline, "nothing may be written under the live seam")

    NOW = ("--now", "2026-11-12T00:00:00+00:00")

    def test_all_twelve_seam_clis_refuse_the_live_default(self):
        lease = ("t-synthetic", "--holder", "claude-code", "--session", "s-1")
        cases = [
            ("create-task.py", "--id", "t-synthetic-freeze", "--title", "synthetic", "--accept", "x"),
            ("lease-task.py", *lease),
            ("complete-task.py", *lease),
            ("fail-task.py", *lease, "--reason", "synthetic"),
            ("post-handoff.py", "--id", "h-synthetic", "--task", "t-synthetic", "--from", "claude-code",
             "--to", "cowork", "--next-action", "x", "--summary", "x"),
            ("emit-event.py", "--type", "TaskCreated", "--actor", "claude-code", "--task", "t-synthetic"),
            ("expire-leases.py", "--dry-run"),
            ("derive-heartbeat.py", "--signals", str(self.signals), *self.NOW),
            ("project-notion.py", *self.NOW),
            ("check-contract-drift.py",),
            ("publish-contracts.py",),
            ("publish-notion.py",),
        ]
        self.assertEqual(len(cases), 12)
        for name, *argv in cases:
            self._assert_refused(name, *argv)

    def test_seam_root_child_and_prefixed_routes(self):
        routes = [str(self.decoy / "projects")]
        if os.name == "nt":
            routes += [B + B + "?" + B + str(self.decoy),
                       B + B + "localhost" + B + str(self.decoy)[0] + "$" + str(self.decoy)[2:]]
        for r in routes:
            self._assert_refused("create-task.py", "--id", "t-synthetic-freeze", "--title", "synthetic",
                                 "--accept", "x", "--seam-root", r)

    def test_out_paths_into_live_are_refused(self):
        """C1a: --out bypassed the guard in derive-heartbeat and project-notion."""
        self._assert_refused("derive-heartbeat.py", "--signals", str(self.signals), *self.NOW,
                             "--seam-root", str(self.other), "--out", str(self.decoy / "heartbeat.json"))
        self._assert_refused("project-notion.py", *self.NOW, "--seam-root", str(self.other),
                             "--out", str(self.decoy / "projects" / "ora-ops" / "out.json"))

    def test_input_paths_from_live_are_refused(self):
        self._assert_refused("derive-heartbeat.py", "--signals", str(self.decoy_sig), *self.NOW,
                             "--out", str(self.other / "heartbeat.json"))
        self._assert_refused("publish-notion.py", "--in", str(self.decoy_file), *self.NOW)
        self._assert_refused("validate.py", str(self.decoy_task))
        self._assert_refused("validate.py", str(self.other / "t-x.json"), "--schema", "task",
                             "--schema-dir", str(self.decoy))
        self._assert_refused("validate.py", str(self.other / "t-x.json"), "--schema", "task",
                             "--registry", str(self.decoy_txt))
        base = ("--id", "h-synthetic", "--task", "t-synthetic", "--from", "claude-code", "--to", "cowork",
                "--next-action", "x", "--seam-root", str(self.other))
        self._assert_refused("post-handoff.py", *base, "--summary-file", str(self.decoy_txt))
        self._assert_refused("post-handoff.py", *base, "--summary", "x", "--artifact", f"path={self.decoy_txt}")
        self._assert_refused("post-handoff.py", *base, "--summary", "x", "--registry", str(self.decoy_txt))

    def test_non_live_paths_still_work(self):
        out = self.other / "heartbeat.json"
        code, output = self._run("derive-heartbeat.py", "--signals", str(self.signals), *self.NOW,
                                 "--out", str(out))
        self.assertEqual(code, 0, output)
        self.assertTrue(out.exists())


class FrozenErrorPropagationTests(_DecoyCase):
    """Closing review F3: no handler may swallow FrozenSeamError (a UsageError subclass) and turn a
    freeze refusal into "unreadable"/"gone"/"best-effort" behaviour. One test per site."""

    def _frozen(self, *a, **k):
        raise aaos.FrozenSeamError("AAOS is FROZEN (docs/adr/0007-freeze.md): synthetic refusal")

    def test_load_heartbeat_reraises(self):
        hb = self.other / "heartbeat.json"
        hb.write_text("{}", encoding="utf-8")
        with mock.patch.object(aaos, "load_json", side_effect=self._frozen):
            with self.assertRaises(aaos.FrozenSeamError):
                aaos.load_heartbeat(root=str(self.other))

    def test_load_heartbeat_still_tolerates_corrupt(self):
        hb = self.other / "heartbeat.json"
        hb.write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(aaos.load_heartbeat(root=str(self.other)))

    def test_archive_lock_if_unchanged_reraises_on_first_read(self):
        lock = self.other / "projects" / "ora-ops" / "locks" / "t-synthetic.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("{}", encoding="utf-8")
        with mock.patch.object(aaos, "load_json", side_effect=self._frozen):
            with self.assertRaises(aaos.FrozenSeamError):
                aaos.archive_lock_if_unchanged(lock, {})
        self.assertTrue(lock.exists())

    def test_lock_restore_failed_escalation_reraises(self):
        locks = self.other / "projects" / "ora-ops" / "locks"
        (locks / "_archive").mkdir(parents=True)
        archived = locks / "_archive" / "t-synthetic.x.lock"
        archived.write_text("{}", encoding="utf-8")
        lock = locks / "t-synthetic.lock"
        err = io.StringIO()
        with mock.patch.object(aaos, "g_link", side_effect=OSError("synthetic")), \
             mock.patch.object(aaos, "g_rename", side_effect=OSError("synthetic")), \
             mock.patch.object(aaos, "emit_event", side_effect=self._frozen), redirect_stderr(err):
            with self.assertRaises(aaos.FrozenSeamError):
                aaos._restore_archived_lock(archived, lock)

    def test_lock_restore_failed_escalation_other_errors_stay_best_effort(self):
        locks = self.other / "projects" / "ora-ops" / "locks"
        (locks / "_archive").mkdir(parents=True)
        archived = locks / "_archive" / "t-synthetic.x.lock"
        archived.write_text("{}", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(aaos, "g_link", side_effect=OSError("synthetic")), \
             mock.patch.object(aaos, "g_rename", side_effect=OSError("synthetic")), \
             mock.patch.object(aaos, "emit_event", side_effect=RuntimeError("synthetic")), redirect_stderr(err):
            self.assertFalse(aaos._restore_archived_lock(archived, locks / "t-synthetic.lock"))

    def test_derive_heartbeat_registry_load_reraises(self):
        module = _tool_loader.load_tool("derive-heartbeat.py")
        sig = self.tmp / "signals.json"
        sig.write_text('{"agents": []}', encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(aaos, "load_registry", side_effect=self._frozen), \
             redirect_stdout(out), redirect_stderr(err):
            code = module.main(["--signals", str(sig), "--out", str(self.other / "hb.json"),
                                "--now", "2026-11-12T00:00:00+00:00"])
        self.assertEqual(code, 2)
        self.assertIn("FROZEN", err.getvalue())  # via freeze_exit, not the generic registry message
        self.assertNotIn("could not load agent registry", out.getvalue() + err.getvalue())

    def test_publish_contracts_seam_root_reraises(self):
        module = _tool_loader.load_tool("publish-contracts.py")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(["--seam-root", str(self.decoy), "--source-commit", "0" * 40])
        self.assertEqual(code, 2)
        self.assertIn("FROZEN", err.getvalue())
        self.assertNotIn("bad --seam-root", err.getvalue())


class GuardHardeningTests(_DecoyCase):
    """Closing review F1: (a) compare against the live root's resolved spelling too, (b) use
    os.fspath, (c) fail closed when resolution raises."""

    def test_a_live_root_given_through_an_alias_is_matched_by_its_real_spelling(self):
        real = self.tmp / "real-root"
        (real / "AAOS" / "projects").mkdir(parents=True)
        alias = self.tmp / "alias-root"
        if not _make_dir_link(alias, real):
            self.skipTest("cannot create a symlink or junction on this host")
        self.addCleanup(lambda: os.rmdir(str(alias)) if os.path.lexists(str(alias)) else None)
        with mock.patch.object(aaos, "LIVE_SEAM_ROOT", str(alias / "AAOS")):
            for p in (real / "AAOS", real / "AAOS" / "projects", real / "AAOS" / "no-such" / "x.json"):
                with self.subTest(p=str(p)):
                    with self.assertRaises(aaos.FrozenSeamError):
                        aaos.refuse_frozen_live_path(str(p), "--out")
            aaos.refuse_frozen_live_path(str(self.other), "--out")  # unrelated path still allowed

    def test_b_pathlike_is_screened_by_its_fspath_not_its_str(self):
        decoy = str(self.decoy)

        class Sneaky:
            def __fspath__(self):
                return decoy

            def __str__(self):
                return "C:/not/the/seam" if os.name == "nt" else "/not/the/seam"

        with self.assertRaises(aaos.FrozenSeamError):
            aaos.refuse_frozen_live_path(Sneaky(), "--out")
        with self.assertRaises(aaos.FrozenSeamError):
            aaos.seam_root(Sneaky())

    def test_c_resolution_failure_fails_closed(self):
        with mock.patch.object(aaos.os.path, "realpath", side_effect=OSError("synthetic")):
            with self.assertRaises(aaos.FrozenSeamError) as ctx:
                aaos.refuse_frozen_live_path(str(self.other / "x.json"), "--out")
        self.assertIn("could not be resolved", str(ctx.exception))

    def test_c_unresolvable_path_value_fails_closed(self):
        with self.assertRaises(aaos.FrozenSeamError):
            aaos.refuse_frozen_live_path(str(self.other) + "\x00bad", "--out")

    def test_c_unfrozen_skips_resolution_entirely(self):
        with mock.patch.dict(os.environ, {"AAOS_UNFREEZE": "1"}), \
             mock.patch.object(aaos.os.path, "realpath", side_effect=OSError("synthetic")):
            aaos.refuse_frozen_live_path(str(self.other / "x.json"), "--out")


@unittest.skipUnless(os.name == "nt", "directory junctions are Windows-only")
class NestedJunctionTests(_DecoyCase):
    """Round-2 review C1: a root that is NOT the live seam, but with a junction BELOW it that
    leads into the live seam (fake/projects -> live/projects, fake/contracts -> live/contracts,
    fake/projects/ora-ops/tasks -> live tasks). seam_root(fake) passes; every leaf path the
    builders return, and every directory a tool derives itself, must still be refused."""

    NOW = ("--now", "2026-11-12T00:00:00+00:00")

    def setUp(self):
        super().setUp()
        for sub in ("handoffs", "locks/_archive", "events"):
            (self.decoy / "projects" / "ora-ops" / sub).mkdir(parents=True, exist_ok=True)
        (self.decoy / "contracts").mkdir()
        self.fake = self.tmp / "fake-seam"
        self.fake.mkdir()
        self.baseline = self._snapshot()

    def _junction(self, link: Path, target: Path):
        link.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if proc.returncode != 0 or not link.exists():
            self.skipTest("cannot create a junction on this host")
        self.addCleanup(lambda: os.rmdir(str(link)) if os.path.lexists(str(link)) else None)

    def _refused(self, fn):
        with self.assertRaises(aaos.UsageError) as ctx:
            fn()
        self.assertIn("FROZEN", str(ctx.exception))

    def _run(self, name, *argv):
        module = _tool_loader.load_tool(name)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out), mock.patch("urllib.request.urlopen") as net:
            code = module.main(list(argv))
        net.assert_not_called()
        return code, out.getvalue() + err.getvalue()

    def _assert_cli_refused(self, name, *argv):
        code, output = self._run(name, *argv)
        self.assertEqual(code, 2, output)
        self.assertIn("FROZEN", output)
        self.assertEqual(self._snapshot(), self.baseline, "nothing may be written under the live seam")

    def test_root_itself_is_allowed(self):
        self._junction(self.fake / "projects", self.decoy / "projects")
        self.assertEqual(aaos.seam_root(str(self.fake)), Path(str(self.fake)))

    def test_every_builder_refuses_a_junctioned_projects_dir(self):
        self._junction(self.fake / "projects", self.decoy / "projects")
        r = str(self.fake)
        builders = [
            lambda: aaos.project_dir("ora-ops", root=r),
            lambda: aaos.task_path("t-synthetic", "ora-ops", root=r),
            lambda: aaos.handoff_path("h-synthetic", "ora-ops", root=r),
            lambda: aaos.lock_path("t-synthetic", "ora-ops", root=r),
            lambda: aaos.lock_archive_dir("ora-ops", root=r),
            lambda: aaos.events_log_path("ora-ops", root=r),
            lambda: aaos.event_spool_dir("ora-ops", root=r),
            lambda: aaos._drain_lock_path("ora-ops", root=r),
            lambda: aaos._events_archive_dir("ora-ops", root=r),
            lambda: aaos._pending_drained_dir("ora-ops", root=r),
            lambda: aaos._pending_corrupt_dir("ora-ops", root=r),
        ]
        for i, fn in enumerate(builders):
            with self.subTest(builder=i):
                self._refused(fn)

    def test_deeper_junction_at_events_is_refused(self):
        real = self.fake / "projects" / "ora-ops"
        real.mkdir(parents=True)
        self._junction(real / "events", self.decoy / "projects" / "ora-ops" / "events")
        r = str(self.fake)
        self.assertEqual(aaos.project_dir("ora-ops", root=r), real)
        self._refused(lambda: aaos.events_log_path("ora-ops", root=r))
        self._refused(lambda: aaos._pending_drained_dir("ora-ops", root=r))

    def test_heartbeat_path_is_screened_as_a_leaf(self):
        """heartbeat.json sits directly under the root, so only a file-level alias could redirect
        it; prove the builder screens its leaf by planting a directory junction AT the leaf."""
        self._junction(self.fake / "heartbeat.json", self.decoy)
        self._refused(lambda: aaos.heartbeat_path(root=str(self.fake)))

    def test_create_task_through_junctioned_projects_is_refused(self):
        self._junction(self.fake / "projects", self.decoy / "projects")
        self._assert_cli_refused("create-task.py", "--id", "t-synthetic-junction", "--title", "synthetic",
                                 "--accept", "x", "--seam-root", str(self.fake), *self.NOW)

    def test_emit_event_through_junctioned_events_is_refused(self):
        real = self.fake / "projects" / "ora-ops"
        real.mkdir(parents=True)
        self._junction(real / "events", self.decoy / "projects" / "ora-ops" / "events")
        self._assert_cli_refused("emit-event.py", "--type", "TaskCreated", "--actor", "claude-code",
                                 "--task", "t-synthetic", "--seam-root", str(self.fake), *self.NOW)

    def test_project_notion_through_junctioned_tasks_is_refused(self):
        real = self.fake / "projects" / "ora-ops"
        real.mkdir(parents=True)
        self._junction(real / "tasks", self.decoy / "projects" / "ora-ops" / "tasks")
        self._assert_cli_refused("project-notion.py", "--seam-root", str(self.fake),
                                 "--out", str(self.tmp / "pn.json"), *self.NOW)
        self.assertFalse((self.tmp / "pn.json").exists())

    def test_expire_leases_through_junctioned_locks_is_refused(self):
        real = self.fake / "projects" / "ora-ops"
        real.mkdir(parents=True)
        self._junction(real / "locks", self.decoy / "projects" / "ora-ops" / "locks")
        self._assert_cli_refused("expire-leases.py", "--dry-run", "--seam-root", str(self.fake), *self.NOW)

    def test_publish_contracts_through_junctioned_contracts_is_refused(self):
        self._junction(self.fake / "contracts", self.decoy / "contracts")
        self._assert_cli_refused("publish-contracts.py", "--seam-root", str(self.fake),
                                 "--source-commit", "0" * 40)

    def test_check_contract_drift_through_junctioned_contracts_is_refused(self):
        self._junction(self.fake / "contracts", self.decoy / "contracts")
        self._assert_cli_refused("check-contract-drift.py", "--seam-root", str(self.fake),
                                 "--source-commit", "0" * 40)

    def test_publish_notion_default_in_through_junctioned_projects_is_refused(self):
        """And it is guard_path (via project_dir) that refuses -- not some later check."""
        self._junction(self.fake / "projects", self.decoy / "projects")
        raised = []
        real = aaos.guard_path

        def spy(path, label="derived seam path"):
            try:
                return real(path, label)
            except aaos.UsageError as exc:
                raised.append((label, exc))
                raise

        with mock.patch.object(aaos, "guard_path", side_effect=spy):
            self._assert_cli_refused("publish-notion.py", "--seam-root", str(self.fake), *self.NOW)
        self.assertTrue(raised, "guard_path never refused")
        self.assertEqual(raised[0][0], "project dir")
        self.assertIsInstance(raised[0][1], aaos.FrozenSeamError)

    # ---- round-3 review probes (a), (b), (c), (d): junction BELOW a derived directory ----

    def _assert_failed_closed(self, steps):
        """steps: list of (tool, argv). At least one must refuse with FROZEN (exit 2); none may
        exit with a traceback-style 1; and the decoy must be unchanged at the end."""
        results = [(name, *self._run(name, *argv)) for name, argv in steps]
        self.assertTrue(any(code == 2 and "FROZEN" in out for _n, code, out in results), results)
        self.assertEqual(self._snapshot(), self.baseline, "nothing may be written under the live seam")
        return results

    def test_probe_a_publish_contracts_with_junctioned_contracts_schemas(self):
        (self.fake / "contracts").mkdir()
        self._junction(self.fake / "contracts" / "schemas", self.decoy / "contracts" / "schemas")
        self.baseline = self._snapshot()
        self._assert_cli_refused("publish-contracts.py", "--seam-root", str(self.fake), "--source-commit", "0" * 40)

    def test_probe_b_lifecycle_with_junctioned_lock_archive(self):
        locks = self.fake / "projects" / "ora-ops" / "locks"
        locks.mkdir(parents=True)
        self._junction(locks / "_archive", self.decoy / "projects" / "ora-ops" / "locks" / "_archive")
        self.baseline = self._snapshot()
        common = ("--seam-root", str(self.fake), *self.NOW)
        results = self._assert_failed_closed([
            ("create-task.py", ["--id", "t-probe-b", "--title", "synthetic", "--accept", "x", *common]),
            ("lease-task.py", ["t-probe-b", "--holder", "claude-code", "--session", "s1", "--settle-sec", "0", *common]),
            ("complete-task.py", ["t-probe-b", "--holder", "claude-code", "--session", "s1",
                                  "--evidence", "label=a", "ref=b", *common]),
        ])
        self.assertNotEqual(results[-1][1], 0, "complete-task must not succeed through a junctioned _archive")

    def test_probe_b_archive_lock_unit(self):
        locks = self.fake / "projects" / "ora-ops" / "locks"
        locks.mkdir(parents=True)
        self._junction(locks / "_archive", self.decoy / "projects" / "ora-ops" / "locks" / "_archive")
        lock = locks / "t-synthetic.lock"
        lock.write_text("{}", encoding="utf-8")
        self.baseline = self._snapshot()
        with self.assertRaises(aaos.FrozenSeamError):
            aaos.archive_lock(lock)
        self.assertTrue(lock.exists(), "the lock must not have moved")
        self.assertEqual(self._snapshot(), self.baseline)
        with self.assertRaises(aaos.FrozenSeamError):
            aaos.lock_archive_dir("ora-ops", root=str(self.fake))

    def test_probe_c_check_contract_drift_does_not_read_through_junction(self):
        (self.decoy / "contracts" / "schemas").mkdir(parents=True, exist_ok=True)
        (self.decoy / "contracts" / "schemas" / "LIVE-SECRET.json").write_text("live", encoding="utf-8")
        (self.fake / "contracts").mkdir()
        self._junction(self.fake / "contracts" / "schemas", self.decoy / "contracts" / "schemas")
        self.baseline = self._snapshot()
        code, output = self._run("check-contract-drift.py", "--seam-root", str(self.fake), "--source-commit", "0" * 40)
        self.assertEqual(code, 2, output)
        self.assertIn("FROZEN", output)
        self.assertNotIn("LIVE-SECRET", output)

    def test_probe_c_drift_with_published_mirror_and_junctioned_schemas(self):
        """Realistic shape: a real published mirror whose schemas/ was swapped for a junction."""
        code, output = self._run("publish-contracts.py", "--seam-root", str(self.fake), "--source-commit", "0" * 40)
        self.assertEqual(code, 0, output)
        import shutil as _sh
        _sh.rmtree(str(self.fake / "contracts" / "schemas"))
        (self.decoy / "contracts" / "schemas").mkdir(parents=True, exist_ok=True)
        (self.decoy / "contracts" / "schemas" / "LIVE-SECRET.json").write_text("live", encoding="utf-8")
        self._junction(self.fake / "contracts" / "schemas", self.decoy / "contracts" / "schemas")
        self.baseline = self._snapshot()
        code, output = self._run("check-contract-drift.py", "--seam-root", str(self.fake), "--source-commit", "0" * 40)
        self.assertEqual(code, 2, output)
        self.assertIn("FROZEN", output)
        self.assertNotIn("LIVE-SECRET", output)
        self.assertEqual(self._snapshot(), self.baseline)

    def test_probe_d_contracts_templates_junction(self):
        (self.decoy / "contracts" / "templates").mkdir(parents=True, exist_ok=True)
        (self.decoy / "contracts" / "templates" / "LIVE-ONLY.txt").write_text("live", encoding="utf-8")
        (self.fake / "contracts").mkdir()
        self._junction(self.fake / "contracts" / "templates", self.decoy / "contracts" / "templates")
        self.baseline = self._snapshot()
        for tool in ("publish-contracts.py", "check-contract-drift.py"):
            with self.subTest(tool=tool):
                code, output = self._run(tool, "--seam-root", str(self.fake), "--source-commit", "0" * 40)
                self.assertEqual(code, 2, output)
                self.assertIn("FROZEN", output)
                self.assertNotIn("LIVE-ONLY", output)
                self.assertEqual(self._snapshot(), self.baseline)

    def test_deep_junction_in_spool_drained_dir(self):
        """emit-event: a junction at events/aaos-events.ndjson.pending/_drained (the drain's move target)."""
        spool = self.fake / "projects" / "ora-ops" / "events" / "aaos-events.ndjson.pending"
        spool.mkdir(parents=True)
        self._junction(spool / "_drained", self.decoy / "projects" / "ora-ops" / "events")
        self.baseline = self._snapshot()
        code, output = self._run("emit-event.py", "--type", "TaskCreated", "--actor", "claude-code",
                                 "--task", "t-synthetic", "--seam-root", str(self.fake), *self.NOW)
        self.assertEqual(code, 2, output)
        self.assertIn("FROZEN", output)
        self.assertEqual(self._snapshot(), self.baseline)


if __name__ == "__main__":
    unittest.main()
