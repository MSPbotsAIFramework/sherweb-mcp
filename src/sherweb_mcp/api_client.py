# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the Sherweb Distributor and Service Provider APIs."""

import asyncio
import logging
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Literal

import httpx

from ._json import error_envelope
from .config import DISTRIBUTOR_API, SERVICE_PROVIDER_API, TOKEN_URL

logger = logging.getLogger("sherweb_mcp.api")

Api = Literal["distributor", "service-provider"]
_BASES: dict[str, str] = {"distributor": DISTRIBUTOR_API, "service-provider": SERVICE_PROVIDER_API}

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own token, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    # A plain CookieJar: httpx uses it as is (an httpx.Cookies would be copied into a default jar).
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("invalid_argument", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class SherwebError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Sherweb API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class SherwebCredentials:
    client_id: str
    client_secret: str
    subscription_key: str

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        return "SherwebCredentials(client_id=<redacted>, client_secret=<redacted>, subscription_key=<redacted>)"


class SherwebClient:
    """Sherweb API wrapper for one caller's credentials.

    Access tokens are requested per API scope, lazily, and kept only on this
    instance, which lives for a single MCP request; nothing is cached across
    requests (so a wrong secret can never ride on someone else's token).
    """

    def __init__(self, credentials: SherwebCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self._tokens: dict[str, str] = {}
        self._token_locks: dict[str, asyncio.Lock] = {}

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client()

    async def token(self, scope: Api) -> str:
        lock = self._token_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            if scope not in self._tokens:
                self._tokens[scope] = await self._request_token(scope)
            return self._tokens[scope]

    async def _request_token(self, scope: Api) -> str:
        creds = self._credentials
        form = {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scope": scope,
        }
        try:
            resp = await self._client().post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
        except httpx.RequestError as e:
            raise SherwebError(0, f"Network error requesting a Sherweb token: {type(e).__name__}") from e
        logger.info("sherweb_api method=POST path=token scope=%s status=%s", scope, resp.status_code)
        if resp.status_code in (400, 401, 403):
            reason = _oauth_error(resp)
            if reason == "invalid_scope":
                raise SherwebError(403, f"This Sherweb API client is not allowed to use the {scope} API (invalid_scope)")
            raise SherwebError(401, f"Sherweb rejected the client id / secret ({reason or 'token request failed'})")
        if resp.status_code >= 400:
            raise SherwebError(resp.status_code, f"Sherweb token endpoint returned HTTP {resp.status_code}")
        try:
            token = resp.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise SherwebError(502, "Sherweb token response had no access_token")
        return token

    async def get(self, api: Api, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", api, path, params=params)

    async def post(self, api: Api, path: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", api, path, params=params, body=body)

    async def request(
        self, method: str, api: Api, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        url = f"{_BASES[api]}/{path.lstrip('/')}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {
            "Authorization": f"Bearer {await self.token(api)}",
            "Ocp-Apim-Subscription-Key": self._credentials.subscription_key,
            "Accept": "application/json",
            "Accept-Language": "en",
        }
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: SherwebError | None = None
            try:
                resp = await self._client().request(method, url, params=query, json=body, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, SherwebError(0, f"Timed out calling Sherweb ({method} {path})")
            except httpx.RequestError as e:
                status = 0
                error = SherwebError(0, f"Network error calling Sherweb ({method} {path}): {type(e).__name__}")

            logger.info(
                "sherweb_api method=%s api=%s path=%s status=%s ms=%d attempt=%d",
                method, api, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            if resp.status_code >= 400:
                raise SherwebError(resp.status_code, _problem_message(resp, path))
            return _parse_body(resp)


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _oauth_error(resp: httpx.Response) -> str | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None


def _problem_message(resp: httpx.Response, path: str) -> str:
    """Message from an RFC 7807 ProblemDetails body (title / detail / errors)."""
    fallback = f"Sherweb returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if not isinstance(payload, dict):
        return fallback
    lower = {str(k).lower(): v for k, v in payload.items()}
    parts = [str(lower[k]) for k in ("title", "detail", "message") if isinstance(lower.get(k), str) and lower[k]]
    errors = lower.get("errors")
    if isinstance(errors, dict):
        for field, problems in errors.items():
            for problem in problems if isinstance(problems, list) else [problems]:
                parts.append(f"{field}: {problem}")
    return f"{fallback}: {' - '.join(parts)[:500]}" if parts else fallback
