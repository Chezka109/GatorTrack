"""Failure-scenario evaluation for GatorTrack.

This script exercises predictable failure modes against a deployed base URL:
- Malformed webhook payloads
- Missing credentials (student not connected)
- Optional GitHub rate limit probing via repeated /debug/clear-cache
    and /debug/assignments

It outputs a JSON summary with counts, latencies, and observed status codes.

Usage:
    python -m evaluation.failure_tests --base-url https://YOUR.onrender.com

Optional (rate limit probe; use carefully):
    python -m evaluation.failure_tests \
        --base-url https://YOUR.onrender.com \
        --probe-rate-limit \
        --iterations 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests


@dataclass
class CaseResult:
    name: str
    method: str
    endpoint: str
    status_code: int | None
    latency_ms: float
    ok: bool
    response_json: Any | None
    error: str | None


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _call(
    session: requests.Session,
    method: str,
    url: str,
    timeout_s: float,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, Any] | tuple[None, None]:
    resp = session.request(method, url, json=json_body, timeout=timeout_s)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _run_case(
    session: requests.Session,
    name: str,
    method: str,
    base_url: str,
    endpoint: str,
    timeout_s: float,
    json_body: dict[str, Any] | None = None,
    ok_if: Callable[[int | None, Any | None], bool] | None = None,
) -> CaseResult:
    url = f"{base_url}{endpoint}"
    start = time.perf_counter()
    try:
        status_code, body = _call(
            session,
            method,
            url,
            timeout_s,
            json_body=json_body,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0

        if ok_if is None:
            ok = status_code is not None and status_code < 500
        else:
            ok = bool(ok_if(status_code, body))

        return CaseResult(
            name=name,
            method=method,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            ok=ok,
            response_json=body,
            error=None,
        )
    except requests.RequestException as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return CaseResult(
            name=name,
            method=method,
            endpoint=endpoint,
            status_code=None,
            latency_ms=latency_ms,
            ok=False,
            response_json=None,
            error=repr(exc),
        )


def _summarize(cases: list[CaseResult]) -> dict[str, Any]:
    total = len(cases)
    failures = [c for c in cases if not c.ok]

    latencies = [c.latency_ms for c in cases]
    latencies_sorted = sorted(latencies)

    def pct(p: float) -> float | None:
        if not latencies_sorted:
            return None
        k = int(round((p / 100.0) * (len(latencies_sorted) - 1)))
        return round(float(latencies_sorted[k]), 3)

    by_status: dict[str, int] = {}
    for c in cases:
        key = str(c.status_code)
        by_status[key] = by_status.get(key, 0) + 1

    return {
        "total_cases": total,
        "failures": len(failures),
        "pass_rate_percent": (
            round(
                ((total - len(failures)) / total * 100.0) if total else 0.0,
                4,
            )
        ),
        "latency_ms": {
            "avg": round(statistics.mean(latencies), 3) if latencies else None,
            "p50": pct(50),
            "p90": pct(90),
            "p95": pct(95),
            "p99": pct(99),
        },
        "status_code_counts": by_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--probe-rate-limit",
        action="store_true",
        help=(
            "Optional probe: repeatedly clears cache and fetches assignments. "
            "Use carefully; can consume GitHub API quota."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="Iterations for rate-limit probe (only if --probe-rate-limit)",
    )

    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    session = requests.Session()

    cases: list[CaseResult] = []

    # Sanity
    cases.append(
        _run_case(
            session,
            name="health_ok",
            method="GET",
            base_url=base_url,
            endpoint="/health",
            timeout_s=args.timeout_seconds,
            ok_if=lambda code, body: code == 200 and isinstance(body, dict),
        )
    )

    # Malformed webhook payloads
    cases.append(
        _run_case(
            session,
            name="webhook_missing_repository_key",
            method="POST",
            base_url=base_url,
            endpoint="/webhook",
            timeout_s=args.timeout_seconds,
            json_body={"not_repository": True},
            ok_if=lambda code, body: code is not None and code < 500,
        )
    )

    cases.append(
        _run_case(
            session,
            name="webhook_repository_missing_name",
            method="POST",
            base_url=base_url,
            endpoint="/webhook",
            timeout_s=args.timeout_seconds,
            json_body={"repository": {"owner": {"login": "org"}}},
            ok_if=lambda code, body: code is not None and code < 500,
        )
    )

    cases.append(
        _run_case(
            session,
            name="webhook_repo_name_invalid_format",
            method="POST",
            base_url=base_url,
            endpoint="/webhook",
            timeout_s=args.timeout_seconds,
            json_body={"repository": {"name": "a", "owner": {"login": "org"}}},
            ok_if=lambda code, body: code is not None and code < 500,
        )
    )

    # Missing credentials: valid repo-like name but unknown user
    cases.append(
        _run_case(
            session,
            name="webhook_user_not_connected",
            method="POST",
            base_url=base_url,
            endpoint="/webhook",
            timeout_s=args.timeout_seconds,
            json_body={
                "repository": {
                    "name": "lab-1-DefinitelyNotConnectedUser",
                    "owner": {"login": "org"},
                }
            },
            ok_if=lambda code, body: isinstance(body, dict)
            and body.get("status") == "user_not_connected",
        )
    )

    rate_probe_results: list[CaseResult] = []
    if args.probe_rate_limit:
        for i in range(args.iterations):
            # Force fresh GitHub fetch by clearing server-side cache.
            rate_probe_results.append(
                _run_case(
                    session,
                    name=f"rateprobe_clear_cache_{i + 1}",
                    method="POST",
                    base_url=base_url,
                    endpoint="/debug/clear-cache",
                    timeout_s=args.timeout_seconds,
                    ok_if=lambda code, body: code is not None and code < 500,
                )
            )
            rate_probe_results.append(
                _run_case(
                    session,
                    name=f"rateprobe_fetch_assignments_{i + 1}",
                    method="GET",
                    base_url=base_url,
                    endpoint="/debug/assignments",
                    timeout_s=args.timeout_seconds,
                    ok_if=lambda code, body: code is not None and code < 500,
                )
            )

            # small pause to reduce burstiness
            time.sleep(0.2)

    summary = {
        "timestamp": _iso_now(),
        "base_url": base_url,
        "cases": [c.__dict__ for c in cases],
        "summary": _summarize(cases),
    }

    if args.probe_rate_limit:
        summary["rate_limit_probe"] = {
            "iterations": args.iterations,
            "cases": [c.__dict__ for c in rate_probe_results],
            "summary": _summarize(rate_probe_results),
        }

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
