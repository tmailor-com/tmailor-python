# tmailor: Python SDK for the Tmailor Open API

Create receive-only temporary email addresses and read the messages they receive, from your own backend.
Typical uses: end-to-end tests of sign-up flows, OTP and verification emails in QA pipelines, inbox
automation.

```python
import tmailor

with tmailor.Client() as client:  # reads TMAILOR_API_KEY
    box = client.mailboxes.create()  # random address on a shared domain
    print(box.email)

    sign_up_somewhere(box.email)  # your code triggers an email

    # since is exclusive (received_at > since): "- 1" also accepts a message received in the creation second.
    msg = box.wait_for_message(timeout=120, since=(box.created_at or 0) - 1)
    print(msg.subject, msg.otp(), msg.links())
    box.delete()
```

- Python 3.9+, one dependency (`httpx`), fully typed (`py.typed`).
- Sync `Client` and async `AsyncClient` with the same methods.
- Typed errors, automatic retries that never create an address twice, rate-limit aware polling.
- API reference: https://platform.tmailor.com/docs

## Server-side only

The API accepts requests only from the IP addresses in your account allowlist, and your API key gives full
access to your account. Use this SDK on servers, CI runners or scripts you control. Never ship the API key
(or address tokens) to browsers or mobile apps.

## Install

```bash
pip install tmailor
```

## Setup

1. Sign in on https://platform.tmailor.com, create an API key and add the public IP of the machine that will
   call the API to the IP allowlist.
2. Provide the key:

```bash
export TMAILOR_API_KEY="tmk_live_..."
```

or pass it explicitly: `tmailor.Client(api_key="tmk_live_...")`. A client without a key can only call
`health()`; any other call raises `tmailor.MissingKeyError` (code `MissingKey`, no HTTP status) before sending
anything.

