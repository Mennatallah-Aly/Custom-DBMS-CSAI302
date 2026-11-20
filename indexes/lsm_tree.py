"""Two-level LSM index with immutable slotted-page runs and background compaction."""

import heapq
from bisect import bisect_right
from collections.abc import Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from random import Random
from threading import RLock
from typing import Self

from storage.disk_manager import DiskManager
from storage.page import SlottedPage
from storage.records import (
    DiskFactory, deserialize_record, new_page_file, read_slotted_page, serialize_record,
)

Key = int | str
Value = int | float | bool | str | tuple[int, int]
Entry = tuple[Key, bytes]


class _Node:
    def __init__(self, key: Key, data: bytes, height: int):
        self.key = key
        self.data = data
        self.forward: list[_Node | None] = [None] * height


class _SkipList:
    """Ordered MemTable. Level zero links every key in flush order."""

    def __init__(self, capacity: int):
        self._height = capacity.bit_length()
        self._random = Random(302)  # Reproducible promotions, independent of workload RNG.
        self.clear()

    def clear(self) -> None:
        self._head = _Node(0, b"", self._height)  # The sentinel key is never compared.
        self.size = 0

    def _locate(self, key: Key, previous: list[_Node] | None = None) -> _Node | None:
        node = self._head
        for level in reversed(range(self._height)):
            nxt = node.forward[level]
            while nxt is not None and nxt.key < key:
                node = nxt
                nxt = node.forward[level]
            if previous is not None:
                previous[level] = node
        match = node.forward[0]
        return match if match is not None and match.key == key else None

    def put(self, key: Key, data: bytes) -> None:
        previous = [self._head] * self._height
        match = self._locate(key, previous)
        if match is not None:
            match.data = data
            return
        height = 1
        while height < self._height and self._random.getrandbits(1):
            height += 1
        node = _Node(key, data, height)
        for level in range(height):
            node.forward[level] = previous[level].forward[level]
            previous[level].forward[level] = node
        self.size += 1

    def get(self, key: Key) -> bytes | None:
        node = self._locate(key)
        return node.data if node is not None else None

    def entries(self) -> Generator[Entry, None, None]:
        node = self._head.forward[0]
        while node is not None:
            yield node.key, node.data
            node = node.forward[0]


def _encode(key: Key, value: Value) -> bytes:
    if isinstance(value, tuple):
        if len(value) != 2 or any(type(n) is not int or n < 0 for n in value):
            raise ValueError("A physical record ID is a (page_id, slot_id) pair")
        return serialize_record((key, *value))
    if value is None:
        raise ValueError("None is reserved for a missing key")
    return serialize_record((key, value))


def _decode(data: bytes) -> tuple[Key, Value]:
    fields = deserialize_record(data)
    if not fields or type(fields[0]) not in (int, str):
        raise ValueError("Invalid index key on disk")
    key = fields[0]
    if len(fields) == 2 and fields[1] is not None:
        return key, fields[1]
    if len(fields) == 3 and all(type(n) is int and n >= 0 for n in fields[1:]):
        return key, (fields[1], fields[2])
    raise ValueError("Invalid index entry on disk")


def _generation(path: Path) -> int:
    return int(path.stem.split("-")[1])


@dataclass
class _SSTable:
    path: Path
    first_keys: list[Key]  # One entry per data page.
    last_key: Key
    disk_factory: DiskFactory

    @classmethod
    def open(cls, path: Path, disk_factory: DiskFactory) -> Self:
        size = path.stat().st_size
        if not size or size % SlottedPage.PAGE_SIZE:
            raise ValueError(f"Incomplete SSTable: {path.name}")
        first_keys = []
        with closing(disk_factory(path)) as disk:
            for page_id in range(disk.get_page_count()):
                page = read_slotted_page(disk, page_id)
                first, _ = _decode(page.get_record(0))
                first_keys.append(first)
            last, _ = _decode(page.get_record(page.slot_count - 1))
        return cls(path, first_keys, last, disk_factory)

    @classmethod
    def write(cls, path: Path, entries: Iterable[Entry], disk_factory: DiskFactory) -> Self:
        """Publish a sorted, complete run by renaming a file on the same filesystem."""
        first_keys = []
        with new_page_file(path, disk_factory) as disk:
            page = SlottedPage(0)
            for key, data in entries:
                if not page.has_space(len(data)):
                    disk.allocate_page()
                    disk.write_page(page.page_id, page.to_bytes())
                    page = SlottedPage(page.page_id + 1)
                if page.slot_count == 0:
                    first_keys.append(key)
                page.insert_record(data)
                last_key = key
            if not first_keys:
                raise ValueError("Cannot publish an empty SSTable")
            disk.allocate_page()
            disk.write_page(page.page_id, page.to_bytes())
        return cls(path, first_keys, last_key, disk_factory)

    def search(self, key: Key) -> Value | None:
        if key < self.first_keys[0] or key > self.last_key:
            return None
        page_id = bisect_right(self.first_keys, key) - 1
        with closing(self.disk_factory(self.path)) as disk:
            page = read_slotted_page(disk, page_id)
        low, high = 0, page.slot_count
        while low < high:
            mid = (low + high) // 2
            stored, value = _decode(page.get_record(mid))
            if key == stored:
                return value
            if key < stored:
                high = mid
            else:
                low = mid + 1
        return None

    def entries(self) -> Generator[Entry, None, None]:
        with closing(self.disk_factory(self.path)) as disk:
            for page_id in range(len(self.first_keys)):
                page = read_slotted_page(disk, page_id)
                for slot in range(page.slot_count):
                    data = page.get_record(slot)
                    key, _ = _decode(data)
                    yield key, data


