from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from typing import Any, Dict, List, Optional

import httpx
import pytest

import tmailor
from tmailor import _core, _ops
from tmailor._core import RequestSpec, backoff, retry_delay
from tmailor._wait import WaitPlan
from tmailor.errors import (
    APIConnectionError,
    CacheUnavailableError,
    DailyCapReachedError,
    InternalServerError,
    RateLimitedError,
    TmailorError,
)

KEY = "tmk_live_abcdefabcdef_SUPERSECRETvalue000000000000000000000000000"
TOKEN = "eyJ.SECRET-ADDRESS-TOKEN.sig"


# ── retry rules
def _err(cls: type, status: int, code: str, retry_after: Any = None) -> TmailorError:
    e: TmailorError = cls("x", code=code, status=status, retry_after=retry_after)
    return e


GET = RequestSpec("GET", "/v1/me", safe=True)
POST = RequestSpec("POST", "/v1/mailbox/delete", json_body={"token": "t"})


@pytest.mark.parametrize(
    ("spec", "err", "connect", "expect"),
    [
        (POST, _err(RateLimitedError, 429, "RateLimited", 7), False, 7.0),
        (POST, _err(RateLimitedError, 429, "RateLimited", 61), False, None),
        (GET, _err(DailyCapReachedError, 429, "DailyCapReached", 5), False, None),
        (POST, _err(InternalServerError, 500, "InternalError"), False, None),
        (GET, _err(InternalServerError, 500, "InternalError"), False, "backoff"),
        (GET, _err(CacheUnavailableError, 503, "CacheUnavailable"), False, None),
        (POST, APIConnectionError(), False, None),
        (POST, APIConnectionError(), True, "backoff"),
        (GET, APIConnectionError(), False, "backoff"),
        (GET, _err(TmailorError, 400, "InvalidField"), False, None),
    ],
)
def test_retry_delay_rules(spec: RequestSpec, err: TmailorError, connect: bool, expect: Any) -> None:
    d = retry_delay(spec, 0, error=err, max_retries=2, max_retry_wait=60, connect_failed=connect)
    if expect == "backoff":
        assert d is not None and 0.25 <= d <= 0.5
    else:
        assert d == expect


def test_retry_delay_stops_at_max() -> None:
    e = _err(RateLimitedError, 429, "RateLimited", 1)
    assert retry_delay(GET, 2, error=e, max_retries=2, max_retry_wait=60) is None
    assert retry_delay(GET, 1, error=e, max_retries=2, max_retry_wait=60) == 1.0


def test_backoff_bounds() -> None:
    for attempt in range(10):
        for _ in range(50):
            d = backoff(attempt)
            ceiling = min(_core.BACKOFF_MAX, _core.BACKOFF_BASE * 2**attempt)
            assert ceiling / 2 <= d <= ceiling


def test_retry_after_header_beats_body_and_negative_clamped() -> None:
    assert _core.retry_after_seconds({"Retry-After": "9"}, {"retry_after": 3}) == 9
    assert _core.retry_after_seconds({}, {"retry_after": 3}) == 3
    assert _core.retry_after_seconds({"Retry-After": "-4"}, {}) == 0
    assert _core.retry_after_seconds({"Retry-After": "soon"}, {"retry_after": True}) is None


# ── wait plan
def test_wait_plan_growth_and_cap() -> None:
    p = WaitPlan(1000, None, 0.0)
    delays = [p.next_delay(0.0, 0.0, None) for _ in range(8)]
    assert delays[:4] == [1.0, 1.5, 2.25, 3.375]
    assert max(d for d in delays if d is not None) == 10.0


def test_wait_plan_deadline() -> None:
    # Review 17e SDK-01: the last poll starts at least LAST_POLL_RESERVE (1 s) before the deadline.
    p = WaitPlan(5, 3, 0.0)
    assert p.next_delay(0.0, 0.0, None) == 3.0
    assert p.next_delay(3.0, 3.0, None) == 1.0
    assert p.next_delay(4.0, 4.0, None) is None
    assert p.next_delay(5.0, 5.0, None) is None
    assert p.rate_limit_delay(3.5, 1) is None
    assert p.rate_limit_delay(0.0, 2) == 2.0
    assert p.rate_limit_delay(0.0, 4) == 4.0
    assert p.rate_limit_delay(0.0, 5) is None


