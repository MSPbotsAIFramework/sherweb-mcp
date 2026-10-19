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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import SherwebClient, SherwebCredentials
from .config import Settings

HEADER_CLIENT_ID = "X-Sherweb-Client-ID"
HEADER_CLIENT_SECRET = "X-Sherweb-Client-Secret"
HEADER_SUBSCRIPTION_KEY = "X-Sherweb-Subscription-Key"
REQUIRED_HEADERS = [HEADER_CLIENT_ID, HEADER_CLIENT_SECRET, HEADER_SUBSCRIPTION_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[SherwebCredentials | None] = contextvars.ContextVar(
    "sherweb_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("sherweb_request_id", default="-")

INSTRUCTIONS = (
    "Sherweb is a cloud-services distributor that MSPs resell through "
    "(Microsoft 365 and other subscriptions). Two views are available:\n"
    "- Distributor: what the MSP owes Sherweb - sherweb_billing_payable_charges "
    "and sherweb_billing_charge_details for a billing period.\n"
    "- Service provider: the MSP's own customers - sherweb_customers_list, "
    "sherweb_customers_accounts_receivable (what the MSP bills them), "
    "sherweb_subscriptions_list / _get and sherweb_catalog_list_products.\n"
    "Usage:\n"
    "- Customer and subscription ids are UUIDs; start with "
    "sherweb_customers_list(search) to find a customer.\n"
    "- Billing tools take any date inside the wanted period (yyyy-MM-dd); "
    "omit it for the current period.\n"
    "- sherweb_subscriptions_change_quantity changes licence counts and billing; "
    "it is asynchronous, so poll sherweb_subscriptions_amendment_status with the "
    "returned trackingId. Call it only when the user explicitly asks, with the "
    "exact customer, subscription and quantity.\n"
    "Sherweb allows 3 concurrent and 100 requests per minute per API."
)


def get_client_from_context() -> SherwebClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return SherwebClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads the three Sherweb headers on /mcp requests; 401 when any is missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        values = {name: _clean(request.headers.get(name)) for name in REQUIRED_HEADERS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        credentials = SherwebCredentials(
            client_id=values[HEADER_CLIENT_ID] or "",
            client_secret=values[HEADER_CLIENT_SECRET] or "",
            subscription_key=values[HEADER_SUBSCRIPTION_KEY] or "",
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="sherweb-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], SherwebClient | None] = get_client_from_context

    from .tools import billing, catalog, customers, navigation, subscriptions

    for module in (navigation, billing, customers, subscriptions, catalog):
        module.register(mcp, client_factory)

    return mcp
