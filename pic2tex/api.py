"""DeepSeek vision API client for Pic2TeX.

A small, dependency-free (standard library only) client for the
OpenAI-compatible DeepSeek chat completions endpoint.  The client is safe to
construct per request, holds no global state, and never imports GUI code.

Design notes
------------
* Transport is :mod:`urllib.request` with an explicit timeout.
* Every transport/API failure surfaces as :class:`DeepSeekError` with a
  user-facing Chinese message; raw exceptions never escape :meth:`convert`.
* The API key is kept out of every error message via :meth:`_redact`.
* Retries (HTTP 429/500/502/503/504 and transient network errors) use
  exponential backoff.  A timeout is retried exactly once, then reported as
  ``kind="timeout"``.
* Streaming uses Server-Sent Events (SSE): each ``data:`` chunk is decoded and
  its ``choices[0].delta.content`` is passed to ``on_delta``.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Iterator

from .models import ImagePayload

__all__ = ["DeepSeekError", "DeepSeekVisionClient", "VisionResult"]

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_CHAT_PATH = "/chat/completions"
_MODELS_PATH = "/models"
_SUPPORTED_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_ALLOWED_DETAILS = ("low", "high", "original", "auto")
_MAX_INLINE_BYTES = 32 * 1024 * 1024
#: HTTP statuses that are worth retrying with backoff.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: Upper bound for a server supplied ``Retry-After`` hint, in seconds.
_MAX_RETRY_AFTER = 10.0
#: How many times a socket timeout is retried before raising ``kind="timeout"``.
_TIMEOUT_RETRIES = 1


def _content_to_text(content: object) -> str:
    """Flatten a chat-completion ``content`` value into plain text.

    The API normally returns a string, but OpenAI-compatible servers may return
    a list of content parts (``{"type": "text", "text": ...}``).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    return ""


def _parse_retry_after(headers: object) -> float | None:
    """Return a numeric ``Retry-After`` header value in seconds, if any."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        value = getter("Retry-After")
    except Exception:  # pragma: no cover - exotic header objects
        return None
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        # HTTP-date form is not supported; fall back to regular backoff.
        return None
    if seconds < 0:
        return None
    return seconds


@dataclass(frozen=True)
class VisionResult:
    """A successful (possibly streamed) vision completion.

    Attributes:
        text: The LaTeX text returned by the model.
        model: The model id reported by the API (or the requested one).
        usage: Token usage dict from the API, when present.
        latency_ms: Wall-clock time of the whole call, including retries.
        request_id: Server request/completion id, when present.
    """

    text: str
    model: str
    usage: dict | None = None
    latency_ms: float = 0.0
    request_id: str | None = None


class DeepSeekError(Exception):
    """A DeepSeek API failure with a user-facing (Chinese) message.

    Args:
        message: Human readable message; must never contain the API key.
        kind: One of ``auth``, ``quota``, ``rate_limit``, ``server``,
            ``network``, ``timeout``, ``bad_request``, ``invalid_response``,
            ``config`` or ``error``.
        status_code: HTTP status code when the failure came from an HTTP
            response, else ``None``.

    Attributes:
        retry_after: Optional server hint in seconds for the next retry.
            Populated internally from the ``Retry-After`` header; not part of
            the frozen interface.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


