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
"""A customer's subscriptions, quantity changes and their tracking."""

import asyncio
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SherwebClient, SherwebError
from ._common import BILLING_CHANGE, READ_ONLY, ClientFactory, CustomerId, compact, guid, items, run_tool

SubscriptionId = Annotated[str, Field(description="Subscription id (UUID) from sherweb_subscriptions_list.")]


async def subscriptions(client: SherwebClient, customer_id: str) -> list[dict]:
    return items(await client.get("service-provider", "billing/subscriptions", {"customerId": customer_id}))


def _find(rows: list[dict], subscription_id: str) -> dict | None:
    wanted = subscription_id.lower()
    return next((r for r in rows if str(r.get("id", "")).lower() == wanted), None)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_subscriptions_list(customerId: CustomerId) -> str:
        """List a customer's subscriptions: product, SKU, quantity, billing cycle, purchase date, fees, term."""

        async def action(client: SherwebClient) -> dict:
            rows = await subscriptions(client, guid(customerId, "customerId"))
            return {"items": rows, "count": len(rows)}

        return await run_tool("sherweb_subscriptions_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_subscriptions_get(customerId: CustomerId, subscriptionId: SubscriptionId) -> str:
        """Get one subscription in full: quantity, status, commitment term, fees and SKU composition."""

        async def action(client: SherwebClient) -> dict:
            customer_id = guid(customerId, "customerId")
            subscription_id = guid(subscriptionId, "subscriptionId")
            # Fees are only on the list and the SKU composition only on the details,
            # so read both (Sherweb allows 3 concurrent requests).
            summary_rows, detail_payload = await asyncio.gather(
                subscriptions(client, customer_id),
                client.get("service-provider", "billing/subscriptions/details", {"customerId": customer_id}),
            )
            summary, detail = _find(summary_rows, subscription_id), _find(items(detail_payload), subscription_id)
            if summary is None and detail is None:
                raise SherwebError(
                    404, f"No subscription {subscription_id} for customer {customer_id}; see sherweb_subscriptions_list."
                )
            return {**(summary or {}), **(detail or {})}

        return await run_tool("sherweb_subscriptions_get", client_factory, action)

    @mcp.tool(annotations=BILLING_CHANGE)
    async def sherweb_subscriptions_change_quantity(
        customerId: CustomerId,
        subscriptionId: SubscriptionId,
        quantity: Annotated[int, Field(description="New total number of seats / licences.", ge=0)],
    ) -> str:
        """Change a subscription's seat / licence count, which changes billing.

        Only when the user explicitly asks.

        Asynchronous: returns a trackingId for sherweb_subscriptions_amendment_status, not a finished change.
        """

        async def action(client: SherwebClient) -> dict:
            customer_id = guid(customerId, "customerId")
            subscription_id = guid(subscriptionId, "subscriptionId")
            current = _find(await subscriptions(client, customer_id), subscription_id)
            if current is None:
                raise SherwebError(404, f"No subscription {subscription_id} for customer {customer_id}; nothing was changed.")
            previous = current.get("quantity")
            if previous == quantity:
                return {"changed": False, "subscriptionId": subscription_id, "quantity": quantity, "note": "Quantity is already set."}
            body = {"subscriptionAmendmentParameters": [{"subscriptionId": subscription_id, "newQuantity": quantity}]}
            receipt = await client.post("service-provider", "billing/subscriptions/amendments", body, {"customerId": customer_id})
            receipt = receipt if isinstance(receipt, dict) else {}
            tracking = (receipt.get("trackingId") or {}).get("requestTrackingId") if isinstance(receipt.get("trackingId"), dict) else None
            return compact(
                {
                    "submitted": True,
                    "customerId": customer_id,
                    "subscriptionId": subscription_id,
                    "productName": current.get("productName"),
                    "previousQuantity": previous,
                    "requestedQuantity": quantity,
                    "trackingId": tracking,
                    "note": "Accepted for processing, not applied yet. "
                    + (
                        f"Check sherweb_subscriptions_amendment_status with trackingId {tracking}."
                        if tracking
                        else "Sherweb returned no tracking id; check the subscription later."
                    ),
                }
            )

        return await run_tool("sherweb_subscriptions_change_quantity", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_subscriptions_amendment_status(
        trackingId: Annotated[str, Field(description="trackingId returned by sherweb_subscriptions_change_quantity.")],
    ) -> str:
        """Check a submitted quantity change: Unknown, Queued, Processing, Success or Failure."""

        async def action(client: SherwebClient) -> dict:
            tracking = guid(trackingId, "trackingId")
            status = await client.get("service-provider", f"tracking/{tracking}")
            return {"trackingId": tracking, "status": status}

        return await run_tool("sherweb_subscriptions_amendment_status", client_factory, action)
