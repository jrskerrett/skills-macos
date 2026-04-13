---
name: macos
description: SSH into Jon's Mac Mini for remote commands, Xcode builds, iOS Simulator, and file transfers (SCP).
---

# macOS Skill

Run commands on Jon's Mac Mini over SSH; transfer files via SCP.

**Script:** `scripts/run.py`
**Host:** `jon@192.168.68.63` (Jons-Mac-mini.local)
**SSH key:** `~/.ssh/id_ed25519`

## Usage

```bash
python scripts/run.py "<command>"                                    # Run on Mac Mini
python scripts/run.py --cwd "~/code/ios-connector" "xcodebuild build" # With working dir
python scripts/run.py --timeout 300 "xcodebuild ..."                  # Custom timeout (default 60s, max 600s)
python scripts/run.py --push local_path "~/remote_path"               # SCP file Beast → Mac
python scripts/run.py --push-dir local_dir "~/remote_dir"             # SCP dir (recursive)
python scripts/run.py --pull "~/remote_path" local_path               # SCP file Mac → Beast
python scripts/run.py --json "<command>"                              # Raw JSON output
```

## Xcode Build Workflow

1. Edit files on Beast
2. Push via `--push` or `--push-dir`
3. Run `xcodebuild` via `scripts/run.py --cwd ... "xcodebuild ..."`
4. Diagnose errors, fix on Beast, repeat

## File Transfer Notes

- `--push` / `--push-dir`: Beast → Mac Mini
- `--pull`: Mac Mini → Beast
- Mac Mini paths use `~` for `/Users/jon`
- Large transfers may need `--timeout` increase

## Security Notes

- SSH key auth only, commands logged to `~/.openclaw/logs/macos-audit.log`
- Rate limit: 60 commands/hour
- Denylist blocks destructive patterns — use `--force` after confirming with Jon
