"""Demonstrate ADR-0006's central claim: assert_lease_held cannot detect two holders.

AAOS-SPEC.md section 7 names assert_lease_held as the mechanism that closes the double-run
risk. It reads task.lease -- the derived projection -- never the lock file, which is what
actually arbitrates acquisition. So a holder the lock file says LOST still passes it.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "tools")
import _aaos_common as aaos

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
P = "ora-ops"
root = tempfile.mkdtemp(prefix="backstop-")
for sub in ("tasks", "handoffs", "locks/_archive", "events"):
    os.makedirs(os.path.join(root, "projects", P, sub), exist_ok=True)

tid = "t-backstop"

# The lock file -- the AUTHORITY -- says cowork/sess-B holds the lease.
lp = aaos.lock_path(tid, P, root=root)
lp.parent.mkdir(parents=True, exist_ok=True)
lp.write_text(json.dumps({
    "protocol_version": "1.0", "task_ref": tid, "holder": "cowork",
    "acquired": NOW.isoformat(),
    "expires": (NOW + timedelta(minutes=45)).isoformat(),
    "session": "sess-B",
}), encoding="utf-8")

# The task -- the PROJECTION -- still says claude-code/sess-A holds it. This is the
# ordinary state for a full settle window after every acquisition, because lease-task.py
# writes this field only after acquire_lease returns.
aaos.save_task({
    "protocol_version": "1.0", "id": tid, "project_id": P, "title": "t",
    "owner_agent": "claude-code", "state": "leased", "priority": 2,
    "inputs": [], "acceptance_criteria": ["x"],
    "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    "lease": {"holder": "claude-code", "session": "sess-A",
              "acquired": NOW.isoformat(),
              "expires": (NOW + timedelta(minutes=45)).isoformat()},
}, root=root)

task = aaos.load_task(tid, P, root=root)
lock = json.loads(lp.read_text(encoding="utf-8"))

print(f"lock file (AUTHORITY) holder : {lock['holder']} / {lock['session']}")
print(f"task.lease (PROJECTION)      : {task['lease']['holder']} / {task['lease']['session']}")
print()

try:
    aaos.assert_lease_held(task, "claude-code", "sess-A", now=NOW)
    print("assert_lease_held(sess-A) -> PASSED")
    print()
    print(">>> sess-A is cleared to commit while the lock file says sess-B holds the")
    print(">>> lease. The designated double-run backstop cannot see the disagreement,")
    print(">>> because it never reads the artifact that arbitrates ownership.")
except aaos.LeaseLostError as exc:
    print(f"assert_lease_held(sess-A) -> RAISED LeaseLostError: {exc}")
    print(">>> backstop detected it; ADR-0006's central claim would be WRONG")

shutil.rmtree(root, ignore_errors=True)
