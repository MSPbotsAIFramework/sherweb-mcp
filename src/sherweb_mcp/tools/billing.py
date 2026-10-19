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
"""Distributor billing: what the MSP owes Sherweb for a billing period."""

from collections import defaultdict
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import SherwebClient, SherwebError
from ._common import READ_ONLY, BillingDate, ClientFactory, billing_date, items, run_tool


def charge_summary(charges: list[dict], amount_key: str) -> dict:
    """Charge count and totals per currency and charge type."""
    totals: dict[str, float] = defaultdict(float)
    by_type: dict[str, int] = defaultdict(int)
    for charge in charges:
        amount = charge.get(amount_key)
        if isinstance(amount, (int, float)):
            totals[str(charge.get("currency") or "?")] += amount
        by_type[str(charge.get("chargeType") or "?")] += 1
    return {
        "charge_count": len(charges),
        f"{amount_key}_by_currency": {k: round(v, 2) for k, v in totals.items()},
        "charges_by_type": dict(by_type),
    }


async def payable_charges(client: SherwebClient, day: str | None) -> Any:
    return await client.get("distributor", "billing/payable-charges", {"date": billing_date(day)}) or {}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_billing_payable_charges(date: BillingDate = None) -> str:
        """Get what you owe Sherweb for a billing period: every charge with pricing, fees, taxes and invoice.

        Includes a summary of charge counts and subtotals per currency.
        """

        async def action(client: SherwebClient) -> dict:
            payload = await payable_charges(client, date)
            charges = items(payload, "charges")
            return {
                "periodFrom": payload.get("periodFrom"),
                "periodTo": payload.get("periodTo"),
                "summary": charge_summary(charges, "subTotal"),
                "charges": charges,
            }

        return await run_tool("sherweb_billing_payable_charges", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def sherweb_billing_charge_details(
        chargeId: Annotated[str, Field(description="Charge id from sherweb_billing_payable_charges.", min_length=1)],
        date: BillingDate = None,
    ) -> str:
        """Get one payable charge in full (pricing, deductions, fees, taxes, invoice) by its id.

        Charges only exist per billing period: pass a date inside the charge's period if it is not current.
        """

        async def action(client: SherwebClient) -> Any:
            payload = await payable_charges(client, date)
            wanted = chargeId.strip()
            charge = next((c for c in items(payload, "charges") if str(c.get("chargeId")) == wanted), None)
            if charge is None:
                period = f"{payload.get('periodFrom') or '?'} to {payload.get('periodTo') or '?'}"
                raise SherwebError(
                    404, f"Charge {wanted} is not in the billing period {period}; pass a date inside the charge's period."
                )
            return charge

        return await run_tool("sherweb_billing_charge_details", client_factory, action)
