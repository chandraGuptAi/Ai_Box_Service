"""Fetch errors carry the same codes as AI_python_backend's error classes, which maps them back."""
from typing import Any


class FetchError(Exception):
    def __init__(self, code: str, message: str, status: int, *, details: Any = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.details, self.retryable = code, message, status, details, retryable


def too_large(message: str, details: Any = None) -> FetchError:
    return FetchError("source_too_large", message, 422, details=details)


def unavailable(message: str, details: Any = None) -> FetchError:
    return FetchError("upstream_unavailable", message, 502, details=details, retryable=True)


def timed_out(message: str, details: Any = None) -> FetchError:
    return FetchError("upstream_timeout", message, 504, details=details, retryable=True)


def pagination(message: str, details: Any = None) -> FetchError:
    return FetchError("upstream_pagination_inconsistent", message, 502, details=details, retryable=True)


def changed(message: str, details: Any = None) -> FetchError:
    return FetchError("source_changed", message, 409, details=details, retryable=True)


def incomplete(message: str, details: Any = None) -> FetchError:
    return FetchError("incomplete_source", message, 502, details=details, retryable=True)


# A page failing with these is split in half and read again.
SPLIT_CODES = frozenset({"upstream_unavailable", "upstream_timeout"})
# The whole fetch is attempted again after these.
RETRY_CODES = frozenset({"source_changed", "upstream_pagination_inconsistent"})