def test_wait_plan_spreads_low_remaining() -> None:
    p = WaitPlan(1000, None, 0.0)
    rl = _core.RateLimitInfo(limit=30, remaining=4, reset=40)
    # 4 left, keep 2 => 2 polls over 40.5 s
    assert p.next_delay(0.0, 0.0, rl) == pytest.approx(40.5 / 2)


def test_wait_plan_validation() -> None:
    with pytest.raises(ValueError):
        WaitPlan(-1, None, 0)
    with pytest.raises(ValueError):
        WaitPlan(10, 0, 0)


# ── client configuration
def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMAILOR_API_KEY", KEY)
    monkeypatch.setenv("TMAILOR_BASE_URL", "https://example.test/")
    with pytest.raises(tmailor.BaseURLError):
        tmailor.Client()  # Review 17f SDK-12: another host needs the explicit flag, also from the environment
    c = tmailor.Client(allow_untrusted_base_url=True)
    assert c.base_url == "https://example.test"
    c.close()
    monkeypatch.delenv("TMAILOR_BASE_URL")
    c = tmailor.Client()
    assert c.base_url == "https://open-api.tmailor.com"
    c.close()


# ── Review 17f SDK-12: base URL validation
@pytest.mark.parametrize(
    ("url", "allow", "expect"),
    [
        ("https://open-api.tmailor.com", False, "https://open-api.tmailor.com"),
        ("HTTPS://Open-API.tmailor.com:443/", False, "https://open-api.tmailor.com:443"),
        ("https://open-api.tmailor.com/v0/", False, "https://open-api.tmailor.com/v0"),
        ("https://staging.example.com", True, "https://staging.example.com"),
        ("http://127.0.0.1:8080", True, "http://127.0.0.1:8080"),
        ("http://[::1]:8080/", True, "http://[::1]:8080"),
        ("http://localhost:9000", True, "http://localhost:9000"),
        ("https://staging.example.com", False, None),
        ("https://open-api.tmailor.com.evil.example", False, None),
        ("https://open-api.tmailor.com:8443", False, None),
        ("http://open-api.tmailor.com", False, None),
        ("http://open-api.tmailor.com", True, None),
        ("http://example.com", True, None),
        ("http://127.0.0.1:8080", False, None),
        ("https://user:pass@open-api.tmailor.com", True, None),
        ("https://open-api.tmailor.com@evil.example", True, None),
        ("https://open-api.tmailor.com/?x=1", True, None),
        ("https://open-api.tmailor.com/#frag", True, None),
        ("https://open-api.tmailor.com/a b", True, None),
        ("https://open-api.tmailor.com\r\nX: y", True, None),
        ("ftp://open-api.tmailor.com", True, None),
        ("open-api.tmailor.com", True, None),
        ("https://", True, None),
        ("https://h:99999", True, None),
    ],
)
def test_check_base_url(url: str, allow: bool, expect: Optional[str]) -> None:
    if expect is None:
        with pytest.raises(tmailor.BaseURLError):
            _core.check_base_url(url, allow_untrusted=allow)
    else:
        assert _core.check_base_url(url, allow_untrusted=allow) == expect


async def test_async_client_checks_base_url() -> None:
    with pytest.raises(ValueError):
        tmailor.AsyncClient(KEY, base_url="https://evil.example")
    c = tmailor.AsyncClient(KEY, base_url="https://evil.example", allow_untrusted_base_url=True)
    await c.aclose()


