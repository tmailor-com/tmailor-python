"""Data returned by the API. Field names follow the JSON of the API (``from`` is ``from_`` in Python).

Every model keeps the original JSON in ``raw`` so fields added to the API later are still reachable.
``raw`` is never part of ``repr()`` because some objects carry address tokens.

Review 17d SDK-06: ``from_api`` validates the body against the OpenAPI schema (required fields, exact JSON
types, ``null`` only where the schema allows it) and raises ``SchemaError`` instead of inventing defaults.
The clients turn that into ``InvalidResponseError`` (code ``InvalidResponse``) without the response body.
Unknown extra fields are ignored (kept in ``raw``).

Review 17e SDK-11: a field is read as required only when ``docs/openapi.yaml`` lists it in ``required`` (the
server always sends it: ``Me.key``/``limits``/``subscriptions[]``, ``Message`` body fields, ``Usage``/``days[]``
were added to the spec after checking the PHP handlers). A field the server may omit is read with an optional
reader and typed ``Optional`` (``Mailbox.message_count``, not in ``required`` of the Mailbox schema).
``tests/test_spec_contract.py`` fails when a reader requires a field the spec does not, or rejects a ``null`` the spec allows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import _schema as S
from ._core import mask_token
from ._extract import extract_links, extract_otp
from .errors import InvalidResponseError

__all__ = [
    "Health",
    "KeyInfo",
    "Limits",
    "Subscription",
    "Me",
    "Domain",
    "DomainList",
    "WebAccess",
    "Sender",
    "Message",
    "MessagePage",
    "UsageDay",
    "Usage",
]


@dataclass(frozen=True)
class Health:
    status: str
    time: int
    version: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Any) -> Health:
        d = S.obj(data, "$")
        return cls(
            status=S.req_str(d, "status", "$"), time=S.req_int(d, "time", "$"), version=S.req_str(d, "version", "$"), raw=dict(d)
        )


@dataclass(frozen=True)
class KeyInfo:
    key_id: int
    prefix: str


@dataclass(frozen=True)
class Limits:
    quota_total: int
    quota_used: int
    quota_remaining: int
    mailbox_count: int
    rate_per_min: int
    daily_req_cap: int
    max_ip: int

    @classmethod
    def from_api(cls, data: Any, path: str = "$.limits") -> Limits:
        d = S.obj(data, path)
        return cls(
            quota_total=S.req_int(d, "quota_total", path),
            quota_used=S.req_int(d, "quota_used", path),
            quota_remaining=S.req_int(d, "quota_remaining", path),
            mailbox_count=S.req_int(d, "mailbox_count", path),
            rate_per_min=S.req_int(d, "rate_per_min", path),
            daily_req_cap=S.req_int(d, "daily_req_cap", path),
            max_ip=S.req_int(d, "max_ip", path),
        )


@dataclass(frozen=True)
class Subscription:
    plan: str
    expires_at: int
    period_end: int
    quota: int
    used: int

    @classmethod
    def from_api(cls, data: Any, path: str = "$.subscriptions[]") -> Subscription:
        d = S.obj(data, path)
        return cls(
            plan=S.req_str(d, "plan", path),
            expires_at=S.req_int(d, "expires_at", path),
            period_end=S.req_int(d, "period_end", path),
            quota=S.req_int(d, "quota", path),
            used=S.req_int(d, "used", path),
        )


@dataclass(frozen=True)
class Me:
    """``GET /v1/me``: your account, limits and plans."""

    user_id: int
    key: KeyInfo
    client_ip: str
    limits: Limits
    ip_allowlist: List[str]
    subscriptions: List[Subscription]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Any) -> Me:
        d = S.obj(data, "$")
        k = S.req_obj(d, "key", "$")  # Review 17e SDK-11: key/limits/subscriptions[] fields are required in the spec
        return cls(
            user_id=S.req_int(d, "user_id", "$"),
            key=KeyInfo(key_id=S.req_int(k, "key_id", "$.key"), prefix=S.req_str(k, "prefix", "$.key")),
            client_ip=S.req_str(d, "client_ip", "$"),
            limits=Limits.from_api(S.req_obj(d, "limits", "$")),
            ip_allowlist=S.req_str_list(d, "ip_allowlist", "$"),
            subscriptions=[
                Subscription.from_api(s, f"$.subscriptions[{i}]") for i, s in enumerate(S.req_list(d, "subscriptions", "$"))
            ],
            raw=dict(d),
        )


@dataclass(frozen=True)
class Domain:
    """One of your own domains. Shared domains are never listed."""

    domain: str
    is_user_domain: bool
    allow_custom_name: bool
    status: str
    verified: bool
    usable: bool
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Any, path: str = "$") -> Domain:
        d = S.obj(data, path)
        return cls(
            domain=S.req_str(d, "domain", path),
            is_user_domain=S.req_bool(d, "is_user_domain", path),
            allow_custom_name=S.req_bool(d, "allow_custom_name", path),
            status=S.req_str(d, "status", path),
            verified=S.req_bool(d, "verified", path),
            usable=S.req_bool(d, "usable", path),
            raw=dict(d),
        )


@dataclass(frozen=True)
class DomainList:
    """``GET /v1/domains``. Iterating it yields the domains."""

    items: List[Domain]
    shared_domains_available: bool

    @classmethod
    def from_api(cls, data: Any) -> DomainList:
        d = S.obj(data, "$")
        return cls(
            items=[Domain.from_api(x, f"$.items[{i}]") for i, x in enumerate(S.req_list(d, "items", "$"))],
            shared_domains_available=S.req_bool(d, "shared_domains_available", "$"),
        )

    def __iter__(self) -> Iterator[Domain]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class WebAccess:
    """Tokens to use the address on tmailor.com. Treat them like passwords (never shown by ``repr``)."""

    accesstoken: str = field(repr=False)
    transfertoken: str = field(repr=False)
    reuse_url: str = field(repr=False)

    @classmethod
    def from_api(cls, data: Any, path: str = "$.web") -> WebAccess:
        d = S.obj(data, path)
        return cls(
            accesstoken=S.req_str(d, "accesstoken", path),
            transfertoken=S.req_str(d, "transfertoken", path),
            reuse_url=S.req_str(d, "reuse_url", path),
        )

    def __repr__(self) -> str:
        return "WebAccess(accesstoken='***', transfertoken='***', reuse_url='***')"


@dataclass(frozen=True)
class Sender:
    name: str
    address: str


@dataclass(frozen=True)
class Message:
    """A message. From the message list ``text``/``html`` are ``None`` (``has_body`` False); from
    ``Mailbox.message(id)`` or ``wait_for_message`` they hold the body. ``seen`` is True once the message was read
    (reading one message marks it as read)."""

    id: str
    from_: Sender
    to: str
    subject: str
    received_at: int
    expires_at: int
    seen: bool
    text: Optional[str] = field(default=None, repr=False)
    html: Optional[str] = field(default=None, repr=False)
    body_available: Optional[bool] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Any, path: str = "$", *, with_body: bool = False) -> Message:
        """``with_body``: the response of ``POST /v1/mailbox/message``. Review 17e SDK-11: ``text``, ``html`` (string or
        null) and ``body_available`` are in ``required`` of the OpenAPI ``Message`` schema (always sent by the server)."""
        d = S.obj(data, path)
        f = S.req_obj(d, "from", path)
        fp = f"{path}.from"
        if with_body:
            text = S.req_str_or_none(d, "text", path)
            html = S.req_str_or_none(d, "html", path)
            body_available: Optional[bool] = S.req_bool(d, "body_available", path)
        else:
            text = html = None
            body_available = None
        seen = S.req_bool(d, "seen", path)
        if with_body and seen is not True:
            # Review 17q SDK-20: POST /v1/mailbox/message marks the message as read (REST 1.2.0), so a full message
            # must come back with seen: true; anything else is a contract violation (no value in the error).
            raise S.SchemaError(f"{path}.seen must be true for a message read with its body")
        return cls(
            id=S.req_str(d, "id", path),
            from_=Sender(name=S.req_str(f, "name", fp), address=S.req_str(f, "address", fp)),
            to=S.req_str(d, "to", path),
            subject=S.req_str(d, "subject", path),
            received_at=S.req_int(d, "received_at", path),
            expires_at=S.req_int(d, "expires_at", path),
            seen=seen,
            text=text,
            html=html,
            body_available=body_available,
            raw=dict(d),
        )

    @classmethod
    def from_api_with_body(cls, data: Any) -> Message:
        return cls.from_api(data, with_body=True)

    @property
    def has_body(self) -> bool:
        return self.text is not None or self.html is not None

    def otp(self) -> Optional[str]:
        """Best-effort one-time code from the subject and body (``None`` when unsure). Runs locally."""
        return extract_otp(self.subject, self.text, self.html)

    def links(self) -> List[str]:
        """http(s) links of the body (HTML ``href`` first, then plain text), without duplicates."""
        return extract_links(self.text, self.html)


@dataclass(frozen=True)
class MessagePage:
    items: List[Message]
    next_cursor: Optional[str]
    total: int

    @classmethod
    def from_api(cls, data: Any) -> MessagePage:
        d = S.obj(data, "$")
        return cls(
            items=[Message.from_api(x, f"$.items[{i}]") for i, x in enumerate(S.req_list(d, "items", "$"))],
            next_cursor=S.cursor(d, "$"),
            total=S.req_int(d, "total", "$"),
        )


@dataclass(frozen=True)
class UsageDay:
    date: str
    req_total: int
    req_4xx: int
    req_5xx: int
    req_429: int
    req_ip_denied: int
    mailbox_created: int
    msg_list: int
    msg_read: int

    @classmethod
    def from_api(cls, data: Any, path: str = "$.days[]") -> UsageDay:
        d = S.obj(data, path)
        return cls(
            date=S.req_str(d, "date", path),
            req_total=S.req_int(d, "req_total", path),
            req_4xx=S.req_int(d, "req_4xx", path),
            req_5xx=S.req_int(d, "req_5xx", path),
            req_429=S.req_int(d, "req_429", path),
            req_ip_denied=S.req_int(d, "req_ip_denied", path),
            mailbox_created=S.req_int(d, "mailbox_created", path),
            msg_list=S.req_int(d, "msg_list", path),
            msg_read=S.req_int(d, "msg_read", path),
        )


@dataclass(frozen=True)
class Usage:
    """``GET /v1/usage``: daily counters (Asia/Ho_Chi_Minh days)."""

    from_: str
    to: str
    timezone: str
    days: List[UsageDay]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Any) -> Usage:
        # Review 17e SDK-11: from/to/timezone/days and every day counter are required in the spec (always sent).
        d = S.obj(data, "$")
        return cls(
            from_=S.req_str(d, "from", "$"),
            to=S.req_str(d, "to", "$"),
            timezone=S.req_str(d, "timezone", "$"),
            days=[UsageDay.from_api(x, f"$.days[{i}]") for i, x in enumerate(S.req_list(d, "days", "$"))],
            raw=dict(d),
        )


def parse_deleted(data: Any) -> int:
    """``POST /v1/mailbox/messages/clear``: ``{"deleted": n}``."""
    return S.req_int(S.obj(data, "$"), "deleted", "$")


@dataclass(frozen=True)
class MailboxFields:
    """Validated fields of the Mailbox schema (internal)."""

    email: str
    domain: str
    is_user_domain: bool
    custom_name: bool
    created_at: int
    message_count: Optional[int]
    token: Optional[str]
    web: Optional[WebAccess]
    raw: Dict[str, Any]

    @classmethod
    def from_api(cls, data: Any, path: str = "$") -> MailboxFields:
        d = S.obj(data, path)
        token = S.req_str_or_none(d, "token", path)
        w = S.req_obj_or_none(d, "web", path)
        web = WebAccess.from_api(w, f"{path}.web") if w is not None else None
        # token and web are null together, and token is the same value as web.accesstoken (OpenAPI Mailbox).
        if (token is None) != (web is None):
            raise S.SchemaError(f"{path}.token and {path}.web must both be null or both be set")
        if token is not None and web is not None and token != web.accesstoken:
            raise S.SchemaError(f"{path}.token must equal {path}.web.accesstoken")
        return cls(
            email=S.req_str(d, "email", path),
            domain=S.req_str(d, "domain", path),
            is_user_domain=S.req_bool(d, "is_user_domain", path),
            custom_name=S.req_bool(d, "custom_name", path),
            created_at=S.req_int(d, "created_at", path),
            message_count=S.opt_int(d, "message_count", path),
            token=token,
            web=web,
            raw=dict(d),
        )


def parse_mailbox_page(data: Any) -> Tuple[List[MailboxFields], Optional[str]]:
    """``GET /v1/mailboxes``: items + next_cursor."""
    d = S.obj(data, "$")
    items = [MailboxFields.from_api(x, f"$.items[{i}]") for i, x in enumerate(S.req_list(d, "items", "$"))]
    return items, S.cursor(d, "$")


class MailboxData:
    """Fields of an address (shared by ``Mailbox`` and ``AsyncMailbox``).

    ``token`` is the address token: every operation on the address sends it in the JSON body.
    ``email``, ``domain`` ... are ``None`` on a mailbox built with ``client.mailbox(token)`` until ``info()``.
    """

    email: Optional[str]
    domain: Optional[str]
    is_user_domain: Optional[bool]
    custom_name: Optional[bool]
    created_at: Optional[int]
    message_count: Optional[int]
    token: Optional[str]
    web: Optional[WebAccess]
    raw: Dict[str, Any]

    def _init_fields(self, token: Optional[str]) -> None:
        self.email = None
        self.domain = None
        self.is_user_domain = None
        self.custom_name = None
        self.created_at = None
        self.message_count = None
        self.token = token
        self.web = None
        self.raw = {}

    def _set_fields(self, f: MailboxFields) -> None:
        self.email = f.email
        self.domain = f.domain
        self.is_user_domain = f.is_user_domain
        self.custom_name = f.custom_name
        self.created_at = f.created_at
        if f.message_count is not None:
            self.message_count = f.message_count
        # Review 17d SDK-03: always the value of the response, also when it is null (never keep an older token).
        self.token = f.token
        self.web = f.web
        self.raw = f.raw

    def _apply(self, d: Mapping[str, Any]) -> None:
        """Load a Mailbox JSON object (validated; raises InvalidResponseError without the body)."""
        problem = ""
        try:
            self._set_fields(MailboxFields.from_api(d))
            return
        except S.SchemaError as exc:
            problem = str(exc)
        raise InvalidResponseError(f"The mailbox data does not match the API schema ({problem})")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(email={self.email!r}, domain={self.domain!r}, created_at={self.created_at!r}, "
            f"message_count={self.message_count!r}, token={mask_token(self.token)})"
        )

    __str__ = __repr__
