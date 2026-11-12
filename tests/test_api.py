"""Tests for :mod:`pic2tex.api`.

Stdlib ``unittest`` only.  No real network and no API key are required: every
test talks to a programmable ``ThreadingHTTPServer`` bound to 127.0.0.1:0.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from pic2tex.api import DeepSeekError, DeepSeekVisionClient, VisionResult
from pic2tex.models import ImagePayload

API_KEY = "sk-test-secret-key-1234567890"
# 1x1 PNG; the exact pixels do not matter, only byte round-tripping does.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
IMAGE = ImagePayload(data=PNG_BYTES, mime="image/png", width=1, height=1)


class AbortConnection(Exception):
    """Raised by a canned reply to drop the connection without responding."""


@dataclass
class Reply:
    """One canned HTTP response (optionally written in several flushes)."""

    status: int = 200
    body: bytes = b"{}"
    headers: dict[str, str] = field(default_factory=dict)
    parts: list[bytes] | None = None


class MockServer:
    """Programmable local HTTP server for the API client tests."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.reply: Callable[[dict], Reply] | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def finish(self) -> None:
                try:
                    super().finish()
                except OSError:
                    pass

            def _handle(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length) if length else b""
                    record = {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "headers_lower": {
                            key.lower(): value for key, value in self.headers.items()
                        },
                        "body": body,
                    }
                    owner.requests.append(record)
                    try:
                        reply = owner.reply(record) if owner.reply is not None else Reply()
                    except AbortConnection:
                        self.close_connection = True
                        try:
                            self.connection.close()
                        except OSError:
                            pass
                        return
                    parts = reply.parts if reply.parts is not None else [reply.body]
                    self.send_response(reply.status)
                    headers = dict(reply.headers)
                    headers.setdefault("Content-Type", "application/json")
                    headers.setdefault(
                        "Content-Length", str(sum(len(part) for part in parts))
                    )
                    headers.setdefault("Connection", "close")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    for part in parts:
                        self.wfile.write(part)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request: object, client_address: object) -> None:
                import traceback

                owner.errors.append(traceback.format_exc())

        self.httpd = Server(("127.0.0.1", 0), Handler)
        # Do not wait for slow/aborted handler threads during teardown.
        self.httpd.block_on_close = False
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            args=(0.01,),
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5.0)


# ----------------------------------------------------------------------
# Canned response helpers
# ----------------------------------------------------------------------
def json_reply(payload: dict, status: int = 200, headers: dict | None = None) -> Reply:
    extra = {"Content-Type": "application/json"}
    if headers:
        extra.update(headers)
    return Reply(status=status, body=json.dumps(payload).encode("utf-8"), headers=extra)