def test_missing_api_key_is_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review 17d SDK-07: building a client without a key is fine; health works and sends no Authorization;
    # an authenticated call raises MissingKeyError before any request is sent.
    monkeypatch.delenv("TMAILOR_API_KEY", raising=False)
    sent: List[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"status": "ok", "time": 1, "version": "v1"})

    c = tmailor.Client(
        base_url="https://x.test", allow_untrusted_base_url=True, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert "api_key=None" in repr(c)
    assert c.health().status == "ok"
    assert len(sent) == 1 and "authorization" not in sent[0].headers
    with pytest.raises(tmailor.MissingKeyError) as ei:
        c.me()
    assert ei.value.code == "MissingKey" and ei.value.status is None
    with pytest.raises(tmailor.MissingKeyError):
        c.mailboxes.create()
    assert len(sent) == 1
    c.close()


async def test_missing_api_key_async(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY", raising=False)
    sent: List[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"status": "ok", "time": 1, "version": "v1"})

    async with tmailor.AsyncClient(
        api_key="  ",
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as c:
        assert (await c.health()).status == "ok"
        assert "authorization" not in sent[0].headers
        with pytest.raises(tmailor.MissingKeyError):
            await c.usage()
    assert len(sent) == 1


def test_health_never_sends_key() -> None:
    seen: Dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json={"status": "ok", "time": 1, "version": "v1"})

    with tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as c:
        c.health()
    assert "authorization" not in seen


def test_invalid_args() -> None:
    with pytest.raises(ValueError):
        tmailor.Client(KEY, max_retries=-1)
    c = tmailor.Client(KEY)
    with pytest.raises(ValueError):
        c.mailbox("")
    with pytest.raises(ValueError):
        _ops.mailboxes_create(idempotency_key="has space")
    with pytest.raises(ValueError):
        _ops.mailboxes_create(idempotency_key="x" * 129)
    with pytest.raises(ValueError):
        _ops.mailboxes_list(size=101)
    with pytest.raises(ValueError):
        _ops.mailbox_message("t", "")
    with pytest.raises(ValueError):
        _ops.unix_time(dt.datetime(2026, 1, 1))  # naive datetime
    with pytest.raises(TypeError):
        _ops.unix_time(True)
    with pytest.raises(TypeError):
        _ops.usage(from_=20261001)  # type: ignore[arg-type]
    c.close()


def test_since_and_dates_conversion() -> None:
    aware = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
    spec = _ops.mailbox_messages("t", since=aware)
    assert json.loads(spec.content or b"{}")["since"] == int(aware.timestamp())
    u = _ops.usage(from_=dt.date(2026, 10, 1), to=dt.datetime(2026, 10, 2, 5))
    assert u.query == {"from": "2026-10-01", "to": "2026-10-02"}


def test_no_token_mailbox() -> None:
    c = tmailor.Client(KEY)
    mb = tmailor.Mailbox(c, data=_mbox(None))
    with pytest.raises(TmailorError) as ei:
        mb.info()
    assert ei.value.code == "NoToken"
    c.close()


def _mbox(token: Optional[str], **extra: Any) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "email": "a@b.c",
        "domain": "b.c",
        "is_user_domain": False,
        "custom_name": False,
        "created_at": 1,
        "token": token,
        "web": None
        if token is None
        else {
            "accesstoken": token,
            "transfertoken": "eyJ.SECRET-TRANSFER.sig",
            "reuse_url": "https://tmailor.com/r#accesstoken/" + token,
        },
    }
    d.update(extra)
    return d


# ── redaction: api key and tokens never in repr/str/errors/logs
def _recording_client(responses: List[httpx.Response]) -> tmailor.Client:
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        return next(it)

    return tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def test_redaction_everywhere(caplog: pytest.LogCaptureFixture) -> None:
    web = {
        "accesstoken": TOKEN,
        "transfertoken": "eyJ.SECRET-TRANSFER.sig",
        "reuse_url": "https://tmailor.com/r#accesstoken/" + TOKEN,
    }
    body: Dict[str, Any] = {
        "email": "a@b.c",
        "domain": "b.c",
        "is_user_domain": False,
        "custom_name": False,
        "created_at": 1,
        "token": TOKEN,
        "web": web,
    }
    caplog.set_level(logging.DEBUG)
    c = _recording_client(
        [
            httpx.Response(201, json=body),
            httpx.Response(
                404,
                json={"error": {"code": "MailboxNotFound", "message": "Mailbox not found", "description": "", "request_id": "r"}},
            ),
        ]
    )
    mb = c.mailboxes.create()
    with pytest.raises(tmailor.MailboxNotFoundError) as ei:
        mb.info()
    texts = [
        repr(c),
        str(c),
        repr(mb),
        str(mb),
        repr(mb.web),
        str(ei.value),
        repr(ei.value),
        str(ei.value.body),
        str(ei.value.headers),
        caplog.text,
        str(ei.value.__cause__),
        str(ei.value.__context__),
    ]
    joined = "\n".join(texts)
    assert "SUPERSECRET" not in joined
    assert "SECRET-ADDRESS-TOKEN" not in joined
    assert "SECRET-TRANSFER" not in joined
    assert "tmk_live_abcdefabcdef_***" in repr(c)
    assert mb.token == TOKEN  # still usable
    c.close()


