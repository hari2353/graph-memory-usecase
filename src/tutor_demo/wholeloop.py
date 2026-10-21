"""The tutor app's whole-loop drive: the OTHER ingest trail end to end.

Same story, same personalization lift — but the signals take the broker
route instead of HTTP, and the reads come back through the Cypher serving
tier, not just REST/MCP:

  1. Preconditions       - ClickHouse + Pulsar + the ClickGraph binary
  2. Tenant lifecycle    - run-scoped tenant in the durable registry
  3. Serve               - deployed platform serve (this app's mapping)
  4. Pulsar              - story signals published via the broker (b64 codec)
  5. Drain               - platform drain-brokers: broker -> ClickHouse ledger
  6. Baseline reads      - recos visible (REST) and cohort-identical (flat)
  7. Outcomes correlate  - verdicts through the broker too; attach by reco id
  8. The loop closes     - platform loop run (apply_effectives rewrote weights)
  9. ClickGraph serves   - Cypher over gm_*: weights + divergence match REST
 10. MCP proofs          - explain_context + get_effectiveness (provenance)

The honest claim this proves: one ingest truth, three delivery channels
(HTTP/MCP daily; broker + Cypher here) — the app never reimplements memory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx

from .broker import PULSAR_ADMIN_URL, produce_envelopes, require_pulsar, topic_for_tenant
from .deployed import (
    CLICKGRAPH_BINARY,
    CLICKGRAPH_PORT,
    CLICKGRAPH_URL,
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_USER,
    EDGES_TABLE,
    NODES_TABLE,
    _client,
    _free_demo_ports,
    _free_ports,
    _kill_tree,
    _mapping_path,
    _platform_dir,
    _platform_run,
    _print_table,
    _require_clickhouse,
    _start_serve,
    _tops,
    clickhouse_endpoint,
    cohort_representatives,
    new_run_tenant,
)
from .generator import LEARNERS, Experiment, make_experiment
from .platform_client import PlatformClient

_SCHEMA_NAME = "tutor_loop_serve"

_OUTCOME_ROW = re.compile(
    r"tenant (?P<tenant>\S+): accepted=(?P<accepted>\d+) duplicates=(?P<dups>\d+) "
    r"rejected=(?P<rejected>\d+)"
)


def _drain(platform_dir: Path, tenant: str) -> dict[str, int]:
    """Run the platform's drain command; return our tenant's batch outcome."""
    report = _platform_run(platform_dir, "drain-brokers")
    print("      " + report.strip().replace("\n", "\n      "))
    ours = [
        match.groupdict()
        for match in _OUTCOME_ROW.finditer(report)
        if match.group("tenant") == tenant
    ]
    if not ours:
        raise SystemExit(f"drain reported no batch for tenant {tenant!r}:\n{report[-800:]}")
    row = ours[-1]
    counts = {key: int(value) for key, value in row.items() if key != "tenant"}
    if counts["rejected"]:
        raise SystemExit(f"drain rejected {counts['rejected']} signal(s) for {tenant}")
    return counts


def _graph_post(path: str, body: dict[str, object]) -> str:
    request = urllib.request.Request(
        f"{CLICKGRAPH_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        if isinstance(raw, bytes):
            return raw.decode()
        return str(raw)


def _cypher(query: str) -> list[dict[str, object]]:
    """One Cypher query against the booted ClickGraph serving tier."""
    payload = json.loads(_graph_post("/query", {"query": query, "schema_name": _SCHEMA_NAME}))
    results: list[dict[str, object]] = payload.get("results") or []
    return results


def _serve_schema_yaml(database: str, nodes_table: str, edges_table: str) -> str:
    """Cypher serving schema over the ledger + its read-model views.

    The projector writes only user nodes (``labels=("user",)``), so the
    skill endpoint set is served from a view over the ledger's edge
    targets; edges read through the dedup view so Cypher cardinality
    never depends on merge timing.
    """
    serving = f"{edges_table}_serving"
    skills = f"{edges_table}_skills"
    return f"""name: {_SCHEMA_NAME}
