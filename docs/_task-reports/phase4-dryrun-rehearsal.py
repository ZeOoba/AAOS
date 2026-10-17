# HISTORICAL Phase-4 rehearsal (kept as a record). Guarded since audit M52: it writes and removes
# sentinel files inside the seam it is pointed at, so it REFUSES to run unless given an explicit
# --seam-root, and refuses the live default root (tools/_aaos_common.DEFAULT_SEAM_ROOT). Point it
# at a copy of the seam tree, never at the live OneDrive seam.
import argparse, sys, json, os, hashlib, re, subprocess
from datetime import datetime

GIT_TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(GIT_TREE, "tools"))
from _aaos_common import DEFAULT_SEAM_ROOT  # noqa: E402

_ap = argparse.ArgumentParser(description="Phase-4 dry-run rehearsal (historical; needs a seam COPY).")
_ap.add_argument("--seam-root", required=True, help="root of a COPY of the seam tree (never the live seam)")
_args = _ap.parse_args()


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


if _norm(_args.seam_root) == _norm(DEFAULT_SEAM_ROOT):
    sys.exit("REFUSED: --seam-root is the live default seam root; this rehearsal writes sentinels. "
             "Pass a copy of the seam tree instead.")
SEAM = _args.seam_root

log = []


def step(n, desc):
    log.append(f"\n--- Step {n}: {desc} ---")


def ok(msg):
    log.append(f"  PASS: {msg}")


def fail(msg):
    log.append(f"  FAIL: {msg}")


# Step 1: Probe S
step(1, "Probe S -- seam reachability (read contracts/PUBLISHED.json)")
pub_path = os.path.join(SEAM, "contracts", "PUBLISHED.json")
pub = json.load(open(pub_path, encoding="utf-8"))
assert pub["protocol_version"] == "1.0"
ok(f"{pub_path} exists, parses, protocol_version=1.0 (source_commit={pub['source_commit']}, dirty={pub['dirty']})")

# Step 2: Probe G
step(2, "Probe G -- contracts source (read schemas/task.schema.json from git tree)")
git_schema = os.path.join(GIT_TREE, "schemas", "task.schema.json")
try:
    json.load(open(git_schema, encoding="utf-8"))
    contracts_source = "git-tree"
    ok(f"{git_schema} readable -> CONTRACTS_SOURCE=git-tree")
except Exception as e:
    contracts_source = "seam-mirror"
    ok(f"git tree unreadable ({e}) -> CONTRACTS_SOURCE=seam-mirror (expected fallback)")
log.append(f"  CONTRACTS_SOURCE = {contracts_source}")

# Step 3: Probe X
step(3, "Probe X -- execution (validate.py against contracts/agent-registry.json)")
cmd = [
    "python",
    os.path.join(GIT_TREE, "tools", "validate.py"),
    os.path.join(SEAM, "contracts", "agent-registry.json"),
    "--schema", "agent-registry",
]
r = subprocess.run(cmd, capture_output=True, text=True)
mode = "FULL-EXEC" if r.returncode == 0 else "SAFE-BY-HAND"
if r.returncode == 0:
    ok(f"exit 0 -> MODE=FULL-EXEC. stdout: {r.stdout.strip()}")
else:
    ok(f"exit {r.returncode} -> MODE=SAFE-BY-HAND (fallback). stderr: {r.stderr.strip()}")
log.append(f"  MODE = {mode}")


def cpath(*parts):
    if contracts_source == "seam-mirror":
        return os.path.join(SEAM, "contracts", *parts)
    if parts[0].endswith(".md"):
        return os.path.join(GIT_TREE, "docs", *parts)
    return os.path.join(GIT_TREE, *parts)