def completion_reply(
    content: object,
    *,
    request_id: str = "chatcmpl-test",
    model: str = "deepseek-flash",
    usage: dict | None = None,
) -> Reply:
    payload: dict = {
        "id": request_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return json_reply(payload)


def sse_line(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return f"data: {data}\n\n".encode("utf-8")


def abort_reply(record: dict) -> Reply:
    """Canned reply handler that resets the connection instead of answering."""
    raise AbortConnection()


class ApiTestCase(unittest.TestCase):
    """Base class giving each test a fresh mock server and client factory."""

    def setUp(self) -> None:
        self.server = MockServer()
        self.addCleanup(self.server.close)

    def make_client(self, **kwargs: object) -> DeepSeekVisionClient:
        kwargs.setdefault("base_url", self.server.base_url)
        # Retries are instant unless a test asks for real (short) sleeping.
        kwargs.setdefault("sleep", lambda _seconds: None)
        return DeepSeekVisionClient(API_KEY, **kwargs)  # type: ignore[arg-type]

    def assert_api_error(self, kind: str) -> DeepSeekError:
        with self.assertRaises(DeepSeekError) as caught:
            self.make_client().convert(IMAGE, "convert please")
        self.assertEqual(caught.exception.kind, kind)
        return caught.exception


class ConvertSuccessTests(ApiTestCase):
    def test_success_sends_expected_request_and_parses_result(self) -> None:
        usage = {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
        self.server.reply = lambda record: completion_reply(
            "\\frac{a}{b}", request_id="chatcmpl-abc", usage=usage
        )

        result = self.make_client().convert(
            IMAGE,
            "Convert this formula to LaTeX",
            max_tokens=1234,
            temperature=0.25,
        )

        self.assertIsInstance(result, VisionResult)
        self.assertEqual(result.text, "\\frac{a}{b}")
        self.assertEqual(result.model, "deepseek-flash")
        self.assertEqual(result.usage, usage)
        self.assertEqual(result.request_id, "chatcmpl-abc")
        self.assertGreaterEqual(result.latency_ms, 0.0)

        self.assertEqual(len(self.server.requests), 1)
        record = self.server.requests[0]
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["path"], "/chat/completions")
        headers = record["headers_lower"]
        self.assertEqual(headers["authorization"], f"Bearer {API_KEY}")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")

        payload = json.loads(record["body"])
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["max_tokens"], 1234)
        self.assertEqual(payload["temperature"], 0.25)
        self.assertIs(payload["stream"], False)
        self.assertEqual(len(payload["messages"]), 1)
        message = payload["messages"][0]
        self.assertEqual(message["role"], "user")

        text_block, image_block = message["content"]
        self.assertEqual(text_block["type"], "text")
        self.assertEqual(text_block["text"], "Convert this formula to LaTeX")
        self.assertEqual(image_block["type"], "image_url")
        self.assertEqual(image_block["image_url"]["detail"], "original")
        data_url = image_block["image_url"]["url"]
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), PNG_BYTES)

    def test_custom_detail_is_forwarded(self) -> None:
        self.server.reply = lambda record: completion_reply("x")
        self.make_client().convert(IMAGE, "hi", detail="high")
        payload = json.loads(self.server.requests[0]["body"])
        image_block = payload["messages"][0]["content"][1]
        self.assertEqual(image_block["image_url"]["detail"], "high")

    def test_content_parts_are_concatenated(self) -> None:
        body = {
            "id": "chatcmpl-parts",
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "\\alpha"},
                            {"type": "text", "text": "+\\beta"},
                        ],
                    }
                }
            ],
        }
        self.server.reply = lambda record: json_reply(body)
        result = self.make_client().convert(IMAGE, "hi")
        self.assertEqual(result.text, "\\alpha+\\beta")

    def test_missing_api_key_is_config_error_without_network(self) -> None:
        client = DeepSeekVisionClient(
            "", base_url=self.server.base_url, sleep=lambda _seconds: None
        )
        with self.assertRaises(DeepSeekError) as caught:
            client.convert(IMAGE, "hi")
        self.assertEqual(caught.exception.kind, "config")
        self.assertEqual(self.server.requests, [])

    def test_invalid_detail_is_bad_request(self) -> None:
        with self.assertRaises(DeepSeekError) as caught:
            self.make_client().convert(IMAGE, "hi", detail="huge")
        self.assertEqual(caught.exception.kind, "bad_request")
        self.assertEqual(self.server.requests, [])


