from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "blocking-wait-handoff" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import codex_app_server as app_server  # noqa: E402


def send_server_json(connection: socket.socket, message: dict[str, object]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", len(payload)))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", len(payload)))
    connection.sendall(bytes(header) + payload)


class FakeWebSocketAppServer:
    def __init__(
        self,
        *,
        unix_path: Path | None = None,
        remote_status: bool = True,
        loaded_thread_ids: list[str] | None = None,
        thread_status: dict[str, object] | None = None,
        history_mode: str = "legacy",
        turn_pages: list[list[dict[str, object]]] | None = None,
    ):
        self.unix_path = unix_path
        self.remote_status = remote_status
        self.loaded_thread_ids = (
            list(loaded_thread_ids)
            if loaded_thread_ids is not None
            else ["thread-123"]
        )
        self.thread_status = (
            dict(thread_status)
            if thread_status is not None
            else {"type": "idle"}
        )
        self.history_mode = history_mode
        self.turn_pages = copy.deepcopy(turn_pages)
        family = socket.AF_UNIX if unix_path is not None else socket.AF_INET
        self.listener = socket.socket(family, socket.SOCK_STREAM)
        if unix_path is not None:
            self.listener.bind(str(unix_path))
            self.endpoint = f"unix://{unix_path}"
        else:
            self.listener.bind(("127.0.0.1", 0))
            port = self.listener.getsockname()[1]
            self.endpoint = f"ws://127.0.0.1:{port}/rpc?source=test"
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.request_headers: dict[str, str] = {}
        self.request_target: str | None = None
        self.messages: list[dict[str, object]] = []
        self.persisted_client_ids: list[str] = []
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeWebSocketAppServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.listener.close()
        self.thread.join(timeout=2)
        if exc_type is None and self.error is not None:
            raise self.error

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(2)
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                header_bytes, remainder = bytes(request).split(b"\r\n\r\n", 1)
                if remainder:
                    raise AssertionError("client sent a frame with the HTTP handshake")
                lines = header_bytes.decode("ascii").split("\r\n")
                _, self.request_target, _ = lines[0].split(" ", 2)
                self.request_headers = {
                    name.strip().lower(): value.strip()
                    for line in lines[1:]
                    if ":" in line
                    for name, value in [line.split(":", 1)]
                }
                key = self.request_headers["sec-websocket-key"]
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + app_server.WEBSOCKET_GUID).encode("ascii")
                    ).digest()
                ).decode("ascii")
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )

                while True:
                    opcode, _, payload = app_server._recv_frame(connection)
                    if opcode == 8:
                        return
                    if opcode != 1:
                        continue
                    message = json.loads(payload.decode("utf-8"))
                    self.messages.append(message)
                    method = message.get("method")
                    request_id = message.get("id")
                    if method == "initialize":
                        send_server_json(
                            connection,
                            {
                                "id": request_id,
                                "result": {
                                    "userAgent": "codex-cli/0.test",
                                    "codexHome": "/srv/codex-home",
                                    "platformFamily": "unix",
                                    "platformOs": "linux",
                                },
                            },
                        )
                    elif method == "remoteControl/status/read":
                        if self.remote_status:
                            send_server_json(
                                connection,
                                {
                                    "id": request_id,
                                    "result": {
                                        "status": "connected",
                                        "serverName": "fake-server",
                                        "installationId": "install-123",
                                        "environmentId": "env-456",
                                    },
                                },
                            )
                        else:
                            send_server_json(
                                connection,
                                {
                                    "id": request_id,
                                    "error": {"code": -32601, "message": "not supported"},
                                },
                            )
                    elif method == "thread/loaded/list":
                        send_server_json(
                            connection,
                            {
                                "id": request_id,
                                "result": {
                                    # Current app-server protocol returns Vec<String>,
                                    # not thread summary objects.
                                    "data": self.loaded_thread_ids,
                                    "nextCursor": None,
                                },
                            },
                        )
                    elif method == "thread/read":
                        include_turns = bool(message["params"].get("includeTurns"))
                        thread: dict[str, object] = {
                            "id": message["params"]["threadId"],
                            "historyMode": self.history_mode,
                            "status": self.thread_status,
                        }
                        if include_turns:
                            if self.history_mode == "paginated":
                                send_server_json(
                                    connection,
                                    {
                                        "id": request_id,
                                        "error": {
                                            "code": -32602,
                                            "message": (
                                                "paginated threads do not support "
                                                "thread/read(includeTurns=true)"
                                            ),
                                        },
                                    },
                                )
                                continue
                            thread["turns"] = [
                                {
                                    "id": "turn-789",
                                    "status": "completed",
                                    "items": [
                                        {
                                            "type": "userMessage",
                                            "clientId": client_id,
                                        }
                                        for client_id in self.persisted_client_ids
                                    ],
                                }
                            ]
                        send_server_json(
                            connection,
                            {
                                "id": request_id,
                                "result": {"thread": thread},
                            },
                        )
                    elif method == "thread/turns/list":
                        pages = self.turn_pages
                        if pages is None:
                            pages = [
                                [
                                    {
                                        "id": "turn-789",
                                        "status": "completed",
                                        "items": [
                                            {
                                                "type": "userMessage",
                                                "clientId": client_id,
                                            }
                                            for client_id in self.persisted_client_ids
                                        ],
                                    }
                                ]
                            ]
                        cursor = message["params"].get("cursor")
                        page_index = int(str(cursor).removeprefix("page-")) if cursor else 0
                        next_cursor = (
                            f"page-{page_index + 1}"
                            if page_index + 1 < len(pages)
                            else None
                        )
                        send_server_json(
                            connection,
                            {
                                "id": request_id,
                                "result": {
                                    "data": pages[page_index],
                                    "nextCursor": next_cursor,
                                    "backwardsCursor": None,
                                },
                            },
                        )
                    elif method == "turn/start":
                        event_id = message["params"]["clientUserMessageId"]
                        self.persisted_client_ids.append(event_id)
                        send_server_json(
                            connection,
                            {"id": request_id, "result": {"turn": {"id": "turn-789"}}},
                        )
                    elif method == "turn/steer":
                        event_id = message["params"]["clientUserMessageId"]
                        self.persisted_client_ids.append(event_id)
                        send_server_json(
                            connection,
                            {
                                "id": request_id,
                                "result": {
                                    "turnId": message["params"]["expectedTurnId"]
                                },
                            },
                        )
        except (app_server.AppServerError, OSError, TimeoutError) as error:
            # A close racing test teardown is expected. Anything earlier is
            # surfaced by the client-side assertion that failed first.
            if self.messages and self.messages[-1].get("method") == "initialized":
                return
            self.error = error
        except BaseException as error:
            self.error = error