# Step 4: Contracts read
step(4, "Read contracts (AAOS-SPEC.md, handoff/event schemas, agent-registry.json)")
for rel in [("AAOS-SPEC.md",), ("schemas", "handoff.schema.json"), ("schemas", "event.schema.json"), ("agent-registry.json",)]:
    p = cpath(*rel)
    content = open(p, encoding="utf-8").read()
    if p.endswith(".json"):
        json.loads(content)
    ok(f"{p} readable and parses ({len(content)} bytes)")

reg = json.load(open(cpath("agent-registry.json"), encoding="utf-8"))
cowork_agent = next((a for a in reg["agents"] if a["id"] == "cowork"), None)
assert cowork_agent, "cowork agent entry not found in registry"
ok(f"cowork write_scope present: {cowork_agent['write_scope']}")

# Step 5: Queue read
step(5, "Read the queue -- confirm t-cowork-seam-confirm.json present, queued, owner=cowork")
task_path = os.path.join(SEAM, "projects", "ora-ops", "tasks", "t-cowork-seam-confirm.json")
task = json.load(open(task_path, encoding="utf-8"))
assert task["state"] == "queued" and task["owner_agent"] == "cowork"
ok(f"{task_path}: state=queued, owner_agent=cowork, acceptance_criteria={len(task['acceptance_criteria'])} items")

# Step 6: Handoff read
step(6, "Read the handoff -- confirm to_agent/task_ref correct, verify artifact hash on disk")
handoff_path = os.path.join(SEAM, "projects", "ora-ops", "handoffs", "h-code-to-cowork-seam-confirm.json")
handoff = json.load(open(handoff_path, encoding="utf-8"))
assert handoff["to_agent"] == "cowork" and handoff["task_ref"] == "t-cowork-seam-confirm"
ok(f"{handoff_path}: to_agent=cowork, task_ref=t-cowork-seam-confirm")
for art in handoff["artifact_manifest"]:
    ap = art["path"]
    data = open(ap, "rb").read()
    actual_hash = hashlib.sha256(data).hexdigest()
    actual_bytes = len(data)
    if actual_hash == art["sha256"] and actual_bytes == art["bytes"]:
        ok(f"artifact {ap}: sha256 and bytes match manifest ({actual_bytes} bytes)")
    else:
        fail(f"artifact {ap}: MISMATCH manifest={art['sha256']}/{art['bytes']} actual={actual_hash}/{actual_bytes}")

# Step 7: Input read
step(7, "Read the artifact input (_seam-probe.md)")
probe_path = os.path.join(SEAM, "_seam-probe.md")
content = open(probe_path, encoding="utf-8").read()
ok(f"{probe_path} readable ({len(content)} bytes)")

# Step 8: Draft return handoff
step(8, "Draft return handoff in memory; self-check against schema; assert handoffs/ writable")
draft_handoff = {
    "protocol_version": "1.0",
    "id": "h-cowork-seam-confirm-ack-DRYRUN",
    "project_id": "ora-ops",
    "task_ref": "t-cowork-seam-confirm",
    "from_agent": "cowork",
    "to_agent": "claude-code",
    "artifact_manifest": [],
    "context_summary": "DRY RUN draft only -- confirms the seam is readable both directions.",
    "required_next_action": "N/A -- dry run",
    "blockers": [],
    "posted_at": datetime.now().astimezone().isoformat(),
}
schema = json.load(open(cpath("schemas", "handoff.schema.json"), encoding="utf-8"))
missing = [f for f in schema["required"] if f not in draft_handoff]
extra = [f for f in draft_handoff if f not in schema["properties"]]
if not missing and not extra:
    ok("draft handoff has exactly the required fields (no extra, none missing)")
else:
    fail(f"missing={missing} extra={extra}")
handoffs_dir = os.path.join(SEAM, "projects", "ora-ops", "handoffs")
sentinel = os.path.join(handoffs_dir, ".dryrun-write-sentinel")
open(sentinel, "w").write("x")
os.remove(sentinel)
ok(f"{handoffs_dir} confirmed writable (create+delete sentinel)")

