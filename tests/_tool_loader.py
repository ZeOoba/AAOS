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
