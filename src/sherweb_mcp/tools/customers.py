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
"""Service-provider customers and their receivable charges."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import SherwebClient, SherwebError
from ._common import READ_ONLY, BillingDate, ClientFactory, CustomerId, Search, billing_date, contains, guid, items, run_tool
from .billing import charge_summary


async def all_customers(client: SherwebClient) -> list[dict]:
    return items(await client.get("service-provider", "customers"))


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_customers_list(search: Search = None) -> str:
        """List your customers: name, id, reseller path, suspension date and company contact details.

        search filters on the display name (Sherweb returns the whole list at once).
        """

        async def action(client: SherwebClient) -> dict:
            customers = await all_customers(client)
            needle = (search or "").strip().lower()
            if needle:
                customers = [c for c in customers if contains(c.get("displayName"), needle)]
            return {"items": customers, "count": len(customers)}

        return await run_tool("sherweb_customers_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_customers_get(customerId: CustomerId) -> str:
        """Get one customer by id: name, reseller path, suspension date and company contact details."""

        async def action(client: SherwebClient) -> Any:
            wanted = guid(customerId, "customerId").lower()
            customer = next((c for c in await all_customers(client) if str(c.get("id", "")).lower() == wanted), None)
            if customer is None:
                raise SherwebError(404, f"No customer {customerId.strip()} under this account; see sherweb_customers_list.")
            return customer

        return await run_tool("sherweb_customers_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_customers_accounts_receivable(customerId: CustomerId, date: BillingDate = None) -> str:
        """Get what you bill a customer for a billing period: each charge with cost, quantity and currency.

        Includes a summary of charge counts and cost totals per currency.
        """

        async def action(client: SherwebClient) -> dict:
            params = {"customerId": guid(customerId, "customerId"), "date": billing_date(date)}
            payload = await client.get("service-provider", "billing/receivable-charges", params) or {}
            charges = items(payload, "charges")
            return {
                "customerId": params["customerId"],
                "periodFrom": payload.get("periodFrom"),
                "periodTo": payload.get("periodTo"),
                "summary": charge_summary(charges, "costPriceProrated"),
                "charges": charges,
            }

        return await run_tool("sherweb_customers_accounts_receivable", client_factory, action)
