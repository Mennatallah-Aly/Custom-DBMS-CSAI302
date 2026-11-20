from abc import ABC, abstractmethod
from collections import OrderedDict, deque


class ReplacementPolicy(ABC):
    """Abstract base class for buffer pool page replacement policies."""

    @abstractmethod
    def record_access(self, page_id: int) -> None:
        pass

    @abstractmethod
    def add_page(self, page_id: int) -> None:
        pass

    @abstractmethod
    def remove_page(self, page_id: int) -> None:
        pass

    @abstractmethod
    def evict(self, pin_counts: dict[int, int]) -> int | None:
        """Return a victim page_id with pin_count == 0, or None if all pages are pinned."""
        pass


class LRUPolicy(ReplacementPolicy):
    """Least Recently Used (LRU) page replacement policy."""

    def __init__(self) -> None:
        self.order: OrderedDict[int, bool] = OrderedDict()

    def record_access(self, page_id: int) -> None:
        if page_id in self.order:
            self.order.move_to_end(page_id)

    def add_page(self, page_id: int) -> None:
        self.order[page_id] = True
        self.order.move_to_end(page_id)

    def remove_page(self, page_id: int) -> None:
        self.order.pop(page_id, None)

    def evict(self, pin_counts: dict[int, int]) -> int | None:
        for page_id in list(self.order.keys()):
            if pin_counts.get(page_id, 0) == 0:
                self.order.pop(page_id)
                return page_id
        return None


class FIFOPolicy(ReplacementPolicy):
    """First-In, First-Out (FIFO) page replacement policy."""

    def __init__(self) -> None:
        self.queue: deque[int] = deque()

    def record_access(self, page_id: int) -> None:
        # FIFO order does not change on subsequent accesses
        pass

    def add_page(self, page_id: int) -> None:
        if page_id not in self.queue:
            self.queue.append(page_id)

    def remove_page(self, page_id: int) -> None:
        if page_id in self.queue:
            self.queue.remove(page_id)

    def evict(self, pin_counts: dict[int, int]) -> int | None:
        for page_id in list(self.queue):
            if pin_counts.get(page_id, 0) == 0:
                self.queue.remove(page_id)
                return page_id
        return None


class BufferPoolManager:
    """Manages fixed-size memory frames backed by DiskManager."""

    def __init__(self, pool_size: int, disk_manager, policy: str = "LRU") -> None:
        if pool_size <= 0:
            raise ValueError("pool_size must be a positive integer.")

        self.pool_size = pool_size
        self.disk_manager = disk_manager
        self.frames: dict[int, bytearray] = {}
        self.pin_counts: dict[int, int] = {}
        self.dirty_flags: dict[int, bool] = {}

        # Metrics for M1 benchmarks (Cache Hit/Miss Ratio)
        self.hits = 0
        self.misses = 0

        policy_name = policy.strip().upper()
        if policy_name == "LRU":
            self.policy: ReplacementPolicy = LRUPolicy()
        elif policy_name == "FIFO":
            self.policy = FIFOPolicy()
        else:
            raise ValueError(f"Unsupported replacement policy: '{policy}'. Use 'LRU' or 'FIFO'.")

    def fetch_page(self, page_id: int) -> bytearray:
        """Fetch a page from cache or load it from disk into a frame."""
        if page_id in self.frames:
            self.hits += 1
            self.pin_counts[page_id] += 1
            self.policy.record_access(page_id)
            return self.frames[page_id]

        # Cache miss: load from disk
        self.misses += 1
        if len(self.frames) >= self.pool_size:
            victim_id = self.policy.evict(self.pin_counts)
            if victim_id is None:
                raise RuntimeError("Buffer pool is full and all frames are currently pinned.")
            self._evict_page(victim_id)

        raw_bytes = self.disk_manager.read_page(page_id)
        frame_buffer = bytearray(raw_bytes)
        self.frames[page_id] = frame_buffer
        self.pin_counts[page_id] = 1
        self.dirty_flags[page_id] = False
        self.policy.add_page(page_id)
        return frame_buffer

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> None:
        """Decrement pin_count and mark the page dirty if modified."""
        if page_id not in self.frames:
            raise KeyError(f"Page {page_id} is not currently in the buffer pool.")
        if self.pin_counts[page_id] > 0:
            self.pin_counts[page_id] -= 1
        if is_dirty:
            self.dirty_flags[page_id] = True

    def flush_page(self, page_id: int) -> None:
        """Write a dirty page back to disk and clear its dirty flag."""
        if page_id in self.frames and self.dirty_flags.get(page_id, False):
            self.disk_manager.write_page(page_id, bytes(self.frames[page_id]))
            self.dirty_flags[page_id] = False

    def flush_all(self) -> None:
        """Flush all dirty pages in the pool to disk."""
        for page_id in list(self.frames.keys()):
            self.flush_page(page_id)

    def _evict_page(self, page_id: int) -> None:
        """Flush if dirty, then remove page metadata from the pool."""
        self.flush_page(page_id)
        del self.frames[page_id]
        del self.pin_counts[page_id]
        del self.dirty_flags[page_id]
        self.policy.remove_page(page_id)

    def hit_ratio(self) -> float:
        """Return cache hit ratio in [0.0, 1.0]."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def reset_stats(self) -> None:
        """Reset hit/miss counters for clean benchmark runs."""
        self.hits = 0
        self.misses = 0