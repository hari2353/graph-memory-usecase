"""A2A conversation arc live gate: the coach improves AFTER the loop.

Same two-tier posture as the other live gates: only runs with ``-m live``
and skips (with the posture hint) when the deployed stack is missing. One
entry point runs the 8-phase arc against real infra:

    story ingest -> flat baseline -> A2A round 1 -> A2A outcome turns ->
    platform loop run -> A2A round 2 -> asserts the conversation improved

``conversation.main()`` exits 0 only when the coach's round-2 narrative
differs from round 1, the cohorts' tops diverged, and both promoted tops
exceed the baseline weight — the conversation demonstrably changed because
the platform's loop carried ledger truth into it.
"""

from __future__ import annotations

import pytest

from tutor_demo import conversation, deployed


@pytest.mark.live
def test_a2a_conversation_improves_after_the_loop() -> None:
    try:
        deployed._platform_dir()
        deployed._require_clickhouse()
    except SystemExit as exc:
        pytest.skip(f"deployed platform not available: {exc}")

    assert conversation.main() == 0
