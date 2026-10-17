"""Create an address, wait for an email, print the code, delete the address.

export TMAILOR_API_KEY=tmk_live_...
python examples/quickstart.py
"""

from __future__ import annotations

import tmailor


def main() -> None:
    with tmailor.Client() as client:
        me = client.me()
        print(f"Quota left: {me.limits.quota_remaining}, rate: {me.limits.rate_per_min}/min")

        box = client.mailboxes.create()
        print(f"Send an email to {box.email} (waiting up to 2 minutes)...")
        try:
            msg = box.wait_for_message(timeout=120, since=(box.created_at or 0) - 1)  # since is exclusive (Review 17d SDK-05)
            print(f"From:    {msg.from_.name} <{msg.from_.address}>")
            print(f"Subject: {msg.subject}")
            print(f"Code:    {msg.otp()}")
            for link in msg.links()[:5]:
                print(f"Link:    {link}")
        except tmailor.WaitTimeoutError:
            print("No email arrived.")
        finally:
            box.delete()


if __name__ == "__main__":
    main()