def test_mask_key_unknown_format() -> None:
    assert _core.mask_key("whatever") == "***"


def test_network_error_has_no_cause_chain() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("boom", request=req)

    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    with pytest.raises(APIConnectionError) as ei:
        c.mailbox(TOKEN).delete()
    # "raise ... from None": the httpx exception (which references the request and its headers) is not chained.
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert ei.value.status is None
    c.close()


def test_user_agent_and_headers() -> None:
    seen: Dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json={"status": "ok", "time": 1, "version": "v1"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with (
        tmailor.Client(KEY, base_url="https://x.test", allow_untrusted_base_url=True, http_client=http) as c,
        pytest.raises(tmailor.InvalidResponseError),
    ):
        c.me()  # the health body is not a Me: rejected (headers are still captured)
    assert seen["user-agent"].startswith(f"tmailor-python/{tmailor.__version__}")
    assert seen["authorization"] == f"Bearer {KEY}"
    assert seen["accept"] == "application/json"


def test_non_json_success_raises() -> None:
    c = _recording_client([httpx.Response(200, content=b"<html>", headers={"X-Request-Id": "rid"})])
    with pytest.raises(tmailor.APIError) as ei:
        c.me()
    assert ei.value.code == "InvalidResponse" and ei.value.request_id == "rid"
    c.close()


def test_request_id_from_header_when_body_missing() -> None:
    c = _recording_client([httpx.Response(418, content=b"", headers={"X-Request-Id": "abc"})])
    with pytest.raises(tmailor.APIError) as ei:
        c.me()
    assert ei.value.request_id == "abc" and ei.value.code == "HTTP418"
    c.close()


def test_mailboxes_iter_and_page_helpers() -> None:
    page = {"items": [_mbox("t1")], "next_cursor": None}
    c = _recording_client([httpx.Response(200, json=page), httpx.Response(200, json=page)])
    assert [m.email for m in c.mailboxes] == ["a@b.c"]
    p = c.mailboxes.list_page()
    assert p.next_cursor is None and p.items[0].token == "t1"
    c.close()


# ── Review 17d SDK-06: strict response parsing
ME_OK: Dict[str, Any] = {
    "user_id": 12,
    "key": {"key_id": 1, "prefix": "abcdefabcdef"},
    "client_ip": "203.0.113.10",
    "limits": {
        "quota_total": 1,
        "quota_used": 0,
        "quota_remaining": 1,
        "mailbox_count": 0,
        "rate_per_min": 30,
        "daily_req_cap": 0,
        "max_ip": 1,
    },
    "ip_allowlist": ["203.0.113.10"],
    "subscriptions": [{"plan": "free", "expires_at": 0, "period_end": 0, "quota": 100, "used": 0}],
}


def _bad(d: Dict[str, Any], path: List[str], value: Any) -> Dict[str, Any]:
    out = json.loads(json.dumps(d))
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    if value is _DROP:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return out


_DROP = object()


def test_models_strict_ok() -> None:
    me = tmailor.Me.from_api(ME_OK)
    assert me.user_id == 12 and me.limits.max_ip == 1 and me.subscriptions[0].quota == 100
    assert len(tmailor.DomainList(items=[], shared_domains_available=False)) == 0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["user_id"], "12"),  # no int("12")
        (["user_id"], True),  # bool is not an integer
        (["user_id"], 1.0),  # no float coercion
        (["user_id"], None),  # not nullable
        (["key"], _DROP),  # required
        (["limits", "max_ip"], "x"),
        (["ip_allowlist"], None),
        (["ip_allowlist"], [1]),
        (["subscriptions"], [{"plan": "free"}]),
    ],
)
def test_models_strict_reject(path: List[str], value: Any) -> None:
    from tmailor._schema import SchemaError

    with pytest.raises(SchemaError) as ei:
        tmailor.Me.from_api(_bad(ME_OK, path, value))
    # The message names the field, never the value.
    assert path[-1] in str(ei.value)
    if isinstance(value, str):
        assert value not in str(ei.value).replace(path[-1], "")


