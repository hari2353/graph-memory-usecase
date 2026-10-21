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
) -> Experiment:
    """Build one fresh experiment: skills+recos first, outcomes correlated."""
    cohort_prefix = f"{run_id}-" if cohort_scoped else ""
    cohort_of = {learner: f"{cohort_prefix}{cohort}" for learner, cohort in LEARNERS}

    events: list[dict[str, Any]] = []
    for ms, (learner, _cohort) in enumerate(LEARNERS):
        for skill in SKILLS:
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
    for ms, (learner, _cohort) in enumerate(LEARNERS):
        for skill in SKILLS:
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
                        "reco_class": RECO_CLASSES[skill],
                    },
                    at_ms=base_ms + 10 + ms * 2,
                )
            )

    attached: list[dict[str, Any]] = []
    for ms, (learner, _cohort) in enumerate(LEARNERS):
        cohort = cohort_of[learner]
        for skill in SKILLS:
            verdict = VERDICTS[cohort.removeprefix(cohort_prefix)][skill]
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
