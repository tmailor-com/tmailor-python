"""Tests of the ``tmailor`` command line tool (offline, httpx.MockTransport)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx
import pytest

import tmailor
from tmailor import cli

KEY = "tmk_live_abcdefabcdef_SUPERSECRETvalue000000000000000000000000000"
TOK_A = "eyJhbGciOi.SECRET-TOKEN-A.sig"
TOK_B = "eyJhbGciOi.SECRET-TOKEN-B.sig"
SECRETS = ("SUPERSECRET", "SECRET-TOKEN", "SECRET-TRANSFER")
MID = "5d41402abc4b2a76b9719d911017c592"


def _box(email: str, token: str, count: int = 1) -> Dict[str, Any]:
    return {
        "email": email,
        "domain": email.split("@")[1],
        "is_user_domain": False,
        "custom_name": False,
        "created_at": 1760342400,
        "message_count": count,
        "token": token,
        "web": {
            "accesstoken": token,
            "transfertoken": "eyJhbGciOi.SECRET-TRANSFER.sig",
            "reuse_url": "https://tmailor.com/reuse-temp-mail-address#accesstoken/" + token,
        },
    }


ITEM = {
    "id": MID,
    "from": {"name": "Shop", "address": "no-reply@shop.example"},
    "to": "a@x.example",
    "subject": "Your code \x1b[31mred\x1b[0m",
    "received_at": 1760342460,
    "expires_at": 1760428860,
    "seen": False,
}
FULL = dict(
    ITEM,
    seen=True,  # reading one message marks it as read
    text="Your verification code is 482913. Open https://shop.example/verify?t=1 to finish.",
    html=None,
    body_available=True,
)
ME = {
    "user_id": 12,
    "key": {"key_id": 1, "prefix": "abcdefabcdef"},
    "client_ip": "203.0.113.10",
    "limits": {
        "quota_total": 100,
        "quota_used": 3,
        "quota_remaining": 97,
        "mailbox_count": 2,
        "rate_per_min": 30,
        "daily_req_cap": 0,
        "max_ip": 1,
    },
    "ip_allowlist": ["203.0.113.10"],
    "subscriptions": [{"plan": "free", "expires_at": 0, "period_end": 1762934400, "quota": 100, "used": 3}],
}


class Server:
    """Fake API. Records (method, path, json body) of every request."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Any]] = []
        self.inbox_pages: List[List[Dict[str, Any]]] = []
        self.override: Dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.bases: List[Tuple[str, bool]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        path = req.url.path
        if path in self.override:
            return self.override[path](req)
        tok = (body or {}).get("token")
        if path.startswith("/v1/mailbox/") and tok not in (TOK_A, TOK_B):
            return _error(404, "MailboxNotFound", "Mailbox not found")
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "time": 1760342400, "version": "v1"})
        if path == "/v1/me":
            return httpx.Response(200, json=ME)
        if path == "/v1/mailboxes" and req.method == "GET":
            return httpx.Response(
                200, json={"items": [_box("a@x.example", TOK_A), _box("b@x.example", TOK_B, 0)], "next_cursor": None}
            )
        if path == "/v1/mailboxes":
            return httpx.Response(201, json=_box("new@x.example", "eyJhbGciOi.SECRET-TOKEN-NEW.sig", 0))
        if path == "/v1/mailbox/messages":
            items = self.inbox_pages.pop(0) if self.inbox_pages else [ITEM]
            return httpx.Response(200, json={"items": items, "next_cursor": None, "total": len(items)})
        if path == "/v1/mailbox/message":
            return httpx.Response(200, json=FULL)
        if path in ("/v1/mailbox/message/delete", "/v1/mailbox/delete"):
            return httpx.Response(204)
        if path == "/v1/mailbox/messages/clear":
            return httpx.Response(200, json={"deleted": 2})
        return _error(404, "NotFound", "Not found")

    def paths(self) -> List[str]:
        return [p for _, p, _ in self.calls]


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message, "description": "Details here.", "request_id": "req-123"}}
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("TMAILOR_API_KEY", KEY)
    monkeypatch.delenv("TMAILOR_BASE_URL", raising=False)
    cfg = tmp_path / "tmailor.cfg"
    monkeypatch.setenv(cli.CONFIG_ENV, str(cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    return cfg


def run(server: Server, *argv: str) -> Tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    seen_keys: List[Optional[str]] = []

    def factory(key: Optional[str], base: str, allow: bool) -> tmailor.Client:
        seen_keys.append(key)
        server.bases.append((base, allow))
        c = tmailor.Client(
            key,
            base_url=base,
            allow_untrusted_base_url=allow,
            http_client=httpx.Client(transport=httpx.MockTransport(server)),
            max_retries=0,
        )
        c._sleep = lambda s: None
        return c

    code = cli.main(list(argv), factory=factory, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def stdin_text(text: str) -> Iterator[None]:
    old = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = old


def no_secret(*texts: str) -> None:
    joined = "\n".join(texts)
    for s in SECRETS:
        assert s not in joined, f"secret {s} leaked"


# ── argument parsing
@pytest.mark.parametrize(
    "argv",
    [
        ["read", "a@x.example"],
        ["inbox"],
        ["delete"],
        ["wait", "a@x.example", "--timeout", "-1"],
        ["nope"],
        ["inbox", "a@x.example", "--limit", "0"],
    ],
)
def test_usage_errors_exit_2(env: Path, argv: List[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        run(Server(), *argv)
    assert ei.value.code == 2


def test_no_command_prints_help(env: Path) -> None:
    code, out, _ = run(Server())
    assert code == 2 and "COMMAND" in out


def test_global_options_before_or_after_command(env: Path) -> None:
    for argv in (["--json", "me"], ["me", "--json"]):
        code, out, _ = run(Server(), *argv)
        assert code == 0 and json.loads(out)["user_id"] == 12


def test_inbox_filters_go_to_the_server(env: Path) -> None:
    s = Server()
    code, out, _ = run(s, "inbox", "A@X.example", "--unread", "--from", "shop", "--subject", "code")
    assert code == 0 and MID in out
    _method, path, body = s.calls[-1]
    assert path == "/v1/mailbox/messages" and body["token"] == TOK_A
    assert body["unread"] is True and body["from"] == "shop" and body["subject"] == "code"
    assert "\x1b" not in out  # terminal escape codes from the subject are stripped


# ── e-mail -> token lookup
def test_email_resolved_once_and_cached(env: Path) -> None:
    s = Server()
    client = tmailor.Client(
        KEY, base_url="https://x.test", allow_untrusted_base_url=True, http_client=httpx.Client(transport=httpx.MockTransport(s))
    )
    r = cli.Resolver(client)
    assert r.get("b@x.example", None).token == TOK_B
    assert r.get("A@x.example", None).token == TOK_A
    assert s.paths() == ["/v1/mailboxes"]
    with pytest.raises(cli.CliError):
        r.get("zzz@x.example", None)  # miss: reloaded once, then an error
    assert s.paths() == ["/v1/mailboxes", "/v1/mailboxes"]
    client.close()


def test_unknown_email_and_token_option(env: Path) -> None:
    code, _, err = run(Server(), "inbox", "nobody@x.example")
    assert code == 1 and "not an address of this account" in err
    s = Server()
    with stdin_text(TOK_B + "\n"):
        code, out, _ = run(s, "read", "--token-stdin", MID)
    assert code == 0 and "482913" in out
    assert s.paths() == ["/v1/mailbox/message"] and s.calls[0][2]["token"] == TOK_B  # no list call with a token


def test_read_shows_otp_links_and_text(env: Path) -> None:
    code, out, err = run(Server(), "read", "a@x.example", MID)
    assert code == 0
    assert "OTP:      482913" in out and "https://shop.example/verify?t=1" in out and "Your verification code" in out
    no_secret(out, err)
    code, out, _ = run(Server(), "--json", "read", "a@x.example", MID)
    d = json.loads(out)
    assert d["otp"] == "482913" and d["links"] == ["https://shop.example/verify?t=1"]


# ── secrets never printed (except `tmailor token`)
@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["--raw", "list"],
        ["--json", "list"],
        ["create"],
        ["--raw", "create"],
        ["read", "a@x.example", MID],
        ["--json", "read", "a@x.example", MID],
        ["me"],
        ["--raw", "me"],
        ["--debug", "inbox", "--token-stdin"],
    ],
)
def test_no_secret_in_output(env: Path, argv: List[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("eyJhbGciOi.SECRET-TOKEN-ZZZ.sig\n"))
    code, out, err = run(Server(), *argv)
    assert code == (1 if "--debug" in argv else 0)
    no_secret(out, err)
    assert KEY not in out + err


def test_token_command_prints_token_with_warning(env: Path) -> None:
    code, out, err = run(Server(), "token", "a@x.example")
    assert code == 0 and out.strip() == TOK_A and "Warning" in err


def test_debug_traceback_is_redacted(env: Path) -> None:
    s = Server()
    s.override["/v1/mailbox/messages"] = lambda req: (_ for _ in ()).throw(RuntimeError("boom " + KEY + " " + TOK_A))
    code, out, err = run(s, "--debug", "inbox", "a@x.example")
    assert code == 1 and "Traceback" in err and "RuntimeError" in err
    no_secret(out, err)
    code, out, err = run(s, "inbox", "a@x.example")
    assert "Traceback" not in err and "--debug" in err
    no_secret(out, err)


# ── confirmation for delete / clear
def test_delete_needs_yes_without_terminal(env: Path) -> None:
    s = Server()
    code, _, err = run(s, "delete", "a@x.example")
    assert code == 1 and "--yes" in err and "/v1/mailbox/delete" not in s.paths()
    code, out, _ = run(s, "delete", "a@x.example", "--yes")
    assert code == 0 and "Deleted a@x.example" in out and s.paths()[-1] == "/v1/mailbox/delete"


@pytest.mark.parametrize(("answer", "deleted"), [("yes", True), ("no", False), ("", False)])
def test_delete_and_clear_ask_on_terminal(env: Path, monkeypatch: pytest.MonkeyPatch, answer: str, deleted: bool) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    for argv, path in (
        (["delete", "a@x.example"], "/v1/mailbox/delete"),
        (["clear", "a@x.example"], "/v1/mailbox/messages/clear"),
    ):
        s = Server()
        code, _, err = run(s, *argv)
        assert (path in s.paths()) is deleted
        assert code == (0 if deleted else 1)
        if not deleted:
            assert "Cancelled" in err


# ── exit codes and error output
def test_api_error_shows_code_message_request_id(env: Path) -> None:
    s = Server()
    s.override["/v1/me"] = lambda req: _error(403, "IpNotAllowed", "IP not allowed")
    code, out, err = run(s, "me")
    assert code == 1 and out == ""
    assert "[IpNotAllowed]" in err and "HTTP 403" in err and "request_id=req-123" in err and "Details here." in err
    assert "Traceback" not in err


def test_wait_returns_new_message_and_timeout_exit_3(env: Path) -> None:
    s = Server()
    s.inbox_pages = [[], [], [ITEM]]  # baseline empty, one empty poll, then the message
    code, out, err = run(s, "wait", "a@x.example", "--timeout", "30")
    assert code == 0 and "482913" in out and "Waiting up to 30 s" in err
    s = Server()
    code, out, err = run(s, "wait", "a@x.example", "--timeout", "0")
    assert code == 3 and "No new matching message" in err
    assert "/v1/mailbox/messages" not in s.paths()


def test_ctrl_c_exits_130(env: Path) -> None:
    s = Server()

    def interrupt(req: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    s.override["/v1/mailbox/messages"] = interrupt
    code, _, err = run(s, "wait", "a@x.example")
    assert code == 130 and "Interrupted" in err


def test_missing_key_and_public_health(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    s = Server()
    code, _, err = run(s, "me")
    assert code == 1 and "MissingKey" in err and s.calls == []
    code, out, _ = run(s, "health")
    assert code == 0 and "Status: ok" in out


# ── key file
def test_key_file_permissions(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    env.write_text(f"# comment\napi_key={KEY}\n")
    os.chmod(env, 0o644)
    code, out, err = run(Server(), "me")
    assert code == 1 and "chmod 600" in err and KEY not in err
    os.chmod(env, 0o600)
    code, out, err = run(Server(), "me")
    assert code == 0 and "User ID:        12" in out


def test_login_writes_private_file(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": KEY)
    code, out, err = run(Server(), "login")
    assert code == 0 and env.exists()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert f"api_key={KEY}" in env.read_text()
    no_secret(out, err)
    # A refused key is not saved.
    env.unlink()
    s = Server()
    s.override["/v1/me"] = lambda req: _error(401, "InvalidKey", "Invalid API key")
    code, out, err = run(s, "login")
    assert code == 1 and not env.exists() and "refused" in err
    code, out, _ = run(Server(), "logout", "--yes")
    assert code == 0


@pytest.mark.parametrize("command", ["seen", "rotate"])
def test_removed_commands_are_refused(env: Path, command: str, capsys: pytest.CaptureFixture[str]) -> None:
    # Contract 2026-10-17: no "mark as seen" (reading marks as read) and no token rotation (platform only).
    s = Server()
    with pytest.raises(SystemExit) as ei:
        run(s, command, "a@x.example", MID, "-y")
    assert ei.value.code == 2 and "invalid choice" in capsys.readouterr().err and s.calls == []


def test_rm_msg_clear(env: Path) -> None:
    s = Server()
    assert run(s, "rm-msg", "a@x.example", MID, "-y")[0] == 0
    code, out, _ = run(s, "clear", "a@x.example", "-y")
    assert code == 0 and "Deleted 2 message(s)." in out
    assert [p for p in s.paths() if p != "/v1/mailboxes"] == [
        "/v1/mailbox/message/delete",
        "/v1/mailbox/messages/clear",
    ]


def test_usage_and_domains(env: Path) -> None:
    s = Server()
    s.override["/v1/usage"] = lambda req: httpx.Response(
        200,
        json={
            "from": "2026-10-16",
            "to": "2026-10-17",
            "timezone": "Asia/Ho_Chi_Minh",
            "days": [
                {
                    "date": "2026-10-16",
                    "req_total": 0,
                    "req_4xx": 0,
                    "req_5xx": 0,
                    "req_429": 0,
                    "req_ip_denied": 0,
                    "mailbox_created": 0,
                    "msg_list": 0,
                    "msg_read": 0,
                },
                {
                    "date": "2026-10-17",
                    "req_total": 9,
                    "req_4xx": 1,
                    "req_5xx": 0,
                    "req_429": 0,
                    "req_ip_denied": 0,
                    "mailbox_created": 2,
                    "msg_list": 5,
                    "msg_read": 1,
                },
            ],
        },
    )
    s.override["/v1/domains"] = lambda req: httpx.Response(200, json={"items": [], "shared_domains_available": True})
    code, out, _ = run(s, "usage", "--from", "2026-10-16", "--to", "2026-10-17")
    assert code == 0 and "2026-10-17" in out and "2026-10-16  " not in out
    code, out, _ = run(s, "domains")
    assert code == 0 and "Shared domains available: yes" in out


def test_mask_tokens_and_redact_helpers() -> None:
    masked = cli.mask_tokens({"token": "x", "web": {"accesstoken": "y", "reuse_url": "z"}, "email": "a@b"})
    assert masked == {"token": "***", "web": {"accesstoken": "***", "reuse_url": "***"}, "email": "a@b"}
    assert "SECRET" not in cli.redact(f"key {KEY} tok {TOK_A}")
    assert cli.clean("a\x1b[2Jb\x07c\n") == "a[2Jbc\n"


def test_python_m_entry_point() -> None:
    import subprocess
    import sys

    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(src))
    r = subprocess.run([sys.executable, "-m", "tmailor", "--version"], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0 and tmailor.__version__ in r.stdout


def test_wait_any_uses_since_created(env: Path) -> None:
    s = Server()
    code, out, err = run(s, "wait", "a@x.example", "--any", "--timeout", "10")
    assert code == 0 and "482913" in out and "a matching message" in err
    polls = [b for _, p, b in s.calls if p == "/v1/mailbox/messages"]
    assert len(polls) == 1 and polls[0]["since"] == 1760342399  # no baseline: the message already there counts


def test_shell_scripted_session(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    answers = iter(["?", "1", "i", "1", "c", "yes", "b", "n", "x", "q"])
    monkeypatch.setattr("sys.stdin", Tty(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    s = Server()
    code, out, err = run(s, "shell")
    assert code == 0
    assert "a@x.example" in out and "OTP:      482913" in out and "Deleted 2 message(s)." in out
    assert "Created new@x.example" in out and "Unknown command" in out
    assert "/v1/mailbox/messages/clear" in s.paths() and "/v1/mailboxes" in s.paths()
    no_secret(out, err)


def test_shell_needs_terminal(env: Path) -> None:
    code, _, err = run(Server(), "shell")
    assert code == 1 and "terminal" in err


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


# ── Review 17f SDK-12: base URL
@pytest.mark.parametrize(
    ("argv", "ok", "base"),
    [
        (["me"], True, "https://open-api.tmailor.com"),
        (["--base-url", "https://open-api.tmailor.com/", "me"], True, "https://open-api.tmailor.com"),
        (["--base-url", "https://staging.example", "me"], False, None),
        (["--base-url", "https://staging.example", "--allow-untrusted-base-url", "me"], True, "https://staging.example"),
        (["me", "--base-url", "https://staging.example", "--allow-untrusted-base-url"], True, "https://staging.example"),
        (["--base-url", "http://127.0.0.1:8080", "me"], False, None),
        (["--base-url", "http://127.0.0.1:8080", "--allow-untrusted-base-url", "me"], True, "http://127.0.0.1:8080"),
        (["--base-url", "http://staging.example", "--allow-untrusted-base-url", "me"], False, None),
        (["--base-url", "https://u:p@open-api.tmailor.com", "--allow-untrusted-base-url", "me"], False, None),
        (["--base-url", "https://open-api.tmailor.com/?a=1", "me"], False, None),
        (["--base-url", "https://evil.example", "health"], False, None),
    ],
)
def test_base_url_rules(env: Path, argv: List[str], ok: bool, base: Optional[str]) -> None:
    s = Server()
    code, out, err = run(s, *argv)
    if ok:
        assert code == 0 and s.bases and s.bases[0][0] == base
    else:
        assert code == 2 and s.calls == [] and s.bases == [] and "Refused base URL" in err
    assert KEY not in out + err


def test_base_url_from_env_and_config_needs_flag(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMAILOR_BASE_URL", "https://evil.example")
    s = Server()
    assert run(s, "me")[0] == 2 and s.calls == []
    monkeypatch.delenv("TMAILOR_BASE_URL")
    cli.write_config(env, {"base_url": "https://evil.example"})
    s = Server()
    assert run(s, "me")[0] == 2 and s.calls == []
    assert run(s, "--allow-untrusted-base-url", "me")[0] == 0 and s.bases == [("https://evil.example", True)]


def test_login_never_saves_base_url(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", _Tty(""))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": KEY)
    s = Server()
    code, _, err = run(s, "login", "--base-url", "https://staging.example")
    assert code == 2 and s.calls == [] and not env.exists()
    code, _, err = run(s, "login", "--base-url", "https://staging.example", "--allow-untrusted-base-url")
    assert code == 0 and s.bases == [("https://staging.example", True)]
    assert "base_url" not in env.read_text() and "not saved" in err


# ── Review 17f SDK-13: private files
def test_config_symlink_refused(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    victim = tmp_path / "victim"
    victim.write_text(f"api_key={KEY}\n")
    os.chmod(victim, 0o600)
    env.symlink_to(victim)
    code, _, err = run(Server(), "me")
    assert code == 1 and "symbolic link" in err
    with pytest.raises(cli.CliError, match="symbolic link"):
        cli.write_config(env, {"api_key": "x"})
    assert victim.read_text() == f"api_key={KEY}\n" and env.is_symlink()  # the target was not touched


def test_config_not_regular_file_refused(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    os.mkfifo(env, 0o600)  # a FIFO: opened non-blocking, refused by fstat (no hang)
    code, _, err = run(Server(), "me")
    assert code == 1 and "regular file" in err


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o700, 0o660])
def test_config_mode_refused(env: Path, monkeypatch: pytest.MonkeyPatch, mode: int) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    env.write_text(f"api_key={KEY}\n")
    os.chmod(env, mode)
    code, _, err = run(Server(), "me")
    assert code == 1 and "chmod 600" in err and KEY not in err


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to chown")
def test_config_owned_by_other_user_refused(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY")
    env.write_text(f"api_key={KEY}\n")
    os.chmod(env, 0o600)
    os.chown(env, 65534, 65534)
    code, _, err = run(Server(), "me")
    assert code == 1 and "another user" in err


def test_config_in_writable_directory_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)  # noqa: S103 group/other writable, no sticky bit
    cfg = shared / "tmailor.cfg"
    monkeypatch.setenv(cli.CONFIG_ENV, str(cfg))
    with pytest.raises(cli.CliError, match="other users can write"):
        cli.write_config(cfg, {"api_key": "x"})
    with pytest.raises(cli.CliError, match="other users can write"):
        cli.read_config(cfg)
    assert not cfg.exists()


def test_refuses_config_under_writable_parent(tmp_path: Path) -> None:
    # Review 17g SDK-13: every directory of the path is checked, for a normal user and for root alike.
    parent = tmp_path / "web"
    parent.mkdir()
    os.chmod(parent, 0o777)  # noqa: S103
    private = parent / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    for call in (lambda: cli.write_config(private / "cfg", {"api_key": "x"}), lambda: cli.read_config(private / "cfg")):
        with pytest.raises(cli.CliError, match="other users can write"):
            call()
    assert not (private / "cfg").exists()
    os.chmod(parent, 0o1777)  # noqa: S103 sticky: others cannot rename "private" away
    cli.write_config(private / "cfg", {"api_key": "x"})
    assert cli.read_config(private / "cfg") == {"api_key": "x"}


# ── Review 17g SDK-13: the directory is walked and checked by descriptor
def test_symlink_in_parent_component_refused(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    os.chmod(real, 0o700)
    cfg = real / "sub" / "cfg"
    cli.write_config(cfg, {"api_key": KEY})
    link = tmp_path / "link"
    link.symlink_to(real)
    via_link = link / "sub" / "cfg"
    for call in (
        lambda: cli.read_config(via_link),
        lambda: cli.write_config(via_link, {"api_key": "x"}),
        lambda: cli.remove_private_file(via_link),
    ):
        with pytest.raises(cli.CliError, match="symbolic link"):
            call()
    assert cfg.exists() and f"api_key={KEY}" in cfg.read_text()  # untouched
    monkeypatch.delenv("TMAILOR_API_KEY")
    monkeypatch.setenv(cli.CONFIG_ENV, str(via_link))
    code, _, err = run(Server(), "me")
    assert code == 1 and "symbolic link" in err and KEY not in err
    tf = real / "sub" / "tok"
    tf.write_text(TOK_B)
    os.chmod(tf, 0o600)
    code, _, err = run(Server(), "inbox", "--token-file", str(link / "sub" / "tok"))
    assert code == 1 and "symbolic link" in err and TOK_B not in err


@pytest.mark.parametrize("mode", [0o770, 0o757, 0o722])
def test_wrong_mode_on_intermediate_directory_refused(tmp_path: Path, mode: int) -> None:
    top = tmp_path / "top"
    (top / "a" / "b").mkdir(parents=True)
    os.chmod(top / "a", mode)
    tf = top / "a" / "b" / "tok"
    with pytest.raises(cli.CliError, match="other users can write"):
        cli.write_private_file(tf, "x")
    assert not tf.exists()


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to chown")
def test_intermediate_directory_owned_by_other_user_refused(tmp_path: Path) -> None:
    top = tmp_path / "other"
    (top / "inner").mkdir(parents=True)
    os.chown(top, 65534, 65534)
    with pytest.raises(cli.CliError, match="another user"):
        cli.read_private_file(top / "inner" / "f", "token file")


@pytest.mark.skipif(not cli._DIR_FD, reason="needs dir_fd support")
def test_directory_swapped_after_check_is_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate an attacker replacing the directory right after it was checked: the read, the write and the
    unlink keep using the checked descriptor, never the new directory at that path."""
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o700)
    cli.write_private_file(d / "f", "original\n")
    real_check = cli._check_dir_stat

    def swap_after_check(st: os.stat_result, directory: str, final: bool) -> None:
        real_check(st, directory, final)
        if final and directory == str(d) and not (tmp_path / "moved").exists():
            os.rename(d, tmp_path / "moved")
            d.mkdir()
            os.chmod(d, 0o700)
            (d / "f").write_text("evil\n")
            os.chmod(d / "f", 0o600)

    monkeypatch.setattr(cli, "_check_dir_stat", swap_after_check)
    assert cli.read_private_file(d / "f", "token file") == "original\n"
    assert (d / "f").read_text() == "evil\n"
    for p in (tmp_path / "moved", d):
        if p.exists():
            for c in p.iterdir():
                c.unlink()
            p.rmdir()
    d.mkdir()
    os.chmod(d, 0o700)
    cli.write_private_file(d / "f", "new\n")  # swap happens during this call
    assert (tmp_path / "moved" / "f").read_text() == "new\n" and (d / "f").read_text() == "evil\n"
    assert cli.remove_private_file(d / "f")  # no swap now ("moved" exists): removes at the checked path
    assert not (d / "f").exists() and (tmp_path / "moved" / "f").exists()


def test_private_file_directly_in_checked_tree(tmp_path: Path) -> None:
    deep = tmp_path / "x" / "y" / "z"
    deep.mkdir(parents=True)
    cli.write_private_file(deep / "f", "ok")
    assert cli.read_private_file(deep / "f", "token file") == "ok"
    assert cli.read_private_file(deep / "missing", "token file") is None
    assert cli.remove_private_file(deep / "f") and not cli.remove_private_file(deep / "f")
    (deep / "plain").touch()
    with pytest.raises(cli.CliError, match="not a directory"):
        cli.read_private_file(deep / "plain" / "f", "token file")


def test_write_config_atomic_private_no_leftover(env: Path) -> None:
    env.write_text("old\n")
    os.chmod(env, 0o600)
    cli.write_config(env, {"api_key": KEY})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and f"api_key={KEY}" in env.read_text()
    assert sorted(p.name for p in env.parent.iterdir()) == [env.name]  # random temp file renamed, none left


# ── Review 17f SDK-14: terminal output
EVIL = "Hi \x9b31m‮evil⁦ \x1b[2J done"


def test_raw_json_escape_c1_and_bidi(env: Path) -> None:
    s = Server()
    s.override["/v1/mailbox/message"] = lambda req: httpx.Response(200, json=dict(FULL, subject=EVIL))
    for flag in ("--raw", "--json"):
        code, out, _ = run(s, flag, "read", "a@x.example", MID)
        assert code == 0
        for ch in ("\x9b", "‮", "⁦", "\x1b"):
            assert ch not in out
        assert "\\u009b" in out and "\\u202e" in out and json.loads(out)["subject"] == EVIL  # same data once decoded


def test_json_on_terminal_is_ascii(env: Path) -> None:
    s = Server()
    s.override["/v1/mailbox/message"] = lambda req: httpx.Response(200, json=dict(FULL, subject=EVIL + " café"))
    out = _Tty()
    code = cli.main(
        ["--json", "read", "a@x.example", MID],
        factory=lambda k, b, a: tmailor.Client(
            k, base_url=b, allow_untrusted_base_url=a, http_client=httpx.Client(transport=httpx.MockTransport(s))
        ),
        out=out,
        err=io.StringIO(),
    )
    text = out.getvalue()
    assert code == 0 and text.isascii() and "\\u00e9" in text


def test_text_mode_strips_bidi_and_error_text_cleaned(env: Path) -> None:
    s = Server()
    s.override["/v1/mailbox/message"] = lambda req: httpx.Response(200, json=dict(FULL, subject=EVIL))
    code, out, _ = run(s, "read", "a@x.example", MID)
    assert code == 0 and "Subject:  Hi 31mevil [2J done" in out
    s = Server()
    s.override["/v1/me"] = lambda req: _error(403, "IpNotAllowed", "bad \x1b]0;title\x07 ‮ip")
    code, out, err = run(s, "me")
    assert code == 1 and "\x1b" not in err and "‮" not in err and "\x07" not in err


# ── Review 17f SDK-15: confirmations
@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (["rm-msg", "a@x.example", MID], "/v1/mailbox/message/delete"),
        (["delete", "a@x.example"], "/v1/mailbox/delete"),
    ],
)
def test_rm_msg_and_delete_need_confirmation(env: Path, monkeypatch: pytest.MonkeyPatch, argv: List[str], path: str) -> None:
    s = Server()
    code, _, err = run(s, *argv)  # no terminal, no --yes
    assert code == 1 and "--yes" in err and path not in s.paths()
    monkeypatch.setattr("sys.stdin", _Tty(""))
    for answer, done in (("no", False), ("yes", True)):
        monkeypatch.setattr("builtins.input", lambda prompt="", a=answer: a)
        s = Server()
        code, _, err = run(s, *argv)
        assert (path in s.paths()) is done and code == (0 if done else 1)
    s = Server()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(s, *argv, "-y")[0] == 0 and path in s.paths()


def test_logout_needs_confirmation(env: Path) -> None:
    cli.write_config(env, {"api_key": KEY})
    code, _, err = run(Server(), "logout")
    assert code == 1 and "--yes" in err and env.exists()
    code, out, _ = run(Server(), "logout", "--yes")
    assert code == 0 and not env.exists() and "Removed" in out


# ── Review 17f SDK-16: no token on the command line
def test_token_argument_removed(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = Server()
    with pytest.raises(SystemExit) as ei:
        run(s, "inbox", "--token", TOK_A)
    assert ei.value.code == 2 and s.calls == []
    assert "--token-stdin" in capsys.readouterr().err
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["inbox"].format_help()  # type: ignore[union-attr,index]
    assert "--token-file" in help_text and "--token " not in help_text


def test_token_file(env: Path, tmp_path: Path) -> None:
    tf = tmp_path / "tok"
    tf.write_text("\n" + TOK_B + "\n")
    os.chmod(tf, 0o600)
    s = Server()
    code, out, err = run(s, "inbox", "--token-file", str(tf))
    assert code == 0 and s.paths() == ["/v1/mailbox/messages"] and s.calls[0][2]["token"] == TOK_B
    no_secret(out, err)
    os.chmod(tf, 0o644)
    s = Server()
    code, _, err = run(s, "inbox", "--token-file", str(tf))
    assert code == 1 and "chmod 600" in err and s.calls == []
    os.chmod(tf, 0o600)
    link = tmp_path / "toklink"
    link.symlink_to(tf)
    code, _, err = run(Server(), "inbox", "--token-file", str(link))
    assert code == 1 and "symbolic link" in err
    code, _, err = run(Server(), "inbox", "--token-file", str(tmp_path / "missing"))
    assert code == 2


def test_token_stdin_and_hidden_prompt(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Server()
    with stdin_text(""):
        code, _, err = run(s, "inbox", "--token-stdin")
    assert code == 2 and "No token" in err and s.calls == []
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, _, err = run(Server(), "inbox", "--ask-token")
    assert code == 2 and "terminal" in err
    monkeypatch.setattr("sys.stdin", _Tty(""))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TOK_B)
    s = Server()
    code, out, err = run(s, "read", "--ask-token", MID)
    assert code == 0 and s.calls[0][2]["token"] == TOK_B and "482913" in out
    no_secret(out, err)
    with pytest.raises(SystemExit):
        run(Server(), "inbox", "--token-stdin", "--ask-token")  # one source only


# ── Review 17g SDK-14: argparse messages go through one safe writer
NASTY = "bad\x1b[2J\x07\x9b‮x⁦\x7f"


@pytest.mark.parametrize(
    "argv",
    [
        ["--" + NASTY],
        ["me", NASTY],
        [NASTY],
        ["inbox", "a@x.example", "--limit", NASTY],
        ["wait", "a@x.example", "--timeout", NASTY],
    ],
)
def test_argparse_errors_are_escaped(env: Path, argv: List[str], capsys: pytest.CaptureFixture[str]) -> None:
    s = Server()
    with pytest.raises(SystemExit) as ei:
        run(s, *argv)
    assert ei.value.code == 2 and s.calls == []
    cap = capsys.readouterr()
    text = cap.err + cap.out
    for ch in ("\x1b", "\x07", "\x9b", "‮", "⁦", "\x7f"):
        assert ch not in text
    assert "error:" in text and ("\\u001b" in text or "\\x1b" in text)  # visible escape (ours or argparse repr)


def test_help_and_version_still_work(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--help"], ["--version"], ["inbox", "--help"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "tmailor" in out and "--token-file" in out
    assert isinstance(cli.build_parser()._subparsers._group_actions[0].choices["inbox"], cli.SafeArgumentParser)  # type: ignore[union-attr,index]


# ── Review 17h SDK-18: fail closed where the safe file operations are missing (no path fallback)
_UNSUPPORTED = [
    {"_DIR_FD": False},
    {"_O_NOFOLLOW": 0},
    {"_O_DIRECTORY": 0},
    {"_POSIX": False, "_WINDOWS": False},  # neither POSIX nor Windows
]


@contextlib.contextmanager
def _no_fs_calls(monkeypatch: pytest.MonkeyPatch, root: Path) -> Iterator[List[str]]:
    """Record (and fail) any file system call on a path under ``root`` or relative to a directory descriptor, and
    any use of the private-file walkers, inside the block. Other calls (argparse, gettext...) pass through."""
    touched: List[str] = []
    with monkeypatch.context() as m:
        for fn in ("open", "stat", "lstat", "unlink", "replace", "rename"):
            real = getattr(os, fn)

            def spy(*a: Any, _n: str = fn, _real: Any = real, **k: Any) -> Any:
                if str(root) in str(a[0] if a else "") or "dir_fd" in k or "src_dir_fd" in k:
                    touched.append(_n)
                    raise AssertionError(_n)
                return _real(*a, **k)

            m.setattr(cli.os, fn, spy)
        real_lexists = os.path.lexists
        m.setattr(cli.os.path, "lexists", lambda p: touched.append("lexists") or real_lexists(p))
        for fn in ("_open_dir", "_win_check_dir"):
            m.setattr(cli, fn, lambda *a, _n=fn: touched.append(_n) or (_ for _ in ()).throw(AssertionError(_n)))
        yield touched


@pytest.mark.parametrize("flags", _UNSUPPORTED)
def test_unsupported_platform_fails_closed(flags: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    cfg = d / "cfg"
    cli.write_config(cfg, {"api_key": KEY})
    before = cfg.read_text()
    for k, v in flags.items():
        monkeypatch.setattr(cli, k, v)
    assert not cli.private_files_supported()
    calls: List[Callable[[], Any]] = [
        lambda: cli.read_private_file(cfg, "token file"),
        lambda: cli.read_config(cfg),
        lambda: cli.write_config(cfg, {"api_key": "x"}),
        lambda: cli.write_private_file(d / "new", "x"),
        lambda: cli.remove_private_file(cfg),
    ]
    with _no_fs_calls(monkeypatch, tmp_path) as touched:
        for call in calls:
            with pytest.raises(cli.CliError, match="not supported for config/token files"):
                call()
    assert touched == []
    assert cfg.read_text() == before and sorted(p.name for p in d.iterdir()) == ["cfg"]
    with pytest.raises(cli.CliError, match="not supported for config/token files"):
        cli._open_dir(str(d))  # the walker itself refuses too (never a path fallback)


@pytest.mark.parametrize("flags", _UNSUPPORTED)
def test_unsupported_platform_env_key_and_token_stdin_still_work(
    flags: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMAILOR_API_KEY", KEY)
    monkeypatch.delenv("TMAILOR_BASE_URL", raising=False)
    monkeypatch.delenv(cli.CONFIG_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    for k, v in flags.items():
        monkeypatch.setattr(cli, k, v)
    with _no_fs_calls(monkeypatch, tmp_path) as touched:
        code, out, err = run(Server(), "me")
        assert code == 0, err
        with stdin_text(TOK_B):
            code, out2, err = run(Server(), "inbox", "--token-stdin")
        assert code == 0, err
        for argv in (("inbox", "--token-file", str(tmp_path / "tok")), ("logout", "--yes"), ("login",)):
            code, _, err = run(Server(), *argv)
            assert code == 1 and "TMAILOR_API_KEY" in err and "--token-stdin" in err, argv
    assert touched == []
    no_secret(out, out2, err)
    monkeypatch.setenv(cli.CONFIG_ENV, str(tmp_path / "cfg"))  # explicit config: refused, not ignored
    code, _, err = run(Server(), "me")
    assert code == 1 and "not supported for config/token files" in err


# Review 17i SDK-18: without TMAILOR_API_KEY the default config is skipped too (no "not supported" error, no file
# access); the run ends with the ordinary missing-key error.
@pytest.mark.parametrize("flags", _UNSUPPORTED)
def test_unsupported_platform_no_env_key_skips_default_config(
    flags: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TMAILOR_API_KEY", raising=False)
    monkeypatch.delenv("TMAILOR_BASE_URL", raising=False)
    monkeypatch.delenv(cli.CONFIG_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    for k, v in flags.items():
        monkeypatch.setattr(cli, k, v)
    assert cli.resolve_key(interactive=True) is None
    with _no_fs_calls(monkeypatch, tmp_path) as touched:
        s = Server()
        with stdin_text(TOK_B):
            code, _, err = run(s, "inbox", "--token-stdin")
    assert touched == [] and s.calls == []
    assert code == 1 and "No API key" in err and "not supported" not in err


# Review 17i SDK-18: logout has no path-based existence pre-check; a missing file is reported via remove_private_file.
def test_logout_missing_file_no_path_precheck(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not env.exists()
    monkeypatch.setattr(cli.os.path, "lexists", lambda p: (_ for _ in ()).throw(AssertionError("lexists")))
    code, out, _ = run(Server(), "logout", "--yes")
    assert code == 0 and "Nothing to remove" in out


# ── Review 17h SDK-18: Windows branch (simulated here: path based, reparse points refused, profile only)
@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_POSIX", False)
    monkeypatch.setattr(cli, "_WINDOWS", True)
    assert cli.private_files_supported()
    return home


def test_windows_branch_roundtrip_and_refusals(windows: Path, tmp_path: Path) -> None:
    sub = windows / "AppData" / "tmailor"
    sub.mkdir(parents=True)
    cli.write_private_file(sub / "cfg", "api_key=x\n")
    assert cli.read_private_file(sub / "cfg", "config file") == "api_key=x\n"
    assert sorted(p.name for p in sub.iterdir()) == ["cfg"]
    assert cli.read_private_file(sub / "missing", "config file") is None
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(cli.CliError, match="user profile"):
        cli.write_private_file(outside / "cfg", "x")
    assert not (outside / "cfg").exists()
    (windows / "link").symlink_to(sub)
    for call in (
        lambda: cli.read_private_file(windows / "link" / "cfg", "config file"),
        lambda: cli.write_private_file(windows / "link" / "cfg", "y"),
        lambda: cli.remove_private_file(windows / "link" / "cfg"),
    ):
        with pytest.raises(cli.CliError, match="symbolic link or junction"):
            call()
    (sub / "flink").symlink_to(sub / "cfg")
    for call in (
        lambda: cli.read_private_file(sub / "flink", "config file"),
        lambda: cli.write_private_file(sub / "flink", "y"),
    ):
        with pytest.raises(cli.CliError, match="symbolic link or reparse point"):
            call()
    assert (sub / "cfg").read_text() == "api_key=x\n"
    assert cli.remove_private_file(sub / "cfg") and not cli.remove_private_file(sub / "cfg")


def test_windows_reparse_attribute_refused(windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _St:
        st_mode = stat.S_IFDIR | 0o700
        st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (a junction)

    monkeypatch.setattr(cli.os, "lstat", lambda p: _St())
    with pytest.raises(cli.CliError, match="junction"):
        cli.read_private_file(windows / "j" / "cfg", "config file")


# ── Review 17h SDK-19: the temp file holding the secret never outlives a failed rename
@pytest.mark.parametrize("win", [False, True])
def test_temp_removed_when_replace_fails(win: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    d = home / "d"
    d.mkdir(parents=True)
    if win:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(cli, "_POSIX", False)
        monkeypatch.setattr(cli, "_WINDOWS", True)
    target = d / "cfg"
    cli.write_private_file(target, "old\n")
    real_replace = os.replace
    seen: List[str] = []

    def boom(src: Any, dst: Any, **kw: Any) -> None:
        seen.append(str(src))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli.os, "replace", boom)
    with pytest.raises(OSError):
        cli.write_private_file(target, f"api_key={KEY}\n")
    monkeypatch.setattr(cli.os, "replace", real_replace)
    assert seen and ".cfg." in seen[0]
    assert sorted(p.name for p in d.iterdir()) == ["cfg"] and target.read_text() == "old\n"
    cli.write_private_file(target, "new\n")  # after a successful rename nothing is unlinked
    assert sorted(p.name for p in d.iterdir()) == ["cfg"] and target.read_text() == "new\n"


def test_temp_removed_when_write_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    monkeypatch.setattr(cli.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "I/O error")))
    with pytest.raises(OSError):
        cli.write_private_file(d / "cfg", f"api_key={KEY}\n")
    assert list(d.iterdir()) == []
