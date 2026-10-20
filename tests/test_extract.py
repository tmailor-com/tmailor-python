from __future__ import annotations

from typing import Optional

import pytest

from tmailor import Message, extract_links, extract_otp


@pytest.mark.parametrize(
    ("subject", "text", "expected"),
    [
        ("Confirm your email", "Your verification code is 482913. It expires in 10 minutes.", "482913"),
        ("Your OTP", "OTP: 7731", "7731"),
        ("123456 is your Instagram code", "", "123456"),
        ("Sign in", "Use code 123 456 to sign in.", "123456"),
        ("Sign in", "Use code 123-456 to sign in.", "123456"),
        ("Login", "Your one-time passcode: A7K29Q", "A7K29Q"),
        ("Security", "Enter this security code: 90210837", "90210837"),
        ("Verify", "Order #55512 shipped. Your verification code is 661204.", "661204"),
        # Year and amounts are not codes; the keyword picks the right number.
        ("Hello", "In 2026 we sent $1500 to you. Code: 3301", "3301"),
        # Closest to the keyword wins, after the keyword before in front of it.
        ("x", "Ticket 4410 created. Your PIN is 0042.", "0042"),
        ("Mã xác minh", "Mã xác minh của bạn là 551203", "551203"),
        ("Código", "Tu código es 824466", "824466"),
        ("G-123456 is your Google verification code", "", "123456"),
        ("Reminder", "Due 2026-10-30. Your code: 4455", "4455"),
        ("Code", "Your code is 1234 5678", "12345678"),
    ],
)
def test_otp_found(subject: str, text: str, expected: str) -> None:
    assert extract_otp(subject, text) == expected


@pytest.mark.parametrize(
    ("subject", "text"),
    [
        ("Weekly news", "Hello, nothing to see here."),
        ("Invoice", "Invoice 1234 total 5678 due 2026-10-30"),  # several numbers, no keyword
        ("Welcome", "Call us at 555-0199 or visit https://example.com/verify?code=123456"),  # digits only in URL
        ("Meeting", "See you at 10:30 on 12/10."),
        ("Report", "Year 2026 summary"),
        ("Color", "Use #123456 as the brand color"),
        ("Stats", "We grew 4500% this year"),
    ],
)
def test_otp_none(subject: str, text: str) -> None:
    assert extract_otp(subject, text) is None


def test_otp_lone_six_digits_without_keyword() -> None:
    assert extract_otp("Welcome", "Here it is: 918273") == "918273"


def test_otp_from_html_only() -> None:
    html = "<html><style>.a{color:#112233}</style><p>Your code is <b>246810</b></p><script>var x=999999</script></html>"
    assert extract_otp("Verify", None, html) == "246810"


def test_otp_ignores_email_addresses() -> None:
    assert extract_otp("Hi", "Write to user123456@example.com, your code is 7788") == "7788"


def test_links_html_and_text() -> None:
    html = (
        '<p><a href="https://example.com/verify?t=1&amp;u=2">Verify</a> '
        '<a href=\'http://example.org/x\'>x</a> <a href="">blank</a> <a href="javascript:alert(1)">bad</a></p>'
    )
    text = "Open https://example.com/verify?t=1&u=2 or https://other.example/path."
    assert extract_links(text, html) == [
        "https://example.com/verify?t=1&u=2",
        "http://example.org/x",
        "https://other.example/path",
    ]


def test_links_none() -> None:
    assert extract_links(None, None) == []


def _msg(text: Optional[str], html: Optional[str] = None, subject: str = "s") -> Message:
    return Message.from_api(
        {
            "id": "a" * 32,
            "from": {"name": "n", "address": "a@b.c"},
            "to": "x@y.z",
            "subject": subject,
            "received_at": 1,
            "expires_at": 2,
            "seen": True,  # a message read with its body is always seen (REST 1.2.0, Review 17q SDK-20)
            "text": text,
            "html": html,
            "body_available": True,
        },
        with_body=True,
    )


def test_message_helpers() -> None:
    m = _msg("Your code is 135790. Visit https://example.com/a")
    assert m.otp() == "135790"
    assert m.links() == ["https://example.com/a"]
    assert m.has_body
    summary = Message.from_api(
        {
            "id": "b" * 32,
            "from": {"name": "", "address": ""},
            "to": "x@y.z",
            "subject": "Your code is 5566",
            "received_at": 1,
            "expires_at": 2,
            "seen": True,
        }
    )
    assert not summary.has_body
    assert summary.otp() == "5566"
    assert summary.from_.address == ""
    # Body never in repr.
    assert "135790" not in repr(m)


