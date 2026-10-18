"""tests/test_iso_py310_normaliser.py -- _parse_iso_dt must give the same answer on 3.10 as on 3.11+
for the timestamp shapes a shell, PowerShell or .NET actually writes.

Python 3.10's datetime.fromisoformat() only accepts a fraction of exactly 3 or 6 digits and an offset
written +HH:MM. 3.11+ accepts any fraction length (truncated to 6 digits), the comma decimal, +HHMM,
+HH and +HHMMSS offsets, and spaces before the offset. Cowork's sandbox runs 3.10, so a value such as
`date -u +%FT%T%z` (+0000), PowerShell's 7-digit fraction or `date +%N`'s 9-digit fraction made
parse_cli_now exit 2 and, worse, made read_events sort that event last without any error.

The oracle is CPython 3.13: tests/fixtures/iso_oracle_py313.json holds its verdict (UTC-offset-aware
isoformat string, or null = rejected) for 667 strings, stratified by fraction length and tail shape.
Whitespace before the offset ('23 +05:30') is NOT covered: 3.13 accepts it only for some fraction
lengths, so mirroring it would be guesswork; it stays rejected on 3.10. The same file is asserted on every interpreter CI runs, so the 3.10 leg is checked against 3.13's
recorded answers. The live differential (3.11+) re-derives the verdict from the interpreter itself
over a wider generated space. Basic, week and ordinal formats are NOT covered and stay rejected on 3.10.
"""
from __future__ import annotations

import itertools
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tool_loader  # noqa: F401,E402  (puts tools/ on sys.path)
import _aaos_common as aaos  # noqa: E402

ORACLE = json.loads((Path(__file__).resolve().parent / "fixtures" / "iso_oracle_py313.json").read_text(encoding="utf-8"))


def _verdict(text):
    try:
        return aaos._parse_iso_dt(text).isoformat()
    except aaos.CorruptStateError:
        return None


class AgainstRecordedPy313(unittest.TestCase):
    def test_fixture_is_not_trivially_all_rejections(self):
        accepted = sum(v is not None for v in ORACLE.values())
        self.assertGreater(accepted, 250)
        self.assertGreater(len(ORACLE) - accepted, 250)

    def test_every_recorded_verdict_matches_on_this_interpreter(self):
        wrong = {s: (_verdict(s), want) for s, want in ORACLE.items() if _verdict(s) != want}
        self.assertEqual(wrong, {}, f"{len(wrong)} of {len(ORACLE)} disagree with CPython 3.13, e.g. {dict(itertools.islice(wrong.items(), 5))}")


