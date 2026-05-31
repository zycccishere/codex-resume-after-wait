#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_PREFLIGHT_SECONDS = 20
DEFAULT_POLL_SECONDS = 15
DEFAULT_MAX_WAIT_SECONDS = 2 * 60 * 60
DEFAULT_RESUME_RETRY_DELAY_SECONDS = 20 * 60
DEFAULT_RESUME_RETRY_MAX_ATTEMPTS = 12
DEFAULT_STATE_DIR = "tmp/codex-wait-handoff"
ACTIVE_PHASES = {"scheduled", "watching"}
RETRYABLE_RESUME_PATTERNS = (
    "reconnecting...",
    "stream disconnected before completion",
    "error sending request for url",
    "backend-api/codex/responses",
    "connection reset",
    "connection timed out",
    "connection closed",
    "network is unreachable",
    "temporarily unavailable",
    "tls handshake",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        + timedelta(seconds=max(int(seconds), 0))
    ).isoformat().replace("+00:00", "Z")


def fatal(message: str, exit_code: int = 1) -> "NoReturn":
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
    tmp_path.replace(path)


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def read_text_from_offset(path: Path, offset: int, max_chars: int = 20000) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(max(offset, 0))
        text = handle.read()
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def retryable_resume_failure(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(pattern in lowered for pattern in RETRYABLE_RESUME_PATTERNS)


def session_lock_key(session_id: str) -> str:
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
    return digest[:20]


def task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def ensure_state_dirs(base_dir: Path) -> dict[str, Path]:
    paths = {
        "base": base_dir,
        "tasks": base_dir / "tasks",
        "prompts": base_dir / "prompts",
        "logs": base_dir / "logs",
        "locks": base_dir / "locks",
        "outputs": base_dir / "outputs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_command(command: list[str], timeout_seconds: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def process_rows() -> list[dict[str, Any]]:
    result = run_command(["ps", "-Ao", "pid=,ppid=,command="], timeout_seconds=10)
    if result.returncode != 0:
        fatal(result.stderr.strip() or result.stdout.strip() or f"ps exited with {result.returncode}")
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid_value = int(parts[0])
            ppid_value = int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid_value, "ppid": ppid_value, "command": parts[2]})
    return rows


def descendants_by_pid(rows: list[dict[str, Any]], root_pid: int) -> list[int]:
    children_by_ppid: dict[int, list[int]] = {}
    for row in rows:
        children_by_ppid.setdefault(int(row["ppid"]), []).append(int(row["pid"]))
    stack = [int(root_pid)]
    seen: set[int] = set()
    ordered: list[int] = []
    while stack:
        current = stack.pop()
        for child in children_by_ppid.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            stack.append(child)
    return ordered


def self_and_ancestor_pids(rows: list[dict[str, Any]], pid: int | None = None) -> list[int]:
    current_pid = int(pid or os.getpid())
    parent_by_pid = {int(row["pid"]): int(row["ppid"]) for row in rows}
    protected: list[int] = []
    seen: set[int] = set()
    probe = current_pid
    while probe > 0 and probe not in seen:
        seen.add(probe)
        protected.append(probe)
        next_probe = parent_by_pid.get(probe)
        if next_probe is None or next_probe == probe:
            break
        probe = next_probe
    return protected


def task_related_pids(task_payload: dict[str, Any], rows: list[dict[str, Any]]) -> list[int]:
    task_id_value = str(task_payload.get("task_id") or "")
    if not task_id_value:
        return []
    matched: set[int] = set()
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    row_by_pid = {int(row["pid"]): row for row in rows}
    if watcher_pid and watcher_pid in row_by_pid:
        matched.add(watcher_pid)
        matched.update(descendants_by_pid(rows, watcher_pid))
    for row in rows:
        command_text = str(row["command"])
        if task_id_value in command_text:
            matched.add(int(row["pid"]))
    return sorted(matched)