def test_invalid_2xx_body_never_kept_in_error() -> None:
    body = _mbox(TOKEN, created_at="yesterday")
    c = _recording_client([httpx.Response(200, json=body, headers={"X-Request-Id": "rid-x"})])
    with pytest.raises(tmailor.InvalidResponseError) as ei:
        c.mailbox(TOKEN).info()
    e = ei.value
    assert e.code == "InvalidResponse" and e.status == 200 and e.request_id == "rid-x"
    assert "created_at" in e.message
    assert e.__cause__ is None and e.__context__ is None and e.body == {}
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert "SECRET" not in rendered and "yesterday" not in rendered
    c.close()


@pytest.mark.parametrize(
    "body",
    [
        _mbox("eyJ.TOKEN-A.sig", web=None),  # token without web
        _mbox(None, web={"accesstoken": "a", "transfertoken": "b", "reuse_url": "c"}),  # web without token
        _mbox("eyJ.TOKEN-A.sig", web={"accesstoken": "eyJ.OTHER.sig", "transfertoken": "b", "reuse_url": "c"}),
        _mbox("eyJ.TOKEN-A.sig", message_count=None),  # present but null
        _mbox("eyJ.TOKEN-A.sig", is_user_domain="false"),  # "false" is not False
    ],
)
def test_mailbox_schema_relations(body: Dict[str, Any]) -> None:
    c = _recording_client([httpx.Response(201, json=body)])
    with pytest.raises(tmailor.InvalidResponseError):
        c.mailboxes.create()
    c.close()


# ── Review 17d SDK-04: malformed JSON on a 2xx response
@pytest.mark.parametrize("status", [200, 201])
def test_malformed_success_json_has_no_chain(status: int) -> None:
    cut = (
        '{"email":"a@b.c","api_key":"' + KEY + '","token":"' + TOKEN + '","web":{"transfertoken":"eyJ.SECRET-TRANSFER.sig"'
    ).encode()
    c = _recording_client([httpx.Response(status, content=cut, headers={"X-Request-Id": "rid-cut"})])
    with pytest.raises(tmailor.InvalidResponseError) as ei:
        if status == 201:
            c.mailboxes.create()
        else:
            c.mailbox(TOKEN).info()
    e = ei.value
    assert e.code == "InvalidResponse" and e.status == status and e.request_id == "rid-cut"
    # Neither the decoder error (its .doc is the whole body) nor anything else is chained.
    assert e.__cause__ is None and e.__context__ is None and e.__suppress_context__
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    texts = "\n".join([rendered, str(e), repr(e), str(e.body), str(e.headers), str(vars(e))])
    for secret in ("SUPERSECRET", "SECRET-ADDRESS-TOKEN", "SECRET-TRANSFER"):
        assert secret not in texts
    c.close()


async def test_malformed_success_json_async() -> None:
    cut = ('{"token":"' + TOKEN).encode()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cut)

    async with tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as c:
        with pytest.raises(tmailor.InvalidResponseError) as ei:
            await c.mailbox(TOKEN).info()
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    assert "SECRET-ADDRESS-TOKEN" not in "".join(traceback.format_exception(type(ei.value), ei.value, ei.value.__traceback__))


# ── Review 17d SDK-01: deadlines and Retry-After <= 0
def test_within_deadline() -> None:
    assert _core.within_deadline(None, 0, 10) is None
    assert _core.within_deadline(3.0, 0, None) == 3.0
    assert _core.within_deadline(3.0, 6.0, 10) == 3.0
    assert _core.within_deadline(4.0, 6.0, 10) is None


def test_retry_after_zero_gets_min_delay() -> None:
    e = _err(RateLimitedError, 429, "RateLimited", 0)
    assert retry_delay(GET, 0, error=e, max_retries=2, max_retry_wait=60) == _core.MIN_RETRY_AFTER
    e5 = _err(InternalServerError, 503, "HTTP503", 0)
    assert retry_delay(GET, 0, error=e5, max_retries=2, max_retry_wait=60) == _core.MIN_RETRY_AFTER
    p = WaitPlan(10, None, 0.0)
    assert p.rate_limit_delay(0.0, 0) == _core.MIN_RETRY_AFTER


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    async def asleep(self, s: float) -> None:
        self.sleep(s)