class NamedShapes(unittest.TestCase):
    def test_shapes_real_tools_write(self):
        for text, want in (
            ("2026-07-10T17:46:23+0000", "2026-07-10T17:46:23+00:00"),            # date -u +%FT%T%z
            ("2026-07-10T11:58:06-0400", "2026-07-10T11:58:06-04:00"),
            ("2026-07-10T17:46:23+00", "2026-07-10T17:46:23+00:00"),
            ("2026-07-10T17:46:23.1234567+00:00", "2026-07-10T17:46:23.123456+00:00"),   # PowerShell / .NET: 7 digits, truncated not rounded
            ("2026-07-10T17:46:23.9999999Z", "2026-07-10T17:46:23.999999+00:00"),
            ("2026-07-10T17:46:23.123456789Z", "2026-07-10T17:46:23.123456+00:00"),      # date +%N: 9 digits
            ("2026-07-10T17:46:23.5Z", "2026-07-10T17:46:23.500000+00:00"),
            ("2026-07-10T17:46:23.58Z", "2026-07-10T17:46:23.580000+00:00"),
            ("2026-07-10T17:46:23.5808Z", "2026-07-10T17:46:23.580800+00:00"),
            ("2026-07-10T17:46:23,580832Z", "2026-07-10T17:46:23.580832+00:00"),         # comma decimal
            ("2026-07-10 17:46:23,5-0530", "2026-07-10T17:46:23.500000-05:30"),
            ("2026-07-10T17:46:23+053015", "2026-07-10T17:46:23+05:30:15"),
        ):
            with self.subTest(text=text):
                self.assertEqual(_verdict(text), want)

    def test_unicode_digits_behave_like_3_13(self):
        # 3.13 accepts a non-ASCII decimal digit after the 6th fraction digit (truncated) and rejects one in
        # the first 6 digits or the offset; the normaliser's \d must keep that.
        self.assertEqual(_verdict("2026-07-10T17:46:23.123456٧Z"), "2026-07-10T17:46:23.123456+00:00")
        self.assertIsNone(_verdict("2026-07-10T17:46:23.12345٧Z"))
        self.assertIsNone(_verdict("2026-07-10T17:46:23+٠٠:00"))

    def test_already_portable_shapes_are_untouched(self):
        for text in ("2026-07-10T17:46:23.580832Z", "2026-07-10T17:46:23+00:00", "2026-07-10T11:58:06.951866-04:00"):
            with self.subTest(text=text):
                self.assertEqual(_verdict(text), datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat())

    def test_the_normaliser_never_accepts_what_3_11_plus_rejects(self):
        for bad in ("2026-07-10T17:46:23.5.5Z", "2026-07-10T17:46:23+0",
                    "2026-07-10T17:46:23+000", "2026-07-10T17:46:23+00000", "2026-07-10T17:46:23+00:0",
                    "2026-07-10T17:46:23+ 00:00",
                    "2026-07-10T17:46:23.5ZZ", "2026-07-10T17:46:23.5z", "2026-07-10T17:46:23,+05:3", "2026-07-10T17:46:23, Z", "2026-07-10T17:46:23+24:00", "2026-07-10T17:46:23+0060x",
                    "2026-07-10T17:46:23", "2026-07-10T17:46:23.5"):
            with self.subTest(bad=bad):
                self.assertIsNone(_verdict(bad))


@unittest.skipIf(sys.version_info < (3, 11), "needs a 3.11+ interpreter as its own oracle; 3.10 is covered by the recorded verdicts")
class LiveDifferential(unittest.TestCase):
    """On 3.11+, fromisoformat is the oracle. The normaliser is a pure function, so it can be run here
    directly: over every generated string it must neither lose an accepted value nor accept a rejected one."""

    @staticmethod
    def _std(text):
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt.isoformat() if dt.tzinfo is not None else None

    def test_normaliser_matches_stdlib_over_the_covered_family(self):
        heads = ("2026-07-10T17:46:23", "2026-07-10 17:46:23", "0001-01-01T00:00:00", "9999-12-31T23:59:59")
        fracs = ("",) + tuple("." + "5" * n for n in range(0, 11)) + tuple("," + "5" * n for n in (0, 1, 3, 6, 9))
        tails = ("", "Z", "ZZ", "z", "+00:00Z", "+00:00", "-04:00", "+05:30:15", "+0000", "-0400", "+0530", "+053015", "+00", "-04", "+0", "+000",
                 "+00000", "+24:00", "+0060", "+00:60", "+00:", "+00:0", "+05:3015", "+0530:15", "+00:00:00", "+00x")
        checked = 0
        for head, frac, tail in itertools.product(heads, fracs, tails):
            text = head + frac + tail
            with self.subTest(text=text):
                self.assertEqual(self._std(aaos._normalise_iso_for_py310(text)), self._std(text))
            checked += 1
        self.assertGreater(checked, 1000)

    def test_normaliser_leaves_everything_outside_the_covered_family_alone(self):
        for text in ("not-a-date", "", "2026-W28-5T17:46:23Z", "20260710T174623Z", "2026-07-10", "2026-07-10T17:46Z",
                     "2026-07-10T17:46:23 Z", "2026-07-10T17:46:23.5 +05:30", "2026-07-10T17:46:23+00:00Z", "2026-07-10T17:46:23z"):
            with self.subTest(text=text):
                self.assertEqual(aaos._normalise_iso_for_py310(text), text)


if __name__ == "__main__":
    unittest.main()
