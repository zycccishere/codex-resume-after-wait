#!/usr/bin/env python3
"""Process-incarnation capture, probing, and safe termination.

A PID is only a namespace slot. This module binds it to a start token before a
watcher is detached and revalidates that token before every signal. Linux uses
``boot_id + /proc/<pid>/stat starttime`` and local macOS uses libproc's
microsecond-resolution process start time. Other Unix hosts expose only the
weaker ``LC_ALL=C ps -o lstart`` fallback, which strict callers can reject.

Remote inspection runs a fixed shell helper.  User patterns travel only on the
helper's stdin, never in an SSH or remote-shell argv that ``pgrep -f`` scans.
The helper also excludes itself and its complete ancestor chain.
"""

from __future__ import annotations

import ctypes
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


IDENTITY_VERSION = 1
DEFAULT_COMMAND_TIMEOUT = 10.0
DEFAULT_SIGNAL_GRACE = 3.0


class ProcessIdentityError(RuntimeError):
    """Base error for unsafe or unavailable process inspection."""


class ProcessNotFound(ProcessIdentityError):
    """The requested PID no longer names a process."""


class ProcessInspectionError(ProcessIdentityError):
    """The process exists or may exist, but its identity could not be proven."""


class UnsafeRemoteHost(ProcessIdentityError):
    """The SSH destination could be parsed as an option or command data."""