graph_schema:
  nodes:
    - label: user
      database: {database}
      table: {nodes_table}
      node_id: key
      property_mappings:
        key: key
        tenant_id: tenant_id
      filter: "labels[1] = 'user'"
    - label: skill
      database: {database}
      table: {skills}
      node_id: key
      property_mappings:
        key: key
        tenant_id: tenant_id
      filter: "labels[1] = 'skill'"
  edges:
    - type: HAS_SKILL
      database: {database}
      table: {serving}
      from_id: user_key
      to_id: target
      from_node: user
      to_node: skill
      property_mappings:
        tenant_id: tenant_id
        weight: weight
      filter: "kind = 'HAS_SKILL'"
    - type: RECOMMENDED
      database: {database}
      table: {serving}
      from_id: user_key
      to_id: target
      from_node: user
      to_node: skill
      property_mappings:
        tenant_id: tenant_id
        weight: weight
      filter: "kind = 'RECOMMENDED'"
"""


def _ensure_serving_views() -> None:
    """Create (or refresh) the serving-tier read-model views over the ledger."""
    for statement in (
        f"CREATE OR REPLACE VIEW {EDGES_TABLE}_serving AS SELECT * FROM {EDGES_TABLE} FINAL",
        f"CREATE OR REPLACE VIEW {EDGES_TABLE}_skills AS "
        "SELECT DISTINCT tenant_id, target AS key, ['skill'] AS labels, "
        "'' AS props_json FROM " + f"{EDGES_TABLE}_serving",
    ):
        response = httpx.post(
            f"{_clickhouse_api_url()}/",
            params={
                "database": CLICKHOUSE_DATABASE,
                "user": CLICKHOUSE_USER,
                "password": CLICKHOUSE_PASSWORD,
            },
            content=statement,
            timeout=30.0,
        )
        response.raise_for_status()


def _clickhouse_api_url() -> str:
    host, port = clickhouse_endpoint()
    return f"http://{host}:{port}"


def _start_clickgraph() -> subprocess.Popen[bytes]:
    if not CLICKGRAPH_BINARY.exists():
        raise SystemExit(
            f"ClickGraph binary not found: {CLICKGRAPH_BINARY} (set GMP_DEMO_CG_BINARY)"
        )
    env = dict(os.environ)
    env.update(
        CLICKHOUSE_URL=_clickhouse_api_url(),
        CLICKHOUSE_USER=CLICKHOUSE_USER,
        CLICKHOUSE_PASSWORD=CLICKHOUSE_PASSWORD,
        RUST_LOG="warn",
    )
    process = subprocess.Popen(
        [
            str(CLICKGRAPH_BINARY),
            "--http-host",
            "127.0.0.1",
            "--http-port",
            str(CLICKGRAPH_PORT),
            "--disable-bolt",
            "--log-level",
            "warn",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("clickgraph exited during boot")
        try:
            urllib.request.urlopen(f"{CLICKGRAPH_URL}/health", timeout=2).read()
            return process
        except OSError:
            time.sleep(0.3)
    process.terminate()
    raise SystemExit("clickgraph did not become healthy within 30s")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        process.terminate()


def _row_weight(row: dict[str, object]) -> float:
    raw = row.get("weight", 0.0)
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _cypher_reco_edges(tenant: str, learner: str) -> list[tuple[str, float]]:
    """RECOMMENDED edges straight from the graph, strongest first.

    ClickGraph resolves node joins by key *value*, so the tenant property
    MUST sit on every matched variable (user, rel, skill) or rows from
    other tenants' identically-keyed symbols leak into the result.
    """
    query = (
        f"MATCH (u:user {{tenant_id: '{tenant}'}})-[r:RECOMMENDED {{tenant_id: '{tenant}'}}]"
        f"->(s:skill {{tenant_id: '{tenant}'}}) WHERE u.key = '{learner}' "
        "RETURN s.key AS skill, r.weight AS weight ORDER BY weight DESC"
    )
    rows = _cypher(query)
    pairs = [(str(row.get("skill")), _row_weight(row)) for row in rows]
    return sorted(pairs, key=lambda entry: -entry[1])


def _phase_publish(platform_dir: Path, tenant: str, experiment: Experiment) -> None:
    """[4-5] publish through the broker, drain into the ledger."""
    print("[4/10] the app publishes its story through the broker (Pulsar)")
    count = produce_envelopes(tenant, list(experiment.events))
    print(f"      broker ack: {count} envelope(s) on {topic_for_tenant(tenant)}")

    print("[5/10] drain: broker -> the ClickHouse ledger (one ingest truth)")
    counts = _drain(platform_dir, tenant)
    assert counts["accepted"] == len(experiment.events), "drain silently dropped events"


def _phase_baseline(client: PlatformClient, learners: tuple[str, ...]) -> dict[str, str]:
    """[6] poll reads until the drained signals surface; assert flat order."""
    print("[6/10] baseline reads (the serve keeps projecting continuously)")
    for attempt in range(60):
        tops = _tops(client, learners)
        if "" not in set(tops.values()):
            break
        if attempt == 0:
            print("       waiting for the projection to surface drained signals...")
        time.sleep(1.0)
    baseline = _tops(client, learners)
    if len(set(baseline.values())) != 1 or "" in set(baseline.values()):
        _print_table("baseline reads: expected one identical order, got:", baseline)
        raise SystemExit("baseline reads are not cohort-identical; unexpected ledger state")
    _print_table("baseline reads (fresh experiment: identical for every cohort)", baseline)
    return baseline


def _phase_outcomes(platform_dir: Path, tenant: str, experiment: Experiment) -> None:
    """[7] verdicts through the broker too; attach by reco id."""
    print("[7/10] outcome verdicts flow through the broker too; attach by reco id")
    outcome_count = produce_envelopes(tenant, list(experiment.attached))
    print(f"       broker ack: {outcome_count} outcome envelope(s)")
    outcome_counts = _drain(platform_dir, tenant)
    assert outcome_counts["accepted"] == len(experiment.attached), "outcome drain dropped"


def _phase_loop(platform_dir: Path) -> None:
    """[8] the effectiveness loop ticks from ledger truth."""
    print("[8/10] the effectiveness loop ticks from ledger truth")
    report = _platform_run(platform_dir, "loop", "run", "--mapping", str(_mapping_path()))
    print("      " + report.strip().replace("\n", "\n      "))


def _phase_lift_rest(
    client: PlatformClient, dawn_learner: str, dusk_learner: str
) -> tuple[str, str]:
    """[8b] re-read the lift; the loop carried the multipliers into the graph."""
    print("[8b/10] re-reading the lift (loop-carried multipliers, REST tier)")
    for _ in range(30):
        if client.recos(dawn_learner) and client.recos(dusk_learner):
            break
        time.sleep(1.0)
    tops_after = {
        learner: (items[0][0] if items else "")
        for learner, items in ((learner, client.recos(learner)) for learner in LEARNERS_NAMES)
    }
    _print_table("REST reads after the loop", tops_after)
    dawn_top, dusk_top = tops_after[dawn_learner], tops_after[dusk_learner]
    if dawn_top == dusk_top:
        print(f"      FAILED: cohorts did not diverge (dawn={dawn_top!r}, dusk={dusk_top!r})")
        raise SystemExit(1)
    return dawn_top, dusk_top


LEARNERS_NAMES: tuple[str, ...] = tuple(learner for learner, _cohort in LEARNERS)


def _phase_clickgraph(
    tenant: str,
    baseline: dict[str, str],
    dawn_learner: str,
    dusk_learner: str,
    dawn_top_rest: str,
    dusk_top_rest: str,
) -> None:
    """[9] the serving tier reads the same divergence, straight Cypher."""
    print("[9/10] booting ClickGraph (serving tier); the SAME lift, read via Cypher")
    cg = _start_clickgraph()
    try:
        _ensure_serving_views()
        print(f"      serving views: {EDGES_TABLE}_serving (+ skills view over its targets)")
        response = _graph_post(
            "/schemas/load",
            {
                "schema_name": _SCHEMA_NAME,
                "config_content": _serve_schema_yaml(CLICKHOUSE_DATABASE, NODES_TABLE, EDGES_TABLE),
            },
        )
        print(f"      schema load: {response.strip()[:160]}")
        edges = {
            learner: _cypher_reco_edges(tenant, learner) for learner in (dawn_learner, dusk_learner)
        }
        for target in (dawn_learner, dusk_learner):
            print(f"      cypher RECOMMENDED edges for {target}: {edges[target]}")

        dawn_top, dawn_weight = edges[dawn_learner][0]
        dusk_top, dusk_weight = edges[dusk_learner][0]
        if dawn_top != dawn_top_rest or dusk_top != dusk_top_rest:
            raise SystemExit(
                "ClickGraph reads disagree with REST reads: "
                f"dawn {dawn_top!r} vs {dawn_top_rest!r}; "
                f"dusk {dusk_top!r} vs {dusk_top_rest!r}"
            )
        if dawn_top == dusk_top:
            raise SystemExit("ClickGraph reads did not diverge per cohort")
        print(
            f"      serving tier lift: baseline {baseline[dawn_learner]!r} "
            f"(both cohorts) -> dawn {dawn_top!r} (weight {dawn_weight:.3f}) / "
            f"dusk {dusk_top!r} (weight {dusk_weight:.3f})"
        )
    finally:
        _stop(cg)


def _phase_mcp(client: PlatformClient, dawn_learner: str) -> None:
    """[10] MCP provenance + the loop's own report."""
    print("[10/10] MCP proofs (the loop's own report, graph-annotated provenance)")
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
    print("      one ledger truth; broker + graph + MCP channels all read it")


