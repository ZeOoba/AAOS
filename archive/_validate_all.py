import json
import sys
import jsonschema

files = [
    "agent-registry.schema.json",
    "task.schema.json",
    "handoff.schema.json",
    "event.schema.json",
    "heartbeat.schema.json",
]

base = r"C:\Users\Owner\AAOS\schemas"
print(f"jsonschema version: {jsonschema.__version__}")
all_ok = True
for f in files:
    path = f"{base}\\{f}"
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
        jsonschema.Draft202012Validator.check_schema(s)
        print(f"OK: {f}")
    except Exception as e:
        all_ok = False
        print(f"FAIL: {f} -> {type(e).__name__}: {e}")

sys.exit(0 if all_ok else 1)
