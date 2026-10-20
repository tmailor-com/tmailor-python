"""Best-effort helpers to pull a one-time code and links out of a message.

Since API 1.3.0 ``POST /v1/mailbox/message`` returns ``otp``, ``codes`` and ``links`` found by the server with the
same rules (``system/api_openapi/src/extract.php``, same test table). ``Message.otp()``, ``codes()`` and ``links()``
use those values; these helpers run locally only when a response has none (message list items, older servers) or
on any string you pass. ``extract_otp`` prefers to return ``None`` over guessing.
"""

from __future__ import annotations

import bisect
import contextlib
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

__all__ = ["extract_otp", "extract_codes", "extract_links", "html_to_text"]

_KEYWORDS = re.compile(
    r"(one[\s-]?time\s+(?:pass(?:word|code)?|code|pin)|verification|verify|confirmation|confirm|"
    r"security\s+code|access\s+code|login\s+code|sign[\s-]?in\s+code|auth(?:entication)?\s+code|"
    r"pass\s?code|\botp\b|\bpin\b|\bcode\b|\btoken\b|mã\s+(?:xác\s+(?:minh|nhận)|otp)|"
    r"código|codice|\bcodigo\b|\bkod\b|\bcode de\b)",
    re.IGNORECASE,
)
_URL = re.compile(r"""(?:https?://|www\.)[^\s<>"'()]+""", re.IGNORECASE)
# Review 20a: the lookbehind starts a match only at the beginning of a run of these characters; without it a long
# run with no "@" takes quadratic time (9 s for 50,000 characters).
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A candidate is 4-8 characters: all digits, or upper-case letters mixed with at least one digit.
# Digits split in two equal halves by one space or dash ("123 456", "1234-5678") are joined; "555-0199" is not.
_CANDIDATE = re.compile(
    r"(?<![\w#$€£.,:/])(?<!\d-)"
    r"(\d{3}[ -]\d{3}|\d{4}[ -]\d{4}|\d{4,8}|(?=[A-Z0-9]{4,8}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{4,8})"
    r"(?![\w%€$£/:]|[.,-]\d)"
)
_YEAR = re.compile(r"^(19|20)\d\d$")
_NEAR = 80
# Review 20a CORE-03: same limits as the server (src/extract.php): each source is cut to its first 200,000
# characters, at most 20 codes and 100 links.
_MAX_CHARS = 200_000
_MAX_CODES = 20
_MAX_LINKS = 100


