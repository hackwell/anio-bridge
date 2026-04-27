"""Authentication for the ANIO API (standalone)."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp

from .const import API_URL, CLIENT_ID, TOKEN_REFRESH_BUFFER
from .exceptions import AnioAuthError, AnioConnectionError, AnioOtpRequiredError
from .models import AuthTokens

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiohttp import ClientSession

    TokenRefreshCallback = Callable[[str, str], Coroutine[None, None, None]]

_LOGGER = logging.getLogger(__name__)


class AnioAuth:
    """Handle authentication with the ANIO API."""

    def __init__(
        self,
        session: ClientSession,
        email: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        app_uuid: str | None = None,
        on_token_refresh: TokenRefreshCallback | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._app_uuid = app_uuid or str(uuid.uuid4())
        self._token_expiry: datetime | None = None
        self._on_token_refresh = on_token_refresh

        if access_token:
            self._token_expiry = self._parse_jwt_expiry(access_token)

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def app_uuid(self) -> str:
        return self._app_uuid

    @property
    def is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expiry:
            return False
        buffer = timedelta(seconds=TOKEN_REFRESH_BUFFER)
        return datetime.now(UTC) < (self._token_expiry - buffer)

    def _parse_jwt_expiry(self, token: str) -> datetime | None:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload = parts[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            decoded = base64.urlsafe_b64decode(payload)
            data = json.loads(decoded)
            if "exp" in data:
                return datetime.fromtimestamp(data["exp"], tz=UTC)
        except (ValueError, KeyError, json.JSONDecodeError) as err:
            _LOGGER.debug("Failed to parse JWT expiry: %s", err)
        return None

    async def login(self, otp_code: str | None = None) -> AuthTokens:
        if not self._email or not self._password:
            raise AnioAuthError("Email and password are required for login")

        headers = {
            "client-id": CLIENT_ID,
            "app-uuid": self._app_uuid,
            "Content-Type": "application/json",
        }
        payload: dict[str, str] = {
            "email": self._email,
            "password": self._password,
        }
        if otp_code:
            payload["otpCode"] = otp_code

        try:
            async with self._session.post(
                f"{API_URL}/v1/auth/login",
                headers=headers,
                json=payload,
            ) as response:
                if response.status == 401:
                    raise AnioAuthError("Invalid email or password")
                if response.status != 200:
                    text = await response.text()
                    raise AnioAuthError(f"Login failed: {text}")

                data = await response.json()
                tokens = AuthTokens.model_validate(data)

                if tokens.is_otp_required and not otp_code:
                    raise AnioOtpRequiredError()

                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                self._token_expiry = self._parse_jwt_expiry(tokens.access_token)

                _LOGGER.info("Login successful, token expires at %s", self._token_expiry)

                if self._on_token_refresh:
                    await self._on_token_refresh(
                        self._access_token,
                        self._refresh_token,
                    )
                return tokens
        except aiohttp.ClientError as err:
            raise AnioConnectionError(f"Connection failed: {err}") from err

    async def refresh(self) -> str:
        if not self._refresh_token:
            raise AnioAuthError("No refresh token available")

        headers = {
            "Authorization": f"Bearer {self._refresh_token}",
            "client-id": CLIENT_ID,
            "app-uuid": self._app_uuid,
        }
        try:
            async with self._session.post(
                f"{API_URL}/v1/auth/refresh-access-token",
                headers=headers,
            ) as response:
                if response.status == 401:
                    raise AnioAuthError("Refresh token expired")
                if response.status != 200:
                    text = await response.text()
                    raise AnioAuthError(f"Token refresh failed: {text}")

                data = await response.json()
                self._access_token = data.get("accessToken")
                self._token_expiry = self._parse_jwt_expiry(self._access_token or "")
                new_refresh = data.get("refreshToken")
                if new_refresh:
                    self._refresh_token = new_refresh

                _LOGGER.debug("Token refreshed, new expiry at %s", self._token_expiry)

                if self._on_token_refresh:
                    await self._on_token_refresh(
                        self._access_token or "",
                        self._refresh_token or "",
                    )
                return self._access_token or ""
        except aiohttp.ClientError as err:
            raise AnioConnectionError(f"Connection failed: {err}") from err

    async def ensure_valid_token(self) -> str:
        if self.is_token_valid and self._access_token:
            return self._access_token

        if self._refresh_token:
            try:
                _LOGGER.debug("Token expired or expiring soon, refreshing")
                return await self.refresh()
            except AnioAuthError as err:
                _LOGGER.warning("Refresh failed (%s), falling back to login", err)

        if self._email and self._password:
            tokens = await self.login()
            return tokens.access_token

        raise AnioAuthError("No valid token and no credentials to obtain one")

    async def logout(self) -> None:
        if not self._access_token:
            return
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with self._session.post(
                f"{API_URL}/v1/auth/logout",
                headers=headers,
            ) as response:
                if response.status == 200:
                    _LOGGER.debug("Logout successful")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Logout failed: %s", err)
        finally:
            self._access_token = None
            self._refresh_token = None
            self._token_expiry = None
