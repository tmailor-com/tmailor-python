# Changelog

## 0.1.0 (unreleased)

First version, for Open API v1 (spec 1.2.0). The history below records the changes made before the first release.

- `Client` and `AsyncClient`: `health`, `me`, `domains`, `usage`, `mailboxes.create/list/list_page`,
  `mailbox(token)`.
- `Mailbox`: `info`, `messages` (auto-paginating), `messages_page`, `message` (marks the message as read),
  `delete_message`, `clear`, `delete`, `wait_for_message`.
- `Message.otp()` and `Message.links()` (local, best effort).
- Typed errors for every API error code, network errors and wait timeouts.
- Automatic retries for 429 / 5xx / network errors on safe requests; `Idempotency-Key` on create.
- API key and address tokens are never shown by `repr()`, `str()` or errors.

### Contract update 2026-10-17 (version kept at 0.1.0, never published)

The Open API now only creates addresses and checks or reads their messages. The SDK follows the new REST
contract; nothing was released before, so there are no deprecated aliases.

Removed:

- `Client.domain_check()` / `AsyncClient.domain_check()` and the models `DomainCheck`, `ExpectedMx`
  (`GET /v1/domains/{domain}/check` is gone: the system checks MX records itself, "Check now" is on
  https://platform.tmailor.com). `domains()` stays.
- `Mailbox.rotate_token()` / `AsyncMailbox.rotate_token()` (`POST /v1/mailbox/token/rotate` is gone: tokens are
  rotated on the platform only), with its token scrubbing helpers and the internal `on_success` hook.
- `Mailbox.mark_seen()` / `AsyncMailbox.mark_seen()` (`POST /v1/mailbox/message/seen` is gone).
- CLI commands `seen` and `rotate`, and the `t` (rotate) command of `tmailor shell`.

Changed:

- `Mailbox.message()` (and the message returned by `wait_for_message`) marks the message as read on the server;
  the response has `seen: true`. The request stays safe to retry (marking twice changes nothing).
- `MailboxExistsError` (409) is also raised when `create(name=...)` on your own domain uses a name that was created
  on the tmailor.com website or mobile app (message "This address was created on the website or mobile app and
  cannot be used through the API. Pick another name"). Documented in the README.
- Conformance: removed `domain_check_*` (3), `mailbox_rotate_*` (4) and `mailbox_mark_seen*` (2); the responses of
  `POST /v1/mailbox/message` in `mailbox_message_*` and `wait_for_message_*` now have `seen: true`; new
  `mailboxes_create_name_used_on_website` and `mailbox_message_read_seen_false` (a full message must come back with
  `seen: true`; anything else raises `InvalidResponseError`), 79 in total.

### Pre-release review fixes (review 2026-10-17d, SDK-01 to SDK-10; version kept at 0.1.0, never published)

- `wait_for_message` never sleeps past its timeout: 429 inside the wait is handled only by the wait plan (no
  transport retry with a fresh budget per poll), other retries inside the wait stop at the deadline, and a
  `Retry-After` of 0 or less waits 1 s everywhere (no busy loop). (SDK-01)
- Iterators (`mailboxes.list`, `messages`, the wait baseline) raise `InvalidResponseError` when the API repeats a
  pagination cursor or returns to an earlier one. (SDK-02)
- `rotate_token()` always replaces the held token with the value of the 200 response, `None` included; an invalid
  200 body leaves `None` and raises `InvalidResponseError`. (SDK-03)
- A 2xx body that is not JSON raises `InvalidResponseError` with no chained decoder error (`__cause__` and
  `__context__` are empty), so the body never travels with the exception. (SDK-04)
- Docs and examples use `since=(box.created_at or 0) - 1`: `since` is exclusive in whole seconds. (SDK-05)
- Responses are validated per endpoint (required fields, exact JSON types, `null` only where the schema allows
  it, `token` equal to `web.accesstoken`); violations raise the new `InvalidResponseError` (code
  `InvalidResponse`) without the body. `from_api` no longer coerces (`"false"` is not `True`, `"12"` is not 12).
  `DomainCheck.expected_mx`/`checked_at` and `Message.expires_at` are no longer optional. (SDK-06)
- `health()` needs no API key and never sends `Authorization`; a client without a key can be built, and other
  calls raise `MissingKeyError` (status `None`) before sending anything. (SDK-07)
- The `wait_for_message` baseline lists all current messages without `from`/`subject`; only polls are
  filtered. (SDK-08)
- `links()` / `html_to_text()` use `html.parser`: only an `<a>` attribute named exactly `href` with an absolute
  http(s) URL counts (`data-href`, `onclick`, `title` are ignored); attribute values never reach the text pass.
  (SDK-09)
- The sdist ships the conformance scenarios at `conformance/scenarios/`; the conformance test fails (no longer
  skips) when they are missing or fewer than expected. (SDK-10)
- Conformance scenarios changed: `health_ok`, `health_degraded_503` (no Authorization), `wait_for_message_new_only`
  (unfiltered baseline); 17 new scenarios (83 in total).

### Pre-release review fixes (review 2026-10-17e; version kept at 0.1.0, never published)

- `wait_for_message`: `timeout` now bounds the HTTP requests too. Before each request the time left is checked
  (none left: `WaitTimeoutError` without sending), the request timeout is `min(client timeout, time left)`, and a
  timeout caused by that cap (polls and the final read of the message) is `WaitTimeoutError`, not
  `APITimeoutError`. The last poll starts at least 1 s before the deadline; `timeout=0` sends nothing. (SDK-01)
- `rotate_token()`: the held token is dropped as soon as any 2xx status arrives, before the body is decoded, so a
  non-JSON 200 also leaves `token` `None` (and raises `InvalidResponseError`). (SDK-03)
- Response readers and `docs/openapi.yaml` agree: the fields the server always sends (`Me.key`, `Me.limits`,
  `Me.subscriptions[]`, the body fields of `Message`, `Usage` and its days) are now `required` in the spec; the
  new `tests/test_spec_contract.py` fails if a reader ever requires a field the spec does not, or rejects a `null`
  the spec allows. (SDK-11)
- Conformance: `wait_for_message_timeout` changed (no poll at the deadline: sleeps `[1]`); new scenarios
  `wait_for_message_timeout_zero`, `wait_for_message_slow_poll_deadline`, `wait_for_message_slow_final_read`
  (86 in total). Scenario format: `response.delay`, `client.timeout`, `expect.elapsed_max`.

### Pre-release review fixes (review 2026-10-17f; version kept at 0.1.0, never published)

- `wait_for_message`: the deadline is now a total bound, not only the per-phase httpx timeout. `AsyncClient` wraps
  each request of the wait in `asyncio.wait_for` (cancelled at the deadline); both clients turn a response that
  ends after the deadline into `WaitTimeoutError`; a request whose timeout equals the time left is deadline-bound.
  Documented limit of the sync client: one trickling read can overrun by at most its read timeout. (SDK-01)
- `rotate_token()`: on any 2xx the mailbox also scrubs `raw` (`token`, `web`, `accesstoken`, `transfertoken`,
  `reuse_url` at any depth), so after a non-JSON or invalid 200 no attribute or `repr` holds a token. (SDK-03)
- `base_url` is validated (`BaseURLError`, a `ValueError`): https only, no user info, query or fragment; a host
  other than `open-api.tmailor.com` needs `allow_untrusted_base_url=True`; `http://` only for a loopback host
  with that flag. Applies to `TMAILOR_BASE_URL`. (SDK-12)
- CLI: `--allow-untrusted-base-url`; `login` never saves a base URL. (SDK-12)
- CLI: config and token files are read with `O_NOFOLLOW` and checked with `fstat` on the same descriptor
  (regular file, owned by you, mode 600 or stricter, safe directory; stricter rules as root) and written through
  a random `O_EXCL` temp file, `fsync` and rename. (SDK-13)
- CLI: every printed line drops control and bidi characters; `--json`/`--raw` escape them (pure ASCII on a
  terminal). (SDK-14)
- CLI: `rm-msg`, `rotate` and `logout` ask for confirmation like `clear`/`delete` (`-y/--yes`). (SDK-15)
- CLI: `--token TOKEN` removed (refused with a hint); use `--token-stdin`, `--token-file FILE` or `--ask-token`.
  (SDK-16)

### Pre-release review fixes (review 2026-10-17g; version kept at 0.1.0, never published)

- CLI private files (`TMAILOR_CONFIG`, `~/.tmailor`, `--token-file`): the directory is no longer checked by path
  and then reopened. The path is walked from `/` with `openat(O_DIRECTORY | O_NOFOLLOW)`, each descriptor is
  checked with `fstat` (owner you or root, not writable by others unless sticky, for every user including root)
  and the read, write, rename and unlink use the final descriptor. A symbolic link anywhere in the path is
  refused. Also fixed: the dir_fd support test was always false (`os.replace` is never in
  `os.supports_dir_fd`), so 17f silently used paths. (SDK-13)
- CLI: argparse output (usage, errors that quote argv, help, version) goes through one writer that escapes C0,
  DEL, C1 and bidi controls as `\uXXXX`; exit code 2 unchanged. (SDK-14)

### Pre-release review fixes (review 2026-10-17h; version kept at 0.1.0, never published)

- CLI private files fail closed: on POSIX without `dir_fd` (open/stat/unlink/rename), `O_NOFOLLOW` or
  `O_DIRECTORY` the config file and `--token-file` are refused with a clear error before any file system call
  (no path fallback); `TMAILOR_API_KEY` and `--token-stdin` keep working. Windows has its own documented branch
  (inside the user profile only, reparse points refused, opened file must match the checked one). (SDK-18)
- CLI: the temporary file holding the key is removed whenever the rename did not happen (write, fsync or
  `os.replace` failure), never after a successful rename. (SDK-19)

### Command line tool

- New `tmailor` command (and `python -m tmailor`), standard library only: `me`, `domains`, `usage`, `health`,
  `create`, `list`, `inbox`, `read`, `wait`, `rm-msg`, `clear`, `delete`, `token`, `shell`,
  `login`, `logout` (`wait --any` also accepts a message already in the inbox). Addresses by e-mail (token looked up once per run) or a token from stdin/file/hidden prompt; `--json`, `--raw`,
  `--base-url`, `--debug`; API key from `TMAILOR_API_KEY`, `~/.tmailor` (mode 600 enforced) or a hidden prompt;
  the key is never printed and tokens only by `tmailor token`; confirmation for `clear`/`delete` (`--yes`);
  exit codes 0/1/2/3/130.
