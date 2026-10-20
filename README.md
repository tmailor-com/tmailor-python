# tmailor: Python SDK for the Tmailor Open API

[![CI](https://github.com/tmailor-com/tmailor-python/actions/workflows/ci.yml/badge.svg)](https://github.com/tmailor-com/tmailor-python/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/tmailor-com/tmailor-python/blob/main/LICENSE)

The official Python SDK and CLI for the Open API of [Tmailor](https://tmailor.com), the temporary email service.
Create receive-only temporary email addresses and read incoming messages from your backend. Use it for
end-to-end sign-up tests, OTP and verification-message checks in QA pipelines, and inbox automation.

- Python 3.9+, one dependency (`httpx`), fully typed (`py.typed`).
- Equivalent sync and async APIs through `Client` and `AsyncClient`; async operations use `await` and `async for`.
- Typed errors, idempotent address creation, automatic retries, and rate-limit-aware polling.
- A `tmailor` command-line tool for trying the API from a terminal.
- [API reference](https://platform.tmailor.com/docs)

## Install

```bash
pip install tmailor-sdk
```

The package is published as `tmailor-sdk`; the module you import is `tmailor`, and the command-line tool is
`tmailor`.

## Setup

1. Sign in to [Tmailor Platform](https://platform.tmailor.com), create an API key, and add the caller's public
   IP address to your allowlist.
2. Provide the key:

```bash
export TMAILOR_API_KEY="tmk_live_..."
```

You can also pass it explicitly: `tmailor.Client(api_key="tmk_live_...")`. A client without a key can call only
`health()`; any other call raises `tmailor.MissingKeyError` (code `MissingKey`, no HTTP status) before sending a
request.

## Quickstart

```python
import tmailor

with tmailor.Client() as client:  # reads TMAILOR_API_KEY
    box = client.mailboxes.create()  # random address on a shared domain
    print(box.email)

    sign_up_somewhere(box.email)  # your code triggers an email

    # `since` is exclusive (received_at > since); "- 1" also accepts a message received in the creation second.
    msg = box.wait_for_message(timeout=120, since=(box.created_at or 0) - 1)
    print(msg.subject, msg.otp(), msg.links())
    box.delete()
```

## Server-side only

The API accepts requests only from the IP addresses in your account allowlist, and your API key gives full
access to your account. Use this SDK on servers, CI runners, or scripts you control. Never ship the API key or
address tokens to browsers or mobile apps.

## Client options

| Option | Default | Description |
|---|---|---|
| `api_key` | `$TMAILOR_API_KEY` | `tmk_live_<prefix>_<secret>` |
| `base_url` | `$TMAILOR_BASE_URL` or `https://open-api.tmailor.com` | API endpoint (see below) |
| `timeout` | `30` | seconds per HTTP request |
| `max_retries` | `2` | automatic retries (see [Retries](#retries)) |
| `max_retry_wait` | `60` | maximum `Retry-After` delay, in seconds, that is handled automatically |
| `http_client` | new `httpx.Client` | bring your own (proxy, custom transport) |
| `allow_untrusted_base_url` | `False` | required for any host other than `open-api.tmailor.com` |

**Base URL.** Every authenticated request sends your API key in the `Authorization` header. Address operations
also send the address token in the JSON body. The SDK therefore accepts only `https://` URLs without user
information (`user:pass@`), a query string, or a fragment. A host other than `open-api.tmailor.com` (staging,
your own proxy) raises `tmailor.BaseURLError` (a `ValueError`) unless you pass `allow_untrusted_base_url=True`,
which states that you trust that host with your credentials. This also applies to `TMAILOR_BASE_URL`. Plain
`http://` is accepted only for a loopback host (`127.0.0.1`, `::1`, `localhost`) during local development, and
it also requires `allow_untrusted_base_url=True`.

Both clients are context managers (`with` / `async with`); otherwise call `close()` or `await aclose()`. The SDK
closes only HTTP clients it creates. If you pass `http_client=`, you remain responsible for closing it.

## Addresses

```python
box = client.mailboxes.create()  # random name, shared domain
box = client.mailboxes.create(domain="mail.example.com", name="signup")  # your own verified domain

box.email, box.domain, box.created_at, box.token, box.web
```

- Creating an address uses 1 unit of your mailbox quota (`402 QuotaExceeded` when the quota is used up).
- Addresses never expire. They stay in your account until you delete them. Deleting an address does not return
  quota.
- Custom names are allowed only on your own domains (`client.domains()`). Add and verify domains on
  [Tmailor Platform](https://platform.tmailor.com). The system checks your domains' MX records automatically; to
  request an immediate check, select "Check now" on the platform. The API has no domain-check operation.
- A `name` that has already been used to create an address on the [tmailor.com](https://tmailor.com) website or
  mobile app cannot be used through the API. `create()` raises `tmailor.MailboxExistsError` (409) in that case; pick another name.

**The token is the key to the address.** Every operation on an address sends `box.token` in the JSON body.
Store it if you need the address later:

```python
box = client.mailbox(saved_token)  # no request is made
box.info()  # loads email, message_count, current tokens
```

Lost a token? `client.mailboxes.list()` returns every address your account still owns and its current token
when available. Listing addresses is also how you recover after a token was changed on Tmailor Platform:

```python
for box in client.mailboxes.list(limit=100):  # follows next_cursor, up to 100 addresses
    print(box.email, box.message_count)

page = client.mailboxes.list_page(page_size=100)  # one page: .items, .next_cursor
```

A token and `box.web` may temporarily be `None`; an address operation without a token raises
`tmailor.TmailorError` with code `NoToken`.

Delete an address:

```python
box.delete()  # same as "Delete" on tmailor.com; all tokens stop working
```

The API does not rotate tokens. To replace an address's tokens, use Tmailor Platform. The old token then
produces `MailboxNotFoundError`, and `client.mailboxes.list()` returns the new token.

When it is available, `box.web` holds the tokens the tmailor.com website uses (`accesstoken`, `transfertoken`,
`reuse_url`). Anyone holding them can read the inbox on tmailor.com or, with `transfertoken`, take over the
address. Treat them like passwords. SDK object representations never expose complete tokens or API keys; a
client representation may show the API key's non-secret prefix.

## Messages

```python
for m in box.messages(unread=True, from_="shop", subject="confirm"):  # newest first, all pages
    print(m.id, m.from_.address, m.subject, m.received_at, m.seen)

page = box.messages_page(page_size=20)  # .items, .next_cursor, .total; items do not include bodies

msg = box.message(m.id)  # fetches .text and sanitized .html; marks the message as read
box.delete_message(msg)
deleted = box.clear()  # deletes all messages and returns the count
```

Calling `box.message()`, or receiving a message from `wait_for_message()`, marks that message as read. It then
no longer appears with `unread=True`. There is no separate "mark as read" call.

Filters run on the server: `unread`, `from_` (text in the sender name or address), `subject` (text in the
subject), and `since` (Unix time or a timezone-aware `datetime`; **exclusive**: only messages with
`received_at > since`, in whole seconds). `limit` caps how many messages the iterator yields; `page_size`
(1 to 100) sets the page size. Messages are kept for 24 hours. If the API ever returns a pagination cursor it
has already returned, the iterators stop with `tmailor.InvalidResponseError` instead of looping.

### Waiting for a message

```python
msg = box.wait_for_message(timeout=120, from_="no-reply@shop.example", subject_contains="verify")
```

`wait_for_message` returns, with its body, the first matching message that was **not in the inbox when the wait
started**. It first records the IDs of all current messages, without filters; `from_` and `subject_contains`
apply only to the polls. If the message may arrive before polling starts (the usual case: you trigger the email,
then wait), pass `since` so that it can still match:

```python
box = client.mailboxes.create()
trigger_email(box.email)
msg = box.wait_for_message(since=(box.created_at or 0) - 1)  # server time, no clock-skew issues
```

`since` is exclusive and counts whole seconds, so `since=box.created_at` would miss a message received in the
same second the address was created. Subtract 1 as shown above.

The first poll is immediate. After an unsuccessful poll, the delay starts at 1 second and grows to 10 seconds.
`poll_interval=` sets a fixed delay with a minimum of 0.5 seconds; a value of zero or less raises `ValueError`.
When `X-RateLimit-Remaining` is low, the SDK spreads the remaining requests until the minute resets. A
`429 RateLimited` response during the wait is handled by sleeping for `Retry-After` (at least 1 second).

`timeout` sets the wait deadline, including for HTTP requests (with the sync `Client`, an in-progress request can
overrun it as described below). No sleep or retry goes past it, each request (polls and the final read of the
message) gets at most the remaining time as its HTTP timeout, and a poll scheduled after a delay starts only if
at least 1 second remains before the deadline. If nothing arrives in time, or the deadline interrupts a request, it raises
`tmailor.WaitTimeoutError` (also a built-in `TimeoutError`); `timeout=0` raises it without sending a request.

httpx timeouts apply to each phase of a request (connect, send, each read), not to the whole request, so the SDK
enforces the deadline on top of them. `AsyncClient` cancels a request at the deadline (`asyncio.wait_for`), and
both clients treat a response that arrives after the deadline as `WaitTimeoutError` (its data is not used). With
the sync `Client`, a server that sends bytes very slowly can still keep one request running past the deadline, by
at most one read timeout (`min(client timeout, time left)`) per read. Use `AsyncClient` when a hard wall-clock
limit matters.

### Codes and links

```python
msg.otp()  # "482913", or None when there is no confident answer
msg.codes()  # ["482913"]: every code-like value, use it when otp() is None or picks the wrong one
msg.links()  # ["https://example.com/verify?t=..."]
```

For a message read with its body (`box.message(id)` or `wait_for_message`), these values come from the API
(fields `otp`, `codes` and `links`, API 1.3.0), so every language and plain `curl` get the same answer. For a
message from the list (subject only), or a server without those fields, the SDK applies the same rules locally.
`otp()` looks for 4- to 8-digit codes
(including forms such as `123 456` and `G-123456`) or 4- to 8-character uppercase codes containing both letters
and digits, such as `A7K29Q`, preferably near words such as "code", "OTP", "verification", or "PIN". It skips candidates inside URLs,
email addresses, dates, amounts, and similar text where possible. Without a nearby keyword, it returns a value
only when there is one distinct six-digit candidate and no other eligible numeric candidate. Always handle
`None`. `links()` returns unique absolute HTTP and HTTPS URLs. The standalone helpers
`tmailor.extract_otp(subject, text, html)`, `tmailor.extract_codes(subject, text, html)` and
`tmailor.extract_links(text, html)` work on any string.

## Account

```python
me = client.me()  # user_id, limits (quota, rate_per_min, daily_req_cap, ...), ip_allowlist, subscriptions
doms = client.domains()  # your own domains + doms.shared_domains_available
usage = client.usage(from_="2026-10-01", to="2026-10-17")  # daily counters
client.health()  # public status: no key needed and none is sent
client.last_rate_limit  # RateLimitInfo(limit, remaining, reset) of the last response
```

## Errors

API, network, response-validation, and wait failures are subclasses of `tmailor.TmailorError` with `code`,
`status`, `message`, `description`, `request_id`, and `retry_after`. Invalid local arguments may instead raise
`TypeError` or `ValueError`; `tmailor.BaseURLError` is a `ValueError`. Handle errors by class or `code`, not by
message text. Include `request_id` when contacting support.

| HTTP | Class (code) |
|---|---|
| 400, 413 | `BadRequestError`: `InvalidJsonError`, `InvalidFieldError`, `InvalidDomainError`, `CustomNameNotAllowedError`, `InvalidNameError` (`.reason`) |
| 401 | `AuthenticationError`: `MissingKeyError`, `InvalidKeyError`, `KeyRevokedError` |
| 402 | `QuotaExceededError` |
| 403 | `PermissionDeniedError`: `AccountLockedError`, `IpNotAllowedError` |
| 404 | `NotFoundError`: `MailboxNotFoundError`, `MessageNotFoundError` |
| 405 | `MethodNotAllowedError` |
| 409 | `ConflictError`: `MailboxExistsError` (including a name created on the website or mobile app) |
| 429 | `RateLimitError`: `RateLimitedError`, `DailyCapReachedError` |
| 500, other 5xx | `InternalServerError`, unless a more specific error code applies |
| 503 | `ServiceUnavailableError`: `NoDomainAvailableError`, `IdempotencyUnavailableError`, `CacheUnavailableError` (`.secret_changed`); `InternalServerError` (`InternalError`, reason `cache_hold_failed`); a degraded `health()` (code `HTTP503`) |
| other 4xx | `APIError` |
| 2xx | `InvalidResponseError` (`InvalidResponse`): body not JSON, a field missing or of the wrong type, or a repeated pagination cursor. The body is never kept in the error |
| none | `MissingKeyError` (no API key configured), `APIConnectionError`, `APITimeoutError` (no HTTP response) |
| none | `WaitTimeoutError` (`wait_for_message`) |

```python
try:
    box = client.mailboxes.create()
except tmailor.QuotaExceededError:
    ...
except tmailor.IpNotAllowedError as e:
    print("Add this server's IP address on platform.tmailor.com", e.request_id)
```

`MailboxNotFoundError` is raised when a token is invalid, was rotated on the platform, belongs to a deleted
address or another account, or was issued by the tmailor.com website (only tokens from the API or the platform
work). `CacheUnavailableError` on `delete()` with `secret_changed=True` means the address was deleted, but old
tokens may still work on tmailor.com for up to 10 minutes.

## Retries

Automatic retries (up to `max_retries`, default 2) happen only when repeating the request is safe:

- `429 RateLimited`: the API did not process the request, so any request is retried after `Retry-After`
  (at least 1 second, unless it is longer than `max_retry_wait`). `DailyCapReached` is never retried.
- `500/502/503/504` and network errors: retried only for reads (`GET`, `info`, `messages`, `message`) and for
  `mailboxes.create()`. A failed connection (nothing sent) is retried for any request.
- `503 CacheUnavailable` on `delete()` is never retried.
- `message()` marks the message as read. Doing that twice has the same effect, so it is retried like a read.

`mailboxes.create()` always sends an `Idempotency-Key` (a new UUID, or yours via `idempotency_key=`), and every
retry reuses it, so a retry can never create a second address or use quota twice. When there is no
`Retry-After`, exponential backoff uses full jitter: the upper bound starts at 0.5 seconds, doubles on each
retry, and is capped at 8 seconds; the actual delay is between half the upper bound and the upper bound.

## Async

```python
import asyncio

import tmailor


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

The package installs the `tmailor` command, also available as `python -m tmailor`, for working with the API from
a terminal. The CLI is built on the SDK and the Python standard library.

```bash
tmailor login                      # asks for the API key in a hidden prompt and saves a private config file
tmailor me                         # account, quota, rate limit, plans
tmailor create                     # random address on a shared domain
tmailor wait k3x9q2m7@example.net  # waits for a new message, prints subject, sender, text, OTP and links
tmailor inbox k3x9q2m7@example.net --unread
tmailor read k3x9q2m7@example.net 5d41402abc4b2a76b9719d911017c592
tmailor delete k3x9q2m7@example.net --yes
```

| Command | What it does |
|---|---|
| `me`, `domains`, `usage [--from D] [--to D] [--all-days]`, `health` | account, own domains, daily counters, service status (`health` needs no key) |
| `create [--domain D] [--name N]`, `list` | create an address, list your addresses |
| `inbox EMAIL [--unread] [--from X] [--subject Y] [--limit N]` | messages from the last 24 hours |
| `read EMAIL ID [--html]` | one message with its text body, OTP, and links (marks it as read) |
| `wait EMAIL [--from X] [--subject Y] [--timeout 120] [--any]` | wait for a new message and print it (`--any`: a message already in the inbox also counts) |
| `rm-msg EMAIL ID [-y]`, `clear EMAIL [-y]` | delete one message, delete all messages |
| `delete EMAIL [-y]` | delete the address |
| `token EMAIL` | print the address token (the only command that shows it; keep it secret) |
| `shell` | interactive mode: pick an address, read or wait for messages, create or delete addresses |
| `login`, `logout [-y]` | save or remove `~/.tmailor` |

- Provide an email address, and the CLI looks up its token once per run. If you have the token instead, never
  put it on the command line (shell history, process list). Use `--token-stdin`
  (`tmailor inbox --token-stdin < token.txt`), `--token-file FILE`, or `--ask-token` (hidden prompt). With one of
  these options, `read` and `rm-msg` take only the message ID.
- For commands that require authentication, the CLI reads the API key from `TMAILOR_API_KEY`, then from
  `~/.tmailor` (`api_key=...`). If neither is available and standard input is a terminal, it uses a hidden
  prompt; otherwise the command fails. `health` does not require or prompt for a key. The key is never printed.
- Config and token files must be private regular files. The CLI rejects symbolic links and unsafe paths. On
  platforms where these checks are not supported, use `TMAILOR_API_KEY` and `--token-stdin`. See
  [docs/cli-security.md](https://github.com/tmailor-com/tmailor-python/blob/main/docs/cli-security.md) for the exact rules.
- These options are accepted before or after every command: `--json` (the CLI result as JSON), `--raw`
  (API-shaped JSON), `--base-url URL`, `--allow-untrusted-base-url`, and `--debug` (traceback on errors). Most
  non-interactive API commands honor `--json` and `--raw`; `token --json` prints the email and token as JSON,
  while `shell`, `login`, and `logout` always use human-readable output. Address tokens are masked as `***` in
  every output mode except `tmailor token`.
- The base URL follows the same rules as the SDK: `https://` only, with no user information, query string, or
  fragment. A host other than `open-api.tmailor.com` is refused unless `--allow-untrusted-base-url` is given on
  that run, including when the URL comes from `TMAILOR_BASE_URL` or a `base_url=` line in the config file.
  `http://` is allowed only for a loopback host, with the flag. `login --base-url` uses the URL to check the key
  but never saves it.
- `rm-msg`, `clear`, `delete`, and `logout` ask for confirmation; `-y`/`--yes` skips it (required when there is
  no terminal).
- By default, errors print the API code, HTTP status, message, and `request_id` without a traceback. Use
  `--debug` to include one. Exit codes are `0` for success, `1` for an error, `2` for invalid arguments, `3` when
  `wait` times out, and `130` when interrupted with Ctrl+C.
- Terminal control characters (C0, DEL, C1) and Unicode bidirectional controls are removed from all printed
  text. Argument errors, which can quote what you typed, show them as visible `\uXXXX` escapes instead. In
  `--json` and `--raw` output, control characters are escaped rather than removed: on a terminal, the JSON is
  ASCII-only; when redirected to a file or pipe, it keeps other Unicode characters but writes C1, DEL, and
  bidirectional controls as `\uXXXX`.

## Links

- [Tmailor](https://tmailor.com): free temporary email service
- [Tmailor Platform](https://platform.tmailor.com): API keys, IP allowlist, domains, and plans
- [API documentation](https://platform.tmailor.com/docs)
- [Issue tracker](https://github.com/tmailor-com/tmailor-python/issues)
- [Support](https://tmailor.com/contact-us)
- [Changelog](https://github.com/tmailor-com/tmailor-python/blob/main/CHANGELOG.md)

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                      # unit, CLI, OpenAPI contract, and shared conformance scenarios (no network)
pytest -m live              # real API; needs TMAILOR_API_KEY from an allowlisted IP address
ruff check . && mypy
```

`tests/test_conformance.py` replays the language-neutral scenarios in `conformance/scenarios/`. Every Tmailor
SDK must pass the same scenarios. The source distribution ships them too (`TMAILOR_CONFORMANCE_DIR` overrides the
location); if they are missing, the test run fails instead of skipping. `tests/test_spec_contract.py` checks
every response reader against `docs/openapi.yaml` of the API repository: a field the SDK requires must be
`required` in the spec, and a `null` the spec allows must be accepted. It needs PyYAML and is skipped when the
spec is not available (`TMAILOR_OPENAPI_SPEC` overrides the path).

## License

MIT. See [LICENSE](https://github.com/tmailor-com/tmailor-python/blob/main/LICENSE).
