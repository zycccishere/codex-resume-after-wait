#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from codex_app_server import (
    AppServerClient,
    AppServerError,
    AppServerRpcError,
    LOADED_THREAD_STATUS_TYPES,
    authority_descriptor_mismatch,
    default_app_server_endpoint,
    default_codex_home,
    inspect_native_thread,
    parse_app_server_endpoint,
)
from handoff_ledger import (
    AuthorityMismatch,
    HandoffLedger,
    InvalidTransition,
    LedgerConflict,
    LedgerError,
    SubmissionBlocked,
)
from job_registry import JobConflict, JobRegistryError, OwnerJobRegistry
from process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    ProcessNotFound,
    capture_local_identity,
    capture_remote_identity,
    find_local_pattern,
    find_remote_pattern,
    probe_local_identity,
    probe_remote_identity,
    terminate_local_identity,
    terminate_remote_identity,
    validate_remote_host,
)


DEFAULT_PREFLIGHT_SECONDS = 20
DEFAULT_POLL_SECONDS = 15
DEFAULT_MAX_WAIT_SECONDS = 2 * 60 * 60
DEFAULT_RESUME_RETRY_DELAY_SECONDS = 20 * 60
DEFAULT_RESUME_RETRY_MAX_ATTEMPTS = 12
STATE_COLLISION_RETRY_SECONDS = 1
DEFAULT_STATE_COLLISION_MAX_ATTEMPTS = 900
WATCHER_STARTUP_TIMEOUT_SECONDS = 10
DEFAULT_STATE_DIR = str(
    Path(
        os.environ.get(
            "CODEX_WAIT_STATE_DIR",
            default_codex_home() / "wait-handoff",
        )
    )
    .expanduser()
    .resolve()
)
DEFAULT_COORDINATION_DIR = str(
    Path(
        os.environ.get(
            "CODEX_WAIT_COORDINATION_DIR",
            default_codex_home() / "wait-handoff-coordination",
        )
    )
    .expanduser()
    .resolve()
)
PROTOCOL_VERSION = 3
CLIENT_MESSAGE_PREFIX = "codex-wait-handoff:"
MARKER_LEDGER_PREFIX = "marker:"
LEGACY_HISTORY_MODE = "legacy"
PAGINATED_HISTORY_MODE = "paginated"
MARKER_AUTHORITY = {
    "endpoint": "marker://local",
    "transport": "marker",
    "endpoint_fingerprint": None,
}
STRONG_PROCESS_IDENTITY_SOURCES = {
    "linux-proc-starttime",
    "macos-proc-starttime",
}
ACTIVE_PHASES = {
    "reserving",
    "registration_recovery_required",
    "scheduled",
    "watching",
    "event_staged",
    "native_message_ready",
    "native_message_queued",
    "native_message_submitting",
    "native_message_submitted",
    "native_message_deferred",
}
PENDING_PHASES = {
    "marker_pending",
    "marker_claiming",
    "marker_unknown",
    "event_staged",
    "native_message_ready",
    "native_message_queued",
    "native_message_deferred",
    "native_message_unknown",
    "native_message_blocked",
}
WATCH_START_PHASES = {"scheduled", "watching"}
CANCEL_FAIL_CLOSED_PHASES = {
    "reserving",
    "registration_recovery_required",
    "scheduled",
    "event_staged",
    "native_message_ready",
    "native_message_queued",
    "native_message_submitting",
    "native_message_submitted",
    "native_message_deferred",
    "native_message_unknown",
    "native_message_accepted",
    "marker_pending",
    "marker_claiming",
    "marker_claimed",
    "marker_unknown",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fatal(message: str, exit_code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        tmp_path.unlink(missing_ok=True)


def write_text(path: Path, text: str) -> None:
    ensure_private_directory(path.parent)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        tmp_path.unlink(missing_ok=True)


def open_private_append(path: Path):
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8")


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def resolve_codex_binary(explicit: str | None = None) -> Path:
    """Prefer the current standalone/app binary over a stale PATH shim."""
    candidates: list[Path] = []
    configured = explicit or os.environ.get("CODEX_WAIT_CODEX_BIN")
    if configured:
        candidates.append(Path(configured).expanduser())
    codex_home = default_codex_home()
    candidates.append(codex_home / "packages" / "standalone" / "current" / "codex")
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/ChatGPT.app/Contents/Resources/codex"))
    path_binary = shutil.which("codex")
    if path_binary:
        candidates.append(Path(path_binary))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    fatal(
        "Could not find a current Codex binary. Pass --codex-bin or set "
        "CODEX_WAIT_CODEX_BIN."
    )


def codex_version(codex_binary: Path) -> str:
    try:
        result = run_command([str(codex_binary), "--version"], timeout_seconds=10)
    except (OSError, subprocess.SubprocessError) as error:
        return f"unknown ({error})"
    text = (result.stdout or result.stderr).strip()
    return text or f"unknown (returncode={result.returncode})"


def is_codex_launcher_token(token: str) -> bool:
    """Recognize a Codex executable without matching arbitrary arguments."""

    name = Path(token).name.lower()
    return name in {"codex", "codex.exe", "codex.js"} or (
        name.startswith("codex-") and not Path(name).suffix
    )


def ancestor_app_server_context() -> dict[str, Any]:
    """Discover the app-server that actually spawned this shell, when visible."""

    try:
        rows = {int(row["pid"]): row for row in process_rows()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"source": "undiscovered", "attachable": None}
    pid = os.getpid()
    seen: set[int] = set()
    saw_codex_process = False
    while pid in rows and pid not in seen:
        seen.add(pid)
        row = rows[pid]
        command = str(row.get("command") or "")
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        try:
            app_server_index = tokens.index("app-server")
        except ValueError:
            app_server_index = None
        launcher_tokens = (
            tokens[:app_server_index]
            if app_server_index is not None
            else tokens[:3]
        )
        has_codex_launcher = any(
            is_codex_launcher_token(token) for token in launcher_tokens
        )
        if has_codex_launcher:
            saw_codex_process = True
        if app_server_index is not None and has_codex_launcher:
            listen: str | None = None
            auth_env: str | None = None
            for index, token in enumerate(tokens):
                if token == "--listen" and index + 1 < len(tokens):
                    listen = tokens[index + 1]
                elif token.startswith("--listen="):
                    listen = token.split("=", 1)[1]
                elif token == "--bearer-token-env" and index + 1 < len(tokens):
                    auth_env = tokens[index + 1]
                elif token.startswith("--bearer-token-env="):
                    auth_env = token.split("=", 1)[1]
            if listen:
                context: dict[str, Any] = {
                    "source": "ancestor-listener",
                    "attachable": True,
                    "endpoint": listen,
                    "ancestor_endpoint": listen,
                    "endpoint_matches_ancestor": True,
                    "auth_token_env": auth_env,
                    "app_server_pid": int(row["pid"]),
                }
                try:
                    identity = capture_local_identity(int(row["pid"]))
                    if identity.source not in STRONG_PROCESS_IDENTITY_SOURCES:
                        raise ProcessIdentityError(
                            "owning app-server lacks a strong process start token"
                        )
                    context["owning_app_server_identity"] = (
                        durable_authority_process_identity(identity)
                    )
                except (OSError, ProcessIdentityError, TypeError, ValueError) as error:
                    context["owning_app_server_identity_error"] = str(error)
                return context
            return {
                "source": "ancestor-private-stdio",
                "attachable": False,
                "reason": "owning app-server uses private stdio and exposes no second-client endpoint",
                "app_server_pid": int(row["pid"]),
            }
        pid = int(row.get("ppid") or 0)
    if saw_codex_process:
        return {
            "source": "ancestor-embedded",
            "attachable": False,
            "reason": "owning Codex process exposes no attachable app-server listener",
        }
    return {"source": "undiscovered", "attachable": None}


def durable_authority_process_identity(identity: ProcessIdentity) -> dict[str, Any]:
    """Return the minimum process incarnation fence suitable for a ticket.

    The process command is useful during discovery but can contain credentials
    or other invocation details.  PID plus the platform start token is enough
    to detect exit and PID reuse, so authority tickets deliberately omit argv
    and all other observational process metadata.
    """

    return {
        "version": identity.version,
        "scope": identity.scope,
        "host": identity.host,
        "pid": identity.pid,
        "source": identity.source,
        "start_token": identity.start_token,
    }


def assess_authority_strength(
    app_server_context: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Classify whether a ticket identifies one local app-server incarnation.

    Public WS/WSS and Remote Control metadata identify an endpoint or logical
    environment, not one server process.  A strict ticket therefore requires
    all locally-verifiable signals: exact ancestor provenance, a strong process
    incarnation, and a connected Unix-socket inode fingerprint.
    """

    transport = str(authority.get("transport") or "")
    source = str(app_server_context.get("source") or "")
    raw_identity = app_server_context.get("owning_app_server_identity")
    ancestor_endpoint = str(
        app_server_context.get("ancestor_endpoint")
        or app_server_context.get("endpoint")
        or ""
    )
    authority_endpoint = str(authority.get("endpoint") or "")
    declared_endpoint_match = app_server_context.get("endpoint_matches_ancestor")
    canonical_endpoint_match = False
    reasons: list[str] = []
    identity: ProcessIdentity | None = None

    try:
        canonical_endpoint_match = (
            bool(ancestor_endpoint)
            and bool(authority_endpoint)
            and parse_app_server_endpoint(ancestor_endpoint).uri
            == parse_app_server_endpoint(authority_endpoint).uri
        )
    except AppServerError:
        canonical_endpoint_match = False

    if source != "ancestor-listener":
        reasons.append("endpoint was not discovered from the app-server ancestor")
    if app_server_context.get("attachable") is not True:
        reasons.append("app-server context is not attachable")
    if app_server_context.get("diagnostic_endpoint_only") is True:
        reasons.append("endpoint is diagnostic-only and cannot own continuation delivery")
    endpoint_matches_ancestor = bool(
        source == "ancestor-listener"
        and declared_endpoint_match is not False
        and canonical_endpoint_match
    )
    if source == "ancestor-listener" and not endpoint_matches_ancestor:
        reasons.append(
            "delivery endpoint is an explicit alias and does not exactly match "
            "the app-server ancestor listener"
        )
    if transport != "unix":
        reasons.append(
            "WS/WSS endpoints expose no public per-process app-server instance nonce"
        )
    if transport == "unix" and not authority.get("endpoint_fingerprint"):
        reasons.append("Unix endpoint has no connected device/inode fingerprint")
    if not isinstance(raw_identity, dict):
        reasons.append(
            str(app_server_context.get("owning_app_server_identity_error") or "")
            or "owning app-server process incarnation is unavailable"
        )
    else:
        try:
            identity = ProcessIdentity.from_dict(raw_identity)
            if (
                identity.scope != "local"
                or identity.host is not None
                or identity.source not in STRONG_PROCESS_IDENTITY_SOURCES
            ):
                raise ProcessIdentityError(
                    "owning app-server identity is not a strong local incarnation"
                )
            probe = probe_local_identity(identity)
            if probe.status != "alive":
                raise ProcessIdentityError(
                    "owning app-server process incarnation is not alive: "
                    f"{probe.reason}: {probe.detail}"
                )
        except (KeyError, TypeError, ValueError, ProcessIdentityError) as error:
            identity = None
            reasons.append(str(error))

    if not reasons and identity is not None:
        return {
            "authority_strength": "strong",
            "authority_strength_reason": (
                "exact ancestor Unix listener is fenced by socket inode and "
                "owning app-server process incarnation"
            ),
            "owner_process_identity": durable_authority_process_identity(identity),
            "authority_provenance": source,
            "endpoint_matches_ancestor": True,
            "ancestor_endpoint": ancestor_endpoint,
        }
    return {
        "authority_strength": "weak",
        "authority_strength_reason": "; ".join(dict.fromkeys(reasons)),
        "owner_process_identity": (
            durable_authority_process_identity(identity) if identity is not None else None
        ),
        "authority_provenance": source or "unknown",
        "endpoint_matches_ancestor": endpoint_matches_ancestor,
        "ancestor_endpoint": ancestor_endpoint or None,
    }


def probe_strong_authority_process(authority: dict[str, Any]) -> None:
    """Fail closed unless a strong ticket's exact app-server process is alive."""

    if authority.get("authority_strength") != "strong":
        return
    raw_identity = authority.get("owner_process_identity")
    if not isinstance(raw_identity, dict):
        raise AuthorityMismatch("strong authority ticket has no owner process identity")
    try:
        identity = ProcessIdentity.from_dict(raw_identity)
        if (
            identity.scope != "local"
            or identity.host is not None
            or identity.source not in STRONG_PROCESS_IDENTITY_SOURCES
        ):
            raise ProcessIdentityError(
                "strong authority ticket has an invalid process incarnation"
            )
        result = probe_local_identity(identity)
    except (KeyError, OSError, TypeError, ValueError, ProcessIdentityError) as error:
        raise AuthorityMismatch(
            f"cannot verify owning app-server process incarnation: {error}"
        ) from error
    if result.status != "alive":
        raise AuthorityMismatch(
            "owning app-server process incarnation is no longer alive: "
            f"{result.reason}: {result.detail}"
        )


def app_server_context_with_endpoint(
    discovered: dict[str, Any],
    endpoint: str,
    configuration_source: str,
) -> dict[str, Any]:
    """Overlay a client-facing endpoint without inventing owner provenance."""

    if discovered.get("attachable") is False:
        return {
            **discovered,
            "endpoint": endpoint,
            "endpoint_configuration_source": configuration_source,
            "endpoint_matches_ancestor": False,
            "diagnostic_endpoint_only": True,
            "reason": (
                f"{discovered.get('reason')}; a configured endpoint cannot become the owner "
                "of a shell spawned by that private authority"
            ),
        }
    if discovered.get("attachable") is True:
        ancestor_endpoint = str(
            discovered.get("ancestor_endpoint") or discovered["endpoint"]
        )
        try:
            requested_uri = parse_app_server_endpoint(endpoint).uri
            discovered_uri = parse_app_server_endpoint(ancestor_endpoint).uri
        except AppServerError as error:
            return {
                **discovered,
                "endpoint": endpoint,
                "endpoint_configuration_source": configuration_source,
                "ancestor_endpoint": ancestor_endpoint,
                "endpoint_matches_ancestor": False,
                "attachable": False,
                "diagnostic_endpoint_only": True,
                "reason": str(error),
            }
        endpoint_matches_ancestor = requested_uri == discovered_uri
        context = {
            **discovered,
            "endpoint": endpoint,
            "endpoint_configuration_source": configuration_source,
            "ancestor_endpoint": ancestor_endpoint,
            "endpoint_matches_ancestor": endpoint_matches_ancestor,
        }
        if not endpoint_matches_ancestor:
            context["reason"] = (
                "configured client-facing endpoint is an unproven alias of the app-server "
                f"ancestor listener ({requested_uri} != {discovered_uri})"
            )
        return context
    return {
        "source": configuration_source,
        "attachable": True,
        "endpoint": endpoint,
        "endpoint_configuration_source": configuration_source,
        "ancestor_endpoint": None,
        "endpoint_matches_ancestor": None,
    }


def app_server_context_from_args(args: argparse.Namespace) -> dict[str, Any]:
    discovered = ancestor_app_server_context()
    explicit = getattr(args, "app_server_endpoint", None)
    if explicit:
        return app_server_context_with_endpoint(
            discovered,
            str(explicit),
            "explicit-argument",
        )
    configured = os.environ.get("CODEX_WAIT_APP_SERVER_ENDPOINT")
    if configured:
        return app_server_context_with_endpoint(discovered, configured, "environment")
    if discovered.get("attachable"):
        return discovered
    return {
        **discovered,
        # The managed socket is still useful for read-only persisted routing
        # diagnostics, but must never be mistaken for this private owner.
        "endpoint": default_app_server_endpoint(),
        "diagnostic_endpoint_only": discovered.get("attachable") is False,
    }


def app_server_auth_env_from_args(args: argparse.Namespace) -> str | None:
    value = getattr(args, "app_server_auth_token_env", None)
    if value:
        return str(value)
    configured = os.environ.get("CODEX_WAIT_APP_SERVER_AUTH_TOKEN_ENV")
    if configured:
        return configured
    discovered = ancestor_app_server_context()
    auth_env = discovered.get("auth_token_env")
    return str(auth_env) if auth_env else None


def classify_delivery_decision(
    requested_protocol: str,
    *,
    route_verified: bool,
    attachable: bool,
    native_message_ready: bool,
    authority_strength: str | None,
    allow_weak_authority: bool,
    context_reason: str | None = None,
    probe_reason: str | None = None,
    authority_strength_reason: str | None = None,
) -> dict[str, str]:
    """Return the single protocol branch used by schedule and doctor.

    Actor routing and delivery authority are independent dimensions.  Keeping
    this decision pure and named prevents a private owner, unloaded thread, or
    weak endpoint from falling through to native delivery as the surrounding
    discovery code evolves.
    """

    if requested_protocol not in {"auto", "native-message", "marker"}:
        return {
            "action": "reject",
            "branch": "unsupported-protocol",
            "reason": f"unsupported resume protocol: {requested_protocol}",
        }
    if not route_verified:
        return {
            "action": "reject",
            "branch": "unverified-owner-route",
            "reason": (
                "the complete actor-to-owner route is unverified; neither native nor "
                "marker delivery can preserve the cross-fork job fence"
            ),
        }
    if requested_protocol == "marker":
        return {
            "action": "marker",
            "branch": "explicit-marker",
            "reason": "marker delivery was explicitly requested",
        }
    if not attachable:
        reason = context_reason or "the owner exposes no attachable app-server endpoint"
        return {
            "action": "reject" if requested_protocol == "native-message" else "marker",
            "branch": (
                "native-rejected-owner-not-attachable"
                if requested_protocol == "native-message"
                else "marker-owner-not-attachable"
            ),
            "reason": reason,
        }
    if not native_message_ready:
        reason = probe_reason or "the exact owner is not positively loaded on this authority"
        return {
            "action": "reject" if requested_protocol == "native-message" else "marker",
            "branch": (
                "native-rejected-owner-not-ready"
                if requested_protocol == "native-message"
                else "marker-owner-not-ready"
            ),
            "reason": reason,
        }
    if authority_strength == "strong":
        return {
            "action": "native-message",
            "branch": "native-strong-authority",
            "reason": "the loaded owner is behind the exact fenced ancestor Unix authority",
        }
    if authority_strength == "weak":
        reason = authority_strength_reason or "the owner authority has only weak instance binding"
        if allow_weak_authority:
            return {
                "action": "native-message",
                "branch": "native-weak-authority-opt-in",
                "reason": reason,
            }
        return {
            "action": "reject" if requested_protocol == "native-message" else "marker",
            "branch": (
                "native-rejected-weak-authority"
                if requested_protocol == "native-message"
                else "marker-weak-authority"
            ),
            "reason": reason,
        }
    reason = authority_strength_reason or "the owner authority strength is unavailable"
    return {
        "action": "reject" if requested_protocol == "native-message" else "marker",
        "branch": (
            "native-rejected-unclassified-authority"
            if requested_protocol == "native-message"
            else "marker-unclassified-authority"
        ),
        "reason": reason,
    }


class OwnerRoutingError(RuntimeError):
    """Raised when a nested thread cannot safely choose a durable event owner."""


def current_actor_thread_id(explicit_owner_thread_id: str | None = None) -> tuple[str, bool]:
    """Return the current executing thread, not the eventual delivery owner.

    Older versions of this script called the value passed to ``--session-id`` the
    current session.  That is ambiguous now that Codex gives every subagent and
    fork a distinct thread ID while also exposing a session-tree ID.  Shell tools
    receive the current *thread* in ``CODEX_THREAD_ID``.  An explicit owner can
    seed read-only diagnostics when that environment value is absent, but
    scheduling rejects the inferred actor because its ancestry is unproven.
    """

    actor_thread_id = os.environ.get("CODEX_THREAD_ID")
    if actor_thread_id:
        return actor_thread_id, False
    if explicit_owner_thread_id:
        return explicit_owner_thread_id, True
    fatal(
        "No current Codex thread id is available. Run this from Codex so "
        "CODEX_THREAD_ID is set. --owner-thread-id can identify a diagnostic "
        "target, but cannot replace the current actor identity for scheduling."
    )


def resolve_owner_route(
    endpoint: str | os.PathLike[str],
    actor_thread_id: str,
    explicit_owner_thread_id: str | None = None,
    bearer_token_env: str | None = None,
) -> dict[str, Any]:
    """Resolve a wait event to one durable user-owned branch.

    A normal durable thread or regular fork owns its own event.  A thread-spawn
    subagent routes to the root of its ``parentThreadId`` chain.  An ephemeral
    thread (including ``/side``) may never own or schedule a strict handoff in
    current Codex because ``thread/read`` does not preserve its fork parent.
    When the shared app-server is not reachable, the result is deliberately
    marked unverified so callers cannot mistake diagnostic behavior for a
    verified route.
    """

    route: dict[str, Any] = {
        "actor_thread_id": actor_thread_id,
        "owner_thread_id": explicit_owner_thread_id or actor_thread_id,
        "explicit_owner": bool(explicit_owner_thread_id),
        "metadata_verified": False,
        "route_verified": False,
        "route": "unverified-explicit" if explicit_owner_thread_id else "unverified-self",
        "ancestry": [],
    }
    try:
        with AppServerClient(endpoint, bearer_token_env=bearer_token_env) as client:
            actor = client.read_thread(actor_thread_id, include_turns=False)
            route["metadata_verified"] = True
            route["actor_source"] = actor.get("source")
            route["actor_session_id"] = actor.get("sessionId")
            route["actor_parent_thread_id"] = actor.get("parentThreadId")
            route["actor_forked_from_id"] = actor.get("forkedFromId")
            route["actor_ephemeral"] = bool(actor.get("ephemeral"))

            if bool(actor.get("ephemeral")):
                # Current app-server thread/read reconstructs pathless ephemeral
                # threads without forkedFromId, even though the initial fork
                # response had it.  A detached script therefore cannot prove a
                # supplied parent capability.  Fail closed until the API exposes
                # durable side ancestry; the user can return to the parent and
                # schedule there.
                raise OwnerRoutingError(
                    "ephemeral threads (including /side conversations) cannot schedule a strict "
                    "handoff in this Codex version because thread/read does not preserve their "
                    "fork parent; return to the durable parent and schedule there"
                )

            ancestry = [actor_thread_id]
            seen = {actor_thread_id}
            current = actor
            while current.get("parentThreadId"):
                parent_thread_id = str(current["parentThreadId"])
                if parent_thread_id in seen:
                    raise OwnerRoutingError("cycle detected in Codex subagent parentThreadId chain")
                if len(ancestry) >= 64:
                    raise OwnerRoutingError("Codex subagent ancestry exceeds the routing safety limit")
                seen.add(parent_thread_id)
                ancestry.append(parent_thread_id)
                current = client.read_thread(parent_thread_id, include_turns=False)

            root_thread_id = ancestry[-1]
            owner_branch = current
            fork_lineage = [root_thread_id]
            fork_seen = {root_thread_id}
            lineage_cursor = owner_branch
            while lineage_cursor.get("forkedFromId"):
                source_thread_id = str(lineage_cursor["forkedFromId"])
                if source_thread_id in fork_seen:
                    raise OwnerRoutingError("cycle detected in Codex forkedFromId lineage")
                if len(fork_lineage) >= 64:
                    raise OwnerRoutingError(
                        "Codex fork lineage exceeds the routing safety limit"
                    )
                fork_seen.add(source_thread_id)
                fork_lineage.append(source_thread_id)
                lineage_cursor = client.read_thread(
                    source_thread_id,
                    include_turns=False,
                )
                if bool(lineage_cursor.get("ephemeral")):
                    raise OwnerRoutingError(
                        "fork lineage crosses an ephemeral thread and cannot form a durable job fence"
                    )
            job_scope_id = fork_lineage[-1]
            if len(ancestry) > 1:
                if explicit_owner_thread_id and explicit_owner_thread_id != root_thread_id:
                    if explicit_owner_thread_id == actor_thread_id:
                        raise OwnerRoutingError(
                            "subagent resume-self is not a strict wake path: the child can be "
                            "unloaded and its completion does not wake an idle parent"
                        )
                    raise OwnerRoutingError(
                        "a subagent may route only to its durable agent-tree root; refusing "
                        f"owner {explicit_owner_thread_id!r} (root is {root_thread_id})"
                    )
                if bool(current.get("ephemeral")):
                    raise OwnerRoutingError("the subagent tree root is ephemeral and cannot own a handoff")
                route.update(
                    {
                        "owner_thread_id": root_thread_id,
                        "owner_session_id": owner_branch.get("sessionId"),
                        "job_scope_id": job_scope_id,
                        "fork_lineage": fork_lineage,
                        "route": "subagent-to-agent-tree-root",
                        "route_verified": True,
                        "ancestry": ancestry,
                        "owner_ephemeral": False,
                    }
                )
                return route

            owner_thread_id = explicit_owner_thread_id or actor_thread_id
            if owner_thread_id != actor_thread_id:
                raise OwnerRoutingError(
                    "a durable top-level thread or regular fork may schedule only for itself; "
                    "cross-thread retargeting is reserved for verified child-to-ancestor routing"
                )
            route.update(
                {
                    "owner_thread_id": actor_thread_id,
                    "owner_session_id": actor.get("sessionId"),
                    "job_scope_id": job_scope_id,
                    "fork_lineage": fork_lineage,
                    "route": "durable-self",
                    "route_verified": True,
                    "ancestry": ancestry,
                    "owner_ephemeral": False,
                }
            )
            return route
    except OwnerRoutingError:
        raise
    except (AppServerError, OSError, socket.timeout) as error:
        if route.get("metadata_verified"):
            raise OwnerRoutingError(
                "Codex actor metadata was readable, but its complete parentThreadId ancestry "
                f"could not be verified; refusing to fall back to child resume-self: {error}"
            ) from error
        route["metadata_error"] = str(error)
        return route


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
        ensure_private_directory(path)
    return paths


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


def task_runtime_snapshot(task_payload: dict[str, Any]) -> dict[str, Any]:
    """Describe one watcher using only its durable process incarnation.

    Status and bulk-stop selection must share the same identity boundary as
    cancellation.  Process-table argv matching can misidentify a reused PID or
    an unrelated command containing a task id, so it is intentionally absent
    here.  ``process_rows`` remains solely for ancestor app-server discovery.
    """

    try:
        watcher_pid = int(task_payload.get("watcher_pid") or 0)
    except (TypeError, ValueError):
        watcher_pid = 0
    watcher_alive = False
    watcher_identity_status = "missing"
    watcher_identity_reason = "missing"
    watcher_identity_detail = "task has no durable watcher process incarnation"
    try:
        identity = persisted_watcher_identity(task_payload)
        probe = probe_local_identity(identity)
        watcher_identity_status = probe.status
        watcher_identity_reason = probe.reason
        watcher_identity_detail = probe.detail
        watcher_alive = probe.status == "alive"
    except (OSError, ProcessIdentityError, TypeError, ValueError) as error:
        if isinstance(task_payload.get("watcher_identity"), dict):
            watcher_identity_status = "invalid"
            watcher_identity_reason = "identity_invalid"
            watcher_identity_detail = str(error)

    related_pids = [watcher_pid] if watcher_alive and watcher_pid > 0 else []
    return {
        "task_id": task_payload.get("task_id"),
        "phase": task_payload.get("phase"),
        "target": task_payload.get("target"),
        "watcher_pid": watcher_pid or None,
        "watcher_alive": watcher_alive,
        "watcher_identity_status": watcher_identity_status,
        "watcher_identity_reason": watcher_identity_reason,
        "watcher_identity_detail": watcher_identity_detail,
        "related_pids": related_pids,
        "note": task_payload.get("note") or "",
    }


def active_and_stale_task_snapshots(
    tasks_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_entries: list[dict[str, Any]] = []
    stale_entries: list[dict[str, Any]] = []
    for task_file in sorted(tasks_dir.glob("*.json")):
        task_payload = load_json(task_file)
        if task_payload.get("phase") not in ACTIVE_PHASES:
            continue
        snapshot = task_runtime_snapshot(task_payload)
        if snapshot["watcher_alive"]:
            active_entries.append(snapshot)
            continue
        stale_entries.append(snapshot)
    return active_entries, stale_entries


def persisted_watcher_identity(task_payload: dict[str, Any]) -> ProcessIdentity:
    """Load the exact watcher incarnation captured before its WATCHING ACK.

    Cancellation must never rediscover a watcher from a PID or command line.
    Those are mutable observations and can name an unrelated process after PID
    reuse.  The durable start token is the sole signal-delivery capability.
    """

    raw_identity = task_payload.get("watcher_identity")
    if not isinstance(raw_identity, dict):
        raise ProcessIdentityError(
            "task has no durable watcher process incarnation; refusing PID-only cancellation"
        )
    try:
        identity = ProcessIdentity.from_dict(raw_identity)
    except (KeyError, TypeError, ValueError) as error:
        raise ProcessIdentityError(
            f"invalid persisted watcher process identity: {error}"
        ) from error
    if identity.scope != "local" or identity.host is not None:
        raise ProcessIdentityError("persisted watcher identity must be local")
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    if watcher_pid <= 0 or identity.pid != watcher_pid:
        raise ProcessIdentityError(
            "persisted watcher identity does not match the durable watcher_pid"
        )
    if identity.source not in {"linux-proc-starttime", "macos-proc-starttime"}:
        raise ProcessIdentityError(
            "persisted watcher identity lacks a strong process start token"
        )
    return identity


def terminate_persisted_watcher(
    task_payload: dict[str, Any],
    *,
    grace_seconds: float = 3.0,
) -> dict[str, Any]:
    """Stop only the watcher incarnation recorded before startup ACK.

    Missing, malformed, or unprobeable identity is a cancellation fence.  A
    reused PID is safe: ``terminate_local_identity`` recognizes that the
    original watcher exited and sends no signal to its replacement.
    """

    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    requested_pids = [watcher_pid] if watcher_pid > 0 else []
    try:
        identity = persisted_watcher_identity(task_payload)
    except ProcessIdentityError as error:
        return {
            "safe_to_cancel": False,
            "requested_pids": requested_pids,
            "excluded_pids": [],
            "terminated_pids": [],
            "still_alive_pids": requested_pids,
            "identity_error": str(error),
        }

    try:
        result = terminate_local_identity(identity, grace_seconds=grace_seconds)
    except (OSError, ProcessIdentityError, ValueError) as error:
        return {
            "safe_to_cancel": False,
            "requested_pids": [identity.pid],
            "excluded_pids": [],
            "terminated_pids": [],
            "still_alive_pids": [identity.pid],
            "identity_error": str(error),
        }

    status = str(result.get("status") or "")
    safe_to_cancel = status in {"stopped", "original_exited"}
    return {
        "safe_to_cancel": safe_to_cancel,
        "requested_pids": [identity.pid],
        "excluded_pids": [],
        "terminated_pids": [identity.pid] if status == "stopped" else [],
        "still_alive_pids": [] if safe_to_cancel else [identity.pid],
        "identity_results": [result],
        "identity_error": None if safe_to_cancel else str(result.get("reason") or status),
    }


def wait_local_pid_exit_event(
    identity: ProcessIdentity,
    max_wait_seconds: int,
) -> tuple[str, str, str] | None:
    """Wait on a kernel process-exit event when the platform exposes one.

    Returns ``(completion_reason, detail, mechanism)``. ``None`` means the
    caller should use the portable polling fallback.
    """
    if identity.scope != "local":
        raise ValueError("kernel process wait requires a local identity")
    pid = identity.pid
    timeout_seconds = max(int(max_wait_seconds), 1)

    pidfd_open = getattr(os, "pidfd_open", None)
    poll_type = getattr(select, "poll", None)
    if callable(pidfd_open) and callable(poll_type):
        try:
            pidfd = pidfd_open(pid, 0)
        except ProcessLookupError:
            return ("process_exited", f"pid {pid} exited before pidfd registration", "pidfd")
        except (OSError, PermissionError):
            pass
        else:
            try:
                state, detail = probe_bound_identity(identity)
                if state == "dead":
                    return ("process_exited", detail, "pidfd")
                if state == "unknown":
                    return None
                poller = poll_type()
                poller.register(pidfd, select.POLLIN)
                events = poller.poll(timeout_seconds * 1000)
                if events:
                    return ("process_exited", f"pidfd reported exit for pid {pid}", "pidfd")
                state, detail = probe_bound_identity(identity)
                return (
                    "process_exited" if state == "dead" else "max_wait_reached",
                    detail,
                    "pidfd",
                )
            finally:
                os.close(pidfd)

    if all(
        hasattr(select, name)
        for name in ("kqueue", "kevent", "KQ_FILTER_PROC", "KQ_NOTE_EXIT")
    ):
        queue = select.kqueue()
        try:
            flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=flags,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                queue.control([event], 0, 0)
            except ProcessLookupError:
                return ("process_exited", f"pid {pid} exited before kqueue registration", "kqueue")
            except OSError:
                state, detail = probe_bound_identity(identity)
                if state == "dead":
                    return ("process_exited", detail, "kqueue")
                return None
            state, detail = probe_bound_identity(identity)
            if state == "dead":
                return ("process_exited", detail, "kqueue")
            if state == "unknown":
                return None
            events = queue.control(None, 1, timeout_seconds)
            if events:
                return ("process_exited", f"kqueue NOTE_EXIT reported pid {pid}", "kqueue")
            state, detail = probe_bound_identity(identity)
            return (
                "process_exited" if state == "dead" else "max_wait_reached",
                detail,
                "kqueue",
            )
        finally:
            queue.close()

    return None


def target_identities(target: dict[str, Any]) -> tuple[ProcessIdentity, ...]:
    values = target.get("process_identities")
    if not isinstance(values, list):
        raise ProcessIdentityError(
            "target has no schedule-time process incarnation; refusing PID-only operation"
        )
    try:
        identities = tuple(ProcessIdentity.from_dict(value) for value in values)
    except (KeyError, TypeError, ValueError) as error:
        raise ProcessIdentityError(f"invalid persisted process identity: {error}") from error
    scope = str(target.get("scope") or "")
    host = str(target.get("host")) if target.get("host") is not None else None
    if not identities:
        return identities
    if any(identity.scope != scope or identity.host != host for identity in identities):
        raise ProcessIdentityError("persisted process identity does not match target scope/host")
    keys = {
        (
            identity.version,
            identity.scope,
            identity.host,
            identity.pid,
            identity.source,
            identity.start_token,
        )
        for identity in identities
    }
    if len(keys) != len(identities):
        raise ProcessIdentityError("persisted process identity set contains duplicates")
    if target.get("mode") == "pid" and (
        len(identities) != 1 or identities[0].pid != int(target["pid"])
    ):
        raise ProcessIdentityError("PID target does not match exactly one persisted incarnation")
    return identities


def bind_target_identities(target: dict[str, Any]) -> dict[str, Any]:
    """Capture the exact process incarnation(s) before detaching a watcher."""

    scope = str(target["scope"])
    mode = str(target["mode"])
    if mode == "pid":
        pid = int(target["pid"])
        identity = (
            capture_remote_identity(str(target["host"]), pid)
            if scope == "remote"
            else capture_local_identity(pid)
        )
        identities = (identity,)
    elif mode == "pattern":
        pattern = str(target["pattern"])
        identities = (
            find_remote_pattern(str(target["host"]), pattern)
            if scope == "remote"
            else find_local_pattern(pattern)
        )
    else:
        raise ValueError(f"Unsupported target: {target}")
    weak = [identity for identity in identities if identity.source == "ps-lstart"]
    if weak:
        raise ProcessIdentityError(
            "this host exposes only a second-resolution ps start time; strict wait/stop "
            "fencing requires Linux /proc start ticks or local macOS libproc"
        )
    target["process_identities"] = [identity.to_dict() for identity in identities]
    target["identity_binding"] = "schedule-time-incarnations"
    return target


def probe_bound_identity(identity: ProcessIdentity) -> tuple[str, str]:
    result = (
        probe_remote_identity(identity)
        if identity.scope == "remote"
        else probe_local_identity(identity)
    )
    return result.as_legacy_tuple()


def stop_target(target: dict[str, Any]) -> dict[str, Any]:
    identities = target_identities(target)
    results = [
        (
            terminate_remote_identity(identity)
            if identity.scope == "remote"
            else terminate_local_identity(identity)
        )
        for identity in identities
    ]
    statuses = {str(result.get("status")) for result in results}
    aggregate = (
        "already_absent"
        if not identities
        else "stopped"
        if statuses <= {"stopped", "original_exited"}
        else "incomplete"
    )
    return {
        "scope": target["scope"],
        "mode": target["mode"],
        "host": target.get("host"),
        "pattern": target.get("pattern"),
        "matched_identities": [identity.to_dict() for identity in identities],
        "results": results,
        "status": aggregate,
    }


def probe_target(target: dict[str, Any]) -> tuple[str, str]:
    try:
        identities = target_identities(target)
    except ProcessIdentityError as error:
        return ("unknown", str(error))
    if not identities:
        return ("dead", "no process incarnation matched at schedule time")
    results = [probe_bound_identity(identity) for identity in identities]
    unknown = [detail for state, detail in results if state == "unknown"]
    if unknown:
        return ("unknown", "; ".join(unknown))
    alive = [detail for state, detail in results if state == "alive"]
    if alive:
        return ("alive", "; ".join(alive))
    return (
        "dead",
        "all schedule-time process incarnations exited or their PIDs were reused",
    )


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
        try:
            host = validate_remote_host(host)
        except ProcessIdentityError as error:
            fatal(f"Unsafe observed-log SSH host: {error}")
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
    owner_thread_id: str,
    event_id: str,
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
                    "and schedule another blocking wait on the same precise target with a new explicit "
                    "--job-id. If it is unhealthy, stuck, "
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
            f"event_id: {event_id}",
            f"owner_thread_id: {owner_thread_id}",
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


def resume_prompt_digest(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def load_verified_resume_prompt(task_payload: dict[str, Any]) -> str:
    prompt_text = read_text(Path(str(task_payload["prompt_file"])))
    expected = task_payload.get("resume_prompt_sha256")
    if not isinstance(expected, str) or not expected:
        fatal("Completion event has no durable final-prompt digest; refusing delivery.")
    actual = resume_prompt_digest(prompt_text)
    if actual != expected:
        fatal("Final continuation prompt digest mismatch; refusing delivery.")
    return prompt_text


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
        fatal(
            "No owner thread id is available. Pass --owner-thread-id or run this from Codex "
            "so CODEX_THREAD_ID is set."
        )
    return value


def requested_owner_thread_id(args: argparse.Namespace) -> str | None:
    owner_thread_id = getattr(args, "owner_thread_id", None)
    legacy_session_id = getattr(args, "session_id", None)
    if owner_thread_id and legacy_session_id and owner_thread_id != legacy_session_id:
        fatal("--owner-thread-id and deprecated --session-id name different threads.")
    return owner_thread_id or legacy_session_id


def assert_current_owner_reservation(task_file: Path, task_payload: dict[str, Any]) -> None:
    """Fence a watcher to the exact durable owner reservation that launched it."""

    reservation_token = str(task_payload.get("reservation_token") or "")
    generation = int(task_payload.get("lock_generation") or 0)
    if not reservation_token or generation <= 0:
        fatal(
            "Task has no complete owner reservation identity; refusing to start a watcher.\n"
            f"task_file: {task_file}"
        )
    if task_uses_ledger(task_payload):
        try:
            reservation = ledger_for_task(task_payload).validate(
                str(task_payload["task_id"]),
                reservation_token,
                generation,
            )
        except LedgerError as error:
            fatal(
                "Watcher reservation token/generation no longer owns this FIFO event; "
                f"refusing stale replay for task {task_file.stem}: {error}"
            )
        if reservation.get("task_file") != str(task_file):
            fatal(
                "Watcher ledger points to a different task file; refusing stale replay for "
                f"task {task_file.stem}."
            )
        return

    # Marker tasks have no app-server authority ledger. Their immutable task
    # path plus the lifetime watcher flock fence duplicate watcher execution.
    if task_payload.get("task_file") != str(task_file):
        fatal(f"Marker watcher task path mismatch for {task_file.stem}.")


def cancellation_block_reason(task_payload: dict[str, Any]) -> str | None:
    phase = str(task_payload.get("phase") or "")
    if phase not in CANCEL_FAIL_CLOSED_PHASES:
        return None
    if phase in {"reserving", "registration_recovery_required", "scheduled"}:
        return (
            "schedule-to-watcher handoff is still in progress; retry after the watcher records "
            "phase=watching"
        )
    if phase in {"native_message_unknown", "native_message_submitting", "native_message_submitted"}:
        return (
            "the native message may already have entered the owner app-server; reconcile its "
            "clientUserMessageId before changing any terminal state"
        )
    if phase == "native_message_accepted":
        return "the native user message is already confirmed on the owner event stream"
    return (
        "the completion event already entered the ordered delivery queue; reconcile or resolve "
        "that exact event before cancelling"
    )


@contextmanager
def exclusive_watcher_guard(task_file: Path):
    """Ensure only one watcher process can advance a task state machine."""

    guard_path = task_file.with_suffix(task_file.suffix + ".watch.guard")
    descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fatal(f"Another watcher already owns task {task_file.stem}.")
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def try_watcher_guard(task_file: Path):
    """Try to adopt a crashed earlier FIFO event without disturbing a live watcher."""

    guard_path = task_file.with_suffix(task_file.suffix + ".watch.guard")
    descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def write_watcher_startup_ack(descriptor: int, task_payload: dict[str, Any]) -> None:
    acknowledgement = {
        "task_id": task_payload["task_id"],
        "reservation_token": task_payload["reservation_token"],
        "lock_generation": task_payload["lock_generation"],
        "watcher_pid": os.getpid(),
        "watcher_identity": task_payload["watcher_identity"],
        "phase": "watching",
    }
    encoded = (json.dumps(acknowledgement, sort_keys=True) + "\n").encode("utf-8")
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
    except BrokenPipeError:
        # Parent death must not kill a watcher that already durably owns the
        # reservation. A live parent will treat the missing ACK as startup
        # failure, terminate this child, and cancel only after taking its lock.
        pass
    finally:
        os.close(descriptor)


def terminate_spawned_watcher(watcher: subprocess.Popen[Any]) -> None:
    if watcher.poll() is not None:
        return
    try:
        watcher.terminate()
    except ProcessLookupError:
        return
    try:
        watcher.wait(timeout=3)
    except subprocess.TimeoutExpired:
        watcher.kill()
        watcher.wait(timeout=3)


def spawn_watcher_with_ack(
    task_file: Path,
    log_file: Path,
    *,
    timeout_seconds: float = WATCHER_STARTUP_TIMEOUT_SECONDS,
) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    """Spawn a detached watcher and require its durable WATCHING acknowledgement."""

    read_descriptor, write_descriptor = os.pipe()
    watcher: subprocess.Popen[Any] | None = None
    watcher_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--task-file",
        str(task_file),
        "--startup-fd",
        str(write_descriptor),
    ]
    try:
        with open_private_append(log_file) as log_handle:
            watcher = subprocess.Popen(
                watcher_command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(write_descriptor,),
            )
        os.close(write_descriptor)
        write_descriptor = -1

        readable, _, _ = select.select(
            [read_descriptor],
            [],
            [],
            max(float(timeout_seconds), 0.0),
        )
        if not readable:
            raise RuntimeError(
                f"watcher did not acknowledge WATCHING within {timeout_seconds:g}s"
            )
        encoded = os.read(read_descriptor, 65536)
        if not encoded:
            returncode = watcher.poll()
            raise RuntimeError(
                "watcher exited before acknowledging WATCHING"
                + (f" (returncode={returncode})" if returncode is not None else "")
            )
        try:
            acknowledgement = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"watcher returned an invalid startup acknowledgement: {error}") from error
        task_payload = load_json(task_file)
        expected = {
            "task_id": task_payload["task_id"],
            "reservation_token": task_payload["reservation_token"],
            "lock_generation": task_payload["lock_generation"],
            "watcher_pid": watcher.pid,
            "watcher_identity": task_payload["watcher_identity"],
            "phase": "watching",
        }
        if acknowledgement != expected:
            raise RuntimeError(
                "watcher startup acknowledgement did not match the durable reservation"
            )
        return watcher, acknowledgement
    except BaseException:
        if watcher is not None:
            terminate_spawned_watcher(watcher)
        raise
    finally:
        if write_descriptor >= 0:
            os.close(write_descriptor)
        os.close(read_descriptor)


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True), flush=True)
        return
    for key, value in payload.items():
        print(f"{key}: {value}", flush=True)


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


def job_key_for_target(
    coordination_scope_id: str,
    target: dict[str, Any],
    logical_job_id: str = "process-lifetime",
) -> str:
    """Identify one incarnation set across a verified ordinary-fork lineage."""

    immutable_identities = sorted(
        (
            {
                "version": identity.version,
                "scope": identity.scope,
                "host": identity.host,
                "pid": identity.pid,
                "source": identity.source,
                "start_token": identity.start_token,
            }
            for identity in target_identities(target)
        ),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    identity = {
        "coordination_scope_id": coordination_scope_id,
        "logical_job_id": logical_job_id,
        # PID and pattern are discovery syntax, not job identity.  The same
        # incarnation reached by either syntax must share one reservation.
        "process_incarnations": immutable_identities,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ledger_for_task(task_payload: dict[str, Any]) -> HandoffLedger:
    return HandoffLedger(
        str(task_payload.get("owner_ledger_dir") or DEFAULT_COORDINATION_DIR),
        str(task_payload.get("owner_ledger_key") or task_payload["owner_thread_id"]),
    )


def task_uses_ledger(task_payload: dict[str, Any]) -> bool:
    return bool(
        task_payload.get("resume_protocol") == "native-message"
        or task_payload.get("owner_ledger_key")
    )


def job_registry_for_task(task_payload: dict[str, Any]) -> OwnerJobRegistry:
    return OwnerJobRegistry(
        str(task_payload.get("job_registry_dir") or DEFAULT_COORDINATION_DIR),
        str(task_payload.get("job_scope_id") or task_payload["owner_thread_id"]),
    )


def finish_job_reservation(task_payload: dict[str, Any], outcome: str) -> None:
    generation = task_payload.get("job_reservation_generation")
    if generation is None:
        return
    job_registry_for_task(task_payload).finish(
        str(task_payload["task_id"]),
        str(task_payload["reservation_token"]),
        int(generation),
        outcome,
    )


LEDGER_JOB_OUTCOMES = {
    "ACCEPTED": "accepted",
    "BLOCKED": "blocked",
    "CANCELLED": "cancelled",
    "UNKNOWN": "unknown",
}


def sync_job_reservation_from_ledger(
    task_payload: dict[str, Any],
    entry: dict[str, Any] | None = None,
) -> str | None:
    """Converge the protocol-independent job fence from the owner ledger.

    The owner ledger is committed first at every terminal transition.  If a
    process dies before updating the common native/marker registry, any later
    status, recovery, cancellation, or dispatch path can safely finish that
    second write without replaying the event.
    """

    if task_payload.get("job_reservation_generation") is None:
        return None
    if entry is None:
        task_id_value, token, generation = task_reservation_identity(task_payload)
        entry = ledger_for_task(task_payload).validate(
            task_id_value,
            token,
            generation,
        )
    state = str(entry.get("state") or "")
    outcome = LEDGER_JOB_OUTCOMES.get(state)
    if outcome is not None:
        finish_job_reservation(task_payload, outcome)
    return state


CANCEL_PRE_READY_LEDGER_STATES = {"SCHEDULED", "WATCHING"}


def delivery_phase_for_ledger_state(protocol: str, state: str) -> str | None:
    if state == "READY":
        return "marker_pending" if protocol == "marker" else "native_message_ready"
    if state == "SUBMITTING":
        return "marker_claiming" if protocol == "marker" else "native_message_submitting"
    if state == "UNKNOWN":
        return "marker_unknown" if protocol == "marker" else "native_message_unknown"
    if state == "ACCEPTED":
        return "marker_claimed" if protocol == "marker" else "native_message_accepted"
    if state == "BLOCKED":
        return "marker_blocked" if protocol == "marker" else "native_message_blocked"
    if state == "CANCELLED":
        return "cancelled"
    return None


def cancellation_result_for_ledger_state(
    task_file: Path,
    task_payload: dict[str, Any],
    entry: dict[str, Any],
    *,
    mirror_task: bool,
) -> dict[str, Any]:
    """Converge terminal common state and describe why cancellation cannot advance."""

    state = str(entry.get("state") or "")
    if state in LEDGER_JOB_OUTCOMES:
        sync_job_reservation_from_ledger(task_payload, entry)
    phase = delivery_phase_for_ledger_state(
        str(task_payload.get("resume_protocol") or ""),
        state,
    )
    if mirror_task and phase is not None:
        task_payload["phase"] = phase
        task_payload["delivery_status"] = f"cancel_observed_owner_ledger_{state.lower()}"
        task_payload["owner_ledger_mirrored_at"] = now_utc()
        write_json(task_file, task_payload)

    status_by_state = {
        "CANCELLED": "cancelled",
        "ACCEPTED": "already_accepted",
        "BLOCKED": "already_blocked",
    }
    warning_by_state = {
        "READY": "the completion event is already in the ordered delivery queue",
        "SUBMITTING": "the completion event has already entered submission",
        "UNKNOWN": "the completion event has an ambiguous submission outcome",
        "ACCEPTED": "the completion event is already accepted",
        "BLOCKED": "the completion event is already blocked",
        "CANCELLED": "the completion event was already cancelled",
    }
    return {
        "status": status_by_state.get(state, "cancel_blocked"),
        "task_id": str(task_payload.get("task_id") or ""),
        "owner_ledger_state": state,
        "common_job_reservation_synced": state in LEDGER_JOB_OUTCOMES,
        "task_phase_mirrored": bool(mirror_task and phase is not None),
        "warning": warning_by_state.get(
            state,
            f"owner ledger state {state or 'missing'} is not cancellable",
        ),
    }


def cancellation_ledger_error(
    task_payload: dict[str, Any],
    error: BaseException,
) -> dict[str, Any]:
    return {
        "status": "cancel_blocked",
        "task_id": str(task_payload.get("task_id") or ""),
        "warning": (
            "the exact owner-ledger state could not be verified; no watcher signal or "
            "reservation transition was attempted"
        ),
        "ledger_error": str(error),
    }


def preflight_cancellation_ledger(
    task_file: Path,
    task_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Refuse to signal a watcher once its owner ledger passed WATCHING."""

    if not task_uses_ledger(task_payload):
        return None
    if task_payload.get("lock_generation") is None:
        return cancellation_ledger_error(
            task_payload,
            ValueError("task is missing its exact owner-ledger generation"),
        )
    try:
        task_id_value, token, generation = task_reservation_identity(task_payload)
        entry = ledger_for_task(task_payload).validate(task_id_value, token, generation)
    except (KeyError, TypeError, ValueError, LedgerError) as error:
        return cancellation_ledger_error(task_payload, error)
    if str(entry.get("state") or "") in CANCEL_PRE_READY_LEDGER_STATES:
        return None
    try:
        result = cancellation_result_for_ledger_state(
            task_file,
            task_payload,
            entry,
            mirror_task=False,
        )
    except JobRegistryError as error:
        return cancellation_ledger_error(task_payload, error)

    # Mirror a stale task phase only if no live watcher owns the task file. The
    # terminal common reservation was already synchronized above regardless.
    with try_watcher_guard(task_file) as acquired:
        if not acquired:
            return result
        fresh_payload = load_json(task_file)
        try:
            task_id_value, token, generation = task_reservation_identity(fresh_payload)
            fresh_entry = ledger_for_task(fresh_payload).validate(
                task_id_value,
                token,
                generation,
            )
            if str(fresh_entry.get("state") or "") in CANCEL_PRE_READY_LEDGER_STATES:
                return None
            return cancellation_result_for_ledger_state(
                task_file,
                fresh_payload,
                fresh_entry,
                mirror_task=True,
            )
        except (KeyError, TypeError, ValueError, JobRegistryError, LedgerError) as error:
            return cancellation_ledger_error(fresh_payload, error)


def cancel_owner_ledger_or_reconcile(
    task_file: Path,
    task_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Cancel a pre-ready ledger entry while the caller holds the task guard."""

    if not task_uses_ledger(task_payload):
        return None
    try:
        task_id_value, token, generation = task_reservation_identity(task_payload)
        ledger = ledger_for_task(task_payload)
        entry = ledger.validate(task_id_value, token, generation)
        if str(entry.get("state") or "") not in CANCEL_PRE_READY_LEDGER_STATES:
            return cancellation_result_for_ledger_state(
                task_file,
                task_payload,
                entry,
                mirror_task=True,
            )
        try:
            ledger.cancel(task_id_value, token, generation)
            return None
        except (InvalidTransition, LedgerError):
            # A dispatcher may have advanced the ledger after the signal-side
            # preflight. Re-read and converge terminality instead of reporting
            # only a stale InvalidTransition and leaving the common fence ACTIVE.
            entry = ledger.validate(task_id_value, token, generation)
            if str(entry.get("state") or "") not in CANCEL_PRE_READY_LEDGER_STATES:
                return cancellation_result_for_ledger_state(
                    task_file,
                    task_payload,
                    entry,
                    mirror_task=True,
                )
            raise
    except (KeyError, TypeError, ValueError, JobRegistryError, LedgerError) as error:
        return cancellation_ledger_error(task_payload, error)


def ensure_task_reservations(
    task_file: Path,
    task_payload: dict[str, Any],
) -> tuple[dict[str, Any], HandoffLedger, dict[str, Any]]:
    """Idempotently reconstruct both reservation generations after a crash."""

    owner_thread_id = str(task_payload["owner_thread_id"])
    task_id_value = str(task_payload["task_id"])
    event_id = str(task_payload["event_id"])
    reservation_token = str(task_payload["reservation_token"])
    protocol = str(task_payload["resume_protocol"])
    job_registry = job_registry_for_task(task_payload)
    job_generation = job_registry.reserve(
        str(task_payload["job_key"]),
        task_id_value,
        event_id,
        task_file,
        reservation_token,
        protocol,
        owner_thread_id,
    )
    stored_job_generation = task_payload.get("job_reservation_generation")
    if stored_job_generation is not None and int(stored_job_generation) != job_generation:
        raise JobRegistryError("task file contains a stale job reservation generation")
    job_entry = job_registry.validate(task_id_value, reservation_token, job_generation)
    if job_entry.get("state") != "ACTIVE":
        raise JobRegistryError(
            "interrupted registration points to a non-ACTIVE common job reservation"
        )
    task_payload["job_reservation_generation"] = job_generation
    task_payload["job_registry_file"] = str(job_registry.json_path)
    task_payload["job_registry_lock"] = str(job_registry.lock_path)
    # Persist this generation before taking the second reservation.  A crash at
    # either boundary is repaired by the exact idempotent reserve/register calls.
    write_json(task_file, task_payload)

    owner_ledger_key = str(
        task_payload.get("owner_ledger_key")
        or (
            owner_thread_id
            if protocol == "native-message"
            else f"{MARKER_LEDGER_PREFIX}{owner_thread_id}"
        )
    )
    task_payload["owner_ledger_key"] = owner_ledger_key
    ledger = HandoffLedger(
        str(task_payload.get("owner_ledger_dir") or DEFAULT_COORDINATION_DIR),
        owner_ledger_key,
    )
    authority = (
        task_payload.get("authority")
        if protocol == "native-message"
        else MARKER_AUTHORITY
    )
    if not isinstance(authority, dict):
        raise LedgerError("native-message task is missing its authority descriptor")
    generation = ledger.register(
        task_id_value,
        event_id,
        task_file,
        reservation_token,
        authority,
        str(task_payload["job_key"]),
    )
    stored_generation = task_payload.get("lock_generation")
    if stored_generation is not None and int(stored_generation) != generation:
        raise LedgerError("task file contains a stale owner-ledger generation")
    task_payload["lock_generation"] = generation
    entry = ledger.validate(task_id_value, reservation_token, generation)
    task_payload["authority_epoch"] = int(entry["authority_epoch"])
    task_payload["owner_ledger_file"] = str(ledger.json_path)
    task_payload["owner_ledger_lock"] = str(ledger.lock_path)
    write_json(task_file, task_payload)
    return task_payload, ledger, entry


def reconcile_registration_failure(
    task_file: Path,
    task_payload: dict[str, Any],
    error: BaseException,
) -> str:
    """Compensate only after proving this task has no owner-ledger entry.

    The common job registry commits before the ordered owner ledger.  If the
    second commit succeeds but its caller receives an error, cancelling the
    common reservation would split the two durable authorities and permit a
    duplicate job.  Exact, read-only lookups run under each store's lock; any
    collision or inspection failure is treated as uncertainty and preserves
    the common reservation for explicit recovery.

    The caller must hold this task's lifetime guard so a concurrent recovery
    cannot register the owner entry between the absence proof and compensation.
    """

    owner_thread_id = str(task_payload["owner_thread_id"])
    task_id_value = str(task_payload["task_id"])
    event_id = str(task_payload["event_id"])
    reservation_token = str(task_payload["reservation_token"])
    protocol = str(task_payload["resume_protocol"])
    job_key = str(task_payload["job_key"])
    task_payload["registration_error"] = str(error)
    task_payload["registration_failed_at"] = now_utc()

    inspection_errors: dict[str, str] = {}
    job_registry = job_registry_for_task(task_payload)
    common_entry: dict[str, Any] | None = None
    common_inspected = False
    try:
        common_entry = job_registry.find_exact_reservation(
            job_key,
            task_id_value,
            event_id,
            task_file,
            reservation_token,
            protocol,
            owner_thread_id,
        )
        common_inspected = True
    except (OSError, JobRegistryError, TypeError, ValueError) as inspection_error:
        inspection_errors["job_registry"] = str(inspection_error)

    ledger: HandoffLedger | None = None
    owner_entry: dict[str, Any] | None = None
    owner_inspected = False
    try:
        ledger = ledger_for_task(task_payload)
        owner_entry = ledger.find_exact_registration(
            task_id_value,
            event_id,
            task_file,
            reservation_token,
            job_key,
        )
        owner_inspected = True
    except (OSError, LedgerError, TypeError, ValueError) as inspection_error:
        inspection_errors["owner_ledger"] = str(inspection_error)

    if common_entry is not None:
        task_payload["job_reservation_generation"] = int(common_entry["generation"])
        task_payload["job_registry_file"] = str(job_registry.json_path)
        task_payload["job_registry_lock"] = str(job_registry.lock_path)
    if owner_entry is not None:
        assert ledger is not None
        task_payload["lock_generation"] = int(owner_entry["generation"])
        task_payload["authority_epoch"] = int(owner_entry["authority_epoch"])
        task_payload["owner_ledger_file"] = str(ledger.json_path)
        task_payload["owner_ledger_lock"] = str(ledger.lock_path)

    task_payload["registration_inspection"] = {
        "common_exact": common_entry is not None,
        "common_state": common_entry.get("state") if common_entry else None,
        "owner_exact": owner_entry is not None,
        "owner_state": owner_entry.get("state") if owner_entry else None,
        "errors": inspection_errors,
    }

    if owner_entry is not None:
        task_payload["phase"] = "registration_recovery_required"
        task_payload["registration_recovery_reason"] = (
            "exact owner-ledger registration exists; common reservation retained"
        )
        task_payload["registration_compensation"] = "retained"
    elif not owner_inspected or not common_inspected:
        task_payload["phase"] = "registration_recovery_required"
        task_payload["registration_recovery_reason"] = (
            "reservation presence could not be proven; no compensation attempted"
        )
        task_payload["registration_compensation"] = "retained_unverified"
    elif common_entry is None:
        task_payload["phase"] = "registration_blocked"
        task_payload["registration_compensation"] = "nothing_to_release"
    elif common_entry.get("state") == "ACTIVE":
        try:
            job_registry.finish(
                task_id_value,
                reservation_token,
                int(common_entry["generation"]),
                "cancelled",
            )
        except (OSError, JobRegistryError, TypeError, ValueError) as compensation_error:
            task_payload["phase"] = "registration_recovery_required"
            task_payload["registration_recovery_reason"] = (
                "common reservation compensation outcome is uncertain"
            )
            task_payload["registration_compensation"] = "uncertain"
            task_payload["registration_compensation_error"] = str(compensation_error)
        else:
            task_payload["phase"] = "registration_blocked"
            task_payload["registration_compensation"] = (
                "common_cancelled_after_proven_owner_absent"
            )
    elif common_entry.get("state") in {"CANCELLED", "BLOCKED"}:
        # A previous compensation may have committed before its task-file
        # mirror. Exact terminal rejection plus proven owner absence is already
        # the desired safe endpoint; recovery must not recreate the ledger.
        task_payload["phase"] = "registration_blocked"
        task_payload["registration_compensation"] = "already_rejected_owner_absent"
    else:
        # Never rewrite UNKNOWN or ACCEPTED. Either is a durable deduplication
        # fence even though no matching owner entry is currently readable.
        task_payload["phase"] = "registration_recovery_required"
        task_payload["registration_recovery_reason"] = (
            "exact common reservation is UNKNOWN/ACCEPTED; terminality was preserved"
        )
        task_payload["registration_compensation"] = "retained_non_active"

    write_json(task_file, task_payload)
    return str(task_payload["phase"])


def task_reservation_identity(task_payload: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(task_payload["task_id"]),
        str(task_payload["reservation_token"]),
        int(task_payload["lock_generation"]),
    )


def command_schedule(args: argparse.Namespace) -> int:
    if not args.blocking:
        fatal(
            "Refusing to schedule a handoff without --blocking. "
            "Use this only for genuinely blocking waits."
        )
    if args.expected_seconds < 300 and not args.allow_short_test:
        fatal(
            "Refusing to schedule a handoff for a task under 5 minutes. "
            "Use sleep directly for short waits, or pass --allow-short-test only for testing."
        )

    explicit_owner_thread_id = requested_owner_thread_id(args)
    actor_thread_id, actor_inferred_from_owner = current_actor_thread_id(
        explicit_owner_thread_id
    )
    app_server_context = app_server_context_from_args(args)
    endpoint = str(app_server_context["endpoint"])
    auth_token_env = app_server_auth_env_from_args(args)
    try:
        owner_route = resolve_owner_route(
            endpoint,
            actor_thread_id,
            explicit_owner_thread_id,
            auth_token_env,
        )
    except OwnerRoutingError as error:
        fatal(f"Unsafe resume owner routing: {error}")
    owner_route["actor_identity_verified"] = not actor_inferred_from_owner
    if actor_inferred_from_owner:
        owner_route["route_verified"] = False
        owner_route["route_verification_error"] = (
            "CODEX_THREAD_ID was absent; actor identity was inferred from the requested owner"
        )
    if not owner_route.get("route_verified"):
        fatal(
            "Codex could not verify the complete actor-to-owner route. Refusing to guess whether "
            "this caller is a top-level task, fork, subagent, or side conversation. Re-run from "
            "the owning app-server context; unverified routes cannot preserve the cross-fork "
            "single-job fence even with marker delivery."
        )

    owner_thread_id = str(owner_route["owner_thread_id"])
    job_scope_id = str(
        owner_route.get("job_scope_id")
        or owner_route.get("owner_session_id")
        or owner_route.get("actor_session_id")
        or owner_thread_id
    )
    target = build_target(args)
    logical_job_id = str(args.job_id or "process-lifetime").strip()
    if not logical_job_id or len(logical_job_id) > 256:
        fatal("--job-id must contain 1 to 256 non-whitespace characters.")
    try:
        bind_target_identities(target)
    except ProcessNotFound as error:
        emit(
            {
                "status": "finished_before_identity_binding",
                "detail": str(error),
                "target": target_summary(target),
            },
            args.json,
        )
        return 3
    except ProcessIdentityError as error:
        fatal(f"Could not bind the watched process incarnation safely: {error}")
    preflight_seconds = max(int(args.preflight_seconds), 0)
    state_dirs = ensure_state_dirs(Path(args.state_dir).expanduser().resolve())
    current_cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    observed_log = build_observed_log(args, target, current_cwd)
    max_wait_seconds = int(args.max_wait_seconds)
    if max_wait_seconds <= 0:
        fatal("--max-wait-seconds must be a positive integer.")
    watch_budget_started_unix = time.time()

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
    event_id = str(uuid.uuid4())
    reservation_token = str(uuid.uuid4())
    client_message_id = f"{CLIENT_MESSAGE_PREFIX}{event_id}"
    task_file = state_dirs["tasks"] / f"{task_id_value}.json"
    prompt_file = state_dirs["prompts"] / f"{task_id_value}.prompt.txt"
    log_file = state_dirs["logs"] / f"{task_id_value}.watch.log"
    prompt_text = load_prompt_text(args)
    requested_protocol = str(args.resume_protocol)

    protocol_probe: dict[str, Any] | None = None
    protocol_fallback_reason: str | None = None
    authority: dict[str, Any] | None = None
    authority_assessment: dict[str, Any] | None = None
    allow_weak_authority = bool(getattr(args, "allow_weak_authority", False))
    probe_reason: str | None = None
    if (
        requested_protocol in {"auto", "native-message"}
        and app_server_context.get("attachable") is True
    ):
        protocol_probe = inspect_native_thread(
            endpoint,
            owner_thread_id,
            bearer_token_env=auth_token_env,
        )
        probe_reason = str(
            protocol_probe.get("error")
            or protocol_probe.get("thread_status")
            or "the exact owner thread is not loaded in this attachable app-server"
        )
        if protocol_probe.get("native_message_ready") and isinstance(
            protocol_probe.get("authority"), dict
        ):
            authority = dict(protocol_probe["authority"])
            authority_assessment = assess_authority_strength(
                app_server_context,
                authority,
            )
            authority.update(authority_assessment)

    delivery_decision = classify_delivery_decision(
        requested_protocol,
        route_verified=bool(owner_route.get("route_verified")),
        attachable=app_server_context.get("attachable") is True,
        native_message_ready=bool(
            protocol_probe
            and protocol_probe.get("native_message_ready")
            and isinstance(protocol_probe.get("authority"), dict)
        ),
        authority_strength=(
            str(authority_assessment.get("authority_strength"))
            if authority_assessment
            else None
        ),
        allow_weak_authority=allow_weak_authority,
        context_reason=str(
            app_server_context.get("reason")
            or "the verified owner authority is not externally attachable"
        ),
        probe_reason=probe_reason,
        authority_strength_reason=(
            str(authority_assessment.get("authority_strength_reason"))
            if authority_assessment
            else None
        ),
    )
    if delivery_decision["action"] == "reject":
        suffix = (
            " Use marker delivery, or pass --allow-weak-authority only after accepting "
            "endpoint reuse/restart risk."
            if delivery_decision["branch"] == "native-rejected-weak-authority"
            else ""
        )
        fatal(
            "Native message delivery is unavailable: "
            f"{delivery_decision['reason']}.{suffix}"
        )
    selected_protocol = delivery_decision["action"]
    if selected_protocol == "native-message":
        if authority is None:
            fatal("Native message delivery selected without an authority descriptor.")
        authority["weak_authority_accepted"] = bool(
            authority["authority_strength"] == "weak" and allow_weak_authority
        )
        if auth_token_env:
            authority["credential_ref"] = {
                "kind": "environment",
                "name": auth_token_env,
            }
    else:
        if requested_protocol == "auto":
            protocol_fallback_reason = delivery_decision["reason"]
        authority = None

    owner_ledger_key = (
        owner_thread_id
        if selected_protocol == "native-message"
        else f"{MARKER_LEDGER_PREFIX}{owner_thread_id}"
    )

    task_payload: dict[str, Any] = {
        "task_id": task_id_value,
        "task_file": str(task_file),
        "phase": "reserving",
        "created_at": now_utc(),
        "session_id": owner_thread_id,
        "owner_thread_id": owner_thread_id,
        "job_scope_id": job_scope_id,
        "actor_thread_id": actor_thread_id,
        "actor_inferred_from_owner": actor_inferred_from_owner,
        "allow_weak_authority": allow_weak_authority,
        "owner_route": owner_route,
        "event_id": event_id,
        "client_user_message_id": client_message_id,
        "job_key": job_key_for_target(job_scope_id, target, logical_job_id),
        "logical_job_id": logical_job_id,
        "protocol_version": PROTOCOL_VERSION,
        "resume_protocol_requested": requested_protocol,
        "resume_protocol": selected_protocol,
        "delivery_branch": delivery_decision["branch"],
        "delivery_decision_reason": delivery_decision["reason"],
        "authority": authority,
        "authority_assessment": authority_assessment,
        "authority_epoch": 1 if authority else None,
        "app_server_endpoint": endpoint,
        "app_server_context": app_server_context,
        "app_server_auth_token_env": auth_token_env,
        "protocol_probe": protocol_probe,
        "protocol_fallback_reason": protocol_fallback_reason,
        "native_at_most_once": selected_protocol == "native-message",
        "strict_exactly_once": False,
        "strict_exactly_once_gap": (
            "turn/start has no server-side event-id deduplication or authority-epoch precondition"
        ),
        "will_wake_idle_thread": selected_protocol == "native-message",
        "reservation_token": reservation_token,
        "owner_ledger_dir": DEFAULT_COORDINATION_DIR,
        "owner_ledger_key": owner_ledger_key,
        "job_registry_dir": DEFAULT_COORDINATION_DIR,
        "target": target,
        "observed_log": observed_log,
        "expected_seconds": int(args.expected_seconds),
        "max_wait_seconds": max_wait_seconds,
        "watch_budget_started_unix": watch_budget_started_unix,
        "watch_deadline_unix": watch_budget_started_unix + max_wait_seconds,
        "allow_short_test": bool(args.allow_short_test),
        "poll_seconds": int(args.poll_seconds),
        "preflight_seconds": preflight_seconds,
        "resume_cwd": str(current_cwd),
        "prompt_file": str(prompt_file),
        "log_file": str(log_file),
        "dry_run_resume": bool(args.dry_run_resume),
        "resume_retry_delay_seconds": max(int(args.resume_retry_delay_seconds), 1),
        "resume_retry_max_attempts": max(int(args.resume_retry_max_attempts), 0),
        "state_collision_max_attempts": max(
            int(args.state_collision_max_attempts),
            0,
        ),
        "continuation_prompt_text": prompt_text or "",
        "note": args.note or "",
    }
    write_json(task_file, task_payload)

    with exclusive_watcher_guard(task_file):
        try:
            task_payload, ledger, registered_entry = ensure_task_reservations(
                task_file,
                task_payload,
            )
        except (
            JobConflict,
            JobRegistryError,
            AuthorityMismatch,
            LedgerConflict,
            LedgerError,
        ) as error:
            failure_phase = reconcile_registration_failure(
                task_file,
                task_payload,
                error,
            )
            recovery_hint = (
                f" Task {task_id_value} retained its reservations in "
                "phase=registration_recovery_required; run recover for that exact task."
                if failure_phase == "registration_recovery_required"
                else ""
            )
            fatal(
                f"Could not reserve one completion event for this job: {error}."
                f"{recovery_hint}"
            )
        task_payload["phase"] = "scheduled"
        write_json(task_file, task_payload)
    try:
        watcher, startup_ack = spawn_watcher_with_ack(task_file, log_file)
    except BaseException as error:
        with exclusive_watcher_guard(task_file):
            task_payload = load_json(task_file)
            if str(task_payload.get("phase") or "") in WATCH_START_PHASES:
                ledger.cancel(
                    task_id_value,
                    reservation_token,
                    int(task_payload["lock_generation"]),
                )
                finish_job_reservation(task_payload, "cancelled")
                task_payload["phase"] = "schedule_failed"
            else:
                task_payload["startup_failure_after_event_advanced"] = True
            task_payload["schedule_error"] = str(error)
            write_json(task_file, task_payload)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        fatal(f"Detached watcher startup failed: {error}")

    emit(
        {
            "status": "scheduled",
            "task_id": task_id_value,
            "task_file": str(task_file),
            "watcher_pid": watcher.pid,
            "watch_log": str(log_file),
            "target": target_summary(target),
            "max_wait_seconds": max_wait_seconds,
            "resume_protocol": selected_protocol,
            "delivery_branch": delivery_decision["branch"],
            "delivery_decision_reason": delivery_decision["reason"],
            "native_at_most_once": selected_protocol == "native-message",
            "strict_exactly_once": False,
            "will_wake_idle_thread": selected_protocol == "native-message",
            "protocol_fallback_reason": protocol_fallback_reason,
            "owner_thread_id": owner_thread_id,
            "actor_thread_id": actor_thread_id,
            "owner_route": owner_route.get("route"),
            "event_id": event_id,
            "client_user_message_id": client_message_id,
            "authority": authority,
            "authority_strength": (
                authority.get("authority_strength") if authority else None
            ),
            "authority_strength_reason": (
                authority.get("authority_strength_reason")
                if authority
                else (
                    authority_assessment.get("authority_strength_reason")
                    if authority_assessment
                    else None
                )
            ),
            "app_server_context": app_server_context,
            "fifo_generation": task_payload["lock_generation"],
            "watcher_startup_ack": startup_ack,
        },
        args.json,
    )
    return 0


def value_contains_client_id(value: Any, client_message_id: str) -> bool:
    if isinstance(value, dict):
        item_type = str(value.get("type") or "").replace("_", "").lower()
        if item_type == "usermessage" and value.get("clientId") == client_message_id:
            return True
        return any(value_contains_client_id(child, client_message_id) for child in value.values())
    if isinstance(value, list):
        return any(value_contains_client_id(child, client_message_id) for child in value)
    return False


def thread_history_mode(thread: dict[str, Any]) -> str:
    """Return the supported history mode, defaulting old servers to legacy."""

    value = thread.get("historyMode")
    if value is None:
        return LEGACY_HISTORY_MODE
    mode = str(value)
    if mode not in {LEGACY_HISTORY_MODE, PAGINATED_HISTORY_MODE}:
        raise AppServerError(f"unsupported owner thread historyMode {mode!r}")
    return mode


def read_thread_history(
    client: AppServerClient,
    owner_thread_id: str,
    summary: dict[str, Any],
    *,
    items_view: str,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Attach turns to a prior metadata-only read without resuming the thread."""

    history_mode = thread_history_mode(summary)
    if history_mode == PAGINATED_HISTORY_MODE:
        thread = dict(summary)
        thread["turns"] = client.list_thread_turns(
            owner_thread_id,
            items_view=items_view,
            max_pages=max_pages,
        )
        return thread

    thread = client.read_thread(owner_thread_id, include_turns=True)
    if thread_history_mode(thread) != LEGACY_HISTORY_MODE:
        raise AppServerError(
            "owner thread historyMode changed while reading legacy history"
        )
    return thread


def wait_for_persisted_user_message(
    client: AppServerClient,
    owner_thread_id: str,
    client_message_id: str,
    timeout_seconds: float,
    history_mode: str,
) -> None:
    """Confirm acceptance without attaching/cold-loading the owner thread.

    A fresh app-server connection is not subscribed by thread/read or
    turn/start, so it cannot rely on item notifications. Polling the exact
    loaded authority's thread history gives positive acknowledgement while
    leaving native streaming to the owner's already-subscribed clients.
    """

    if history_mode not in {LEGACY_HISTORY_MODE, PAGINATED_HISTORY_MODE}:
        raise AppServerError(f"unsupported owner thread historyMode {history_mode!r}")
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    while True:
        if history_mode == PAGINATED_HISTORY_MODE:
            thread: dict[str, Any] = {
                "turns": client.list_thread_turns(
                    owner_thread_id,
                    items_view="full",
                    max_pages=1,
                )
            }
        else:
            thread = client.read_thread(owner_thread_id, include_turns=True)
        if value_contains_client_id(thread, client_message_id):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError(
                "timed out waiting for userMessage.clientId in owner thread history"
            )
        time.sleep(min(0.25, remaining))


def active_in_progress_turn_id(thread: dict[str, Any]) -> str | None:
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else None
    if status_type != "active":
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise AppServerError("active owner history returned no turns array")
    active_ids = [
        str(turn["id"])
        for turn in turns
        if isinstance(turn, dict)
        and turn.get("id")
        and turn.get("status") == "inProgress"
    ]
    if len(active_ids) != 1:
        raise AppServerError(
            "active owner history did not expose exactly one in-progress turn"
        )
    return active_ids[0]


def classify_rpc_rejection(error: AppServerRpcError) -> str:
    """Classify definitive JSON-RPC rejections without guessing acceptance.

    Codex currently supplies structured data for input-size and non-steerable
    turn errors, but not for NoActiveTurn/ExpectedTurnMismatch.  Those two
    official messages are transient read/submit collisions and are safe to
    re-probe immediately because the RPC response proves non-acceptance.
    """

    payload = error.error if isinstance(error.error, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if data.get("input_error_code") == "input_too_large":
        return "permanent"
    codex_error_info = (
        data.get("codexErrorInfo")
        if isinstance(data.get("codexErrorInfo"), dict)
        else {}
    )
    if isinstance(codex_error_info.get("activeTurnNotSteerable"), dict):
        return "state_collision"
    message = str(payload.get("message") or error).lower()
    if error.method in {"turn/steer", "turn/start"} and (
        "no active turn to steer" in message
        or "expected active turn id" in message
        or "cannot steer a review turn" in message
        or "cannot steer a compact turn" in message
    ):
        return "state_collision"
    if "input must not be empty" in message or "input exceeds the maximum length" in message:
        return "permanent"
    return "retryable"


def update_delivery_task(
    task_file: Path,
    *,
    phase: str,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    payload = load_json(task_file)
    payload["phase"] = phase
    payload["delivery_status"] = status
    payload.update(fields)
    write_json(task_file, payload)
    return payload


def dispatch_native_message(
    task_file: Path,
    task_payload: dict[str, Any],
    prompt_text: str,
) -> int:
    """Submit one FIFO event once and wait for its user-message acknowledgement."""

    owner_thread_id = str(task_payload["owner_thread_id"])
    task_id_value, token, generation = task_reservation_identity(task_payload)
    client_message_id = str(task_payload["client_user_message_id"])
    ledger = ledger_for_task(task_payload)
    retry_delay_seconds = max(
        int(task_payload.get("resume_retry_delay_seconds", DEFAULT_RESUME_RETRY_DELAY_SECONDS)),
        1,
    )
    retry_max_attempts = max(
        int(task_payload.get("resume_retry_max_attempts", DEFAULT_RESUME_RETRY_MAX_ATTEMPTS)),
        0,
    )
    collision_max_attempts = max(
        int(
            task_payload.get(
                "state_collision_max_attempts",
                DEFAULT_STATE_COLLISION_MAX_ATTEMPTS,
            )
        ),
        0,
    )
    acknowledgement_timeout = max(
        int(task_payload.get("delivery_ack_timeout_seconds") or 30),
        1,
    )
    attempts = list(task_payload.get("delivery_attempts") or [])

    persisted_entry = ledger.validate(task_id_value, token, generation)
    attempt = max(
        (
            int(row.get("attempt") or 0)
            for row in attempts
            if isinstance(row, dict)
        ),
        default=0,
    )
    retry_budget_used = sum(
        1
        for row in persisted_entry.get("submission_deferrals", [])
        if isinstance(row, dict) and row.get("classification") != "state_collision"
    )
    collision_budget_used = sum(
        1
        for row in persisted_entry.get("submission_deferrals", [])
        if isinstance(row, dict) and row.get("classification") == "state_collision"
    )
    sync_job_reservation_from_ledger(task_payload, persisted_entry)
    if persisted_entry["state"] == "ACCEPTED":
        return 0
    if persisted_entry["state"] == "BLOCKED":
        return 4
    if persisted_entry["state"] == "CANCELLED":
        return 4

    while True:
        # A later watcher normally waits behind the earlier watcher's task
        # flock. If that process crashed after publishing READY, the later
        # dispatcher may adopt the exact earlier token/generation under its
        # now-free task flock, so a ready queue does not need a supervisor.
        snapshot = ledger.snapshot() or {}
        unresolved = sorted(
            (
                row
                for row in snapshot.get("entries", [])
                if row.get("state") in {"READY", "SUBMITTING", "UNKNOWN"}
            ),
            key=lambda row: int(row.get("ready_sequence") or 0),
        )
        if unresolved and unresolved[0].get("task_id") != task_id_value:
            earlier = unresolved[0]
            if earlier.get("state") == "UNKNOWN":
                update_delivery_task(
                    task_file,
                    phase="native_message_queued",
                    status="blocked_by_earlier_unknown_event",
                    delivery_last_error=f"earlier task {earlier.get('task_id')} is UNKNOWN",
                )
                return 4
            earlier_file = Path(str(earlier.get("task_file") or ""))
            if earlier.get("state") == "READY" and earlier_file.is_file():
                with try_watcher_guard(earlier_file) as adopted:
                    if adopted:
                        earlier_payload = load_json(earlier_file)
                        try:
                            ledger.validate(
                                str(earlier["task_id"]),
                                str(earlier["token"]),
                                int(earlier["generation"]),
                            )
                        except LedgerError:
                            continue
                        earlier_prompt = read_text(Path(earlier_payload["prompt_file"]))
                        dispatch_native_message(
                            earlier_file,
                            earlier_payload,
                            earlier_prompt,
                        )
                        continue
            if earlier.get("state") == "SUBMITTING" and earlier_file.is_file():
                with try_watcher_guard(earlier_file) as adopted:
                    if adopted:
                        earlier_payload = load_json(earlier_file)
                        ledger.fence_interrupted_submission(
                            str(earlier["task_id"]),
                            str(earlier["token"]),
                            int(earlier["generation"]),
                            "a later FIFO dispatcher found the submitting watcher absent",
                        )
                        sync_job_reservation_from_ledger(earlier_payload)
                        update_delivery_task(
                            earlier_file,
                            phase="native_message_unknown",
                            status="interrupted_submission_fenced_unknown",
                        )
                        continue
            time.sleep(1)
            continue

        try:
            entry = ledger.begin_next_submission(task_id_value)
        except SubmissionBlocked as error:
            snapshot = ledger.snapshot() or {}
            earlier = sorted(
                (
                    row
                    for row in snapshot.get("entries", [])
                    if row.get("state") in {"READY", "SUBMITTING", "UNKNOWN"}
                ),
                key=lambda row: int(row.get("ready_sequence") or 0),
            )
            if earlier and earlier[0].get("state") == "UNKNOWN":
                update_delivery_task(
                    task_file,
                    phase="native_message_queued",
                    status="blocked_by_earlier_unknown_event",
                    delivery_last_error=str(error),
                )
                return 4
            update_delivery_task(
                task_file,
                phase="native_message_queued",
                status="waiting_for_earlier_fifo_event",
                delivery_last_error=str(error),
            )
            time.sleep(1)
            continue
        if entry is None:
            return 0

        attempt += 1
        started_at = now_utc()
        expected_authority = dict(entry["authority"])
        endpoint = str(expected_authority["endpoint"])
        credential_ref = expected_authority.get("credential_ref")
        bearer_token_env = (
            str(credential_ref.get("name"))
            if isinstance(credential_ref, dict) and credential_ref.get("kind") == "environment"
            else None
        )
        update_delivery_task(
            task_file,
            phase="native_message_submitting",
            status="submission_attempt_started",
            delivery_attempt=attempt,
            delivery_last_attempt_started_at=started_at,
        )
        print(
            f"[{now_utc()}] submitting event {task_payload['event_id']} to exact owner "
            f"{owner_thread_id} through {endpoint} attempt={attempt}",
            flush=True,
        )

        request_started = False
        submission_rpc_accepted = False
        turn_id: str | None = None
        submission_method: str | None = None
        try:
            strength = str(expected_authority.get("authority_strength") or "")
            if strength not in {"strong", "weak"}:
                raise AuthorityMismatch(
                    "native ticket has no recognized authority-strength classification"
                )
            if strength == "weak" and not bool(
                expected_authority.get("weak_authority_accepted")
            ):
                raise AuthorityMismatch(
                    "weak native authority ledger entry has no durable explicit opt-in"
                )
            probe_strong_authority_process(expected_authority)
            with AppServerClient(
                endpoint,
                bearer_token_env=bearer_token_env,
            ) as client:
                current_authority = client.authority_descriptor()
                if bearer_token_env:
                    current_authority["credential_ref"] = {
                        "kind": "environment",
                        "name": bearer_token_env,
                    }
                mismatch = authority_descriptor_mismatch(expected_authority, current_authority)
                if mismatch:
                    raise AuthorityMismatch(mismatch)

                loaded_ids = client.loaded_thread_ids()
                thread_summary = client.read_thread(
                    owner_thread_id,
                    include_turns=False,
                )
                history_mode = thread_history_mode(thread_summary)
                status = thread_summary.get("status")
                status_type = status.get("type") if isinstance(status, dict) else None
                if (
                    owner_thread_id not in loaded_ids
                    and status_type not in LOADED_THREAD_STATUS_TYPES
                ):
                    raise AuthorityMismatch(
                        "owner thread has no positive loaded evidence on the ticketed authority; "
                        "refusing turn/start after a possible host handoff"
                    )
                if thread_summary.get("canAcceptDirectInput") is False:
                    raise AppServerError(
                        "owner thread reports canAcceptDirectInput=false"
                    )
                thread = thread_summary
                if status_type == "active":
                    thread = read_thread_history(
                        client,
                        owner_thread_id,
                        thread_summary,
                        items_view="notLoaded",
                        max_pages=1,
                    )
                active_turn_id = active_in_progress_turn_id(thread)
                # Revalidate immediately before the first continuation request.
                # A dead/reused owner process is still a definitive pre-request
                # failure and may return to READY; no network ambiguity exists.
                probe_strong_authority_process(expected_authority)
                # READY -> SUBMITTING was committed before this first request
                # byte. Any transport ambiguity from here is permanently UNKNOWN.
                request_started = True
                if active_turn_id is not None:
                    submission_method = "turn/steer"
                    turn_id = client.turn_steer(
                        owner_thread_id,
                        active_turn_id,
                        prompt_text,
                        client_message_id,
                    )
                else:
                    submission_method = "turn/start"
                    turn_id = client.turn_start(
                        owner_thread_id,
                        prompt_text,
                        client_message_id,
                    )
                submission_rpc_accepted = True
                update_delivery_task(
                    task_file,
                    phase="native_message_submitted",
                    status=(
                        "steer_rpc_accepted"
                        if submission_method == "turn/steer"
                        else "start_rpc_accepted_waiting_for_history"
                    ),
                    delivery_turn_id=turn_id,
                    delivery_submission_method=submission_method,
                    delivery_rpc_accepted_at=now_utc(),
                )
                if submission_method == "turn/start":
                    wait_for_persisted_user_message(
                        client,
                        owner_thread_id,
                        client_message_id,
                        acknowledgement_timeout,
                        history_mode,
                    )
                completed_at = now_utc()
                ledger.finish_submission(
                    task_id_value,
                    token,
                    generation,
                    "accepted",
                    detail=(
                        "turn/steer synchronously accepted input for the exact active turn"
                        if submission_method == "turn/steer"
                        else "matching userMessage.clientId observed in owner thread history"
                    ),
                )
                finish_job_reservation(task_payload, "accepted")
                attempts.append(
                    {
                        "attempt": attempt,
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "status": (
                            "active_turn_steer_confirmed"
                            if submission_method == "turn/steer"
                            else "user_message_confirmed_in_history"
                        ),
                        "turn_id": turn_id,
                        "submission_method": submission_method,
                    }
                )
                update_delivery_task(
                    task_file,
                    phase="native_message_accepted",
                    status=(
                        "active_turn_steer_confirmed"
                        if submission_method == "turn/steer"
                        else "matching_user_message_confirmed_in_history"
                    ),
                    delivery_protocol="native-message",
                    delivery_accepted_at=completed_at,
                    delivery_attempts=attempts,
                )
                print(
                    f"[{now_utc()}] native user message accepted via {submission_method} "
                    f"clientUserMessageId={client_message_id}",
                    flush=True,
                )
                return 0
        except AppServerRpcError as error:
            if submission_rpc_accepted:
                # turn/start already returned success; a later history-read
                # RPC failure says nothing about whether the accepted message
                # was persisted.  Requeueing here would duplicate it.
                detail = str(error)
                ledger.finish_submission(
                    task_id_value,
                    token,
                    generation,
                    "unknown",
                    detail=detail,
                )
                finish_job_reservation(task_payload, "unknown")
                update_delivery_task(
                    task_file,
                    phase="native_message_unknown",
                    status="post_acceptance_ack_outcome_unknown_no_retry",
                    delivery_last_error=detail,
                    delivery_turn_id=turn_id,
                )
                return 4
            # A JSON-RPC error is an explicit negative response: no core
            # submission was accepted, so this event may safely retain its
            # original FIFO position for a bounded retry.
            detail = str(error)
            rejection_kind = classify_rpc_rejection(error)
            ledger.defer_submission(
                task_id_value,
                token,
                generation,
                detail,
                classification=rejection_kind,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "started_at": started_at,
                    "completed_at": now_utc(),
                    "status": "rpc_rejected",
                    "rejection_kind": rejection_kind,
                    "error": detail,
                }
            )
            task_payload = update_delivery_task(
                task_file,
                phase="native_message_deferred",
                status="rpc_explicitly_rejected",
                delivery_attempts=attempts,
                delivery_last_error=detail,
            )
            if rejection_kind == "state_collision":
                # The owner moved between thread/read and submission, or is in
                # a temporary Review/Compact turn.  The negative response is
                # safe to retry, but a 20-minute authority backoff would make
                # sequential steer unusable.  Use a separate durable budget so
                # a permanently non-steerable owner cannot hold this FIFO and
                # watcher forever.
                collision_budget_used += 1
                collisions_remaining = (
                    collision_max_attempts == 0
                    or collision_budget_used < collision_max_attempts
                )
                if collisions_remaining:
                    time.sleep(STATE_COLLISION_RETRY_SECONDS)
                    continue
                ledger.block_next_ready(
                    task_id_value,
                    token,
                    generation,
                    "state-collision retry limit reached without submission",
                )
                finish_job_reservation(task_payload, "blocked")
                update_delivery_task(
                    task_file,
                    phase="native_message_blocked",
                    status="state_collision_retries_exhausted",
                    delivery_attempts=attempts,
                )
                return 4
            if rejection_kind == "permanent":
                ledger.block_next_ready(
                    task_id_value,
                    token,
                    generation,
                    "permanent app-server input rejection",
                )
                finish_job_reservation(task_payload, "blocked")
                update_delivery_task(
                    task_file,
                    phase="native_message_blocked",
                    status="permanent_rpc_rejection",
                    delivery_attempts=attempts,
                )
                return 4
            retry_budget_used += 1
            attempts_remaining = (
                retry_max_attempts == 0 or retry_budget_used < retry_max_attempts
            )
            if attempts_remaining:
                time.sleep(retry_delay_seconds)
                continue
            # The last explicit rejection is still proof of non-acceptance.
            ledger.block_next_ready(
                task_id_value,
                token,
                generation,
                "explicit RPC rejection retry limit reached",
            )
            finish_job_reservation(task_payload, "blocked")
            update_delivery_task(
                task_file,
                phase="native_message_blocked",
                status="explicit_rejection_retries_exhausted",
                delivery_attempts=attempts,
            )
            return 4
        except (AuthorityMismatch, AppServerError, OSError, socket.timeout) as error:
            detail = str(error)
            if request_started:
                ledger.finish_submission(
                    task_id_value,
                    token,
                    generation,
                    "unknown",
                    detail=detail,
                )
                finish_job_reservation(task_payload, "unknown")
                update_delivery_task(
                    task_file,
                    phase="native_message_unknown",
                    status="submission_outcome_unknown_no_retry",
                    delivery_last_error=detail,
                    delivery_turn_id=turn_id,
                )
                print(
                    f"[{now_utc()}] submission outcome is UNKNOWN; refusing automatic retry: "
                    f"{detail}",
                    flush=True,
                )
                return 4

            # Authority/connectivity checks happened after the durable claim
            # but before turn/start. No continuation bytes were sent, so a
            # definitive local defer is safe.
            ledger.defer_submission(
                task_id_value,
                token,
                generation,
                detail,
                classification="authority_unavailable",
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "started_at": started_at,
                    "completed_at": now_utc(),
                    "status": "pre_submission_blocked",
                    "error": detail,
                }
            )
            update_delivery_task(
                task_file,
                phase="native_message_deferred",
                status="authority_unavailable_before_submission",
                delivery_attempts=attempts,
                delivery_last_error=detail,
            )
            retry_budget_used += 1
            attempts_remaining = (
                retry_max_attempts == 0 or retry_budget_used < retry_max_attempts
            )
            if attempts_remaining:
                time.sleep(retry_delay_seconds)
                continue
            ledger.block_next_ready(
                task_id_value,
                token,
                generation,
                "pre-submission authority retry limit reached",
            )
            finish_job_reservation(task_payload, "blocked")
            update_delivery_task(
                task_file,
                phase="native_message_blocked",
                status="authority_unavailable_retries_exhausted",
                delivery_attempts=attempts,
            )
            return 4


def command_watch(args: argparse.Namespace) -> int:
    task_file = Path(args.task_file).expanduser().resolve()
    startup_descriptor = getattr(args, "startup_fd", None)
    try:
        with exclusive_watcher_guard(task_file):
            task_payload = load_json(task_file)
            phase = str(task_payload.get("phase") or "")
            if phase not in WATCH_START_PHASES:
                fatal(
                    "Refusing to replay a watcher from a non-restartable phase.\n"
                    f"task_id: {task_payload.get('task_id') or task_file.stem}\n"
                    f"phase: {phase or 'missing'}"
                )
            assert_current_owner_reservation(task_file, task_payload)
            try:
                watcher_identity = capture_local_identity(os.getpid())
                if watcher_identity.source not in {
                    "linux-proc-starttime",
                    "macos-proc-starttime",
                }:
                    raise ProcessIdentityError(
                        "this host does not expose a strong watcher process start token"
                    )
            except ProcessIdentityError as error:
                fatal(
                    "Could not bind the detached watcher to its exact process incarnation: "
                    f"{error}"
                )
            if task_uses_ledger(task_payload):
                task_id_value, token, generation = task_reservation_identity(task_payload)
                try:
                    ledger_for_task(task_payload).mark_watching(
                        task_id_value,
                        token,
                        generation,
                    )
                except LedgerError as error:
                    fatal(f"Could not enter WATCHING in the owner FIFO ledger: {error}")
            task_payload["phase"] = "watching"
            task_payload["watcher_pid"] = os.getpid()
            task_payload["watcher_identity"] = watcher_identity.to_dict()
            task_payload["watch_started_at"] = (
                task_payload.get("watch_started_at") or now_utc()
            )
            task_payload["watch_loop_started_at"] = now_utc()
            write_json(task_file, task_payload)
            if startup_descriptor is not None:
                write_watcher_startup_ack(int(startup_descriptor), task_payload)
                startup_descriptor = None
            return command_watch_owned(task_file)
    finally:
        if startup_descriptor is not None:
            try:
                os.close(int(startup_descriptor))
            except OSError:
                pass


def command_watch_owned(task_file: Path) -> int:
    """Observe one target, publish one event, then invoke the v3 delivery path."""

    task_payload = load_json(task_file)
    target = task_payload["target"]
    poll_seconds = max(int(task_payload.get("poll_seconds", DEFAULT_POLL_SECONDS)), 1)
    max_wait_seconds = max(
        int(task_payload.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS)),
        1,
    )
    watch_budget_started_unix = float(
        task_payload.get("watch_budget_started_unix") or time.time()
    )
    watch_deadline_unix = float(
        task_payload.get("watch_deadline_unix")
        or (watch_budget_started_unix + max_wait_seconds)
    )
    task_payload["watch_budget_started_unix"] = watch_budget_started_unix
    task_payload["watch_deadline_unix"] = watch_deadline_unix
    write_json(task_file, task_payload)
    completion_reason = "process_exited"
    completion_detail = ""
    last_probe_state = "unknown"
    last_probe_detail = ""

    print(
        f"[{now_utc()}] task {task_payload['task_id']} watching {target_summary(target)}",
        flush=True,
    )

    kernel_wait_result: tuple[str, str, str] | None = None
    remaining_wait_seconds = max(int(watch_deadline_unix - time.time()), 0)
    if (
        remaining_wait_seconds > 0
        and target.get("scope") == "local"
        and target.get("mode") == "pid"
    ):
        identities = target_identities(target)
        if len(identities) != 1:
            fatal("A PID target must carry exactly one process incarnation.")
        kernel_wait_result = wait_local_pid_exit_event(
            identities[0],
            remaining_wait_seconds,
        )

    if kernel_wait_result is not None:
        completion_reason, completion_detail, wait_mechanism = kernel_wait_result
        last_probe_state = "dead" if completion_reason == "process_exited" else "alive"
        last_probe_detail = completion_detail
        task_payload = load_json(task_file)
        task_payload["wait_mechanism"] = wait_mechanism
        write_json(task_file, task_payload)
    else:
        task_payload = load_json(task_file)
        task_payload["wait_mechanism"] = "poll"
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
            remaining_wait_seconds = max(int(watch_deadline_unix - time.time()), 0)
            if remaining_wait_seconds <= 0:
                completion_reason = "max_wait_reached"
                completion_detail = f"last_probe_state={state}; last_probe_detail={detail}"
                break
            delay = min(poll_seconds, max(remaining_wait_seconds, 1))
            if state == "unknown":
                delay = min(delay, 5)
            time.sleep(delay)

    wait_elapsed_seconds = max(int(time.time() - watch_budget_started_unix), 0)
    task_payload = load_json(task_file)
    prompt_text = build_resume_prompt(
        task_id_value=str(task_payload["task_id"]),
        task_file=task_file,
        owner_thread_id=str(task_payload["owner_thread_id"]),
        event_id=str(task_payload["event_id"]),
        target=target,
        observed_log=task_payload.get("observed_log"),
        note=task_payload.get("note") or None,
        prompt_text=(task_payload.get("continuation_prompt_text") or "").strip() or None,
        completion_reason=completion_reason,
        wait_elapsed_seconds=wait_elapsed_seconds,
        max_wait_seconds=max_wait_seconds,
        completion_detail=completion_detail or None,
    )
    write_text(Path(task_payload["prompt_file"]), prompt_text)
    # The final prompt is durable before the completion event becomes visible.
    # Recovery can therefore never dispatch the schedule-time placeholder or a
    # partially written continuation.
    task_payload.update(
        {
            "phase": "event_staged",
            "completed_at": now_utc(),
            "event_ready_at": now_utc(),
            "completion_reason": completion_reason,
            "completion_detail": completion_detail,
            "wait_elapsed_seconds": wait_elapsed_seconds,
            "last_probe_state": last_probe_state,
            "last_probe_detail": last_probe_detail,
            "resume_prompt_sha256": resume_prompt_digest(prompt_text),
        }
    )
    write_json(task_file, task_payload)

    if task_payload.get("dry_run_resume"):
        if task_uses_ledger(task_payload):
            task_id_value, token, generation = task_reservation_identity(task_payload)
            ledger_for_task(task_payload).cancel(task_id_value, token, generation)
        finish_job_reservation(task_payload, "cancelled")
        task_payload["phase"] = "resume_dry_run_complete"
        task_payload["resume_completed_at"] = now_utc()
        write_json(task_file, task_payload)
        return 0

    resume_protocol = str(task_payload.get("resume_protocol") or "marker")
    if resume_protocol == "native-message":
        task_id_value, token, generation = task_reservation_identity(task_payload)
        try:
            ready_sequence = ledger_for_task(task_payload).mark_ready(
                task_id_value,
                token,
                generation,
            )
        except LedgerError as error:
            update_delivery_task(
                task_file,
                phase="native_message_blocked",
                status="could_not_publish_ready_event",
                delivery_last_error=str(error),
            )
            return 4
        task_payload = update_delivery_task(
            task_file,
            phase="native_message_ready",
            status="queued_for_owner_fifo",
            ready_sequence=ready_sequence,
        )
        return dispatch_native_message(task_file, task_payload, prompt_text)

    if resume_protocol == "marker":
        task_id_value, token, generation = task_reservation_identity(task_payload)
        try:
            ready_sequence = ledger_for_task(task_payload).mark_ready(
                task_id_value,
                token,
                generation,
            )
        except LedgerError as error:
            update_delivery_task(
                task_file,
                phase="marker_blocked",
                status="could_not_publish_marker_ready_event",
                delivery_last_error=str(error),
            )
            return 4
        task_payload = update_delivery_task(
            task_file,
            phase="marker_pending",
            status="pending_manual_claim",
            marker_pending_at=now_utc(),
            ready_sequence=ready_sequence,
        )
        print(
            f"[{now_utc()}] marker pending owner_thread_id="
            f"{task_payload['owner_thread_id']} event_id={task_payload['event_id']}",
            flush=True,
        )
        return 0

    update_delivery_task(
        task_file,
        phase="resume_failed",
        status="unsupported_stored_protocol",
        resume_error=f"unsupported stored resume protocol: {resume_protocol}",
    )
    return 4


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
        owner_tasks = [
            load_json(task_file)
            for task_file in sorted(tasks_dir.glob("*.json"))
            if str(load_json(task_file).get("owner_thread_id") or "") == args.session_id
        ]
        native_ledger = HandoffLedger(
            DEFAULT_COORDINATION_DIR, args.session_id
        ).snapshot()
        marker_ledger = HandoffLedger(
            DEFAULT_COORDINATION_DIR,
            f"{MARKER_LEDGER_PREFIX}{args.session_id}",
        ).snapshot()
        job_scope_ids = sorted(
            {
                str(task.get("job_scope_id") or task.get("owner_thread_id"))
                for task in owner_tasks
                if task.get("job_scope_id") or task.get("owner_thread_id")
            }
        )
        if not job_scope_ids:
            job_scope_ids = [args.session_id]
        job_registries = {
            scope_id: OwnerJobRegistry(
                DEFAULT_COORDINATION_DIR,
                scope_id,
            ).snapshot()
            for scope_id in job_scope_ids
        }
        emit(
            {
                "status": (
                    "ok"
                    if owner_tasks
                    or native_ledger
                    or marker_ledger
                    or any(job_registries.values())
                    else "no_tasks"
                ),
                "owner_thread_id": args.session_id,
                "tasks": owner_tasks,
                "native_ledger": native_ledger,
                "marker_ledger": marker_ledger,
                "job_scope_ids": job_scope_ids,
                "job_registries": job_registries,
            },
            args.json,
        )
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

    active_entries, stale_entries = active_and_stale_task_snapshots(tasks_dir)

    payload = {
        "active_tasks": active_entries,
        "active_count": len(active_entries),
    }
    if args.include_stale:
        payload["stale_tasks"] = stale_entries
        payload["stale_count"] = len(stale_entries)
    emit(payload, args.json)
    return 0


def pending_tasks_for_owner(tasks_dir: Path, owner_thread_id: str) -> list[dict[str, Any]]:
    if not tasks_dir.exists():
        return []
    pending: list[dict[str, Any]] = []
    for task_file in sorted(tasks_dir.glob("*.json")):
        try:
            payload = load_json(task_file)
        except (OSError, json.JSONDecodeError):
            continue
        owner = str(payload.get("owner_thread_id") or payload.get("session_id") or "")
        if owner != owner_thread_id:
            continue
        if str(payload.get("phase") or "") in PENDING_PHASES:
            pending.append(payload)
    return sorted(
        pending,
        key=lambda payload: (
            0 if payload.get("resume_protocol") == "marker" else 1,
            int(payload.get("ready_sequence") or 2**63 - 1),
            str(payload.get("event_ready_at") or payload.get("created_at") or ""),
        ),
    )


def command_pending(args: argparse.Namespace) -> int:
    owner_thread_id = sanitize_session_id(requested_owner_thread_id(args))
    state_dir = Path(args.state_dir).expanduser().resolve()
    pending = pending_tasks_for_owner(state_dir / "tasks", owner_thread_id)
    emit(
        {
            "status": "ok",
            "owner_thread_id": owner_thread_id,
            "pending_count": len(pending),
            "pending_tasks": pending,
        },
        args.json,
    )
    return 0


def command_claim(args: argparse.Namespace) -> int:
    requested_owner = requested_owner_thread_id(args)
    current_thread_id = os.environ.get("CODEX_THREAD_ID")
    if not current_thread_id:
        fatal(
            "Marker claim requires CODEX_THREAD_ID from the current Codex task; an explicit "
            "owner ID is not proof that this process is running in that owner."
        )
    if requested_owner and requested_owner != current_thread_id:
        fatal(
            "Refusing to claim an event from a different current thread. Marker output is not "
            "an atomic cross-thread delivery mechanism."
        )
    owner_thread_id = sanitize_session_id(current_thread_id)
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    # Claim, cancel, stop, and the watcher all serialize on the same transition
    # lock. A marker can therefore become claimed or cancelled, never both.
    with exclusive_watcher_guard(task_file):
        payload = load_json(task_file)
        task_owner = str(payload.get("owner_thread_id") or payload.get("session_id") or "")
        if task_owner != owner_thread_id:
            fatal(
                "Refusing to claim an event from another branch/thread.\n"
                f"current_owner_thread_id: {owner_thread_id}\n"
                f"event_owner_thread_id: {task_owner}"
            )
        if payload.get("phase") != "marker_pending":
            fatal(
                f"Task {args.task_id} is not claimable from the marker queue "
                f"(phase={payload.get('phase')})."
            )
        prompt_text = load_verified_resume_prompt(payload)
        if task_uses_ledger(payload):
            task_id_value, token, generation = task_reservation_identity(payload)
            ledger = ledger_for_task(payload)
            try:
                entry = ledger.begin_next_submission(task_id_value)
            except (SubmissionBlocked, LedgerError) as error:
                fatal(f"Marker event cannot bypass the owner READY-order FIFO: {error}")
            if entry is None:
                fatal("Marker event is missing from the owner READY-order FIFO.")
            payload["phase"] = "marker_claiming"
            payload["delivery_status"] = "marker_claim_committed_waiting_for_output"
            payload["marker_claim_started_at"] = now_utc()
            write_json(task_file, payload)
        emit(
            {
                "status": "claimed",
                "task_id": args.task_id,
                "event_id": payload.get("event_id"),
                "owner_thread_id": owner_thread_id,
                "resume_prompt": prompt_text,
                "exactly_once": False,
                "warning": (
                    "Marker claim is a safe branch-bound fallback, but file output and Codex input "
                    "are not one atomic transaction. Current public Codex APIs do not provide a "
                    "strict exactly-once enqueue operation."
                ),
            },
            args.json,
        )
        # stdout was flushed before recording ACCEPTED.  If output fails or
        # this process dies in between, the durable SUBMITTING fence is later
        # recovered as UNKNOWN and the marker is never replayed.
        if task_uses_ledger(payload):
            ledger.finish_submission(
                task_id_value,
                token,
                generation,
                "accepted",
                detail="exact owner claimed marker output under the task transition lock",
            )
            finish_job_reservation(payload, "accepted")
        payload["phase"] = "marker_claimed"
        payload["delivery_status"] = "claimed_by_owner"
        payload["marker_claimed_at"] = now_utc()
        payload["marker_claimed_by_thread_id"] = owner_thread_id
        write_json(task_file, payload)
        return 0


def command_doctor(args: argparse.Namespace) -> int:
    explicit_owner_thread_id = requested_owner_thread_id(args)
    actor_thread_id, actor_inferred_from_owner = current_actor_thread_id(explicit_owner_thread_id)
    app_server_context = app_server_context_from_args(args)
    endpoint = str(app_server_context["endpoint"])
    auth_token_env = app_server_auth_env_from_args(args)
    try:
        owner_route = resolve_owner_route(
            endpoint,
            actor_thread_id,
            explicit_owner_thread_id,
            auth_token_env,
        )
        owner_thread_id = str(owner_route["owner_thread_id"])
        owner_route["actor_identity_verified"] = not actor_inferred_from_owner
        if actor_inferred_from_owner:
            owner_route["route_verified"] = False
            owner_route["route_verification_error"] = (
                "CODEX_THREAD_ID was absent; actor identity was inferred from the requested owner"
            )
        routing_error = None
    except OwnerRoutingError as error:
        owner_route = {
            "actor_thread_id": actor_thread_id,
            "owner_thread_id": explicit_owner_thread_id or actor_thread_id,
            "metadata_verified": True,
            "route_verified": False,
            "route": "rejected",
        }
        owner_thread_id = str(owner_route["owner_thread_id"])
        routing_error = str(error)
    report = inspect_native_thread(
        endpoint,
        owner_thread_id,
        bearer_token_env=auth_token_env,
    )
    authority_assessment: dict[str, Any] | None = None
    if report.get("native_message_ready") and isinstance(report.get("authority"), dict):
        authority_assessment = assess_authority_strength(
            app_server_context,
            dict(report["authority"]),
        )
    allow_weak_authority = bool(getattr(args, "allow_weak_authority", False))
    decision_arguments = {
        "route_verified": bool(
            owner_route.get("route_verified") and routing_error is None
        ),
        "attachable": app_server_context.get("attachable") is True,
        "native_message_ready": bool(
            report.get("native_message_ready")
            and isinstance(report.get("authority"), dict)
        ),
        "authority_strength": (
            str(authority_assessment.get("authority_strength"))
            if authority_assessment
            else None
        ),
        "allow_weak_authority": allow_weak_authority,
        "context_reason": str(
            app_server_context.get("reason")
            or "the verified owner authority is not externally attachable"
        ),
        "probe_reason": str(
            report.get("error")
            or report.get("thread_status")
            or "the exact owner is not positively loaded on this authority"
        ),
        "authority_strength_reason": (
            str(authority_assessment.get("authority_strength_reason"))
            if authority_assessment
            else None
        ),
    }
    auto_decision = classify_delivery_decision(
        "auto",
        **decision_arguments,
    )
    native_decision = classify_delivery_decision(
        "native-message",
        **decision_arguments,
    )
    marker_decision = classify_delivery_decision(
        "marker",
        **decision_arguments,
    )
    preferred = resolve_codex_binary(args.codex_bin)
    path_binary_value = shutil.which("codex")
    path_binary = Path(path_binary_value).resolve() if path_binary_value else None
    report.update(
        {
            "preferred_codex_binary": str(preferred),
            "preferred_codex_version": codex_version(preferred),
            "path_codex_binary": str(path_binary) if path_binary else None,
            "path_codex_version": codex_version(path_binary) if path_binary else None,
            "version_skew": bool(path_binary and path_binary != preferred),
            "actor_thread_id": actor_thread_id,
            "actor_inferred_from_owner": actor_inferred_from_owner,
            "owner_route": owner_route,
            "owner_routing_error": routing_error,
            "delivery_branch": auto_decision["branch"],
            "recommended_protocol": auto_decision["action"],
            "auto_delivery_decision": auto_decision,
            "explicit_native_decision": native_decision,
            "explicit_marker_decision": marker_decision,
            "native_message_protocol": (
                "available (authority-bound FIFO with userMessage acknowledgement)"
                if native_decision["action"] == "native-message"
                else "unavailable"
            ),
            "marker_protocol": (
                "available (owner-bound manual claim; does not wake an idle task)"
                if marker_decision["action"] == "marker"
                else "unavailable"
            ),
            "authority_assessment": authority_assessment,
            "authority_strength": (
                authority_assessment.get("authority_strength")
                if authority_assessment
                else None
            ),
            "authority_strength_reason": (
                authority_assessment.get("authority_strength_reason")
                if authority_assessment
                else None
            ),
            "weak_authority_opt_in": allow_weak_authority,
            "native_streaming": (
                "uses the owning app-server event stream; attached Terminal, Desktop, and "
                "Remote Control clients receive normal incremental events"
            ),
            "app_server_auth_token_env": auth_token_env,
            "app_server_context": app_server_context,
            "credential_material_persisted": False,
            "strict_exactly_once_protocol": "unavailable in the current public Codex API",
            "strict_exactly_once_gap": (
                "clientUserMessageId is observable but is not a server-side idempotency key; "
                "host handoff also has no public authority-epoch CAS"
            ),
        }
    )
    emit(report, args.json)
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    task_payload = load_json(task_file)
    if ledger_result := preflight_cancellation_ledger(task_file, task_payload):
        emit(ledger_result, args.json)
        return 0 if ledger_result["status"] == "cancelled" else 4
    if reason := cancellation_block_reason(task_payload):
        fatal(f"Task {args.task_id} cannot be cancelled safely: {reason}.")
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    termination = terminate_persisted_watcher(task_payload)
    if not termination["safe_to_cancel"]:
        emit(
            {
                "status": "cancel_blocked",
                "task_id": args.task_id,
                "still_alive_pids": termination["still_alive_pids"],
                "warning": (
                    "the exact durable watcher incarnation could not be stopped safely; "
                    "the event reservation was retained"
                ),
                "identity_error": termination.get("identity_error"),
            },
            args.json,
        )
        return 4
    with exclusive_watcher_guard(task_file):
        task_payload = load_json(task_file)
        if task_uses_ledger(task_payload):
            ledger_result = cancel_owner_ledger_or_reconcile(task_file, task_payload)
            if ledger_result is not None:
                emit(ledger_result, args.json)
                return 0 if ledger_result["status"] == "cancelled" else 4
        elif reason := cancellation_block_reason(task_payload):
            fatal(f"Task {args.task_id} cannot be cancelled safely: {reason}.")
        finish_job_reservation(task_payload, "cancelled")
        task_payload["phase"] = "cancelled"
        task_payload["cancelled_at"] = now_utc()
        task_payload["protected_pids_during_cancel"] = termination.get("excluded_pids", [])
        task_payload["stopped_related_pids"] = termination["terminated_pids"]
        task_payload["still_alive_pids_after_cancel"] = []
        task_payload["watcher_exited_after_cancel"] = True
        task_payload["watcher_identity_stop_result"] = termination.get("identity_results", [])
        write_json(task_file, task_payload)
    emit(
        {
            "status": "cancelled",
            "task_id": args.task_id,
            "watcher_pid": watcher_pid or "none",
            "watcher_exited": True,
            "protected_pids": termination.get("excluded_pids", []),
            "stopped_related_pids": termination["terminated_pids"],
            "still_alive_pids": termination["still_alive_pids"],
        },
        args.json,
    )
    return 0


def stop_single_task(task_file: Path, also_stop_target: bool) -> dict[str, Any]:
    task_payload = load_json(task_file)
    if ledger_result := preflight_cancellation_ledger(task_file, task_payload):
        return ledger_result
    if reason := cancellation_block_reason(task_payload):
        return {
            "status": "cancel_blocked",
            "task_id": str(task_payload.get("task_id") or ""),
            "warning": reason,
        }
    watcher_pid = int(task_payload.get("watcher_pid") or 0)
    termination = terminate_persisted_watcher(task_payload)
    if not termination["safe_to_cancel"]:
        return {
            "status": "cancel_blocked",
            "task_id": str(task_payload.get("task_id") or ""),
            "still_alive_pids": termination["still_alive_pids"],
            "warning": (
                "the exact durable watcher incarnation could not be stopped safely; "
                "the event reservation was retained"
            ),
            "identity_error": termination.get("identity_error"),
        }

    with exclusive_watcher_guard(task_file):
        task_payload = load_json(task_file)
        if task_uses_ledger(task_payload):
            ledger_result = cancel_owner_ledger_or_reconcile(task_file, task_payload)
            if ledger_result is not None:
                return ledger_result
        elif reason := cancellation_block_reason(task_payload):
            return {
                "status": "cancel_blocked",
                "task_id": str(task_payload.get("task_id") or ""),
                "warning": reason,
            }
        finish_job_reservation(task_payload, "cancelled")
        task_payload["phase"] = "cancelled"
        task_payload["cancelled_at"] = now_utc()
        task_payload["protected_pids_during_cancel"] = termination.get("excluded_pids", [])
        task_payload["stopped_related_pids"] = termination["terminated_pids"]
        task_payload["still_alive_pids_after_cancel"] = []
        task_payload["watcher_exited_after_cancel"] = True
        task_payload["watcher_identity_stop_result"] = termination.get("identity_results", [])
        write_json(task_file, task_payload)

    # Stopping the watched process is intentionally sequenced after the event
    # cancellation has committed. Otherwise --also-stop-target can kill user
    # work even when the ledger concurrently advanced to SUBMITTING/UNKNOWN and
    # correctly rejected cancellation.
    target_stop_result: dict[str, Any] | None = None
    if also_stop_target:
        try:
            target_stop_result = stop_target(task_payload["target"])
        except Exception as error:  # cancellation is already durable
            target_stop_result = {"status": "stop_failed", "detail": str(error)}
        with exclusive_watcher_guard(task_file):
            task_payload = load_json(task_file)
            task_payload["target_stop_result"] = target_stop_result
            write_json(task_file, task_payload)
    return {
        "status": "cancelled",
        "task_id": str(task_payload.get("task_id") or ""),
        "watcher_pid": watcher_pid or "none",
        "watcher_exited": True,
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
        active_entries, _ = active_and_stale_task_snapshots(tasks_dir)
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


def command_reconcile(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    # A live dispatcher owns this same lifetime lock while waiting for its ACK.
    # Only a reconciler that acquires the lock may conclude SUBMITTING was
    # interrupted and fence it UNKNOWN.
    with exclusive_watcher_guard(task_file):
        return command_reconcile_owned(args, task_file)


def command_reconcile_owned(args: argparse.Namespace, task_file: Path) -> int:
    task_payload = load_json(task_file)
    if task_payload.get("resume_protocol") != "native-message":
        fatal("Only native-message events have an app-server acknowledgement to reconcile.")

    ledger = ledger_for_task(task_payload)
    task_id_value, token, generation = task_reservation_identity(task_payload)
    entry = ledger.validate(task_id_value, token, generation)
    if entry["state"] == "SUBMITTING":
        entry = ledger.fence_interrupted_submission(
            task_id_value,
            token,
            generation,
            "recovery found a prior process interrupted after READY->SUBMITTING",
        )
        finish_job_reservation(task_payload, "unknown")
        task_payload = update_delivery_task(
            task_file,
            phase="native_message_unknown",
            status="interrupted_submission_fenced_unknown",
        )
    sync_job_reservation_from_ledger(task_payload, entry)
    if entry["state"] == "ACCEPTED":
        emit(
            {
                "status": "accepted",
                "task_id": task_id_value,
                "client_user_message_id": task_payload["client_user_message_id"],
            },
            args.json,
        )
        return 0
    if entry["state"] != "UNKNOWN":
        fatal(
            f"Task {task_id_value} is {entry['state']}; only UNKNOWN/SUBMITTING needs "
            "positive-history reconciliation."
        )

    authority = dict(entry["authority"])
    credential_ref = authority.get("credential_ref")
    bearer_token_env = (
        str(credential_ref.get("name"))
        if isinstance(credential_ref, dict) and credential_ref.get("kind") == "environment"
        else None
    )
    client_message_id = str(task_payload["client_user_message_id"])
    try:
        strength = str(authority.get("authority_strength") or "")
        if strength not in {"strong", "weak"}:
            raise AuthorityMismatch(
                "native ticket has no recognized authority-strength classification"
            )
        if strength == "weak" and not bool(
            authority.get("weak_authority_accepted")
        ):
            raise AuthorityMismatch(
                "weak native authority ledger entry has no durable explicit opt-in"
            )
        probe_strong_authority_process(authority)
        with AppServerClient(
            str(authority["endpoint"]),
            bearer_token_env=bearer_token_env,
        ) as client:
            current_authority = client.authority_descriptor()
            if bearer_token_env:
                current_authority["credential_ref"] = {
                    "kind": "environment",
                    "name": bearer_token_env,
                }
            mismatch = authority_descriptor_mismatch(authority, current_authority)
            if mismatch:
                fatal(f"Cannot reconcile through a different authority: {mismatch}")
            owner_thread_id = str(task_payload["owner_thread_id"])
            thread_summary = client.read_thread(
                owner_thread_id,
                include_turns=False,
            )
            thread = read_thread_history(
                client,
                owner_thread_id,
                thread_summary,
                items_view="full",
            )
    except (AuthorityMismatch, AppServerError, OSError) as error:
        emit(
            {
                "status": "still_unknown",
                "task_id": task_id_value,
                "reason": str(error),
                "automatic_retry": False,
            },
            args.json,
        )
        return 4

    if not value_contains_client_id(thread, client_message_id):
        emit(
            {
                "status": "still_unknown",
                "task_id": task_id_value,
                "reason": (
                    "the matching clientUserMessageId was not found; absence is not proof "
                    "that the original submission failed"
                ),
                "automatic_retry": False,
            },
            args.json,
        )
        return 4

    ledger.confirm_unknown_accepted(
        task_id_value,
        token,
        generation,
        f"owner history contains userMessage.clientId={client_message_id}",
    )
    finish_job_reservation(task_payload, "accepted")
    update_delivery_task(
        task_file,
        phase="native_message_accepted",
        status="accepted_by_positive_history_reconciliation",
        delivery_reconciled_at=now_utc(),
    )
    emit(
        {
            "status": "reconciled_accepted",
            "task_id": task_id_value,
            "client_user_message_id": client_message_id,
        },
        args.json,
    )
    return 0


def command_recover(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    task_file = state_dir / "tasks" / f"{args.task_id}.json"
    if not task_file.exists():
        fatal(f"Task not found: {args.task_id}")
    restart_watcher = False
    restart_log_file: Path | None = None
    with exclusive_watcher_guard(task_file):
        task_payload = load_json(task_file)
        phase = str(task_payload.get("phase") or "")
        protocol = str(task_payload.get("resume_protocol") or "")
        if protocol not in {"native-message", "marker"}:
            fatal("This recovery command supports protocol-v3 native-message or marker tasks.")

        # The scheduler can be killed after either durable reservation but
        # before mirroring its generation into the task JSON.  Exact reserve /
        # register calls recover both crash points without creating a second
        # event or changing its FIFO identity.
        if phase in {
            "reserving",
            "registration_recovery_required",
            "scheduled",
            "watching",
        } and task_payload.get("job_key") and (
            task_payload.get("job_reservation_generation") is None
            or task_payload.get("lock_generation") is None
        ):
            try:
                task_payload, ledger, entry = ensure_task_reservations(
                    task_file,
                    task_payload,
                )
            except (JobRegistryError, LedgerError) as error:
                failure_phase = reconcile_registration_failure(
                    task_file,
                    task_payload,
                    error,
                )
                if failure_phase == "registration_blocked":
                    emit(
                        {
                            "status": "registration_blocked",
                            "task_id": args.task_id,
                            "reason": str(error),
                        },
                        args.json,
                    )
                    return 4
                fatal(
                    "Could not repair the interrupted registration without guessing: "
                    f"{error}"
                )
        else:
            ledger = ledger_for_task(task_payload)
            task_id_value, token, generation = task_reservation_identity(task_payload)
            entry = ledger.validate(task_id_value, token, generation)

        task_id_value, token, generation = task_reservation_identity(task_payload)
        # Re-read through the recovered immutable generation before acting.
        entry = ledger.validate(task_id_value, token, generation)
        if entry["state"] == "SUBMITTING":
            ledger.fence_interrupted_submission(
                task_id_value,
                token,
                generation,
                (
                    "marker claimant exited after acquiring delivery rights"
                    if protocol == "marker"
                    else "explicit recovery found an interrupted submission"
                ),
            )
            entry = ledger.validate(task_id_value, token, generation)
            sync_job_reservation_from_ledger(task_payload, entry)
            update_delivery_task(
                task_file,
                phase=("marker_unknown" if protocol == "marker" else "native_message_unknown"),
                status=(
                    "marker_output_outcome_unknown_no_replay"
                    if protocol == "marker"
                    else "interrupted_submission_fenced_unknown"
                ),
            )
            emit(
                {
                    "status": "marker_unknown" if protocol == "marker" else "fenced_unknown",
                    "task_id": task_id_value,
                    "next": (
                        "never replay marker output automatically"
                        if protocol == "marker"
                        else "run reconcile; never resend automatically"
                    ),
                },
                args.json,
            )
            return 4

        terminal_state = sync_job_reservation_from_ledger(task_payload, entry)
        if terminal_state == "ACCEPTED":
            update_delivery_task(
                task_file,
                phase=("marker_claimed" if protocol == "marker" else "native_message_accepted"),
                status="terminal_ledger_mirror_recovered_no_replay",
            )
            emit(
                {"status": "already_accepted", "task_id": task_id_value},
                args.json,
            )
            return 0
        if terminal_state == "BLOCKED":
            update_delivery_task(
                task_file,
                phase=("marker_blocked" if protocol == "marker" else "native_message_blocked"),
                status="terminal_ledger_mirror_recovered_no_replay",
            )
            emit({"status": "blocked", "task_id": task_id_value}, args.json)
            return 4
        if terminal_state == "CANCELLED":
            update_delivery_task(
                task_file,
                phase="cancelled",
                status="terminal_ledger_mirror_recovered_no_replay",
            )
            emit({"status": "cancelled", "task_id": task_id_value}, args.json)
            return 4
        if entry["state"] == "UNKNOWN":
            update_delivery_task(
                task_file,
                phase=("marker_unknown" if protocol == "marker" else "native_message_unknown"),
                status="terminal_ledger_mirror_recovered_no_replay",
            )
            emit(
                {
                    "status": "marker_unknown" if protocol == "marker" else "unknown",
                    "task_id": task_id_value,
                    "next": (
                        "never replay marker output automatically"
                        if protocol == "marker"
                        else "run reconcile; never resend automatically"
                    ),
                },
                args.json,
            )
            return 4

        if entry["state"] in {"SCHEDULED", "WATCHING"} and phase in {
            "reserving",
            "registration_recovery_required",
            "scheduled",
            "watching",
        }:
            task_payload["phase"] = "scheduled"
            task_payload["recovery_prepared_at"] = now_utc()
            task_payload.pop("watcher_pid", None)
            task_payload.pop("watcher_identity", None)
            write_json(task_file, task_payload)
            restart_watcher = True
            restart_log_file = Path(str(task_payload["log_file"]))
        elif entry["state"] in {"SCHEDULED", "WATCHING"} and phase == "event_staged":
            sequence = ledger.mark_ready(task_id_value, token, generation)
            entry = ledger.validate(task_id_value, token, generation)
            task_payload = update_delivery_task(
                task_file,
                phase=("marker_pending" if protocol == "marker" else "native_message_ready"),
                status="recovered_staged_event",
                marker_pending_at=(now_utc() if protocol == "marker" else None),
                ready_sequence=sequence,
            )
        elif entry["state"] == "READY":
            # The ledger commit is the source of truth if the watcher died
            # before mirroring event_staged -> *_pending/ready in task JSON.
            task_payload = update_delivery_task(
                task_file,
                phase=("marker_pending" if protocol == "marker" else "native_message_ready"),
                status="recovered_ready_ledger_mirror",
                marker_pending_at=(now_utc() if protocol == "marker" else None),
                ready_sequence=entry.get("ready_sequence"),
            )
        else:
            fatal(
                f"Task {task_id_value} cannot be recovered from task phase={phase}, "
                f"ledger state={entry['state']}."
            )

        if not restart_watcher and protocol == "marker":
            emit({"status": "marker_recovered", "task_id": args.task_id}, args.json)
            return 0
        if not restart_watcher:
            prompt_text = load_verified_resume_prompt(task_payload)
            return dispatch_native_message(task_file, task_payload, prompt_text)

    # A replacement watcher must acquire the same lifetime guard before it can
    # acknowledge WATCHING, so spawning necessarily happens after recovery
    # releases its inspection/repair guard.
    assert restart_watcher and restart_log_file is not None
    try:
        watcher, startup_ack = spawn_watcher_with_ack(task_file, restart_log_file)
    except BaseException as error:
        # Retain SCHEDULED. Another recovery can retry, while a child whose ACK
        # pipe was lost still wins by holding the task guard and advancing it.
        emit(
            {
                "status": "watcher_restart_failed",
                "task_id": args.task_id,
                "reason": str(error),
                "reservation_retained": True,
            },
            args.json,
        )
        return 4
    emit(
        {
            "status": "watcher_restarted",
            "task_id": args.task_id,
            "watcher_pid": watcher.pid,
            "watcher_startup_ack": startup_ack,
        },
        args.json,
    )
    return 0


def command_freeze(args: argparse.Namespace) -> int:
    owner_thread_id = sanitize_session_id(requested_owner_thread_id(args))
    actor_thread_id = os.environ.get("CODEX_THREAD_ID")
    if actor_thread_id != owner_thread_id:
        fatal(
            "Freeze must run from the exact durable owner task before handoff; "
            f"current={actor_thread_id or 'missing'} owner={owner_thread_id}."
        )
    ledger = HandoffLedger(args.coordination_dir, owner_thread_id)
    try:
        snapshot = ledger.freeze_authority(int(args.expected_epoch))
    except LedgerError as error:
        fatal(f"Could not freeze owner authority: {error}")
    emit(
        {
            "status": "frozen",
            "owner_thread_id": owner_thread_id,
            "authority_epoch": snapshot["authority_epoch"],
            "mode": snapshot["mode"],
        },
        args.json,
    )
    return 0


def command_rebind(args: argparse.Namespace) -> int:
    owner_thread_id = sanitize_session_id(requested_owner_thread_id(args))
    actor_thread_id = os.environ.get("CODEX_THREAD_ID")
    if actor_thread_id != owner_thread_id:
        fatal(
            "Rebind must run from the exact durable owner task after handoff; "
            f"current={actor_thread_id or 'missing'} owner={owner_thread_id}."
        )
    app_server_context = app_server_context_from_args(args)
    if app_server_context.get("attachable") is not True:
        fatal(
            "The new owner authority is not provably attachable from this task: "
            f"{app_server_context.get('reason') or app_server_context.get('source')}"
        )
    endpoint = str(app_server_context["endpoint"])
    auth_token_env = app_server_auth_env_from_args(args)
    route = resolve_owner_route(endpoint, actor_thread_id, owner_thread_id, auth_token_env)
    if not route.get("route_verified") or route.get("owner_thread_id") != owner_thread_id:
        fatal("The new app-server could not verify this exact durable owner route.")
    report = inspect_native_thread(
        endpoint,
        owner_thread_id,
        bearer_token_env=auth_token_env,
    )
    if not report.get("native_message_ready"):
        fatal(
            "The new authority is not attachable with this owner already loaded: "
            f"{report.get('error') or report.get('thread_status')}"
        )
    authority = dict(report["authority"])
    authority_assessment = assess_authority_strength(app_server_context, authority)
    authority.update(authority_assessment)
    if (
        authority["authority_strength"] != "strong"
        and not bool(getattr(args, "allow_weak_authority", False))
    ):
        fatal(
            "The new authority has only weak instance binding: "
            f"{authority['authority_strength_reason']}. Refusing rebind without "
            "--allow-weak-authority."
        )
    if auth_token_env:
        authority["credential_ref"] = {"kind": "environment", "name": auth_token_env}
    authority["weak_authority_accepted"] = bool(
        authority["authority_strength"] == "weak"
        and getattr(args, "allow_weak_authority", False)
    )
    ledger = HandoffLedger(args.coordination_dir, owner_thread_id)
    try:
        # Strength assessment happened before route/thread probing.  Fence the
        # exact destination process again immediately before the epoch CAS so
        # a dead or reused strong owner cannot be committed by rebind.
        probe_strong_authority_process(authority)
        new_epoch = ledger.rebind_authority(int(args.expected_epoch), authority)
    except LedgerError as error:
        fatal(f"Authority rebind failed closed: {error}")
    emit(
        {
            "status": "rebound",
            "owner_thread_id": owner_thread_id,
            "authority_epoch": new_epoch,
            "authority": authority,
            "authority_strength": authority["authority_strength"],
            "authority_strength_reason": authority["authority_strength_reason"],
        },
        args.json,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hand off a long blocking wait to a detached watcher and route one completion event "
            "to its immutable durable Codex owner."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schedule = subparsers.add_parser("schedule", help="Preflight a long-running job and schedule a detached wait.")
    schedule.add_argument("--pid", type=int, help="PID to watch.")
    schedule.add_argument("--pattern", help="Process pattern to watch when PID capture is awkward.")
    schedule.add_argument("--host", help="Optional remote host. Without this, the watch is local.")
    schedule.add_argument(
        "--job-id",
        help=(
            "Stable logical job id. The default permits one accepted wake for the process "
            "incarnation across forks; use a new explicit id for an intentional later "
            "monitoring cycle."
        ),
    )
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
    schedule.add_argument(
        "--owner-thread-id",
        help=(
            "Immutable durable thread that receives the completion event. Normally omitted for a "
            "top-level thread or regular fork. Current /side conversations are rejected even when "
            "this option is supplied because their parent cannot be re-verified via thread/read."
        ),
    )
    schedule.add_argument(
        "--session-id",
        help="Deprecated alias for --owner-thread-id. This value is a thread ID, not sessionId.",
    )
    schedule.add_argument("--cwd", help="Working directory to use when resuming Codex.")
    schedule.add_argument(
        "--resume-protocol",
        choices=("auto", "native-message", "marker"),
        default=os.environ.get("CODEX_WAIT_RESUME_PROTOCOL", "auto"),
        help=(
            "Delivery protocol. auto uses native-message only when the exact owner thread is "
            "already loaded in the ticketed app-server, otherwise it leaves an owner-bound marker."
        ),
    )
    schedule.add_argument(
        "--allow-weak-authority",
        action="store_true",
        help=(
            "Explicitly allow native delivery through an authority without a per-process "
            "instance proof (including WS/WSS and non-ancestor endpoints). This preserves "
            "native streaming but cannot fence endpoint reuse or proxy backend changes."
        ),
    )
    schedule.add_argument(
        "--app-server-endpoint",
        help=(
            "Exact owning app-server endpoint: unix://, ws://, or wss://. The scheduler first "
            "uses a proven ancestor listener; otherwise the managed daemon socket is only a "
            "routing diagnostic endpoint. Explicit remote TUI sessions should pass their exact "
            "connect endpoint when ancestor discovery is unavailable."
        ),
    )
    schedule.add_argument(
        "--app-server-auth-token-env",
        help=(
            "Name of an environment variable containing the endpoint bearer token. The token "
            "itself is never written to the ticket."
        ),
    )
    schedule.add_argument("--note", help="Short note appended to the resume prompt.")
    schedule.add_argument(
        "--resume-prompt",
        help="Inline continuation instructions for the durable owner task.",
    )
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
            "Seconds to wait after a definitive pre-submission or explicit rejection. "
            "Default: 1200 seconds (20 minutes)."
        ),
    )
    schedule.add_argument(
        "--resume-retry-max-attempts",
        type=int,
        default=int(os.environ.get("CODEX_WAIT_RESUME_RETRY_MAX_ATTEMPTS", DEFAULT_RESUME_RETRY_MAX_ATTEMPTS)),
        help=(
            "Maximum attempts only when no continuation was accepted. Ambiguous submission "
            "outcomes are never retried. "
            "Use 0 for unlimited retries. Default: 12."
        ),
    )
    schedule.add_argument(
        "--state-collision-max-attempts",
        type=int,
        default=int(
            os.environ.get(
                "CODEX_WAIT_STATE_COLLISION_MAX_ATTEMPTS",
                DEFAULT_STATE_COLLISION_MAX_ATTEMPTS,
            )
        ),
        help=(
            "Maximum one-second retries after explicit no-active/mismatched-turn or "
            "Review/Compact rejection. This budget persists across watcher recovery and is "
            "separate from authority retries. Use 0 only for an intentional unlimited wait. "
            "Default: 900."
        ),
    )
    schedule.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    schedule.add_argument("--dry-run-resume", action="store_true", help="Observe the target but skip event delivery.")
    schedule.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    watch = subparsers.add_parser("watch", help="Internal detached watcher entrypoint.")
    watch.add_argument("--task-file", required=True, help="Internal task file path.")
    watch.add_argument(
        "--startup-fd",
        type=int,
        help=argparse.SUPPRESS,
    )

    status = subparsers.add_parser("status", help="Inspect active or historical wait tasks.")
    status.add_argument("--task-id", help="Show a specific task.")
    status.add_argument("--session-id", help="Show the active task for a session if one exists.")
    status.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    active = subparsers.add_parser(
        "active",
        help="List tasks whose exact persisted watcher incarnation is still alive.",
    )
    active.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    active.add_argument("--include-stale", action="store_true", help="Also report stale active-looking task records with no live processes.")
    active.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    pending = subparsers.add_parser(
        "pending",
        help="List undelivered durable events for one exact owner thread without claiming them.",
    )
    pending.add_argument("--owner-thread-id", help="Exact owner thread id. Defaults to CODEX_THREAD_ID.")
    pending.add_argument("--session-id", help="Exact owner thread id. Defaults to CODEX_THREAD_ID.")
    pending.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    pending.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    claim = subparsers.add_parser(
        "claim",
        help="Claim a marker-only event from its exact owner thread.",
    )
    claim.add_argument("--task-id", required=True, help="Task id to claim.")
    claim.add_argument(
        "--owner-thread-id",
        help="Optional owner cross-check; must equal the required CODEX_THREAD_ID.",
    )
    claim.add_argument(
        "--session-id",
        help="Alias for --owner-thread-id; must equal the required CODEX_THREAD_ID.",
    )
    claim.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    claim.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Resolve UNKNOWN only when the matching clientUserMessageId is found in history.",
    )
    reconcile.add_argument("--task-id", required=True, help="Task id to reconcile.")
    reconcile.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    reconcile.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    recover = subparsers.add_parser(
        "recover",
        help="Recover a staged/ready event or fence an interrupted submission UNKNOWN.",
    )
    recover.add_argument("--task-id", required=True, help="Task id to recover.")
    recover.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    recover.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    freeze = subparsers.add_parser(
        "freeze",
        help="Freeze one owner FIFO before an execution-host handoff.",
    )
    freeze.add_argument("--owner-thread-id", help="Exact durable owner; defaults to CODEX_THREAD_ID.")
    freeze.add_argument("--session-id", help="Alias for --owner-thread-id.")
    freeze.add_argument("--expected-epoch", type=int, required=True, help="Current authority epoch CAS value.")
    freeze.add_argument("--coordination-dir", default=DEFAULT_COORDINATION_DIR, help="Owner-ledger directory.")
    freeze.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    rebind = subparsers.add_parser(
        "rebind",
        help="CAS pending events to a newly verified attachable owner authority.",
    )
    rebind.add_argument("--owner-thread-id", help="Exact durable owner; defaults to CODEX_THREAD_ID.")
    rebind.add_argument("--session-id", help="Alias for --owner-thread-id.")
    rebind.add_argument("--expected-epoch", type=int, required=True, help="Old authority epoch CAS value.")
    rebind.add_argument("--app-server-endpoint", help="New exact unix://, ws://, or wss:// owner endpoint.")
    rebind.add_argument("--app-server-auth-token-env", help="Bearer-token environment variable name.")
    rebind.add_argument(
        "--allow-weak-authority",
        action="store_true",
        help="Explicitly permit rebinding to a weak WS/WSS or unproven endpoint authority.",
    )
    rebind.add_argument("--coordination-dir", default=DEFAULT_COORDINATION_DIR, help="Owner-ledger directory.")
    rebind.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    doctor = subparsers.add_parser(
        "doctor",
        help="Check owner routing, authority pinning, and native-message streaming availability.",
    )
    doctor.add_argument("--owner-thread-id", help="Optional explicit durable owner thread id.")
    doctor.add_argument("--session-id", help="Exact owner thread id. Defaults to CODEX_THREAD_ID.")
    doctor.add_argument("--app-server-endpoint", help="Exact unix://, ws://, or wss:// owner endpoint.")
    doctor.add_argument("--app-server-auth-token-env", help="Bearer-token environment variable name.")
    doctor.add_argument(
        "--allow-weak-authority",
        action="store_true",
        help="Show whether explicit weak-authority opt-in would enable native delivery.",
    )
    doctor.add_argument("--codex-bin", help="Optional Codex binary override for version diagnostics.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    cancel = subparsers.add_parser("cancel", help="Cancel a scheduled wait task.")
    cancel.add_argument("--task-id", required=True, help="Task id to cancel.")
    cancel.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    cancel.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    stop = subparsers.add_parser(
        "stop",
        help="Safely stop the exact watcher first, then optionally stop the watched target.",
    )
    stop.add_argument("--task-id", help="Task id to stop.")
    stop.add_argument(
        "--all-active",
        action="store_true",
        help="Stop every task with a live, identity-verified watcher.",
    )
    stop.add_argument(
        "--also-stop-target",
        action="store_true",
        help="After the exact watcher is down, also stop the captured target incarnation set.",
    )
    stop.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="State directory for tasks and logs.")
    stop.add_argument("--json", action="store_true", help="Print machine-readable JSON output.")

    serve = subparsers.add_parser(
        "serve",
        help="Start a loopback dashboard with guarded exact-watcher stop controls.",
    )
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
    if args.command == "pending":
        return command_pending(args)
    if args.command == "claim":
        return command_claim(args)
    if args.command == "reconcile":
        return command_reconcile(args)
    if args.command == "recover":
        return command_recover(args)
    if args.command == "freeze":
        return command_freeze(args)
    if args.command == "rebind":
        return command_rebind(args)
    if args.command == "doctor":
        return command_doctor(args)
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
