"""
tests/_tool_loader.py — helper to import the hyphenated tools/*.py CLI
scripts in-process for unit testing.

Filenames like "create-task.py" cannot be imported with a plain `import`
statement (Python identifiers can't contain hyphens), so this module uses
importlib.util.spec_from_file_location per docs/PHASE3-DESIGN.md's
suggestion. Loaded modules are cached in sys.modules under a synthetic,
collision-free name so repeated calls for the same file return the same
module object (mirroring normal import caching).

tools/*.py scripts do `import _aaos_common as aaos`; this module ensures
tools/ is on sys.path before exec'ing any of them, the same sys.path
pattern tests/test_validate.py and tests/test_aaos_common.py already use.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def load_tool(filename: str):
    """Load tools/<filename> (e.g. "create-task.py") as a module object.

    Cached per filename in sys.modules so multiple test classes loading the
    same tool don't re-exec it (and so `module.main` is a stable object
    across a test run, matching how a normal `import` would behave).
    """
    module_name = "_aaos_tool_" + filename.replace("-", "_").replace(".py", "")
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = TOOLS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Write-scope registry derivation (OI-263)
#
# agent-registry.json declares claude-code's write_scope as the literal absolute
# "C:/Users/Owner/AAOS/**". Any test that builds an artifact under REPO_ROOT and
# expects it to pass write-scope validation therefore only passes on a checkout
# physically located at that path -- it asserts a directory layout rather than
# match_write_scope()'s logic, and fails on every other machine and CI runner.
#
# These helpers rebase that one canonical entry onto wherever this checkout
# actually is. Everything else in the registry is left alone, so out-of-scope
# cases still validate against a realistic document. Tests pass the result to
# post-handoff.py via its --registry flag; the production registry is untouched.
# ---------------------------------------------------------------------------

CANONICAL_REPO_GLOB = "C:/Users/Owner/AAOS/**"


def local_repo_glob(repo_root=None) -> str:
    """`repo_root` (default: this checkout) as a write_scope glob."""
    return str(repo_root or REPO_ROOT).replace("\\", "/").rstrip("/") + "/**"


def derive_local_registry(repo_root=None) -> dict:
    """The production registry with CANONICAL_REPO_GLOB rebased onto `repo_root`.

    `repo_root` is injectable ONLY so the regression test can prove the rebase
    happens on a root that differs from this checkout's. On the operating machine
    REPO_ROOT already equals CANONICAL_REPO_GLOB's directory, so a test that used
    the default would assert nothing here and only carry meaning on CI.
    """
    registry = json.loads(
        (REPO_ROOT / "agent-registry.json").read_text(encoding="utf-8")
    )
    here = local_repo_glob(repo_root)
    for agent in registry["agents"]:
        for field in ("write_scope", "reachable_paths"):
            if field in agent:
                agent[field] = [
                    here if entry == CANONICAL_REPO_GLOB else entry
                    for entry in agent[field]
                ]
    return registry


def write_local_registry(directory) -> Path:
    """Write derive_local_registry() into `directory`; return the path."""
    path = Path(directory) / "agent-registry.json"
    path.write_text(json.dumps(derive_local_registry()), encoding="utf-8")
    return path