def _merge_entries(newest_first: list[_SSTable]) -> Generator[Entry, None, None]:
    with ExitStack() as stack:
        streams = [stack.enter_context(closing(run.entries())) for run in newest_first]
        # heapq.merge is stable. For equal keys the first stream holds the newest value.
        ordered = heapq.merge(*streams, key=itemgetter(0))
        for _, versions in groupby(ordered, key=itemgetter(0)):
            yield next(versions)


class LSMTree:
    """Point index. Use one live instance per directory, shared by its caller threads."""

    def __init__(self, directory: str | Path, memtable_limit: int = 128,
                 compaction_threshold: int = 4, *, disk_factory: DiskFactory = DiskManager):
        if type(memtable_limit) is not int or memtable_limit < 1:
            raise ValueError("memtable_limit must be a positive integer")
        if type(compaction_threshold) is not int or compaction_threshold < 2:
            raise ValueError("compaction_threshold must be an integer of at least two")
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._limit = memtable_limit
        self._threshold = compaction_threshold
        self._disk_factory = disk_factory
        self._memtable = _SkipList(memtable_limit)
        self._lock = RLock()
        self._closed = False

        # L1's generation covers all L0 inputs up to that generation. This also
        # handles a restart between publishing a merge and unlinking its inputs.
        base_path = max(self._directory.glob("L1-*.sst"), key=_generation, default=None)
        covered = _generation(base_path) if base_path else 0
        self._base = _SSTable.open(base_path, disk_factory) if base_path else None
        recent = sorted(self._directory.glob("L0-*.sst"), key=_generation)
        self._level0 = [_SSTable.open(path, disk_factory) for path in recent
                        if _generation(path) > covered]
        newest = self._level0[-1] if self._level0 else self._base
        self._next_generation = _generation(newest.path) + 1 if newest else 1
        self._key_type = type(newest.first_keys[0]) if newest else None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lsm-merge")
        self._job: Future | None = None
        with self._lock:
            self._schedule_compaction()

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("Index is closed")

    def _check_key(self, key: Key) -> None:
        if type(key) not in (int, str) or self._key_type not in (None, type(key)):
            raise TypeError("Use only integer keys or only string keys within one index")

    def insert(self, key: Key, value: Value) -> None:
        """Insert or replace a value. A full sorted MemTable becomes one L0 SSTable."""
        with self._lock:
            self._check_open()
            self._check_key(key)
            data = _encode(key, value)
            self._key_type = type(key)
            self._memtable.put(key, data)
            if self._memtable.size >= self._limit:
                self._flush_locked()

    def search(self, key: Key) -> Value | None:
        """Check memory, newest L0 files first, then the consolidated L1 file."""
        with self._lock:
            self._check_open()
            self._check_key(key)
            data = self._memtable.get(key)
            if data is not None:
                return _decode(data)[1]
            for run in reversed(self._level0):
                value = run.search(key)
                if value is not None:
                    return value
            return self._base.search(key) if self._base else None

    def _flush_locked(self) -> None:
        if self._memtable.size:
            path = self._directory / f"L0-{self._next_generation:016d}.sst"
            run = _SSTable.write(path, self._memtable.entries(), self._disk_factory)
            self._level0.append(run)
            self._next_generation += 1
            self._memtable.clear()
        self._schedule_compaction()

    def flush(self) -> None:
        """Persist all pending writes. A failed run write leaves its MemTable intact."""
        with self._lock:
            self._check_open()
            self._flush_locked()

    def _schedule_compaction(self) -> None:
        if self._job is None and len(self._level0) >= self._threshold:
            self._job = self._executor.submit(self._compact)

    def _compact(self) -> None:
        while True:
            with self._lock:
                if len(self._level0) < self._threshold:
                    self._job = None
                    return
                inputs = list(reversed(self._level0[:self._threshold]))
                count = len(inputs)
                generation = _generation(inputs[0].path)
                if self._base:
                    inputs.append(self._base)
            # Foreground lookups and flushes can proceed during the streaming merge.
            path = self._directory / f"L1-{generation:016d}.sst"
            with closing(_merge_entries(inputs)) as entries:
                merged = _SSTable.write(path, entries, self._disk_factory)
            with self._lock:
                self._base = merged
                del self._level0[:count]
            for run in inputs:
                run.path.unlink(missing_ok=True)

    def wait_for_compaction(self) -> None:
        """Wait for scheduled work and surface any background I/O error."""
        with self._lock:
            job = self._job
        if job is not None:
            try:
                job.result()
            except Exception:
                with self._lock:
                    if self._job is job:
                        self._job = None  # An explicit flush can retry.
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._flush_locked()
                self._closed = True
        try:
            self.wait_for_compaction()
        finally:
            self._executor.shutdown(wait=True)

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
