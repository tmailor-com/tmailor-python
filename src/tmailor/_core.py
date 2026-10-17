"""Transport-independent logic shared by Client and AsyncClient.

Kept free of I/O so that the sync and async clients behave exactly the same, and so the logic is easy to
port to other languages (see ../../conformance/README.md).
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple, TypeVar, Union
from urllib.parse import urlsplit

import httpx

from ._schema import SchemaError
from ._version import __version__
from .errors import APIError, InvalidResponseError, MissingKeyError, TmailorError, WaitTimeoutError, error_class

T = TypeVar("T")

DEFAULT_BASE_URL = "https://open-api.tmailor.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
# A Retry-After longer than this is not waited for automatically (for example DailyCapReached until midnight).
DEFAULT_MAX_RETRY_WAIT = 60.0
BACKOFF_BASE = 0.5
BACKOFF_MAX = 8.0
# HTTP statuses that are retried for SAFE requests only (the request may or may not have been processed).
RETRY_SAFE_STATUSES = frozenset({500, 502, 503, 504})
# Review 17d SDK-01: a Retry-After of 0 (or negative, clamped to 0) still waits this long, so a retry or a poll
# after a 429 never turns into a busy loop.
MIN_RETRY_AFTER = 1.0

JsonBody = Optional[Dict[str, Any]]

USER_AGENT = f"tmailor-python/{__version__} (python {platform.python_version()})"


def resolve_api_key(api_key: Optional[str]) -> Optional[str]:
    """The key from the argument or ``TMAILOR_API_KEY``; ``None`` when there is none.

    Review 17d SDK-07: a missing key is no longer an error when the client is built (``health()`` is public);
    ``require_key`` raises MissingKeyError when an operation that needs the key is called.
    """
    key = api_key if api_key is not None else os.environ.get("TMAILOR_API_KEY", "")
    key = key.strip()
    return key or None


def require_key(key: Optional[str]) -> str:
    if not key:
        raise MissingKeyError(
            "No API key: pass api_key=... or set the TMAILOR_API_KEY environment variable",
            code="MissingKey",
        )
    return key


TRUSTED_HOST = "open-api.tmailor.com"
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


class BaseURLError(ValueError):
    """Review 17f SDK-12: a base URL that the SDK refuses to send credentials to."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_base_url(url: str, *, allow_untrusted: bool = False) -> str:
    """Review 17f SDK-12: validate and normalize a base URL; raise BaseURLError when it is refused.

    Every authenticated request sends the API key (``Authorization``) and address tokens (JSON body) to this URL, so:

    * only ``https://`` is accepted, except ``http://`` to a loopback host (127.0.0.0/8, ::1, localhost) for local
      development, which also needs ``allow_untrusted``;
    * user info (``user:pass@``), a query string, a fragment, whitespace and control characters are refused;
    * any host other than ``open-api.tmailor.com`` (port 443) needs ``allow_untrusted=True``: the caller states
      that it trusts that host with the credentials.

    Returns ``scheme://host[:port][/path]`` (lower-case scheme and host, no trailing ``/``).
    """
    if not isinstance(url, str) or not url.strip():
        raise BaseURLError("base_url is empty")
    raw = url.strip()
    if any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in raw):
        raise BaseURLError("base_url must not contain spaces or control characters")
    if "?" in raw or "#" in raw:
        raise BaseURLError("base_url must not contain a query string or a fragment")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise BaseURLError("base_url is not a valid URL") from None
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise BaseURLError("base_url must start with https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise BaseURLError("base_url must not contain user info (user:password@)")
    host = (parts.hostname or "").lower()
    if not host:
        raise BaseURLError("base_url has no host")
    trusted = scheme == "https" and host == TRUSTED_HOST and port in (None, 443)
    if scheme == "http":
        if not _is_loopback(host):
            raise BaseURLError("plain http:// is only allowed for a loopback host (local development); use https://")
        if not allow_untrusted:
            raise BaseURLError(
                "http:// to a loopback host is a development setting: pass allow_untrusted_base_url=True "
                "(CLI: --allow-untrusted-base-url)"
            )
    elif not trusted and not allow_untrusted:
        raise BaseURLError(
            f"base_url host {host!r} is not {TRUSTED_HOST}: your API key and address tokens would be sent to it. "
            "Pass allow_untrusted_base_url=True (CLI: --allow-untrusted-base-url) only if you trust that host."
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def resolve_base_url(base_url: Optional[str], allow_untrusted: bool = False) -> str:
    """The base URL from the argument, ``TMAILOR_BASE_URL`` or the default, validated by ``check_base_url``."""
    url = base_url or os.environ.get("TMAILOR_BASE_URL") or DEFAULT_BASE_URL
    return check_base_url(url, allow_untrusted=allow_untrusted)


def mask_key(key: Optional[str]) -> Optional[str]:
    """``tmk_live_<prefix>_<secret>`` -> ``tmk_live_<prefix>_***``. The prefix is not secret (GET /v1/me shows it)."""
    if key is None:
        return None
    parts = key.split("_")
    if len(parts) >= 4 and parts[0] == "tmk":
        return "_".join(parts[:3]) + "_***"
    return "***"


def mask_token(token: Optional[str]) -> str:
    if not token:
        return "None"
    return "'***'"


@dataclass
class RequestSpec:
    """One logical API call. Retries resend exactly the same bytes and headers."""

    method: str
    path: str
    query: Optional[Dict[str, Any]] = None
    json_body: JsonBody = None
    # True when repeating the request cannot change state twice: GET, read-only POSTs (token in body)
    # and POST /v1/mailboxes with an Idempotency-Key.
    safe: bool = False
    headers: Dict[str, str] = field(default_factory=dict)
    content: Optional[bytes] = None
    # Review 17d SDK-07: False for the public GET /v1/health (no Authorization header is sent, no key needed).
    auth: bool = True

    def __post_init__(self) -> None:
        if self.json_body is not None:
            self.content = json.dumps(self.json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.headers.setdefault("Content-Type", "application/json")
        if self.query:
            self.query = {k: v for k, v in self.query.items() if v is not None}


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class RateLimitInfo:
    """Values of the X-RateLimit-* headers of the last response that had them."""

    limit: Optional[int]
    remaining: Optional[int]
    reset: Optional[int]

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Optional[RateLimitInfo]:
        lim = _int_header(headers, "X-RateLimit-Limit")
        rem = _int_header(headers, "X-RateLimit-Remaining")
        rst = _int_header(headers, "X-RateLimit-Reset")
        if lim is None and rem is None and rst is None:
            return None
        return cls(limit=lim, remaining=rem, reset=rst)


def _int_header(headers: Mapping[str, str], name: str) -> Optional[int]:
    v = headers.get(name)
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def retry_after_seconds(headers: Mapping[str, str], body_error: Mapping[str, Any]) -> Optional[int]:
    v = _int_header(headers, "Retry-After")
    if v is None:
        ra = body_error.get("retry_after")
        if isinstance(ra, int) and not isinstance(ra, bool):
            v = ra
    if v is not None and v < 0:
        v = 0
    return v


_KEEP_ERROR_HEADERS = frozenset(
    {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-request-id", "allow"}
)


def build_error(response: httpx.Response) -> APIError:
    """Turn an error response into a typed exception. Never includes the request body or headers."""
    status = response.status_code
    err: Dict[str, Any] = {}
    try:
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
    except (ValueError, UnicodeDecodeError):
        err = {}
    code = err.get("code") if isinstance(err.get("code"), str) else None
    message = err.get("message") if isinstance(err.get("message"), str) else None
    if not message:
        message = f"HTTP {status} {response.reason_phrase}".strip()
    d_raw = err.get("description")
    description = d_raw if isinstance(d_raw, str) else ""
    r_raw = err.get("request_id")
    request_id: Optional[str] = r_raw if isinstance(r_raw, str) else None
    if request_id is None:
        request_id = response.headers.get("X-Request-Id")
    cls = error_class(code, status)
    keep_headers = {k: v for k, v in response.headers.items() if k.lower() in _KEEP_ERROR_HEADERS}
    return cls(
        message,
        code=code or f"HTTP{status}",
        status=status,
        description=description,
        request_id=request_id,
        retry_after=retry_after_seconds(response.headers, err),
        body=err,
        headers=keep_headers,
    )


def retry_delay(
    spec: RequestSpec,
    attempt: int,
    *,
    error: Optional[TmailorError],
    max_retries: int,
    max_retry_wait: float,
    connect_failed: bool = False,
) -> Optional[float]:
    """Seconds to wait before the next attempt, or None when the request must not be retried.

    Rules (same in every SDK):
      * at most ``max_retries`` retries;
      * 429 RateLimited: the API did not process the request, so ANY request is retried after Retry-After;
        DailyCapReached or a Retry-After above ``max_retry_wait`` is not retried;
      * 500/502/503/504 and network errors: only SAFE requests (``spec.safe``), except a failed connection
        (nothing was sent), which is retried for any request;
      * 503 CacheUnavailable is never retried (a delete may have half-happened, see secret_changed).
    """
    if attempt >= max_retries:
        return None
    if error is None:
        return None
    status = error.status
    if status == 429:
        if error.code == "DailyCapReached":
            return None
        ra = error.retry_after
        if ra is None:
            return backoff(attempt)
        if ra > max_retry_wait:
            return None
        return max(float(ra), MIN_RETRY_AFTER)  # Review 17d SDK-01: Retry-After <= 0 is not a busy retry
    if status is None:
        if connect_failed or spec.safe:
            return backoff(attempt)
        return None
    if status in RETRY_SAFE_STATUSES and spec.safe:
        if error.code == "CacheUnavailable":
            return None
        ra = error.retry_after
        if ra is not None:
            return max(float(ra), MIN_RETRY_AFTER) if ra <= max_retry_wait else None  # Review 17d SDK-01
        return backoff(attempt)
    return None


def backoff(attempt: int) -> float:
    """Exponential backoff with full jitter: random in [base*2^n / 2, base*2^n], capped."""
    ceiling = min(BACKOFF_MAX, BACKOFF_BASE * (2**attempt))
    return random.uniform(ceiling / 2, ceiling)


def within_deadline(delay: Optional[float], now: float, deadline: Optional[float]) -> Optional[float]:
    """Review 17d SDK-01: a retry sleep that would end at or after ``deadline`` is not taken (None)."""
    if delay is None or deadline is None:
        return delay
    return delay if now + delay < deadline else None


def request_timeout(timeout: float, now: float, deadline: Optional[float]) -> Tuple[float, bool]:
    """Review 17e SDK-01: the HTTP timeout of one attempt inside a deadline (wait_for_message).

    Returns ``(timeout, deadline_bound)``. Without a deadline it is the client timeout. With one, the time left
    (``deadline - now``) caps the request: nothing left raises WaitTimeoutError BEFORE anything is sent, and
    ``deadline_bound`` is True when the cap is the deadline (a timeout of that request is then a WaitTimeoutError,
    not an APITimeoutError). httpx applies the value to each phase (connect, write, read, pool), so it is NOT a
    total deadline: Review 17f SDK-01 adds an outer bound (async: ``asyncio.wait_for``) and ``past_deadline`` after
    every request (sync and async).
    """
    if deadline is None:
        return timeout, False
    remaining = deadline - now
    if remaining <= 0:
        raise WaitTimeoutError()
    if remaining <= timeout:  # Review 17f SDK-01: equal also means the deadline is the binding limit
        return remaining, True
    return timeout, False


def past_deadline(now: float, deadline: Optional[float]) -> bool:
    """Review 17f SDK-01: True when a request ended after the wait deadline (its result is not used)."""
    return deadline is not None and now > deadline


def parse_json(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    ok = True
    data: Any = None
    try:
        data = response.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        ok = False
    if not ok:
        # Review 17d SDK-04: raised OUTSIDE the except block and "from None": neither __cause__ nor __context__
        # holds the decoder error, whose .doc is the whole body (it can carry address tokens).
        raise InvalidResponseError(
            "The API returned a response that is not JSON",
            status=response.status_code,
            request_id=response.headers.get("X-Request-Id"),
        ) from None
    return data


def parse_body(response: httpx.Response, data: Any, parse: Callable[[Any], T]) -> T:
    """Validate a 2xx body with a ``from_api`` reader (Review 17d SDK-06).

    A schema violation raises InvalidResponseError naming the field only: no value, no body, no chained exception.
    """
    problem = ""
    try:
        return parse(data)
    except SchemaError as exc:
        problem = str(exc)
    raise InvalidResponseError(
        f"The API response does not match the expected format: {problem}",
        status=response.status_code,
        request_id=response.headers.get("X-Request-Id"),
    ) from None


def check_cursor(seen: Set[str], cursor: str) -> None:
    """Review 17d SDK-02: stop paginating when the API hands out a cursor that was already used (loop or cycle)."""
    if cursor in seen:
        raise InvalidResponseError("The API returned a pagination cursor that was already used; stopping to avoid a loop")
    seen.add(cursor)


def base_headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def request_headers(spec: RequestSpec, base: Mapping[str, str], api_key: Optional[str]) -> Dict[str, str]:
    """Headers of one request. Authorization only for operations that need the key (Review 17d SDK-07)."""
    headers = dict(base)
    if spec.auth:
        headers["Authorization"] = f"Bearer {require_key(api_key)}"
    headers.update(spec.headers)
    return headers


ResponseTuple = Tuple[Any, httpx.Response]


def clean_str(value: Union[str, None]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None
