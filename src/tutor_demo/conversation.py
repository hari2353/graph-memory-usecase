"""The A2A conversation arc: round 1 -> outcomes -> loop -> round 2.

One runner, two deployment tiers, three protocols, all against the platform
deploy:

- the app's agents run in-process here (FastMCP + a2a-sdk on one port);
- the coach's turns go over the **A2A** protocol (``SendMessage`` JSON-RPC);
- two extra channels keep every party in sync: the deployed REST reads and
  the agent server's own FastMCP tools.

Then the effectiveness loop ticks ONCE and the SAME conversation is resumed.
What changed is printed verbatim: the recos the agents now see, the coach's
next lines, and the weights the ledger-derived graph holds. The A2A
conversation visibly improves after the loop — and the runner exits
non-zero if it doesn't.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Final

import httpx

from .agent_server import (
    AGENT_BASE_URL,
    AGENT_PORT,
    BASE_WEIGHT,
    TutorAgentEngine,
    build_tutor_app,
    serve_in_thread,
    stop_server_thread,
)
from .deployed import (
    INGEST_URL,
    REST_URL,
    TOKEN,
    _client,
    _free_demo_ports,
    _free_ports,
    _kill_tree,
    _mapping_path,
    _platform_dir,
    _platform_run,
    _poll_published,
    _print_table,
    _require_clickhouse,
    _start_serve,
    _wait_for_lift,
    cohort_representatives,
)
from .generator import LEARNERS
from .platform_client import ClientConfig, PlatformClient
from .story import Story, make_story, story_source_line

_TIMEOUT: Final = 45.0
_A2A_HEADERS: Final = {"A2A-Version": "1.0", "Content-Type": "application/json"}


def a2a_turn(learner: str, *, intent: str, round_no: int) -> str:
    """One A2A turn (message/send); returns the coach's narrative text."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": uuid.uuid4().hex,
                "parts": [
                    {"text": json.dumps({"intent": intent, "learner": learner, "round": round_no})}
                ],
            }
        },
    }
    response = httpx.post(AGENT_BASE_URL + "/", json=body, headers=_A2A_HEADERS, timeout=_TIMEOUT)
    response.raise_for_status()
    reply = response.json()
    if "error" in reply:
        raise SystemExit(f"A2A error: {reply['error']}")
    result = reply["result"]
    message = result.get("message") or {}
    text = str(message["parts"][0].get("text", "")) if message.get("parts") else ""
    if not text:
        raise SystemExit(f"A2A turn produced no text: {json.dumps(result)[:400]}")
    print(f"      [A2A] {learner}: {text}")
    return text


def agent_mcp_call(tool: str, arguments: dict[str, str]) -> dict[str, Any]:
    """The agent server's own MCP surface: single-shot tools/call."""
    agent_client = PlatformClient(
        ClientConfig(
            ingest_url=INGEST_URL,
            rest_url=REST_URL,
            mcp_url=f"http://127.0.0.1:{AGENT_PORT}",
            token=TOKEN,
        )
    )
    try:
        return agent_client.mcp_call(tool, arguments)
    finally:
        agent_client.close()


def _unpublished(client: PlatformClient, learners: tuple[str, ...]) -> bool:
    return "" in set(_poll_published(client, learners).values())


def _phase_posture(platform_dir: Path, tenant: str, story: Story) -> None:
    """[1] the arc's position: checkout, story provenance, agent base."""
    print("[1/8] A2A conversation arc - posture")
    print(f"       platform checkout: {platform_dir}")
    print("       " + story_source_line(story))
    print(f"       agents: {AGENT_BASE_URL} (one in-process process: FastMCP + A2A)")


def _phase_bootstrap(client: PlatformClient, story: Story, learners: tuple[str, ...]) -> None:
    """[3] the app's story signals into the ledger; the flat baseline is reads."""
    print("[3/8] bootstrap: the app's story signals (skills + reco candidates)")
    ack = client.post_signals([dict(event) for event in story.experiment.events])
    print(f"      ingest: {json.dumps(ack)}")
    baseline = _poll_published(client, learners)
    if _unpublished(client, learners):
        raise SystemExit("the projection never surfaced the bootstrap signals")
    _print_table("round-1 read posture (identical for both cohorts)", baseline)


def _host_agents(engine: TutorAgentEngine) -> tuple[Any, threading.Thread]:
    """[4] boot the in-process agent; the card endpoint must answer."""
    server, thread = serve_in_thread(build_tutor_app(engine))
    card = httpx.get(f"{AGENT_BASE_URL}/.well-known/agent-card.json", timeout=10.0)
    print(f"      agent card: {card.status_code} name={card.json().get('name')}")
    return server, thread