def task_runtime_snapshot(task_payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched_pids = task_related_pids(task_payload, rows)
    row_by_pid = {int(row["pid"]): row for row in rows}
    matched_rows = [row_by_pid[pid] for pid in matched_pids if pid in row_by_pid]
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    watcher_alive = watcher_pid in row_by_pid
    resume_pids = [
        int(row["pid"])
        for row in matched_rows
        if "codex exec resume" in str(row["command"])
    ]
    return {
        "task_id": task_payload.get("task_id"),
        "phase": task_payload.get("phase"),
        "target": task_payload.get("target"),
        "watcher_pid": watcher_pid or None,
        "watcher_alive": watcher_alive,
        "resume_pids": sorted(resume_pids),
        "related_pids": matched_pids,
        "note": task_payload.get("note") or "",
    }


def active_and_stale_task_snapshots(tasks_dir: Path, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_entries: list[dict[str, Any]] = []
    stale_entries: list[dict[str, Any]] = []
    for task_file in sorted(tasks_dir.glob("*.json")):
        task_payload = load_json(task_file)
        snapshot = task_runtime_snapshot(task_payload, rows)
        if snapshot["related_pids"]:
            active_entries.append(snapshot)
            continue
        if task_payload.get("phase") in {"scheduled", "watching", "resume_started"}:
            stale_entries.append(snapshot)
    return active_entries, stale_entries


def terminate_pids(
    pids: list[int],
    grace_seconds: float = 3.0,
    exclude_pids: list[int] | None = None,
) -> dict[str, Any]:
    excluded = sorted(set(int(pid) for pid in (exclude_pids or []) if int(pid) > 0))
    live = [
        pid
        for pid in sorted(set(int(pid) for pid in pids if int(pid) > 0))
        if pid not in excluded and pid_exists(int(pid))
    ]
    if not live:
        return {
            "requested_pids": sorted(set(int(pid) for pid in pids if int(pid) > 0)),
            "excluded_pids": excluded,
            "terminated_pids": [],
            "still_alive_pids": [],
        }

    for pid in reversed(live):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        still_alive = [pid for pid in live if pid_exists(pid)]
        if not still_alive:
            return {"requested_pids": live, "terminated_pids": live, "still_alive_pids": []}
        time.sleep(0.2)

    still_alive = [pid for pid in live if pid_exists(pid)]
    for pid in reversed(still_alive):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
    time.sleep(0.2)
    final_alive = [pid for pid in live if pid_exists(pid)]
    terminated = [pid for pid in live if pid not in final_alive]
    return {
        "requested_pids": live,
        "excluded_pids": excluded,
        "terminated_pids": terminated,
        "still_alive_pids": final_alive,
    }


def ssh_command(host: str, remote_command: str, timeout_seconds: int | None = 30) -> subprocess.CompletedProcess[str]:
    return run_command(["ssh", host, remote_command], timeout_seconds=timeout_seconds)


def probe_local_pid(pid: int) -> tuple[str, str]:
    return ("alive", f"pid {pid} exists") if pid_exists(pid) else ("dead", f"pid {pid} not found")


def probe_local_pattern(pattern: str) -> tuple[str, str]:
    result = run_command(["pgrep", "-af", "--", pattern], timeout_seconds=10)
    if result.returncode == 0:
        return ("alive", result.stdout.strip())
    if result.returncode == 1:
        return ("dead", f"no local process matched pattern {pattern!r}")
    detail = result.stderr.strip() or result.stdout.strip() or f"pgrep exited with {result.returncode}"
    return ("unknown", detail)


def probe_remote_pid(host: str, pid: int) -> tuple[str, str]:
    command = f"ps -o stat= -p {pid} | head -n 1"
    try:
        result = ssh_command(host, command)
    except subprocess.TimeoutExpired:
        return ("unknown", f"ssh timeout while probing {host}:{pid}")
    if result.returncode == 0:
        state = result.stdout.strip()
        if not state or state.startswith("Z"):
            return ("dead", f"remote pid {pid} is absent or zombie on {host}")
        return ("alive", f"{host} pid {pid} state {state}")
    if result.returncode == 255:
        detail = result.stderr.strip() or f"ssh returned {result.returncode}"
        return ("unknown", detail)
    return ("dead", result.stderr.strip() or f"remote pid {pid} not found on {host}")


def probe_remote_pattern(host: str, pattern: str) -> tuple[str, str]:
    command = f"pgrep -af -- {shlex.quote(pattern)}"
    try:
        result = ssh_command(host, command)
    except subprocess.TimeoutExpired:
        return ("unknown", f"ssh timeout while probing pattern on {host}")
    if result.returncode == 0:
        return ("alive", result.stdout.strip())
    if result.returncode == 1:
        return ("dead", f"no remote process matched pattern {pattern!r} on {host}")
    if result.returncode == 255:
        detail = result.stderr.strip() or f"ssh returned {result.returncode}"
        return ("unknown", detail)
    return ("unknown", result.stderr.strip() or result.stdout.strip() or f"pgrep exited with {result.returncode}")


def stop_local_pid(pid: int) -> dict[str, Any]:
    termination = terminate_pids([pid])
    return {
        "scope": "local",
        "mode": "pid",
        "requested_pid": pid,
        "terminated_pids": termination["terminated_pids"],
        "still_alive_pids": termination["still_alive_pids"],
        "status": "stopped" if not termination["still_alive_pids"] else "still_alive",
    }


def stop_local_pattern(pattern: str) -> dict[str, Any]:
    result = run_command(["pgrep", "-af", "--", pattern], timeout_seconds=10)
    if result.returncode == 1:
        return {
            "scope": "local",
            "mode": "pattern",
            "pattern": pattern,
            "matched_pids": [],
            "terminated_pids": [],
            "still_alive_pids": [],
            "status": "already_absent",
        }
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"pgrep exited with {result.returncode}"
        return {"scope": "local", "mode": "pattern", "pattern": pattern, "status": "probe_failed", "detail": detail}
    matched_pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        try:
            matched_pids.append(int(parts[0]))
        except ValueError:
            continue
    termination = terminate_pids(matched_pids)
    return {
        "scope": "local",
        "mode": "pattern",
        "pattern": pattern,
        "matched_pids": matched_pids,
        "terminated_pids": termination["terminated_pids"],
        "still_alive_pids": termination["still_alive_pids"],
        "status": "stopped" if not termination["still_alive_pids"] else "still_alive",
    }


def stop_remote_pid(host: str, pid: int) -> dict[str, Any]:
    command = (
        f"if ps -o stat= -p {pid} | head -n 1 >/dev/null 2>&1; then "
        f"kill -TERM {pid} 2>/dev/null || true; "
        f"sleep 1; "
        f"if ps -o stat= -p {pid} | head -n 1 >/dev/null 2>&1; then "
        f"kill -KILL {pid} 2>/dev/null || true; "
        f"sleep 1; "
        f"fi; "
        f"if ps -o stat= -p {pid} | head -n 1 >/dev/null 2>&1; then "
        f"echo still_alive; "
        f"else echo stopped; fi; "
        f"else echo already_absent; fi"
    )
    try:
        result = ssh_command(host, command, timeout_seconds=30)
    except subprocess.TimeoutExpired:
        return {"scope": "remote", "mode": "pid", "host": host, "requested_pid": pid, "status": "probe_failed", "detail": "ssh timeout"}
    detail = result.stderr.strip() or result.stdout.strip()
    if result.returncode == 255:
        return {"scope": "remote", "mode": "pid", "host": host, "requested_pid": pid, "status": "probe_failed", "detail": detail or "ssh returned 255"}
    status = (result.stdout.strip().splitlines() or ["unknown"])[-1].strip()
    return {
        "scope": "remote",
        "mode": "pid",
        "host": host,
        "requested_pid": pid,
        "status": status if status in {"stopped", "still_alive", "already_absent"} else "unknown",
        "detail": detail,
    }


def stop_remote_pattern(host: str, pattern: str) -> dict[str, Any]:
    quoted_pattern = shlex.quote(pattern)
    command = (
        f"pids=$(pgrep -f -- {quoted_pattern} | tr '\\n' ' '); "
        f"if [ -z \"$pids\" ]; then echo already_absent; exit 0; fi; "
        f"kill -TERM $pids 2>/dev/null || true; "
        f"sleep 1; "
        f"alive=$(pgrep -f -- {quoted_pattern} | tr '\\n' ' '); "
        f"if [ -n \"$alive\" ]; then kill -KILL $alive 2>/dev/null || true; sleep 1; fi; "
        f"final=$(pgrep -f -- {quoted_pattern} | tr '\\n' ' '); "
        f"if [ -n \"$final\" ]; then echo still_alive:$final; else echo stopped:$pids; fi"
    )
    try:
        result = ssh_command(host, command, timeout_seconds=30)
    except subprocess.TimeoutExpired:
        return {"scope": "remote", "mode": "pattern", "host": host, "pattern": pattern, "status": "probe_failed", "detail": "ssh timeout"}
    detail = result.stderr.strip() or result.stdout.strip()
    if result.returncode == 255:
        return {"scope": "remote", "mode": "pattern", "host": host, "pattern": pattern, "status": "probe_failed", "detail": detail or "ssh returned 255"}
    last_line = (result.stdout.strip().splitlines() or ["unknown"])[-1].strip()
    status = last_line.split(":", 1)[0]
    return {
        "scope": "remote",
        "mode": "pattern",
        "host": host,
        "pattern": pattern,
        "status": status if status in {"stopped", "still_alive", "already_absent"} else "unknown",
        "detail": detail,
    }


def stop_target(target: dict[str, Any]) -> dict[str, Any]:
    scope = target["scope"]
    mode = target["mode"]
    if scope == "local" and mode == "pid":
        return stop_local_pid(int(target["pid"]))
    if scope == "local" and mode == "pattern":
        return stop_local_pattern(str(target["pattern"]))
    if scope == "remote" and mode == "pid":
        return stop_remote_pid(str(target["host"]), int(target["pid"]))
    if scope == "remote" and mode == "pattern":
        return stop_remote_pattern(str(target["host"]), str(target["pattern"]))
    raise ValueError(f"Unsupported target: {target}")


def probe_target(target: dict[str, Any]) -> tuple[str, str]:
    scope = target["scope"]
    mode = target["mode"]
    if scope == "local" and mode == "pid":
        return probe_local_pid(int(target["pid"]))
    if scope == "local" and mode == "pattern":
        return probe_local_pattern(str(target["pattern"]))
    if scope == "remote" and mode == "pid":
        return probe_remote_pid(str(target["host"]), int(target["pid"]))
    if scope == "remote" and mode == "pattern":
        return probe_remote_pattern(str(target["host"]), str(target["pattern"]))
    raise ValueError(f"Unsupported target: {target}")


def build_target(args: argparse.Namespace) -> dict[str, Any]:
    has_pid = args.pid is not None
    has_pattern = args.pattern is not None
    if has_pid == has_pattern:
        fatal("Exactly one of --pid or --pattern is required.")

    target: dict[str, Any] = {
        "scope": "remote" if args.host else "local",
        "mode": "pid" if has_pid else "pattern",
    }
    if args.host:
        target["host"] = args.host
    if has_pid:
        target["pid"] = int(args.pid)
    if has_pattern:
        target["pattern"] = args.pattern
    return target


def target_summary(target: dict[str, Any]) -> str:
    scope = target["scope"]
    mode = target["mode"]
    prefix = f"{scope} "
    if scope == "remote":
        prefix += f"{target['host']} "
    if mode == "pid":
        return prefix + f"pid {target['pid']}"
    return prefix + f"pattern {target['pattern']!r}"


def build_observed_log(args: argparse.Namespace, target: dict[str, Any], current_cwd: Path) -> dict[str, Any] | None:
    if not args.observed_log:
        if args.observed_log_host or args.observed_log_label:
            fatal("--observed-log-host and --observed-log-label require --observed-log.")
        return None

    host = args.observed_log_host or (str(target["host"]) if target.get("scope") == "remote" else "")
    scope = "remote" if host else "local"
    raw_path = str(args.observed_log)
    if scope == "remote":
        if not raw_path.startswith("/"):
            fatal("--observed-log must be an absolute remote path when the observed log is remote.")
        return {
            "scope": "remote",
            "host": host,
            "path": raw_path,
            "label": args.observed_log_label or "Observed Log",
        }

    local_path = Path(raw_path).expanduser()
    if not local_path.is_absolute():
        local_path = current_cwd / local_path
    return {
        "scope": "local",
        "path": str(local_path.resolve()),
        "label": args.observed_log_label or "Observed Log",
    }


def observed_log_summary(observed_log: dict[str, Any] | None) -> str:
    if not observed_log:
        return ""
    scope = observed_log.get("scope") or "local"
    label = observed_log.get("label") or "Observed Log"
    if scope == "remote":
        return f"{label}: remote {observed_log.get('host')}:{observed_log.get('path')}"
    return f"{label}: local {observed_log.get('path')}"


def format_duration_brief(total_seconds: int) -> str:
    seconds = max(int(total_seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def build_resume_prompt(
    task_id_value: str,
    task_file: Path,
    target: dict[str, Any],
    observed_log: dict[str, Any] | None,
    note: str | None,
    prompt_text: str | None,
    completion_reason: str,
    wait_elapsed_seconds: int,
    max_wait_seconds: int,
    completion_detail: str | None = None,
) -> str:
    lines = ["The scheduled blocking wait has completed.", ""]
    if completion_reason == "process_exited":
        lines.extend(
            [
                "Resume reason: watched process exited.",
                (
                    f"The watched process exited after about {format_duration_brief(wait_elapsed_seconds)}. "
                    "Read the task metadata, collect outputs, update the relevant workspace artifacts, "
                    "and continue from the blocked step."
                ),
            ]
        )
    elif completion_reason == "max_wait_reached":
        lines.extend(
            [
                "Resume reason: maximum wait time reached before the watched process was confirmed exited.",
                (
                    f"The watcher waited about {format_duration_brief(wait_elapsed_seconds)} "
                    f"(configured limit: {format_duration_brief(max_wait_seconds)}) and resumed anyway."
                ),
                (
                    "Do not assume the task finished successfully. First confirm whether the run is still "
                    "healthy and progressing as expected. If it is healthy, continue the monitoring workflow "
                    "and schedule another blocking wait on the same precise target. If it is unhealthy, stuck, "
                    "or off the rails, diagnose the issue, fix it, relaunch if needed, and only then schedule "
                    "a new blocking wait."
                ),
            ]
        )
    else:
        lines.extend(
            [
                f"Resume reason: {completion_reason}.",
                (
                    "Inspect the task metadata and current run state before proceeding, then either continue "
                    "the blocked workflow or repair and relaunch as needed."
                ),
            ]
        )
    lines.extend(
        [
            "",
            f"task_id: {task_id_value}",
            f"task_file: {task_file}",
            f"watched_target: {target_summary(target)}",
            f"wait_elapsed_seconds: {int(wait_elapsed_seconds)}",
            f"max_wait_seconds: {int(max_wait_seconds)}",
            f"completion_reason: {completion_reason}",
        ]
    )
    observed_log_text = observed_log_summary(observed_log)
    if observed_log_text:
        lines.append(f"observed_log: {observed_log_text}")
    if completion_detail:
        lines.append(f"completion_detail: {completion_detail}")
    if note:
        lines.extend(["", "Scheduler note:", note.strip()])
    if prompt_text:
        lines.extend(["", "Continuation instructions:", prompt_text.strip()])
    return "\n".join(lines).strip() + "\n"


def load_prompt_text(args: argparse.Namespace) -> str | None:
    if args.resume_prompt_file:
        prompt_path = Path(args.resume_prompt_file).expanduser().resolve()
        if not prompt_path.exists():
            fatal(f"Resume prompt file does not exist: {prompt_path}")
        return read_text(prompt_path).strip()
    if args.resume_prompt:
        return args.resume_prompt.strip()
    return None


def sanitize_session_id(session_id: str | None) -> str:
    value = session_id or os.environ.get("CODEX_THREAD_ID")
    if not value:
        fatal("No session id available. Pass --session-id or run this from Codex so CODEX_THREAD_ID is set.")
    return value


def active_task_from_lock(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    try:
        lock_payload = load_json(lock_path)
    except json.JSONDecodeError:
        lock_path.unlink(missing_ok=True)
        return None

    task_file_value = lock_payload.get("task_file")
    if not task_file_value:
        lock_path.unlink(missing_ok=True)
        return None
    task_file = Path(task_file_value)
    if not task_file.exists():
        lock_path.unlink(missing_ok=True)
        return None

    try:
        task_payload = load_json(task_file)
    except json.JSONDecodeError:
        lock_path.unlink(missing_ok=True)
        return None
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    if task_payload.get("phase") in ACTIVE_PHASES and watcher_pid and pid_exists(watcher_pid):
        return task_payload

    lock_path.unlink(missing_ok=True)
    return None


def acquire_session_lock(base_dir: Path, session_id: str, task_file: Path) -> Path:
    lock_path = base_dir / "locks" / f"{session_lock_key(session_id)}.json"
    active_task = active_task_from_lock(lock_path)
    if active_task:
        fatal(
            "This Codex session already has an active wait handoff running.\n"
            f"active_task_id: {active_task.get('task_id')}\n"
            f"task_file: {active_task.get('task_file')}"
        )

    payload = {
        "session_id": session_id,
        "task_file": str(task_file),
        "created_at": now_utc(),
    }
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        active_task = active_task_from_lock(lock_path)
        if active_task:
            fatal(
                "This Codex session already has an active wait handoff running.\n"
                f"active_task_id: {active_task.get('task_id')}\n"
                f"task_file: {active_task.get('task_file')}"
            )
        with lock_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return lock_path


def release_session_lock(lock_path: Path | None, task_file: Path | None = None) -> None:
    if not lock_path:
        return
    if not lock_path.exists():
        return
    if task_file is None:
        lock_path.unlink(missing_ok=True)
        return
    try:
        payload = load_json(lock_path)
    except json.JSONDecodeError:
        lock_path.unlink(missing_ok=True)
        return
    if payload.get("task_file") == str(task_file):
        lock_path.unlink(missing_ok=True)


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def do_preflight(target: dict[str, Any], preflight_seconds: int) -> tuple[str, str]:
    deadline = time.monotonic() + max(preflight_seconds, 0)
    while True:
        state, detail = probe_target(target)
        if state != "alive":
            return (state, detail)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (state, detail)
        time.sleep(min(1.0, remaining))


def command_schedule(args: argparse.Namespace) -> int:
    if not args.blocking:
        fatal("Refusing to schedule a handoff without --blocking. Use this only for genuinely blocking waits.")
    if args.expected_seconds < 300 and not args.allow_short_test:
        fatal(
            "Refusing to schedule a handoff for a task under 5 minutes. "
            "Use sleep directly for short waits, or pass --allow-short-test for explicit iteration-only testing."
        )

    session_id = sanitize_session_id(args.session_id)
    target = build_target(args)
    preflight_seconds = max(args.preflight_seconds, 0)
    state_dirs = ensure_state_dirs(Path(args.state_dir).expanduser().resolve())
    current_cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    observed_log = build_observed_log(args, target, current_cwd)

    preflight_state, preflight_detail = do_preflight(target, preflight_seconds)
    if preflight_state == "dead":
        emit(
            {
                "status": "finished_during_preflight",
                "detail": preflight_detail,
                "target": target_summary(target),
                "preflight_seconds": preflight_seconds,
            },
            args.json,
        )
        return 3
    if preflight_state == "unknown":
        fatal(f"Preflight could not verify the target: {preflight_detail}")

    task_id_value = task_id()
    task_file = state_dirs["tasks"] / f"{task_id_value}.json"
    prompt_text = load_prompt_text(args)
    max_wait_seconds = int(args.max_wait_seconds)
    if max_wait_seconds <= 0:
        fatal("--max-wait-seconds must be a positive integer.")
    lock_path = acquire_session_lock(state_dirs["base"], session_id, task_file)
    prompt_file = state_dirs["prompts"] / f"{task_id_value}.prompt.txt"
    log_file = state_dirs["logs"] / f"{task_id_value}.watch.log"
    resume_log_file = state_dirs["logs"] / f"{task_id_value}.resume.log"
    last_message_file = state_dirs["outputs"] / f"{task_id_value}.last-message.txt"

    resume_prompt = build_resume_prompt(
        task_id_value=task_id_value,
        task_file=task_file,
        target=target,
        observed_log=observed_log,
        note=args.note,
        prompt_text=prompt_text,
        completion_reason="process_exited",
        wait_elapsed_seconds=0,
        max_wait_seconds=max_wait_seconds,
    )
    write_text(prompt_file, resume_prompt)

    task_payload: dict[str, Any] = {
        "task_id": task_id_value,
        "phase": "scheduled",
        "created_at": now_utc(),
        "session_id": session_id,
        "session_lock": str(lock_path),
        "target": target,
        "observed_log": observed_log,
        "expected_seconds": args.expected_seconds,
        "max_wait_seconds": max_wait_seconds,
        "allow_short_test": bool(args.allow_short_test),
        "poll_seconds": args.poll_seconds,
        "preflight_seconds": preflight_seconds,
        "resume_cwd": str(current_cwd),
        "resume_skip_git_repo_check": True,
        "prompt_file": str(prompt_file),
        "log_file": str(log_file),
        "resume_log_file": str(resume_log_file),
        "last_message_file": str(last_message_file),
        "task_file": str(task_file),
        "dry_run_resume": bool(args.dry_run_resume),
        "resume_bypass_approvals_and_sandbox": not bool(args.resume_preserve_approvals_and_sandbox),
        "resume_retry_delay_seconds": max(int(args.resume_retry_delay_seconds), 1),
        "resume_retry_max_attempts": max(int(args.resume_retry_max_attempts), 0),
        "continuation_prompt_text": prompt_text or "",
        "note": args.note or "",
    }
    write_json(task_file, task_payload)

    watcher_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--task-file",
        str(task_file),
    ]
    try:
        with log_file.open("a", encoding="utf-8") as log_handle:
            watcher = subprocess.Popen(
                watcher_command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception:
        release_session_lock(lock_path, task_file)
        raise

    task_payload["watcher_pid"] = watcher.pid
    task_payload["watch_started_at"] = now_utc()
    write_json(task_file, task_payload)

    emit(
        {
            "status": "scheduled",
            "task_id": task_id_value,
            "task_file": str(task_file),
            "watcher_pid": watcher.pid,
            "watch_log": str(log_file),
            "resume_log": str(resume_log_file),
            "last_message_file": str(last_message_file),
            "target": target_summary(target),
            "max_wait_seconds": max_wait_seconds,
        },
        args.json,
    )
    return 0


def command_watch(args: argparse.Namespace) -> int:
    task_file = Path(args.task_file).expanduser().resolve()
    task_payload = load_json(task_file)
    lock_path = Path(task_payload["session_lock"])
    target = task_payload["target"]
    poll_seconds = max(int(task_payload.get("poll_seconds", DEFAULT_POLL_SECONDS)), 1)
    max_wait_seconds = max(int(task_payload.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS)), 1)
    watch_started_monotonic = time.monotonic()
    completion_reason = "process_exited"
    completion_detail = ""
    last_probe_state = "unknown"
    last_probe_detail = ""
    log_prefix = f"[{now_utc()}] task {task_payload['task_id']}"
    print(f"{log_prefix} watching {target_summary(target)}", flush=True)

    task_payload["phase"] = "watching"
    task_payload["watch_loop_started_at"] = now_utc()
    write_json(task_file, task_payload)

    while True:
        state, detail = probe_target(target)
        last_probe_state = state
        last_probe_detail = detail
        print(f"[{now_utc()}] probe={state} detail={detail}", flush=True)
        if state == "dead":
            completion_reason = "process_exited"
            completion_detail = detail
            break
        wait_elapsed_seconds = int(time.monotonic() - watch_started_monotonic)
        if wait_elapsed_seconds >= max_wait_seconds:
            completion_reason = "max_wait_reached"
            completion_detail = f"last_probe_state={state}; last_probe_detail={detail}"
            print(
                f"[{now_utc()}] max_wait_reached elapsed={wait_elapsed_seconds}s limit={max_wait_seconds}s",
                flush=True,
            )
            break
        if state == "unknown":
            time.sleep(min(poll_seconds, 5, max(max_wait_seconds - wait_elapsed_seconds, 1)))
            continue
        time.sleep(min(poll_seconds, max(max_wait_seconds - wait_elapsed_seconds, 1)))

    task_payload = load_json(task_file)
    wait_elapsed_seconds = int(time.monotonic() - watch_started_monotonic)
    task_payload["phase"] = "completed" if completion_reason == "process_exited" else "max_wait_reached"
    task_payload["completed_at"] = now_utc()
    task_payload["completion_reason"] = completion_reason
    task_payload["completion_detail"] = completion_detail
    task_payload["wait_elapsed_seconds"] = wait_elapsed_seconds
    task_payload["last_probe_state"] = last_probe_state
    task_payload["last_probe_detail"] = last_probe_detail
    write_json(task_file, task_payload)
    release_session_lock(lock_path, task_file)

    if task_payload.get("dry_run_resume"):
        task_payload["phase"] = "resume_dry_run_complete"
        task_payload["resume_completed_at"] = now_utc()
        write_json(task_file, task_payload)
        print(f"[{now_utc()}] dry-run resume completed", flush=True)
        return 0

    prompt_text = build_resume_prompt(
        task_id_value=task_payload["task_id"],
        task_file=task_file,
        target=target,
        observed_log=task_payload.get("observed_log"),
        note=task_payload.get("note") or None,
        prompt_text=(task_payload.get("continuation_prompt_text") or "").strip() or None,
        completion_reason=str(task_payload.get("completion_reason") or "process_exited"),
        wait_elapsed_seconds=int(task_payload.get("wait_elapsed_seconds") or 0),
        max_wait_seconds=max_wait_seconds,
        completion_detail=task_payload.get("completion_detail") or None,
    )
    write_text(Path(task_payload["prompt_file"]), prompt_text)
    resume_log_file = Path(task_payload["resume_log_file"])
    last_message_file = Path(task_payload["last_message_file"])
    resume_cwd = Path(task_payload["resume_cwd"])

    resume_command = [
        os.environ.get("CODEX_WAIT_CODEX_BIN", "codex"),
        "exec",
        "resume",
    ]
    if task_payload.get("resume_bypass_approvals_and_sandbox", False):
        resume_command.append("--dangerously-bypass-approvals-and-sandbox")
    if task_payload.get("resume_skip_git_repo_check", True):
        resume_command.append("--skip-git-repo-check")
    resume_command.extend([
        "--output-last-message",
        str(last_message_file),
        task_payload["session_id"],
        "-",
    ])
    retry_delay_seconds = max(
        int(task_payload.get("resume_retry_delay_seconds", DEFAULT_RESUME_RETRY_DELAY_SECONDS) or 0),
        1,
    )
    retry_max_attempts = max(
        int(task_payload.get("resume_retry_max_attempts", DEFAULT_RESUME_RETRY_MAX_ATTEMPTS) or 0),
        0,
    )
    resume_attempts = list(task_payload.get("resume_attempts") or [])
    attempt = 0
    while True:
        attempt += 1
        started_at = now_utc()
        task_payload = load_json(task_file)
        task_payload["phase"] = "resume_started"
        task_payload["resume_started_at"] = task_payload.get("resume_started_at") or started_at
        task_payload["resume_last_attempt_started_at"] = started_at
        task_payload["resume_attempt"] = attempt
        task_payload["resume_retry_delay_seconds"] = retry_delay_seconds
        task_payload["resume_retry_max_attempts"] = retry_max_attempts
        write_json(task_file, task_payload)

        log_offset = resume_log_file.stat().st_size if resume_log_file.exists() else 0
        print(
            f"[{now_utc()}] resuming session {task_payload['session_id']} attempt={attempt}",
            flush=True,
        )
        with resume_log_file.open("a", encoding="utf-8") as resume_log:
            resume_log.write(f"\n[{now_utc()}] codex resume attempt {attempt} start\n")
            resume_log.flush()
            result = subprocess.run(
                resume_command,
                input=prompt_text,
                text=True,
                stdout=resume_log,
                stderr=subprocess.STDOUT,
                cwd=resume_cwd,
                check=False,
            )
            resume_log.write(f"\n[{now_utc()}] codex resume attempt {attempt} returncode={result.returncode}\n")
            resume_log.flush()

        attempt_log = read_text_from_offset(resume_log_file, log_offset)
        retryable = result.returncode != 0 and retryable_resume_failure(attempt_log)
        completed_at = now_utc()
        attempt_record = {
            "attempt": attempt,
            "started_at": started_at,
            "completed_at": completed_at,
            "returncode": result.returncode,
            "retryable": retryable,
        }
        resume_attempts.append(attempt_record)

        task_payload = load_json(task_file)
        task_payload["resume_returncode"] = result.returncode
        task_payload["resume_completed_at"] = completed_at
        task_payload["resume_last_attempt_completed_at"] = completed_at
        task_payload["resume_attempts"] = resume_attempts

        if result.returncode == 0:
            task_payload["phase"] = "resume_ok"
            write_json(task_file, task_payload)
            print(f"[{now_utc()}] resume returncode=0 attempt={attempt}", flush=True)
            return 0

        attempts_remaining = retry_max_attempts == 0 or attempt < retry_max_attempts
        if retryable and attempts_remaining:
            task_payload["phase"] = "resume_retry_waiting"
            task_payload["resume_retry_reason"] = "retryable_network_disconnect"
            task_payload["resume_next_retry_delay_seconds"] = retry_delay_seconds
            task_payload["resume_next_retry_after"] = utc_after(retry_delay_seconds)
            write_json(task_file, task_payload)
            print(
                f"[{now_utc()}] resume returncode={result.returncode} attempt={attempt}; "
                f"detected retryable network disconnect; retrying in {retry_delay_seconds}s",
                flush=True,
            )
            time.sleep(retry_delay_seconds)
            continue

        task_payload["phase"] = "resume_failed"
        task_payload["resume_retry_reason"] = (
            "retryable_network_disconnect_attempts_exhausted" if retryable else "non_retryable_resume_failure"
        )
        write_json(task_file, task_payload)
        print(f"[{now_utc()}] resume returncode={result.returncode} attempt={attempt}", flush=True)
        return result.returncode


def command_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    tasks_dir = state_dir / "tasks"
    if not tasks_dir.exists():
        fatal(f"State directory does not exist: {state_dir}")

    if args.task_id:
        task_file = tasks_dir / f"{args.task_id}.json"
        if not task_file.exists():
            fatal(f"Task not found: {args.task_id}")
        emit(load_json(task_file), args.json)
        return 0

    if args.session_id:
        lock_path = state_dir / "locks" / f"{session_lock_key(args.session_id)}.json"
        active_task = active_task_from_lock(lock_path)
        if not active_task:
            emit({"status": "no_active_task", "session_id": args.session_id}, args.json)
            return 0
        emit(active_task, args.json)
        return 0

    task_files = sorted(tasks_dir.glob("*.json"))
    payload = {
        "tasks": [load_json(task_file) for task_file in task_files],
        "count": len(task_files),
    }
    emit(payload, args.json)
    return 0


def command_active(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    tasks_dir = state_dir / "tasks"
    if not tasks_dir.exists():
        fatal(f"State directory does not exist: {state_dir}")

    rows = process_rows()
    active_entries, stale_entries = active_and_stale_task_snapshots(tasks_dir, rows)

    payload = {
        "active_tasks": active_entries,
        "active_count": len(active_entries),
    }
    if args.include_stale:
        payload["stale_tasks"] = stale_entries
        payload["stale_count"] = len(stale_entries)
    emit(payload, args.json)
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    task_payload = load_json(task_file)
    rows = process_rows()
    runtime = task_runtime_snapshot(task_payload, rows)
    protected_pids = self_and_ancestor_pids(rows)
    termination = terminate_pids(runtime["related_pids"], exclude_pids=protected_pids)
    lock_path = Path(task_payload.get("session_lock", ""))
    release_session_lock(lock_path, task_file)
    task_payload["phase"] = "cancelled"
    task_payload["cancelled_at"] = now_utc()
    task_payload["protected_pids_during_cancel"] = termination.get("excluded_pids", [])
    task_payload["stopped_related_pids"] = termination["terminated_pids"]
    task_payload["still_alive_pids_after_cancel"] = termination["still_alive_pids"]
    task_payload["watcher_exited_after_cancel"] = not bool(termination["still_alive_pids"])
    write_json(task_file, task_payload)
    emit(
        {
            "status": "cancelled",
            "task_id": args.task_id,
            "watcher_pid": runtime["watcher_pid"] or "none",
            "watcher_exited": not bool(termination["still_alive_pids"]),
            "protected_pids": termination.get("excluded_pids", []),
            "stopped_related_pids": termination["terminated_pids"],
            "still_alive_pids": termination["still_alive_pids"],
        },
        args.json,
    )
    return 0


def stop_single_task(task_file: Path, also_stop_target: bool) -> dict[str, Any]:
    task_payload = load_json(task_file)
    rows = process_rows()
    runtime = task_runtime_snapshot(task_payload, rows)
    protected_pids = self_and_ancestor_pids(rows)
    termination = terminate_pids(runtime["related_pids"], exclude_pids=protected_pids)
    lock_path = Path(task_payload.get("session_lock", ""))
    release_session_lock(lock_path, task_file)

    target_stop_result: dict[str, Any] | None = None
    if also_stop_target:
        target_stop_result = stop_target(task_payload["target"])

    task_payload["phase"] = "cancelled"
    task_payload["cancelled_at"] = now_utc()
    task_payload["protected_pids_during_cancel"] = termination.get("excluded_pids", [])
    task_payload["stopped_related_pids"] = termination["terminated_pids"]
    task_payload["still_alive_pids_after_cancel"] = termination["still_alive_pids"]
    task_payload["watcher_exited_after_cancel"] = not bool(termination["still_alive_pids"])
    if target_stop_result is not None:
        task_payload["target_stop_result"] = target_stop_result
    write_json(task_file, task_payload)
    return {
        "status": "cancelled",
        "task_id": str(task_payload.get("task_id") or ""),
        "watcher_pid": runtime["watcher_pid"] or "none",
        "watcher_exited": not bool(termination["still_alive_pids"]),
        "protected_pids": termination.get("excluded_pids", []),
        "stopped_related_pids": termination["terminated_pids"],
        "still_alive_pids": termination["still_alive_pids"],
        "target_stop_result": target_stop_result or "not_requested",
    }


def command_stop(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    tasks_dir = state_dir / "tasks"
    if args.all_active:
        if args.task_id:
            fatal("Use either --task-id or --all-active, not both.")
        if not tasks_dir.exists():
            fatal(f"State directory does not exist: {state_dir}")
        rows = process_rows()
        active_entries, _ = active_and_stale_task_snapshots(tasks_dir, rows)
        stopped: list[dict[str, Any]] = []
        for entry in active_entries:
            task_id_value = str(entry["task_id"])
            task_file = tasks_dir / f"{task_id_value}.json"
            if task_file.exists():
                stopped.append(stop_single_task(task_file, also_stop_target=args.also_stop_target))
        emit({"status": "ok", "stopped_count": len(stopped), "stopped_tasks": stopped}, args.json)
        return 0

    if not args.task_id:
        fatal("Either --task-id or --all-active is required.")
    task_file = tasks_dir / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    emit(stop_single_task(task_file, also_stop_target=args.also_stop_target), args.json)
    return 0


def command_serve(args: argparse.Namespace) -> int:
    from wait_handoff_dashboard import command_serve as serve_dashboard

    return serve_dashboard(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hand off a long blocking wait to a detached watcher, then resume the same Codex "
            "session after the watched job exits."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schedule = subparsers.add_parser("schedule", help="Preflight a long-running job and schedule a detached wait.")
    schedule.add_argument("--pid", type=int, help="PID to watch.")
    schedule.add_argument("--pattern", help="Process pattern to watch when PID capture is awkward.")
    schedule.add_argument("--host", help="Optional remote host. Without this, the watch is local.")
    schedule.add_argument("--expected-seconds", type=int, required=True, help="Estimated runtime of the job.")
    schedule.add_argument("--blocking", action="store_true", help="Confirm that the wait blocks the next step.")
    schedule.add_argument(
        "--allow-short-test",
        action="store_true",
        help="Bypass the default 5-minute minimum for explicit testing and iteration.",
    )
    schedule.add_argument(
        "--preflight-seconds",
        type=int,
        default=DEFAULT_PREFLIGHT_SECONDS,
        help="Seconds to keep watching before the handoff is accepted.",
    )
    schedule.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="Polling interval for the detached watcher.",
    )
    schedule.add_argument(
        "--max-wait-seconds",
        type=int,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help="Maximum time to wait before resuming anyway. Default: 7200 seconds (2 hours).",
    )
    schedule.add_argument("--session-id", help="Override CODEX_THREAD_ID when scheduling.")
    schedule.add_argument("--cwd", help="Working directory to use when resuming Codex.")
    schedule.add_argument("--note", help="Short note appended to the resume prompt.")
    schedule.add_argument("--resume-prompt", help="Inline continuation instructions for the resumed session.")
    schedule.add_argument("--resume-prompt-file", help="File containing continuation instructions.")
    schedule.add_argument(
        "--observed-log",
        help=(
            "Optional application/process log to show in the dashboard and resume prompt. "
            "Use when a real incremental log already exists or is cheap and meaningful to create."
        ),
    )
    schedule.add_argument(
        "--observed-log-host",
        help="Optional host for --observed-log. Defaults to --host for remote watched targets.",
    )
    schedule.add_argument("--observed-log-label", help="Human label for --observed-log in the dashboard.")
    schedule.add_argument(
        "--resume-retry-delay-seconds",
        type=int,
        default=int(os.environ.get("CODEX_WAIT_RESUME_RETRY_DELAY_SECONDS", DEFAULT_RESUME_RETRY_DELAY_SECONDS)),
        help=(
            "Seconds to wait before retrying `codex exec resume` after a retryable network disconnect. "
            "Default: 1200 seconds (20 minutes)."
        ),
    )
    schedule.add_argument(
        "--resume-retry-max-attempts",
        type=int,
        default=int(os.environ.get("CODEX_WAIT_RESUME_RETRY_MAX_ATTEMPTS", DEFAULT_RESUME_RETRY_MAX_ATTEMPTS)),
        help=(
            "Maximum total `codex exec resume` attempts for retryable network disconnects. "
            "Use 0 for unlimited retries. Default: 12."
        ),
    )
    schedule.add_argument(
        "--resume-preserve-approvals-and-sandbox",
        action="store_true",
        help=(
            "Opt out of the default no-sandbox resume behavior and keep the normal Codex "
            "approval and sandbox settings when resuming. Not recommended for most real "
            "blocking resumes."
        ),
    )
    schedule.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    schedule.add_argument("--dry-run-resume", action="store_true", help="Skip the final `codex exec resume` call.")
    schedule.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    watch = subparsers.add_parser("watch", help=argparse.SUPPRESS)
    watch.add_argument("--task-file", required=True, help="Internal task file path.")

    status = subparsers.add_parser("status", help="Inspect active or historical wait tasks.")
    status.add_argument("--task-id", help="Show a specific task.")
    status.add_argument("--session-id", help="Show the active task for a session if one exists.")
    status.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    active = subparsers.add_parser("active", help="List only tasks that still have live watch/resume processes.")
    active.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    active.add_argument("--include-stale", action="store_true", help="Also report stale active-looking task records with no live processes.")
    active.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    cancel = subparsers.add_parser("cancel", help="Cancel a scheduled wait task.")
    cancel.add_argument("--task-id", required=True, help="Task id to cancel.")
    cancel.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    cancel.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    stop = subparsers.add_parser("stop", help="Safely stop local watch/resume first, then optionally stop the watched target.")
    stop.add_argument("--task-id", help="Task id to stop.")
    stop.add_argument("--all-active", action="store_true", help="Stop every currently active watch/resume chain.")
    stop.add_argument(
        "--also-stop-target",
        action="store_true",
        help="After local watch/resume is down, also stop the watched target process or pattern.",
    )
    stop.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    stop.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    serve = subparsers.add_parser("serve", help="Start a read-only local web dashboard for wait tasks.")
    serve.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    serve.add_argument("--host", default="127.0.0.1", help="Host interface for the local dashboard.")
    serve.add_argument("--port", type=int, default=8765, help="Preferred dashboard port. Uses the next open port if busy.")
    serve.add_argument("--limit", type=int, default=80, help="Number of recent tasks to show in the sidebar.")
    serve.add_argument("--refresh-seconds", type=float, default=2.0, help="Browser auto-refresh interval.")
    serve.add_argument("--max-log-chars", type=int, default=60000, help="Maximum tail characters to load from each text file.")
    serve.add_argument("--open", action="store_true", help="Open the dashboard in the default browser after starting.")
    serve.add_argument("--quiet", action="store_true", help="Suppress HTTP access logs.")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "schedule":
        return command_schedule(args)
    if args.command == "watch":
        return command_watch(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "active":
        return command_active(args)
    if args.command == "cancel":
        return command_cancel(args)
    if args.command == "stop":
        return command_stop(args)
    if args.command == "serve":
        return command_serve(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
