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
"""Tool discovery (kept for compatibility) and the connection check."""

import asyncio
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import SherwebClient, SherwebError
from ._common import LOCAL, READ_ONLY, ClientFactory, run_tool

DOMAINS: dict[str, tuple[str, list[str]]] = {
    "billing": (
        "Distributor payable charges (what you owe Sherweb) for a billing period, and one charge's detail.",
        ["sherweb_billing_payable_charges", "sherweb_billing_charge_details"],
    ),
    "customers": (
        "Your customers, and the receivable charges you bill each of them.",
        ["sherweb_customers_list", "sherweb_customers_get", "sherweb_customers_accounts_receivable"],
    ),
    "subscriptions": (
        "A customer's subscriptions, their details, quantity changes and change status.",
        [
            "sherweb_subscriptions_list",
            "sherweb_subscriptions_get",
            "sherweb_subscriptions_change_quantity",
            "sherweb_subscriptions_amendment_status",
        ],
    ),
    "catalog": ("Products a given customer can be sold.", ["sherweb_catalog_list_products"]),
}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def sherweb_navigate(
        domain: Annotated[Literal["billing", "customers", "subscriptions", "catalog"], Field(description="Area to describe.")],
    ) -> str:
        """Describe the tools of one Sherweb area. All tools are already callable; this changes nothing."""
        summary, tools = DOMAINS[domain]
        return to_json({"domain": domain, "summary": summary, "tools": tools})

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_status() -> str:
        """Check the credentials: which Sherweb APIs (distributor, service provider) they can reach."""

        async def action(client: SherwebClient) -> dict:
            async def probe(scope: str) -> dict:
                try:
                    await client.token(scope)  # type: ignore[arg-type]
                    return {"authorized": True}
                except SherwebError as e:
                    return {"authorized": False, "error": e.message}

            distributor, service_provider = await asyncio.gather(probe("distributor"), probe("service-provider"))
            return {
                "apis": {"distributor": distributor, "service-provider": service_provider},
                "domains": {name: summary for name, (summary, _tools) in DOMAINS.items()},
                "note": "Token checks only; the subscription key is validated on the first API call.",
            }

        return await run_tool("sherweb_status", client_factory, action)
