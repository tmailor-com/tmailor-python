"""Runs one shared conformance scenario (../../conformance/scenarios/*.json) against the Python SDK.

Format: ../../conformance/README.md. Every request the SDK sends must match the next exchange; the
scenario fails if a request is unexpected, differs, or if an exchange is left unused.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import httpx

import tmailor
from tmailor import errors as E


def _scenario_dir() -> Path:
    """Review 17d SDK-10: stable locations, first match wins.

    1. ``$TMAILOR_CONFORMANCE_DIR`` (explicit override);
    2. ``<package root>/conformance/scenarios``: the source distribution ships them there;
    3. ``<repo>/open-sdk/conformance/scenarios``: the shared copy next to every SDK in the repository.
    A missing directory is not a skip: test_scenarios_found fails.
    """
    env = os.environ.get("TMAILOR_CONFORMANCE_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for base in (here.parents[1], here.parents[2]):
        cand = base / "conformance" / "scenarios"
        if cand.is_dir():
            return cand
    return here.parents[1] / "conformance" / "scenarios"


SCENARIO_DIR = _scenario_dir()
# Number of scenario files shipped with this SDK version; a smaller count means fixtures were lost.
# 2026-10-17: -9 (domain check, rotate, mark seen removed), +1 name used on website, +1 seen=false on read (Review 17q)
EXPECTED_SCENARIOS = 79
DEFAULT_KEY = "tmk_live_c0nf0rmance01_TESTSECRETdoNotUseAnywhere0000000000000"
BASE_URL = "https://open-api.test"

ERROR_CLASSES: Dict[str, Type[Exception]] = {
    "MissingKey": E.MissingKeyError,
    "InvalidKey": E.InvalidKeyError,
    "KeyRevoked": E.KeyRevokedError,
    "AccountLocked": E.AccountLockedError,
    "IpNotAllowed": E.IpNotAllowedError,
    "InvalidJson": E.InvalidJsonError,
    "InvalidField": E.InvalidFieldError,
    "InvalidDomain": E.InvalidDomainError,
    "CustomNameNotAllowed": E.CustomNameNotAllowedError,
    "InvalidName": E.InvalidNameError,
    "QuotaExceeded": E.QuotaExceededError,
    "MailboxNotFound": E.MailboxNotFoundError,
    "MessageNotFound": E.MessageNotFoundError,
    "NotFound": E.NotFoundError,
    "MailboxExists": E.MailboxExistsError,
    "Conflict": E.ConflictError,
    "RateLimited": E.RateLimitedError,
    "DailyCapReached": E.DailyCapReachedError,
    "InternalServerError": E.InternalServerError,
    "ServiceUnavailable": E.ServiceUnavailableError,
    "NoDomainAvailable": E.NoDomainAvailableError,
    "IdempotencyUnavailable": E.IdempotencyUnavailableError,
    "CacheUnavailable": E.CacheUnavailableError,
    "BadRequest": E.BadRequestError,
    "InvalidResponse": E.InvalidResponseError,
    "ConnectionError": E.APIConnectionError,
    "Timeout": E.APITimeoutError,
    "WaitTimeout": E.WaitTimeoutError,
}

PY_ARG = {"from": "from_"}


def load_scenarios() -> List[Dict[str, Any]]:
    return [json.loads(p.read_text("utf-8")) for p in sorted(SCENARIO_DIR.glob("*.json"))]


class FakeClock:
    def __init__(self, wall_start: float) -> None:
        self.now = 0.0
        self.wall_start = wall_start
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall_start + self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    async def asleep(self, s: float) -> None:
        self.sleep(s)


class Replay:
    """httpx transport handler that checks each request against the scenario."""

    def __init__(self, scenario: Dict[str, Any]) -> None:
        self.sid = scenario["id"]
        self.exchanges = scenario["exchanges"]
        self.i = 0
        self.captures: Dict[str, str] = {}
        self.clock: Optional[FakeClock] = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert self.i < len(self.exchanges), f"{self.sid}: unexpected extra request {request.method} {request.url}"
        ex = self.exchanges[self.i]
        self.i += 1
        exp = ex["request"]
        n = self.i
        assert request.method == exp["method"], f"{self.sid}#{n}: method {request.method} != {exp['method']}"
        assert request.url.path == exp["path"], f"{self.sid}#{n}: path {request.url.path} != {exp['path']}"
        query = dict(
            urllib.parse.parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query)
        )
        assert query == exp.get("query", {}), f"{self.sid}#{n}: query {query} != {exp.get('query')}"
        for name, matcher in (exp.get("headers") or {}).items():
            self._check_header(n, name, request.headers.get(name), matcher)
        body = request.content
        if exp.get("json") is None:
            assert body in (b"", None), f"{self.sid}#{n}: expected no body, got {body!r}"
        else:
            got = json.loads(body)
            assert got == exp["json"], f"{self.sid}#{n}: body {got} != {exp['json']}"
        # A token must never appear in the URL.
        assert "token" not in query, f"{self.sid}#{n}: token in query string"
        resp = ex["response"]
        # Review 17e SDK-01: "delay" = seconds the server takes. The fake clock advances by it, or by the request's
        # timeout (raising a read timeout) when the SDK gave the request less time than that.
        delay = resp.get("delay")
        if delay is not None and self.clock is not None:
            limit = (request.extensions.get("timeout") or {}).get("read")
            if limit is not None and delay > limit:
                self.clock.now += limit
                raise httpx.ReadTimeout("read timed out", request=request)
            self.clock.now += delay
        if "network_error" in resp:
            kind = resp["network_error"]
            if kind == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if kind == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            raise httpx.ReadError("connection reset", request=request)
        headers = dict(resp.get("headers") or {})
        if "json" in resp:
            content = json.dumps(resp["json"]).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            content = str(resp.get("body", "")).encode()
        return httpx.Response(resp["status"], headers=headers, content=content)

    def _check_header(self, n: int, name: str, got: Optional[str], m: Any) -> None:
        where = f"{self.sid}#{n}: header {name}"
        if isinstance(m, str):
            assert got == m, f"{where}: {got!r} != {m!r}"
        elif m.get("$absent"):
            assert got is None, f"{where} must be absent"
        elif m.get("$present"):
            assert got, f"{where} must be present"
        elif "$regex" in m:
            assert got is not None and re.search(m["$regex"], got), f"{where}: {got!r} !~ {m['$regex']}"
        elif "$capture" in m:
            assert got, f"{where} must be present"
            self.captures[m["$capture"]] = got
        elif "$equals" in m:
            assert got == self.captures[m["$equals"]], f"{where}: {got!r} differs from captured value"
        else:  # pragma: no cover
            raise AssertionError(f"{where}: unknown matcher {m}")

    def assert_done(self) -> None:
        assert self.i == len(self.exchanges), f"{self.sid}: {len(self.exchanges) - self.i} exchange(s) not used"


def to_plain(v: Any) -> Any:
    """SDK result -> JSON-like value with the API field names."""
    if isinstance(v, tmailor.models.MailboxData):
        d = dict(
            email=v.email,
            domain=v.domain,
            is_user_domain=v.is_user_domain,
            custom_name=v.custom_name,
            created_at=v.created_at,
            token=v.token,
            web=to_plain(v.web),
        )
        if v.message_count is not None:
            d["message_count"] = v.message_count
        return d
    if isinstance(v, tmailor.DomainList):
        return {"items": [to_plain(x) for x in v.items], "shared_domains_available": v.shared_domains_available}
    if is_dataclass(v) and not isinstance(v, type):
        out = {}
        for f in fields(v):
            if f.name == "raw":
                continue
            out["from" if f.name == "from_" else f.name] = to_plain(getattr(v, f.name))
        return out
    if isinstance(v, list):
        return [to_plain(x) for x in v]
    return v


def assert_subset(sid: str, exp: Any, got: Any, path: str = "$") -> None:
    if isinstance(exp, dict):
        assert isinstance(got, dict), f"{sid}: {path} expected object, got {got!r}"
        for k, v in exp.items():
            assert k in got, f"{sid}: {path}.{k} missing in {sorted(got)}"
            assert_subset(sid, v, got[k], f"{path}.{k}")
    elif isinstance(exp, list):
        assert isinstance(got, list) and len(got) == len(exp), f"{sid}: {path} expected {len(exp)} items, got {got!r}"
        for i, (a, b) in enumerate(zip(exp, got)):
            assert_subset(sid, a, b, f"{path}[{i}]")
    else:
        assert exp == got, f"{sid}: {path} {got!r} != {exp!r}"


def _args(scn: Dict[str, Any]) -> Dict[str, Any]:
    return {PY_ARG.get(k, k): v for k, v in (scn["call"].get("args") or {}).items()}


def make_sync(scn: Dict[str, Any]) -> Tuple[tmailor.Client, Replay, FakeClock]:
    replay = Replay(scn)
    cfg = scn.get("client") or {}
    http = httpx.Client(transport=httpx.MockTransport(replay))
    client = tmailor.Client(
        cfg.get("api_key", DEFAULT_KEY),
        base_url=BASE_URL,
        allow_untrusted_base_url=True,  # Review 17f SDK-12: fake host
        http_client=http,
        max_retries=cfg.get("max_retries", 2),
        max_retry_wait=cfg.get("max_retry_wait", 60),
        timeout=cfg.get("timeout", 30.0),
    )
    clock = FakeClock((scn.get("clock") or {}).get("wall_start", 1760342400))
    client._sleep = clock.sleep
    replay.clock = clock
    client._monotonic = clock.monotonic
    client._time = clock.time
    return client, replay, clock


def make_async(scn: Dict[str, Any]) -> Tuple[tmailor.AsyncClient, Replay, FakeClock]:
    replay = Replay(scn)
    cfg = scn.get("client") or {}
    http = httpx.AsyncClient(transport=httpx.MockTransport(replay))
    client = tmailor.AsyncClient(
        cfg.get("api_key", DEFAULT_KEY),
        base_url=BASE_URL,
        allow_untrusted_base_url=True,  # Review 17f SDK-12: fake host
        http_client=http,
        max_retries=cfg.get("max_retries", 2),
        max_retry_wait=cfg.get("max_retry_wait", 60),
        timeout=cfg.get("timeout", 30.0),
    )
    clock = FakeClock((scn.get("clock") or {}).get("wall_start", 1760342400))
    client._sleep = clock.asleep
    replay.clock = clock
    client._monotonic = clock.monotonic
    client._time = clock.time
    return client, replay, clock


def call_sync(client: tmailor.Client, scn: Dict[str, Any]) -> Tuple[Any, Any, Optional[BaseException]]:
    """Returns (result, mailbox object or None, error or None)."""
    op = scn["call"]["op"]
    a = _args(scn)
    mb: Any = None
    try:
        if op == "mailboxes.create":
            mb = client.mailboxes.create(**a)
            return mb, mb, None
        if op == "mailboxes.list":
            return list(client.mailboxes.list(**a)), None, None
        if op.startswith("mailbox."):
            mb = client.mailbox(scn["call"]["token"])
            res = getattr(mb, op.split(".", 1)[1])(**a)
            if op == "mailbox.messages":
                res = list(res)
            then = scn["call"].get("then")
            if then:
                getattr(mb, then.split(".", 1)[1])()
            return res, mb, None
        return getattr(client, op)(**a), None, None
    except AssertionError:
        raise
    except Exception as exc:
        return None, mb, exc


async def call_async(client: tmailor.AsyncClient, scn: Dict[str, Any]) -> Tuple[Any, Any, Optional[BaseException]]:
    op = scn["call"]["op"]
    a = _args(scn)
    mb: Any = None
    try:
        if op == "mailboxes.create":
            mb = await client.mailboxes.create(**a)
            return mb, mb, None
        if op == "mailboxes.list":
            return [m async for m in client.mailboxes.list(**a)], None, None
        if op.startswith("mailbox."):
            mb = client.mailbox(scn["call"]["token"])
            if op == "mailbox.messages":
                res: Any = [m async for m in mb.messages(**a)]
            else:
                res = await getattr(mb, op.split(".", 1)[1])(**a)
            then = scn["call"].get("then")
            if then:
                await getattr(mb, then.split(".", 1)[1])()
            return res, mb, None
        return await getattr(client, op)(**a), None, None
    except AssertionError:
        raise
    except Exception as exc:
        return None, mb, exc


def check(
    scn: Dict[str, Any], outcome: Any, err: Optional[BaseException], mailbox: Any, client: Any, replay: Replay, clock: FakeClock
) -> None:
    sid = scn["id"]
    exp = scn["expect"]
    replay.assert_done()
    if "error" in exp:
        e = exp["error"]
        assert err is not None, f"{sid}: expected {e['class']}, got result {outcome!r}"
        cls = ERROR_CLASSES[e["class"]]
        assert isinstance(err, cls), f"{sid}: {type(err).__name__} is not {cls.__name__}"
        assert isinstance(err, tmailor.TmailorError)
        assert err.code == e["code"], f"{sid}: code {err.code} != {e['code']}"
        assert err.status == e["status"], f"{sid}: status {err.status} != {e['status']}"
        for k in ("request_id", "retry_after", "message"):
            if k in e:
                assert getattr(err, k) == e[k], f"{sid}: {k} {getattr(err, k)!r} != {e[k]!r}"
        if "secret_changed" in e:
            assert isinstance(err, E.CacheUnavailableError) and err.secret_changed == e["secret_changed"]
        if "reason" in e:
            assert isinstance(err, E.InvalidNameError) and err.reason == e["reason"]
        # No secret ever leaks into the error text.
        text = f"{err} {err!r} {err.body} {err.headers}"
        assert DEFAULT_KEY not in text and "TESTSECRET" not in text and "conformance-token" not in text, f"{sid}: secret in error"
    else:
        assert err is None, f"{sid}: unexpected error {err!r}"
        assert_subset(sid, exp["result"], to_plain(outcome))
        if "otp" in exp:
            assert outcome.otp() == exp["otp"], f"{sid}: otp {outcome.otp()!r} != {exp['otp']!r}"
        for k in ("codes", "links"):  # API 1.3.0: values found by the server
            if k in exp:
                got = getattr(outcome, k)()
                assert got == exp[k], f"{sid}: {k} {got!r} != {exp[k]!r}"
    if "sleeps" in exp:
        assert len(clock.sleeps) == len(exp["sleeps"]), f"{sid}: sleeps {clock.sleeps} != {exp['sleeps']}"
        for a, b in zip(clock.sleeps, exp["sleeps"]):
            assert abs(a - b) < 1e-6, f"{sid}: sleeps {clock.sleeps} != {exp['sleeps']}"
    if "elapsed_max" in exp:  # Review 17e SDK-01: the call (sleeps AND requests) ended by then on the fake clock
        assert clock.now <= exp["elapsed_max"] + 1e-6, f"{sid}: elapsed {clock.now} > {exp['elapsed_max']}"
    if "sleep_count" in exp:
        assert len(clock.sleeps) == exp["sleep_count"], f"{sid}: {len(clock.sleeps)} sleeps != {exp['sleep_count']}"
    if "rate_limit" in exp:
        rl = client.last_rate_limit
        assert rl is not None and {"limit": rl.limit, "remaining": rl.remaining, "reset": rl.reset} == exp["rate_limit"]
    if "mailbox" in exp:
        assert mailbox is not None and mailbox.token == exp["mailbox"]["token"], f"{sid}: mailbox token not updated"
        assert "conformance-token" not in repr(mailbox), f"{sid}: token in repr"
