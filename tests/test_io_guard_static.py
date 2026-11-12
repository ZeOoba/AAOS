"""
tests/test_io_guard_static.py -- static enforcement of the guarded-I/O rule (docs/adr/0007-freeze.md,
addendum 2026-11-12; round-3 review).

Every filesystem read, write, move, link, unlink, mkdir and directory enumeration in tools/ must
go through the guarded primitives in tools/_aaos_common.py (the block between the
"# >>> GUARDED I/O PRIMITIVES" and "# <<< GUARDED I/O PRIMITIVES" markers). Those primitives screen
every path -- source and destination, and every enumerated entry -- against the live seam while
AAOS is frozen. A raw call anywhere else is a route the screen cannot see, so this test fails CI
on any new one.

The scan blanks out comments and string literals (via tokenize) first, so docstrings that
mention os.replace etc. do not count.

Not covered by this rule, by decision (recorded in the ADR): metadata-only calls (Path.exists,
is_file, is_dir, stat, os.stat). They open no file for reading or writing and enumerate nothing.
Every path they are called on is either built by a guarded builder or is about to be passed to a
guarded primitive. Path.replace() is not matched because it cannot be told apart from str.replace
lexically; no tool uses Path.replace(), and os.replace is matched.
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
COMMON = TOOLS / "_aaos_common.py"
BEGIN = "# >>> GUARDED I/O PRIMITIVES"
END = "# <<< GUARDED I/O PRIMITIVES"

FORBIDDEN = [
    re.compile(r"\bos\.(replace|rename|renames|link|symlink|unlink|remove|rmdir|removedirs|makedirs|mkdir|"
               r"open|listdir|scandir|walk|truncate|chmod|utime)\s*\("),
    re.compile(r"\bshutil\.\w+\s*\("),
    re.compile(r"\b(io|codecs|gzip|tempfile)\.\w*open\w*\s*\("),
    re.compile(r"(?<![\w.])open\s*\("),
    re.compile(r"\.(open|read_text|write_text|read_bytes|write_bytes|iterdir|glob|rglob|mkdir|rmdir|"
               r"unlink|rename|touch|symlink_to|hardlink_to|link_to|walk)\s*\("),
]

# Raw call sites allowed OUTSIDE the primitives block: (file name, stripped code line). Keep empty.
ALLOWED_OUTSIDE_BLOCK: set[tuple[str, str]] = set()


def _code_lines(path: Path) -> list[str]:
    """Source lines with every comment and string literal (incl. f-string parts) blanked."""
    src = path.read_text(encoding="utf-8")
    lines = [list(l) for l in src.splitlines(keepends=True)]
    blank_types = {tokenize.COMMENT, tokenize.STRING}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            blank_types.add(getattr(tokenize, name))
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in blank_types:
            continue
        (sr, sc), (er, ec) = tok.start, tok.end
        for r in range(sr, er + 1):
            row = lines[r - 1]
            start = sc if r == sr else 0
            end = ec if r == er else len(row)
            for c in range(start, min(end, len(row))):
                if row[c] != "\n":
                    row[c] = " "
    return ["".join(l) for l in lines]


def _violations(path: Path, block: tuple[int, int] | None = None) -> list[str]:
    out = []
    for i, line in enumerate(_code_lines(path), start=1):
        if block and block[0] < i < block[1]:
            continue
        for rx in FORBIDDEN:
            if rx.search(line) and (path.name, line.strip()) not in ALLOWED_OUTSIDE_BLOCK:
                out.append(f"{path.name}:{i}: {line.strip()}")
                break
    return out


def _block_span() -> tuple[int, int]:
    lines = COMMON.read_text(encoding="utf-8").splitlines()
    begins = [i for i, l in enumerate(lines, 1) if l.startswith(BEGIN)]
    ends = [i for i, l in enumerate(lines, 1) if l.startswith(END)]
    assert len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0], "primitives block markers missing"
    return begins[0], ends[0]


class GuardedIoStaticTests(unittest.TestCase):
    def test_no_raw_filesystem_calls_outside_the_primitives_block(self):
        span = _block_span()
        violations = []
        for f in sorted(TOOLS.glob("*.py")):
            violations += _violations(f, span if f == COMMON else None)
        self.assertEqual(violations, [], "raw filesystem calls must go through the guarded primitives:\n"
                         + "\n".join(violations))

    def test_every_primitive_screens_its_paths_first(self):
        start, end = _block_span()
        block = COMMON.read_text(encoding="utf-8").splitlines()[start:end - 1]
        src = "\n".join(block)
        funcs = re.findall(r"^def (g_\w+)\(.*?(?=^def |\Z)", src, flags=re.M | re.S)
        bodies = dict(re.findall(r"^def (g_\w+)\((.*?)(?=^def |\Z)", src, flags=re.M | re.S))
        self.assertGreaterEqual(len(funcs), 12)
        for name, body in bodies.items():
            if name == "g_walk":
                self.assertIn("g_iterdir(", body, name)  # screened via g_iterdir
                continue
            self.assertIn("_io_guard(", body, f"{name} must call _io_guard before touching the filesystem")

    def test_every_cli_main_is_wrapped_by_freeze_exit(self):
        for f in sorted(TOOLS.glob("*.py")):
            if f.name == "_aaos_common.py":
                continue
            src = f.read_text(encoding="utf-8")
            self.assertRegex(src, r"@_?aaos\.freeze_exit\ndef main\(", f.name)

    def test_scanner_flags_raw_calls_and_ignores_strings(self):
        """Self-test: the scanner must catch each forbidden shape and skip comments/docstrings."""
        import tempfile
        bad = ['os.replace(a, b)', 'p.read_text(encoding="utf-8")', 'open(p)', 'd.iterdir()',
               'shutil.move(a, b)', 'os.makedirs(d, exist_ok=True)', 'x.rglob("*")', 'p.unlink()',
               'p.write_bytes(b"")', 'os.listdir(d)', 'os.scandir(d)', 'p.open("rb")']
        ok = ['"os.replace(a, b)"', '# open(p)', 'aaos.g_read_text(p)', 'g_open(p)', 's.replace("a", "b")',
              'aaos.g_iterdir(d)']
        with tempfile.TemporaryDirectory() as d:
            for snippet, expect in [(b, True) for b in bad] + [(o, False) for o in ok]:
                f = Path(d) / "snippet.py"
                f.write_text(f"x = 1\n{snippet}\n", encoding="utf-8")
                with self.subTest(snippet=snippet):
                    self.assertEqual(bool(_violations(f)), expect)


if __name__ == "__main__":
    unittest.main()
