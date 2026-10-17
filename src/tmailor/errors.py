"""Typed errors raised by the tmailor SDK.

Every error the API returns has the JSON shape
``{"error": {"code", "message", "description", "request_id", ...}}``. The SDK maps ``code`` to a
subclass of :class:`TmailorError`. Branch on the class (or on ``err.code``), never on the message text.

Network problems (DNS, TLS, connection reset, read timeout) raise :class:`APIConnectionError`, which
has no HTTP status. :class:`WaitTimeoutError` is raised by ``Mailbox.wait_for_message`` when no
matching message arrives in time.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Type

__all__ = [
    "TmailorError",
    "APIError",
    "InvalidResponseError",
    "APIConnectionError",
    "APITimeoutError",
    "WaitTimeoutError",
    "BadRequestError",
    "InvalidJsonError",
    "InvalidFieldError",
    "InvalidDomainError",
    "CustomNameNotAllowedError",
    "InvalidNameError",
    "AuthenticationError",
    "MissingKeyError",
    "InvalidKeyError",
    "KeyRevokedError",
    "QuotaExceededError",
    "PermissionDeniedError",
    "AccountLockedError",
    "IpNotAllowedError",
    "NotFoundError",
    "MailboxNotFoundError",
    "MessageNotFoundError",
    "MethodNotAllowedError",
    "ConflictError",
    "MailboxExistsError",
    "RateLimitError",
    "RateLimitedError",
    "DailyCapReachedError",
    "InternalServerError",
    "ServiceUnavailableError",
    "NoDomainAvailableError",
    "IdempotencyUnavailableError",
    "CacheUnavailableError",
]


class TmailorError(Exception):
    """Base class of every error raised by the SDK.

    Attributes:
        code: Error code from the API (for example ``"QuotaExceeded"``), or an SDK code such as
            ``"ConnectionError"`` / ``"WaitTimeout"`` for errors that did not come from the API.
        status: HTTP status code, or ``None`` when no HTTP response was received.
        message: Short human readable message.
        description: Longer explanation from the API (may be empty).
        request_id: ``request_id`` of the failed request. Quote it when you contact support.
        retry_after: Seconds to wait before retrying (429 errors), or ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "Error",
        status: Optional[int] = None,
        description: str = "",
        request_id: Optional[str] = None,
        retry_after: Optional[int] = None,
        body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.description = description
        self.request_id = request_id
        self.retry_after = retry_after
        # Only the "error" object of the response is kept. It never contains an API key or an address token.
        self.body: Dict[str, Any] = dict(body or {})
        self.headers: Dict[str, str] = dict(headers or {})

    def __str__(self) -> str:
        parts = [f"[{self.code}]"]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        parts.append(self.message)
        if self.request_id:
            parts.append(f"(request_id={self.request_id})")
        return " ".join(parts)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, status={self.status!r}, "
            f"message={self.message!r}, request_id={self.request_id!r})"
        )


class APIError(TmailorError):
    """An error response (HTTP status >= 400) from the API."""


class InvalidResponseError(APIError):
    """The API answered 2xx with a body the SDK cannot trust: not JSON, missing or mistyped fields, or a pagination
    cursor that was already used (Review 17d SDK-02/04/06). ``code`` is ``"InvalidResponse"``; ``status`` is the HTTP
    status when known. The response body is never kept in the error (it can carry address tokens).
    """

    def __init__(self, message: str, *, status: Optional[int] = None, request_id: Optional[str] = None) -> None:
        super().__init__(message, code="InvalidResponse", status=status, request_id=request_id)


class APIConnectionError(TmailorError):
    """The request did not get an HTTP response (DNS, TLS, connection reset, ...)."""

    def __init__(self, message: str = "Connection error", *, code: str = "ConnectionError") -> None:
        super().__init__(message, code=code)


class APITimeoutError(APIConnectionError):
    """The HTTP request timed out."""

    def __init__(self, message: str = "Request timed out") -> None:
        super().__init__(message, code="Timeout")


class WaitTimeoutError(TmailorError, TimeoutError):
    """``wait_for_message`` did not see a matching new message before its timeout."""

    def __init__(self, message: str = "No matching message arrived before the timeout") -> None:
        super().__init__(message, code="WaitTimeout")


# ── 400
class BadRequestError(APIError):
    """HTTP 400: the request is invalid."""


class InvalidJsonError(BadRequestError):
    pass


class InvalidFieldError(BadRequestError):
    pass


class InvalidDomainError(BadRequestError):
    """``domain`` is not one of your usable domains (400 on create)."""


class CustomNameNotAllowedError(BadRequestError):
    pass


