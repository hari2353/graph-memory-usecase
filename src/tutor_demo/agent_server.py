"""The tutor app's agent tier: FastMCP tools + an A2A agent in one process.

The coach agent is the app's conversational surface. Its tools talk to the
deployed platform over HTTP only (REST reads, ingest of the recos it delivers
and the outcomes it logs) — no platform code is imported:

- **MCP (FastMCP)**: ``get_recommendations``, ``emit_recommendation``,
  ``log_outcomes``, ``get_skills`` — single-shot JSON-RPC at ``/mcp``.
- **A2A (a2a-sdk)**: agent card at ``/.well-known/agent-card.json`` plus
  ``message/send`` JSON-RPC at ``/`` — the coach reads a turn's intent
  (`{"intent": "recommend"|"outcomes", "learner": ...}`) and answers with
  narrative text shaped by the run's story.

Every reply names its data source: the ledger weights (REST) and the story
provenance (``(llm)`` or ``(scripted)``) — the conversation never pretends
the AI invented the numbers; the ledger did.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Final

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, DEFAULT_RPC_URL
from fastmcp import FastMCP

from .deployed import _client
from .platform_client import PlatformClient
from .story import Story, make_story

AGENT_HOST: Final = "127.0.0.1"
AGENT_PORT: Final = int(os.environ.get("GMP_TUTOR_AGENT_PORT", "9101"))
AGENT_BASE_URL: Final = f"http://{AGENT_HOST}:{AGENT_PORT}"
MIN_READS: Final = 2
BASE_WEIGHT: Final = 0.5


@dataclass(frozen=True)
class Turn:
    """One coach turn: narrative + the reads it was built from."""

    text: str
    recos: tuple[tuple[str, float], ...]
    ingest: dict[str, int]


def _source_tag(story: Story) -> str:
    return "(llm)" if story.spec.origin.startswith("llm:") else "(scripted)"


class TutorAgentEngine:
    """The coach's brain: ledger reads in, writes out, narrative between."""

    def __init__(
        self,
        client: PlatformClient,
        story: Story,
        tenant: str,
    ) -> None:
        self._client = client
        self._story = story
        self._tenant = tenant

    # -- the app-side capabilities, exposed via MCP tools and A2A intents ----
    def get_recommendations(self, learner: str) -> list[tuple[str, float]]:
        return self._client.recos(learner)

    def get_skills(self, learner: str) -> list[tuple[str, float]]:
        return self._client.context(learner, "skills")

    def emit_recommendation(self, learner: str, round_no: int) -> Turn:
        """Narrate + deliver the recommendation bundle for one learner."""
        recos = self.get_recommendations(learner)
        ingest = self._deliver_recos(learner)
        payload = self._narrate(learner, recos, round_no)
        return Turn(text=payload, recos=tuple(recos), ingest=ingest)

    def log_outcomes(self, learner: str) -> Turn:
        """Emit the learner's verdicts, correlated by the delivered reco ids."""
        events = [
            event
            for event in self._story.experiment.attached
            if event.get("partitionkey") == learner
        ]
        ack = self._client.post_signals([dict(event) for event in events])
        verdicts = {
            self._story.skills[0]: "",
            self._story.skills[1]: "",
        }
        for event in events:
            data = event.get("data") or {}
            for skill in self._story.skills:
                if data.get("correlation_id") == self._story.experiment.reco_id.get(
                    (learner, skill)
                ):
                    verdicts[skill] = str(data.get("verdict"))
        text = ", ".join(f"{skill}: {verdicts[skill]}" for skill in self._story.skills)
        tag = _source_tag(self._story)
        narrative = (
            f"logged your verdicts ({text}). "
            f"The effectiveness loop will read them from the ledger; "
            f"next round I re-check the plan. [{tag}]"
            if verdicts[self._story.skills[0]]
            else f"nothing to log - check the run state. [{tag}]"
        )
        return Turn(text=narrative, recos=(), ingest=ack)

    # -- internals -----------------------------------------------------------
    def dispatch(self, intent: dict[str, Any]) -> Turn:
        """Route one intent payload (A2A text or MCP arguments) to the brain."""
        learner = str(intent.get("learner", ""))
        intent_name = str(intent.get("intent", "recommend"))
        if intent_name == "outcomes":
            return self.log_outcomes(learner)
        return self.emit_recommendation(learner, int(intent.get("round", 1)))

    def _deliver_recos(self, learner: str) -> dict[str, int]:
        """Re-emit the story's reco envelopes: idempotent delivery proof."""
        events = [
            dict(event)
            for event in self._story.experiment.events
            if event.get("type") == "itf.reco.v1" and event.get("partitionkey") == learner
        ]
        return self._client.post_signals(events) if events else {"accepted": 0, "duplicates": 0}

    def _narrate(self, learner: str, recos: list[tuple[str, float]], round_no: int) -> str:
        cohort = self._story.cohort_of(learner)
        top_target, top_weight = recos[0] if recos else ("", 0.0)
        skill = top_target.removeprefix("skill:")
        line = self._story.line(learner, skill)
        tag = _source_tag(self._story)
        head = f"[round {round_no}] {learner} ({cohort} cohort)"
        if round_no == 1 or top_weight <= BASE_WEIGHT:
            body = (
                f"your plan currently ranks {skill} at {top_weight:.3f} - {line}"
                f"{_alt_pose(recos)} I've marked these delivered."
            )
            return f"{head}: {body} [{tag}]"
        moved = (
            f"{top_weight:.3f} - up from 0.5: your cohort's acted-on outcome was "
            f"promoted by the loop {skill} is worth more now. {line}"
        )
        return f"{head}: your plan reads {moved} [{tag}]"


