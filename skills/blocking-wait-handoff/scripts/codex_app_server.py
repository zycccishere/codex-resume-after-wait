#!/usr/bin/env python3
"""Small dependency-free WebSocket client for Codex app-server.

The wait handoff script deliberately keeps this client local and boring.  It
only implements the subset of RFC 6455 and JSON-RPC needed by the handoff
protocol over ``unix://``, ``ws://``, and ``wss://`` endpoints.  This lets the
skill work without asking users to install a Python WebSocket package into
every local or SSH environment.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import stat
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOADED_THREAD_STATUS_TYPES = frozenset({"idle", "active", "systemError"})
THREAD_TURNS_ITEMS_VIEWS = frozenset({"notLoaded", "summary", "full"})


class AppServerError(RuntimeError):
    """Raised when the app-server transport or JSON-RPC request fails."""


class AppServerRpcError(AppServerError):
    def __init__(self, method: str, error: Any):
        super().__init__(f"app-server request {method!r} failed: {error}")
        self.method = method
        self.error = error


class AppServerTimeoutError(AppServerError):
    """Raised when no matching app-server message arrives before a deadline."""


@dataclass(frozen=True)
class AppServerEndpoint:
    """Parsed app-server endpoint with no authentication material."""

    uri: str
    transport: str
    socket_path: Path | None = None
    host: str | None = None
    port: int | None = None
    request_target: str = "/"
    host_header: str = "localhost"


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def default_app_server_socket() -> Path:
    configured = os.environ.get("CODEX_WAIT_APP_SERVER_SOCKET")
    if configured:
        return Path(configured).expanduser().resolve()
    return default_codex_home() / "app-server-control" / "app-server-control.sock"


def default_app_server_endpoint() -> str:
    return f"unix://{default_app_server_socket()}"


def parse_app_server_endpoint(endpoint: str | os.PathLike[str]) -> AppServerEndpoint:
    """Parse and canonicalize a supported app-server endpoint.

    A plain path remains accepted for callers written before endpoint URIs were
    introduced.  ``unix://`` with no path resolves to Codex's managed-daemon
    socket, matching ``codex app-server --listen unix://``.
    """

    if isinstance(endpoint, os.PathLike):
        raw = os.fspath(endpoint)
    else:
        raw = endpoint
    if not isinstance(raw, str) or not raw:
        raise AppServerError("app-server endpoint must be a non-empty path or URI")

    if "://" not in raw:
        path = Path(raw).expanduser().resolve()
        return AppServerEndpoint(
            uri=f"unix://{path}",
            transport="unix",
            socket_path=path,
            request_target="/rpc",
        )

    if raw.startswith("unix://"):
        raw_path = unquote(raw[len("unix://") :])
        path = (
            default_app_server_socket()
            if not raw_path
            else Path(raw_path).expanduser().resolve()
        )
        return AppServerEndpoint(
            uri=f"unix://{path}",
            transport="unix",
            socket_path=path,
            request_target="/rpc",
        )

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"}:
        raise AppServerError(
            f"unsupported app-server endpoint scheme {parsed.scheme!r}; "
            "expected unix://, ws://, or wss://"
        )
    if parsed.username is not None or parsed.password is not None:
        raise AppServerError(
            "credentials are not allowed in app-server endpoint URIs; "
            "use a bearer-token environment variable"
        )
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise AppServerError(f"invalid app-server endpoint {raw!r}: {error}") from error
    if not host:
        raise AppServerError(f"app-server endpoint has no host: {raw!r}")
    if scheme == "ws":
        loopback = host.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise AppServerError(
                "unencrypted ws:// app-server endpoints must be loopback; use wss:// "
                "or an SSH-forwarded localhost endpoint so bearer credentials and "
                "continuations are not sent in cleartext"
            )
    if port is None:
        port = 443 if scheme == "wss" else 80

    host_for_uri = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "wss" else 80
    netloc = host_for_uri if port == default_port else f"{host_for_uri}:{port}"
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    canonical = SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    return AppServerEndpoint(
        uri=urlunsplit(canonical),
        transport=scheme,
        host=host,
        port=port,
        request_target=target,
        host_header=netloc,
    )


def unix_socket_fingerprint(socket_path: Path) -> str:
    """Return an inode-bound identity for a Unix app-server listener."""

    metadata = socket_path.stat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise AppServerError(f"app-server endpoint is not a Unix socket: {socket_path}")
    return f"unix-inode:{metadata.st_dev}:{metadata.st_ino}"


def authority_descriptor_mismatch(
    expected: dict[str, Any], current: dict[str, Any]
) -> str | None:
    """Return why an authority changed, or ``None`` when it still matches.

    Unix descriptors fail closed without an inode fingerprint.  For network
    endpoints the endpoint itself is the transport boundary; remote-control
    installation/environment identifiers are additionally fenced when the
    original descriptor supplied them.
    """

    for field in ("transport", "endpoint"):
        if expected.get(field) != current.get(field):
            return f"{field} changed"

    expected_fingerprint = expected.get("endpoint_fingerprint")
    if expected.get("transport") == "unix" and not expected_fingerprint:
        return "expected Unix endpoint fingerprint is missing"
    if expected_fingerprint != current.get("endpoint_fingerprint"):
        return "endpoint fingerprint changed"

    expected_initialize = expected.get("initialize")
    current_initialize = current.get("initialize")
    if isinstance(expected_initialize, dict):
        if not isinstance(current_initialize, dict):
            return "initialize authority identity is no longer available"
        for field in ("codexHome", "platformFamily", "platformOs"):
            value = expected_initialize.get(field)
            if value is not None and value != current_initialize.get(field):
                return f"initialize {field} changed"

    expected_remote = expected.get("remote_control")
    current_remote = current.get("remote_control")
    if isinstance(expected_remote, dict):
        if not isinstance(current_remote, dict):
            return "remote-control identity is no longer available"
        for field in ("installationId", "environmentId", "serverName"):
            value = expected_remote.get(field)
            if value is not None and value != current_remote.get(field):
                return f"remote-control {field} changed"
    return None


def authority_descriptors_match(
    expected: dict[str, Any], current: dict[str, Any]
) -> bool:
    return authority_descriptor_mismatch(expected, current) is None


def _recv_exact(
    sock: socket.socket, size: int, buffered: bytearray | None = None
) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    if buffered:
        take = min(len(buffered), remaining)
        chunks.append(bytes(buffered[:take]))
        del buffered[:take]
        remaining -= take
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AppServerError("app-server WebSocket closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_frame(sock: socket.socket, payload: bytes | str, opcode: int = 1) -> None:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _recv_frame(
    sock: socket.socket, buffered: bytearray | None = None
) -> tuple[int, bool, bytes]:
    first, second = _recv_exact(sock, 2, buffered)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2, buffered))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8, buffered))[0]
    mask = _recv_exact(sock, 4, buffered) if second & 0x80 else None
    payload = _recv_exact(sock, length, buffered)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, fin, payload


class AppServerClient:
    def __init__(
        self,
        endpoint: str | os.PathLike[str],
        timeout_seconds: float = 10.0,
        *,
        bearer_token_env: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.endpoint = parse_app_server_endpoint(endpoint)
        # Kept as a convenience for existing Unix-only callers. Network
        # endpoints deliberately expose no pretend filesystem path.
        self.socket_path = self.endpoint.socket_path
        self.timeout_seconds = timeout_seconds
        self.bearer_token_env = bearer_token_env
        self._ssl_context = ssl_context
        self.sock: socket.socket | None = None
        self._next_id = 1
        self._receive_buffer = bytearray()
        self._connected_endpoint_fingerprint: str | None = None
        self.notifications: list[dict[str, Any]] = []
        self.initialize_response: dict[str, Any] | None = None
        self.remote_control_status: dict[str, Any] | None = None

    def __enter__(self) -> "AppServerClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self.sock is not None:
            return
        endpoint = self.endpoint
        authorization: str | None = None
        if self.bearer_token_env:
            token = os.environ.get(self.bearer_token_env)
            if not token:
                raise AppServerError(
                    "app-server bearer-token environment variable is unset or empty: "
                    f"{self.bearer_token_env}"
                )
            if "\r" in token or "\n" in token:
                raise AppServerError("app-server bearer token contains a newline")
            try:
                authorization = f"Bearer {token}"
                authorization.encode("ascii")
            except UnicodeEncodeError as error:
                raise AppServerError("app-server bearer token is not ASCII") from error

        endpoint_fingerprint: str | None = None
        if endpoint.transport == "unix":
            assert endpoint.socket_path is not None
            if not endpoint.socket_path.exists():
                raise AppServerError(
                    f"app-server socket does not exist: {endpoint.socket_path}"
                )
            endpoint_fingerprint = unix_socket_fingerprint(endpoint.socket_path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout_seconds)
        else:
            assert endpoint.host is not None and endpoint.port is not None
            try:
                sock = socket.create_connection(
                    (endpoint.host, endpoint.port), timeout=self.timeout_seconds
                )
            except OSError as error:
                raise AppServerError(
                    f"unable to connect to app-server endpoint {endpoint.uri}: {error}"
                ) from error
            if endpoint.transport == "wss":
                context = self._ssl_context or ssl.create_default_context()
                try:
                    sock = context.wrap_socket(sock, server_hostname=endpoint.host)
                except Exception:
                    sock.close()
                    raise

        try:
            if endpoint.transport == "unix":
                assert endpoint.socket_path is not None
                sock.connect(str(endpoint.socket_path))
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request_headers = [
                f"GET {endpoint.request_target} HTTP/1.1",
                f"Host: {endpoint.host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if authorization is not None:
                request_headers.append(f"Authorization: {authorization}")
            request = "\r\n".join(request_headers) + "\r\n\r\n"
            sock.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise AppServerError("app-server closed during WebSocket handshake")
                response.extend(chunk)
                if len(response) > 65536:
                    raise AppServerError("app-server returned an oversized WebSocket handshake")
            header, remainder = bytes(response).split(b"\r\n\r\n", 1)
            if not header.startswith(b"HTTP/1.1 101"):
                raise AppServerError(header.decode("utf-8", "replace"))
            expected = base64.b64encode(
                hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
            ).decode("ascii")
            headers = header.decode("ascii", "replace").split("\r\n")[1:]
            parsed_headers = {
                name.strip().lower(): value.strip()
                for line in headers
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            if parsed_headers.get("sec-websocket-accept") != expected:
                raise AppServerError("invalid Sec-WebSocket-Accept from app-server")
            if endpoint.transport == "unix":
                assert endpoint.socket_path is not None
                if unix_socket_fingerprint(endpoint.socket_path) != endpoint_fingerprint:
                    raise AppServerError(
                        "app-server Unix socket changed while the client connected"
                    )
        except Exception:
            sock.close()
            raise
        self.sock = sock
        self._connected_endpoint_fingerprint = endpoint_fingerprint
        self._receive_buffer.extend(remainder)
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            _send_frame(sock, b"", opcode=8)
        except OSError:
            pass
        sock.close()
        self._receive_buffer.clear()
        self._connected_endpoint_fingerprint = None

    def _initialize(self) -> None:
        self.initialize_response = self.request(
            "initialize",
            {
                "clientInfo": {
                    # Codex treats this official secondary-client name as
                    # non-originating.  A watcher must not overwrite the
                    # owning Terminal/Desktop client's process-global
                    # originator or User-Agent suffix merely by probing it.
                    "name": "codex_app_server_daemon",
                    "title": "Codex App Server Daemon",
                    "version": "2.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.sock is None:
            raise AppServerError("app-server client is not connected")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        _send_frame(self.sock, json.dumps(payload, separators=(",", ":")))

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if self.sock is None:
            raise AppServerError("app-server client is not connected")
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        _send_frame(self.sock, json.dumps(payload, separators=(",", ":")))
        while True:
            message = self.recv_message()
            if on_message is not None:
                on_message(message)
            if message.get("id") != request_id:
                self.notifications.append(message)
                continue
            if "error" in message:
                raise AppServerRpcError(method, message["error"])
            result = message.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def recv_message(self) -> dict[str, Any]:
        """Receive the next complete JSON-RPC message from the app-server."""

        if self.sock is None:
            raise AppServerError("app-server client is not connected")
        fragments = bytearray()
        data_opcode: int | None = None
        while True:
            opcode, fin, payload = _recv_frame(self.sock, self._receive_buffer)
            if opcode == 8:
                raise AppServerError("app-server closed the WebSocket")
            if opcode == 9:
                _send_frame(self.sock, payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                data_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0 and data_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if not fin:
                continue
            if data_opcode != 1:
                fragments.clear()
                data_opcode = None
                continue
            try:
                value = json.loads(bytes(fragments).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AppServerError(f"invalid JSON from app-server: {error}") from error
            if not isinstance(value, dict):
                raise AppServerError("app-server JSON-RPC message was not an object")
            return value

    def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Wait for one matching message while retaining unrelated messages."""

        if self.sock is None:
            raise AppServerError("app-server client is not connected")
        for index, message in enumerate(self.notifications):
            if predicate(message):
                return self.notifications.pop(index)

        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        previous_timeout = self.sock.gettimeout()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerTimeoutError(
                        f"timed out after {timeout_seconds:g}s waiting for app-server message"
                    )
                self.sock.settimeout(remaining)
                try:
                    message = self.recv_message()
                except (socket.timeout, TimeoutError) as error:
                    raise AppServerTimeoutError(
                        f"timed out after {timeout_seconds:g}s waiting for app-server message"
                    ) from error
                if predicate(message):
                    return message
                self.notifications.append(message)
        finally:
            if self.sock is not None:
                self.sock.settimeout(previous_timeout)

    def authority_descriptor(self) -> dict[str, Any]:
        """Describe the exact app-server authority behind this connection."""

        if self.sock is None or self.initialize_response is None:
            raise AppServerError("app-server client is not connected and initialized")

        endpoint_fingerprint: str | None = None
        if self.endpoint.transport == "unix":
            endpoint_fingerprint = self._connected_endpoint_fingerprint
            if endpoint_fingerprint is None:
                raise AppServerError("connected Unix authority has no endpoint fingerprint")

        remote_control: dict[str, Any] | None
        try:
            remote_control = self.request("remoteControl/status/read", {})
            self.remote_control_status = remote_control
        except AppServerRpcError:
            remote_control = None
            self.remote_control_status = None

        initialize = self.initialize_response
        return {
            "endpoint": self.endpoint.uri,
            "transport": self.endpoint.transport,
            "endpoint_fingerprint": endpoint_fingerprint,
            "initialize": {
                "userAgent": initialize.get("userAgent"),
                "codexHome": initialize.get("codexHome"),
                "platformFamily": initialize.get("platformFamily"),
                "platformOs": initialize.get("platformOs"),
            },
            "remote_control": remote_control,
        }

    def loaded_thread_ids(self) -> set[str]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        loaded: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("thread/loaded/list", params)
            data = result.get("data")
            if not isinstance(data, list):
                raise AppServerError("thread/loaded/list returned non-array data")
            for thread_id in data:
                if not isinstance(thread_id, str) or not thread_id:
                    raise AppServerError(
                        "thread/loaded/list returned a non-string thread id"
                    )
                loaded.add(thread_id)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return loaded
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppServerError(
                    "thread/loaded/list returned an invalid nextCursor"
                )
            if next_cursor in seen_cursors:
                raise AppServerError(
                    "thread/loaded/list returned a repeated nextCursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def read_thread(self, thread_id: str, include_turns: bool = True) -> dict[str, Any]:
        result = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": bool(include_turns)},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError(f"thread/read returned no thread for {thread_id}")
        if str(thread.get("id")) != thread_id:
            raise AppServerError(
                f"thread/read identity mismatch: expected {thread_id}, got {thread.get('id')}"
            )
        return thread

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        items_view: str = "summary",
        limit: int = 100,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return every stored turn without resuming or subscribing to a thread.

        Paginated-history threads reject ``thread/read(includeTurns=true)``.
        ``thread/turns/list`` is therefore the only safe history reader for
        those threads.  By default every cursor is followed so reconciliation
        can find an old event; ``max_pages=1`` gives live probes a bounded view
        of the newest descending page.
        """

        if items_view not in THREAD_TURNS_ITEMS_VIEWS:
            raise AppServerError(
                f"unsupported thread/turns/list itemsView {items_view!r}"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise AppServerError("thread/turns/list limit must be a positive integer")
        if (
            max_pages is not None
            and (
                isinstance(max_pages, bool)
                or not isinstance(max_pages, int)
                or max_pages < 1
            )
        ):
            raise AppServerError("thread/turns/list max_pages must be a positive integer")

        cursor: str | None = None
        page_count = 0
        seen_cursors: set[str] = set()
        turns: list[dict[str, Any]] = []
        seen_turn_ids: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": limit,
                "sortDirection": "desc",
                "itemsView": items_view,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = self.request("thread/turns/list", params)
            page_count += 1
            data = result.get("data")
            if not isinstance(data, list):
                raise AppServerError("thread/turns/list returned non-array data")
            for turn in data:
                if not isinstance(turn, dict):
                    raise AppServerError(
                        "thread/turns/list returned a non-object turn"
                    )
                turn_id = turn.get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise AppServerError(
                        "thread/turns/list returned a turn without a string id"
                    )
                if turn_id in seen_turn_ids:
                    raise AppServerError(
                        f"thread/turns/list repeated turn id {turn_id!r}"
                    )
                seen_turn_ids.add(turn_id)
                turns.append(turn)

            next_cursor = result.get("nextCursor")
            if next_cursor is None or (
                max_pages is not None and page_count >= max_pages
            ):
                return turns
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppServerError(
                    "thread/turns/list returned an invalid nextCursor"
                )
            if next_cursor in seen_cursors:
                raise AppServerError(
                    "thread/turns/list returned a repeated nextCursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def turn_start(self, thread_id: str, text: str, event_id: str) -> str:
        """Insert a native user message and return the accepted turn id."""

        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": event_id,
                "input": [
                    {
                        "type": "text",
                        "text": text,
                        "textElements": [],
                    }
                ],
            },
        )
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not turn_id:
            raise AppServerError("turn/start returned no turn id")
        return str(turn_id)

    def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        event_id: str,
    ) -> str:
        """Synchronously queue input into one exact active regular turn."""

        result = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": expected_turn_id,
                "clientUserMessageId": event_id,
                "input": [
                    {
                        "type": "text",
                        "text": text,
                        "textElements": [],
                    }
                ],
            },
        )
        turn_id = result.get("turnId")
        if not turn_id:
            raise AppServerError("turn/steer returned no turn id")
        if str(turn_id) != expected_turn_id:
            raise AppServerError(
                "turn/steer response did not match expected active turn id"
            )
        return str(turn_id)

def inspect_native_thread(
    endpoint: str | os.PathLike[str],
    thread_id: str,
    *,
    bearer_token_env: str | None = None,
) -> dict[str, Any]:
    """Return a conservative native-message capability report."""

    try:
        parsed_endpoint = parse_app_server_endpoint(endpoint)
        endpoint_uri = parsed_endpoint.uri
        transport = parsed_endpoint.transport
    except AppServerError as error:
        return {
            "endpoint": os.fspath(endpoint),
            "thread_id": thread_id,
            "reachable": False,
            "loaded": False,
            "native_message_ready": False,
            "error": str(error),
        }

    report: dict[str, Any] = {
        "endpoint": endpoint_uri,
        "transport": transport,
        "thread_id": thread_id,
        "reachable": False,
        "loaded": False,
        "native_message_ready": False,
    }
    try:
        with AppServerClient(
            parsed_endpoint.uri, bearer_token_env=bearer_token_env
        ) as client:
            report["reachable"] = True
            report["authority"] = client.authority_descriptor()
            listed_ids = client.loaded_thread_ids()
            report["listed_loaded"] = thread_id in listed_ids
            thread = client.read_thread(thread_id, include_turns=False)
            report["thread_status"] = thread.get("status")
            status = thread.get("status")
            status_type = status.get("type") if isinstance(status, dict) else None
            # Accept only positive loaded evidence. Missing or newly introduced
            # status variants fail closed instead of being mistaken for loaded.
            report["loaded"] = bool(
                report["listed_loaded"]
                or status_type in LOADED_THREAD_STATUS_TYPES
            )
            report["thread_source"] = thread.get("source")
            report["session_id"] = thread.get("sessionId")
            report["forked_from_id"] = thread.get("forkedFromId")
            report["parent_thread_id"] = thread.get("parentThreadId")
            report["ephemeral"] = bool(thread.get("ephemeral"))
            report["history_mode"] = thread.get("historyMode")
    except (AppServerError, OSError) as error:
        report["error"] = str(error)
    report["native_message_ready"] = bool(
        report.get("reachable")
        and report.get("loaded")
        and isinstance(report.get("authority"), dict)
    )
    report["native_exactly_once_ready"] = False
    report["native_exactly_once_gap"] = (
        "turn/start exposes clientUserMessageId for confirmation but no documented "
        "server-side idempotency contract for repeated event ids"
    )
    return report