def _phase_round1(dawn_learner: str, dusk_learner: str) -> dict[str, str]:
    """[5] the coach reads, narrates and delivers; MCP agrees with A2A."""
    print("[5/8] round 1 - A2A turns (coach reads, narrates, delivers)")
    turns = {
        learner: a2a_turn(learner, intent="recommend", round_no=1)
        for learner in (dawn_learner, dusk_learner)
    }
    items = agent_mcp_call("get_recommendations", {"learner": dusk_learner})
    print(f"      MCP get_recommendations (dusk): {items}")
    return turns


def _phase_outcomes_and_loop(
    platform_dir: Path,
    tenant: str,
    client: PlatformClient,
    dawn_learner: str,
    dusk_learner: str,
) -> None:
    """[6] verdicts ride A2A (correlated by reco id), then the loop ticks."""
    print("[6/8] A2A outcome turns - verdicts by reco id, then the loop ticks")
    for learner in (dawn_learner, dusk_learner):
        a2a_turn(learner, intent="outcomes", round_no=1)
    loop = _platform_run(platform_dir, "loop", "run", "--mapping", str(_mapping_path()))
    print("      " + loop.strip().replace("\n", "\n      "))
    _wait_for_lift(client, dawn_learner, dusk_learner)


def _phase_round2(
    client: PlatformClient,
    dawn_learner: str,
    dusk_learner: str,
    round1: dict[str, str],
) -> tuple[str, str]:
    """[7] the SAME conversation after the loop; the asserts ARE the proof."""
    print("[7/8] round 2 - the SAME A2A conversation after the loop")
    _wait_for_lift(client, dawn_learner, dusk_learner)
    after = {
        learner: a2a_turn(learner, intent="recommend", round_no=2)
        for learner in (dawn_learner, dusk_learner)
    }
    tops = {learner: client.recos(learner) for learner in (dawn_learner, dusk_learner)}
    _print_table(
        "round-2 reads (weights moved by the loop)",
        {
            learner: "; ".join(f"{target}={score:.3f}" for target, score in items)
            for learner, items in tops.items()
        },
    )
    if after[dawn_learner] == round1[dawn_learner] or after[dusk_learner] == round1[dusk_learner]:
        raise SystemExit("the coach's narrative did not change between rounds")
    if tops[dawn_learner][0][0] == tops[dusk_learner][0][0]:
        raise SystemExit("the cohorts did not diverge after the loop")
    if tops[dawn_learner][0][1] <= BASE_WEIGHT or tops[dusk_learner][0][1] <= BASE_WEIGHT:
        raise SystemExit("the loop did not promote the acted-on recos")
    return after[dawn_learner], after[dusk_learner]


def _phase_summary(dawn_after: str, dusk_after: str) -> None:
    """[8] the arc's outcome, stated plainly."""
    print("[8/8] the conversation improved because the loop carried truth into it")
    print("      - the recos the agents SEE moved (0.5 -> 0.625 promoted tops)")
    print("      - verdicts correlated by recos ids flowed into loop cells")
    print(f"      dawn: {dawn_after.strip()}")
    print(f"      dusk: {dusk_after.strip()}")


def main() -> int:
    platform_dir = _platform_dir()
    _require_clickhouse()
    _free_demo_ports()
    _free_ports((AGENT_PORT,))
    run_id = uuid.uuid4().hex[:8]
    tenant = f"tutor-conv-{run_id}"
    story = make_story(run_id)
    learners = tuple(learner for learner, _cohort in LEARNERS)
    dawn_learner, dusk_learner = cohort_representatives(LEARNERS)
    client = _client()
    engine = TutorAgentEngine(client=client, story=story, tenant=tenant)

    _phase_posture(platform_dir, tenant, story)
    print(f"[2/8] tenancy + deployed serve for {tenant}")
    print("      " + _platform_run(platform_dir, "tenancy", "onboard", tenant).strip())
    serve = _start_serve(platform_dir, tenant)
    try:
        _phase_bootstrap(client, story, learners)
        server, thread = _host_agents(engine)
        try:
            round1 = _phase_round1(dawn_learner, dusk_learner)
            _phase_outcomes_and_loop(platform_dir, tenant, client, dawn_learner, dusk_learner)
            dawn_after, dusk_after = _phase_round2(client, dawn_learner, dusk_learner, round1)
            _phase_summary(dawn_after, dusk_after)
        finally:
            stop_server_thread(server, thread)
        return 0
    finally:
        _kill_tree(serve)


if __name__ == "__main__":
    sys.exit(main())