def _poll_429(retry_after: int, calls: List[int]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        err = {"error": {"code": "RateLimited", "message": "m", "description": "", "request_id": "r", "retry_after": retry_after}}
        return httpx.Response(429, json=err, headers={"Retry-After": str(retry_after)})

    return handler


@pytest.mark.parametrize(
    ("timeout", "retry_after", "max_calls", "max_slept"), [(5, 30, 1, 0.0), (3, 0, 3, 2.0), (10, -5, 10, 9.0)]
)
def test_wait_429_bounded_sync(timeout: float, retry_after: int, max_calls: int, max_slept: float) -> None:
    calls: List[int] = []
    clock = _Clock()
    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(_poll_429(retry_after, calls))),
    )
    c._sleep, c._monotonic, c._time = clock.sleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    with pytest.raises(tmailor.WaitTimeoutError):
        c.mailbox(TOKEN).wait_for_message(timeout=timeout, since=1)
    assert len(calls) <= max_calls and sum(clock.sleeps) <= max_slept and all(s > 0 for s in clock.sleeps)
    assert clock.now <= timeout
    c.close()


@pytest.mark.parametrize(("timeout", "retry_after"), [(5, 30), (3, 0)])
async def test_wait_429_bounded_async(timeout: float, retry_after: int) -> None:
    calls: List[int] = []
    clock = _Clock()
    c = tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_poll_429(retry_after, calls))),
    )
    c._sleep, c._monotonic, c._time = clock.asleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    with pytest.raises(tmailor.WaitTimeoutError):
        await c.mailbox(TOKEN).wait_for_message(timeout=timeout)  # 429 already on the baseline
    assert clock.now <= timeout and all(s > 0 for s in clock.sleeps)
    await c.aclose()


def test_wait_5xx_retry_never_passes_deadline() -> None:
    calls: List[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            503,
            json={"error": {"code": "HTTP503", "message": "m", "description": "", "request_id": "r"}},
            headers={"Retry-After": "30"},
        )

    clock = _Clock()
    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    c._sleep, c._monotonic, c._time = clock.sleep, (lambda: clock.now), (lambda: clock.now)
    with pytest.raises(tmailor.ServiceUnavailableError):
        c.mailbox(TOKEN).wait_for_message(timeout=5, since=1)
    assert calls == [1] and clock.sleeps == []  # a 30 s retry does not fit in 5 s: raised, not slept
    c.close()


# ── Review 17d SDK-02: repeated cursors (unit level; cycles are in the conformance scenarios)
def test_check_cursor() -> None:
    seen: set = set()
    _core.check_cursor(seen, "CURSOR-ONE")
    _core.check_cursor(seen, "CURSOR-TWO")
    with pytest.raises(tmailor.InvalidResponseError) as ei:
        _core.check_cursor(seen, "CURSOR-ONE")
    assert ei.value.code == "InvalidResponse" and "CURSOR" not in str(ei.value)


async def test_async_context_manager_and_repr() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "shared_domains_available": True})

    async with tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as c:
        d = await c.domains()
        assert d.shared_domains_available and list(d) == []
        assert "SUPERSECRET" not in repr(c)
        mb = c.mailbox(TOKEN)
        assert "SECRET-ADDRESS-TOKEN" not in repr(mb)


async def test_async_owned_client_closes() -> None:
    c = tmailor.AsyncClient(KEY)
    await c.aclose()
    assert c._http.is_closed


# ── Review 17e SDK-01: the wait deadline also bounds the HTTP requests (slow fake transport)
EMPTY_PAGE: Dict[str, Any] = {"items": [], "next_cursor": None, "total": 0}
MSG_ITEM: Dict[str, Any] = {
    "id": "0" * 31 + "2",
    "from": {"name": "Shop", "address": "no-reply@shop.example"},
    "to": "a@b.c",
    "subject": "Code",
    "received_at": 1760342401,
    "expires_at": 1760428801,
    "seen": False,
}