class InvalidNameError(BadRequestError):
    """The custom name breaks the naming rules. ``reason`` holds the detail when the API sends it."""

    @property
    def reason(self) -> Optional[str]:
        r = self.body.get("reason")
        return r if isinstance(r, str) else None


# ── 401
class AuthenticationError(APIError):
    """HTTP 401: missing, invalid or revoked API key."""


class MissingKeyError(AuthenticationError):
    """HTTP 401 ``MissingKey``, or raised by the SDK (``status`` None) when an operation that needs a key is called
    without one (Review 17d SDK-07: ``health()`` works without a key)."""


class InvalidKeyError(AuthenticationError):
    pass


class KeyRevokedError(AuthenticationError):
    pass


# ── 402
class QuotaExceededError(APIError):
    """HTTP 402: the mailbox quota of all your plans is used up for this period."""


# ── 403
class PermissionDeniedError(APIError):
    """HTTP 403."""


class AccountLockedError(PermissionDeniedError):
    pass


class IpNotAllowedError(PermissionDeniedError):
    """The request IP is not in the allowlist of the account (set it on https://platform.tmailor.com)."""


# ── 404
class NotFoundError(APIError):
    """HTTP 404."""


class MailboxNotFoundError(NotFoundError):
    """The token is wrong, was rotated on https://platform.tmailor.com, belongs to another account, or the address
    was deleted."""


class MessageNotFoundError(NotFoundError):
    pass


# ── 405 / 409
class MethodNotAllowedError(APIError):
    pass


class ConflictError(APIError):
    """HTTP 409 (for example an Idempotency-Key reused with a different body, or still in progress)."""


class MailboxExistsError(ConflictError):
    """409 on create: the address is already in use. Also raised for a ``name`` on your own domain that was
    created on the tmailor.com website or mobile app: such an address cannot be used through the API, pick
    another name."""


# ── 429
class RateLimitError(APIError):
    """HTTP 429. ``retry_after`` tells how many seconds to wait."""


class RateLimitedError(RateLimitError):
    pass


class DailyCapReachedError(RateLimitError):
    pass


# ── 5xx
class InternalServerError(APIError):
    """HTTP 500 or another 5xx without a more specific class."""


class ServiceUnavailableError(APIError):
    """HTTP 503: temporarily unavailable."""


class NoDomainAvailableError(ServiceUnavailableError):
    pass


class IdempotencyUnavailableError(ServiceUnavailableError):
    """The Idempotency-Key could not be checked, so nothing was created. Retry later with the same key."""


class CacheUnavailableError(ServiceUnavailableError):
    """Address delete could not be confirmed.

    ``secret_changed`` is ``False`` when nothing changed (retry later) and ``True`` when the address was deleted
    but old tokens may keep working on tmailor.com for up to 10 minutes.
    """

    @property
    def secret_changed(self) -> Optional[bool]:
        v = self.body.get("secret_changed")
        return v if isinstance(v, bool) else None


ERROR_CODES: Dict[str, Type[APIError]] = {
    "InvalidJson": InvalidJsonError,
    "InvalidField": InvalidFieldError,
    "InvalidDomain": InvalidDomainError,
    "CustomNameNotAllowed": CustomNameNotAllowedError,
    "InvalidName": InvalidNameError,
    "MissingKey": MissingKeyError,
    "InvalidKey": InvalidKeyError,
    "KeyRevoked": KeyRevokedError,
    "QuotaExceeded": QuotaExceededError,
    "AccountLocked": AccountLockedError,
    "IpNotAllowed": IpNotAllowedError,
    "NotFound": NotFoundError,
    "MailboxNotFound": MailboxNotFoundError,
    "MessageNotFound": MessageNotFoundError,
    "MethodNotAllowed": MethodNotAllowedError,
    "MailboxExists": MailboxExistsError,
    "Conflict": ConflictError,
    "RateLimited": RateLimitedError,
    "DailyCapReached": DailyCapReachedError,
    "InternalError": InternalServerError,
    "NoDomainAvailable": NoDomainAvailableError,
    "IdempotencyUnavailable": IdempotencyUnavailableError,
    "CacheUnavailable": CacheUnavailableError,
}

STATUS_CLASSES: Dict[int, Type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: QuotaExceededError,
    403: PermissionDeniedError,
    404: NotFoundError,
    405: MethodNotAllowedError,
    409: ConflictError,
    413: BadRequestError,
    429: RateLimitError,
    500: InternalServerError,
    503: ServiceUnavailableError,
}


def error_class(code: Optional[str], status: int) -> Type[APIError]:
    """Pick the error class: known ``code`` first, then the HTTP status, then a generic class."""
    if code and code in ERROR_CODES:
        return ERROR_CODES[code]
    if status in STATUS_CLASSES:
        return STATUS_CLASSES[status]
    if status >= 500:
        return InternalServerError
    return APIError
