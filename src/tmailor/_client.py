"""Synchronous client."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Mapping, Optional, Set, Tuple, TypeVar, Union

import httpx

from . import _ops
from ._core import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    DEFAULT_TIMEOUT,
    RateLimitInfo,
    RequestSpec,
    base_headers,
    build_error,
    check_cursor,
    mask_key,
    parse_body,
    parse_json,
    past_deadline,
    request_headers,
    request_timeout,
    resolve_api_key,
    resolve_base_url,
    retry_delay,
    within_deadline,
)
from ._wait import WaitPlan
from .errors import APIConnectionError, APITimeoutError, RateLimitedError, TmailorError, WaitTimeoutError
from .models import (
    DomainList,
    Health,
    MailboxData,
    MailboxFields,
    Me,
    Message,
    MessagePage,
    Usage,
    parse_deleted,
    parse_mailbox_page,
)

__all__ = ["Client", "Mailbox", "MailboxPage", "Mailboxes"]

T = TypeVar("T")


class Client:
    """Client for the Tmailor Open API.

    >>> with Client() as client:              # reads TMAILOR_API_KEY
    ...     box = client.mailboxes.create()
    ...     msg = box.wait_for_message(timeout=120)
    ...     print(msg.otp())

    Use it on a server only: requests must come from an IP in your account allowlist, and the API key
    must never be shipped to browsers or mobile apps.

    Args:
        api_key: ``tmk_live_...`` key. Defaults to the ``TMAILOR_API_KEY`` environment variable. Only
            ``health()`` works without a key; other calls raise MissingKeyError before sending anything.
        base_url: Defaults to ``TMAILOR_BASE_URL`` or ``https://open-api.tmailor.com``.
        timeout: Seconds for each HTTP request.
        max_retries: Automatic retries for 429, 5xx and network errors (see README "Retries").
        max_retry_wait: Longest ``Retry-After`` (seconds) that is waited for automatically.
        http_client: Your own ``httpx.Client`` (proxies, custom transport). Its base URL is not used.
        allow_untrusted_base_url: Required (True) for a ``base_url`` whose host is not ``open-api.tmailor.com``
            (staging, a proxy, ``http://127.0.0.1`` for local development): the API key and address tokens are sent
            to that host. ``http://`` is accepted only for a loopback host. Raises ``BaseURLError`` (a ValueError).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        http_client: Optional[httpx.Client] = None,
        allow_untrusted_base_url: bool = False,
    ) -> None:
        # Review 17d SDK-07: a missing key is reported by the first call that needs it, not here.
        self._api_key = resolve_api_key(api_key)
        # Review 17f SDK-12: https only, no user info/query/fragment; another host needs allow_untrusted_base_url.
        self.base_url = resolve_base_url(base_url, allow_untrusted_base_url)
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.max_retries = max_retries
        self.max_retry_wait = float(max_retry_wait)
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout, follow_redirects=False)
        self._headers = base_headers()
        self._timeout = timeout
        self.last_rate_limit: Optional[RateLimitInfo] = None
        # Replaceable in tests.
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._time: Callable[[], float] = time.time
        self.mailboxes = Mailboxes(self)

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r}, api_key={mask_key(self._api_key)!r})"

    __str__ = __repr__

    # ── lifecycle
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── account
    def health(self) -> Health:
        """``GET /v1/health`` (public: no key needed, none is sent). A degraded service returns 503 and raises
        ServiceUnavailableError (code ``HTTP503``)."""
        return self._call(_ops.health(), Health.from_api)

    def me(self) -> Me:
        return self._call(_ops.me(), Me.from_api)

    def domains(self) -> DomainList:
        return self._call(_ops.domains(), DomainList.from_api)

    def usage(self, from_: _ops.DateLike = None, to: _ops.DateLike = None) -> Usage:
        return self._call(_ops.usage(from_, to), Usage.from_api)

    def mailbox(self, token: str) -> Mailbox:
        """A Mailbox for a token you stored earlier. No request is made; call ``.info()`` to load its fields."""
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        return Mailbox(self, token=token.strip())

    # ── transport
    def _call(
        self, spec: RequestSpec, parse: Callable[[Any], T], *, deadline: Optional[float] = None, retry_429: bool = True
    ) -> T:
        data, resp = self._request(spec, deadline=deadline, retry_429=retry_429)
        return parse_body(resp, data, parse)

    def _request(
        self,
        spec: RequestSpec,
        *,
        deadline: Optional[float] = None,
        retry_429: bool = True,
    ) -> Tuple[Any, httpx.Response]:
        """Send ``spec`` with the retry rules.

        Review 17d SDK-01: ``deadline`` (monotonic) bounds every retry sleep, and ``retry_429=False`` leaves 429
        RateLimited to the caller (wait_for_message handles it with its own deadline-aware plan).
        Review 17e SDK-01: ``deadline`` also bounds every HTTP attempt: no time left raises WaitTimeoutError before
        sending, the attempt timeout is ``min(timeout, time left)`` and a timeout caused by the deadline raises
        WaitTimeoutError.
        """
        url = self.base_url + spec.path
        headers = request_headers(spec, self._headers, self._api_key)
        attempt = 0
        while True:
            connect_failed = False
            err: TmailorError
            timeout, deadline_bound = request_timeout(self._timeout, self._monotonic(), deadline)  # Review 17e SDK-01
            try:
                resp = self._http.request(
                    spec.method, url, params=spec.query or None, content=spec.content, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                if deadline_bound or past_deadline(self._monotonic(), deadline):
                    raise WaitTimeoutError() from None  # Review 17e/17f SDK-01: the wait deadline ended this request
                err = APITimeoutError(f"Request timed out ({type(exc).__name__})")
                connect_failed = isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            except httpx.ConnectError as exc:
                err = APIConnectionError(f"Connection failed ({type(exc).__name__})")
                connect_failed = True
            except httpx.TransportError as exc:
                err = APIConnectionError(f"Network error ({type(exc).__name__})")
            else:
                # Review 17f SDK-01: httpx timeouts are per phase (connect/write/read/pool), not a total deadline; a
                # server that trickles bytes can stretch a request past it. A request that ENDED after the wait
                # deadline is a WaitTimeoutError (its result is dropped). Sync limit: the overrun itself cannot be
                # cut short, it lasts at most one read timeout (min(client timeout, time left)) per read phase.
                if past_deadline(self._monotonic(), deadline):
                    resp.close()
                    raise WaitTimeoutError()
                rl = RateLimitInfo.from_headers(resp.headers)
                if rl is not None:
                    self.last_rate_limit = rl
                if resp.status_code < 400:
                    return parse_json(resp), resp
                err = build_error(resp)
            delay: Optional[float] = None
            if retry_429 or err.status != 429:
                delay = retry_delay(
                    spec,
                    attempt,
                    error=err,
                    max_retries=self.max_retries,
                    max_retry_wait=self.max_retry_wait,
                    connect_failed=connect_failed,
                )
                delay = within_deadline(delay, self._monotonic(), deadline)
            if delay is None:
                raise err from None
            self._sleep(delay)
            attempt += 1


@dataclass(frozen=True)
class MailboxPage:
    items: List[Mailbox]
    next_cursor: Optional[str]


class Mailboxes:
    """``client.mailboxes``: create and list addresses."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def create(
        self, domain: Optional[str] = None, name: Optional[str] = None, *, idempotency_key: Optional[str] = None
    ) -> Mailbox:
        """Create an address (uses 1 unit of mailbox quota; never expires).

        Without ``domain`` a random shared domain and a random name are used. ``domain`` must be one of your
        usable domains (``client.domains()``); ``name`` is only allowed with your own domain.
        An ``Idempotency-Key`` is generated (or pass yours), and retries reuse it, so a retry never creates
        a second address.
        """
        f = self._client._call(_ops.mailboxes_create(domain, name, idempotency_key), MailboxFields.from_api)
        return Mailbox._from_fields(self._client, f)

    def list_page(self, cursor: Optional[str] = None, page_size: Optional[int] = None) -> MailboxPage:
        items, next_cursor = self._client._call(_ops.mailboxes_list(cursor, page_size), parse_mailbox_page)
        return MailboxPage(items=[Mailbox._from_fields(self._client, f) for f in items], next_cursor=next_cursor)

    def list(self, *, limit: Optional[int] = None, page_size: Optional[int] = None) -> Iterator[Mailbox]:
        """Every address of the account (newest first) with its current token, following ``next_cursor``.

        ``limit`` caps the total number of mailboxes yielded (``None`` = all). A cursor the API already returned
        raises InvalidResponseError instead of looping forever.
        """
        cursor: Optional[str] = None
        seen: Set[str] = set()  # Review 17d SDK-02
        n = 0
        size = page_size if page_size is not None else (min(limit, 100) if limit else 50)
        while True:
            page = self.list_page(cursor, size)
            for mb in page.items:
                if limit is not None and n >= limit:
                    return
                yield mb
                n += 1
            if not page.next_cursor or (limit is not None and n >= limit):
                return
            check_cursor(seen, page.next_cursor)
            cursor = page.next_cursor

    def __iter__(self) -> Iterator[Mailbox]:
        return self.list()


class Mailbox(MailboxData):
    """One address. Every call sends ``token`` in the JSON body."""

    def __init__(self, client: Client, *, token: Optional[str] = None, data: Optional[Mapping[str, Any]] = None) -> None:
        self._client = client
        self._init_fields(token)
        if data:
            self._apply(data)

    @classmethod
    def _from_fields(cls, client: Client, f: MailboxFields) -> Mailbox:
        mb = cls(client)
        mb._set_fields(f)
        return mb

    def info(self) -> Mailbox:
        """Reload the address (``message_count``, current tokens). Updates this object and returns it."""
        self._set_fields(self._client._call(_ops.mailbox_info(self.token), MailboxFields.from_api))
        return self

    def _page(
        self,
        *,
        unread: Optional[bool] = None,
        from_: Optional[str] = None,
        subject: Optional[str] = None,
        since: _ops.TimeLike = None,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
        deadline: Optional[float] = None,
        retry_429: bool = True,
    ) -> MessagePage:
        spec = _ops.mailbox_messages(
            self.token, unread=unread, from_=from_, subject=subject, since=since, size=page_size, cursor=cursor
        )
        return self._client._call(spec, MessagePage.from_api, deadline=deadline, retry_429=retry_429)

    def messages_page(
        self,
        *,
        unread: Optional[bool] = None,
        from_: Optional[str] = None,
        subject: Optional[str] = None,
        since: _ops.TimeLike = None,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> MessagePage:
        """One page of messages (newest first) with ``total`` and ``next_cursor``."""
        return self._page(unread=unread, from_=from_, subject=subject, since=since, page_size=page_size, cursor=cursor)

    def messages(
        self,
        unread: Optional[bool] = None,
        from_: Optional[str] = None,
        subject: Optional[str] = None,
        since: _ops.TimeLike = None,
        limit: Optional[int] = None,
        *,
        page_size: Optional[int] = None,
    ) -> Iterator[Message]:
        """Messages of the last 24 hours, newest first, following the cursor.

        Filters run on the server: ``unread`` (only not seen), ``from_`` (text in sender name or address),
        ``subject`` (text in subject), ``since`` (unix time or aware datetime; EXCLUSIVE: only messages with
        ``received_at > since``). ``limit`` caps the total number of messages yielded. Items have no body: use
        ``message(id)``. A cursor the API already returned raises InvalidResponseError instead of looping.
        """
        cursor: Optional[str] = None
        seen: Set[str] = set()  # Review 17d SDK-02
        n = 0
        size = page_size if page_size is not None else (min(limit, 100) if limit else 50)
        while True:
            page = self.messages_page(unread=unread, from_=from_, subject=subject, since=since, page_size=size, cursor=cursor)
            for m in page.items:
                if limit is not None and n >= limit:
                    return
                yield m
                n += 1
            if not page.next_cursor or (limit is not None and n >= limit):
                return
            check_cursor(seen, page.next_cursor)
            cursor = page.next_cursor

    def message(self, message_id: Union[str, Message]) -> Message:
        """Read one message with ``text`` and sanitized ``html``. This marks the message as read (``seen`` is True
        in the result and in later message lists). There is no separate "mark as seen" call."""
        return self._client._call(_ops.mailbox_message(self.token, _mid(message_id)), Message.from_api_with_body)

    def delete_message(self, message_id: Union[str, Message]) -> None:
        self._client._request(_ops.mailbox_message_delete(self.token, _mid(message_id)))

    def clear(self) -> int:
        """Delete every message. Returns how many were deleted."""
        return self._client._call(_ops.mailbox_clear(self.token), parse_deleted)

    def delete(self) -> None:
        """Delete the address like "Delete" on tmailor.com. Quota is not returned; all tokens stop working."""
        self._client._request(_ops.mailbox_delete(self.token))

    def _wait_page(self, plan: WaitPlan, **filters: Any) -> MessagePage:
        """One page for wait_for_message. Review 17d SDK-01: every sleep is bounded by the wait deadline.

        429 RateLimited is not retried inside ``_request`` (no fresh retry budget per poll); the plan sleeps
        Retry-After (at least 1 s) when it fits before the deadline, otherwise the wait times out.
        """
        c = self._client
        while True:
            try:
                return self._page(page_size=100, deadline=plan.deadline, retry_429=False, **filters)
            except RateLimitedError as exc:
                retry_after = exc.retry_after
            d = plan.rate_limit_delay(c._monotonic(), retry_after)
            if d is None:
                raise WaitTimeoutError() from None
            c._sleep(d)

    def wait_for_message(
        self,
        timeout: float = 120,
        from_: Optional[str] = None,
        subject_contains: Optional[str] = None,
        poll_interval: Optional[float] = None,
        *,
        since: _ops.TimeLike = None,
    ) -> Message:
        """Poll until a NEW matching message arrives and return it with its body.

        "New" means not in the inbox when the wait started (the ids of ALL current messages are recorded first,
        without filters). Pass ``since`` (unix time / aware datetime) instead to accept every message received
        strictly after it (Review 17d SDK-05); to include a message that arrives in the same second the address was created use
        ``since=(box.created_at or 0) - 1``.
        ``from_`` and ``subject_contains`` filter the polls on the server (case-insensitive substring).
        The delay grows from 1 s to 10 s (or is fixed with ``poll_interval``) and slows down when
        ``X-RateLimit-Remaining`` gets low or the API answers 429. No sleep (poll, retry or Retry-After) goes past
        ``timeout``. Raises WaitTimeoutError after ``timeout``. The returned message is read with
        ``POST /v1/mailbox/message``, so it is marked as read.
        """
        c = self._client
        plan = WaitPlan(timeout, poll_interval, c._monotonic())
        since_ts = _ops.unix_time(since)
        baseline: Set[str] = set()
        if since_ts is None:
            # Review 17d SDK-08: the baseline lists every current message WITHOUT from/subject (conformance rule 5).
            cursor: Optional[str] = None
            seen: Set[str] = set()  # Review 17d SDK-02
            while True:
                page = self._wait_page(plan, cursor=cursor)
                baseline.update(m.id for m in page.items)
                if not page.next_cursor:
                    break
                check_cursor(seen, page.next_cursor)
                cursor = page.next_cursor
        while True:
            page = self._wait_page(plan, from_=from_, subject=subject_contains, since=since_ts)
            new = [m for m in page.items if m.id not in baseline]
            if new:
                first = min(new, key=lambda m: (m.received_at, m.id))
                spec = _ops.mailbox_message(self.token, first.id)
                return c._call(spec, Message.from_api_with_body, deadline=plan.deadline)
            delay = plan.next_delay(c._monotonic(), c._time(), c.last_rate_limit)
            if delay is None:
                raise WaitTimeoutError()
            c._sleep(delay)


def _mid(m: Union[str, Message]) -> str:
    return m.id if isinstance(m, Message) else m