@dataclass(frozen=True)
class ProcessIdentity:
    scope: str
    pid: int
    source: str
    start_token: str
    ppid: int | None = None
    state: str = ""
    command: str = ""
    host: str | None = None
    version: int = IDENTITY_VERSION

    def __post_init__(self) -> None:
        _validate_pid(self.pid)
        if self.scope not in {"local", "remote"}:
            raise ValueError("scope must be local or remote")
        if not self.source or not self.start_token:
            raise ValueError("source and start_token must not be empty")
        if self.scope == "remote":
            validate_remote_host(self.host)
        elif self.host is not None:
            raise ValueError("local identities cannot carry a remote host")

    @property
    def is_zombie(self) -> bool:
        return self.state.upper().startswith("Z")

    def same_incarnation(self, other: "ProcessIdentity") -> bool:
        return (
            self.version == other.version
            and self.scope == other.scope
            and self.host == other.host
            and self.pid == other.pid
            and self.source == other.source
            and self.start_token == other.start_token
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scope": self.scope,
            "host": self.host,
            "pid": self.pid,
            "ppid": self.ppid,
            "state": self.state,
            "source": self.source,
            "start_token": self.start_token,
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProcessIdentity":
        version = int(value.get("version", IDENTITY_VERSION))
        if version != IDENTITY_VERSION:
            raise ValueError(f"unsupported process identity version {version}")
        ppid_value = value.get("ppid")
        return cls(
            version=version,
            scope=str(value["scope"]),
            host=str(value["host"]) if value.get("host") is not None else None,
            pid=int(value["pid"]),
            ppid=int(ppid_value) if ppid_value is not None else None,
            state=str(value.get("state") or ""),
            source=str(value["source"]),
            start_token=str(value["start_token"]),
            command=str(value.get("command") or ""),
        )


@dataclass(frozen=True)
class ProbeResult:
    status: str
    reason: str
    detail: str
    current: ProcessIdentity | None = None

    def as_legacy_tuple(self) -> tuple[str, str]:
        return self.status, self.detail


@dataclass(frozen=True)
class PatternResult:
    status: str
    detail: str
    identities: tuple[ProcessIdentity, ...] = ()

    def as_legacy_tuple(self) -> tuple[str, str]:
        return self.status, self.detail


def _validate_pid(pid: int) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    return pid


def validate_remote_host(host: str | None) -> str:
    if not isinstance(host, str) or not host:
        raise UnsafeRemoteHost("remote host must be a non-empty string")
    if host.startswith("-"):
        raise UnsafeRemoteHost("remote host must not start with '-'")
    if any(character in host for character in ("\x00", "\r", "\n")):
        raise UnsafeRemoteHost("remote host contains a control character")
    if any(character.isspace() for character in host):
        raise UnsafeRemoteHost("remote host must not contain whitespace")
    return host


def _validate_pattern(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("process pattern must be a non-empty string")
    if any(character in pattern for character in ("\x00", "\r", "\n")):
        raise ValueError("process pattern cannot contain NUL or a newline")
    return pattern


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else None,
    )


def _parse_linux_stat(text: str, pid: int) -> tuple[int, str, str]:
    prefix = f"{pid} ("
    if not text.startswith(prefix):
        raise ProcessInspectionError(f"unexpected /proc stat prefix for pid {pid}")
    close = text.rfind(")")
    if close < len(prefix):
        raise ProcessInspectionError(f"malformed /proc stat for pid {pid}")
    fields = text[close + 1 :].strip().split()
    # fields[0] is kernel field 3 (state); field 22 is therefore index 19.
    if len(fields) <= 19:
        raise ProcessInspectionError(f"short /proc stat for pid {pid}")
    try:
        ppid = int(fields[1])
        start_ticks = fields[19]
    except (ValueError, IndexError) as error:
        raise ProcessInspectionError(f"invalid /proc stat for pid {pid}") from error
    return ppid, fields[0], start_ticks


def _capture_linux_proc(pid: int, proc_root: Path) -> ProcessIdentity:
    stat_path = proc_root / str(pid) / "stat"
    try:
        first = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ProcessNotFound(f"pid {pid} is absent") from error
    except (OSError, UnicodeError) as error:
        raise ProcessInspectionError(f"cannot read {stat_path}: {error}") from error

    ppid, state, start_ticks = _parse_linux_stat(first, pid)
    boot_path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    try:
        boot_id = boot_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise ProcessInspectionError(f"cannot read Linux boot identity: {error}") from error
    if not boot_id:
        raise ProcessInspectionError("Linux boot identity is empty")

    try:
        second = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ProcessNotFound(f"pid {pid} exited during identity capture") from error
    second_ppid, second_state, second_ticks = _parse_linux_stat(second, pid)
    if second_ticks != start_ticks:
        raise ProcessInspectionError(f"pid {pid} changed incarnation during capture")

    command = ""
    try:
        command = (proc_root / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        ).strip()
    except OSError:
        pass
    return ProcessIdentity(
        scope="local",
        pid=pid,
        ppid=second_ppid,
        state=second_state or state,
        source="linux-proc-starttime",
        start_token=f"{boot_id}:{second_ticks}",
        command=command,
    )


def _parse_ps_identity(line: str, pid: int, *, scope: str, host: str | None) -> ProcessIdentity:
    parts = line.strip().split(None, 8)
    if len(parts) < 8:
        raise ProcessInspectionError(f"unexpected ps identity output for pid {pid}: {line!r}")
    try:
        observed_pid = int(parts[0])
        ppid = int(parts[1])
    except ValueError as error:
        raise ProcessInspectionError(f"invalid ps identity output for pid {pid}") from error
    if observed_pid != pid:
        raise ProcessInspectionError(
            f"ps identity mismatch: requested pid {pid}, received {observed_pid}"
        )
    start_token = " ".join(parts[2:7])
    state = parts[7]
    command = parts[8] if len(parts) > 8 else ""
    return ProcessIdentity(
        scope=scope,
        host=host,
        pid=pid,
        ppid=ppid,
        state=state,
        source="ps-lstart",
        start_token=start_token,
        command=command,
    )


def _capture_local_ps(pid: int) -> ProcessIdentity:
    environment = dict(os.environ)
    environment.update({"LC_ALL": "C", "LANG": "C"})
    command = ["ps", "-o", "pid=,ppid=,lstart=,stat=,command=", "-p", str(pid)]
    first = _run_command(command, env=environment)
    if first.returncode in {0, 1} and not first.stdout.strip():
        raise ProcessNotFound(f"pid {pid} is absent")
    if first.returncode != 0:
        raise ProcessInspectionError(
            first.stderr.strip() or first.stdout.strip() or f"ps exited {first.returncode}"
        )
    identity = _parse_ps_identity(first.stdout.splitlines()[0], pid, scope="local", host=None)

    second = _run_command(command, env=environment)
    if second.returncode in {0, 1} and not second.stdout.strip():
        raise ProcessNotFound(f"pid {pid} exited during identity capture")
    if second.returncode != 0:
        raise ProcessInspectionError(second.stderr.strip() or f"ps exited {second.returncode}")
    verified = _parse_ps_identity(second.stdout.splitlines()[0], pid, scope="local", host=None)
    if not identity.same_incarnation(verified):
        raise ProcessInspectionError(f"pid {pid} changed incarnation during capture")
    return verified


def _capture_macos_proc(pid: int) -> ProcessIdentity:
    """Capture microsecond-resolution start time through macOS libproc."""

    max_command_length = 16

    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * max_command_length),
            ("pbi_name", ctypes.c_char * (2 * max_command_length)),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError as error:
        raise ProcessInspectionError(f"cannot load macOS libproc: {error}") from error
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int

    def read() -> ProcBSDInfo:
        info = ProcBSDInfo()
        size = ctypes.sizeof(info)
        received = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if received != size or int(info.pbi_pid) != pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError as error:
                raise ProcessNotFound(f"pid {pid} is absent") from error
            except PermissionError as error:
                raise ProcessInspectionError(
                    f"macOS denied identity inspection for pid {pid}"
                ) from error
            # libproc may stop returning BSD info for an exited-but-unreaped
            # child while kill(pid, 0) still succeeds.  Use ps only to prove
            # that terminal state; never use this fallback as a replacement
            # identity for a live process.
            try:
                fallback = _capture_local_ps(pid)
            except ProcessNotFound:
                raise
            if fallback.is_zombie:
                raise ProcessNotFound(f"pid {pid} is zombie")
            raise ProcessInspectionError(
                f"macOS proc_pidinfo returned {received} bytes for pid {pid}, expected {size}"
            )
        return info

    first = read()
    second = read()
    first_token = f"{int(first.pbi_start_tvsec)}:{int(first.pbi_start_tvusec)}"
    second_token = f"{int(second.pbi_start_tvsec)}:{int(second.pbi_start_tvusec)}"
    if first_token != second_token:
        raise ProcessInspectionError(f"pid {pid} changed incarnation during capture")
    state = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}.get(
        int(second.pbi_status), f"?{int(second.pbi_status)}"
    )
    command = bytes(second.pbi_name).split(b"\0", 1)[0].decode("utf-8", "replace")
    return ProcessIdentity(
        scope="local",
        pid=pid,
        ppid=int(second.pbi_ppid),
        state=state,
        source="macos-proc-starttime",
        start_token=second_token,
        command=command,
    )


