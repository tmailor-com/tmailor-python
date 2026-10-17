"""Request builders: one function per API operation, returning a RequestSpec.

The sync and async clients only differ in how they send a RequestSpec. Ports to other languages should
keep this table identical (paths, methods, bodies, safety flags).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional, Union

from ._core import RequestSpec, new_idempotency_key
from .errors import TmailorError

DateLike = Union[str, _dt.date, None]
TimeLike = Union[int, float, _dt.datetime, None]

MAX_PAGE = 100


def _date(v: DateLike, name: str) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, str):
        return v
    raise TypeError(f"{name} must be a date or a 'YYYY-MM-DD' string")


def unix_time(v: TimeLike) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError("since must be a unix time or a datetime")
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            raise ValueError("since: use a timezone-aware datetime (for example datetime.now(timezone.utc))")
        return int(v.timestamp())
    if isinstance(v, (int, float)):
        return int(v)
    raise TypeError("since must be a unix time or a datetime")


def page_size(v: Optional[int]) -> int:
    n = 50 if v is None else int(v)
    if not 1 <= n <= MAX_PAGE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE}")
    return n


def _token(token: Optional[str]) -> str:
    if not token:
        raise TmailorError(
            "This mailbox has no token (tokens can be temporarily unavailable). Get it again with client.mailboxes.list()",
            code="NoToken",
        )
    return token


def health() -> RequestSpec:
    # Review 17d SDK-07: public endpoint, sent without Authorization (works without an API key).
    return RequestSpec("GET", "/v1/health", safe=True, auth=False)


def me() -> RequestSpec:
    return RequestSpec("GET", "/v1/me", safe=True)


def domains() -> RequestSpec:
    return RequestSpec("GET", "/v1/domains", safe=True)


def usage(from_: DateLike = None, to: DateLike = None) -> RequestSpec:
    return RequestSpec("GET", "/v1/usage", query={"from": _date(from_, "from_"), "to": _date(to, "to")}, safe=True)


def mailboxes_create(
    domain: Optional[str] = None, name: Optional[str] = None, idempotency_key: Optional[str] = None
) -> RequestSpec:
    body: Dict[str, Any] = {}
    if domain is not None:
        body["domain"] = domain
    if name is not None:
        body["name"] = name
    key = idempotency_key if idempotency_key is not None else new_idempotency_key()
    if not (1 <= len(key) <= 128) or any(not (0x21 <= ord(c) <= 0x7E) for c in key):
        raise ValueError("idempotency_key must be 1-128 visible ASCII characters")
    # Safe to retry: the server returns the first result for the same key + same body.
    return RequestSpec("POST", "/v1/mailboxes", json_body=body, safe=True, headers={"Idempotency-Key": key})


def mailboxes_list(cursor: Optional[str] = None, size: Optional[int] = None) -> RequestSpec:
    return RequestSpec("GET", "/v1/mailboxes", query={"limit": page_size(size), "cursor": cursor}, safe=True)


# ── one address: POST + token in the JSON body (never in the URL or a header)
def mailbox_info(token: Optional[str]) -> RequestSpec:
    return RequestSpec("POST", "/v1/mailbox/info", json_body={"token": _token(token)}, safe=True)


def mailbox_messages(
    token: Optional[str],
    *,
    unread: Optional[bool] = None,
    from_: Optional[str] = None,
    subject: Optional[str] = None,
    since: TimeLike = None,
    size: Optional[int] = None,
    cursor: Optional[str] = None,
) -> RequestSpec:
    body: Dict[str, Any] = {"token": _token(token)}
    if unread is not None:
        body["unread"] = bool(unread)
    if from_:
        body["from"] = from_
    if subject:
        body["subject"] = subject
    s = unix_time(since)
    if s is not None:
        body["since"] = s
    body["limit"] = page_size(size)
    if cursor:
        body["cursor"] = cursor
    return RequestSpec("POST", "/v1/mailbox/messages", json_body=body, safe=True)


def _msg(message_id: str) -> str:
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("message_id is required")
    return message_id


def mailbox_message(token: Optional[str], message_id: str) -> RequestSpec:
    # The server marks the message as read; that is idempotent, so the request stays safe to retry.
    body = {"token": _token(token), "message_id": _msg(message_id)}
    return RequestSpec("POST", "/v1/mailbox/message", json_body=body, safe=True)


def mailbox_message_delete(token: Optional[str], message_id: str) -> RequestSpec:
    return RequestSpec("POST", "/v1/mailbox/message/delete", json_body={"token": _token(token), "message_id": _msg(message_id)})


def mailbox_clear(token: Optional[str]) -> RequestSpec:
    return RequestSpec("POST", "/v1/mailbox/messages/clear", json_body={"token": _token(token)})


def mailbox_delete(token: Optional[str]) -> RequestSpec:
    return RequestSpec("POST", "/v1/mailbox/delete", json_body={"token": _token(token)})