def _alt_pose(recos: list[tuple[str, float]]) -> str:
    if len(recos) < MIN_READS:
        return ""
    alt_target, alt_weight = recos[1]
    return f" Alternatively {alt_target.removeprefix('skill:')} sits at {alt_weight:.3f}."


class TutorAgentExecutor(AgentExecutor):
    """A2A wrapper: parse a turn, answer with narrative text."""

    def __init__(self, engine: TutorAgentEngine) -> None:
        self._engine = engine

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        turn = self._engine.dispatch(self._intent(context.get_user_input()))
        await event_queue.enqueue_event(self._agent_message(turn.text, context.context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Single-shot turns are not cancellable."""

    def _intent(self, raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        words = raw.split()
        learner = next((word for word in words if word.startswith("learner-")), "")
        return {"intent": "recommend", "learner": learner, "round": 1}

    def _agent_message(self, text: str, context_id: str | None) -> Message:
        part = Part(text=text)
        message = Message(
            message_id=uuid.uuid4().hex,
            context_id=context_id or "",
            role="ROLE_AGENT",
            parts=[part],
        )
        return message


def build_mcp(engine: TutorAgentEngine) -> FastMCP:
    """The app's MCP surface (FastMCP): tools over the same engine."""
    mcp = FastMCP("tutor-coach")

    @mcp.tool
    def get_recommendations(learner: str) -> dict[str, Any]:
        return {
            "learner": learner,
            "items": [list(item) for item in engine.get_recommendations(learner)],
        }

    @mcp.tool
    def emit_recommendation(learner: str, round_no: int = 1) -> dict[str, Any]:
        turn = engine.emit_recommendation(learner, round_no)
        return {
            "text": turn.text,
            "ingest": turn.ingest,
            "recos": [list(item) for item in turn.recos],
        }

    @mcp.tool
    def log_outcomes(learner: str) -> dict[str, Any]:
        turn = engine.log_outcomes(learner)
        return {"text": turn.text, "ingest": turn.ingest}

    @mcp.tool
    def get_skills(learner: str) -> dict[str, Any]:
        return {"learner": learner, "items": [list(item) for item in engine.get_skills(learner)]}

    return mcp


def _agent_card() -> AgentCard:
    return AgentCard(
        name="tutor-coach",
        description="Personalization coach for the tutor app (FastMCP + A2A)",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        supported_interfaces=[
            AgentInterface(
                url=AGENT_BASE_URL + "/",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="recommend",
                name="deliver-recos",
                description="reads the learner's recos and answers with a plan",
                tags=["personalization", "education"],
            ),
            AgentSkill(
                id="outcomes",
                name="log-verdicts",
                description="records the learner's acted_on/dismissed verdicts",
                tags=["outcomes", "education"],
            ),
        ],
    )


def build_tutor_app(engine: TutorAgentEngine, *, include_a2a: bool = True) -> Any:
    """One uvicorn app: FastMCP's ASGI tree + (optionally) the A2A surface."""
    mcp = build_mcp(engine)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    if not include_a2a:
        return app
    handler = LegacyRequestHandler(
        agent_executor=TutorAgentExecutor(engine),
        task_store=InMemoryTaskStore(),
        agent_card=_agent_card(),
    )
    card_routes = create_agent_card_routes(agent_card=_agent_card())
    rpc_routes = create_jsonrpc_routes(handler, rpc_url=DEFAULT_RPC_URL)
    for route in (*card_routes, *rpc_routes):
        app.router.routes.append(route)
    return app


def build_standalone_engine() -> TutorAgentEngine:
    """CLI boot: env tenant + the deterministic story (no cross-run state)."""
    tenant = os.environ.get("GMP_TUTOR_TENANT", "tutor-app")
    return TutorAgentEngine(client=_client(), story=make_story(), tenant=tenant)


def main() -> int:
    """`tutor-agent`: A2A card + JSON-RPC + FastMCP tools on one port."""
    engine = build_standalone_engine()
    app = build_tutor_app(engine)
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT, log_level="warning")
    return 0


def mcp_main() -> int:
    """`tutor-mcp`: the MCP-only surface (FastMCP http app)."""
    app = build_tutor_app(build_standalone_engine(), include_a2a=False)
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT, log_level="warning")
    return 0


def serve_in_thread(app: Any) -> tuple[Any, threading.Thread]:
    """In-process uvicorn for runners that host the agent alongside themselves."""
    server = uvicorn.Server(
        uvicorn.Config(app, host=AGENT_HOST, port=AGENT_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def stop_server_thread(server: Any, thread: threading.Thread) -> None:
    """Ask uvicorn to exit; join the thread (lifespan teardown included)."""
    server.should_exit = True
    thread.join(timeout=20.0)


__all__ = [
    "AGENT_BASE_URL",
    "AGENT_CARD_WELL_KNOWN_PATH",
    "AGENT_HOST",
    "AGENT_PORT",
    "Turn",
    "TutorAgentEngine",
    "build_tutor_app",
    "main",
    "mcp_main",
    "serve_in_thread",
    "stop_server_thread",
]
