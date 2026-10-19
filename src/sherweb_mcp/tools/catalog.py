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
"""Per-customer product catalog."""

from mcp.server.fastmcp import FastMCP

from ..api_client import SherwebClient
from ._common import READ_ONLY, ClientFactory, CustomerId, Search, contains, guid, items, run_tool


def _matches(item: dict, needle: str) -> bool:
    names = item.get("name") if isinstance(item.get("name"), list) else []
    return contains(item.get("sku"), needle) or any(isinstance(n, dict) and contains(n.get("value"), needle) for n in names)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_catalog_list_products(customerId: CustomerId, search: Search = None) -> str:
        """List the products a customer can be sold: SKU, name, description, billing cycle and commitment term.

        Catalogs differ per customer; search filters on SKU or product name.
        """

        async def action(client: SherwebClient) -> dict:
            customer_id = guid(customerId, "customerId")
            payload = await client.get("service-provider", f"customer-catalogs/{customer_id}") or {}
            products = items(payload, "catalogItems")
            needle = (search or "").strip().lower()
            if needle:
                products = [p for p in products if _matches(p, needle)]
            return {"customerId": customer_id, "catalogItems": products, "count": len(products)}

        return await run_tool("sherweb_catalog_list_products", client_factory, action)
