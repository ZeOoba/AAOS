# L0 Cowork locality probe (read-only)

Plan item L0 / ballot Q14-A. Executed by Gabriel in ONE interactive desktop Cowork session. Timebox: 2 sessions; if Cowork cannot reproduce the answers, V1 proceeds with Code-only contenders.

## Before you start
- Open a new interactive Cowork session on the desktop app (not a scheduled task).
- Select the folder `Claude_Workspace\AAOS` (under `OneDrive - Oxford Capital Partners`) so it is mounted. Nothing else is needed.
- Paste the prompt below, then paste the raw output back into the Code session.

## Prompt to paste into Cowork

> READ-ONLY locality probe for AAOS (plan item L0). Do NOT create, edit, move, rename or delete any file anywhere in the mounted folders, and do NOT run any AAOS tool (no lease, create, emit, drain, validate-with-write). Run only the read-only commands below, in order, and paste the raw output of each. Replace `<seam>` with the path at which `Claude_Workspace/AAOS` appears in this session.
>
> 1. `uname -a; head -3 /etc/os-release; python3 --version; date -u +%FT%T%z; echo "TZ=$TZ"`
> 2. Print `<seam>` itself (the path you would use to read `contracts/PUBLISHED.json`).
> 3. `mount | grep -iE "workspace|aaos|onedrive|virtiofs|9p|fuse|cifs|smb|vboxsf|drvfs"`; if that prints nothing, `findmnt -T <seam>`. Then `df -hT <seam>`.
> 4. `stat -c 'name=%n inode=%i nlink=%h dev=%d type=%F size=%s mtime=%y' <seam>/_seam-probe.md <seam>/contracts/PUBLISHED.json`
> 5. `ls -la --time-style=full-iso <seam>/projects/ora-ops/events/`
> 6. `python3 -c "import os,sys; p='<seam>/_seam-probe.md'; s=os.stat(p); print(s.st_ino, s.st_nlink, hasattr(os,'link'), os.name, sys.platform)"`
> 7. In plain words: (a) is this path the same physical folder as `C:\Users\Owner\OneDrive - Oxford Capital Partners\Claude_Workspace\AAOS` on the host, and how do you know; (b) is this an interactive or a scheduled session; (c) anything that failed, was denied, or was slow.
>
> Stop after step 7.

## How the result is used
| Result | Consequence |
|---|---|
| Mount type is a VM share (virtiofs, 9p, or SMB-like) | Cowork is a distinct, less-trusted atomicity writer. A1a stays as planned (Cowork never holds the drain lock or a reclaim token). V1 must test `O_EXCL`, `os.link` and rename-over-existing across the mount before A1b/A6 are enabled for multi-device sync. |
| Mount is a native/bind mount of the host NTFS path | The locality precondition holds; AMD-0003 keeps the original "single NTFS volume" wording. |
| Clocks or timezone differ from the host | Confirms the M22 ordering fix is required; no lexical ordering across surfaces. |
| `os.link` unavailable (`hasattr` false or mount refuses) | Cowork-side tools may not use the hardlink publish path; spool and handoff writes must use the plain atomic-write path only. |
| Probe cannot be run or is denied | Record the reason; proceed with Code-only contenders for V1 (Q14-A fallback). |
