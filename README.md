# Sherweb MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Sherweb](https://www.sherweb.com)
Partner APIs, used by MSPs that resell cloud subscriptions (Microsoft 365 and others) through
Sherweb. It lets an AI agent read what the MSP owes Sherweb (Distributor API) and the MSP's
customers, their subscriptions, catalogs and receivable charges (Service Provider API) and, on
explicit request, change a subscription's quantity.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; access tokens live only for the duration of one request
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Sherweb-Client-ID` | Yes | string | none | - | Sherweb API client id | `your-client-id` |
| `X-Sherweb-Client-Secret` | Yes | string | none | - | Sherweb API client secret | `your-client-secret` |
| `X-Sherweb-Subscription-Key` | Yes | string | none | - | Sherweb API subscription key | `your-subscription-key` |

Where to get them:

- In the Sherweb partner portal open **Security -> APIs**. Create API credentials to get the
  client id and secret, and copy the subscription key shown for the API subscription.
- Give the client access to the **Distributor API** (billing tools) and the **Service Provider
  API** (customers, subscriptions, catalog). A client that only has one of them can still use
  that API's tools; `sherweb_status` shows which ones the credentials reach.

How credentials are sent to Sherweb: the server gets an access token from
`POST https://api.sherweb.com/auth/oidc/connect/token` (client credentials grant, scope
`distributor` or `service-provider`, requested only when a tool needs that API), then calls
`https://api.sherweb.com/distributor/v1/...` or `https://api.sherweb.com/service-provider/v1/...`
with `Authorization: Bearer <token>` and `Ocp-Apim-Subscription-Key: <subscription key>`.
Tokens are never reused across requests. Values that look like unresolved templates (`${...}`)
are treated as missing.

A request to `/mcp` without all three headers gets HTTP 401 naming the missing ones.

## Tools

All tools return a JSON string. Customer and subscription ids are UUIDs. Billing tools take any
`date` inside the wanted billing period (`yyyy-MM-dd`, UTC) and default to the current period.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `sherweb_navigate` | Describe the tools of one area (changes nothing) | **domain** (`billing`, `customers`, `subscriptions`, `catalog`) |
| `sherweb_status` | Check which Sherweb APIs the credentials can reach | - |
| `sherweb_billing_payable_charges` | What you owe Sherweb for a billing period, with a per-currency summary | date |
| `sherweb_billing_charge_details` | One payable charge in full | **chargeId**, date |
| `sherweb_customers_list` | Your customers, optionally filtered by name | search |
| `sherweb_customers_get` | One customer | **customerId** |
| `sherweb_customers_accounts_receivable` | What you bill a customer for a billing period, with a summary | **customerId**, date |
| `sherweb_subscriptions_list` | A customer's subscriptions | **customerId** |
| `sherweb_subscriptions_get` | One subscription in full (fees, status, SKU composition) | **customerId**, **subscriptionId** |
| `sherweb_subscriptions_change_quantity` | Change a subscription's seat count (**write**, changes billing) | **customerId**, **subscriptionId**, **quantity** |
| `sherweb_subscriptions_amendment_status` | Status of a submitted quantity change | **trackingId** |
| `sherweb_catalog_list_products` | Products a customer can be sold, optionally filtered | **customerId**, search |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Sherweb returned HTTP 401 for customers: Access Denied - Access denied due to invalid subscription key.", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`; Sherweb's ProblemDetails title and detail are included.
Sherweb allows 3 concurrent requests, 100 requests per minute and 10,000 per day per API.
Reads are retried with backoff on 429, 5xx and network errors; the quantity change is retried
only on 429, so it never runs twice.

## Run locally

```bash
uv sync
uv run python -m sherweb_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t sherweb-mcp:dev .
docker run --rm -p 8080:8080 sherweb-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Sherweb-Client-ID: your-client-id" \
  -H "X-Sherweb-Client-Secret: your-client-secret" \
  -H "X-Sherweb-Subscription-Key: your-subscription-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# customers whose name contains "example"
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Sherweb-Client-ID: your-client-id" \
  -H "X-Sherweb-Client-Secret: your-client-secret" \
  -H "X-Sherweb-Subscription-Key: your-subscription-key" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"sherweb_customers_list","arguments":{"search":"example"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (12).** Kept as is because the tool names are an existing contract; every tool is
  listed from the first `tools/list`.
- **No shared token cache.** Older versions cached access tokens in the process under the client
  id and subscription key, without the secret, so a caller with a wrong secret could reuse a token
  another caller had obtained. Tokens are now requested per request and per API.
- **Quantity changes.** `sherweb_subscriptions_change_quantity` first confirms the subscription
  exists for that customer and reports the previous quantity; asking for the current quantity
  changes nothing. Sherweb processes the change asynchronously: poll
  `sherweb_subscriptions_amendment_status` with the returned `trackingId`.
- **Subscription details.** `sherweb_subscriptions_get` merges the subscription list (fees) with
  the details endpoint (status, SKU composition).
- **Charge summaries.** Payable and receivable charge tools add charge counts and totals per
  currency so large periods can be read without scanning every charge.
- **No interactive prompts.** The confirmation prompt older versions showed before a quantity
  change cannot be relayed by a stateless gateway; the tool description and its destructive
  annotation tell the agent to act only on explicit request.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