def main() -> int:
    platform_dir = _platform_dir()
    _require_clickhouse()
    require_pulsar()
    _free_demo_ports()
    _free_ports((CLICKGRAPH_PORT,))
    tenant, run_id = new_run_tenant()
    experiment = make_experiment(f"run-{run_id}")
    dawn_learner, dusk_learner = cohort_representatives(LEARNERS)

    print(f"[1/10] platform checkout: {platform_dir}")
    print(f"       ClickGraph: {CLICKGRAPH_URL} (ClickHouse HTTP at {_clickhouse_api_url()})")
    print(f"       Pulsar: {PULSAR_ADMIN_URL} (topic {topic_for_tenant(tenant)})")

    print(f"[2/10] registering this run's tenant ({tenant}) in the durable registry")
    print("      " + _platform_run(platform_dir, "tenancy", "onboard", tenant).strip())

    print("[3/10] booting the deployed platform serve (this app's mapping spec)")
    serve = _start_serve(platform_dir, tenant)
    try:
        client = _client()
        _phase_publish(platform_dir, tenant, experiment)
        baseline = _phase_baseline(client, LEARNERS_NAMES)
        _phase_outcomes(platform_dir, tenant, experiment)
        _phase_loop(platform_dir)
        dawn_top_rest, dusk_top_rest = _phase_lift_rest(client, dawn_learner, dusk_learner)
        _phase_clickgraph(
            tenant, baseline, dawn_learner, dusk_learner, dawn_top_rest, dusk_top_rest
        )
        _phase_mcp(client, dawn_learner)
        return 0
    finally:
        _kill_tree(serve)


if __name__ == "__main__":
    sys.exit(main())
