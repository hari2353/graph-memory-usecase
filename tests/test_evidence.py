"""Market-steal additions: the app exercises the platform's point-in-time
reads (Zep/Graphiti's headline property, now over the app's own client),
produces per-learner evidence cards (the tutor market's mastery-proof
artifact), and gates runs on honest memory semantics."""

from __future__ import annotations

import httpx

from tutor_demo.evidence import learner_card, run_gate
from tutor_demo.platform_client import ClientConfig, PlatformClient


def _client(handler) -> PlatformClient:
    config = ClientConfig(
        ingest_url="http://ingest",
        rest_url="http://rest",
        mcp_url="http://mcp",
        token="tutor-demo-token",
    )
    return PlatformClient(config, transport=httpx.MockTransport(handler))


# ── mid-story memory read (as_of reaches the wire) ───────────────────────────
def test_context_read_threads_as_of_query_param() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["as_of"] = request.url.params.get("as_of")
        return httpx.Response(
            200,
            json={"payload": {"items": [{"kind": "RECOMMENDED", "target": "skill:algebra", "score": 0.5}]}},
        )

    client = _client(handler)
    items = client.context("learner-ada-1", "recos", as_of="2026-10-21T09:00:00.000Z")
    assert captured["as_of"] == "2026-10-21T09:00:00.000Z"
    assert items == [("skill:algebra", 0.5)]

    captured.clear()
    client.context("learner-ada-1", "recos")
    assert captured["as_of"] is None  # default now-read stays param-free


# ── learner evidence card ────────────────────────────────────────────────────
def test_learner_card_is_honest_bounded_and_dumps_no_events() -> None:
    card = learner_card(
        "learner-ada-1",
        "run-abc-tutor-dawn",
        baseline_top="skill:algebra",
        midstory_top="skill:algebra",
        midstory_as_of="2026-10-21T09:00:00.000Z",
        after_top="skill:graph-theory",
        loop={"attached": 4, "unattached": 0},
    )
    assert "learner-ada-1" in card and "run-abc-tutor-dawn" in card
    assert "skill:algebra" in card           # what they saw first
    assert "skill:graph-theory" in card      # what the loop promoted
    assert "2026-10-21T09:00:00.000Z" in card  # the mid-story instant, named
    assert "attached=4" in card              # loop truth, not vibes
    assert len(card) < 2_000                 # bounded by construction
    assert "itf." not in card                # raw signal types never dumped


def test_run_gate_flags_a_midstory_showing_postloop_order() -> None:
    baseline = {"dawn": "skill:algebra", "dusk": "skill:algebra"}
    # sane: mid-story equals baseline even after the loop re-ordered 'after'
    assert run_gate(baseline, {"dawn": "skill:algebra", "dusk": "skill:algebra"}) == []
    # broken memory semantics: the historical slice shows the post-loop order
    reasons = run_gate(baseline, {"dawn": "skill:graph-theory", "dusk": "skill:algebra"})
    assert len(reasons) == 1 and "dawn" in reasons[0]