class DeepSeekVisionClient:
    """DeepSeek OpenAI-compatible vision client (stdlib only).

    Args:
        api_key: DeepSeek API key.  An empty key is accepted at construction
            time so the GUI can build the client before configuration; calls
            then fail with ``kind="config"``.
        base_url: API root, e.g. ``https://api.deepseek.com`` or
            ``https://api.deepseek.com/v1``.  ``/chat/completions`` is
            appended when missing.
        model: Vision model id.
        timeout: Per-socket-operation timeout in seconds.
        max_retries: Retries after the first attempt for retryable failures
            (total attempts are ``max_retries + 1``).  Timeouts have their own
            single-retry policy.
        retry_base: Initial backoff in seconds (test seam; documented).
        retry_max_delay: Backoff cap in seconds (test seam; documented).
        sleep: Sleep function used between retries (test seam; documented).

    The instance is immutable after construction and safe to use from a worker
    thread; it never imports tkinter.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = "deepseek-flash",
        timeout: float = 180.0,
        max_retries: int = 2,
        retry_base: float = 0.5,
        retry_max_delay: float = 8.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if retry_base <= 0 or retry_max_delay <= 0:
            raise ValueError("retry delays must be positive")

        self._api_key = str(api_key or "").strip()
        self._model = str(model or "deepseek-flash").strip() or "deepseek-flash"
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._retry_base = float(retry_base)
        self._retry_max_delay = float(retry_max_delay)
        self._sleep = sleep

        normalized = str(base_url or _DEFAULT_BASE_URL).strip() or _DEFAULT_BASE_URL
        if "://" not in normalized:
            normalized = "https://" + normalized
        root = normalized.rstrip("/")
        if root.endswith(_CHAT_PATH):
            root = root[: -len(_CHAT_PATH)].rstrip("/")
        self._root_url = root
        self._chat_url = root + _CHAT_PATH
        self._models_url = root + _MODELS_PATH

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def convert(
        self,
        image: ImagePayload,
        prompt: str,
        *,
        max_tokens: int = 8000,
        temperature: float = 0.0,
        detail: str = "original",
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> VisionResult:
        """Send one image + prompt to the model and return the LaTeX text.

        Args:
            image: Encoded PNG/JPEG/GIF/WebP payload.
            prompt: Instruction text sent next to the image.
            max_tokens: Maximum completion tokens.
            temperature: Sampling temperature in ``[0, 2]``.
            detail: Image detail hint: ``low``/``high``/``original``/``auto``
                (``None`` omits the field).
            stream: When true, the response is parsed as SSE.  A server that
                answers with a normal JSON body (including a JSON error body)
                is handled transparently.
            on_delta: Optional callback invoked for every streamed text chunk.
                Exceptions raised by the callback propagate to the caller.

        Returns:
            The completion text plus model/usage/request-id metadata.

        Raises:
            DeepSeekError: On every API, transport, timeout or protocol error.
                Retryable failures are retried first.
        """
        self._validate_inputs(image, prompt, detail, max_tokens, temperature)
        payload = self._build_payload(
            image,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            detail=detail,
            stream=stream,
        )

        delivered = False

        def emit(chunk: str) -> None:
            nonlocal delivered
            delivered = True
            if on_delta is not None:
                on_delta(chunk)

        started = time.perf_counter()
        retries = 0
        timeout_retries = 0
        while True:
            try:
                result = self._convert_once(
                    payload,
                    stream=stream,
                    on_delta=emit if stream else None,
                )
            except DeepSeekError as exc:
                if exc.kind == "timeout":
                    if timeout_retries >= _TIMEOUT_RETRIES:
                        raise
                    self._sleep(self._backoff(timeout_retries))
                    timeout_retries += 1
                    continue
                # Never retry once part of a stream has already reached the
                # caller: a retry would duplicate the partial text.
                if (
                    delivered
                    or not self._is_retryable(exc)
                    or retries >= self._max_retries
                ):
                    raise
                self._sleep(self._retry_delay(exc, retries))
                retries += 1
                continue
            latency_ms = (time.perf_counter() - started) * 1000.0
            return replace(result, latency_ms=latency_ms)

    def check_connection(self) -> tuple[bool, str]:
        """Probe the API without raising.

        Performs ``GET {base}/models`` and reports a short Chinese status
        message (including latency on success).

        Returns:
            ``(True, message)`` on HTTP 200, otherwise ``(False, message)``
            with a friendly explanation.  This method never raises.
        """
        if not self._api_key:
            return False, "未配置 DeepSeek API Key，请先在设置中填写"

        started = time.perf_counter()
        try:
            with self._open(
                url=self._models_url,
                method="GET",
                body=None,
                stream=False,
            ) as response:
                raw = self._read_body(response)
            latency_ms = (time.perf_counter() - started) * 1000.0
            message = f"连接成功（{latency_ms:.0f} ms）"
            models = self._model_ids(raw)
            if models:
                message += f"，可用模型 {len(models)} 个"
                if self._model in models:
                    message += f"，{self._model} 可用"
                else:
                    message += f"，未发现 {self._model}"
            return True, message
        except DeepSeekError as exc:
            status = exc.status_code
            if status in (401, 403) or exc.kind == "auth":
                return False, "API Key 无效或无权限"
            if status == 402 or exc.kind == "quota":
                return False, "DeepSeek 账户余额不足，请充值后重试"
            if status == 404:
                return False, "接口地址不存在（HTTP 404），请检查设置中的 Base URL 是否正确"
            if status == 429 or exc.kind == "rate_limit":
                return False, "请求过于频繁（触发限流），请稍后重试"
            if exc.kind == "timeout":
                return False, f"无法连接 DeepSeek（请求超时 {self._timeout:g} 秒）"
            if exc.kind == "network":
                return False, str(exc)
            return False, f"连接失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - diagnostic helper never raises
            return False, f"连接检查失败：{type(exc).__name__}"

    # ------------------------------------------------------------------
    # Request construction / validation
    # ------------------------------------------------------------------
    def _build_payload(
        self,
        image: ImagePayload,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        detail: str | None,
        stream: bool,
    ) -> dict:
        """Build the OpenAI-compatible chat completion request body."""
        encoded = base64.b64encode(bytes(image.data)).decode("ascii")
        image_url: dict = {"url": f"data:{image.mime};base64,{encoded}"}
        if detail is not None:
            image_url["detail"] = detail
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": image_url},
                    ],
                }
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": bool(stream),
        }

    def _validate_inputs(
        self,
        image: ImagePayload,
        prompt: str,
        detail: str | None,
        max_tokens: int,
        temperature: float,
    ) -> None:
        """Validate arguments before doing any network I/O."""
        if not self._api_key:
            raise DeepSeekError("未配置 DeepSeek API Key，请先在设置中填写", kind="config")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DeepSeekError("提示词不能为空", kind="bad_request")

        data = getattr(image, "data", None)
        mime = getattr(image, "mime", None)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise DeepSeekError("图片数据为空，无法发送", kind="bad_request")
        if mime not in _SUPPORTED_MIMES:
            raise DeepSeekError(
                f"不支持的图片格式：{mime!r}（支持 PNG / JPEG / GIF / WebP）",
                kind="bad_request",
            )
        if len(data) > _MAX_INLINE_BYTES:
            raise DeepSeekError(
                f"图片过大（{len(data) / (1024 * 1024):.1f} MiB），超过 32 MiB 内联上限",
                kind="bad_request",
            )
        if detail is not None and detail not in _ALLOWED_DETAILS:
            raise DeepSeekError(
                f"不支持的 detail 值：{detail!r}（可选 low / high / original / auto）",
                kind="bad_request",
            )
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise DeepSeekError("max_tokens 必须是正整数", kind="bad_request")
        if not isinstance(temperature, (int, float)) or not 0.0 <= float(temperature) <= 2.0:
            raise DeepSeekError("temperature 必须在 0 到 2 之间", kind="bad_request")

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------
    def _open(
        self,
        *,
        url: str,
        method: str,
        body: dict | None,
        stream: bool,
    ) -> http.client.HTTPResponse:
        """Open one HTTP request, mapping every failure to DeepSeekError."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "Pic2TeX/1.0",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise self._network_error(exc) from None
        except (TimeoutError, socket.timeout) as exc:  # noqa: UP041 - alias safe
            raise self._timeout_error() from None
        except (ConnectionResetError, http.client.HTTPException, OSError) as exc:
            raise self._network_error(exc) from None

    def _read_body(self, response: http.client.HTTPResponse) -> bytes:
        """Read a response body, mapping transport failures."""
        try:
            return response.read()
        except (TimeoutError, socket.timeout):
            raise self._timeout_error() from None
        except (OSError, http.client.HTTPException) as exc:
            raise self._network_error(exc) from None

    def _iter_lines(self, response: http.client.HTTPResponse) -> Iterator[str]:
        """Yield decoded lines of a streaming response, mapping failures."""
        try:
            for raw_line in response:
                yield raw_line.decode("utf-8", "replace")
        except (TimeoutError, socket.timeout):
            raise self._timeout_error() from None
        except (OSError, http.client.HTTPException) as exc:
            raise self._network_error(exc) from None

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------
    def _redact(self, text: str) -> str:
        """Replace the configured API key with a placeholder."""
        if not text:
            return text
        redacted = text
        if self._api_key:
            redacted = redacted.replace(self._api_key, "[已隐藏]")
        if "Bearer " in redacted:
            # Defensive: strip any header echo such as "Bearer sk-...".
            prefix, _, rest = redacted.partition("Bearer ")
            token = rest.split()[0] if rest.split() else ""
            if token:
                redacted = prefix + "Bearer [已隐藏]" + rest[len(token):]
        return redacted

    def _timeout_error(self) -> DeepSeekError:
        return DeepSeekError(
            f"请求超时（{self._timeout:g} 秒），请检查网络后重试",
            kind="timeout",
        )

    def _network_error(self, exc: BaseException) -> DeepSeekError:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return self._timeout_error()
        detail = str(reason) or type(exc).__name__
        if isinstance(reason, str) and "timed out" in reason.lower():
            return self._timeout_error()
        detail = self._redact(detail).strip()
        if len(detail) > 200:
            detail = detail[:200] + "..."
        return DeepSeekError(f"无法连接 DeepSeek 服务（网络错误）：{detail}", kind="network")

    def _http_error(self, exc: urllib.error.HTTPError) -> DeepSeekError:
        """Turn an HTTPError into a mapped DeepSeekError with its JSON body."""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - body may be unreadable
            raw = b""
        finally:
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - nothing useful to do
                pass
        error = self._error_from_body(exc.code, raw)
        error.retry_after = _parse_retry_after(getattr(exc, "headers", None))
        return error

    def _error_from_body(self, status: int, raw: bytes) -> DeepSeekError:
        """Map an HTTP status + JSON error body to a friendly DeepSeekError."""
        message = self._extract_error_message(raw)
        detail = f"：{message}" if message else ""
        if status == 401:
            kind, base = "auth", "API Key 无效或无权限，请检查设置"
        elif status == 402:
            kind, base = "quota", "DeepSeek 账户余额不足，请充值后重试"
        elif status == 403:
            kind, base = "auth", "API Key 无权访问该模型或接口"
        elif status == 404:
            kind, base = "config", "接口地址不存在（HTTP 404），请检查 Base URL 是否正确"
        elif status == 429:
            kind, base = "rate_limit", "请求过于频繁（触发限流），请稍后重试"
        elif status in (400, 413, 422):
            kind, base = "bad_request", f"请求参数有误（HTTP {status}）"
        elif 500 <= status < 600:
            kind, base = "server", f"DeepSeek 服务端错误（HTTP {status}），请稍后重试"
        else:
            kind, base = "error", f"请求失败（HTTP {status}）"
        return DeepSeekError(f"{base}{detail}", kind=kind, status_code=status)

    def _extract_error_message(self, raw: bytes) -> str | None:
        """Pull a redacted ``error.message`` out of a JSON error body."""
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return self._error_message_from_object(obj)

    def _error_message_from_object(self, obj: dict) -> str | None:
        error = obj.get("error")
        message: object = None
        if isinstance(error, dict):
            message = error.get("message")
        elif isinstance(error, str):
            message = error
        if message is None:
            message = obj.get("message")
        if message is None:
            return None
        text = self._redact(str(message)).strip().replace("\n", " ")
        return text[:300] or None

    def _error_from_payload(self, obj: dict) -> DeepSeekError:
        """Map an ``{"error": ...}`` object returned with HTTP 200."""
        message = self._error_message_from_object(obj)
        detail = f"：{message}" if message else ""
        blob = ""
        error = obj.get("error")
        if isinstance(error, dict):
            blob = " ".join(
                str(error.get(key) or "") for key in ("message", "type", "code")
            )
        blob = (blob + " " + str(obj.get("type") or "")).lower()
        if "balance" in blob or "quota" in blob or "insufficient" in blob:
            kind, base = "quota", "DeepSeek 账户余额不足，请充值后重试"
        elif "rate" in blob or "too many" in blob:
            kind, base = "rate_limit", "请求过于频繁（触发限流），请稍后重试"
        elif "auth" in blob or "api key" in blob or "unauthorized" in blob:
            kind, base = "auth", "API Key 无效或无权限，请检查设置"
        elif "invalid" in blob or "bad request" in blob:
            kind, base = "bad_request", "请求参数有误"
        else:
            kind, base = "server", "DeepSeek 返回了错误"
        return DeepSeekError(f"{base}{detail}", kind=kind)

    # ------------------------------------------------------------------
    # Retry policy
    # ------------------------------------------------------------------
    def _is_retryable(self, exc: DeepSeekError) -> bool:
        return exc.status_code in _RETRYABLE_STATUS or exc.kind == "network"

    def _backoff(self, retry_index: int) -> float:
        """Exponential backoff: base, 2*base, 4*base, ... capped."""
        return min(self._retry_base * (2**retry_index), self._retry_max_delay)

    def _retry_delay(self, exc: DeepSeekError, retry_index: int) -> float:
        if exc.retry_after is not None:
            return min(float(exc.retry_after), _MAX_RETRY_AFTER)
        return self._backoff(retry_index)

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------
    def _convert_once(
        self,
        payload: dict,
        *,
        stream: bool,
        on_delta: Callable[[str], None] | None,
    ) -> VisionResult:
        """Perform exactly one HTTP round trip and parse its response."""
        with self._open(
            url=self._chat_url,
            method="POST",
            body=payload,
            stream=stream,
        ) as response:
            if stream:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in content_type:
                    return self._parse_sse_lines(self._iter_lines(response), on_delta)
                # A non-SSE reply to a streaming request: usually a normal JSON
                # completion or a JSON error object.
                return self._parse_json_or_sse_body(response, on_delta)
            raw = self._read_body(response)
            return self._parse_completion(raw, response.headers)

    def _parse_completion(
        self,
        raw: bytes,
        headers: object | None = None,
    ) -> VisionResult:
        """Parse a non-streaming chat completion JSON body."""
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise self._invalid_response("响应不是有效的 JSON 数据") from None
        if not isinstance(obj, dict):
            raise self._invalid_response("响应格式无效")
        if obj.get("error"):
            raise self._error_from_payload(obj)
        result = self._completion_from_object(obj, headers)
        return result

    def _parse_json_or_sse_body(
        self,
        response: http.client.HTTPResponse,
        on_delta: Callable[[str], None] | None,
    ) -> VisionResult:
        """Handle ``stream=True`` answers that are not ``text/event-stream``."""
        raw = self._read_body(response)
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace")
            if "data:" in text:
                return self._parse_sse_lines(text.splitlines(), on_delta)
            raise self._invalid_response("响应不是有效的 JSON 数据") from None
        if isinstance(obj, dict):
            if obj.get("error"):
                raise self._error_from_payload(obj)
            if isinstance(obj.get("choices"), list):
                return self._completion_from_object(obj, response.headers)
        raise self._invalid_response("响应格式无效")

    def _completion_from_object(
        self, obj: dict, headers: object | None = None
    ) -> VisionResult:
        """Build a VisionResult from a standard chat completion object."""
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._invalid_response("响应格式无效：缺少 choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise self._invalid_response("响应格式无效：缺少 message")
        text = _content_to_text(message.get("content"))
        if not text.strip():
            raise self._invalid_response("DeepSeek 返回了空内容，请重试")
        model = obj.get("model")
        request_id = obj.get("id")
        usage = obj.get("usage")
        return VisionResult(
            text=text,
            model=model if isinstance(model, str) and model else self._model,
            usage=usage if isinstance(usage, dict) else None,
            request_id=request_id if isinstance(request_id, str) and request_id else self._header_request_id(headers),
        )

    def _parse_sse_lines(
        self,
        lines: Iterable[str],
        on_delta: Callable[[str], None] | None,
    ) -> VisionResult:
        """Parse SSE lines, invoking ``on_delta`` per text chunk."""
        parts: list[str] = []
        usage: dict | None = None
        request_id: str | None = None
        model = self._model
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(":"):
                continue  # blank separator or SSE comment
            if not stripped.startswith("data:"):
                continue  # event:, id:, retry: ... are not needed
            data = stripped[len("data:"):].strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                raise self._invalid_response("流式响应包含无效的 JSON 数据") from None
            if not isinstance(obj, dict):
                raise self._invalid_response("流式响应格式无效")
            if obj.get("error"):
                raise self._error_from_payload(obj)
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            chunk_id = obj.get("id")
            if isinstance(chunk_id, str) and chunk_id:
                request_id = chunk_id
            chunk_model = obj.get("model")
            if isinstance(chunk_model, str) and chunk_model:
                model = chunk_model
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            text = self._delta_text(choice)
            if text:
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
        text = "".join(parts)
        if not text.strip():
            raise self._invalid_response("DeepSeek 未返回任何内容，请重试")
        return VisionResult(
            text=text,
            model=model,
            usage=usage,
            request_id=request_id,
        )

    def _delta_text(self, choice: dict) -> str:
        """Extract incremental text from one stream choice."""
        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = _content_to_text(delta.get("content"))
            if text:
                return text
        message = choice.get("message")
        if isinstance(message, dict):
            return _content_to_text(message.get("content"))
        return ""

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _invalid_response(self, message: str) -> DeepSeekError:
        return DeepSeekError(message, kind="invalid_response")

    def _header_request_id(self, headers: object) -> str | None:
        getter = getattr(headers, "get", None)
        if getter is None:
            return None
        try:
            value = getter("x-request-id")
        except Exception:  # pragma: no cover - exotic header objects
            return None
        return value if isinstance(value, str) and value else None

    def _model_ids(self, raw: bytes) -> list[str]:
        """Extract model ids from a ``GET /models`` body (best effort)."""
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(obj, dict):
            return []
        data = obj.get("data")
        if not isinstance(data, list):
            return []
        ids: list[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
        return ids
