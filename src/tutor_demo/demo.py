"""The tutor app's live drive: prove the platform's personalization loop.

One command runs the whole story against a real deployed platform (dev
posture):

  1. Preconditions     - ClickHouse reachable, platform checkout found
  2. Tenant lifecycle  - `platform tenancy onboard tutor-app-run-<id>`
  3. Serve             - `platform serve` with this app's mapping + tenant token
  4. The app works     - skill assessments + recommendations ingest (real HTTP)
  5. Baseline reads    - every cohort's learners see the same recommendation
  6. Outcomes correlate- learners act / dismiss; outcomes attach by event id
  7. The loop closes   - `platform loop run` ticks the effectiveness from truth
  8. The lift          - cohort recommendations re-order (before/after table)
  9. Ask why           - MCP explain_context + get_effectiveness (provenance)

Week-zero applications fail here with hand-rolled math; the honest measure is
that the app itself owns none of the memory work - emit, read, correlate, loop.

(`whole-loop` is the same story whose signals instead travel Pulsar ->
ClickHouse -> ClickGraph; see wholeloop.py.)
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from .deployed import (
    TENANT_PREFIX,
    _client,
    _free_demo_ports,
    _kill_tree,
    _mapping_path,
    _platform_dir,
    _platform_run,
    _print_table,
    _require_clickhouse,
    _start_serve,
    _tops,
    _wait_for_lift,
    cohort_representatives,
)
from .generator import LEARNERS, Experiment, make_experiment
from .platform_client import PlatformClient


def _baseline(
    client: PlatformClient, experiment: Experiment, learners: tuple[str, ...]
) -> dict[str, str]:
    """Ingest the app's signal story, then read once (the platform projects)."""
    print("[4/9] ingest: skill assessments + recommendations (real signals)")
    ack = client.post_signals(list(experiment.events))
    print(f"      ack: {ack}")
    assert ack["accepted"] == len(experiment.events), "ingest silently dropped events"

    # the graph carries the ingest only after the next projection interval
    # (tick -> replay -> atomic publish); poll instead of assuming the timing
    for attempt in range(45):
        baseline = _tops(client, learners)
        if "" not in set(baseline.values()):
            break
        if attempt == 0:
            print("      waiting for the next projection interval to publish...")
        time.sleep(1.0)
    baseline = _tops(client, learners)
    if len(set(baseline.values())) != 1 or "" in set(baseline.values()):
        _print_table("baseline reads: expected one identical order, got:", baseline)
        raise SystemExit("baseline reads are not cohort-identical; unexpected ledger state")
    _print_table("baseline reads (fresh experiment: identical for every cohort)", baseline)
    return baseline


def _correlate_and_loop(client: PlatformClient, experiment: Experiment, platform_dir: Path) -> None:
    """Emit outcomes correlated by reco id, then let the operator run the loop."""
    print("[5/9] the app correlates outcomes to the reco ids it handed out")
    ack = client.post_signals(list(experiment.attached))
    print(f"      ack: {ack}")
    assert ack["accepted"] == len(experiment.attached), "outcome ingest dropped events"

    print("[6/9] the effectiveness loop ticks from ledger truth")
    report = _platform_run(platform_dir, "loop", "run", "--mapping", str(_mapping_path()))
    print("      " + report.strip().replace("\n", "\n      "))


def _lift(
    client: PlatformClient,
    baseline: dict[str, str],
    dawn_learner: str,
    dusk_learner: str,
    dawn_cohort: str,
    dusk_cohort: str,
) -> None:
    """Re-read, show that the cohorts' orders visibly diverged."""
    print("[7/9] re-reading personalized recos (loop carried the multipliers)")
    after = _wait_for_lift(client, dawn_learner, dusk_learner)
    _print_table("after the loop", after)
    dawn_top, dusk_top = after[dawn_learner], after[dusk_learner]
    # SPEC 16.1's flagship: the same graph serves divergent orders per cohort.
    if dawn_top == dusk_top:
        print(
            "      FAILED: cohorts did not diverge "
            f"(dawn={dawn_top!r}, dusk={dusk_top!r}, baseline={baseline[dawn_learner]!r})"
        )
        raise SystemExit(1)

    print("[8/9] the lift, end to end")
    print(f"      baseline order: {baseline[dawn_learner]!r} (both cohorts)")
    print(f"      {dawn_cohort} -> {dawn_top!r}   (acted_on reco promoted)")
    print(f"      {dusk_cohort} -> {dusk_top!r}   (their acted_on reco promoted)")
    print("      the app wrote none of this logic: emit, read, correlate, loop.")


def _proofs(client: PlatformClient, dawn_learner: str) -> None:
    """Phase 9: MCP provenance + the loop's own report from ledger truth."""
    print("[9/9] why this reco? MCP provenance + the loop's own report")
    why = client.mcp_call("explain_context", {"user": dawn_learner, "capability": "recos"})
    for row in (why.get("observations") or [])[:2]:
        print(
            "      explain_context: {"
            + ", ".join(str(row.get(key)) for key in ("kind", "target", "score", "produced_by"))
            + "}"
        )
    status = client.mcp_call("get_effectiveness", {})
    known = {key: status.get(key) for key in ("tenant", "reco_delivered", "attached", "unattached")}
    print(f"      get_effectiveness: {json.dumps(known)}")


def main() -> int:
    platform_dir = _platform_dir()
    _require_clickhouse()
    _free_demo_ports()
    run_id = uuid.uuid4().hex[:8]
    experiment = make_experiment(f"run-{run_id}")
    learners = tuple(learner for learner, _cohort in LEARNERS)
    dawn_learner, dusk_learner = cohort_representatives(LEARNERS)
    cohort_scope = experiment.cohort_prefix
    dawn_cohort = f"{cohort_scope}tutor-dawn"
    dusk_cohort = f"{cohort_scope}tutor-dusk"
    # each run provisions its own experiment tenant: reruns never inherit a
    # prior run's ledger truth, the platform's multi-tenant isolation is the
    # boundary (and it is honest - one run == one fresh environment)
    tenant = f"{TENANT_PREFIX}-{run_id}"

    print(f"[1/9] platform checkout: {platform_dir}")
    print(f"[2/9] registering this run's tenant ({tenant}) in the durable registry")
    print("      " + _platform_run(platform_dir, "tenancy", "onboard", tenant).strip())

    print("[3/9] booting the deployed platform serve (this app's mapping spec)")
    serve = _start_serve(platform_dir, tenant)
    try:
        client = _client()
        baseline = _baseline(client, experiment, learners)
        _correlate_and_loop(client, experiment, platform_dir)
        _lift(client, baseline, dawn_learner, dusk_learner, dawn_cohort, dusk_cohort)
        _proofs(client, dawn_learner)
        return 0
    finally:
        _kill_tree(serve)


if __name__ == "__main__":
    sys.exit(main())
