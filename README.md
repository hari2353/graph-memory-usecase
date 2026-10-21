graph-memory-usecase
====================

A **real (small) application** in front of [graph-memory-platform][platform]:
a personalization loop with learners, cohorts and outcomes — the proof that
the platform's memory engineering actually changes what users see, driven
end-to-end over the deployed HTTP channels.

[platform]: https://github.com/hari2353/graph-memory-platform

What it proves
--------------

Run `uv run demo` and the app executes this story against a deployed
`platform serve` (its own tenant, its own mapping, zero platform code
imported):

| Phase | The app does | The platform does |
|---|---|---|
| 1–2 | registers itself | `platform tenancy onboard tutor-app-run-<id>` (durable registry row) |
| 3 | — | `platform serve` boots with `tutor_demo/mapping.yaml` |
| 4 | ingests skill assessments + recommendations (real envelopes) | ledger (idempotent, DLQ-backed) |
| 5 | reads recommendations | REST `/v1/context/{learner}/recos` — **identical order for every cohort** |
| 6 | emits outcomes correlated by the exact reco event ids | outcomes ride the ledger, not the graph |
| 7 | — | `platform loop run`: effectiveness tick → `(kind, target, cohort)` weight rows |
| 8 | reads again | **cohort orders diverge** — each cohort's acted-on reco is promoted |
| 9 | asks *why* | MCP `explain_context` (provenance) + `get_effectiveness` (loop report) |

The last printouts are the money shots:

```
[8/9] the lift, end to end
      baseline order: 'skill:algebra' (both cohorts)
      run-55c4dfe6-tutor-dawn -> 'skill:algebra'        (acted_on reco promoted)
      run-55c4dfe6-tutor-dusk -> 'skill:graph-theory'   (their acted_on reco promoted)
      the app wrote none of this logic: emit, read, correlate, loop.

[9/9] why this reco? MCP provenance + the loop's own report
      explain_context: {RECOMMENDED, skill:algebra, 0.625, ['itf.reco.v1']}
      explain_context: {RECOMMENDED, skill:graph-theory, 0.375, ['itf.reco.v1']}
      get_effectiveness: {"tenant": "tutor-app-run-55c4dfe6", "reco_delivered": 8, "attached": 8, "unattached": 0}
```

Same graph, same events-emitting app, no hand-rolled ranking in the
application: the difference is produced entirely by the platform's
effectiveness loop reading ledger truth — the provenance rows even carry the
tick's multipliers (0.625 = 0.5 × 1.25 acted-on, 0.375 = 0.5 × 0.75
dismissed). Exit code is non-zero if the lift doesn't happen, so this doubles
as a live regression gate for the platform.

Quickstart
----------

Prerequisites: [uv][uv], Docker (for ClickHouse), and a checkout of
`graph-memory-platform` next to this repo (or `GMP_DEMO_PLATFORM_DIR`).

[uv]: https://docs.astral.sh/uv/

```bash
# 1. dev infra (one time)
docker compose -f ../graph-memory-platform/ops/compose/clickhouse.yaml up -d

# 2. the whole loop
uv run demo
```

The demo boots its own `platform serve` on ports **8131/8132/8133**, provisions
this run's experiment tenant, runs the loop above, then stops the serve.
Artifact rows persist in ClickHouse — inspect them from the platform repo:

```bash
cd ../graph-memory-platform
uv run platform tenancy list
uv run platform spans --limit 5     # per-request spans the app's reads produced
uv run platform metering --limit 5  # the app's request ticks
```

Offline tests don't need any of that:

```bash
uv run pytest -m "not live"  # offline tier: generator determinism + client contract
uv run pytest -m live        # live tier: the full 9-phase loop vs the deployed platform
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
```

The same two-tier process the platform uses: offline always; `-m live` as the
release gate (a plain `uv run pytest` also runs the live tier and skips it
cleanly when the platform isn't up).

Design notes
------------

- **The app is a third-party consumer.** It imports no platform code; every
  platform interaction goes over the deployed channels (ingest, REST, MCP),
  exactly like a production integration would. The only dependencies are
  `httpx` and the platform's *runtime*.
- **The mapping is the contract.** `tutor_demo/mapping.yaml` declares how the
  app's signals project into the graph — including which outcomes are loop
  truth (`itf.reco_outcome.v1` never touches the graph) and how correlation
  (`data.correlation_id`), cohorts (`data.cohort`), classes and verdicts map.
  It mirrors the platform's proven loop spec.
- **Run-scoped experiment tenants and cohorts.** Cohort ids embed the run id
  (`run-<id>-tutor-dawn`) and the run provisions its own tenant
  (`tutor-app-run-<id>`) — the same posture as a fresh experiment
  environment. Reruns are clean in the strongest sense: they inherit no prior
  ledger truth, the baseline is honestly flat, and the lift is attributable.
  Multi-tenant isolation is the platform's job; the demo leans on it instead
  of fighting history.
- **Deterministic signal generation** (`generator.py` has no clock/IO), so
  the story is testable offline and the live run is reproducible.

Environment knobs
-----------------

| Variable | Meaning | Default |
|---|---|---|
| `GMP_DEMO_PLATFORM_DIR` | platform checkout to run | sibling checkout |
| `GMP_DEMO_INGEST_PORT` / `_REST_PORT` / `_MCP_PORT` | serve ports | 8131 / 8132 / 8133 |
| `GMP_CLICKHOUSE_HOST` / `GMP_CLICKHOUSE_PORT` | dev ClickHouse probe | 127.0.0.1 / 8124 |

License
-------

Apache-2.0 — see [LICENSE](LICENSE).
