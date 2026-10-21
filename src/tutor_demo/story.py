"""The tutor app's story material — invented by an LLM when one is wired up.

The platform's effectiveness loop only needs CONTRACT data (two cohorts with
mirrored verdict postures, two skills labelled gap/explore); the story itself
— what learners study, what cohorts are called, how the coach talks — is
legitimately made up. When ``GMP_TUTOR_LLM_BASE_URL`` (+ ``_MODEL``) points at
an OpenAI-compatible chat endpoint, this module asks one to invent the story.
Otherwise it says so and falls back to the deterministic story, and every
runner prints which one it used. AI-invented or not, the *shape* the platform
expects is enforced here, not trusted from the LLM.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx

from .generator import LEARNERS, SKILLS, Experiment, make_experiment

LLM_BASE_URL: Final = os.environ.get("GMP_TUTOR_LLM_BASE_URL", "")
LLM_API_KEY: Final = os.environ.get("GMP_TUTOR_LLM_API_KEY", "")
LLM_MODEL: Final = os.environ.get("GMP_TUTOR_LLM_MODEL", "gpt-4o-mini")

_SCRIPTED: Final = "scripted (no LLM configured)"
MIN_STORY_TERMS: Final = 2


def llm_chat(system: str, user: str) -> str | None:
    """One OpenAI-compatible chat completion; None means explainable fallback."""
    if not LLM_BASE_URL:
        return None
    try:
        response = httpx.post(
            f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": "Bearer dev-noauth"},
            json={
                "model": LLM_MODEL,
                "temperature": 0.4,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
        print(f"      (llm attempt failed: {type(exc).__name__}; falling back)")
        return None


@dataclass(frozen=True)
class StorySpec:
    """What an LLM (or the fallback) invented for this run."""

    skills: tuple[str, str]
    cohorts: tuple[str, str]
    motto: str
    lines: dict[tuple[str, str], str]
    origin: str

    def label(self) -> str:
        """Provenance tag printed by every runner."""
        return self.origin


@dataclass(frozen=True)
class Story:
    """The run's spec + the contract-shaped experiment built from it."""

    spec: StorySpec
    experiment: Experiment

    @property
    def skills(self) -> tuple[str, str]:
        return self.spec.skills

    def cohort_of(self, learner: str) -> str:
        return (
            self.spec.cohorts[0]
            if dict(LEARNERS)[learner] == "tutor-dawn"
            else self.spec.cohorts[1]
        )

    def verdict(self, learner: str, skill: str) -> str:
        """Authored posture mirrored per cohort slot (the app's truth)."""
        slots = dict(LEARNERS)
        acted = 0 if slots[learner] == "tutor-dawn" else 1
        return "acted_on" if skill == self.skills[acted] else "dismissed"

    def line(self, learner: str, skill: str) -> str:
        """The coaching line for this (cohort, skill), defaulted if unwritten."""
        pair = (self.cohort_of(learner), skill)
        return self.spec.lines.get(pair, f"start with reps on {skill} this week")


_STORY_SCHEMA: Final = (
    "Return ONLY compact JSON: {"
    '"skills": ["<concept to revise>", "<stretch concept>"], '
    '"cohorts": ["<study-group name>", "<another>"], '
    '"motto": "<one short sentence>", '
    '"lines": {"<cohort>|<skill>": "<one coaching sentence>"} '
    "} - 2 skills, 2 cohorts, 4 lines. Human, specific, brief. "
    "ASCII text only."
)


def _ascii(text: str) -> str:
    """Windows consoles are cp1252 here; LLM prose passes through sanitized."""
    return text.encode("ascii", "replace").decode("ascii")


def _parse_spec(text: str) -> StorySpec | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data: Any = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    skills, cohorts, lines_raw = data.get("skills"), data.get("cohorts"), data.get("lines") or {}
    if not _two_strings(skills) or not _two_strings(cohorts) or not isinstance(lines_raw, dict):
        return None
    lines: dict[tuple[str, str], str] = {}
    for key, value in lines_raw.items():
        if isinstance(key, str) and isinstance(value, str) and "|" in key:
            cohort, skill = (part.strip() for part in key.split("|", 1))
            lines[(_ascii(cohort), _ascii(skill))] = _ascii(value)
    for cohort in cohorts:
        for skill in skills:
            lines.setdefault((cohort, skill), f"start with reps on {skill}")
    return StorySpec(
        skills=(_ascii(skills[0]), _ascii(skills[1])),
        cohorts=(_ascii(cohorts[0]), _ascii(cohorts[1])),
        motto=_ascii(str(data.get("motto", ""))),
        lines=lines,
        origin=f"llm:{LLM_MODEL}",
    )


def _two_strings(candidate: Any) -> bool:
    """True when a decoded JSON list is exactly two non-empty strings."""
    return (
        isinstance(candidate, list)
        and len(candidate) == MIN_STORY_TERMS
        and all(isinstance(item, str) and item for item in candidate)
    )


def scripted_story() -> StorySpec:
    """The deterministic story (also the offline/test story)."""
    return StorySpec(
        skills=(SKILLS[0], SKILLS[1]),
        cohorts=("tutor-dawn", "tutor-dusk"),
        motto="two cohorts, one revision plan",
        lines={
            ("tutor-dawn", "algebra"): "your worked examples landed; formalize them",
            ("tutor-dawn", "graph-theory"): "worth a stretch once the algebra clicks",
            ("tutor-dusk", "algebra"): "keep as a drill, don't lead with it",
            ("tutor-dusk", "graph-theory"): "your explorations fit here naturally",
        },
        origin=_SCRIPTED,
    )


def llm_story() -> StorySpec:
    """Ask the configured LLM for a story; fall back with a printed reason."""
    reply = llm_chat(system=_STORY_SCHEMA, user="Invent a two-skill revision story.")
    if reply is None:
        return scripted_story()
    parsed = _parse_spec(reply)
    if parsed is None:
        print("      (llm story unusable; falling back)")
        return scripted_story()
    return parsed


def make_story(run_id: str | None = None) -> Story:
    """One story per run: spec + experiment (contract-shaped either way)."""
    key = run_id or uuid.uuid4().hex[:8]
    spec = llm_story()
    experiment = make_experiment(key, skills=spec.skills, cohort_labels=spec.cohorts)
    return Story(spec=spec, experiment=experiment)


def story_source_line(story: Story) -> str:
    """The provenance banner every runner prints first."""
    subject = " / ".join(story.skills)
    groups = " / ".join(story.spec.cohorts)
    banner = f"story[{story.spec.label()}]: skills {subject}; cohorts {groups}"
    return f"{banner}; motto: {story.spec.motto}"


__all__ = ["Story", "StorySpec", "llm_chat", "make_story", "story_source_line"]