class AppServerEndpointTests(unittest.TestCase):
    def test_endpoint_uri_parsing(self) -> None:
        unix = app_server.parse_app_server_endpoint(Path("relative.sock"))
        self.assertEqual(unix.transport, "unix")
        self.assertTrue(unix.socket_path.is_absolute())
        self.assertEqual(unix.request_target, "/rpc")

        ws = app_server.parse_app_server_endpoint("ws://127.0.0.1:4222/rpc?q=1")
        self.assertEqual(ws.uri, "ws://127.0.0.1:4222/rpc?q=1")
        self.assertEqual(
            (ws.host, ws.port, ws.request_target),
            ("127.0.0.1", 4222, "/rpc?q=1"),
        )

        wss = app_server.parse_app_server_endpoint("wss://example.com/control")
        self.assertEqual(wss.uri, "wss://example.com/control")
        self.assertEqual((wss.host, wss.port), ("example.com", 443))

        with self.assertRaisesRegex(app_server.AppServerError, "unsupported"):
            app_server.parse_app_server_endpoint("https://example.com")
        with self.assertRaisesRegex(app_server.AppServerError, "credentials"):
            app_server.parse_app_server_endpoint("ws://secret@example.com:4222")

    def test_initialize_uses_non_originating_daemon_client_info(self) -> None:
        client = app_server.AppServerClient("ws://127.0.0.1:43210/rpc")
        with (
            mock.patch.object(client, "request", return_value={}) as request,
            mock.patch.object(client, "notify") as notify,
        ):
            client._initialize()

        request.assert_called_once()
        method, params = request.call_args.args
        self.assertEqual(method, "initialize")
        self.assertEqual(
            params["clientInfo"],
            {
                "name": "codex_app_server_daemon",
                "title": "Codex App Server Daemon",
                "version": "2.0.0",
            },
        )
        notify.assert_called_once_with("initialized")

    def test_ws_authorization_turn_start_and_history_confirmation(self) -> None:
        event_id = "event-123"
        with FakeWebSocketAppServer() as server:
            with mock.patch.dict(os.environ, {"WAIT_TEST_BEARER": "top-secret"}):
                client = app_server.AppServerClient(
                    server.endpoint,
                    bearer_token_env="WAIT_TEST_BEARER",
                )
                with client:
                    self.assertEqual(
                        client.initialize_response["codexHome"], "/srv/codex-home"
                    )
                    descriptor = client.authority_descriptor()
                    self.assertEqual(descriptor["transport"], "ws")
                    self.assertIsNone(descriptor["endpoint_fingerprint"])
                    self.assertEqual(
                        descriptor["remote_control"]["environmentId"], "env-456"
                    )
                    self.assertEqual(client.loaded_thread_ids(), {"thread-123"})
                    self.assertEqual(
                        client.read_thread("thread-123", include_turns=False)["id"],
                        "thread-123",
                    )
                    self.assertEqual(
                        client.turn_start("thread-123", "continue now", event_id),
                        "turn-789",
                    )
                    history = client.read_thread(
                        "thread-123",
                        include_turns=True,
                    )
                    self.assertEqual(
                        history["turns"][0]["items"][0],
                        {"type": "userMessage", "clientId": event_id},
                    )
                    self.assertEqual(client.notifications, [])

                self.assertNotIn("top-secret", repr(client.__dict__))

            self.assertEqual(server.request_target, "/rpc?source=test")
            self.assertEqual(
                server.request_headers["authorization"], "Bearer top-secret"
            )
            turn_request = next(
                message
                for message in server.messages
                if message.get("method") == "turn/start"
            )
            self.assertEqual(
                turn_request["params"],
                {
                    "threadId": "thread-123",
                    "clientUserMessageId": event_id,
                    "input": [
                        {
                            "type": "text",
                            "text": "continue now",
                            "textElements": [],
                        }
                    ],
                },
            )
            self.assertFalse(
                any(message.get("method") == "thread/resume" for message in server.messages),
                "native delivery must never cold-load a persisted thread",
            )
            self.assertTrue(
                any(
                    message.get("method") == "thread/read"
                    and message.get("params", {}).get("includeTurns") is True
                    for message in server.messages
                ),
                "positive ACK must come from persisted owner-thread history",
            )

    def test_thread_turns_list_follows_every_paginated_history_cursor(self) -> None:
        pages = [
            [
                {
                    "id": "turn-newest",
                    "status": "completed",
                    "items": [],
                }
            ],
            [
                {
                    "id": "turn-middle",
                    "status": "completed",
                    "items": [],
                }
            ],
            [
                {
                    "id": "turn-oldest",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "clientId": "event-on-last-page",
                        }
                    ],
                }
            ],
        ]
        with FakeWebSocketAppServer(
            history_mode="paginated",
            turn_pages=pages,
        ) as server:
            with app_server.AppServerClient(server.endpoint) as client:
                thread = client.read_thread("thread-123", include_turns=False)
                self.assertEqual(thread["historyMode"], "paginated")
                turns = client.list_thread_turns(
                    "thread-123",
                    items_view="full",
                    limit=1,
                )

        self.assertEqual(
            [turn["id"] for turn in turns],
            ["turn-newest", "turn-middle", "turn-oldest"],
        )
        requests = [
            message
            for message in server.messages
            if message.get("method") == "thread/turns/list"
        ]
        self.assertEqual(len(requests), 3)
        self.assertEqual(
            [request["params"].get("cursor") for request in requests],
            [None, "page-1", "page-2"],
        )
        self.assertTrue(
            all(
                request["params"]["itemsView"] == "full"
                and request["params"]["limit"] == 1
                for request in requests
            )
        )
        self.assertFalse(
            any(
                message.get("method") == "thread/read"
                and message.get("params", {}).get("includeTurns") is True
                for message in server.messages
            )
        )

    def test_thread_turns_list_can_stop_after_the_newest_page(self) -> None:
        pages = [
            [{"id": "turn-newest", "status": "inProgress", "items": []}],
            [{"id": "turn-older", "status": "completed", "items": []}],
        ]
        with FakeWebSocketAppServer(
            history_mode="paginated",
            turn_pages=pages,
        ) as server:
            with app_server.AppServerClient(server.endpoint) as client:
                turns = client.list_thread_turns(
                    "thread-123",
                    items_view="notLoaded",
                    max_pages=1,
                )

        self.assertEqual([turn["id"] for turn in turns], ["turn-newest"])
        requests = [
            message
            for message in server.messages
            if message.get("method") == "thread/turns/list"
        ]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"]["sortDirection"], "desc")
        self.assertEqual(requests[0]["params"]["itemsView"], "notLoaded")

    def test_turn_steer_sends_expected_turn_and_client_message_ids(self) -> None:
        event_id = "event-steer-123"
        with FakeWebSocketAppServer() as server:
            with app_server.AppServerClient(server.endpoint) as client:
                self.assertEqual(
                    client.turn_steer(
                        "thread-123",
                        "active-turn-456",
                        "continue in FIFO order",
                        event_id,
                    ),
                    "active-turn-456",
                )

            steer_request = next(
                message
                for message in server.messages
                if message.get("method") == "turn/steer"
            )
            self.assertEqual(
                steer_request["params"],
                {
                    "threadId": "thread-123",
                    "expectedTurnId": "active-turn-456",
                    "clientUserMessageId": event_id,
                    "input": [
                        {
                            "type": "text",
                            "text": "continue in FIFO order",
                            "textElements": [],
                        }
                    ],
                },
            )

    def test_turn_steer_rejects_response_for_a_different_turn(self) -> None:
        client = app_server.AppServerClient("ws://127.0.0.1:43210/rpc")
        with (
            mock.patch.object(
                client,
                "request",
                return_value={"turnId": "new-or-unrelated-turn"},
            ),
            self.assertRaisesRegex(app_server.AppServerError, "expected active turn"),
        ):
            client.turn_steer(
                "thread-123",
                "expected-active-turn",
                "continue once",
                "event-steer-123",
            )

    def test_unix_inode_fingerprint_and_authority_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "app-server.sock"
            with FakeWebSocketAppServer(
                unix_path=socket_path, remote_status=False
            ) as server:
                with app_server.AppServerClient(server.endpoint) as client:
                    descriptor = client.authority_descriptor()
                    self.assertEqual(descriptor["transport"], "unix")
                    self.assertTrue(
                        descriptor["endpoint_fingerprint"].startswith("unix-inode:")
                    )
                    self.assertIsNone(descriptor["remote_control"])

                same = copy.deepcopy(descriptor)
                self.assertTrue(
                    app_server.authority_descriptors_match(descriptor, same)
                )
                changed = copy.deepcopy(descriptor)
                changed["endpoint_fingerprint"] = "unix-inode:1:2"
                self.assertEqual(
                    app_server.authority_descriptor_mismatch(descriptor, changed),
                    "endpoint fingerprint changed",
                )
                self.assertFalse(
                    app_server.authority_descriptors_match(descriptor, changed)
                )

    def test_bearer_token_is_resolved_only_at_connect_time(self) -> None:
        client = app_server.AppServerClient(
            "ws://127.0.0.1:9", bearer_token_env="MISSING_WAIT_TEST_BEARER"
        )
        self.assertEqual(client.bearer_token_env, "MISSING_WAIT_TEST_BEARER")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(app_server.AppServerError, "unset or empty"):
                client.connect()

    def test_inspect_reports_native_message_authority_without_goal_probe(self) -> None:
        with FakeWebSocketAppServer() as server:
            report = app_server.inspect_native_thread(server.endpoint, "thread-123")
        self.assertTrue(report["reachable"])
        self.assertTrue(report["loaded"])
        self.assertTrue(report["native_message_ready"])
        self.assertEqual(report["authority"]["endpoint"], server.endpoint)
        self.assertFalse(
            any("goal" in str(message.get("method")) for message in server.messages)
        )

    def test_inspect_accepts_only_list_hit_or_exact_positive_status(self) -> None:
        for status_type in ("idle", "active", "systemError"):
            with self.subTest(status_type=status_type):
                with FakeWebSocketAppServer(
                    loaded_thread_ids=[],
                    thread_status={"type": status_type},
                ) as server:
                    report = app_server.inspect_native_thread(server.endpoint, "thread-123")
                self.assertFalse(report["listed_loaded"])
                self.assertTrue(report["loaded"])
                self.assertTrue(report["native_message_ready"])

        for status in ({"type": "notLoaded"}, {"type": "futureStatus"}, {}):
            with self.subTest(status=status):
                with FakeWebSocketAppServer(
                    loaded_thread_ids=[],
                    thread_status=status,
                ) as server:
                    report = app_server.inspect_native_thread(server.endpoint, "thread-123")
                self.assertTrue(report["reachable"])
                self.assertFalse(report["listed_loaded"])
                self.assertFalse(report["loaded"])
                self.assertFalse(report["native_message_ready"])


if __name__ == "__main__":
    unittest.main()
