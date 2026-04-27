"""Exceptions for the ANIO API client."""

from __future__ import annotations


class AnioApiError(Exception):
    """Base exception for ANIO API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AnioAuthError(AnioApiError):
    """Authentication errors."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message, status_code=401)


class AnioOtpRequiredError(AnioAuthError):
    """OTP/2FA code required."""

    def __init__(self, message: str = "OTP code required") -> None:
        super().__init__(message)


class AnioRateLimitError(AnioApiError):
    """Rate limited (HTTP 429)."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class AnioConnectionError(AnioApiError):
    """Network/connection errors."""

    def __init__(self, message: str = "Connection failed") -> None:
        super().__init__(message)


class AnioDeviceNotFoundError(AnioApiError):
    """Device not found (HTTP 404)."""

    def __init__(self, device_id: str) -> None:
        super().__init__(f"Device not found: {device_id}", status_code=404)
        self.device_id = device_id


class AnioMessageTooLongError(AnioApiError):
    """Message exceeds maximum length."""

    def __init__(self, length: int, max_length: int) -> None:
        super().__init__(
            f"Message too long: {length} characters (max {max_length})",
            status_code=400,
        )
        self.length = length
        self.max_length = max_length
