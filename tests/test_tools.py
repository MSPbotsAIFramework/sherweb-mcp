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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from sherweb_mcp.api_client import SherwebError
from sherweb_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "sherweb_navigate": {"domain"},
    "sherweb_status": set(),
    "sherweb_billing_payable_charges": set(),
    "sherweb_billing_charge_details": {"chargeId"},
    "sherweb_customers_list": set(),
    "sherweb_customers_get": {"customerId"},
    "sherweb_customers_accounts_receivable": {"customerId"},
    "sherweb_subscriptions_list": {"customerId"},
    "sherweb_subscriptions_get": {"customerId", "subscriptionId"},
    "sherweb_subscriptions_change_quantity": {"customerId", "subscriptionId", "quantity"},
    "sherweb_subscriptions_amendment_status": {"trackingId"},
    "sherweb_catalog_list_products": {"customerId"},
}

WRITE_TOOLS = {"sherweb_subscriptions_change_quantity"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_all_tools_listed(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is (name not in WRITE_TOOLS)
    assert bool(annotations.destructiveHint) is (name in WRITE_TOOLS)


def test_write_tool_says_when_to_use_it(tools):
    assert "explicitly asks" in tools["sherweb_subscriptions_change_quantity"].description


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (409, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(SherwebError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("sherweb_customers_list", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_navigate_needs_no_credentials():
    content, _ = asyncio.run(create_mcp_server().call_tool("sherweb_navigate", {"domain": "catalog"}))
    assert json.loads(content[0].text)["tools"] == ["sherweb_catalog_list_products"]
