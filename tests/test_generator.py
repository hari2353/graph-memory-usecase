"""The signal story is deterministic and shaped like a real app's bus traffic."""

from __future__ import annotations

import pytest

from tutor_demo.generator import (
    INGEST_TYPE_OUTCOME,
    INGEST_TYPE_RECO,
    INGEST_TYPE_SKILL,
    LEARNERS,
    SKILLS,
    Experiment,
    make_experiment,
)


def test_experiment_events_are_deterministic_and_well_shaped() -> None:
    first = make_experiment("run-abc123", base_ms=0)
    second = make_experiment("run-abc123", base_ms=0)
    assert first.events == second.events  # no clock, no randomness inside
    assert first.attached == second.attached

    # 4 learners x (2 skills + 2 recos), then 4 learners x 2 outcomes
    assert len(first.events) == len(LEARNERS) * (len(SKILLS) * 2)
    assert len(first.attached) == len(LEARNERS) * len(SKILLS)
    assert first.delivered_count() == len(LEARNERS) * len(SKILLS)

    types = {event["type"] for event in first.events}
    assert types == {INGEST_TYPE_SKILL, INGEST_TYPE_RECO}

    for event in first.events:
        assert event["specversion"] == "1.0"
        assert event["time"].endswith("Z")
        assert event["partitionkey"] in {learner for learner, _cohort in LEARNERS}
        assert isinstance(event["data"], dict)


def test_outcomes_correlate_to_the_exact_reco_ids() -> None:
    experiment = make_experiment("run-corr9")
    delivered = set(experiment.reco_id.values())
    for outcome in experiment.attached:
        assert outcome["type"] == INGEST_TYPE_OUTCOME
        assert outcome["data"]["correlation_id"] in delivered
        assert outcome["data"]["verdict"] in {"acted_on", "dismissed"}
        # mirrored verdicts: acted on one skill, dismissed the other
        assert outcome["data"]["verdict"] != ""


def test_cohorts_are_run_scoped_so_reruns_never_collide() -> None:
    def cohorts(experiment: Experiment) -> set[str]:
        return {
            str(event["data"]["cohort"])
            for event in experiment.events
            if event["type"] == INGEST_TYPE_RECO
        }

    one, two = cohorts(make_experiment("run-111")), cohorts(make_experiment("run-222"))
    # same segments inside a run; disjoint ids across runs (fresh experiments)
    assert one == {"run-111-tutor-dawn", "run-111-tutor-dusk"}
    assert two == {"run-222-tutor-dawn", "run-222-tutor-dusk"}
    assert not (one & two)


def test_verdict_posture_is_mirrored_across_cohorts() -> None:
    experiment = make_experiment("run-m")
    lookup = {
        outcome["data"]["correlation_id"]: outcome["data"]["verdict"]
        for outcome in experiment.attached
    }
    (dawn_learner, _dawn_cohort), _ada2, (dusk_learner, _dusk_cohort), _bao2 = LEARNERS
    algebra, graph = SKILLS
    # one cohort acts on their gap reco, the mirror cohort dismisses it
    assert lookup[experiment.reco_id[(dawn_learner, algebra)]] == "acted_on"
    assert lookup[experiment.reco_id[(dusk_learner, algebra)]] == "dismissed"
    assert lookup[experiment.reco_id[(dawn_learner, graph)]] == "dismissed"
    assert lookup[experiment.reco_id[(dusk_learner, graph)]] == "acted_on"


@pytest.mark.parametrize("cohort_scoped", [True, False])
def test_cohort_scoping_flag(cohort_scoped: bool) -> None:
    experiment = make_experiment("run-f", cohort_scoped=cohort_scoped)
    recos = [event for event in experiment.events if event["type"] == INGEST_TYPE_RECO]
    prefixes = {str(event["data"]["cohort"]).startswith("run-f-") for event in recos}
    assert prefixes == {cohort_scoped}