class _HtmlScan(HTMLParser):
    """Review 17d SDK-09: real HTML tokenizer instead of a regex.

    ``hrefs``: the value of the attribute named exactly ``href`` (names are lower-cased by the parser) of each
    ``<a>`` tag, entities decoded. ``data-href``, ``onclick`` or ``title`` values that merely contain the text
    ``href=...`` are never taken. ``text``: visible text (no attribute values, no script/style/title content).
    """

    # Not "head": a missing </head> would hide the whole body. <title> is the only head text we drop.
    _SKIP = frozenset({"script", "style", "title", "template", "noscript"})
    _BREAK = frozenset({"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        else:
            self.parts.append(" ")
        if tag == "a":
            for name, value in attrs:
                if name == "href":
                    if value is not None:
                        self.hrefs.append(value)
                    break  # the first href wins, as in browsers

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "a":
            self.handle_starttag(tag, attrs)
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")
        else:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _scan(html: str) -> _HtmlScan:
    p = _HtmlScan()
    html = html[:_MAX_CHARS]
    # Malformed HTML: keep whatever was parsed so far (older Python versions may raise on odd markup).
    with contextlib.suppress(ValueError, AssertionError):
        p.feed(html)
        p.close()
    return p


def _http_url(url: str) -> bool:
    """Only absolute http(s) URLs with a host (rejects javascript:, data:, relative and scheme-less links)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def html_to_text(html: str) -> str:
    """Very small HTML to text conversion (enough for code and link extraction). Attribute values are dropped."""
    return "".join(_scan(html).parts)


def _mask(text: str) -> str:
    # Hide URLs and e-mail addresses (full of digits and words like "verify") without shifting positions.
    masked = _URL.sub(lambda m: " " * len(m.group(0)), text)
    return _EMAIL.sub(lambda m: " " * len(m.group(0)), masked)


def _candidates(masked: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for m in _CANDIDATE.finditer(masked):
        raw = m.group(1)
        code = raw.replace(" ", "").replace("-", "")
        if not 4 <= len(code) <= 8:
            continue
        out.append((m.start(1), code))
    return out


def extract_otp(subject: Optional[str], text: Optional[str], html: Optional[str] = None) -> Optional[str]:
    """Return the most likely one-time code, or None when there is no confident answer.

    Rules: a candidate is 4-8 digits (``"123456"``, ``"123 456"``) or 4-8 upper-case letters mixed with
    digits (``"A7K29Q"``). Candidates close to a keyword (code, OTP, verification, PIN, ...) win, the closest
    one first, a candidate right after the keyword before one in front of it. Without any keyword nearby a
    code is returned only when the whole message has exactly one distinct 6-digit number.

    Since API 1.3.0 the server runs the same rules (``otp`` of ``POST /v1/mailbox/message``); ``Message.otp()``
    uses the server value and calls this function only when the response has none.
    """
    return _scan_codes(subject, text, html)[0]


def extract_codes(subject: Optional[str], text: Optional[str], html: Optional[str] = None) -> List[str]:
    """Every distinct code-like value (the candidates of :func:`extract_otp`), subject first, at most 20."""
    return _scan_codes(subject, text, html)[1]



def _scan_codes(subject: Optional[str], text: Optional[str], html: Optional[str]) -> Tuple[Optional[str], List[str]]:
    body = text if text else (html_to_text(html) if html else "")
    sources = [s for s in (subject or "", body) if s]
    best: Optional[Tuple[int, int, str]] = None
    lone: List[str] = []
    codes: List[str] = []
    for raw_src in sources:
        src = _mask(raw_src[:_MAX_CHARS])
        cands = _candidates(src)
        if not cands:
            continue
        kws = [(m.start(), m.end()) for m in _KEYWORDS.finditer(src)]
        # Review 20a CORE-01: no candidate x keyword loop (sender-controlled text, O(C*K)). Keywords do not overlap and
        # come in order, so starts and ends both increase: a candidate only needs the closest keyword ending before it
        # (rank 0, always better than rank 1) or the closest one starting after it (rank 1). Binary search: O(C log K).
        k_start = [k[0] for k in kws]
        k_end = [k[1] for k in kws]
        for pos, code in cands:
            is_digits = code.isdigit()
            if is_digits and len(code) == 4 and _YEAR.match(code) and not kws:
                continue
            if code not in codes and len(codes) < _MAX_CODES:
                codes.append(code)
            if is_digits:
                lone.append(code)
            if not kws:
                continue
            # Prefer digits over letter+digit mixes at equal distance.
            penalty = 0 if is_digits else 5
            score: Optional[Tuple[int, int, str]] = None
            i = bisect.bisect_right(k_end, pos) - 1  # last keyword with end <= pos
            if i >= 0 and pos - k_end[i] <= _NEAR:
                score = (0, pos - k_end[i] + penalty, code)
            else:
                end = pos + len(code)
                j = bisect.bisect_left(k_start, end)  # first keyword with start >= end
                if j < len(k_start) and k_start[j] - end <= _NEAR:
                    score = (1, k_start[j] - end + penalty, code)
            if score is not None and (best is None or score < best):
                best = score
    if best is not None:
        return best[2], codes
    six = {c for c in lone if len(c) == 6}
    if len(six) == 1 and len(set(lone)) == 1:
        return six.pop(), codes
    return None, codes


def extract_links(text: Optional[str], html: Optional[str] = None) -> List[str]:
    """http(s) links from the HTML ``href`` attributes first, then from the plain text, without duplicates."""
    seen: List[str] = []
    page_text = ""
    if html:
        scan = _scan(html)  # Review 17d SDK-09: parsed attributes, not a regex over the markup
        for raw in scan.hrefs:
            url = raw.strip()
            if _http_url(url) and url not in seen:
                seen.append(url)
                if len(seen) >= _MAX_LINKS:
                    return seen
        page_text = "".join(scan.parts)
    for src in (text or "", page_text):
        for m in _URL.finditer(src[:_MAX_CHARS]):
            url = m.group(0).rstrip(".,;:!?]")
            # Review 20a CORE-04: still an absolute http(s) URL with a host after the trailing punctuation is cut.
            if url.lower().startswith("www.") or not _http_url(url):
                continue
            if url not in seen:
                seen.append(url)
                if len(seen) >= _MAX_LINKS:
                    return seen
    return seen
