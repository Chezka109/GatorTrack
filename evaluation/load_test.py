"""Realistic load test harness for GatorTrack.

Runs a configurable-duration test against a deployed base URL.

Generates:
- N "users" repeatedly hitting /connect and /health
- ~webhooks_per_hour webhook POSTs spread over time

Writes JSONL logs and prints a summary including availability + MTBF.

Usage:
    python -m evaluation.load_test \
        --base-url https://YOUR.onrender.com \
        --duration-seconds 3600

Notes:
- This measures service availability (did endpoints respond)
    vs business correctness.
- For /webhook, any non-5xx response counts as available.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


@dataclass
class Result:
    ts: float
    endpoint: str
    method: str
    status_code: int | None
    ok: bool
    latency_ms: float | None
    error: str | None


def _now_ts() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _request(
    session: requests.Session,
    method: str,
    url: str,
    endpoint: str,
    timeout_s: float,
    json_body: dict[str, Any] | None = None,
) -> Result:
    ts = _now_ts()
    start = time.perf_counter()
    try:
        resp = session.request(method, url, json=json_body, timeout=timeout_s)
        latency_ms = (time.perf_counter() - start) * 1000.0

        # Availability for this harness:
        # - any non-5xx response counts as available
        # - 5xx counts as failure
        ok = resp.status_code < 500

        return Result(
            ts=ts,
            endpoint=endpoint,
            method=method,
            status_code=resp.status_code,
            ok=ok,
            latency_ms=latency_ms,
            error=None,
        )
    except requests.RequestException as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return Result(
            ts=ts,
            endpoint=endpoint,
            method=method,
            status_code=None,
            ok=False,
            latency_ms=latency_ms,
            error=repr(exc),
        )


def _write_jsonl(
    path: str,
    record: dict[str, Any],
    lock: threading.Lock,
) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _summarize(
    results: list[Result],
    started_ts: float,
    ended_ts: float,
) -> dict[str, Any]:
    total = len(results)
    failures = [r for r in results if not r.ok]
    success = total - len(failures)

    availability = (success / total * 100.0) if total else 0.0

    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    latencies_sorted = sorted(latencies)

    def pct(p: float) -> float | None:
        if not latencies_sorted:
            return None
        k = int(round((p / 100.0) * (len(latencies_sorted) - 1)))
        return float(latencies_sorted[k])

    def pct_rounded(p: float) -> float | None:
        value = pct(p)
        return None if value is None else round(value, 3)

    # MTBF: mean time between failures based on failure timestamps.
    # If 0 or 1 failures, MTBF is effectively the whole run.
    failure_ts = sorted([r.ts for r in failures])
    if len(failure_ts) >= 2:
        intervals = [b - a for a, b in zip(failure_ts, failure_ts[1:])]
        mtbf_s = statistics.mean(intervals)
    elif len(failure_ts) == 1:
        mtbf_s = ended_ts - started_ts
    else:
        mtbf_s = ended_ts - started_ts

    summary_by_endpoint: dict[str, Any] = {}
    for endpoint in sorted(set(r.endpoint for r in results)):
        subset = [r for r in results if r.endpoint == endpoint]
        sub_total = len(subset)
        sub_fail = sum(1 for r in subset if not r.ok)
        sub_avail = ((sub_total - sub_fail) / sub_total * 100.0) if sub_total else 0.0
        summary_by_endpoint[endpoint] = {
            "total": sub_total,
            "failures": sub_fail,
            "availability_percent": round(sub_avail, 4),
        }

    return {
        "started_at": _iso(started_ts),
        "ended_at": _iso(ended_ts),
        "duration_seconds": round(ended_ts - started_ts, 3),
        "total_requests": total,
        "failures": len(failures),
        "availability_percent": round(availability, 4),
        "mtbf_seconds": round(float(mtbf_s), 3),
        "latency_ms": {
            "count": len(latencies),
            "p50": pct_rounded(50),
            "p90": pct_rounded(90),
            "p95": pct_rounded(95),
            "p99": pct_rounded(99),
        },
        "by_endpoint": summary_by_endpoint,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        required=True,
        help="Deployed base URL, e.g. https://xyz.onrender.com",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=3600,
        help="How long to run",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=30,
        help="Number of synthetic users",
    )
    parser.add_argument(
        "--webhooks-per-hour",
        type=int,
        default=100,
        help="Target webhook POSTs/hour",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10.0,
        help="HTTP timeout per request",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="JSONL log path (default: evaluation_logs/load_*.jsonl)",
    )

    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    duration_s = int(args.duration_seconds)

    log_dir = os.path.join(os.getcwd(), "evaluation_logs")
    os.makedirs(log_dir, exist_ok=True)

    started_ts = _now_ts()
    ended_target = started_ts + duration_s

    log_file = args.log_file
    if not log_file:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = os.path.join(log_dir, f"load_{stamp}.jsonl")

    write_lock = threading.Lock()

    # Pre-compute webhook schedule (uniformly spaced), with slight jitter.
    # Example: 100/hour -> every 36s.
    webhook_interval = 3600.0 / max(1, args.webhooks_per_hour)
    webhook_times: list[float] = []
    t = started_ts
    while t < ended_target:
        jitter = random.uniform(-0.15, 0.15) * webhook_interval
        webhook_times.append(t + webhook_interval + jitter)
        t += webhook_interval

    results: list[Result] = []
    results_lock = threading.Lock()

    def record(res: Result) -> None:
        with results_lock:
            results.append(res)
        _write_jsonl(
            log_file,
            {
                "ts": res.ts,
                "ts_iso": _iso(res.ts),
                "endpoint": res.endpoint,
                "method": res.method,
                "status_code": res.status_code,
                "ok": res.ok,
                "latency_ms": res.latency_ms,
                "error": res.error,
            },
            write_lock,
        )

    def user_loop(user_id: int) -> None:
        session = requests.Session()
        rng = random.Random(user_id)

        while _now_ts() < ended_target:
            # Simulate typical behavior: mostly idle with occasional page hits
            # and periodic health checks.
            pick = rng.random()
            if pick < 0.6:
                endpoint = "/health"
                url = f"{base_url}{endpoint}"
                record(
                    _request(
                        session,
                        "GET",
                        url,
                        endpoint,
                        args.timeout_seconds,
                    )
                )
            else:
                endpoint = "/connect"
                url = f"{base_url}{endpoint}"
                record(
                    _request(
                        session,
                        "GET",
                        url,
                        endpoint,
                        args.timeout_seconds,
                    )
                )

            time.sleep(rng.uniform(2.0, 8.0))

    def webhook_loop() -> None:
        session = requests.Session()

        for scheduled_ts in webhook_times:
            now = _now_ts()
            if now >= ended_target:
                break
            sleep_s = scheduled_ts - now
            if sleep_s > 0:
                time.sleep(sleep_s)

            # Repository names mimic GitHub Classroom format:
            # assignment-slug-Username
            username = f"LoadUser{random.randint(1, max(1, args.users))}"
            repo_name = f"lab-1-{username}"
            payload = {
                "repository": {
                    "name": repo_name,
                    "owner": {"login": "classroom-org"},
                }
            }

            endpoint = "/webhook"
            url = f"{base_url}{endpoint}"
            record(
                _request(
                    session,
                    "POST",
                    url,
                    endpoint,
                    args.timeout_seconds,
                    json_body=payload,
                )
            )

    max_workers = max(4, args.users + 2)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for i in range(args.users):
            futures.append(ex.submit(user_loop, i + 1))
        futures.append(ex.submit(webhook_loop))

        # Wait for completion
        for fut in as_completed(futures):
            _ = fut.result()

    ended_ts = _now_ts()
    summary = _summarize(results, started_ts, ended_ts)

    summary_path = log_file.replace(".jsonl", "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\nLogs: {log_file}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
