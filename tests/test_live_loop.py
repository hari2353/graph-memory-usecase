"""The live gate: the full personalization loop against the deployed platform.

Marked ``live`` — the same posture as the platform's own live tier: it boots
the deployed serve (subprocess), drives the real HTTP channels (ingest, REST,
MCP one-shot), and asserts the story's end state: ingest acked, baseline
flat, outcomes attached by event id, the loop run from ledger truth, and the
cohorts' top recommendations visibly diverged. Skips with a hint when the
platform checkout or ClickHouse is missing.
"""

from __future__ import annotations

import pytest

from tutor_demo import demo, deployed


@pytest.mark.live
def test_full_personalization_loop_lifts_cohort_recommendations() -> None:
    try:
        deployed._platform_dir()
        deployed._require_clickhouse()
    except SystemExit as exc:
        pytest.skip(f"deployed platform not available: {exc}")

    # one entry point == the whole 9-phase story; it exits 0 only when the
    # loop closed and the lift is real, so exit code is the assertion
    assert demo.main() == 0