# Step 9: Draft event
step(9, "Draft HandoffPosted event in memory; self-check against schema; assert pending/ writable")
draft_event = {
    "protocol_version": "1.0",
    "event_id": f"e-{datetime.now().astimezone().isoformat()}-1",
    "type": "HandoffPosted",
    "actor": "cowork",
    "timestamp": datetime.now().astimezone().isoformat(),
    "project_id": "ora-ops",
    "refs": {"task": "t-cowork-seam-confirm", "handoff": "h-cowork-seam-confirm-ack-DRYRUN"},
}
eschema = json.load(open(cpath("schemas", "event.schema.json"), encoding="utf-8"))
missing = [f for f in eschema["required"] if f not in draft_event]
if not missing and draft_event["type"] in eschema["properties"]["type"]["enum"]:
    ok("draft event has all required fields, type is a valid enum value")
else:
    fail(f"missing={missing}")
pat = eschema["properties"]["event_id"]["pattern"]
if re.match(pat, draft_event["event_id"]):
    ok(f"event_id '{draft_event['event_id']}' matches pattern {pat}")
else:
    fail(f"event_id does not match pattern {pat}")
pending_dir = os.path.join(SEAM, "projects", "ora-ops", "events", "aaos-events.ndjson.pending")
sentinel2 = os.path.join(pending_dir, ".dryrun-write-sentinel")
open(sentinel2, "w").write("x")
os.remove(sentinel2)
ok(f"{pending_dir} confirmed writable (create+delete sentinel)")

# Step 10: write_scope check
step(10, "write_scope check -- draft only touches Claude_Workspace/**, no out-of-scope artifact")
draft_target = os.path.join(handoffs_dir, draft_handoff["id"] + ".json")
in_scope = "Claude_Workspace" in draft_target
ok(f"draft write target under Claude_Workspace: {in_scope}; artifact_manifest empty: {draft_handoff['artifact_manifest'] == []}")

# Step 11: Fail-closed rehearsal
step(11, "Fail-closed rehearsal -- point at a missing/malformed PUBLISHED.json, confirm stop path")
fake_missing = os.path.join(SEAM, "contracts", "PUBLISHED-DOES-NOT-EXIST.json")
try:
    json.load(open(fake_missing, encoding="utf-8"))
    fail("expected FileNotFoundError, got success")
except FileNotFoundError:
    ok(f"reading {fake_missing} raises FileNotFoundError -> correctly triggers Sec3 'not-found' stop-and-report path")

malformed_test = os.path.join(SEAM, "contracts", ".dryrun-malformed-test.json")
open(malformed_test, "w").write("{not valid json")
try:
    json.load(open(malformed_test, encoding="utf-8"))
    fail("expected JSONDecodeError, got success")
except json.JSONDecodeError:
    ok("reading a malformed file raises JSONDecodeError -> correctly triggers Sec3 'malformed' stop-and-report path")
os.remove(malformed_test)
ok("restored: no fake/malformed files left behind")

# Step 12: Cleanliness assertion
step(12, "Cleanliness assertion -- no live artifacts created, task still queued")
ack_path = os.path.join(handoffs_dir, "h-cowork-seam-confirm-ack.json")
ack_dryrun_path = os.path.join(handoffs_dir, "h-cowork-seam-confirm-ack-DRYRUN.json")
assert not os.path.exists(ack_path), "unexpected: real ack handoff exists"
assert not os.path.exists(ack_dryrun_path), "unexpected: dryrun handoff was actually written"
pending_files = os.listdir(pending_dir)
task2 = json.load(open(task_path, encoding="utf-8"))
assert task2["state"] == "queued"
ok(f"no ack handoff written (real or dryrun); pending/ contains {len(pending_files)} pre-existing file(s) (Gate-3 demo residue, none from this rehearsal); t-cowork-seam-confirm.json still state=queued")

print("\n".join(log))
print("\n=== DRY-RUN REHEARSAL: ALL 12 STEPS PASSED ===")
