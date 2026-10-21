"""The tutor app's Pulsar producer — the broker front door for its signals.

Mirrors the platform's published contract (brokers/pulsar_cli.py):

    topic:      gmp.signals.<tenant>          (tenant is a routing fact)
    payload:    base64(envelope JSON)         (the CLI framing codec)
    ordering key: envelope.partitionkey (user) (the kit's publish-side rule)

No platform code is imported: this is httpx (admin REST) + subprocess (the
broker image's own client through docker exec), the same pure-consumer
posture as the REST/MCP paths.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess

import httpx

PULSAR_ADMIN_URL: str = os.environ.get("GMP_PULSAR_ADMIN_URL", "http://127.0.0.1:8099")
PULSAR_CONTAINER: str = os.environ.get("GMP_PULSAR_CONTAINER", "gmp-dev-pulsar")


def topic_for_tenant(tenant: str) -> str:
    """The platform's topic routing rule (the topic itself is the tenant)."""
    return f"gmp.signals.{tenant}"


def require_pulsar() -> None:
    """Fail with the dev posture hint if no broker is up."""
    try:
        probe = httpx.get(f"{PULSAR_ADMIN_URL}/admin/v2/brokers/standalone", timeout=5.0)
        probe.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(
            f"Pulsar dev broker not reachable at {PULSAR_ADMIN_URL} ({type(exc).__name__}: {exc})"
            "\nstart it first:"
            "\n  docker compose -f <platform>/ops/compose/pulsar.yaml up -d"
        ) from exc


def produce_envelopes(tenant: str, envelopes: list[dict[str, object]]) -> int:
    """Publish the story's envelopes through the real broker; returns the count.

    Ordering key = the envelope's partition key; payloads are base64(envelope
    JSON) exactly as the platform's drain receiver expects. Envelopes for one
    user share a single client call (per-key order preserved by the broker).
    """
    topic = topic_for_tenant(tenant)
    batches: dict[str, str] = {}
    for envelope in envelopes:
        encoded = base64.b64encode(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        key = str(envelope["partitionkey"])
        batches[key] = f"{batches[key]},{encoded}" if key in batches else encoded

    produced = 0
    for user, encoded_batch in sorted(batches.items()):
        completed = subprocess.run(
            [
                "docker",
                "exec",
                PULSAR_CONTAINER,
                "/pulsar/bin/pulsar-client",
                "produce",
                "-k",
                user,
                "-m",
                encoded_batch,
                topic,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or "successfully produced" not in completed.stdout:
            raise SystemExit(
                f"pulsar produce failed for {user!r} ({completed.returncode}):\n"
                f"{completed.stdout[-600:]}\n{completed.stderr[-600:]}"
            )
        stamp = [line for line in completed.stdout.splitlines() if "successfully produced" in line]
        produced += int(stamp[-1].split("messages successfully")[0].split()[-1])
    return produced