# ── Review 17d SDK-09: links() reads the real href attribute only
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # data-href / data-x-href are other attributes, not href.
        ('<a data-href="https://evil.example/a" href="https://good.example/ok">x</a>', ["https://good.example/ok"]),
        ('<a data-href="https://evil.example/a">x</a>', []),
        # "href=" inside another attribute value is text, not an attribute.
        ('<a title=\'href="https://evil.example/t"\' href="https://good.example/t">x</a>', ["https://good.example/t"]),
        ('<a onclick="location.href=\'https://evil.example/o\'" href="https://good.example/o">x</a>', ["https://good.example/o"]),
        ("<a title='href=\"https://evil.example/only\"'>x</a>", []),
        # Attribute names are case-insensitive; entities are decoded (also in the scheme).
        ('<A HREF="https://example.com/?a=1&amp;b=2">x</A>', ["https://example.com/?a=1&b=2"]),
        ('<a href="&#104;ttps://example.com/e">x</a>', ["https://example.com/e"]),
        ('<a href="jav&#x61;script:alert(1)">x</a>', []),
        ('<a href="data:text/html,hi">x</a> <a href="/relative">r</a> <a href="https://">empty host</a>', []),
        # Only <a>: href on other tags is ignored; an <a> without href yields nothing.
        ('<link href="https://evil.example/css"><div href="https://evil.example/div">d</div><a name="x">n</a>', []),
        # Broken HTML: unclosed quote / tag, stray "<", nothing raised.
        ('<a href="https://good.example/1">ok</a><a href="https://broken', ["https://good.example/1"]),
        ("<p>1 < 2 <a href=https://good.example/u>u</a>", ["https://good.example/u"]),
        ("<<<a href=>><//a>", []),
        # Attribute values never leak into the text pass.
        (
            '<img alt="https://evil.example/alt"><a title="see https://evil.example/title" href="https://good.example/z">z</a>',
            ["https://good.example/z"],
        ),
        # Script / style content is not text.
        ('<script>var u="https://evil.example/js"</script><style>a{background:url(https://evil.example/c)}</style>', []),
    ],
)
def test_links_parser_rejects_lookalikes(html: str, expected: list) -> None:
    assert extract_links(None, html) == expected


def test_links_visible_text_url_kept() -> None:
    html = "<p>Open https://good.example/text then <a href='https://good.example/a'>this</a></p>"
    assert extract_links(None, html) == ["https://good.example/a", "https://good.example/text"]


# ── Review 20a: same table in system/api_openapi/tests/extract_test.php (server extractor)
def test_otp_many_candidates_and_keywords_is_fast() -> None:
    import time

    body = "code 1234 " * 20000  # 200,000 characters: 20,000 candidates and 20,000 keywords
    t0 = time.monotonic()
    assert extract_otp("x", body) == "1234"
    assert time.monotonic() - t0 < 2.0


@pytest.mark.parametrize(
    ("subject", "text", "expected"),
    [
        ("Code", "Your code is ٤٥٦٧٨٩", "٤٥٦٧٨٩"),  # Arabic-Indic
        ("Hi", "x" * 200_001 + " code 4455", None),  # past the 200,000-character limit
        ("Hi", "x" * 150_000 + " code 4455", "4455"),  # long word run with no "@": linear e-mail mask
    ],
)
def test_otp_unicode_and_limits(subject: str, text: str, expected: Optional[str]) -> None:
    assert extract_otp(subject, text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Open https://] or https://?x or https://. now", []),
        ("Go to https://good.example/a?b=1. Thanks", ["https://good.example/a?b=1"]),
        ("See https://a.example/x\u00a0more", ["https://a.example/x"]),  # NBSP ends a URL
    ],
)
def test_links_text_urls_need_a_host(text: str, expected: list) -> None:
    assert extract_links(text, None) == expected


def test_links_capped_at_100() -> None:
    text = " ".join(f"https://h{i}.example/" for i in range(150))
    links = extract_links(text, None)
    assert len(links) == 100 and links[0] == "https://h0.example/" and links[-1] == "https://h99.example/"


def test_codes_capped_and_ordered() -> None:
    from tmailor import extract_codes

    assert extract_codes("x", "Ticket 4410 created. Your PIN is 0042.") == ["4410", "0042"]
    assert len(extract_codes("x", " ".join(str(100000 + i) for i in range(30)))) == 20


def test_server_values_win_and_cli_keeps_subject_codes() -> None:
    from tmailor.cli import message_dict

    m = Message.from_api(
        {
            "id": "c" * 32,
            "from": {"name": "n", "address": "a@b.c"},
            "to": "x@y.z",
            "subject": "Your code is 5566",
            "received_at": 1,
            "expires_at": 2,
            "seen": True,
            "text": None,
            "html": None,
            "body_available": False,
            "otp": "5566",
            "codes": ["5566"],
            "links": [],
        },
        with_body=True,
    )
    assert (m.otp(), m.codes(), m.links()) == ("5566", ["5566"], [])
    d = message_dict(m)  # Review 20a CORE-05: body expired, the subject code stays in the JSON output
    assert (d["otp"], d["codes"], d["links"]) == ("5566", ["5566"], [])