class ErrorMappingTests(ApiTestCase):
    def test_401_maps_to_auth_and_redacts_key(self) -> None:
        body = {
            "error": {
                "message": f"Authentication Fails (invalid key {API_KEY})",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        }
        self.server.reply = lambda record: json_reply(body, status=401)

        error = self.assert_api_error("auth")

        self.assertEqual(error.status_code, 401)
        self.assertNotIn(API_KEY, str(error))
        self.assertIn("API Key 无效或无权限", str(error))
        self.assertEqual(len(self.server.requests), 1)  # never retried

    def test_400_is_not_retried(self) -> None:
        self.server.reply = lambda record: json_reply(
            {"error": {"message": "image is invalid", "type": "invalid_request_error"}},
            status=400,
        )
        error = self.assert_api_error("bad_request")
        self.assertEqual(error.status_code, 400)
        self.assertEqual(len(self.server.requests), 1)

    def test_malformed_json_is_invalid_response(self) -> None:
        self.server.reply = lambda record: Reply(
            status=200, body=b"{ not really json", headers={"Content-Type": "application/json"}
        )
        self.assert_api_error("invalid_response")

    def test_empty_content_is_invalid_response(self) -> None:
        self.server.reply = lambda record: completion_reply("")
        self.assert_api_error("invalid_response")

    def test_streaming_empty_content_is_invalid_response(self) -> None:
        self.server.reply = lambda record: Reply(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            parts=[b"data: [DONE]\n\n"],
        )
        with self.assertRaises(DeepSeekError) as caught:
            self.make_client().convert(IMAGE, "hi", stream=True)
        self.assertEqual(caught.exception.kind, "invalid_response")

    def test_streaming_malformed_json_is_invalid_response(self) -> None:
        self.server.reply = lambda record: Reply(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            parts=[b"data: {broken\n\n"],
        )
        with self.assertRaises(DeepSeekError) as caught:
            self.make_client().convert(IMAGE, "hi", stream=True)
        self.assertEqual(caught.exception.kind, "invalid_response")

    def test_200_json_error_while_streaming_is_mapped(self) -> None:
        self.server.reply = lambda record: json_reply(
            {"error": {"message": "model overloaded", "type": "server_error"}}
        )
        with self.assertRaises(DeepSeekError) as caught:
            self.make_client().convert(IMAGE, "hi", stream=True)
        self.assertEqual(caught.exception.kind, "server")

    def test_network_error_is_wrapped(self) -> None:
        self.server.reply = abort_reply
        client = self.make_client(timeout=2.0, max_retries=0)
        with self.assertRaises(DeepSeekError) as caught:
            client.convert(IMAGE, "hi")
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("无法连接", str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_timeout_is_retried_once_then_raises_timeout(self) -> None:
        def slow_reply(record: dict) -> Reply:
            time.sleep(0.4)
            return completion_reply("too late")

        self.server.reply = slow_reply
        client = self.make_client(timeout=0.1, max_retries=2)
        with self.assertRaises(DeepSeekError) as caught:
            client.convert(IMAGE, "hi")
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(len(self.server.requests), 2)  # exactly one retry


class RetryTests(ApiTestCase):
    def test_429_then_success_retries_once(self) -> None:
        calls = {"count": 0}

        def reply(record: dict) -> Reply:
            calls["count"] += 1
            if calls["count"] == 1:
                return json_reply(
                    {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
                    status=429,
                )
            return completion_reply("ok")

        self.server.reply = reply
        started = time.perf_counter()
        result = self.make_client(retry_base=0.01, sleep=time.sleep).convert(IMAGE, "hi")
        elapsed = time.perf_counter() - started

        self.assertEqual(result.text, "ok")
        self.assertEqual(calls["count"], 2)
        self.assertEqual(len(self.server.requests), 2)
        self.assertLess(elapsed, 5.0)

    def test_retry_after_header_is_honored(self) -> None:
        delays: list[float] = []
        calls = {"count": 0}

        def reply(record: dict) -> Reply:
            calls["count"] += 1
            if calls["count"] == 1:
                return json_reply(
                    {"error": {"message": "slow down"}},
                    status=429,
                    headers={"Retry-After": "2"},
                )
            return completion_reply("ok")

        self.server.reply = reply
        result = self.make_client(sleep=delays.append).convert(IMAGE, "hi")

        self.assertEqual(result.text, "ok")
        self.assertEqual(delays, [2.0])

    def test_500_exhausts_retries(self) -> None:
        self.server.reply = lambda record: json_reply(
            {"error": {"message": "boom", "type": "server_error"}}, status=500
        )
        client = self.make_client(max_retries=1)
        with self.assertRaises(DeepSeekError) as caught:
            client.convert(IMAGE, "hi")
        self.assertEqual(caught.exception.kind, "server")
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(len(self.server.requests), 2)  # 1 initial + 1 retry


class StreamingTests(ApiTestCase):
    def test_streaming_yields_deltas_in_order(self) -> None:
        chunks: list[dict] = [
            {
                "id": "chatcmpl-stream",
                "model": "deepseek-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant"}}],
            },
            {"choices": [{"index": 0, "delta": {"content": "\\frac"}}]},
            {"choices": [{"index": 0, "delta": {"content": "{a}"}}]},
            {"choices": [{"index": 0, "delta": {"content": "{b}"}}]},
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        ]
        parts: list[bytes] = [b": keep-alive comment\n\n", b"\n"]
        parts.extend(sse_line(chunk) for chunk in chunks)
        parts.append(b"data: [DONE]\n\n")

        self.server.reply = lambda record: Reply(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            parts=parts,
        )

        deltas: list[str] = []
        result = self.make_client().convert(
            IMAGE, "stream please", stream=True, on_delta=deltas.append
        )

        self.assertEqual(deltas, ["\\frac", "{a}", "{b}"])
        self.assertEqual(result.text, "\\frac{a}{b}")
        self.assertEqual(result.request_id, "chatcmpl-stream")
        self.assertEqual(result.model, "deepseek-flash")
        self.assertEqual(result.usage["total_tokens"], 13)

        record = self.server.requests[0]
        self.assertEqual(record["headers_lower"]["accept"], "text/event-stream")
        payload = json.loads(record["body"])
        self.assertIs(payload["stream"], True)

    def test_streaming_without_callback_still_returns_text(self) -> None:
        self.server.reply = lambda record: Reply(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            parts=[sse_line({"choices": [{"delta": {"content": "abc"}}]}), b"data: [DONE]\n\n"],
        )
        result = self.make_client().convert(IMAGE, "hi", stream=True)
        self.assertEqual(result.text, "abc")

    def test_streaming_request_answered_with_json_completion(self) -> None:
        self.server.reply = lambda record: completion_reply("plain", request_id="req-json")
        result = self.make_client().convert(IMAGE, "hi", stream=True)
        self.assertEqual(result.text, "plain")
        self.assertEqual(result.request_id, "req-json")


