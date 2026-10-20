# Conformance scenarios

Language-neutral test cases that every Tmailor SDK must pass. Each file in `scenarios/` describes one SDK call,
the exact HTTP requests the SDK must send, canned responses to return, and what the SDK must produce
(result, error, sleeps). No network is used: the SDK's HTTP layer is replaced by a replay transport.

The Python runner is `../python/tests/conformance_runner.py`; port it first when you start a new SDK.

## Scenarios (80 files)

- Auth and keys: `auth_account_locked`, `auth_invalid_key`, `auth_ip_not_allowed`, `auth_key_revoked`, `auth_missing_key`
- Health: `health_degraded_503`, `health_ok`, `health_without_key`
- Account (`me`, `domains`, `usage`): `domains_ok`, `me_ok`, `me_without_key`, `usage_default`, `usage_invalid_field`, `usage_range`
- Create and list addresses: `mailboxes_create_customnamenotallowed`, `mailboxes_create_empty_body`, `mailboxes_create_explicit_idempotency_key`, `mailboxes_create_idempotency_conflict`, `mailboxes_create_invalid_name_reason`, `mailboxes_create_invaliddomain`, `mailboxes_create_mailbox_exists`, `mailboxes_create_name_used_on_website`, `mailboxes_create_max_retries_zero`, `mailboxes_create_network_error_retry`, `mailboxes_create_no_domain_exhausts_retries`, `mailboxes_create_own_domain`, `mailboxes_create_quota_exceeded`, `mailboxes_create_random`, `mailboxes_create_retry_429_same_key`, `mailboxes_create_retry_503_idempotency_unavailable`, `mailboxes_list_cursor_repeat`, `mailboxes_list_empty`, `mailboxes_list_limit`, `mailboxes_list_null_tokens`, `mailboxes_list_pagination`
- One address: `mailbox_clear`, `mailbox_delete`, `mailbox_delete_cache_unavailable`, `mailbox_delete_message`, `mailbox_delete_message_429_retried`, `mailbox_delete_twice`, `mailbox_info`, `mailbox_info_invalid_type`, `mailbox_info_not_found`, `mailbox_info_retry_500`, `mailbox_message_body_unavailable`, `mailbox_message_not_found`, `mailbox_message_read`, `mailbox_message_read_seen_false`, `mailbox_message_server_otp_null`, `mailbox_messages_cursor_cycle`, `mailbox_messages_default`, `mailbox_messages_filters_pagination`, `mailbox_messages_item_missing_id`, `mailbox_messages_seen_not_boolean`
- wait_for_message: `wait_for_message_429_inside_wait`, `wait_for_message_baseline_cursor_repeat`, `wait_for_message_baseline_unfiltered`, `wait_for_message_fixed_interval`, `wait_for_message_new_only`, `wait_for_message_rate_limit_slowdown`, `wait_for_message_retry_after_beyond_deadline`, `wait_for_message_retry_after_zero`, `wait_for_message_since`, `wait_for_message_since_same_second`, `wait_for_message_slow_final_read`, `wait_for_message_slow_poll_deadline`, `wait_for_message_timeout`, `wait_for_message_timeout_zero`
- Transport, retries and errors: `connect_error_retried_any`, `daily_cap_not_retried`, `gateway_error_non_json`, `internal_error_get_retried`, `rate_limited_exhausts_retries`, `rate_limited_retry_after_too_long`, `rate_limited_retry_after_zero`, `read_error_unsafe_not_retried`, `success_body_not_json`, `timeout_safe_retried_then_raised`, `unknown_error_code`

Removed on 2026-10-17 with their endpoints: `domain_check_*`, `mailbox_rotate_*`, `mailbox_mark_seen*`.

## File format

```jsonc
{
  "id": "mailboxes_create_retry_429_same_key",       // = file name
  "description": "...",
  "client": { "api_key": "...", "max_retries": 2, "max_retry_wait": 60, "timeout": 30 },   // optional overrides ("" = no key)
  "clock":  { "wall_start": 1760342400 },             // optional, see "Clock"
  "call": {
    "op": "mailboxes.create",                         // see "Operations"
    "token": "eyJ...",                                // mailbox.* ops: build the mailbox from this token
    "args": { "domain": "...", "from": "..." },       // neutral argument names (see below)
    "then": "mailbox.info"                            // optional follow-up call on the same mailbox object
  },
  "exchanges": [
    {
      "request":  { "method": "POST", "path": "/v1/mailboxes", "query": {}, "headers": { ... }, "json": {} },
      "response": { "status": 201, "headers": { ... }, "json": { ... } }
    }
  ],
  "expect": { ... }
}
```

### Requests

The SDK's requests are compared, in order, with `exchanges[i].request`:

