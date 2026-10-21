"""How the tutor app talks to the deployed platform — HTTP only.

No platform code is imported here: the app is a pure third-party consumer of
the deployed channels (ingest :batch, REST /v1 context, single-shot MCP
JSON-RPC), exactly as any real tenant would integrate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ClientConfig:
    """Where the deployed platform lives and who the app talks as."""

    ingest_url: str
    rest_url: str
    mcp_url: str
    token: str
    timeout: float = 20.0


class PlatformClient:
    """Thin sync client for the three channels the tutor app uses."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._ingest = config.ingest_url.rstrip("/")
        self._rest = config.rest_url.rstrip("/")
        self._mcp = config.mcp_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {config.token}"}
        self._client = httpx.Client(timeout=config.timeout, transport=transport)

    # -- ingest ------------------------------------------------------------
    def post_signals(self, events: list[dict[str, Any]], *, chunk: int = 50) -> dict[str, Any]:
        """Idempotent ingest; oversized batches split like real producers do."""
        accepted = duplicates = 0
        rejections: list[Any] = []
        for start in range(0, len(events), chunk):
            body = {
                "signals": events[start : start + chunk],
                "idempotency_key": uuid.uuid4().hex,
            }
            response = self._client.post(
                f"{self._ingest}/v1/signals:batch", headers=self._headers, json=body
            )
            response.raise_for_status()
            payload = cast_dict(response.json())
            accepted += int(payload.get("accepted", 0))
            duplicates += int(payload.get("duplicates", 0))
            rejections.extend(payload.get("rejected") or [])
        return {"accepted": accepted, "duplicates": duplicates, "rejected": rejections}

    # -- context reads ------------------------------------------------------
    def context(self, user: str, capability: str) -> list[tuple[str, float]]:
        """Rendered items as (target, score) — the app's personalized reads."""
        response = self._client.get(
            f"{self._rest}/v1/context/{user}/{capability}", headers=self._headers
        )
        response.raise_for_status()
        payload = cast_dict(response.json())
        inner = cast_dict(payload.get("payload", {}))
        items = inner.get("items") or []
        return [(str(item["target"]), float(item.get("score", 0.0))) for item in items]

    def recos(self, user: str) -> list[tuple[str, float]]:
        return self.context(user, "recos")

    # -- MCP -----------------------------------------------------------------
    def mcp_call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Single-shot streamable-JSON call (the serve's stateless posture)."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        response = self._client.post(f"{self._mcp}/mcp", headers=self._headers, json=body)
        response.raise_for_status()
        reply = cast_dict(response.json())
        result = cast_dict(reply["result"])
        assert result.get("isError") is not True, f"MCP tool {tool} errored: {result}"
        structured = result.get("structuredContent")
        return cast_dict(structured if structured is not None else result)

    def close(self) -> None:
        self._client.close()


def cast_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got: {type(value).__name__}")
    return value


__all__ = ["PlatformClient", "cast_dict"]