| Option | Default | |
|---|---|---|
| `api_key` | `$TMAILOR_API_KEY` | `tmk_live_<prefix>_<secret>` |
| `base_url` | `$TMAILOR_BASE_URL` or `https://open-api.tmailor.com` | |
| `timeout` | `30` | seconds per HTTP request |
| `max_retries` | `2` | automatic retries (see [Retries](#retries)) |
| `max_retry_wait` | `60` | longest `Retry-After` waited for automatically |
| `http_client` | new `httpx.Client` | bring your own (proxy, custom transport) |
| `allow_untrusted_base_url` | `False` | required for any host other than `open-api.tmailor.com` |

**Base URL.** Every authenticated request sends your API key (`Authorization` header) and, for address
operations, the address token (JSON body) to `base_url`. So the SDK accepts only `https://` URLs without user info
(`user:pass@`), query string or fragment, and a host other than `open-api.tmailor.com` (staging, your own proxy)
raises `tmailor.BaseURLError` (a `ValueError`) unless you pass `allow_untrusted_base_url=True`, which states that
you trust that host with your credentials. This applies to `TMAILOR_BASE_URL` too. Plain `http://` is accepted
only for a loopback host (`127.0.0.1`, `::1`, `localhost`) during local development, and also needs
`allow_untrusted_base_url=True`.

Both clients are context managers (`with` / `async with`); otherwise call `close()` / `await aclose()`.

## Addresses

```python
box = client.mailboxes.create()  # random name, shared domain
box = client.mailboxes.create(domain="mail.example.com", name="signup")  # your own verified domain

box.email, box.domain, box.created_at, box.token, box.web
```

- Creating an address uses 1 unit of your mailbox quota (`402 QuotaExceeded` when it is used up).
- Addresses never expire. They stay in your account until you delete them. Deleting does not return quota.
- Custom names are only allowed on your own domains (`client.domains()`); add and verify domains on the
  platform. The system checks the MX records of your domains by itself; to check one right away use
  "Check now" on https://platform.tmailor.com (the API has no domain check call).
- A `name` that was already created on the tmailor.com website or mobile app cannot be used through the API:
  `create()` raises `tmailor.MailboxExistsError` (409, message "This address was created on the website or
  mobile app and cannot be used through the API. Pick another name"). Pick another name.

**The token is the key to the address.** Every operation on one address sends `box.token` in the JSON body.
Store it if you need the address later:

```python
box = client.mailbox(saved_token)  # no request is made
box.info()  # loads email, message_count, current tokens
```

Lost a token? `client.mailboxes.list()` returns every address your account still owns, each with its current
token (this is also how you recover after the token was changed on tmailor.com):

```python
for box in client.mailboxes.list():  # follows next_cursor for you
    print(box.email, box.message_count)

page = client.mailboxes.list_page(page_size=100)  # one page: .items, .next_cursor
```

Delete an address:

```python
box.delete()  # like "Delete" on tmailor.com; all tokens stop working
```

The API does not rotate tokens. To replace the tokens of an address use https://platform.tmailor.com; the old
token then gets `MailboxNotFoundError` and `client.mailboxes.list()` returns the new one.

`box.web` holds the tokens the tmailor.com website uses (`accesstoken`, `transfertoken`, `reuse_url`).
Anyone holding them can read the inbox on tmailor.com (or, with `transfertoken`, take the address over).
Treat them like passwords. `repr()` of SDK objects never shows tokens or your API key.

## Messages

```python
for m in box.messages(unread=True, from_="shop", subject="confirm"):  # newest first, all pages
    print(m.id, m.from_.address, m.subject, m.received_at, m.seen)

page = box.messages_page(page_size=20)  # .items, .next_cursor, .total

msg = box.message(m.id)  # with .text and sanitized .html; marks the message as read (msg.seen is True)
box.delete_message(msg)
deleted = box.clear()  # delete all, returns the count
```

Reading one message (`box.message()`, and the message returned by `wait_for_message()`) marks it as read, like
opening it on tmailor.com: it no longer shows up with `unread=True`. There is no separate "mark as seen" call.

Filters run on the server: `unread`, `from_` (text in sender name or address), `subject` (text in subject),
`since` (unix time or timezone-aware `datetime`; **exclusive**: only messages with `received_at > since`, in
whole seconds). `limit` caps how many messages the iterator yields; `page_size` (1 to 100) is the page size.
Messages are kept for 24 hours. If the API ever hands out a pagination cursor it already returned, the iterators
stop with `tmailor.InvalidResponseError` instead of looping.

### Waiting for an email

```python
msg = box.wait_for_message(timeout=120, from_="no-reply@shop.example", subject_contains="verify")
```

`wait_for_message` returns the first matching message that was **not in the inbox when the wait started**,
with its body (the ids of every current message are recorded first, without filters; `from_` and
`subject_contains` only apply to the polls). If the email may already have arrived before you start waiting
(the usual case: you trigger the email, then wait), pass `since` so earlier arrivals count too:

```python
box = client.mailboxes.create()
trigger_email(box.email)
msg = box.wait_for_message(since=(box.created_at or 0) - 1)  # server time, no clock-skew issues
```

`since` is exclusive and counts in whole seconds, so `since=box.created_at` would miss a message received in
the same second the address was created; subtract 1 as above.

Polling starts at 1 second and slows down to 10 seconds (`poll_interval=` fixes it). It respects your rate
limit: when `X-RateLimit-Remaining` gets low it spreads the remaining requests until the minute resets, and a
`429` inside the wait is absorbed by sleeping `Retry-After` (at least 1 second). `timeout` bounds the whole
wait, HTTP requests included: no sleep or retry goes past it, every request (polls and the final read of the
message) gets at most the time that is left as its HTTP timeout, and the last poll starts at least 1 second
before the deadline. When nothing arrives in time, or the deadline cuts a request, it raises
`tmailor.WaitTimeoutError` (also a built-in `TimeoutError`); `timeout=0` raises it without sending anything.

httpx timeouts apply to each phase of a request (connect, send, each read), not to the whole request, so the
deadline is enforced on top of them: `AsyncClient` cancels a request at the deadline (`asyncio.wait_for`), and
both clients treat a response that arrives after the deadline as `WaitTimeoutError` (its data is not used). With
the sync `Client` a server that sends bytes very slowly can still keep one request running past the deadline, by
at most one read timeout (`min(client timeout, time left)`) per read; use `AsyncClient` when a hard wall-clock
limit matters.

### Codes and links

```python
msg.otp()  # "482913", or None when there is no confident answer
msg.links()  # ["https://example.com/verify?t=..."]
```

The API never extracts anything for you; these helpers run locally. `otp()` looks for 4 to 8 digit codes
(also `123 456`, `G-123456`) or upper-case letter and digit codes (`A7K29Q`) close to words such as code,
OTP, verification or PIN, ignoring URLs, e-mail addresses, years, amounts and dates. Without such a word it
answers only when the message holds exactly one 6-digit number. Always handle `None`.
`tmailor.extract_otp(subject, text, html)` and `tmailor.extract_links(text, html)` work on any string.

## Account

```python
me = client.me()  # user_id, limits (quota, rate_per_min, daily_req_cap, ...), ip_allowlist, subscriptions
doms = client.domains()  # your own domains + doms.shared_domains_available
usage = client.usage(from_="2026-10-01", to="2026-10-17")  # daily counters
client.health()  # public status: no key needed and none is sent
client.last_rate_limit  # RateLimitInfo(limit, remaining, reset) of the last response
```

## Errors

Every error is a `tmailor.TmailorError` with `code`, `status`, `message`, `description`, `request_id` and
`retry_after`. Branch on the class or on `code`, never on the message text, and quote `request_id` when you
contact support.

| HTTP | Class (code) |
|---|---|
| 400 | `BadRequestError`: `InvalidJsonError`, `InvalidFieldError`, `InvalidDomainError`, `CustomNameNotAllowedError`, `InvalidNameError` (`.reason`) |
| 401 | `AuthenticationError`: `MissingKeyError`, `InvalidKeyError`, `KeyRevokedError` |
| 402 | `QuotaExceededError` |
| 403 | `PermissionDeniedError`: `AccountLockedError`, `IpNotAllowedError` |
| 404 | `NotFoundError`: `MailboxNotFoundError`, `MessageNotFoundError` |
| 409 | `ConflictError`: `MailboxExistsError` (also a name created on the website or mobile app) |
| 429 | `RateLimitError`: `RateLimitedError`, `DailyCapReachedError` |
| 500 | `InternalServerError` |
| 503 | `ServiceUnavailableError`: `NoDomainAvailableError`, `IdempotencyUnavailableError`, `CacheUnavailableError` (`.secret_changed`); `InternalServerError` (`InternalError`, reason `cache_hold_failed`); a degraded `health()` (code `HTTP503`) |
| 2xx | `InvalidResponseError` (`InvalidResponse`): body not JSON, a field missing or of the wrong type, or a repeated pagination cursor. The body is never kept in the error |
| none | `MissingKeyError` (no API key configured), `APIConnectionError`, `APITimeoutError` (no HTTP response) |
| none | `WaitTimeoutError` (`wait_for_message`) |

```python
try:
    box = client.mailboxes.create()
except tmailor.QuotaExceededError:
    ...
except tmailor.IpNotAllowedError as e:
    print("Add this server's IP on platform.tmailor.com", e.request_id)
```

`MailboxNotFoundError` is the single answer for a token that is wrong, rotated on the platform, deleted or
owned by another account, and for a token issued by the tmailor.com website (only tokens from the API or the
platform work). `CacheUnavailableError` on `delete()` with `secret_changed=True` means the address was deleted
but old tokens may still work on tmailor.com for up to 10 minutes.

## Retries

Automatic retries (up to `max_retries`, default 2) happen only when repeating the request is safe:

- `429 RateLimited`: the API did not process the request, so any request is retried after `Retry-After`
  (at least 1 second, unless it is longer than `max_retry_wait`). `DailyCapReached` is never retried.
- `500/502/503/504` and network errors: only for reads (`GET`, `info`, `messages`, `message`) and for
  `mailboxes.create()`. A failed connection (nothing sent) is retried for any request.
- `503 CacheUnavailable` (delete) is never retried.
- `message()` marks the message as read; doing that twice changes nothing, so it is retried like a read.

`mailboxes.create()` always sends an `Idempotency-Key` (a new UUID, or yours via `idempotency_key=`) and every
retry reuses it, so a retry can never create a second address or use quota twice. Backoff is exponential with
jitter (0.5 s doubling, capped at 8 s) when there is no `Retry-After`.

## Async

```python
import asyncio, tmailor


async def main() -> None:
    async with tmailor.AsyncClient() as client:
        box = await client.mailboxes.create()
        msg = await box.wait_for_message(timeout=120, since=(box.created_at or 0) - 1)
        async for m in box.messages():
            print(m.subject)
        await box.delete()


asyncio.run(main())
```

## Command line

The package installs a `tmailor` command (also `python -m tmailor`) to try the API by hand. It uses only the
SDK and the Python standard library.

```bash
tmailor login                      # asks for the API key (hidden) and saves it to ~/.tmailor (mode 600)
tmailor me                         # account, quota, rate limit, plans
tmailor create                     # random address on a shared domain
tmailor wait k3x9q2m7@example.net  # waits for a new message, prints subject, sender, text, OTP and links
tmailor inbox k3x9q2m7@example.net --unread
tmailor read k3x9q2m7@example.net 5d41402abc4b2a76b9719d911017c592
tmailor delete k3x9q2m7@example.net --yes
```

| Command | What it does |
|---|---|
| `me`, `domains`, `usage [--from D] [--to D]`, `health` | account, own domains, daily counters, service status (no key needed) |
| `create [--domain D] [--name N]`, `list` | create an address, list your addresses |
| `inbox EMAIL [--unread] [--from X] [--subject Y] [--limit N]` | messages of the last 24 hours |
| `read EMAIL ID [--html]` | one message with text body, OTP and links (marks it as read) |
| `wait EMAIL [--from X] [--subject Y] [--timeout 120] [--any]` | wait for a new message and print it (`--any`: a message already in the inbox counts too) |
| `rm-msg EMAIL ID [-y]`, `clear EMAIL [-y]` | delete one message, delete all |
| `delete EMAIL [-y]` | delete the address |
| `token EMAIL` | print the address token (only here; keep it secret) |
| `shell` | interactive mode: pick an address, read or wait for mail, create or delete addresses |
| `login`, `logout [-y]` | save or remove `~/.tmailor` |

- Addresses are given by e-mail: the tool looks the token up with `GET /v1/mailboxes` (once per run). When you
  have the token instead, never put it on the command line (shell history, process list): use
  `--token-stdin` (`tmailor inbox --token-stdin < token.txt`), `--token-file FILE` (same checks as the config
  file) or `--ask-token` (hidden prompt). With one of them, `read`/`rm-msg` take only the message ID.
- API key: `TMAILOR_API_KEY`, else `~/.tmailor` (`api_key=...`), else a hidden prompt. The key is never printed.
  The config file (and `TMAILOR_CONFIG`, which points to another one) must be a regular file, not a symbolic
  link, owned by you, mode `600` or stricter. Every directory of its path must be a real directory (no
  symbolic link, give the real path), owned by you or root and not writable by others unless it has the sticky
  bit; as root the directory holding the file must not be writable by others at all. The checks are made on
  the open directory descriptors that are then used, so nothing can be swapped in between. `login` writes it atomically (random temporary file created
  with mode 600, `fsync`, rename); if anything fails before the rename the temporary file is removed.
- Supported platforms for the config file and `--token-file`: on POSIX the CLI needs `openat`-style calls
  (`dir_fd` for open/stat/unlink/rename), `O_NOFOLLOW` and `O_DIRECTORY`. Where any of them is missing it fails
  closed ("platform not supported for config/token files") and never falls back to path operations: use
  `TMAILOR_API_KEY` and `--token-stdin` there (they keep working; a set `TMAILOR_CONFIG` is refused, the default
  `~/.tmailor` is skipped). On Windows the file must be inside your user profile; the CLI works by path but
  refuses any symbolic link, junction or other reparse point in the file or in a directory below the profile,
  and checks that the file opened is the one checked. Threat model there: the profile's default ACL (only you,
  SYSTEM and Administrators can write) means no other unprivileged user can swap entries; a program already
  running as you is out of scope (it could read the file anyway). Owner/mode checks are POSIX only.
- Options for every command (before or after it): `--json` (result as JSON), `--raw` (API JSON as received),
  `--base-url URL`, `--allow-untrusted-base-url`, `--debug` (traceback on errors). Tokens are masked as `***`
  everywhere except `tmailor token`.
- Base URL: same rules as the SDK (https only, no user info/query/fragment). A host other than
  `open-api.tmailor.com` is refused unless `--allow-untrusted-base-url` is given on that run (also when it comes
  from `TMAILOR_BASE_URL` or a `base_url=` line of the config file); `http://` only for a loopback host, with the
  flag. `login --base-url` uses the URL to check the key but never saves it.
- `rm-msg`, `clear`, `delete` and `logout` ask for confirmation; `-y`/`--yes` skips it (required when
  there is no terminal).
- Errors print the API code, HTTP status, message and `request_id`, without a traceback. Exit codes: 0 ok,
  1 error, 2 bad arguments, 3 `wait` timed out, 130 interrupted (Ctrl+C).
- Everything printed has terminal control characters (C0, DEL, C1) and Unicode bidirectional controls removed.
  Argument errors (which can quote what you typed) show them as visible `\uXXXX` escapes instead.
  `--json`/`--raw` keep the data exact but escape them: on a terminal the JSON is pure ASCII
  (`ensure_ascii`); redirected to a file or pipe it keeps Unicode, with C1, DEL and bidi controls written as
  `\uXXXX`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # unit, CLI, OpenAPI contract and shared conformance scenarios (no network)
pytest -m live              # real API, needs TMAILOR_API_KEY from an allowlisted IP
ruff check . && mypy
```

`tests/test_conformance.py` replays the language-neutral scenarios in `conformance/scenarios/`, the same
files every Tmailor SDK must pass. The source distribution ships them too
(`TMAILOR_CONFORMANCE_DIR` overrides the location); if they are missing the test run fails instead of skipping.
`tests/test_spec_contract.py` checks every response reader against `docs/openapi.yaml` of the API repository
(a field the SDK requires must be `required` in the spec; a `null` the spec allows must be accepted). It needs
PyYAML and is skipped when the spec is not there (`TMAILOR_OPENAPI_SPEC` overrides the path).

## License

MIT
