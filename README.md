# Custom-DBMS-CSAI302
Custom Database Management System – CSAI 302 Course Project, Fall 2026

## Member 2 · table layouts and LSM indexing

Run from the repository root with Python 3.11 or later. These modules use the standard library and the team's existing `DiskManager` and `SlottedPage`.

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from indexes.lsm_tree import LSMTree
from storage.layouts import ColumnLayout, RowLayout

with TemporaryDirectory() as directory:
    root = Path(directory)
    with ColumnLayout(root / "students.db", ["id", "name"]) as students:
        with LSMTree(root / "by-id") as by_id:
            row_id = students.insert((17, "Mona"))
            by_id.insert(17, row_id)

    with ColumnLayout(root / "students.db") as students, LSMTree(root / "by-id") as by_id:
        assert students.get_row(by_id.search(17)) == (17, "Mona")
        assert list(students.scan(["name"])) == [("Mona",)]
```

### M1 · persistent NSM and DSM

`RowLayout(path, columns)` packs a whole row into one slot. `ColumnLayout(path, columns)` puts each column on a separate page within a group. The same slot number across the group identifies one row. Both return a stable integer row ID from `insert(row)` and support `get_row(row_id, columns=None)` and `scan(columns=None)`. A scan yields tuples in insertion order with fields in the requested order. A DSM projection reads only the selected column pages during the scan.

Page zero stores the layout, ordered column names and committed group count. Both layouts use the shared 4096-byte page format and CRC. Fields support integers, finite floats, booleans, Unicode text and null. The codec writes flat UTF-8 JSON arrays and restores tuples. Each encoded NSM row or DSM cell must fit in one slot (4064 bytes).

Pending rows are published when the next row no longer fits, on an explicit `flush()`, or on `close()`. Data pages are written before the group count in page zero. A flush seals the group, so frequent flushing can leave unused space. Reopening loads the stored schema and reads one page per committed group to rebuild the row-ID directory. Use one writer per table and serialize access to a layout instance.

### M2 · LSM point index

`LSMTree(directory, memtable_limit=128, compaction_threshold=4)` supports `insert(key, value)` and `search(key)`. Use either integer keys or string keys throughout an index. Values can be logical row IDs, non-null scalar fields, or physical `(page_id, slot_id)` pairs. Inserting an existing key updates its value. Missing keys return `None`.

The MemTable is a skip list. Its upper levels accelerate point lookup and insertion, while its bottom level streams distinct keys in sorted order. Replacing a value keeps the same node. Reaching the distinct-key limit flushes an immutable, sorted L0 SSTable. A point lookup searches memory, L0 from newest to oldest, then L1. Each run keeps its first key per page in memory, so a lookup reads at most one candidate page per run.

Once enough L0 files accumulate, a background worker merges the oldest batch with L1. It streams sorted entries, keeps the newest value per key, and atomically renames the completed file before retiring its inputs. A new L1 filename records the highest generation it covers. Reopening ignores older covered files if cleanup was interrupted. Writes arriving during the merge remain searchable.

Use one live index instance per directory. Threads may share that instance. `flush()` persists the MemTable, `wait_for_compaction()` waits for scheduled merges and reports worker errors, and `close()` does both. Unflushed writes are held in memory. Transactional crash recovery and multi-process writers are outside these two modules.

### Team integration and tests

Member 3 can call `insert` and `search` from the index manager. Both layouts and the LSM accept an optional `disk_factory(path)` for a buffer-pool adapter that preserves the existing page I/O contract, including write order and visibility. The default uses `DiskManager` directly. Member 4 can run the same projection on both layouts or compare an indexed lookup with a table scan. For on-disk size comparisons, call `flush()` and `wait_for_compaction()` before measuring the index directory.

Run Member 2's correctness and integration tests from the repository root. The tests use temporary databases.

```powershell
python -m unittest discover -s tests -p "test_member2_*.py" -v
```

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

Run the tests and reproduce both sets of measurements from the repository root.
The replacement-policy experiment uses Member 3's `storage/buffer_pool.py`.

```powershell
python -m unittest discover -s tests -v
python -m benchmarks.bench_m1
python -m benchmarks.bench_m2
```

The scripts write raw per-run CSVs, summaries, environment and workload settings,
and plots under `benchmark_results/m1` and `benchmark_results/m2`. Both use
seed 302, warm-up, and five measured repetitions per configuration by default.
The M1 policy test compares identical page sequences on eight frames. Its hit/miss
ratios describe the DBMS buffer pool. Page I/O timings measure calls to DiskManager
and include effects of the operating system's file cache.

M2 starts with 500 rows and replays 200 operations, with either 90% reads or
90% writes. Indexed lookup timing includes fetching the matching row. The B+
Tree searches a reloaded snapshot in memory, while the LSM can read SSTable
pages. The CSV records foreground operations and persistence costs separately.
