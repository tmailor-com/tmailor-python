"""Live tests against the real API. Opt-in: ``pytest -m live``.

Needs:
  TMAILOR_API_KEY            an API key whose allowlist contains this machine's public IP
  TMAILOR_BASE_URL           optional, default https://open-api.tmailor.com
  TMAILOR_LIVE_SEND_CMD      optional command that delivers ONE email to an address; ``{to}`` is replaced by the
                             address (for example a script that talks to your SMTP relay). Without it the
                             receive / OTP part is skipped.
  TMAILOR_LIVE_EXPECT_OTP    optional code that the sent email contains (checked with Message.otp()).

Each test creates its own addresses and deletes them at the end (deleting does not return quota).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time

import pytest

import tmailor

pytestmark = pytest.mark.live

if not os.environ.get("TMAILOR_API_KEY"):
    pytest.skip("TMAILOR_API_KEY is not set", allow_module_level=True)


def _send(to: str) -> None:
    cmd = os.environ["TMAILOR_LIVE_SEND_CMD"].replace("{to}", shlex.quote(to))
    subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, timeout=60)  # noqa: S602


def test_live_full_flow() -> None:
    with tmailor.Client(max_retries=3) as client:
        me = client.me()
        assert me.user_id > 0
        assert client.last_rate_limit is not None and client.last_rate_limit.limit
        doms = client.domains()
        assert isinstance(doms.shared_domains_available, bool)
        assert client.health().status == "ok"
        assert client.usage().timezone

        box = client.mailboxes.create()
        try:
            assert box.email and box.token and box.web is not None
            assert box.token not in repr(box)
            same = client.mailbox(box.token).info()
            assert same.email == box.email and same.message_count == 0

            listed = [m for m in client.mailboxes.list(limit=200) if m.email == box.email]
            assert listed and listed[0].token == box.token

            assert list(box.messages()) == []

            if os.environ.get("TMAILOR_LIVE_SEND_CMD"):
                # Pattern 1: the email is triggered BEFORE waiting -> pass since (server time of the address creation).
                t0 = time.monotonic()
                _send(box.email)
                msg = box.wait_for_message(timeout=120, since=(box.created_at or 0) - 1)
                print(f"\n[live] message 1 arrived after {time.monotonic() - t0:.1f}s")
                assert msg.body_available and msg.has_body
                assert msg.to == box.email
                expect = os.environ.get("TMAILOR_LIVE_EXPECT_OTP")
                if expect:
                    assert msg.otp() == expect
                # Reading a message marks it as read (wait_for_message reads it too).
                assert msg.seen is True
                assert box.message(msg.id).seen is True
                assert msg.id not in [m.id for m in box.messages(unread=True)]
                if msg.subject:
                    assert [m.id for m in box.messages(subject=msg.subject)] == [msg.id]

                # Pattern 2: default wait (only messages that were not there when the wait started), mail sent meanwhile.
                t1 = time.monotonic()
                sender = threading.Timer(2.0, _send, [box.email])
                sender.start()
                second = box.wait_for_message(timeout=120)
                sender.join()
                print(f"[live] message 2 arrived {time.monotonic() - t1 - 2:.1f}s after sending")
                assert second.id != msg.id
                box.delete_message(second)
                with pytest.raises(tmailor.MessageNotFoundError):
                    box.message(second.id)
                assert box.clear() == 1
                assert list(box.messages()) == []

            assert box.info().email == same.email
        finally:
            box.delete()
        with pytest.raises(tmailor.MailboxNotFoundError):
            box.info()
        with pytest.raises(tmailor.MailboxNotFoundError):
            box.delete()


def test_live_errors() -> None:
    with tmailor.Client() as client:
        with pytest.raises(tmailor.MailboxNotFoundError):
            client.mailbox("not-a-real-token").info()
        with pytest.raises(tmailor.InvalidDomainError) as ei:
            client.mailboxes.create(domain="example.com")
        assert ei.value.status == 400 and ei.value.request_id
    with tmailor.Client(api_key="tmk_live_aaaaaaaaaaaa_" + "b" * 43) as bad, pytest.raises(tmailor.InvalidKeyError):
        bad.me()
    # Review 17d SDK-07: health is public; a client without a key works for it and refuses the rest locally.
    with tmailor.Client(api_key="") as anon:
        assert anon.health().status == "ok"
        with pytest.raises(tmailor.MissingKeyError) as ei:
            anon.me()
        assert ei.value.status is None


async def test_live_async_create_delete() -> None:
    async with tmailor.AsyncClient() as client:
        box = await client.mailboxes.create(idempotency_key=f"sdk-live-{time.time_ns()}")
        try:
            info = await client.mailbox(box.token or "").info()
            assert info.email == box.email
            assert [m async for m in box.messages()] == []
        finally:
            await box.delete()
