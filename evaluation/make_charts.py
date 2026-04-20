"""Generate presentation-ready charts from evaluation JSON summaries.

Creates PNGs in the output directory:
- availability_by_endpoint.png
- latency_percentiles_log.png
- failure_status_codes.png

Usage:
    /path/to/python -m evaluation.make_charts \
        --load-summary evaluation_logs/load_..._summary.json \
        --failure-summary evaluation_logs/failure_..._summary.json \
        --out-dir evaluation_charts

If --load-summary is omitted, the newest load_*_summary.json in
evaluation_logs is used.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402


@dataclass(frozen=True)
class ChartPaths:
    availability_by_endpoint: Path
    latency_percentiles_log: Path
    failure_status_codes: Path


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _newest_load_summary(log_dir: Path) -> Path:
    candidates = sorted(log_dir.glob("load_*_summary.json"))
    if not candidates:
        raise FileNotFoundError(f"No load_*_summary.json found in {log_dir}")
    return candidates[-1]


def _ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)


def _style() -> None:
    # Keep styling minimal and portable (no custom fonts/colors).
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 150,
            "font.size": 12,
            "axes.titlesize": 14,
            "axes.labelsize": 12,
        }
    )


def _save(fig: Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def make_availability_by_endpoint(
    load_summary: dict[str, Any],
    out_path: Path,
) -> None:
    by_endpoint: dict[str, Any] = load_summary.get("by_endpoint", {})

    endpoints = sorted(by_endpoint.keys())
    avail = [float(by_endpoint[e]["availability_percent"]) for e in endpoints]

    # Include overall as a separate bar at the end.
    endpoints_all = endpoints + ["ALL"]
    avail_all = avail + [float(load_summary.get("availability_percent", 0.0))]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(endpoints_all, avail_all)

    ax.set_title("Load Test Availability by Endpoint")
    ax.set_xlabel("Endpoint")
    ax.set_ylabel("Availability (%)")

    # Zoom in so differences are visible.
    ax.set_ylim(90, 100)

    for bar, v in zip(bars, avail_all):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            v + 0.1,
            f"{v:.2f}%",
            ha="center",
            va="bottom",
            fontsize=10,
        )

    _save(fig, out_path)


def make_latency_percentiles_log(
    load_summary: dict[str, Any],
    out_path: Path,
) -> None:
    latency = load_summary.get("latency_ms", {})

    percentiles = ["p50", "p90", "p95", "p99"]
    values = [float(latency[p]) for p in percentiles]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(percentiles, values)

    ax.set_title("Load Test Latency Percentiles (log scale)")
    ax.set_xlabel("Percentile")
    ax.set_ylabel("Latency (ms)")
    ax.set_yscale("log")

    for bar, v in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            v,
            f"{v:.0f}ms" if v >= 1000 else f"{v:.1f}ms",
            ha="center",
            va="bottom",
            fontsize=10,
        )

    _save(fig, out_path)


def make_failure_status_codes(
    failure_summary: dict[str, Any],
    out_path: Path,
) -> None:
    summary = failure_summary.get("summary", {})
    counts: dict[str, Any] = summary.get("status_code_counts", {})

    # Keep numeric-ish ordering (200 before 500, etc.).
    def sort_key(k: str) -> tuple[int, str]:
        try:
            return int(k), k
        except ValueError:
            return 10**9, k

    codes = sorted(counts.keys(), key=sort_key)
    values = [int(counts[c]) for c in codes]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(codes, values)

    ax.set_title("Failure-Injection Outcomes (Status Codes)")
    ax.set_xlabel("HTTP Status Code")
    ax.set_ylabel("Count")
    ax.set_ylim(0, max(values) + 1)

    for bar, v in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            v + 0.05,
            str(v),
            ha="center",
            va="bottom",
            fontsize=11,
        )

    _save(fig, out_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--load-summary",
        default=None,
        help=("Path to load_*_summary.json (default: newest in evaluation_logs)"),
    )
    parser.add_argument(
        "--failure-summary",
        default=str(Path("evaluation_logs") / "failure_20260401T020656Z_summary.json"),
        help="Path to failure_*_summary.json",
    )
    parser.add_argument(
        "--out-dir",
        default="evaluation_charts",
        help="Directory to write PNG charts",
    )

    args = parser.parse_args()

    repo_root = Path(os.getcwd())
    log_dir = repo_root / "evaluation_logs"

    if args.load_summary:
        load_path = Path(args.load_summary)
    else:
        load_path = _newest_load_summary(log_dir)
    failure_path = Path(args.failure_summary)
    out_dir = Path(args.out_dir)

    _ensure_out_dir(out_dir)
    _style()

    load_summary = _load_json(load_path)
    failure_summary = _load_json(failure_path)

    paths = ChartPaths(
        availability_by_endpoint=out_dir / "availability_by_endpoint.png",
        latency_percentiles_log=out_dir / "latency_percentiles_log.png",
        failure_status_codes=out_dir / "failure_status_codes.png",
    )

    make_availability_by_endpoint(load_summary, paths.availability_by_endpoint)
    make_latency_percentiles_log(load_summary, paths.latency_percentiles_log)
    make_failure_status_codes(failure_summary, paths.failure_status_codes)

    print(
        json.dumps(
            {
                "load_summary": str(load_path),
                "failure_summary": str(failure_path),
                "out_dir": str(out_dir),
                "charts": {
                    "availability_by_endpoint": str(paths.availability_by_endpoint),
                    "latency_percentiles_log": str(paths.latency_percentiles_log),
                    "failure_status_codes": str(paths.failure_status_codes),
                },
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
