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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PREFLIGHT_SECONDS = 20
DEFAULT_POLL_SECONDS = 15
DEFAULT_STATE_DIR = "tmp/codex-wait-handoff"
ACTIVE_PHASES = {"scheduled", "watching"}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def build_resume_prompt(
    task_id_value: str,
    task_file: Path,
    target: dict[str, Any],
    note: str | None,
    prompt_text: str | None,
) -> str:
    lines = [
        "The scheduled blocking wait has completed.",
        "",
        f"task_id: {task_id_value}",
        f"task_file: {task_file}",
        f"watched_target: {target_summary(target)}",
        "",
        "The watched process has exited. Read the task metadata, collect outputs, update the relevant workspace artifacts, and continue from the blocked step.",
    ]
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
    lock_path = acquire_session_lock(state_dirs["base"], session_id, task_file)
    prompt_file = state_dirs["prompts"] / f"{task_id_value}.prompt.txt"
    log_file = state_dirs["logs"] / f"{task_id_value}.watch.log"
    resume_log_file = state_dirs["logs"] / f"{task_id_value}.resume.log"
    last_message_file = state_dirs["outputs"] / f"{task_id_value}.last-message.txt"

    resume_prompt = build_resume_prompt(
        task_id_value=task_id_value,
        task_file=task_file,
        target=target,
        note=args.note,
        prompt_text=prompt_text,
    )
    write_text(prompt_file, resume_prompt)

    task_payload: dict[str, Any] = {
        "task_id": task_id_value,
        "phase": "scheduled",
        "created_at": now_utc(),
        "session_id": session_id,
        "session_lock": str(lock_path),
        "target": target,
        "expected_seconds": args.expected_seconds,
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
    log_prefix = f"[{now_utc()}] task {task_payload['task_id']}"
    print(f"{log_prefix} watching {target_summary(target)}", flush=True)

    task_payload["phase"] = "watching"
    task_payload["watch_loop_started_at"] = now_utc()
    write_json(task_file, task_payload)

    while True:
        state, detail = probe_target(target)
        print(f"[{now_utc()}] probe={state} detail={detail}", flush=True)
        if state == "dead":
            break
        if state == "unknown":
            time.sleep(min(poll_seconds, 5))
            continue
        time.sleep(poll_seconds)

    task_payload = load_json(task_file)
    task_payload["phase"] = "completed"
    task_payload["completed_at"] = now_utc()
    write_json(task_file, task_payload)
    release_session_lock(lock_path, task_file)

    if task_payload.get("dry_run_resume"):
        task_payload["phase"] = "resume_dry_run_complete"
        task_payload["resume_completed_at"] = now_utc()
        write_json(task_file, task_payload)
        print(f"[{now_utc()}] dry-run resume completed", flush=True)
        return 0

    prompt_text = read_text(Path(task_payload["prompt_file"]))
    resume_log_file = Path(task_payload["resume_log_file"])
    last_message_file = Path(task_payload["last_message_file"])
    resume_cwd = Path(task_payload["resume_cwd"])

    task_payload["phase"] = "resume_started"
    task_payload["resume_started_at"] = now_utc()
    write_json(task_file, task_payload)

    resume_command = [
        os.environ.get("CODEX_WAIT_CODEX_BIN", "codex"),
        "exec",
        "resume",
    ]
    if task_payload.get("resume_skip_git_repo_check", True):
        resume_command.append("--skip-git-repo-check")
    resume_command.extend([
        "--output-last-message",
        str(last_message_file),
        task_payload["session_id"],
        "-",
    ])
    print(f"[{now_utc()}] resuming session {task_payload['session_id']}", flush=True)
    with resume_log_file.open("a", encoding="utf-8") as resume_log:
        result = subprocess.run(
            resume_command,
            input=prompt_text,
            text=True,
            stdout=resume_log,
            stderr=subprocess.STDOUT,
            cwd=resume_cwd,
            check=False,
        )

    task_payload = load_json(task_file)
    task_payload["resume_returncode"] = result.returncode
    task_payload["resume_completed_at"] = now_utc()
    task_payload["phase"] = "resume_ok" if result.returncode == 0 else "resume_failed"
    write_json(task_file, task_payload)
    print(f"[{now_utc()}] resume returncode={result.returncode}", flush=True)
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


def command_cancel(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    task_payload = load_json(task_file)
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    watcher_exited = True
    if watcher_pid and pid_exists(watcher_pid):
        os.kill(watcher_pid, signal.SIGTERM)
        watcher_exited = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not pid_exists(watcher_pid):
                watcher_exited = True
                break
            time.sleep(0.2)
    lock_path = Path(task_payload.get("session_lock", ""))
    release_session_lock(lock_path, task_file)
    task_payload["phase"] = "cancelled"
    task_payload["cancelled_at"] = now_utc()
    task_payload["watcher_exited_after_cancel"] = watcher_exited
    write_json(task_file, task_payload)
    emit(
        {
            "status": "cancelled",
            "task_id": args.task_id,
            "watcher_pid": watcher_pid or "none",
            "watcher_exited": watcher_exited,
        },
        args.json,
    )
    return 0


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
    schedule.add_argument("--session-id", help="Override CODEX_THREAD_ID when scheduling.")
    schedule.add_argument("--cwd", help="Working directory to use when resuming Codex.")
    schedule.add_argument("--note", help="Short note appended to the resume prompt.")
    schedule.add_argument("--resume-prompt", help="Inline continuation instructions for the resumed session.")
    schedule.add_argument("--resume-prompt-file", help="File containing continuation instructions.")
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

    cancel = subparsers.add_parser("cancel", help="Cancel a scheduled wait task.")
    cancel.add_argument("--task-id", required=True, help="Task id to cancel.")
    cancel.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    cancel.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

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
    if args.command == "cancel":
        return command_cancel(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
