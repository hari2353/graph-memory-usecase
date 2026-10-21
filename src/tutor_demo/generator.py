"""Deterministic ITF-shaped signal generation for the tutor app.

Every run generates a fresh, self-consistent experiment: learners in two
app-declared cohorts receive skill assessments + recommendations (RECOnition
signals), and the app then correlates outcomes back to the exact recommender
ids it handed out. Cohort ids are run-scoped (like real experiment cohorts),
so reruns never collide with prior ledger state — the demo is re-runnable.

This is deliberately pure and deterministic (no clock, no IO): the app's
behavior is code, not a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

LEARNERS: tuple[tuple[str, str], ...] = (
    ("learner-ada-1", "tutor-dawn"),
    ("learner-ada-2", "tutor-dawn"),
    ("learner-bao-1", "tutor-dusk"),
    ("learner-bao-2", "tutor-dusk"),
)
"""(learner, cohort) pairs. Two cohorts; the app owns the segmentation."""

SKILLS: tuple[str, ...] = ("algebra", "graph-theory")

RECO_CLASSES: dict[str, str] = {"algebra": "gap", "graph-theory": "explore"}
"""The tutor's recommender tags, mirroring a real production schema.

Verdict posture, per cohort (the app's a/b experiment):
  dawn acts on their gap reco and dismisses the explore one;
  dusk is the mirror image. The cohort verdicts are the app's truth.
"""

VERDICTS: dict[str, dict[str, str]] = {
    "tutor-dawn": {"algebra": "acted_on", "graph-theory": "dismissed"},
    "tutor-dusk": {"algebra": "dismissed", "graph-theory": "acted_on"},
}

INGEST_TYPE_SKILL = "itf.skill_assessed.v1"
INGEST_TYPE_RECO = "itf.reco.v1"
INGEST_TYPE_OUTCOME = "itf.reco_outcome.v1"


@dataclass(frozen=True)
class Experiment:
    """One run's signal story, plus the correlation map the app tracks."""

    run_id: str
    cohort_prefix: str
    events: tuple[dict[str, Any], ...]
    """In order: all skill assessments, then all recommendations."""
    reco_id: dict[tuple[str, str], str]
    """(learner, skill) -> recommender event id."""
    attached: tuple[dict[str, Any], ...]
    """Outcome events (correlated by `data.correlation_id` -> reco id)."""

    def delivered_count(self) -> int:
        return len(self.reco_id)


SOURCE: str = "https://tutor-app.example/signals"


def envelope(
    event_id: str,
    type_: str,
    user: str,
    data: dict[str, Any],
    *,
    at_ms: int,
) -> dict[str, Any]:
    """One CloudEvents-ish envelope; `at_ms` offsets the wall clock."""
    base = datetime(2026, 10, 21, 9, 0, tzinfo=UTC) + timedelta(milliseconds=at_ms)
    stamp = base.strftime("%Y-%m-%dT%H:%M:%S.") + f"{base.microsecond // 1000:03d}Z"
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": SOURCE,
        "type": type_,
        "time": stamp,
        "partitionkey": user,
        "data": data,
    }


def make_experiment(
    run_id: str,
    *,
    cohort_scoped: bool = True,
    base_ms: int = 0,
    skills: tuple[str, ...] = SKILLS,
    cohort_labels: tuple[str, str] = ("tutor-dawn", "tutor-dusk"),
) -> Experiment:
    """Build one fresh experiment: skills+recos first, outcomes correlated.

    Two structural invariants are fixed whatever the story says (the loop's
    math needs them): two skills — the first is the "gap" class, the second
    the "explore" class — and mirrored verdict postures per cohort slot
    (LEARNERS order). Names are story material: the app hands the defaults,
    or the names an AI invented for this run.
    """
    reco_class_of = dict(zip(skills, ("gap", "explore"), strict=False))
    cohort_prefix = f"{run_id}-" if cohort_scoped else ""
    dawn_label, dusk_label = cohort_labels
    cohort_of = {
        learner: f"{cohort_prefix}{dawn_label if slot == 'tutor-dawn' else dusk_label}"
        for learner, slot in LEARNERS
    }

    events: list[dict[str, Any]] = []
    for ms, (learner, _slot) in enumerate(LEARNERS):
        for skill in skills:
            events.append(
                envelope(
                    f"skill-{run_id}-{learner}-{skill}",
                    INGEST_TYPE_SKILL,
                    learner,
                    {"skill": skill},
                    at_ms=base_ms + ms * 2,
                )
            )
    reco_id: dict[tuple[str, str], str] = {}
    for ms, (learner, _slot) in enumerate(LEARNERS):
        for skill in skills:
            eid = f"reco-{run_id}-{learner}-{skill}"
            reco_id[(learner, skill)] = eid
            events.append(
                envelope(
                    eid,
                    INGEST_TYPE_RECO,
                    learner,
                    {
                        "skill": skill,
                        "cohort": cohort_of[learner],
                        "reco_class": reco_class_of[skill],
                    },
                    at_ms=base_ms + 10 + ms * 2,
                )
            )

    attached: list[dict[str, Any]] = []
    for ms, (learner, slot) in enumerate(LEARNERS):
        for index, skill in enumerate(skills):
            dawn_acts = index == 0 and slot == "tutor-dawn"
            dusk_acts = index == 1 and slot == "tutor-dusk"
            verdict = "acted_on" if dawn_acts or dusk_acts else "dismissed"
            attached.append(
                envelope(
                    f"outcome-{run_id}-{learner}-{skill}",
                    INGEST_TYPE_OUTCOME,
                    learner,
                    {
                        "correlation_id": reco_id[(learner, skill)],
                        "verdict": verdict,
                    },
                    at_ms=base_ms + 20 + ms * 2,
                )
            )

    return Experiment(
        run_id=run_id,
        cohort_prefix=cohort_prefix,
        events=tuple(events),
        reco_id=reco_id,
        attached=tuple(attached),
    )


__all__ = [
    "INGEST_TYPE_OUTCOME",
    "INGEST_TYPE_RECO",
    "INGEST_TYPE_SKILL",
    "LEARNERS",
    "RECO_CLASSES",
    "SKILLS",
    "VERDICTS",
    "Experiment",
    "envelope",
    "make_experiment",
]
