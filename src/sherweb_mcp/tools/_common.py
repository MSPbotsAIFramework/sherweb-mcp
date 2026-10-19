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
"""Shared argument types, lookup helpers and the error-handling wrapper."""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import SherwebClient, SherwebError

logger = logging.getLogger("sherweb_mcp.tools")

ClientFactory = Callable[[], SherwebClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
# Changing a licence count changes what the customer is billed; setting the
# same quantity again is harmless.
BILLING_CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Sherweb credentials are missing for this request (X-Sherweb-Client-ID, X-Sherweb-Client-Secret, "
    "X-Sherweb-Subscription-Key).",
    False,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

BillingDate = Annotated[
    str | None,
    Field(
        description="Any date inside the billing period, yyyy-MM-dd (UTC); omit for the current period.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]
CustomerId = Annotated[str, Field(description="Customer id (UUID) from sherweb_customers_list.")]
Search = Annotated[str | None, Field(description="Case-insensitive text to filter on.")]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def guid(value: str, name: str) -> str:
    text = value.strip()
    if not _UUID.match(text):
        raise ValueError(f"{name} must be a Sherweb id (UUID), got {value!r}.")
    return text


def billing_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"date must be a real date as yyyy-MM-dd, got {value!r}.") from e
    return value


def contains(value: Any, needle: str) -> bool:
    return needle in str(value or "").lower()


def items(payload: Any, key: str = "items") -> list[dict]:
    rows = payload.get(key) if isinstance(payload, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[SherwebClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except SherwebError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Sherweb returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
