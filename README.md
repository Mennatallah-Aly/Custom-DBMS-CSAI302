# Custom-DBMS-CSAI302
Custom Database Management System – CSAI 302 Course Project, Fall 2026

## Member 4 · persistence, object storage and benchmarks

Use `ObjectStore(directory).put(blob)` for large byte strings. Store the returned
object ID in a regular table field and pass it to `get(object_id)` after reopening.
Each immutable object occupies its own file of checksummed 4096-byte slotted pages.
The header records the plaintext and stored lengths. Publication occurs after all
chunks are written. The current API processes one complete blob in memory.

```python
from storage.object_store import ObjectStore

objects = ObjectStore("documents")
document_id = objects.put(b"example document")
assert objects.get(document_id) == b"example document"
```

An optional pair of byte-to-byte callbacks, `encrypt=` and `decrypt=`, protects
the payload before writing and restores it after reading. The caller supplies the
encryption scheme and keys. The object ID and header lengths remain visible.
The AES-GCM integration test uses an ephemeral key and requires `cryptography`.

These Member 4 modules rely on the team's storage.layouts, indexes.lsm_tree, and storage.buffer_pool components. Merge or check out the other members' work before running the integrated tests and benchmarks.

Run the tests and reproduce both sets of measurements from the repository root.
The replacement-policy experiment uses Member 3's `storage/buffer_pool.py`.

```powershell
python -m unittest discover -s tests -v
python -m benchmarks.bench_m1
python -m benchmarks.bench_m2
```

The scripts write raw per-run CSVs, summaries, environment and workload settings,
and plots under `benchmark_results/m1` and `benchmark_results/m2` by default. Sample outputs checked into this branch are under `benchmarks/benchmark_results/`. Both use
seed 302, warm-up, and five measured repetitions per configuration by default.
The M1 policy test compares identical page sequences on eight frames. Its hit/miss
ratios describe the DBMS buffer pool. Page I/O timings measure calls to DiskManager
and include effects of the operating system's file cache.

M2 starts with 500 rows and replays 200 operations, with either 90% reads or
90% writes. Indexed lookup timing includes fetching the matching row. The B+
Tree searches a reloaded snapshot in memory, while the LSM can read SSTable
pages. The CSV records foreground operations and persistence costs separately.
