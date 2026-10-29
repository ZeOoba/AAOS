"""Regenerate (or --check) tests/fixtures/iso_oracle_py313.json from the interpreter running this script.

    python tests/record_iso_oracle.py            # rewrite the fixture
    python tests/record_iso_oracle.py --check    # exit 1 if the committed fixture differs from what this interpreter records

Run it on CPython >= 3.13.17 (the newest behaviour the normaliser follows). The recorded verdict of each string is
its own `datetime.fromisoformat` answer: the UTC-offset-aware isoformat string, or null when rejected or naive.

The corpus (the strings) is the key set of the existing fixture, frozen; this script only re-records the verdicts.
It splits the corpus in two:
  * "stable": verdicts every supported 3.11+ patch gives, asserted on every interpreter.
  * "empty_fraction_before_offset": a '.' or ',' with no digits directly before an offset or Z. CPython 3.13.17 rejects
    these; 3.11.17 and 3.13.14 accept them as if the separator were absent. Each entry stores both answers
    ("rejecting" = recorded here, "accepting" = the verdict of the same string with the empty separator removed),
    and the test asserts the one matching the interpreter it runs on (probed with the `canary` string).
This file refuses to record on an interpreter that still accepts the canary, because that would record the old answers.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "iso_oracle_py313.json"
CANARY = "2026-07-10T17:46:23.Z"
EMPTY_FRAC = re.compile(r"\d[.,](?=[+\-]|Z)")


def verdict(text: str):
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.isoformat() if dt.tzinfo is not None else None


def load_corpus(data: dict) -> list[str]:
    if "stable" in data:
        return sorted(set(data["stable"]) | set(data["empty_fraction_before_offset"]))
    return sorted(data)  # legacy flat {string: verdict} layout


def record(corpus: list[str]) -> dict:
    if verdict(CANARY) is not None:
        raise SystemExit(f"{sys.version.split()[0]} accepts {CANARY!r}; record on CPython >= 3.13.17 (the rejecting behaviour)")
    stable, empty = {}, {}
    for s in corpus:
        if EMPTY_FRAC.search(s):
            empty[s] = {"rejecting": verdict(s), "accepting": verdict(EMPTY_FRAC.sub(lambda m: m.group(0)[0], s))}
        else:
            stable[s] = verdict(s)
    return {
        "_meta": {
            "recorded_on": sys.version.split()[0],
            "regenerate": "python tests/record_iso_oracle.py",
            "canary": CANARY,
        },
        "stable": stable,
        "empty_fraction_before_offset": empty,
    }


def dump(obj: dict) -> str:
    def block(d):
        return "{\n" + ",\n".join(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}" for k, v in d.items()) + "\n }"
    return ("{\n" + ",\n".join(f'"{k}": ' + (json.dumps(v) if k == "_meta" else block(v)) for k, v in obj.items()) + "\n}\n")


def main(argv) -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    new = dump(record(load_corpus(data)))
    if "--check" in argv:
        old = FIXTURE.read_text(encoding="utf-8")
        if old != new:
            print("fixture differs from what this interpreter records; rerun without --check", file=sys.stderr)
            return 1
        print("fixture is current")
        return 0
    FIXTURE.write_text(new, encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE} ({len(json.loads(new)['stable'])} stable, {len(json.loads(new)['empty_fraction_before_offset'])} empty-fraction entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
