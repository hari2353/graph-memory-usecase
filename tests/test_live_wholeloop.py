"""Whole-loop live gate: broker -> ledger -> loop -> Cypher on the deployed stack.

The same two-tier posture as the platform: this tier only runs with
``-m live`` and skips (with the posture hint) when the dev stack is not up.
One entry point runs the 10-phase whole-loop story against real infra:

    signals -> Pulsar -> platform drain-brokers -> ClickHouse ledger ->
    serve projection -> REST reads -> loop run -> ClickGraph Cypher reads

It exits 0 only when baseline reads are flat, the loop ran from ledger
truth, and ClickGraph's Cypher reads match the REST reads per cohort.
"""

from __future__ import annotations

import pytest

from tutor_demo import deployed, wholeloop
from tutor_demo.broker import require_pulsar


@pytest.mark.live
def test_whole_loop_pulsar_to_ledger_to_clickgraph() -> None:
    try:  # platform checkout + ClickHouse first (the serve tier)
        deployed._platform_dir()
        deployed._require_clickhouse()
    except SystemExit as exc:
        pytest.skip(f"deployed platform not available: {exc}")

    try:  # the broker tier (dev compose posture)
        require_pulsar()
    except SystemExit as exc:
        pytest.skip(f"pulsar dev broker not available: {exc}")

    if not deployed.CLICKGRAPH_BINARY.exists():
        pytest.skip(f"clickgraph binary not present: {deployed.CLICKGRAPH_BINARY}")

    # one entry point == the whole 10-phase story; exit 0 only when the
    # broker trail closed AND the Cypher reads matched the REST reads
    assert wholeloop.main() == 0