class _SlowServer:
    """Each response takes ``delays[i]`` seconds on the fake clock; a smaller request timeout cuts it (ReadTimeout)."""

    def __init__(self, clock: _Clock, steps: List[Any]) -> None:
        self.clock = clock
        self.steps = steps
        self.timeouts: List[float] = []
        self.paths: List[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        delay, status, body = self.steps[len(self.paths)]
        self.paths.append(req.url.path)
        limit = req.extensions["timeout"]["read"]
        self.timeouts.append(limit)
        if delay > limit:
            self.clock.now += limit
            raise httpx.ReadTimeout("slow", request=req)
        self.clock.now += delay
        return httpx.Response(status, json=body)


def _slow_sync(steps: List[Any], timeout: float = 30.0) -> Any:
    clock = _Clock()
    server = _SlowServer(clock, steps)
    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        timeout=timeout,
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    c._sleep, c._monotonic, c._time = clock.sleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    return c, clock, server


def _slow_async(steps: List[Any], timeout: float = 30.0) -> Any:
    clock = _Clock()
    server = _SlowServer(clock, steps)
    c = tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        timeout=timeout,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    c._sleep, c._monotonic, c._time = clock.asleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    return c, clock, server


SLOW_CASES = [
    # (wait timeout, server steps, expected request timeouts, expected paths)
    (3, [(10, 200, EMPTY_PAGE)], [3.0], ["/v1/mailbox/messages"]),  # slow poll cut at the deadline
    (
        5,
        [(2, 200, {"items": [MSG_ITEM], "next_cursor": None, "total": 1}), (10, 200, {})],
        [5.0, 3.0],
        ["/v1/mailbox/messages", "/v1/mailbox/message"],
    ),  # slow final read cut at the deadline
    (0, [], [], []),  # timeout 0: nothing is sent
]


@pytest.mark.parametrize(("timeout", "steps", "timeouts", "paths"), SLOW_CASES)
def test_wait_deadline_bounds_requests_sync(timeout: float, steps: List[Any], timeouts: List[float], paths: List[str]) -> None:
    c, clock, server = _slow_sync(steps)
    with pytest.raises(tmailor.WaitTimeoutError) as ei:
        c.mailbox(TOKEN).wait_for_message(timeout=timeout, since=1)
    assert ei.value.code == "WaitTimeout" and ei.value.__cause__ is None
    assert server.paths == paths and server.timeouts == pytest.approx(timeouts)
    assert clock.now <= timeout + 1e-9
    c.close()


@pytest.mark.parametrize(("timeout", "steps", "timeouts", "paths"), SLOW_CASES)
async def test_wait_deadline_bounds_requests_async(
    timeout: float, steps: List[Any], timeouts: List[float], paths: List[str]
) -> None:
    c, clock, server = _slow_async(steps)
    with pytest.raises(tmailor.WaitTimeoutError):
        await c.mailbox(TOKEN).wait_for_message(timeout=timeout, since=1)
    assert server.paths == paths and server.timeouts == pytest.approx(timeouts)
    assert clock.now <= timeout + 1e-9
    await c.aclose()


def test_wait_uses_client_timeout_when_deadline_is_far() -> None:
    # 120 s wait, 30 s client timeout: the poll keeps the client timeout; a real timeout there is APITimeoutError
    # (retried for the safe list request), not a WaitTimeoutError.
    body_ok = {"items": [MSG_ITEM], "next_cursor": None, "total": 1}
    full = dict(MSG_ITEM, seen=True, text="Your code is 123456", html=None, body_available=True)
    c, clock, server = _slow_sync([(31, 200, body_ok), (1, 200, body_ok), (1, 200, full)], timeout=30)
    msg = c.mailbox(TOKEN).wait_for_message(timeout=120, since=1)
    assert msg.otp() == "123456"
    assert server.timeouts[0] == 30.0 and len(server.paths) == 3
    assert clock.now <= 120
    c.close()


def test_slow_request_outside_wait_is_api_timeout() -> None:
    c, _clock, server = _slow_sync([(31, 200, {})], timeout=30)
    c.max_retries = 0
    with pytest.raises(tmailor.APITimeoutError):
        c.mailbox(TOKEN).info()
    assert server.timeouts == [30.0]
    c.close()


# ── Review 17f SDK-01: per-phase httpx timeouts are not a total deadline
class _TrickleServer(_SlowServer):
    """Sends bytes slowly enough that no single read times out: the response arrives after ``delay`` whatever the
    request timeout (the per-phase httpx timeout never fires)."""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        delay, status, body = self.steps[len(self.paths)]
        self.paths.append(req.url.path)
        self.timeouts.append(req.extensions["timeout"]["read"])
        self.clock.now += delay
        return httpx.Response(status, json=body)


def test_wait_trickle_past_deadline_sync_is_wait_timeout() -> None:
    clock = _Clock()
    server = _TrickleServer(clock, [(10, 200, {"items": [MSG_ITEM], "next_cursor": None, "total": 1})])
    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    c._sleep, c._monotonic, c._time = clock.sleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    with pytest.raises(tmailor.WaitTimeoutError) as ei:
        c.mailbox(TOKEN).wait_for_message(timeout=3, since=1)
    # The page that ended after the deadline is not used (no read of the message follows).
    assert ei.value.__cause__ is None and server.paths == ["/v1/mailbox/messages"] and server.timeouts == [3.0]
    c.close()


def test_wait_timeout_equal_to_client_timeout_is_wait_timeout() -> None:
    # remaining == client timeout: the deadline is the binding limit, so the cut request is a WaitTimeoutError.
    c, clock, server = _slow_sync([(40, 200, EMPTY_PAGE)], timeout=30)
    with pytest.raises(tmailor.WaitTimeoutError):
        c.mailbox(TOKEN).wait_for_message(timeout=30, since=1)
    assert server.timeouts == [30.0] and clock.now == 30
    c.close()


async def test_wait_outer_deadline_async_real_clock() -> None:
    import asyncio
    import time

    calls: List[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        await asyncio.sleep(5)  # ignores the httpx timeout, like a trickling server
        return httpx.Response(200, json=EMPTY_PAGE)

    async with tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as c:
        t0 = time.monotonic()
        with pytest.raises(tmailor.WaitTimeoutError) as ei:
            await c.mailbox(TOKEN).wait_for_message(timeout=0.4, since=1)
        elapsed = time.monotonic() - t0
    assert ei.value.__cause__ is None and calls == ["/v1/mailbox/messages"]
    assert elapsed < 1.5  # cancelled at the deadline, not after the 5 s response


async def test_async_trickle_past_deadline_fake_clock() -> None:
    clock = _Clock()
    server = _TrickleServer(clock, [(10, 200, EMPTY_PAGE)])
    c = tmailor.AsyncClient(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    c._sleep, c._monotonic, c._time = clock.asleep, (lambda: clock.now), (lambda: 1760342400 + clock.now)
    with pytest.raises(tmailor.WaitTimeoutError):
        await c.mailbox(TOKEN).wait_for_message(timeout=3, since=1)
    await c.aclose()


# ── Contract 2026-10-17: the Open API only creates addresses and checks / reads their messages
@pytest.mark.parametrize(
    ("obj", "name"),
    [
        (tmailor.Client, "domain_check"),
        (tmailor.AsyncClient, "domain_check"),
        (tmailor.Mailbox, "rotate_token"),
        (tmailor.AsyncMailbox, "rotate_token"),
        (tmailor.Mailbox, "mark_seen"),
        (tmailor.AsyncMailbox, "mark_seen"),
        (_ops, "domain_check"),
        (_ops, "mailbox_rotate"),
        (_ops, "mailbox_message_seen"),
        (tmailor, "DomainCheck"),
        (tmailor, "ExpectedMx"),
    ],
)
def test_removed_operations_are_gone(obj: Any, name: str) -> None:
    assert not hasattr(obj, name)


def test_read_message_marks_it_as_read() -> None:
    sent: List[httpx.Request] = []
    body = dict(MSG_ITEM, seen=True, text="Code 482913", html=None, body_available=True)

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json=body)

    c = tmailor.Client(
        KEY,
        base_url="https://x.test",
        allow_untrusted_base_url=True,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    m = c.mailbox(TOKEN).message(MSG_ITEM["id"])
    assert m.seen is True and m.otp() == "482913"
    assert [r.url.path for r in sent] == ["/v1/mailbox/message"]  # one request, no separate "seen" call
    assert _ops.mailbox_message(TOKEN, MSG_ITEM["id"]).safe  # marking as read is idempotent: still retried
    c.close()


def test_name_used_on_website_is_mailbox_exists() -> None:
    msg = "This address was created on the website or mobile app and cannot be used through the API. Pick another name"
    err = {"error": {"code": "MailboxExists", "message": msg, "description": "d", "request_id": "r"}}
    c = _recording_client([httpx.Response(409, json=err)])
    with pytest.raises(tmailor.MailboxExistsError) as ei:
        c.mailboxes.create(domain="mail.example.com", name="signup")
    assert isinstance(ei.value, tmailor.ConflictError) and ei.value.status == 409 and ei.value.message == msg
    c.close()