def capture_local_identity(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessIdentity:
    _validate_pid(pid)
    if sys.platform.startswith("linux") and (proc_root / "self" / "stat").exists():
        return _capture_linux_proc(pid, proc_root)
    if sys.platform == "darwin":
        return _capture_macos_proc(pid)
    return _capture_local_ps(pid)


def probe_local_identity(identity: ProcessIdentity) -> ProbeResult:
    if identity.scope != "local":
        raise ValueError("probe_local_identity requires a local identity")
    try:
        current = capture_local_identity(identity.pid)
    except ProcessNotFound:
        return ProbeResult("dead", "absent", f"pid {identity.pid} is absent")
    except ProcessIdentityError as error:
        return ProbeResult("unknown", "inspection_failed", str(error))
    if not identity.same_incarnation(current):
        return ProbeResult(
            "dead",
            "pid_reused",
            f"pid {identity.pid} now names a different process incarnation",
            current,
        )
    if current.is_zombie:
        return ProbeResult("dead", "zombie", f"pid {identity.pid} is zombie", current)
    return ProbeResult("alive", "matching", f"pid {identity.pid} identity matches", current)


def _local_parent_chain() -> set[int]:
    result = _run_command(["ps", "-Ao", "pid=,ppid="], timeout=DEFAULT_COMMAND_TIMEOUT)
    if result.returncode != 0:
        raise ProcessInspectionError(
            result.stderr.strip() or result.stdout.strip() or f"ps exited {result.returncode}"
        )
    parents: dict[int, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            parents[int(fields[0])] = int(fields[1])
        except ValueError:
            continue
    protected: set[int] = set()
    current = os.getpid()
    while current > 0 and current not in protected:
        protected.add(current)
        parent = parents.get(current)
        if parent is None or parent == current:
            break
        current = parent
    return protected


def find_local_pattern(pattern: str) -> tuple[ProcessIdentity, ...]:
    pattern = _validate_pattern(pattern)
    result = _run_command(["pgrep", "-f", "--", pattern])
    if result.returncode == 1:
        return ()
    if result.returncode != 0:
        raise ProcessInspectionError(
            result.stderr.strip() or result.stdout.strip() or f"pgrep exited {result.returncode}"
        )
    protected = _local_parent_chain()
    identities: list[ProcessIdentity] = []
    for field in result.stdout.split():
        try:
            pid = int(field)
        except ValueError:
            continue
        if pid in protected:
            continue
        try:
            identity = capture_local_identity(pid)
        except ProcessNotFound:
            continue
        if not identity.is_zombie:
            identities.append(identity)
    return tuple(sorted(identities, key=lambda item: item.pid))


def probe_local_pattern(pattern: str) -> PatternResult:
    try:
        identities = find_local_pattern(pattern)
    except ProcessIdentityError as error:
        return PatternResult("unknown", str(error))
    if not identities:
        return PatternResult("dead", f"no local process matched pattern {pattern!r}")
    return PatternResult(
        "alive",
        ", ".join(f"pid {identity.pid}" for identity in identities),
        identities,
    )


def _send_local_signal_if_matching(identity: ProcessIdentity, signum: int) -> str:
    probe = probe_local_identity(identity)
    if probe.status == "unknown":
        return "unknown"
    if probe.status == "dead":
        return probe.reason
    try:
        os.kill(identity.pid, signum)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "permission_denied"
    return "signaled"


def terminate_local_identity(
    identity: ProcessIdentity,
    *,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE,
    poll_seconds: float = 0.1,
) -> dict[str, Any]:
    if identity.scope != "local":
        raise ValueError("terminate_local_identity requires a local identity")
    signals_sent: list[str] = []
    pidfd: int | None = None
    pidfd_sender = getattr(signal, "pidfd_send_signal", None)
    pidfd_open = getattr(os, "pidfd_open", None)
    if identity.source == "linux-proc-starttime" and callable(pidfd_open) and callable(pidfd_sender):
        try:
            pidfd = pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return _termination_result(identity, "original_exited", "absent", signals_sent)
        except OSError:
            pidfd = None
        if pidfd is not None:
            probe = probe_local_identity(identity)
            if probe.status != "alive":
                os.close(pidfd)
                status = "probe_failed" if probe.status == "unknown" else "original_exited"
                detail = probe.detail if probe.status == "unknown" else probe.reason
                return _termination_result(identity, status, detail, signals_sent)

    def send(signum: int) -> str:
        if pidfd is not None:
            try:
                pidfd_sender(pidfd, signum, None, 0)
            except ProcessLookupError:
                return "absent"
            except PermissionError:
                return "permission_denied"
            return "signaled"
        return _send_local_signal_if_matching(identity, signum)

    try:
        first = send(signal.SIGTERM)
        if first != "signaled":
            status = "probe_failed" if first in {"unknown", "permission_denied"} else "original_exited"
            return _termination_result(identity, status, first, signals_sent)
        signals_sent.append("TERM")

        deadline = time.monotonic() + max(grace_seconds, 0.0)
        while time.monotonic() < deadline:
            probe = probe_local_identity(identity)
            if probe.status == "dead":
                return _termination_result(identity, "stopped", probe.reason, signals_sent)
            if probe.status == "unknown":
                return _termination_result(identity, "probe_failed", probe.reason, signals_sent)
            time.sleep(max(min(poll_seconds, deadline - time.monotonic()), 0.0))

        probe = probe_local_identity(identity)
        if probe.status == "dead":
            return _termination_result(identity, "stopped", probe.reason, signals_sent)
        if probe.status == "unknown":
            return _termination_result(identity, "probe_failed", probe.reason, signals_sent)

        second = send(signal.SIGKILL)
        if second != "signaled":
            status = "probe_failed" if second in {"unknown", "permission_denied"} else "stopped"
            return _termination_result(identity, status, second, signals_sent)
        signals_sent.append("KILL")
        time.sleep(max(poll_seconds, 0.0))
        final = probe_local_identity(identity)
        if final.status == "alive":
            return _termination_result(identity, "still_alive", final.reason, signals_sent)
        if final.status == "unknown":
            return _termination_result(identity, "probe_failed", final.reason, signals_sent)
        return _termination_result(identity, "stopped", final.reason, signals_sent)
    finally:
        if pidfd is not None:
            os.close(pidfd)


def _termination_result(
    identity: ProcessIdentity,
    status: str,
    reason: str,
    signals_sent: list[str],
) -> dict[str, Any]:
    return {
        "scope": identity.scope,
        "host": identity.host,
        "pid": identity.pid,
        "identity": identity.to_dict(),
        "status": status,
        "reason": reason,
        "signals_sent": list(signals_sent),
    }


REMOTE_HELPER_SCRIPT = r'''
snapshot() {
    snap_pid=$1
    case "$snap_pid" in *[!0-9]*|'') return 2 ;; esac
    if [ -d /proc ]; then
        [ -d "/proc/$snap_pid" ] || return 1
        [ -r "/proc/$snap_pid/stat" ] || return 2
        snap_line=$(cat "/proc/$snap_pid/stat" 2>/dev/null) || {
            [ -d "/proc/$snap_pid" ] && return 2
            return 1
        }
        snap_tail=${snap_line##*) }
        snap_index=1
        snap_start=
        set -- $snap_tail
        snap_state=${1-}
        snap_ppid=${2-}
        for snap_field do
            if [ "$snap_index" -eq 20 ]; then snap_start=$snap_field; break; fi
            snap_index=$((snap_index + 1))
        done
        snap_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || return 2
        [ -n "$snap_state" ] && [ -n "$snap_ppid" ] && [ -n "$snap_start" ] && [ -n "$snap_boot" ] || return 2
        snap_source=linux-proc-starttime
        snap_token=$snap_boot:$snap_start
        return 0
    fi
    snap_start_one=$(LC_ALL=C ps -o lstart= -p "$snap_pid" 2>/dev/null | sed -n '1{s/^[[:space:]]*//;s/[[:space:]]*$//;p;}')
    [ -n "$snap_start_one" ] || return 1
    snap_ppid=$(LC_ALL=C ps -o ppid= -p "$snap_pid" 2>/dev/null | awk 'NR==1 {print $1}')
    snap_state=$(LC_ALL=C ps -o stat= -p "$snap_pid" 2>/dev/null | awk 'NR==1 {print $1}')
    snap_start_two=$(LC_ALL=C ps -o lstart= -p "$snap_pid" 2>/dev/null | sed -n '1{s/^[[:space:]]*//;s/[[:space:]]*$//;p;}')
    [ -n "$snap_ppid" ] && [ -n "$snap_state" ] && [ "$snap_start_one" = "$snap_start_two" ] || return 2
    snap_source=ps-lstart
    snap_token=$snap_start_two
    return 0
}

emit_identity() {
    printf 'IDENTITY\t%s\t%s\t%s\t%s\t%s\n' "$snap_pid" "$snap_ppid" "$snap_state" "$snap_source" "$snap_token"
}

is_protected() {
    protected_candidate=$1
    case " $protected_pids " in *" $protected_candidate "*) return 0 ;; *) return 1 ;; esac
}

IFS= read -r operation || { printf 'ERROR\tmissing operation\n'; exit 2; }
case "$operation" in
    CAPTURE_PID)
        IFS= read -r requested_pid || { printf 'ERROR\tmissing pid\n'; exit 2; }
        snapshot "$requested_pid"; snapshot_status=$?
        case "$snapshot_status" in
            0) emit_identity ;;
            1) printf 'ABSENT\n' ;;
            *) printf 'ERROR\tidentity inspection failed\n' ;;
        esac
        ;;
    SIGNAL_PID)
        IFS= read -r requested_pid || { printf 'ERROR\tmissing pid\n'; exit 2; }
        IFS= read -r expected_source || { printf 'ERROR\tmissing source\n'; exit 2; }
        IFS= read -r expected_token || { printf 'ERROR\tmissing token\n'; exit 2; }
        IFS= read -r requested_signal || { printf 'ERROR\tmissing signal\n'; exit 2; }
        case "$requested_signal" in TERM|KILL) ;; *) printf 'ERROR\tinvalid signal\n'; exit 2 ;; esac
        snapshot "$requested_pid"; snapshot_status=$?
        case "$snapshot_status" in
            1) printf 'ABSENT\n'; exit 0 ;;
            0) ;;
            *) printf 'ERROR\tidentity inspection failed\n'; exit 0 ;;
        esac
        if [ "$snap_source" != "$expected_source" ] || [ "$snap_token" != "$expected_token" ]; then
            printf 'REUSED\n'; exit 0
        fi
        case "$snap_state" in Z*) printf 'ZOMBIE\n'; exit 0 ;; esac
        if kill -"$requested_signal" "$requested_pid" 2>/dev/null; then printf 'SIGNALED\n'; else printf 'ERROR\tsignal failed\n'; fi
        ;;
    PATTERN)
        IFS= read -r requested_pattern || { printf 'ERROR\tmissing pattern\n'; exit 2; }
        protected_pids="$$"
        protected_probe=$$
        while [ "$protected_probe" -gt 1 ] 2>/dev/null; do
            protected_parent=$(LC_ALL=C ps -o ppid= -p "$protected_probe" 2>/dev/null | awk 'NR==1 {print $1}')
            case "$protected_parent" in *[!0-9]*|'') printf 'ERROR\tancestor inspection failed\n'; exit 0 ;; esac
            [ "$protected_parent" -gt 0 ] 2>/dev/null || break
            is_protected "$protected_parent" && break
            protected_pids="$protected_pids $protected_parent"
            protected_probe=$protected_parent
        done
        matched_pids=$(pgrep -f -- "$requested_pattern" 2>/dev/null); pgrep_status=$?
        case "$pgrep_status" in 0|1) ;; *) printf 'ERROR\tpgrep failed\n'; exit 0 ;; esac
        for requested_pid in $matched_pids; do
            is_protected "$requested_pid" && continue
            snapshot "$requested_pid"; snapshot_status=$?
            case "$snapshot_status" in 0) ;; 1) continue ;; *) printf 'ERROR\tmatched process identity inspection failed\n'; exit 0 ;; esac
            case "$snap_state" in Z*) continue ;; esac
            emit_identity
        done
        printf 'DONE\n'
        ;;
    *) printf 'ERROR\tunknown operation\n'; exit 2 ;;
esac
'''.strip()

REMOTE_HELPER_COMMAND = "exec sh -c " + shlex.quote(REMOTE_HELPER_SCRIPT)


def _remote_request(host: str, payload: str, *, timeout: float) -> list[str]:
    host = validate_remote_host(host)
    try:
        result = _run_command(
            ["ssh", "--", host, REMOTE_HELPER_COMMAND],
            input_text=payload,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProcessInspectionError(f"ssh timeout while inspecting {host}") from error
    if result.returncode == 255:
        raise ProcessInspectionError(result.stderr.strip() or "ssh returned 255")
    if result.returncode != 0:
        raise ProcessInspectionError(
            result.stderr.strip() or result.stdout.strip() or f"remote helper exited {result.returncode}"
        )
    lines = [line.rstrip("\r") for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise ProcessInspectionError("remote helper returned no result")
    errors = [line for line in lines if line.startswith("ERROR\t")]
    if errors:
        raise ProcessInspectionError(errors[0].split("\t", 1)[1])
    return lines


def _identity_from_remote_line(line: str, host: str) -> ProcessIdentity:
    fields = line.split("\t", 5)
    if len(fields) != 6 or fields[0] != "IDENTITY":
        raise ProcessInspectionError(f"invalid remote identity response: {line!r}")
    try:
        pid = int(fields[1])
        ppid = int(fields[2])
    except ValueError as error:
        raise ProcessInspectionError(f"invalid remote identity response: {line!r}") from error
    return ProcessIdentity(
        scope="remote",
        host=host,
        pid=pid,
        ppid=ppid,
        state=fields[3],
        source=fields[4],
        start_token=fields[5],
    )


def capture_remote_identity(
    host: str,
    pid: int,
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> ProcessIdentity:
    host = validate_remote_host(host)
    _validate_pid(pid)
    lines = _remote_request(host, f"CAPTURE_PID\n{pid}\n", timeout=timeout)
    if lines[0] == "ABSENT":
        raise ProcessNotFound(f"remote pid {pid} is absent on {host}")
    identity = _identity_from_remote_line(lines[0], host)
    if identity.pid != pid:
        raise ProcessInspectionError(
            f"remote identity mismatch: requested pid {pid}, received {identity.pid}"
        )
    return identity


def probe_remote_identity(identity: ProcessIdentity) -> ProbeResult:
    if identity.scope != "remote" or identity.host is None:
        raise ValueError("probe_remote_identity requires a remote identity")
    try:
        current = capture_remote_identity(identity.host, identity.pid)
    except ProcessNotFound:
        return ProbeResult("dead", "absent", f"remote pid {identity.pid} is absent")
    except ProcessIdentityError as error:
        return ProbeResult("unknown", "inspection_failed", str(error))
    if not identity.same_incarnation(current):
        return ProbeResult(
            "dead",
            "pid_reused",
            f"remote pid {identity.pid} now names a different process incarnation",
            current,
        )
    if current.is_zombie:
        return ProbeResult("dead", "zombie", f"remote pid {identity.pid} is zombie", current)
    return ProbeResult("alive", "matching", f"remote pid {identity.pid} identity matches", current)


def find_remote_pattern(
    host: str,
    pattern: str,
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> tuple[ProcessIdentity, ...]:
    host = validate_remote_host(host)
    pattern = _validate_pattern(pattern)
    lines = _remote_request(host, f"PATTERN\n{pattern}\n", timeout=timeout)
    if "DONE" not in lines:
        raise ProcessInspectionError("remote pattern helper did not complete its snapshot")
    identities: list[ProcessIdentity] = []
    for line in lines:
        if line == "DONE":
            continue
        identities.append(_identity_from_remote_line(line, host))
    unique = {(item.pid, item.source, item.start_token): item for item in identities}
    return tuple(sorted(unique.values(), key=lambda item: item.pid))


def probe_remote_pattern(host: str, pattern: str) -> PatternResult:
    try:
        identities = find_remote_pattern(host, pattern)
    except ProcessIdentityError as error:
        return PatternResult("unknown", str(error))
    if not identities:
        return PatternResult("dead", f"no remote process matched pattern {pattern!r} on {host}")
    return PatternResult(
        "alive",
        ", ".join(f"{host} pid {identity.pid}" for identity in identities),
        identities,
    )


def _remote_signal(identity: ProcessIdentity, signal_name: str) -> str:
    assert identity.host is not None
    lines = _remote_request(
        identity.host,
        (
            f"SIGNAL_PID\n{identity.pid}\n{identity.source}\n"
            f"{identity.start_token}\n{signal_name}\n"
        ),
        timeout=DEFAULT_COMMAND_TIMEOUT,
    )
    return lines[0]


def terminate_remote_identity(
    identity: ProcessIdentity,
    *,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE,
    poll_seconds: float = 0.25,
) -> dict[str, Any]:
    if identity.scope != "remote" or identity.host is None:
        raise ValueError("terminate_remote_identity requires a remote identity")
    signals_sent: list[str] = []
    try:
        first = _remote_signal(identity, "TERM")
    except ProcessIdentityError as error:
        return _termination_result(identity, "probe_failed", str(error), signals_sent)
    if first != "SIGNALED":
        if first in {"ABSENT", "ZOMBIE", "REUSED"}:
            reason = "pid_reused" if first == "REUSED" else first.lower()
            return _termination_result(identity, "original_exited", reason, signals_sent)
        return _termination_result(identity, "probe_failed", first, signals_sent)
    signals_sent.append("TERM")

    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while time.monotonic() < deadline:
        probe = probe_remote_identity(identity)
        if probe.status == "dead":
            return _termination_result(identity, "stopped", probe.reason, signals_sent)
        if probe.status == "unknown":
            return _termination_result(identity, "probe_failed", probe.detail, signals_sent)
        time.sleep(max(min(poll_seconds, deadline - time.monotonic()), 0.0))

    probe = probe_remote_identity(identity)
    if probe.status == "dead":
        return _termination_result(identity, "stopped", probe.reason, signals_sent)
    if probe.status == "unknown":
        return _termination_result(identity, "probe_failed", probe.detail, signals_sent)
    try:
        second = _remote_signal(identity, "KILL")
    except ProcessIdentityError as error:
        return _termination_result(identity, "probe_failed", str(error), signals_sent)
    if second != "SIGNALED":
        if second in {"ABSENT", "ZOMBIE", "REUSED"}:
            reason = "pid_reused" if second == "REUSED" else second.lower()
            return _termination_result(identity, "stopped", reason, signals_sent)
        return _termination_result(identity, "probe_failed", second, signals_sent)
    signals_sent.append("KILL")
    time.sleep(max(poll_seconds, 0.0))
    final = probe_remote_identity(identity)
    if final.status == "alive":
        return _termination_result(identity, "still_alive", final.reason, signals_sent)
    if final.status == "unknown":
        return _termination_result(identity, "probe_failed", final.detail, signals_sent)
    return _termination_result(identity, "stopped", final.reason, signals_sent)


def terminate_local_pattern(
    pattern: str,
    *,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE,
) -> dict[str, Any]:
    identities = find_local_pattern(pattern)
    results = [
        terminate_local_identity(identity, grace_seconds=grace_seconds)
        for identity in identities
    ]
    statuses = {result["status"] for result in results}
    aggregate = (
        "already_absent"
        if not identities
        else "stopped"
        if statuses <= {"stopped", "original_exited"}
        else "incomplete"
    )
    return {
        "scope": "local",
        "mode": "pattern",
        "pattern": pattern,
        "matched_identities": [identity.to_dict() for identity in identities],
        "results": results,
        "status": aggregate,
    }


def terminate_remote_pattern(
    host: str,
    pattern: str,
    *,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE,
) -> dict[str, Any]:
    identities = find_remote_pattern(host, pattern)
    results = [
        terminate_remote_identity(identity, grace_seconds=grace_seconds)
        for identity in identities
    ]
    statuses = {result["status"] for result in results}
    aggregate = (
        "already_absent"
        if not identities
        else "stopped"
        if statuses <= {"stopped", "original_exited"}
        else "incomplete"
    )
    return {
        "scope": "remote",
        "mode": "pattern",
        "host": validate_remote_host(host),
        "pattern": pattern,
        "matched_identities": [identity.to_dict() for identity in identities],
        "results": results,
        "status": aggregate,
    }
