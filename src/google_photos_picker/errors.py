"""Exceptions for the Google Photos Picker client."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class PickerError(Exception):
    """Base class for all errors raised by this library."""


class AuthError(PickerError):
    """OAuth token acquisition/refresh failed."""


class PickerTimeout(PickerError):
    """The user did not finish picking before the session's polling timeout."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} timed out before the user finished picking")
        self.session_id = session_id


class VideoNotReady(PickerError):
    """A video is still processing (or failed) and cannot be downloaded yet."""

    def __init__(self, item_id: str, status: Optional[str]) -> None:
        super().__init__(f"video {item_id} is not ready for download (status={status})")
        self.item_id = item_id
        self.status = status


class APIError(PickerError):
    """The API returned a non-2xx response."""

    def __init__(self, message: str, *, http_status: int, status: Optional[str] = None,
                 details: Optional[List[Dict[str, Any]]] = None, response_body: Any = None
                 ) -> None:
        super().__init__(f"{http_status} {status or ''}: {message}".replace("  ", " "))
        self.message = message
        self.http_status = http_status
        self.status = status
        self.details = details or []
        self.response_body = response_body


class InvalidArgument(APIError):
    pass


class Unauthenticated(APIError):
    pass


class PermissionDenied(APIError):
    pass


class NotFound(APIError):
    """Unknown or already-deleted session."""


class FailedPrecondition(APIError):
    """No Google Photos account, or media items requested before picking finished."""


class RateLimited(APIError):
    """Too many requests or too many open sessions – delete sessions you no longer need."""


class ServerError(APIError):
    pass


_BY_STATUS = {
    "INVALID_ARGUMENT": InvalidArgument,
    "UNAUTHENTICATED": Unauthenticated,
    "PERMISSION_DENIED": PermissionDenied,
    "NOT_FOUND": NotFound,
    "FAILED_PRECONDITION": FailedPrecondition,
    "RESOURCE_EXHAUSTED": RateLimited,
}
_BY_HTTP = {400: InvalidArgument, 401: Unauthenticated, 403: PermissionDenied, 404: NotFound,
            429: RateLimited}


def from_response(http_status: int, body: Any) -> APIError:
    status = message = None
    details: List[Dict[str, Any]] = []
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        status, message, details = err.get("status"), err.get("message"), err.get("details") or []
    if message is None:
        message = body if isinstance(body, str) and body else "request failed"
    cls = _BY_STATUS.get(status or "") or _BY_HTTP.get(http_status)
    if cls is None:
        cls = ServerError if http_status >= 500 else APIError
    return cls(str(message), http_status=http_status, status=status, details=details,
               response_body=body)