- `method`, `path`: exact. `path` has no query string.
- `query`: exact map of decoded query parameters (values are strings). `{}` = no query string.
- `json`: the decoded request body must be deeply equal. `null` = no body at all.
- `headers`: only the listed headers are checked (case-insensitive names). A value is either an exact string or
  a matcher object:
  - `{"$present": true}` / `{"$absent": true}`
  - `{"$regex": "..."}` (search, not full match)
  - `{"$capture": "name"}`: must be present; remember its value
  - `{"$equals": "name"}`: must equal the value captured earlier (used to check that retries reuse the same
    `Idempotency-Key`)

Every scenario expects a `User-Agent` matching `^tmailor-<language>/<semver>` and, except `health`,
`Authorization: Bearer <api_key>`. `health` is public: it must be sent WITHOUT `Authorization` (matcher
`{"$absent": true}`), also when a key is configured. An extra request, or an exchange left unused at the end, fails
the scenario (`"exchanges": []` means the SDK must not send anything). An address token must never appear in the
query string.

### Responses

- `status`, `headers`, and either `json` (serialize it) or `body` (raw text, for non-JSON error pages).
- `{"network_error": "connect" | "read" | "timeout"}` instead of a response: raise the transport error of
  your HTTP library. `connect` = connection refused before anything was sent (always retryable); `read` =
  connection dropped after sending; `timeout` = read timeout.
- `"delay": <seconds>` (with a normal response): the server takes this long. Advance the fake clock by it; if the
  SDK gave the request a smaller timeout, advance the clock by that timeout instead and raise a read timeout.

### Expectations

- `result`: deep **subset** match against the SDK result converted to API field names
  (`from_` -> `from`). Objects: listed keys must match; arrays: same length, each element subset-matched;
  scalars: equal. `null` means the call returns nothing. For list operations (`mailboxes.list`,
  `mailbox.messages`) the result is the full array the iterator produced.
