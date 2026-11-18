"""Run evidence: per-learner cards and the honest-run gate.

The tutor market sells mastery *evidence*; this module builds it from the
app's own reads, bounded, with the mid-story instant named so a card says
what the learner's plan looked like *then* — not just after the loop.
The gate makes the run fail loudly if the historical slice shows a
post-loop order (broken memory semantics), not just on missing lift.
"""

from __future__ import annotations


def learner_card(
    learner: str,
    cohort: str,
    *,
    baseline_top: str,
    midstory_top: str,
    midstory_as_of: str,
    after_top: str,
    loop: dict[str, int],
) -> str:
    """One learner's before / mid-story / after-loop plan, with loop truth."""
    counters = ", ".join(f"{key}={loop[key]}" for key in sorted(loop))
    return "\n".join(
        [
            f"# learner evidence — {learner} ({cohort})",
            "",
            f"- before the loop (baseline): {baseline_top}",
            f"- mid-story (as_of {midstory_as_of}): {midstory_top}",
            f"- after the loop: {after_top}",
            f"- loop truth: {counters}",
        ]
    )


def run_gate(baseline: dict[str, str], midstory: dict[str, str]) -> list[str]:
    """Failure reasons for broken memory semantics; empty means sound.

    The mid-story (as_of) slice must still show the baseline order for every
    representative learner: a historical read that already shows the
    post-loop order means the platform's point-in-time contract is broken.
    """
    return [
        f"{learner}: mid-story (as_of) read shows {midstory[learner]!r}, "
        f"expected the baseline order {order!r}"
        for learner, order in baseline.items()
        if learner in midstory and midstory[learner] and midstory[learner] != order
    ]


__all__ = ["learner_card", "run_gate"]
