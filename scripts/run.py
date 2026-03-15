#!/usr/bin/env python3
"""
macOS remote execution script for OpenClaw macos skill.
Runs shell commands on Jon's Mac Mini over SSH.
Also supports pushing/pulling files via SCP.

Security features:
- Audit log: all commands logged to ~/.openclaw/logs/macos-audit.log
- Denylist: blocks obviously destructive patterns
- Rate limiting: max 60 commands/hour
- SSH key auth only — no passwords

Target: jon@192.168.68.52 (Jons-Mac-mini.local)

Usage:
  python run.py "<command>"
  python run.py --cwd "~/code/ios-connector" "<command>"
  python run.py --push /local/path "~/remote/path"
  python run.py --push-dir /local/dir "~/remote/dir"
  python run.py --pull "~/remote/path" /local/path
  python run.py --timeout 120 "<command>"
"""

import sys
import os
import json
import subprocess
import argparse
import shlex
import time
import re
from pathlib import Path
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────
MAC_HOST        = "192.168.68.52"
MAC_USER        = "jon"
MAC_TARGET      = f"{MAC_USER}@{MAC_HOST}"
SSH_KEY         = str(Path.home() / ".ssh" / "id_ed25519")

DEFAULT_TIMEOUT = 60     # builds take longer than bash commands
MAX_TIMEOUT     = 600    # 10 min max for full xcodebuild
MAX_OUTPUT      = 100_000

HOME            = Path.home()
LOG_DIR         = HOME / ".openclaw" / "logs"
AUDIT_LOG       = LOG_DIR / "macos-audit.log"
RATE_STATE      = LOG_DIR / "macos-rate.json"

RATE_LIMIT      = 60
RATE_WINDOW     = 3600

SSH_OPTS = [
    "-i", SSH_KEY,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",       # fail fast if key auth doesn't work
]

# ── Denylist ───────────────────────────────────────────────────────────────────
DENYLIST = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"dd\s+if=",
    r"mkfs\.",
    r":\(\)\{.*\}",
    r"chmod\s+-R\s+777\s+/",
    r"diskutil\s+erase",
    r"diskutil\s+zeroDisk",
    r"shred\s+",
]

# ── Audit logging ──────────────────────────────────────────────────────────────

def ensure_log_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)


def audit_log(action: str, detail: str, result: dict):
    ensure_log_dir()
    entry = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "action":  action,
        "detail":  detail,
        "rc":      result.get("returncode"),
        "success": result.get("success"),
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Rate limiting ──────────────────────────────────────────────────────────────

def check_rate_limit() -> tuple[bool, int]:
    ensure_log_dir()
    now = time.time()
    state = {"calls": []}
    if RATE_STATE.exists():
        try:
            state = json.loads(RATE_STATE.read_text())
        except Exception:
            pass
    state["calls"] = [t for t in state["calls"] if now - t < RATE_WINDOW]
    count = len(state["calls"])
    if count >= RATE_LIMIT:
        RATE_STATE.write_text(json.dumps(state))
        return False, count
    state["calls"].append(now)
    RATE_STATE.write_text(json.dumps(state))
    return True, count + 1


# ── Denylist check ─────────────────────────────────────────────────────────────

def check_denylist(command: str) -> str | None:
    for pattern in DENYLIST:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n... [truncated {len(text) - limit} chars] ...\n\n" + text[-half:]


def format_result(result: dict) -> str:
    lines = []
    if result.get("stdout"):
        lines.append(result["stdout"].rstrip())
    if result.get("stderr"):
        lines.append(f"[stderr]\n{result['stderr'].rstrip()}")
    if not result.get("success") and result.get("returncode", 0) != 0:
        lines.append(f"[exit code: {result['returncode']}]")
    return "\n".join(lines) if lines else "[no output]"


# ── SSH command execution ──────────────────────────────────────────────────────

