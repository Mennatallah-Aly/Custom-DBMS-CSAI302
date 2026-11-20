"""Milestone 2 index benchmark for the current Custom-DBMS project.

Run from the repository root:
    python -m benchmarks.bench_m2

Compares the existing:
  * BPlusTree + BPlusTreeStorage snapshot persistence
  * LSMTree + its immutable SSTables
  * Full Table Scan using the existing RowLayout

The same generated rows and deterministic operation plan are used for all
three strategies. Indexed lookups include fetching the matching row from
RowLayout; full-scan lookups scan the same RowLayout and return the row.

Important implementation distinction:
  * BPlusTree.search() is an in-memory tree lookup. BPlusTreeStorage saves
    and reloads a full snapshot; it does not provide page-at-a-time searches.
  * LSMTree searches its persisted SSTables after an explicit flush/reopen.
The CSV records persistence/finalization separately from foreground
insertion and lookup latency so these costs are not conflated.
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
from pathlib import Path
from tempfile import TemporaryDirectory

# Allow both:
#   python -m benchmarks.bench_m2
#   python benchmarks/bench_m2.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from indexes.bplus_tree import BPlusTree
from indexes.bplus_tree_storage import BPlusTreeStorage
from indexes.lsm_tree import LSMTree
from storage.layouts import RowLayout
from benchmarks.charts import bar_chart


COLUMNS = ("id", "category", "payload")
STRATEGIES = ("B+ Tree", "LSM Tree", "Full Table Scan")
WORKLOADS = {
    "read-heavy": 0.90,   # 90% reads, 10% writes
    "write-heavy": 0.10,  # 10% reads, 90% writes
}


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


def _make_row(key: int, payload_size: int) -> tuple:
    return (key, f"group-{key % 10}", f"{key:08d}-" + ("x" * payload_size))


def _make_workload_plan(initial_rows: int, operation_count: int,
                        read_ratio: float, seed: int) -> list[tuple[str, int]]:
    """Create an identical deterministic read/write sequence for every strategy."""
    read_count = int(round(operation_count * read_ratio))
    write_count = operation_count - read_count
    operation_types = ["read"] * read_count + ["write"] * write_count
    rng = random.Random(seed)
    rng.shuffle(operation_types)

    known_keys = list(range(initial_rows))
    next_key = initial_rows
    plan = []
    for operation_type in operation_types:
        if operation_type == "read":
            key = rng.choice(known_keys)
            plan.append(("read", key))
        else:
            key = next_key
            next_key += 1
            known_keys.append(key)
            plan.append(("write", key))
    return plan


def _lsm_size_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.glob("*.sst") if path.is_file())


def _find_by_full_scan(table: RowLayout, key: int):
    for row in table.scan():
        if row[0] == key:
            return row
    return None


def _run_strategy(strategy: str, initial_rows: int, payload_size: int,
                  plan: list[tuple[str, int]], memtable_limit: int,
                  compaction_threshold: int, repeat: int,
                  workload_name: str) -> dict:
    with TemporaryDirectory(prefix="adb_bench_m2_") as temp_dir:
        root = Path(temp_dir)
        table_path = root / "table.db"
        bptree_path = root / "bplus_tree.db"
        lsm_directory = root / "lsm_index"

        result = {
            "workload": workload_name,
            "strategy": strategy,
            "repeat": repeat,
            "initial_rows": initial_rows,
            "operation_count": len(plan),
            "memtable_limit": memtable_limit if strategy == "LSM Tree" else "",
            "compaction_threshold": (
                compaction_threshold if strategy == "LSM Tree" else ""
            ),
        }

        with RowLayout(table_path, COLUMNS) as table:
            # Build exactly the same base table for each strategy.
            table_build_started = time.perf_counter_ns()
            initial_row_ids = []
            for key in range(initial_rows):
                row_id = table.insert(_make_row(key, payload_size))
                initial_row_ids.append(row_id)
            table.flush()
            table_build_ms = (time.perf_counter_ns() - table_build_started) / 1_000_000
            result["base_table_build_ms"] = table_build_ms

            index = None
            bptree_storage = None
            initial_index_insert_times = []
            initial_persist_ms = 0.0
            initial_index_size_bytes = 0
            initial_index_node_or_sstable_count = 0

            # Build the selected index using the project's real public API.
            if strategy == "B+ Tree":
                index = BPlusTree()  # Uses the agreed default max_keys=3.
                build_started = time.perf_counter_ns()
                for key, row_id in zip(range(initial_rows), initial_row_ids):
                    started = time.perf_counter_ns()
                    index.insert(key, row_id)
                    initial_index_insert_times.append(
                        (time.perf_counter_ns() - started) / 1_000_000
                    )
                result["index_build_insert_total_ms"] = (
                    time.perf_counter_ns() - build_started
                ) / 1_000_000

                bptree_storage = BPlusTreeStorage(bptree_path)
                persist_started = time.perf_counter_ns()
                save_info = bptree_storage.save(index)
                initial_persist_ms = (
                    time.perf_counter_ns() - persist_started
                ) / 1_000_000
                initial_index_size_bytes = bptree_path.stat().st_size
                initial_index_node_or_sstable_count = save_info["node_count"]

                # Reload the snapshot to verify the actual storage API and use
                # the persisted version for subsequent lookups.
                index = bptree_storage.load()
                if len(index) != initial_rows:
                    raise AssertionError("B+ Tree snapshot reload lost entries")

            elif strategy == "LSM Tree":
                index = LSMTree(
                    lsm_directory,
                    memtable_limit=memtable_limit,
                    compaction_threshold=compaction_threshold,
                )
                build_started = time.perf_counter_ns()
                for key, row_id in zip(range(initial_rows), initial_row_ids):
                    started = time.perf_counter_ns()
                    index.insert(key, row_id)
                    initial_index_insert_times.append(
                        (time.perf_counter_ns() - started) / 1_000_000
                    )
                result["index_build_insert_total_ms"] = (
                    time.perf_counter_ns() - build_started
                ) / 1_000_000

                persist_started = time.perf_counter_ns()
                index.flush()
                index.wait_for_compaction()
                initial_persist_ms = (
                    time.perf_counter_ns() - persist_started
                ) / 1_000_000
                index.close()

                initial_index_size_bytes = _lsm_size_bytes(lsm_directory)
                initial_index_node_or_sstable_count = len(
                    list(lsm_directory.glob("*.sst"))
                )

                # Reopen after flush: lookups now use persisted SSTables rather
                # than relying on the original in-memory MemTable.
                index = LSMTree(
                    lsm_directory,
                    memtable_limit=memtable_limit,
                    compaction_threshold=compaction_threshold,
                )

            else:
                # Full Table Scan has no separate index to build or store.
                result["index_build_insert_total_ms"] = None
                initial_persist_ms = None

            result["initial_index_persist_ms"] = initial_persist_ms
            result["initial_index_size_bytes"] = initial_index_size_bytes
            result["initial_index_pages_or_sstables"] = (
                initial_index_node_or_sstable_count
                if strategy != "Full Table Scan" else None
            )
            result["initial_index_insert_avg_ms"] = (
                _mean(initial_index_insert_times)
                if strategy != "Full Table Scan" else None
            )
            result["initial_index_insert_p95_ms"] = (
                _percentile(initial_index_insert_times, 0.95)
                if strategy != "Full Table Scan" else None
            )

            # Execute the same pre-generated read/write plan for every strategy.
            table_insert_times = []
            index_insert_times = []
            write_path_times = []
            lookup_times = []
            index_probe_times = []
            read_count = 0
            write_count = 0

            workload_started = time.perf_counter_ns()
            for operation_type, key in plan:
                if operation_type == "write":
                    row = _make_row(key, payload_size)
                    operation_started = time.perf_counter_ns()

                    table_started = time.perf_counter_ns()
                    row_id = table.insert(row)
                    table_insert_times.append(
                        (time.perf_counter_ns() - table_started) / 1_000_000
                    )

                    if strategy == "B+ Tree":
                        index_started = time.perf_counter_ns()
                        index.insert(key, row_id)
                        index_insert_times.append(
                            (time.perf_counter_ns() - index_started) / 1_000_000
                        )
                    elif strategy == "LSM Tree":
                        index_started = time.perf_counter_ns()
                        index.insert(key, row_id)
                        index_insert_times.append(
                            (time.perf_counter_ns() - index_started) / 1_000_000
                        )

                    write_path_times.append(
                        (time.perf_counter_ns() - operation_started) / 1_000_000
                    )
                    write_count += 1

                else:
                    operation_started = time.perf_counter_ns()
                    if strategy == "Full Table Scan":
                        row = _find_by_full_scan(table, key)
                    else:
                        probe_started = time.perf_counter_ns()
                        row_id = index.search(key)
                        index_probe_times.append(
                            (time.perf_counter_ns() - probe_started) / 1_000_000
                        )
                        row = table.get_row(row_id) if row_id is not None else None

                    elapsed_ms = (
                        time.perf_counter_ns() - operation_started
                    ) / 1_000_000
                    lookup_times.append(elapsed_ms)
                    if row != _make_row(key, payload_size):
                        raise AssertionError(
                            f"{strategy} returned an incorrect result for key {key}"
                        )
                    read_count += 1

            # Persist final table and index state. Persistence is reported
            # separately from per-operation foreground latency.
            table.flush()
            final_persist_started = time.perf_counter_ns()
            if strategy == "B+ Tree":
                final_save_info = bptree_storage.save(index)
                final_index_size_bytes = bptree_path.stat().st_size
                final_index_node_or_sstable_count = final_save_info["node_count"]
            elif strategy == "LSM Tree":
                index.flush()
                index.wait_for_compaction()
                final_index_size_bytes = _lsm_size_bytes(lsm_directory)
                final_index_node_or_sstable_count = len(
                    list(lsm_directory.glob("*.sst"))
                )
            else:
                final_index_size_bytes = 0
                final_index_node_or_sstable_count = 0

            final_persist_ms = (
                time.perf_counter_ns() - final_persist_started
            ) / 1_000_000

            result.update({
                "persisted_ops_per_second": len(plan) * 1e9 / (time.perf_counter_ns() - workload_started),
                "read_ops": read_count,
                "write_ops": write_count,
                "lookup_avg_ms": _mean(lookup_times),
                "lookup_p50_ms": _percentile(lookup_times, 0.50),
                "lookup_p95_ms": _percentile(lookup_times, 0.95),
                "index_probe_avg_ms": (
                    _mean(index_probe_times)
                    if strategy != "Full Table Scan" else None
                ),
                "table_insert_avg_ms": _mean(table_insert_times),
                "index_insert_avg_ms": (
                    _mean(index_insert_times)
                    if strategy != "Full Table Scan" else None
                ),
                "index_insert_p95_ms": (
                    _percentile(index_insert_times, 0.95)
                    if strategy != "Full Table Scan" else None
                ),
                "write_path_avg_ms": _mean(write_path_times),
                "write_path_p95_ms": _percentile(write_path_times, 0.95),
                "final_persist_ms": (
                    final_persist_ms if strategy != "Full Table Scan" else None
                ),
                "final_index_size_bytes": final_index_size_bytes,
                "final_index_pages_or_sstables": final_index_node_or_sstable_count,
                "table_size_bytes": table_path.stat().st_size,
            })

            if strategy == "LSM Tree":
                index.close()

        return result


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _summarize(raw_rows: list[dict]) -> list[dict]:
    metrics = [
        "lookup_avg_ms", "lookup_p95_ms", "index_probe_avg_ms",
        "table_insert_avg_ms", "index_insert_avg_ms",
        "write_path_avg_ms", "write_path_p95_ms", "persisted_ops_per_second",
        "index_build_insert_total_ms", "initial_index_persist_ms",
        "initial_index_size_bytes", "final_persist_ms",
        "final_index_size_bytes", "table_size_bytes",
    ]
    summary = []
    for workload_name in WORKLOADS:
        for strategy in STRATEGIES:
            group = [
                row for row in raw_rows
                if row["workload"] == workload_name and row["strategy"] == strategy
            ]
            if not group:
                continue
            item = {
                "workload": workload_name,
                "strategy": strategy,
                "runs": len(group),
                "read_ops_mean": _mean([float(row["read_ops"]) for row in group]),
                "write_ops_mean": _mean([float(row["write_ops"]) for row in group]),
            }
            for metric in metrics:
                values = [
                    float(row[metric]) for row in group
                    if row[metric] not in (None, "")
                ]
                item[f"{metric}_mean"] = _mean(values) if values else None
                item[f"{metric}_stddev"] = (
                    statistics.stdev(values) if len(values) > 1
                    else (0.0 if values else None)
                )
            summary.append(item)
    return summary


def _make_plots(summary_rows: list[dict], output_dir: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        for metric, title, unit in [
            ("lookup_avg_ms", "Lookup including row fetch", "Milliseconds"),
            ("write_path_avg_ms", "Foreground insert", "Milliseconds"),
            ("initial_index_size_bytes", "Index size after build", "Bytes"),
        ]:
            bar_chart(output_dir / f"m2_{metric}.svg", title, unit, [
                (r["workload"] + " / " + r["strategy"], r[metric + "_mean"], r[metric + "_stddev"])
                for r in summary_rows
            ])
        return True

    if not summary_rows:
        return False

    strategy_labels = list(STRATEGIES)
    workload_labels = list(WORKLOADS)
    width = 0.24
    x = list(range(len(workload_labels)))

    def grouped_plot(metric: str, title: str, ylabel: str, filename: str) -> None:
        fig, ax = plt.subplots(figsize=(10, 5))
        for index, strategy in enumerate(strategy_labels):
            values = []
            for workload in workload_labels:
                row = next(
                    item for item in summary_rows
                    if item["strategy"] == strategy and item["workload"] == workload
                )
                values.append(row[f"{metric}_mean"])
            offsets = [position + (index - 1) * width for position in x]
            ax.bar(offsets, values, width=width, label=strategy)
        ax.set_xticks(x, workload_labels)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        fig.savefig(output_dir / filename, dpi=160)
        plt.close(fig)

    grouped_plot(
        "lookup_avg_ms",
        "Milestone 2: end-to-end lookup latency",
        "Average lookup latency (ms)",
        "m2_lookup_latency.png",
    )
    grouped_plot(
        "write_path_avg_ms",
        "Milestone 2: foreground write-path latency",
        "Average write-path latency (ms)",
        "m2_write_latency.png",
    )
    grouped_plot(
        "final_index_size_bytes",
        "Milestone 2: final index size",
        "Index size (bytes; Full Table Scan = 0)",
        "m2_index_size.png",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare B+ Tree, LSM Tree and Full Table Scan."
    )
    parser.add_argument("--rows", type=int, default=500,
                        help="Initial table rows per run (default: 500).")
    parser.add_argument("--operations", type=int, default=200,
                        help="Read/write operations per workload (default: 200).")
    parser.add_argument("--payload-size", type=int, default=64,
                        help="Payload characters per row; keep <= 3000 (default: 64).")
    parser.add_argument("--repeats", type=int, default=5,
                        help="Repeated runs per strategy and workload (default: 5).")
    parser.add_argument("--memtable-limit", type=int, default=64,
                        help="LSM MemTable entry limit (default: 64).")
    parser.add_argument("--compaction-threshold", type=int, default=4,
                        help="LSM L0 compaction threshold (default: 4).")
    parser.add_argument("--seed", type=int, default=302,
                        help="Random seed (default: 302).")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/m2"),
                        help="Output directory (default: benchmark_results/m2).")
    args = parser.parse_args()

    if args.rows < 1 or args.operations < 1 or args.repeats < 1:
        parser.error("--rows, --operations and --repeats must be positive integers")
    if not 0 <= args.payload_size <= 3000:
        parser.error("--payload-size must be between 0 and 3000 characters")
    if args.memtable_limit < 1:
        parser.error("--memtable-limit must be at least 1")
    if args.compaction_threshold < 2:
        parser.error("--compaction-threshold must be at least 2")

    args.output.mkdir(parents=True, exist_ok=True)
    raw_rows = []

    for strategy in STRATEGIES:
        _run_strategy(strategy, 16, args.payload_size, [("read", 0), ("write", 16)],
                      args.memtable_limit, args.compaction_threshold, 0, "warm-up")
    for repeat in range(1, args.repeats + 1):
        for workload_index, (workload_name, read_ratio) in enumerate(WORKLOADS.items()):
            plan = _make_workload_plan(
                initial_rows=args.rows,
                operation_count=args.operations,
                read_ratio=read_ratio,
                seed=args.seed + repeat * 1009 + workload_index * 7919,
            )
            start = (repeat - 1) % len(STRATEGIES)
            for strategy in STRATEGIES[start:] + STRATEGIES[:start]:
                row = _run_strategy(
                    strategy=strategy,
                    initial_rows=args.rows,
                    payload_size=args.payload_size,
                    plan=plan,
                    memtable_limit=args.memtable_limit,
                    compaction_threshold=args.compaction_threshold,
                    repeat=repeat,
                    workload_name=workload_name,
                )
                raw_rows.append(row)
                print(
                    f"[M2] run {repeat}/{args.repeats} {workload_name:11s} "
                    f"{strategy:16s} "
                    f"lookup={row['lookup_avg_ms']:.4f} ms, "
                    f"write={row['write_path_avg_ms']:.4f} ms, "
                    f"index_size={row['final_index_size_bytes']} bytes"
                )

    summary_rows = _summarize(raw_rows)
    _write_csv(args.output / "m2_raw_results.csv", raw_rows)
    _write_csv(args.output / "m2_summary.csv", summary_rows)
    plots_created = _make_plots(summary_rows, args.output)

    status = {
        "seed": args.seed, "rows": args.rows, "operations": args.operations,
        "payload_size": args.payload_size, "memtable_limit": args.memtable_limit,
        "compaction_threshold": args.compaction_threshold, "warm_up_per_strategy": 1,
        "python": platform.python_version(), "os": platform.platform(), "processor": platform.processor(),
        "strategies": list(STRATEGIES),
        "workloads": {
            "read-heavy": "90% reads / 10% writes",
            "write-heavy": "10% reads / 90% writes",
        },
        "repeats_per_strategy_and_workload": args.repeats,
        "plots_created": plots_created,
        "methodology_notes": [
            "B+ Tree point searches are in-memory after loading a full BPlusTreeStorage snapshot.",
            "LSM Tree is explicitly flushed, compacted, closed and reopened before its initial lookup phase.",
            "Indexed lookup latency includes index search plus RowLayout.get_row.",
            "Full Table Scan lookup latency scans the same RowLayout and returns the matching row.",
            "Full Table Scan has no index; its index size is recorded as zero and its index build/insert metrics are not applicable.",
            "Final persistence time is reported separately from foreground write-path latency.",
        ],
    }
    (args.output / "m2_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )

    print("\n[M2] Results written to:", args.output.resolve())
    print("[M2] Note: B+ Tree snapshot persistence and LSM SSTable persistence are different mechanisms.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
