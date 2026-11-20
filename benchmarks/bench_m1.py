"""Milestone 1 benchmarks for the current Custom-DBMS project.

Run from the repository root:
    python -m benchmarks.bench_m1

This script benchmarks the existing RowLayout (NSM) and ColumnLayout (DSM)
implementations. It instruments the existing DiskManager through the
``disk_factory`` extension point already supported by storage.layouts.

The replacement-policy experiment replays an identical page trace through
fresh instances of the team's LRU and FIFO buffer pool.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import random
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

# Allow both:
#   python -m benchmarks.bench_m1
#   python benchmarks/bench_m1.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.disk_manager import DiskManager
from storage.layouts import ColumnLayout, RowLayout
from storage.page import SlottedPage
from benchmarks.charts import bar_chart


COLUMNS = ("id", "category", "payload")


@dataclass
class IOMetrics:
    read_calls: int = 0
    write_calls: int = 0
    allocate_calls: int = 0
    read_bytes: int = 0
    write_bytes: int = 0
    read_ns: int = 0
    write_ns: int = 0
    allocate_ns: int = 0

    def reset(self) -> None:
        self.read_calls = 0
        self.write_calls = 0
        self.allocate_calls = 0
        self.read_bytes = 0
        self.write_bytes = 0
        self.read_ns = 0
        self.write_ns = 0
        self.allocate_ns = 0

    def snapshot(self) -> dict:
        return asdict(self)


class CountingDiskManager(DiskManager):
    """DiskManager-compatible instrumentation; page format and behavior stay unchanged."""

    def __init__(self, file_path, page_size=4096, *, metrics: IOMetrics):
        super().__init__(file_path, page_size=page_size)
        self._metrics = metrics

    def read_page(self, page_id):
        started = time.perf_counter_ns()
        data = super().read_page(page_id)
        self._metrics.read_calls += 1
        self._metrics.read_bytes += len(data)
        self._metrics.read_ns += time.perf_counter_ns() - started
        return data

    def write_page(self, page_id, data):
        started = time.perf_counter_ns()
        super().write_page(page_id, data)
        self._metrics.write_calls += 1
        self._metrics.write_bytes += self.page_size
        self._metrics.write_ns += time.perf_counter_ns() - started

    def allocate_page(self):
        started = time.perf_counter_ns()
        page_id = super().allocate_page()
        self._metrics.allocate_calls += 1
        self._metrics.allocate_ns += time.perf_counter_ns() - started
        return page_id


def _disk_factory(metrics: IOMetrics) -> Callable:
    # _Layout accepts a factory with the same call shape as DiskManager(path).
    return lambda path: CountingDiskManager(path, metrics=metrics)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _make_row(row_id: int, payload_size: int) -> tuple:
    return (row_id, f"group-{row_id % 10}", f"{row_id:08d}-" + ("x" * payload_size))


def _timed_scan(table, projection=None) -> tuple[float, int]:
    started = time.perf_counter_ns()
    checksum = 0
    count = 0
    for row in table.scan(projection):
        # Consume the iterator and its values so the full scan is timed.
        if row:
            checksum += len(str(row[0]))
        count += 1
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    # Keep the result observable without retaining the full table in memory.
    if checksum < 0:  # pragma: no cover - defensive; checksum is non-negative
        raise AssertionError("Impossible scan checksum")
    return elapsed_ms, count


def _run_layout_once(layout_name: str, rows: int, payload_size: int,
                     point_reads: int, seed: int, repeat: int) -> dict:
    layout_class = RowLayout if layout_name == "NSM" else ColumnLayout
    metrics = IOMetrics()
    factory = _disk_factory(metrics)
    rng = random.Random(seed)

    with TemporaryDirectory(prefix=f"adb_m1_{layout_name.lower()}_") as temp_dir:
        database_path = Path(temp_dir) / "table.db"
        result = {
            "layout": layout_name,
            "repeat": repeat,
            "rows": rows,
            "payload_size": payload_size,
        }

        with layout_class(database_path, COLUMNS, disk_factory=factory) as table:
            insert_times = []
            metrics.reset()
            insert_started = time.perf_counter_ns()
            for row_id in range(rows):
                started = time.perf_counter_ns()
                returned_id = table.insert(_make_row(row_id, payload_size))
                insert_times.append((time.perf_counter_ns() - started) / 1_000_000)
                if returned_id != row_id:
                    raise AssertionError(
                        f"{layout_name} returned row ID {returned_id}, expected {row_id}"
                    )
            insert_total_ms = (time.perf_counter_ns() - insert_started) / 1_000_000
            insert_io = metrics.snapshot()

            # Explicitly publish the final pending page group and measure its I/O.
            metrics.reset()
            flush_started = time.perf_counter_ns()
            table.flush()
            flush_ms = (time.perf_counter_ns() - flush_started) / 1_000_000
            flush_io = metrics.snapshot()

            # Full scan.
            metrics.reset()
            full_scan_ms, full_scan_count = _timed_scan(table)
            full_scan_io = metrics.snapshot()
            if full_scan_count != rows:
                raise AssertionError(
                    f"{layout_name} full scan returned {full_scan_count} rows, expected {rows}"
                )

            # Projection scan: DSM can read only the requested column pages.
            metrics.reset()
            projection_scan_ms, projection_count = _timed_scan(
                table, projection=("id", "payload")
            )
            projection_io = metrics.snapshot()
            if projection_count != rows:
                raise AssertionError(
                    f"{layout_name} projection scan returned {projection_count} rows"
                )

            # Random point reads. Both layouts use the same deterministic row IDs.
            sample_count = min(point_reads, rows)
            selected_ids = [rng.randrange(rows) for _ in range(sample_count)]
            metrics.reset()
            point_read_times = []
            for row_id in selected_ids:
                started = time.perf_counter_ns()
                row = table.get_row(row_id)
                point_read_times.append((time.perf_counter_ns() - started) / 1_000_000)
                if row[0] != row_id:
                    raise AssertionError(
                        f"{layout_name} get_row({row_id}) returned the wrong row"
                    )
            point_read_io = metrics.snapshot()

            result.update({
                "insert_total_ms": insert_total_ms,
                "insert_avg_ms": _mean(insert_times),
                "insert_p95_ms": _percentile(insert_times, 0.95),
                "insert_page_reads": insert_io["read_calls"],
                "insert_page_writes": insert_io["write_calls"],
                "insert_page_allocations": insert_io["allocate_calls"],
                "flush_ms": flush_ms,
                "flush_page_writes": flush_io["write_calls"],
                "flush_page_allocations": flush_io["allocate_calls"],
                "flush_avg_write_page_ms": (
                    flush_io["write_ns"] / flush_io["write_calls"] / 1_000_000
                    if flush_io["write_calls"] else 0.0
                ),
                "full_scan_ms": full_scan_ms,
                "full_scan_page_reads": full_scan_io["read_calls"],
                "full_scan_avg_read_page_ms": (
                    full_scan_io["read_ns"] / full_scan_io["read_calls"] / 1_000_000
                    if full_scan_io["read_calls"] else 0.0
                ),
                "projection_scan_ms": projection_scan_ms,
                "projection_page_reads": projection_io["read_calls"],
                "projection_read_bytes": projection_io["read_bytes"],
                "point_read_avg_ms": _mean(point_read_times),
                "point_read_p95_ms": _percentile(point_read_times, 0.95),
                "point_read_page_reads": point_read_io["read_calls"],
                "point_read_avg_read_page_ms": (
                    point_read_io["read_ns"] / point_read_io["read_calls"] / 1_000_000
                    if point_read_io["read_calls"] else 0.0
                ),
            })

        result["table_file_bytes"] = database_path.stat().st_size
        return result


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _summary(raw_rows: list[dict]) -> list[dict]:
    metrics = [
        "insert_avg_ms", "insert_p95_ms", "insert_page_reads",
        "insert_page_writes", "flush_ms", "flush_page_writes",
        "flush_avg_write_page_ms", "full_scan_ms", "full_scan_page_reads",
        "full_scan_avg_read_page_ms", "projection_scan_ms",
        "projection_page_reads", "point_read_avg_ms", "point_read_p95_ms",
        "point_read_page_reads", "point_read_avg_read_page_ms",
        "table_file_bytes",
    ]
    summary_rows = []
    for layout_name in ("NSM", "DSM"):
        group = [row for row in raw_rows if row["layout"] == layout_name]
        if not group:
            continue
        item = {"layout": layout_name, "runs": len(group)}
        for metric in metrics:
            values = [float(row[metric]) for row in group]
            item[f"{metric}_mean"] = _mean(values)
            item[f"{metric}_stddev"] = statistics.stdev(values) if len(values) > 1 else 0.0
        summary_rows.append(item)
    return summary_rows


def _make_plots(summary_rows: list[dict], output_dir: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        for metric, name in [("full_scan_ms", "Full scan"), ("projection_scan_ms", "Projection scan"),
                             ("point_read_avg_ms", "Point lookup")]:
            bar_chart(output_dir / f"m1_{metric}.svg", f"NSM vs DSM · {name}", "Milliseconds",
                      [(r["layout"], r[f"{metric}_mean"], r[f"{metric}_stddev"]) for r in summary_rows])
        return True

    if not summary_rows:
        return False

    labels = [row["layout"] for row in summary_rows]
    x = list(range(len(labels)))
    width = 0.25

    fig, ax = plt.subplots(figsize=(9, 5))
    for offset, metric, label in [
        (-width, "full_scan_ms_mean", "Full scan"),
        (0, "projection_scan_ms_mean", "Projection scan"),
        (width, "point_read_avg_ms_mean", "Point-read average"),
    ]:
        values = [row[metric] for row in summary_rows]
        ax.bar([value + offset for value in x], values, width=width, label=label)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Milestone 1: NSM vs DSM latency")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "m1_layout_latency.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    for offset, metric, label in [
        (-width, "full_scan_page_reads_mean", "Full-scan page reads"),
        (0, "projection_page_reads_mean", "Projection page reads"),
        (width, "point_read_page_reads_mean", "Point-read page reads"),
    ]:
        values = [row[metric] for row in summary_rows]
        ax.bar([value + offset for value in x], values, width=width, label=label)
    ax.set_xticks(x, labels)
    ax.set_ylabel("DiskManager read_page calls")
    ax.set_title("Milestone 1: page-read I/O comparison")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "m1_page_reads.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    for offset, metric, label in [
        (-width, "full_scan_avg_read_page_ms_mean", "Read page latency (full scan)"),
        (0, "point_read_avg_read_page_ms_mean", "Read page latency (point reads)"),
        (width, "flush_avg_write_page_ms_mean", "Write page latency (flush)"),
    ]:
        values = [row[metric] for row in summary_rows]
        ax.bar([value + offset for value in x], values, width=width, label=label)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Average DiskManager page operation latency (ms)")
    ax.set_title("Milestone 1: page I/O latency")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "m1_page_io_latency.png", dpi=160)
    plt.close(fig)
    return True


def _policy_runs(root: Path, repeats: int, seed: int, accesses: int) -> list[dict]:
    from storage.buffer_pool import BufferPoolManager

    path = root / "pages.db"
    disk = DiskManager(path)
    expected = []
    try:
        for _ in range(32):
            page = SlottedPage(disk.allocate_page())
            page.insert_record(str(page.page_id).encode())
            raw = page.to_bytes()
            disk.write_page(page.page_id, raw)
            expected.append(raw)
    finally:
        disk.close()

    results = []
    for repeat in range(1, repeats + 1):
        trace_seed = seed + repeat * 1009
        rng = random.Random(trace_seed)
        trace = [rng.randrange(4) if rng.random() < 0.8 else rng.randrange(4, 32)
                 for _ in range(accesses)]
        policies = ("LRU", "FIFO") if repeat % 2 else ("FIFO", "LRU")
        for policy in policies:
            with BufferPoolManager(path, pool_size=8, replacement_policy=policy) as pool:
                for page_id in range(8):
                    pool.read_page(page_id)
                pool.reset_stats()
                elapsed = []
                for page_id in trace:
                    start = time.perf_counter_ns()
                    data = pool.read_page(page_id)
                    elapsed.append((time.perf_counter_ns() - start) / 1e6)
                    if data != expected[page_id]:
                        raise AssertionError("Buffer pool returned the wrong page")
                stats = pool.get_stats()
                if stats["cache_hits"] + stats["cache_misses"] != accesses:
                    raise AssertionError("Cache counters do not match the trace")
                results.append(dict(policy=policy, repeat=repeat, seed=trace_seed,
                                    accesses=accesses, mean_access_ms=_mean(elapsed),
                                    p95_access_ms=_percentile(elapsed, 0.95),
                                    **{k: stats[k] for k in ("cache_hits", "cache_misses",
                                       "hit_ratio", "miss_ratio", "disk_reads", "evictions")}))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark existing NSM and DSM storage layouts.")
    parser.add_argument("--rows", type=int, default=500, help="Number of rows per run (default: 500).")
    parser.add_argument("--payload-size", type=int, default=128,
                        help="Payload characters per row; keep <= 3000 (default: 128).")
    parser.add_argument("--point-reads", type=int, default=200,
                        help="Random point reads per layout and run (default: 200).")
    parser.add_argument("--repeats", type=int, default=5,
                        help="Repeated runs per layout (default: 5).")
    parser.add_argument("--seed", type=int, default=302, help="Random seed (default: 302).")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/m1"),
                        help="Output directory (default: benchmark_results/m1).")
    args = parser.parse_args()

    if args.rows < 1 or args.point_reads < 1 or args.repeats < 1:
        parser.error("--rows, --point-reads and --repeats must be positive integers")
    if not 0 <= args.payload_size <= 3000:
        parser.error("--payload-size must be between 0 and 3000 characters")

    args.output.mkdir(parents=True, exist_ok=True)
    raw_rows = []
    for layout_name in ("NSM", "DSM"):
        _run_layout_once(layout_name, min(32, args.rows), args.payload_size,
                         min(10, args.point_reads), args.seed, 0)
    for repeat in range(1, args.repeats + 1):
        for layout_name in (("NSM", "DSM") if repeat % 2 else ("DSM", "NSM")):
            row = _run_layout_once(
                layout_name=layout_name,
                rows=args.rows,
                payload_size=args.payload_size,
                point_reads=args.point_reads,
                seed=args.seed + repeat * 1009,
                repeat=repeat,
            )
            raw_rows.append(row)
            print(
                f"[M1] run {repeat}/{args.repeats} {layout_name}: "
                f"full_scan={row['full_scan_ms']:.3f} ms, "
                f"projection={row['projection_scan_ms']:.3f} ms, "
                f"point_read_avg={row['point_read_avg_ms']:.4f} ms"
            )

    summary_rows = _summary(raw_rows)
    _write_csv(args.output / "m1_raw_results.csv", raw_rows)
    _write_csv(args.output / "m1_summary.csv", summary_rows)
    plots_created = _make_plots(summary_rows, args.output)

    with TemporaryDirectory(prefix="adb_policy_") as tmp:
        policy_rows = _policy_runs(Path(tmp), args.repeats, args.seed, args.point_reads)
    _write_csv(args.output / "m1_policy_raw.csv", policy_rows)
    summaries = []
    for policy in ("LRU", "FIFO"):
        runs = [r for r in policy_rows if r["policy"] == policy]
        item = {"policy": policy, "runs": len(runs)}
        for metric in ("hit_ratio", "miss_ratio", "disk_reads", "mean_access_ms", "p95_access_ms"):
            values = [r[metric] for r in runs]
            item[metric + "_mean"] = _mean(values)
            item[metric + "_stddev"] = statistics.stdev(values) if len(values) > 1 else 0
        summaries.append(item)
    _write_csv(args.output / "m1_policy_summary.csv", summaries)
    bar_chart(args.output / "m1_policy_hits.svg", "LRU vs FIFO · cache hit rate", "Percent",
              [(r["policy"], 100*r["hit_ratio_mean"], 100*r["hit_ratio_stddev"]) for r in summaries])
    status = {
        "buffer_pool_benchmark": "completed",
        "seed": args.seed, "payload_size": args.payload_size, "point_reads": args.point_reads,
        "warm_up_per_layout": 1,
        "python": platform.python_version(), "os": platform.platform(), "processor": platform.processor(),
        "policy_setup": "32 pages, 8 frames, warm pages 0..7; 80% of requests target pages 0..3",
        "repeats_per_layout": args.repeats,
        "rows_per_run": args.rows,
        "plots_created": plots_created,
    }
    (args.output / "m1_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )

    print("\n[M1] Results written to:", args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