class BaseUrlTests(ApiTestCase):
    def test_v1_suffix_maps_to_v1_chat_completions(self) -> None:
        self.server.reply = lambda record: completion_reply("ok")
        client = DeepSeekVisionClient(
            API_KEY,
            base_url=self.server.base_url + "/v1",
            sleep=lambda _seconds: None,
        )
        client.convert(IMAGE, "hi")
        self.assertEqual(self.server.requests[0]["path"], "/v1/chat/completions")

    def test_full_chat_completions_url_is_not_duplicated(self) -> None:
        self.server.reply = lambda record: completion_reply("ok")
        client = DeepSeekVisionClient(
            API_KEY,
            base_url=self.server.base_url + "/chat/completions",
            sleep=lambda _seconds: None,
        )
        client.convert(IMAGE, "hi")
        self.assertEqual(self.server.requests[0]["path"], "/chat/completions")


class CheckConnectionTests(ApiTestCase):
    def test_200_reports_success_and_model_info(self) -> None:
        self.server.reply = lambda record: json_reply(
            {
                "object": "list",
                "data": [{"id": "deepseek-flash"}, {"id": "deepseek-chat"}],
            }
        )
        ok, message = self.make_client().check_connection()

        self.assertTrue(ok)
        self.assertIn("连接成功", message)
        self.assertIn("ms", message)
        self.assertIn("deepseek-flash", message)

        record = self.server.requests[0]
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/models")
        self.assertEqual(
            record["headers_lower"]["authorization"], f"Bearer {API_KEY}"
        )

    def test_401_reports_invalid_key(self) -> None:
        self.server.reply = lambda record: json_reply(
            {"error": {"message": "invalid api key"}}, status=401
        )
        ok, message = self.make_client().check_connection()
        self.assertFalse(ok)
        self.assertEqual(message, "API Key 无效或无权限")

    def test_404_suggests_base_url(self) -> None:
        self.server.reply = lambda record: json_reply(
            {"error": {"message": "not found"}}, status=404
        )
        ok, message = self.make_client().check_connection()
        self.assertFalse(ok)
        self.assertIn("Base URL", message)

    def test_network_failure_never_raises(self) -> None:
        self.server.reply = abort_reply
        client = self.make_client(timeout=2.0)
        ok, message = client.check_connection()
        self.assertFalse(ok)
        self.assertIn("无法连接", message)
        self.assertNotIn(API_KEY, message)


if __name__ == "__main__":
    unittest.main()