def run_ssh(command: str, cwd: str = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a command on the Mac Mini over SSH."""
    # Wrap command with cd if cwd specified
    if cwd:
        # Use $HOME expansion instead of quoting ~ directly
        expanded_cwd = cwd.replace("~", "$HOME")
        full_command = f"cd {expanded_cwd} && {command}"
    else:
        full_command = command

    ssh_cmd = ["ssh"] + SSH_OPTS + [MAC_TARGET, full_command]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success":    result.returncode == 0,
            "returncode": result.returncode,
            "stdout":     truncate(result.stdout),
            "stderr":     truncate(result.stderr),
            "command":    command,
            "cwd":        cwd or "~",
            "host":       MAC_TARGET,
        }
    except subprocess.TimeoutExpired:
        return {
            "success":    False,
            "returncode": -1,
            "stdout":     "",
            "stderr":     f"SSH command timed out after {timeout}s",
            "command":    command,
            "cwd":        cwd or "~",
            "host":       MAC_TARGET,
        }
    except Exception as e:
        return {
            "success":    False,
            "returncode": -1,
            "stdout":     "",
            "stderr":     f"SSH error: {e}",
            "command":    command,
            "cwd":        cwd or "~",
            "host":       MAC_TARGET,
        }


# ── SCP file transfer ──────────────────────────────────────────────────────────

def run_scp_push(local_path: str, remote_path: str, recursive: bool = False, timeout: int = 120) -> dict:
    """Push a file or directory from Beast to Mac Mini."""
    scp_opts = ["-i", SSH_KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if recursive:
        scp_opts.append("-r")

    # Expand remote ~ to full path via SSH first, then SCP
    # Expand ~ to /Users/jon for SCP (SCP doesn't expand ~ in destinations)
    expanded_remote = remote_path.replace("~", "/Users/jon")
    remote_target = f"{MAC_TARGET}:{expanded_remote}"
    scp_cmd = ["scp"] + scp_opts + [local_path, remote_target]

    try:
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        action = "push-dir" if recursive else "push"
        return {
            "success":    result.returncode == 0,
            "returncode": result.returncode,
            "stdout":     truncate(result.stdout) or f"{'Directory' if recursive else 'File'} pushed to {remote_path}",
            "stderr":     truncate(result.stderr),
            "command":    f"scp {local_path} → {remote_target}",
            "cwd":        local_path,
            "host":       MAC_TARGET,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "returncode": -1, "stdout": "",
            "stderr": f"SCP timed out after {timeout}s — try increasing --timeout",
            "command": f"scp {local_path} → {remote_path}", "cwd": local_path, "host": MAC_TARGET,
        }
    except Exception as e:
        return {
            "success": False, "returncode": -1, "stdout": "",
            "stderr": f"SCP error: {e}",
            "command": f"scp {local_path} → {remote_path}", "cwd": local_path, "host": MAC_TARGET,
        }


def run_scp_pull(remote_path: str, local_path: str, timeout: int = 120) -> dict:
    """Pull a file from Mac Mini to Beast."""
    scp_opts = ["-i", SSH_KEY, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    expanded_remote = remote_path.replace("~", "/Users/jon")
    remote_source = f"{MAC_TARGET}:{expanded_remote}"
    scp_cmd = ["scp"] + scp_opts + [remote_source, local_path]

    try:
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success":    result.returncode == 0,
            "returncode": result.returncode,
            "stdout":     truncate(result.stdout) or f"File pulled to {local_path}",
            "stderr":     truncate(result.stderr),
            "command":    f"scp {remote_source} → {local_path}",
            "cwd":        local_path,
            "host":       MAC_TARGET,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False, "returncode": -1, "stdout": "",
            "stderr": f"SCP timed out after {timeout}s",
            "command": f"scp {remote_path} → {local_path}", "cwd": local_path, "host": MAC_TARGET,
        }
    except Exception as e:
        return {
            "success": False, "returncode": -1, "stdout": "",
            "stderr": f"SCP error: {e}",
            "command": f"scp {remote_path} → {local_path}", "cwd": local_path, "host": MAC_TARGET,
        }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run commands on Mac Mini over SSH")
    parser.add_argument("command",    nargs="?", help="Shell command to run on Mac Mini")
    parser.add_argument("--cwd",      help="Working directory on Mac Mini (default: ~)")
    parser.add_argument("--timeout",  type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json",     action="store_true", help="Output raw JSON")
    parser.add_argument("--force",    action="store_true", help="Bypass denylist")
    parser.add_argument("--push",     nargs=2, metavar=("LOCAL", "REMOTE"),
                        help="Push file: --push /local/path ~/remote/path")
    parser.add_argument("--push-dir", nargs=2, metavar=("LOCAL", "REMOTE"),
                        help="Push directory recursively: --push-dir /local/dir ~/remote/dir")
    parser.add_argument("--pull",     nargs=2, metavar=("REMOTE", "LOCAL"),
                        help="Pull file: --pull ~/remote/path /local/path")

    args = parser.parse_args()
    timeout = min(args.timeout, MAX_TIMEOUT)

    # ── Rate limit ─────────────────────────────────────────────────────────────
    allowed, count = check_rate_limit()
    if not allowed:
        msg = (
            f"[RATE LIMITED] {count} commands run in the last hour (limit: {RATE_LIMIT}).\n"
            "This may indicate a prompt injection loop. Tell Jon before continuing."
        )
        print(msg, file=sys.stderr)
        sys.exit(3)
    if count > RATE_LIMIT * 0.8:
        print(f"[WARNING] {count}/{RATE_LIMIT} commands used this hour.", file=sys.stderr)

    # ── File transfer modes ────────────────────────────────────────────────────
    if args.push:
        local, remote = args.push
        result = run_scp_push(local, remote, recursive=False, timeout=timeout)
        audit_log("push", f"{local} → {remote}", result)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(format_result(result))
        sys.exit(0 if result["success"] else 1)

    if args.push_dir:
        local, remote = args.push_dir
        result = run_scp_push(local, remote, recursive=True, timeout=timeout)
        audit_log("push-dir", f"{local} → {remote}", result)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(format_result(result))
        sys.exit(0 if result["success"] else 1)

    if args.pull:
        remote, local = args.pull
        result = run_scp_pull(remote, local, timeout=timeout)
        audit_log("pull", f"{remote} → {local}", result)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(format_result(result))
        sys.exit(0 if result["success"] else 1)

    # ── Command mode ───────────────────────────────────────────────────────────
    if args.command:
        command = args.command
    else:
        command = sys.stdin.read().strip()
        if not command:
            parser.print_help()
            sys.exit(1)

    # Denylist check
    if not args.force:
        matched = check_denylist(command)
        if matched:
            msg = (
                f"[BLOCKED] Command matches denylist pattern: {matched}\n"
                f"Command: {command}\n"
                f"If this is intentional, rerun with --force and confirm with Jon first."
            )
            print(msg, file=sys.stderr)
            result = {"success": False, "returncode": -2, "stdout": "", "stderr": msg,
                      "command": command, "cwd": args.cwd or "~", "host": MAC_TARGET}
            audit_log("cmd", command, result)
            sys.exit(2)

    result = run_ssh(command, cwd=args.cwd, timeout=timeout)
    audit_log("cmd", command, result)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_result(result))
        if not result["success"]:
            sys.exit(result["returncode"] if result["returncode"] != 0 else 1)


if __name__ == "__main__":
    main()
