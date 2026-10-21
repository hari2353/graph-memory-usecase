"""Plumbing the tutor app shares across all its live drives.

Nothing here is platform code: it locates the platform checkout, checks the
dev posture (ClickHouse, ports), boots the deployed serve, and reads context
— the same third-party-consumer contract every runner uses.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Final

from .platform_client import ClientConfig, PlatformClient

TENANT_PREFIX: Final = "tutor-app-run"
TOKEN: Final = "tutor-demo-token"

CLICKHOUSE_DATABASE: Final = os.environ.get("GMP_CLICKHOUSE_DATABASE", "gmp")
NODES_TABLE: Final = os.environ.get("GMP_DEMO_NODES_TABLE", "gm_nodes")
EDGES_TABLE: Final = os.environ.get("GMP_DEMO_EDGES_TABLE", "gm_edges")

INGEST_PORT: Final = int(os.environ.get("GMP_DEMO_INGEST_PORT", "8131"))
REST_PORT: Final = int(os.environ.get("GMP_DEMO_REST_PORT", "8132"))
MCP_PORT: Final = int(os.environ.get("GMP_DEMO_MCP_PORT", "8133"))
INGEST_URL: Final = f"http://127.0.0.1:{INGEST_PORT}"
REST_URL: Final = f"http://127.0.0.1:{REST_PORT}"
MCP_URL: Final = f"http://127.0.0.1:{MCP_PORT}"

CLICKHOUSE_USER: Final = os.environ.get("GMP_CLICKHOUSE_USER", "clickhouse")
CLICKHOUSE_PASSWORD: Final = os.environ.get("GMP_CLICKHOUSE_PASSWORD", "e2e-ch-pass-1234")

CLICKGRAPH_PORT: Final = int(os.environ.get("GMP_DEMO_CG_PORT", "7478"))
CLICKGRAPH_URL: Final = f"http://127.0.0.1:{CLICKGRAPH_PORT}"
CLICKGRAPH_BINARY: Final = Path(
    os.environ.get("GMP_DEMO_CG_BINARY", r"F:\cg\target\debug\clickgraph.exe")
)

MIN_COHORTS: Final = 2


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


def clickhouse_endpoint() -> tuple[str, int]:
    host = os.environ.get("GMP_CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("GMP_CLICKHOUSE_PORT", "8124"))
    return host, port


def _require_clickhouse() -> None:
    host, port = clickhouse_endpoint()
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


def _free_ports(ports: tuple[int, ...]) -> None:
    """Kill leftovers of a crashed previous run (these ports are the demo's)."""
    if os.name != "nt":
        return
    listing = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, check=False
    ).stdout
    pids = {
        int(line.split()[-1])
        for line in listing.splitlines()
        if "LISTENING" in line and any(f":{port} " in line for port in ports)
    }
    for pid in sorted(pids):
        print(f"      cleanup: stopping leftover pid {pid}")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def _free_demo_ports() -> None:
    _free_ports((INGEST_PORT, REST_PORT, MCP_PORT))


def _client() -> PlatformClient:
    return PlatformClient(
        ClientConfig(ingest_url=INGEST_URL, rest_url=REST_URL, mcp_url=MCP_URL, token=TOKEN)
    )


def _tops(client: PlatformClient, learners: tuple[str, ...]) -> dict[str, str]:
    """Per-learner top recommendation target."""
    return {
        learner: (items[0][0] if items else "")
        for learner, items in ((learner, client.recos(learner)) for learner in learners)
    }


def _poll_published(client: PlatformClient, learners: tuple[str, ...]) -> dict[str, str]:
    """Poll reads until every learner's top is set (the graph is published)."""
    for attempt in range(45):
        tops = _tops(client, learners)
        if "" not in set(tops.values()):
            return tops
        if attempt == 0:
            print("      waiting for the next projection interval to publish...")
        time.sleep(1.0)
    return _tops(client, learners)


def _wait_for_lift(client: PlatformClient, learner_a: str, learner_b: str) -> dict[str, str]:
    """Poll reads until the two cohorts' orders visibly diverge (loop is live)."""
    for _ in range(30):
        tops_a, tops_b = client.recos(learner_a), client.recos(learner_b)
        if tops_a and tops_b and tops_a[0][0] != tops_b[0][0]:
            return {learner_a: tops_a[0][0], learner_b: tops_b[0][0]}
        time.sleep(1.0)
    return {
        learner_a: client.recos(learner_a)[0][0] if client.recos(learner_a) else "",
        learner_b: client.recos(learner_b)[0][0] if client.recos(learner_b) else "",
    }


def _print_table(title: str, tops: dict[str, str]) -> None:
    print(f"-- {title}")
    for learner, target in tops.items():
        print(f"   {learner:<16} -> {target or '(none)'}")


def new_run_tenant(run_id: str | None = None) -> tuple[str, str]:
    """A run-scoped experiment tenant: reruns never inherit prior ledger state."""
    run_id = run_id or uuid.uuid4().hex[:8]
    return f"{TENANT_PREFIX}-{run_id}", run_id


def cohort_representatives(
    learners: tuple[tuple[str, str], ...],
) -> tuple[str, str]:
    """One learner per cohort (first listed); the whole-loop compares these."""
    seen: dict[str, str] = {}
    for learner, cohort in learners:
        seen.setdefault(cohort, learner)
    members = list(seen.values())
    if len(members) < MIN_COHORTS:
        raise SystemExit("the story needs at least two distinct cohorts")
    return members[0], members[-1]
