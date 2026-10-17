"""Pattern for an end-to-end sign-up test (pytest style).

The fixture creates one address per test and always deletes it. ``trigger_signup`` stands for your own code
that makes your application send the verification email.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import pytest

import tmailor


@pytest.fixture(scope="session")
def client() -> Iterator[tmailor.Client]:
    with tmailor.Client() as c:
        yield c


@pytest.fixture
def inbox(client: tmailor.Client) -> Iterator[tmailor.Mailbox]:
    box = client.mailboxes.create()
    try:
        yield box
    finally:
        with contextlib.suppress(tmailor.MailboxNotFoundError):
            box.delete()


def trigger_signup(email: str) -> None:  # replace with a call to your application
    raise NotImplementedError


def test_signup_sends_code(inbox: tmailor.Mailbox) -> None:
    trigger_signup(inbox.email or "")
    # since is exclusive (received_at > since): "- 1" keeps a message received in the creation second (Review 17d SDK-05).
    msg = inbox.wait_for_message(timeout=90, subject_contains="verify", since=(inbox.created_at or 0) - 1)
    code = msg.otp()
    assert code is not None, msg.text
