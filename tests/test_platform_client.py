"""The HTTP client is testable without any platform: httpx MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tutor_demo.platform_client import ClientConfig, PlatformClient, cast_dict


def _req_json(request: httpx.Request) -> dict[str, Any]:
    return cast_dict(json.loads(request.content.decode("utf-8")))


def _client(
    handler: Any, *, ingest: str = "http://i", rest: str = "http://r", mcp: str = "http://m"
) -> PlatformClient:
    config = ClientConfig(ingest_url=ingest, rest_url=rest, mcp_url=mcp, token="tutor-demo-token")
    return PlatformClient(config, transport=httpx.MockTransport(handler))


def test_signals_batch_is_posted_as_envelope_list_and_acks_sum() -> None:
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(_req_json(request))
        return httpx.Response(200, json={"accepted": 2, "duplicates": 0, "rejected": []})

    client = _client(handler)
    events = [{"specversion": "1.0", "id": f"e{i}", "type": "itf.reco.v1"} for i in range(4)]
    ack = client.post_signals(events, chunk=2)
    assert ack == {"accepted": 4, "duplicates": 0, "rejected": []}
    assert len(seen_bodies) == 2  # chunked into two batches
    for body in seen_bodies:
        assert "signals" in body  # the platform's ingest envelope key
        assert "idempotency_key" in body


def test_context_read_renders_target_score_pairs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/context/learner-x/recos"
        return httpx.Response(
            200,
            json={
                "payload": {
                    "items": [
                        {"kind": "RECOMMENDED", "target": "skill:algebra", "score": 1.5},
                        {"kind": "RECOMMENDED", "target": "skill:graph", "score": 0.4},
                    ]
                }
            },
        )

    client = _client(handler)
    assert client.recos("learner-x") == [("skill:algebra", 1.5), ("skill:graph", 0.4)]


def test_mcp_call_returns_structured_content_and_surfaces_tool_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _req_json(request)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "get_effectiveness"
        assert request.headers["Authorization"] == "Bearer tutor-demo-token"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "isError": False,
                    "structuredContent": {
                        "tenant": "tutor-app",
                        "reco_delivered": 8,
                        "attached": 8,
                        "unattached": 0,
                    },
                },
            },
        )

    client = _client(handler)
    report = client.mcp_call("get_effectiveness", {})
    assert report["tenant"] == "tutor-app"
    assert report["attached"] == 8


def test_mcp_tool_error_raises_instead_of_returning_fake_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "not wired"}],
                    "isError": True,
                },
            },
        )

    client = _client(handler)
    with pytest.raises(AssertionError, match="not wired"):
        client.mcp_call("explain_context", {"user": "u", "capability": "recos"})


def test_cast_dict_rejects_non_objects() -> None:
    assert cast_dict({"a": 1}) == {"a": 1}
    with pytest.raises(ValueError, match="JSON object"):
        cast_dict("nope")
