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
"""Tokens, retries, ProblemDetails errors and tool requests against a mocked Sherweb API."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from sherweb_mcp import api_client
from sherweb_mcp.api_client import SherwebClient, SherwebCredentials, SherwebError
from sherweb_mcp.server import _credentials_var, create_mcp_server

CREDS = SherwebCredentials(client_id="cid", client_secret="secret", subscription_key="sub-key")
CUSTOMER = "11111111-2222-3333-4444-555555555555"
SUB = "66666666-7777-8888-9999-000000000000"
TRACK = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


def token_ok(request: httpx.Request) -> httpx.Response:
    scope = parse_qs(request.content.decode())["scope"][0]
    return httpx.Response(200, json={"access_token": f"tok-{scope}", "expires_in": 3600, "token_type": "Bearer"})


class Recorder:
    """Mock transport: replies by "METHOD path" (path after api.sherweb.com)."""

    def __init__(self, routes=None):
        self.requests: list[httpx.Request] = []
        self.routes = {"POST /auth/oidc/connect/token": token_ok} | (routes or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        reply = self.routes.get(key, {})
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def api_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/oidc/" not in r.url.path]


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> SherwebClient:
    return SherwebClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


SP = "/service-provider/v1"


# ---- client ----------------------------------------------------------------


async def test_token_request_and_api_headers():
    rec = Recorder({f"GET {SP}/customers": {"items": []}})
    await _client(rec).get("service-provider", "customers")
    token_request, api_request = rec.requests
    form = parse_qs(token_request.content.decode())
    assert str(token_request.url) == "https://api.sherweb.com/auth/oidc/connect/token"
    assert form == {"grant_type": ["client_credentials"], "client_id": ["cid"], "client_secret": ["secret"], "scope": ["service-provider"]}
    assert "Ocp-Apim-Subscription-Key" not in token_request.headers
    assert api_request.headers["Authorization"] == "Bearer tok-service-provider"
    assert api_request.headers["Ocp-Apim-Subscription-Key"] == "sub-key"
    assert str(api_request.url) == "https://api.sherweb.com/service-provider/v1/customers"


async def test_one_token_per_scope_per_request():
    rec = Recorder({f"GET {SP}/customers": {"items": []}, "GET /distributor/v1/billing/payable-charges": {"charges": []}})
    client = _client(rec)
    await client.get("service-provider", "customers")
    await client.get("service-provider", "customers")
    await client.get("distributor", "billing/payable-charges")
    tokens = [r for r in rec.requests if "/oidc/" in r.url.path]
    assert [parse_qs(t.content.decode())["scope"][0] for t in tokens] == ["service-provider", "distributor"]
    assert rec.requests[-1].headers["Authorization"] == "Bearer tok-distributor"


@pytest.mark.parametrize(
    "oauth_error,status,text",
    [("invalid_client", 401, "client id / secret"), ("invalid_scope", 403, "not allowed")],
)
async def test_token_errors(monkeypatch, oauth_error, status, text):
    rec = Recorder({"POST /auth/oidc/connect/token": lambda r: httpx.Response(400, json={"error": oauth_error})})
    with pytest.raises(SherwebError) as err:
        await _client(rec).get("service-provider", "customers")
    assert err.value.status_code == status
    assert text in err.value.message
    assert "secret" not in err.value.message.replace("client id / secret", "")


async def test_problem_details_message():
    problem = {"title": "Access Denied", "status": 401, "detail": "Access denied due to invalid subscription key."}
    rec = Recorder({f"GET {SP}/customers": lambda r: httpx.Response(401, json=problem)})
    with pytest.raises(SherwebError) as err:
        await _client(rec).get("service-provider", "customers")
    assert err.value.status_code == 401
    assert "invalid subscription key" in err.value.message


async def test_get_retries_5xx_but_post_does_not():
    replies = iter([httpx.Response(503), httpx.Response(200, json={"items": []})])
    rec = Recorder(
        {
            f"GET {SP}/customers": lambda r: next(replies),
            f"POST {SP}/billing/subscriptions/amendments": lambda r: httpx.Response(500, json={"title": "boom"}),
        }
    )
    client = _client(rec)
    assert await client.get("service-provider", "customers") == {"items": []}
    with pytest.raises(SherwebError):
        await client.post("service-provider", "billing/subscriptions/amendments", {}, {"customerId": CUSTOMER})
    assert len([r for r in rec.api_calls() if r.method == "POST"]) == 1


async def test_status_probes_both_apis(monkeypatch):
    def token(request):
        scope = parse_qs(request.content.decode())["scope"][0]
        if scope == "distributor":
            return httpx.Response(400, json={"error": "invalid_scope"})
        return token_ok(request)

    rec = Recorder({"POST /auth/oidc/connect/token": token})
    result = await _call(monkeypatch, rec, "sherweb_status", {})
    assert result["apis"]["service-provider"] == {"authorized": True}
    assert result["apis"]["distributor"]["authorized"] is False


# ---- billing / customers -----------------------------------------------------

PAYABLE = {
    "periodFrom": "2026-06-01",
    "periodTo": "2026-06-30",
    "charges": [
        {"chargeId": "c1", "chargeType": "Recurring", "subTotal": 100.5, "currency": "USD"},
        {"chargeId": "c2", "chargeType": "Usage", "subTotal": 20, "currency": "USD"},
        {"chargeId": "c3", "chargeType": "Recurring", "subTotal": 9, "currency": "CAD"},
    ],
}


async def test_payable_charges_with_summary(monkeypatch):
    rec = Recorder({"GET /distributor/v1/billing/payable-charges": PAYABLE})
    result = await _call(monkeypatch, rec, "sherweb_billing_payable_charges", {"date": "2026-06-17"})
    assert rec.api_calls()[0].url.params["date"] == "2026-06-17"
    assert result["summary"] == {
        "charge_count": 3,
        "subTotal_by_currency": {"USD": 120.5, "CAD": 9.0},
        "charges_by_type": {"Recurring": 2, "Usage": 1},
    }
    assert len(result["charges"]) == 3


async def test_charge_details_found_and_missing(monkeypatch):
    rec = Recorder({"GET /distributor/v1/billing/payable-charges": PAYABLE})
    assert (await _call(monkeypatch, rec, "sherweb_billing_charge_details", {"chargeId": "c2"}))["subTotal"] == 20
    missing = await _call(monkeypatch, rec, "sherweb_billing_charge_details", {"chargeId": "nope"})
    assert missing["error"]["code"] == "not_found"
    assert "2026-06-01 to 2026-06-30" in missing["error"]["message"]


async def test_invalid_date_is_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "sherweb_billing_payable_charges", {"date": "2026-02-30"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


CUSTOMERS = {"items": [{"id": CUSTOMER, "displayName": "Example Holdings"}, {"id": "x", "displayName": "Sample Co"}]}


async def test_customers_list_search(monkeypatch):
    rec = Recorder({f"GET {SP}/customers": CUSTOMERS})
    result = await _call(monkeypatch, rec, "sherweb_customers_list", {"search": "example"})
    assert result == {"items": [CUSTOMERS["items"][0]], "count": 1}


async def test_customers_get(monkeypatch):
    rec = Recorder({f"GET {SP}/customers": CUSTOMERS})
    result = await _call(monkeypatch, rec, "sherweb_customers_get", {"customerId": CUSTOMER.upper()})
    assert result["displayName"] == "Example Holdings"


async def test_accounts_receivable(monkeypatch):
    receivable = {"periodFrom": "2026-06-01", "periodTo": "2026-06-30", "charges": [{"costPriceProrated": 12.5, "currency": "USD", "chargeType": "Recurring"}]}
    rec = Recorder({f"GET {SP}/billing/receivable-charges": receivable})
    result = await _call(monkeypatch, rec, "sherweb_customers_accounts_receivable", {"customerId": CUSTOMER})
    params = rec.api_calls()[0].url.params
    assert params["customerId"] == CUSTOMER and "date" not in params
    assert result["summary"]["costPriceProrated_by_currency"] == {"USD": 12.5}


async def test_customer_id_must_be_uuid(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "sherweb_subscriptions_list", {"customerId": "../customers"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


# ---- subscriptions / catalog ----------------------------------------------------

SUBS = {"items": [{"id": SUB, "productName": "Microsoft 365 Business Basic", "quantity": 10, "fees": {"amount": 6}}]}
DETAILS = {"items": [{"id": SUB, "quantity": 10, "estStatus": "Active", "skuComposition": [{"sku": "ABC"}]}]}


async def test_subscription_get_merges_list_and_details(monkeypatch):
    rec = Recorder({f"GET {SP}/billing/subscriptions": SUBS, f"GET {SP}/billing/subscriptions/details": DETAILS})
    result = await _call(monkeypatch, rec, "sherweb_subscriptions_get", {"customerId": CUSTOMER, "subscriptionId": SUB})
    assert result["fees"] == {"amount": 6}
    assert result["skuComposition"] == [{"sku": "ABC"}]
    assert result["estStatus"] == "Active"


async def test_change_quantity_submits_amendment(monkeypatch):
    receipt = {"subscriptionsAmendmentId": "deprecated", "trackingId": {"requestTrackingId": TRACK}}
    rec = Recorder({f"GET {SP}/billing/subscriptions": SUBS, f"POST {SP}/billing/subscriptions/amendments": receipt})
    args = {"customerId": CUSTOMER, "subscriptionId": SUB, "quantity": 12}
    result = await _call(monkeypatch, rec, "sherweb_subscriptions_change_quantity", args)
    post = [r for r in rec.api_calls() if r.method == "POST"][0]
    assert post.url.params["customerId"] == CUSTOMER
    assert json.loads(post.content) == {"subscriptionAmendmentParameters": [{"subscriptionId": SUB, "newQuantity": 12}]}
    assert (result["previousQuantity"], result["requestedQuantity"], result["trackingId"]) == (10, 12, TRACK)


async def test_change_quantity_noop_and_unknown_subscription(monkeypatch):
    rec = Recorder({f"GET {SP}/billing/subscriptions": SUBS})
    same = await _call(monkeypatch, rec, "sherweb_subscriptions_change_quantity", {"customerId": CUSTOMER, "subscriptionId": SUB, "quantity": 10})
    assert same["changed"] is False
    other = "99999999-9999-9999-9999-999999999999"
    missing = await _call(monkeypatch, rec, "sherweb_subscriptions_change_quantity", {"customerId": CUSTOMER, "subscriptionId": other, "quantity": 3})
    assert missing["error"]["code"] == "not_found"
    assert not [r for r in rec.api_calls() if r.method == "POST"]


async def test_amendment_status(monkeypatch):
    rec = Recorder({f"GET {SP}/tracking/{TRACK}": "Processing"})
    result = await _call(monkeypatch, rec, "sherweb_subscriptions_amendment_status", {"trackingId": TRACK})
    assert result == {"trackingId": TRACK, "status": "Processing"}


async def test_catalog_search(monkeypatch):
    catalog = {"customerId": CUSTOMER, "catalogItems": [{"sku": "M365-BB", "name": [{"value": "Microsoft 365 Business Basic"}]}, {"sku": "AV-1", "name": [{"value": "Antivirus"}]}]}
    rec = Recorder({f"GET {SP}/customer-catalogs/{CUSTOMER}": catalog})
    result = await _call(monkeypatch, rec, "sherweb_catalog_list_products", {"customerId": CUSTOMER, "search": "business"})
    assert [p["sku"] for p in result["catalogItems"]] == ["M365-BB"]
    assert result["count"] == 1