- `error`: the call must fail with
  - `class`: neutral class name, mapped by each SDK (Python: `MailboxNotFound` -> `MailboxNotFoundError`,
    `InvalidResponse` -> `InvalidResponseError`,
    `BadRequest` -> `BadRequestError`, `ServiceUnavailable` -> `ServiceUnavailableError`,
    `InternalServerError`, `ConnectionError` -> `APIConnectionError`, `Timeout` -> `APITimeoutError`,
    `WaitTimeout` -> `WaitTimeoutError`; the class must be a subclass of the SDK's root error);
  - `code`: the API code, `HTTP<status>` when the body had none, or `ConnectionError` / `Timeout` /
    `WaitTimeout` / `InvalidResponse` / `MissingKey` (no key configured, status `null`);
  - `status`: HTTP status or `null`;
  - optional `request_id`, `retry_after`, `message`, `secret_changed`, `reason`.
  The error text must not contain the API key or any token.
- `sleeps`: exact list of seconds the SDK slept (Retry-After or wait schedule), in order.
- `sleep_count`: number of sleeps when their length is random (exponential backoff with jitter).
- `rate_limit`: `{limit, remaining, reset}` the client exposes after the call.
- `elapsed_max`: the fake monotonic clock (sleeps plus `delay` of slow responses) must not be past this value
  when the call returns or raises.
- `mailbox`: `{token}` the mailbox object must hold afterwards (create); `null` = no token.
- `otp`: expected `Message.otp()` of the result.
- `codes`, `links`: expected `Message.codes()` / `Message.links()` of the result.

Since API 1.3.0 every `POST /v1/mailbox/message` response carries `otp`, `codes` and `links` found by the server
(`system/api_openapi/src/extract.php`). An SDK returns these values as they are (`otp: null` stays `null`, see
`mailbox_message_server_otp_null`); it does not need its own extractor or HTML parser. A local fallback (as in
Python, for list items and older servers) is optional.

### Clock

SDKs must let tests replace `sleep`, a monotonic clock and wall-clock time. In the runner every sleep advances
the fake monotonic clock; wall time = `clock.wall_start` + monotonic time (default `wall_start`
1760342400). `X-RateLimit-Reset` in the scenarios is relative to that wall time.

### Operations and argument names

| op | args |
|---|---|
| `health`, `me`, `domains` | |
| `usage` | `from`, `to` (`YYYY-MM-DD`) |
| `mailboxes.create` | `domain`, `name`, `idempotency_key` |
| `mailboxes.list` | `limit` (total cap), `page_size` |
| `mailbox.info`, `mailbox.clear`, `mailbox.delete` | |
| `mailbox.messages` | `unread`, `from`, `subject`, `since`, `limit` (total cap), `page_size` |
| `mailbox.message`, `mailbox.delete_message` | `message_id` |
| `mailbox.wait_for_message` | `timeout`, `from`, `subject_contains`, `poll_interval`, `since` |

Map neutral names to the language's conventions (Python `from_`, Go `From`, ...).

## Behaviour every SDK must share

These rules are what the scenarios check; keep them identical across languages.

1. **Retries** (`max_retries`, default 2):
   - 429 `RateLimited`: retry ANY request after `Retry-After` (header, else `error.retry_after`) unless it is
     longer than `max_retry_wait` (default 60). 429 `DailyCapReached`: never.
   - 500/502/503/504 and network errors: only **safe** requests: GETs, the read-only POSTs `mailbox/info`,
     `mailbox/messages`, `mailbox/message` (it marks the message as read, which is idempotent), and `POST /v1/mailboxes` (always sent with an Idempotency-Key).
     Exception: a connection that failed before sending anything is retried for any request.
   - 503 `CacheUnavailable`: never retried.
   - Backoff without Retry-After: random in `[c/2, c]`, `c = min(8, 0.5 * 2^attempt)`.
   - A `Retry-After` of 0 or less waits 1 s (never a zero-length retry loop).
2. **Idempotency**: `mailboxes.create` generates a UUID `Idempotency-Key` unless one is given; every retry
   sends the same key AND byte-identical body.
3. **Tokens**: always in the JSON body field `token`, never in the URL or a header. The Open API cannot rotate
   tokens (that is done on https://platform.tmailor.com); after a rotation there, the old token gets
   `404 MailboxNotFound`.
4. **Pagination**: iterators follow `next_cursor` until null; `limit` stops early without fetching more pages;
   default page size 50 (max 100). `mailbox.messages` sends `limit` (page size) in the body. A `next_cursor`
   that the same iteration already received (repeat or cycle) raises `InvalidResponse` (status `null`).
5. **wait_for_message**: without `since`, list current ids once (all pages, page size 100, NO `from` /
   `subject`), then poll page 1 with page size 100 and the server-side filters `from` / `subject`; return the
   oldest message whose id was not in the baseline, fetched with `mailbox/message`. With `since`, skip the
   baseline and send `since` (exclusive on the server: `received_at > since`; examples use
   `created_at - 1`).
   Delays: 1 s, x1.5 per poll, max 10 s (fixed if `poll_interval`, min 0.5 s); if `X-RateLimit-Remaining`
   <= 2, wait until `X-RateLimit-Reset` + 0.5 s (max 61 s); if remaining < max(5, limit/5), spread the
   remaining requests (minus 2) until the reset. Never sleep past the deadline. The deadline also bounds every
   HTTP request of the wait (baseline, polls, retries and the final `mailbox/message`): with no time left raise
   `WaitTimeout` without sending; otherwise the request timeout is `min(client timeout, time left)`, and a timeout
   caused by that cap is `WaitTimeout` (not `Timeout`). The last poll starts at least 1 s before the deadline
   (a sleep or a Retry-After that would leave less than 1 s ends the wait with `WaitTimeout`); `timeout: 0`
   sends nothing. A 429 `RateLimited` during the wait (baseline or poll) is NOT retried by the
   transport: the wait sleeps `Retry-After` (at least 1 s) if it fits before the deadline, else raises
   `WaitTimeout`. Other retries inside the wait (5xx, network) are skipped when their delay would reach the
   deadline (the error is raised).
6. **Errors**: map `error.code`; unknown code -> class of the HTTP status; non-JSON body -> code `HTTP<status>`;
   `request_id` from the body, else the `X-Request-Id` header. A 2xx body that is not JSON, misses a required
   field, has a wrong JSON type (no coercion: `"1"` is not an integer, `"false"` is not a boolean) or `null` where
   the schema does not allow it raises `InvalidResponse` with the HTTP status; the error never holds the body.
7. **Secrets**: the API key and tokens never appear in string representations, error messages or logs.
   Chained transport exceptions (which can carry request headers) and JSON decoder errors (which hold the body)
   are dropped.
8. **User-Agent**: `tmailor-<language>/<version>`.
9. **API key**: optional when the client is built; only operations other than `health` need it, and they raise
   `MissingKey` (status `null`) before sending anything when it is missing.
10. **Scope**: the SDK covers creating addresses and checking or reading their messages only. It has no domain
   check, token rotation or "mark as seen" call: those endpoints were removed from the Open API on 2026-10-17
   (domain MX records are checked by the system, and "Check now" and token rotation are on
   https://platform.tmailor.com). Reading one message (`mailbox.message`, also the last step of
   `wait_for_message`) marks it as read, so its response has `seen: true`. `mailboxes.create` with a `name` that
   was already used on the tmailor.com website or mobile app fails with `409 MailboxExists`.

## Adding a scenario

Write a new JSON file (the `id` equals the file name), run every SDK's conformance test, and fix the SDKs that
fail. Changing an existing scenario is a behaviour change: note it in each SDK's CHANGELOG.
