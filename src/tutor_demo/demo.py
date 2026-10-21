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
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Final

from .generator import LEARNERS, Experiment, make_experiment
from .platform_client import ClientConfig, PlatformClient

TENANT_PREFIX: Final = "tutor-app-run"
TOKEN: Final = "tutor-demo-token"
INGEST_PORT: Final = int(os.environ.get("GMP_DEMO_INGEST_PORT", "8131"))
REST_PORT: Final = int(os.environ.get("GMP_DEMO_REST_PORT", "8132"))
MCP_PORT: Final = int(os.environ.get("GMP_DEMO_MCP_PORT", "8133"))
INGEST_URL: Final = f"http://127.0.0.1:{INGEST_PORT}"
REST_URL: Final = f"http://127.0.0.1:{REST_PORT}"
MCP_URL: Final = f"http://127.0.0.1:{MCP_PORT}"


def _platform_dir() -> Path:
    """Locate the platform checkout (deploy posture, not a code dependency)."""
    configured = os.environ.get("GMP_DEMO_PLATFORM_DIR")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[3] / "graph-memory-platform",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "src/graph_memory").exists():
            return candidate.resolve()
    raise SystemExit("graph-memory-platform checkout not found; set GMP_DEMO_PLATFORM_DIR.")


def _mapping_path() -> Path:
    return Path(__file__).resolve().parent / "mapping.yaml"


def _require_clickhouse() -> None:
    host = os.environ.get("GMP_CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("GMP_CLICKHOUSE_PORT", "8124"))
    probe = socket.socket()
    probe.settimeout(2.0)
    try:
        probe.connect((host, port))
    except OSError as exc:
        raise SystemExit(
            f"ClickHouse is not reachable at {host}:{port} ({exc});\n"
            "start the dev posture first:\n"
            "  docker compose -f <platform>/ops/compose/clickhouse.yaml up -d"
        ) from exc
    finally:
        probe.close()


def _platform_run(platform_dir: Path, *args: str) -> str:
    """Run a platform CLI command in the platform's own environment."""
    completed = subprocess.run(
        ["uv", "run", "platform", *args],
        cwd=platform_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"platform {' '.join(args)} failed ({completed.returncode}):\n"
            f"{completed.stdout[-1200:]}\n{completed.stderr[-1200:]}"
        )
    return completed.stdout


def _start_serve(platform_dir: Path, tenant: str) -> subprocess.Popen[str]:
    """Boot the platform serve wired to this app's mapping + tenant token."""
    env = dict(os.environ)
    env["GMP_DEV_STATIC_TOKENS"] = json.dumps(
        {"tutor-demo-token": tenant, "dev-acme-token": "acme", "dev-other-token": "other"}
    )
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "platform",
            "serve",
            "--mapping",
            str(_mapping_path()),
            "--ingest-port",
            str(INGEST_PORT),
            "--rest-port",
            str(REST_PORT),
            "--mcp-port",
            str(MCP_PORT),
        ],
        cwd=platform_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        if line.startswith("serving:"):
            return process
        print(f"      {line.strip()}")
        if "Traceback" in line or "Error" in line:
            break
    process.terminate()
    raise SystemExit("deployed platform serve did not boot cleanly (see output above)")


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Stop the serve and everything it spawned (uv.exe wraps the uvicorn)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()


def _free_demo_ports() -> None:
    """Kill leftovers of a crashed previous run (the demo owns these ports)."""
    if os.name != "nt":
        return
    ports = (INGEST_PORT, REST_PORT, MCP_PORT)
    listing = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, check=False
    ).stdout
    pids = {
        int(line.split()[-1])
        for line in listing.splitlines()
        if "LISTENING" in line and any(f":{port} " in line for port in ports)
    }
    for pid in sorted(pids):
        print(f"      cleanup: stopping leftover serve pid {pid}")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def _client() -> PlatformClient:
    return PlatformClient(
        ClientConfig(ingest_url=INGEST_URL, rest_url=REST_URL, mcp_url=MCP_URL, token=TOKEN)
    )


def _tops(client: PlatformClient) -> dict[str, str]:
    """Per-learner top recommendation target."""
    return {
        learner: (items[0][0] if items else "")
        for learner, items in ((learner, client.recos(learner)) for learner, _cohort in LEARNERS)
    }


def _wait_for_lift(client: PlatformClient) -> dict[str, str]:
    """Poll reads until the cohorts' orders visibly diverge (the loop is live)."""
    dawn_learner, dusk_learner = LEARNERS[0][0], LEARNERS[2][0]
    for _ in range(30):
        tops = _tops(client)
        if (
            tops[dawn_learner] != tops[dusk_learner]
            and tops[dawn_learner] != ""
            and tops[dusk_learner] != ""
        ):
            return tops
        time.sleep(1.0)
    return _tops(client)


def _print_table(title: str, tops: dict[str, str]) -> None:
    print(f"-- {title}")
    for learner, target in tops.items():
        print(f"   {learner:<16} -> {target or '(none)'}")


def _baseline(client: PlatformClient, experiment: Experiment) -> dict[str, str]:
    """Ingest the app's signal story, then read once (the platform projects)."""
    print("[4/9] ingest: skill assessments + recommendations (real signals)")
    ack = client.post_signals(list(experiment.events))
    print(f"      ack: {ack}")
    assert ack["accepted"] == len(experiment.events), "ingest silently dropped events"

    # the graph carries the ingest only after the next projection interval
    # (tick -> replay -> atomic publish); poll instead of assuming the timing
    for attempt in range(45):
        baseline = _tops(client)
        if "" not in set(baseline.values()):
            break
        if attempt == 0:
            print("      waiting for the next projection interval to publish...")
        time.sleep(1.0)
    baseline = _tops(client)
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
    after = _wait_for_lift(client)
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
    dawn_learner, dusk_learner = LEARNERS[0][0], LEARNERS[2][0]
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
        baseline = _baseline(client, experiment)
        _correlate_and_loop(client, experiment, platform_dir)
        _lift(client, baseline, dawn_learner, dusk_learner, dawn_cohort, dusk_cohort)
        _proofs(client, dawn_learner)
        return 0
    finally:
        _kill_tree(serve)


if __name__ == "__main__":
    sys.exit(main())
