"""The agent tier is testable offline: mock REST + real ASGI (in-process)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from starlette.testclient import TestClient as StarletteTestClient

from tutor_demo.agent_server import TutorAgentEngine, build_tutor_app
from tutor_demo.platform_client import ClientConfig, PlatformClient, cast_dict
from tutor_demo.story import llm_story, make_story

_A2A_HEADERS = {"A2A-Version": "1.0", "Content-Type": "application/json"}
_FLAT = [("skill:algebra", 0.5), ("skill:graph-theory", 0.5)]
_LIFTED_DAWN = [("skill:algebra", 0.625), ("skill:graph-theory", 0.375)]
_LIFTED_DUSK = [("skill:graph-theory", 0.625), ("skill:algebra", 0.375)]


def _client(reads: dict[str, list[tuple[str, float]]]) -> PlatformClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v1/signals:batch"):
            body = cast_dict(json.loads(request.content.decode("utf-8")))
            signals = body.get("signals") or []
            return httpx.Response(
                200, json={"accepted": len(signals), "duplicates": 0, "rejected": []}
            )
        learner = path.split("/")[3] if "/v1/context/" in path else ""
        capability = path.split("/")[4] if "/v1/context/" in path else ""
        items = reads.get(learner) if capability == "recos" else []
        return httpx.Response(
            200,
            json={"payload": {"items": [{"target": t, "score": s} for t, s in items or []]}},
        )

    config = ClientConfig(ingest_url="http://i", rest_url="http://r", mcp_url="http://m", token="t")
    return PlatformClient(config, transport=httpx.MockTransport(handler))


def test_story_fallback_is_scripted_and_contract_shaped() -> None:
    story = make_story("run-offline")
    assert story.spec.origin.startswith("scripted"), "no LLM should be configured offline"
    experiment = story.experiment
    assert experiment.run_id == "run-offline"
    skill_events = [e for e in experiment.events if e["type"] == "itf.skill_assessed.v1"]
    reco_events = [e for e in experiment.events if e["type"] == "itf.reco.v1"]
    assert len(skill_events) == 8 and len(reco_events) == 8
    classes = {cast_dict(event["data"])["reco_class"] for event in reco_events}
    assert classes == {"gap", "explore"}
    cohorts = {cast_dict(event["data"])["cohort"] for event in reco_events}
    assert cohorts == {"run-offline-tutor-dawn", "run-offline-tutor-dusk"}
    verdicts = [cast_dict(event["data"])["verdict"] for event in experiment.attached]
    assert verdicts.count("acted_on") == 4 and verdicts.count("dismissed") == 4


def test_llm_story_falls_back_when_no_endpoint_is_configured() -> None:
    spec = llm_story()
    assert spec.origin.startswith("scripted")
    assert len(spec.skills) == 2 and len(spec.cohorts) == 2


def test_engine_turns_narrate_then_improve_after_feedback() -> None:
    reads: dict[str, list[tuple[str, float]]] = {
        "learner-ada-1": _FLAT,
        "learner-bao-1": _FLAT,
    }
    client = _client(reads)
    story = make_story("run-agents")
    engine = TutorAgentEngine(client=client, story=story, tenant="tutor-app-run-agents")

    flat = engine.emit_recommendation("learner-ada-1", round_no=1)
    assert "round 1" in flat.text and "0.500" in flat.text
    assert flat.recos == tuple(_FLAT)
    assert {"accepted", "duplicates"} <= set(flat.ingest)

    reads.clear()  # the loop ticks: promoted tops for the acted-on reco
    reads.update({"learner-ada-1": _LIFTED_DAWN, "learner-bao-1": _LIFTED_DUSK})
    lift = engine.emit_recommendation("learner-ada-1", round_no=2)
    assert "0.625" in lift.text and "up from 0.5" in lift.text
    assert lift.text != flat.text

    logged = engine.log_outcomes("learner-ada-1")
    assert "acted_on" in logged.text and "dismissed" in logged.text


def _read_map(round_no: int) -> dict[str, list[tuple[str, float]]]:
    return {
        "learner-ada-1": _LIFTED_DAWN if round_no == 2 else _FLAT,
        "learner-bao-1": _LIFTED_DUSK if round_no == 2 else _FLAT,
    }


def _a2a_post(app: Any, learner: str, *, round_no: int) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": uuid.uuid4().hex,
                "parts": [
                    {
                        "text": json.dumps(
                            {"intent": "recommend", "learner": learner, "round": round_no}
                        )
                    }
                ],
            }
        },
    }
    with StarletteTestClient(app) as rest:
        response = rest.post("/", json=body, headers=_A2A_HEADERS)
        assert response.status_code == 200
        return cast_dict(response.json())


def test_a2a_surface_serves_card_and_agent_turns() -> None:
    client = _client(_read_map(round_no=2))
    story = make_story("run-a2a")
    app = build_tutor_app(TutorAgentEngine(client=client, story=story, tenant="t"))

    with StarletteTestClient(app) as rest:
        card = rest.get("/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["name"] == "tutor-coach"

        reply = _a2a_post(app, "learner-ada-1", round_no=2)
        assert reply["jsonrpc"] == "2.0"
        payload = cast_dict(reply["result"])
        message = cast_dict(payload["message"])
        assert message["role"] == "ROLE_AGENT"
        assert "0.625" in message["parts"][0]["text"]


def test_mcp_tool_reads_the_same_recos_via_fastmcp() -> None:
    client = _client(_read_map(round_no=2))
    story = make_story("run-mcp")
    app = build_tutor_app(TutorAgentEngine(client=client, story=story, tenant="t"))

    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_recommendations", "arguments": {"learner": "learner-ada-1"}},
    }
    with StarletteTestClient(app) as rest:
        response = rest.post("/mcp", json=body)
        assert response.status_code == 200
        result = cast_dict(response.json())["result"]
        content = cast_dict(result["structuredContent"])
        assert content["items"] == [["skill:algebra", 0.625], ["skill:graph-theory", 0.375]]


def test_plain_text_turn_defaults_to_recommend_intent() -> None:
    client = _client(_read_map(round_no=2))
    story = make_story("run-plain")
    app = build_tutor_app(TutorAgentEngine(client=client, story=story, tenant="t"))
    body = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": uuid.uuid4().hex,
                "parts": [{"text": "what should learner-ada-1 practice next?"}],
            }
        },
    }
    with StarletteTestClient(app) as rest:
        response = rest.post("/", json=body, headers=_A2A_HEADERS)
        assert response.status_code == 200
        message = cast_dict(cast_dict(response.json())["result"]["message"])
        assert "learner-ada-1" in message["parts"][0]["text"]
