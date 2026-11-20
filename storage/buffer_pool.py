"""Fixed-size page buffer pool with LRU/FIFO replacement.

The class also implements the DiskManager page-file API so it can be supplied
as ``disk_factory`` to storage.layouts and indexes.lsm_tree:

    from storage.buffer_pool import BufferPoolManager

    factory = lambda path: BufferPoolManager(
        path, pool_size=32, replacement_policy="LRU"
    )
    table = RowLayout(path, columns, disk_factory=factory)

Pages returned by fetch_page()/pin_page() are mutable bytearrays. A caller
must pair every successful fetch_page() with unpin_page(), and pass
is_dirty=True if it modified the page. Dirty pages are written on eviction,
flush_page(), flush_all_pages(), or close().

The DiskManager-compatible write_page() method is write-through: it updates
the cache and the underlying file before returning, preserving the existing
page-file visibility contract used by Member 2's components.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator

from storage.disk_manager import DiskManager
from storage.page import SlottedPage


class BufferPoolFullError(RuntimeError):
    """Raised when every frame is pinned and no page can be evicted."""


@dataclass
class _Frame:
    page_id: int
    data: bytearray
    pin_count: int = 0
    dirty_flag: bool = False


class BufferPoolManager:
    """Cache database pages in a fixed number of memory frames.

    Args:
        file_path: Database file managed by the existing DiskManager.
        pool_size: Maximum number of resident pages (memory frames).
        replacement_policy: ``"LRU"`` or ``"FIFO"``.
        page_size: Must remain the team's agreed 4096-byte page size.

    DiskManager-compatible methods:
        create_file, get_page_count, allocate_page, read_page, write_page,
        close.

    Buffer-pool methods:
        fetch_page/pin_page, unpin_page, new_page, flush_page,
        flush_all_pages, get_stats, reset_stats.
    """

    def __init__(
        self,
        file_path: str | Path,
        page_size: int = SlottedPage.PAGE_SIZE,
        pool_size: int = 16,
        replacement_policy: str = "LRU",
    ) -> None:
        if type(page_size) is not int or page_size != SlottedPage.PAGE_SIZE:
            raise ValueError(
                f"page_size must be the shared {SlottedPage.PAGE_SIZE}-byte page size"
            )
        if type(pool_size) is not int or pool_size < 1:
            raise ValueError("pool_size must be a positive integer")
        if not isinstance(replacement_policy, str):
            raise TypeError("replacement_policy must be 'LRU' or 'FIFO'")

        policy = replacement_policy.strip().upper()
        if policy not in {"LRU", "FIFO"}:
            raise ValueError("replacement_policy must be either 'LRU' or 'FIFO'")

        self.file_path = str(file_path)
        self.page_size = page_size
        self.pool_size = pool_size
        self.replacement_policy = policy

        # Reuse the team's DiskManager; this class is only a cache/adapter.
        self.disk_manager = DiskManager(self.file_path, page_size=page_size)

        # OrderedDict preserves frame load order for FIFO. LRU moves a frame
        # to the end whenever it is accessed.
        self._frames: OrderedDict[int, _Frame] = OrderedDict()
        self._page_table: dict[int, _Frame] = {}
        self._lock = RLock()
        self._closed = False

        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "disk_reads": 0,
            "disk_writes": 0,
            "evictions": 0,
            "dirty_evictions": 0,
            "page_allocations": 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("BufferPoolManager is closed")

    @staticmethod
    def _validate_page_id(page_id: int) -> None:
        if type(page_id) is not int or page_id < 0:
            raise ValueError("page_id must be a non-negative integer")

    def _validate_existing_page(self, page_id: int) -> None:
        self._validate_page_id(page_id)
        if page_id >= self.disk_manager.get_page_count():
            raise ValueError("Invalid page ID")

    def _touch(self, page_id: int) -> None:
        if self.replacement_policy == "LRU":
            self._frames.move_to_end(page_id)

    def _flush_frame(self, frame: _Frame) -> bool:
        """Write one dirty frame to disk. Return True if a write occurred."""
        if not frame.dirty_flag:
            return False

        self.disk_manager.write_page(frame.page_id, bytes(frame.data))
        frame.dirty_flag = False
        self._stats["disk_writes"] += 1
        return True

    def _ensure_free_frame(self) -> None:
        """Make room for one frame, evicting the oldest eligible frame."""
        if len(self._frames) < self.pool_size:
            return

        victim_id = None
        victim = None

        # FIFO: oldest loaded frame first. LRU: least recently used frame
        # first, because accesses move frames to the end of the OrderedDict.
        for candidate_id, candidate in self._frames.items():
            if candidate.pin_count == 0:
                victim_id = candidate_id
                victim = candidate
                break

        if victim is None or victim_id is None:
            raise BufferPoolFullError(
                "All buffer-pool frames are pinned; unpin a page before fetching another"
            )

        was_dirty = victim.dirty_flag
        self._flush_frame(victim)
        if was_dirty:
            self._stats["dirty_evictions"] += 1

        del self._frames[victim_id]
        del self._page_table[victim_id]
        self._stats["evictions"] += 1

    def _load_frame(self, page_id: int) -> _Frame:
        """Return a resident frame, loading it from disk on a cache miss."""
        self._check_open()
        self._validate_existing_page(page_id)

        frame = self._page_table.get(page_id)
        if frame is not None:
            self._stats["cache_hits"] += 1
            self._touch(page_id)
            return frame

        self._stats["cache_misses"] += 1
        self._ensure_free_frame()

        data = self.disk_manager.read_page(page_id)
        if len(data) != self.page_size:
            raise ValueError(
                f"DiskManager returned {len(data)} bytes; expected {self.page_size}"
            )

        frame = _Frame(page_id=page_id, data=bytearray(data))
        self._frames[page_id] = frame
        self._page_table[page_id] = frame
        self._stats["disk_reads"] += 1
        return frame

    def _new_frame_for_write(self, page_id: int) -> _Frame:
        """Get/create a frame for a full-page write without reading old bytes."""
        self._check_open()
        self._validate_existing_page(page_id)

        frame = self._page_table.get(page_id)
        if frame is not None:
            self._stats["cache_hits"] += 1
            self._touch(page_id)
            return frame

        self._stats["cache_misses"] += 1
        self._ensure_free_frame()
        frame = _Frame(page_id=page_id, data=bytearray(self.page_size))
        self._frames[page_id] = frame
        self._page_table[page_id] = frame
        return frame

    # ------------------------------------------------------------------
    # DiskManager-compatible page-file API
    # ------------------------------------------------------------------

    def create_file(self) -> None:
        with self._lock:
            self._check_open()
            self.disk_manager.create_file()

    def get_page_count(self) -> int:
        with self._lock:
            self._check_open()
            return self.disk_manager.get_page_count()

    def allocate_page(self) -> int:
        """Allocate a zero-filled page and return its page ID."""
        with self._lock:
            self._check_open()
            page_id = self.disk_manager.allocate_page()
            self._stats["page_allocations"] += 1
            # DiskManager initializes the page with zero bytes and flushes it.
            self._stats["disk_writes"] += 1
            return page_id

    def read_page(self, page_id: int) -> bytes:
        """Return exactly one page as immutable bytes, using the cache."""
        with self._lock:
            frame = self._load_frame(page_id)
            return bytes(frame.data)

    def write_page(self, page_id: int, data: bytes) -> None:
        """Write through to disk, preserving DiskManager's visibility contract."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")

        raw = bytes(data)
        if len(raw) > self.page_size:
            raise ValueError("Data is larger than the page size")
        raw = raw.ljust(self.page_size, b"\x00")

        with self._lock:
            frame = self._new_frame_for_write(page_id)
            frame.data[:] = raw
            frame.dirty_flag = True
            # DiskManager.write_page() is write-through. Keep that behavior
            # for layouts/LSM that use this class as a disk_factory adapter.
            self._flush_frame(frame)

    # ------------------------------------------------------------------
    # Buffer-pool API
    # ------------------------------------------------------------------

    def fetch_page(self, page_id: int) -> bytearray:
        """Pin a page in memory and return its mutable page-sized bytearray.

        The caller must call unpin_page(page_id, is_dirty=...) exactly once
        for each successful fetch_page() call.
        """
        with self._lock:
            frame = self._load_frame(page_id)
            frame.pin_count += 1
            return frame.data

    def pin_page(self, page_id: int) -> bytearray:
        """Alias for fetch_page(), provided for conventional buffer-pool APIs."""
        return self.fetch_page(page_id)

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> bool:
        """Release one pin and optionally mark the page dirty."""
        self._validate_page_id(page_id)
        if not isinstance(is_dirty, bool):
            raise TypeError("is_dirty must be a bool")

        with self._lock:
            self._check_open()
            frame = self._page_table.get(page_id)
            if frame is None:
                raise ValueError(f"Page {page_id} is not resident in the buffer pool")
            if frame.pin_count == 0:
                raise ValueError(f"Page {page_id} is not pinned")

            if is_dirty:
                frame.dirty_flag = True
            frame.pin_count -= 1
            return True

    def new_page(self) -> tuple[int, bytearray]:
        """Allocate a page, pin it, and return (page_id, mutable_page_bytes).

        Capacity is checked before allocating on disk, so an all-pinned pool
        cannot leave behind an allocated page when no frame is available.
        """
        with self._lock:
            self._check_open()
            self._ensure_free_frame()
            page_id = self.allocate_page()
            return page_id, self.fetch_page(page_id)

    @contextmanager
    def pinned_page(self, page_id: int, *, is_dirty: bool = False) -> Iterator[bytearray]:
        """Context manager that safely pairs fetch_page() with unpin_page()."""
        data = self.fetch_page(page_id)
        try:
            yield data
        finally:
            self.unpin_page(page_id, is_dirty=is_dirty)

    def flush_page(self, page_id: int) -> bool:
        """Flush one resident dirty page; return False if it is not resident."""
        self._validate_page_id(page_id)
        with self._lock:
            self._check_open()
            frame = self._page_table.get(page_id)
            if frame is None:
                return False
            return self._flush_frame(frame)

    def flush_all_pages(self) -> int:
        """Flush every dirty frame and return the number of pages written."""
        with self._lock:
            self._check_open()
            flushed = 0
            for frame in self._frames.values():
                if self._flush_frame(frame):
                    flushed += 1
            return flushed

    def flush(self) -> int:
        """Alias for flush_all_pages()."""
        return self.flush_all_pages()

    # ------------------------------------------------------------------
    # Statistics for Member 4's cache benchmarks
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, int | float | str]:
        """Return a snapshot of cache, I/O, and frame statistics."""
        with self._lock:
            self._check_open()
            accesses = self._stats["cache_hits"] + self._stats["cache_misses"]
            hits = self._stats["cache_hits"]
            misses = self._stats["cache_misses"]

            result: dict[str, int | float | str] = dict(self._stats)
            result.update({
                "replacement_policy": self.replacement_policy,
                "pool_size": self.pool_size,
                "resident_pages": len(self._frames),
                "pinned_pages": sum(frame.pin_count > 0 for frame in self._frames.values()),
                "dirty_pages": sum(frame.dirty_flag for frame in self._frames.values()),
                "hit_ratio": hits / accesses if accesses else 0.0,
                "miss_ratio": misses / accesses if accesses else 0.0,
                "cache_hit_ratio": hits / accesses if accesses else 0.0,
                "cache_miss_ratio": misses / accesses if accesses else 0.0,
            })
            return result

    def reset_stats(self) -> None:
        """Reset counters without clearing cached pages or changing their state."""
        with self._lock:
            self._check_open()
            for key in self._stats:
                self._stats[key] = 0

    def get_frame_info(self, page_id: int) -> dict[str, int | bool] | None:
        """Return diagnostic state for one resident page, or None if not cached."""
        self._validate_page_id(page_id)
        with self._lock:
            self._check_open()
            frame = self._page_table.get(page_id)
            if frame is None:
                return None
            return {
                "page_id": frame.page_id,
                "pin_count": frame.pin_count,
                "dirty_flag": frame.dirty_flag,
            }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush dirty pages and close the underlying DiskManager."""
        with self._lock:
            if self._closed:
                return
            # A pinned page may still be in use by its caller. Refuse to close
            # rather than invalidate an outstanding mutable page reference.
            if any(frame.pin_count > 0 for frame in self._frames.values()):
                raise RuntimeError("Cannot close BufferPoolManager while pages are pinned")

            # If flushing fails, leave the manager open so the caller can
            # inspect/retry instead of silently discarding dirty frames.
            self.flush_all_pages()
            self.disk_manager.close()
            self._closed = True
            self._frames.clear()
            self._page_table.clear()

    def __enter__(self) -> BufferPoolManager:
        with self._lock:
            self._check_open()
            return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
    