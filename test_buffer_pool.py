import unittest
from storage.buffer_pool import BufferPoolManager


class MockDiskManager:
    """In-memory mock of DiskManager for isolated unit testing."""

    def __init__(self, page_size: int = 64) -> None:
        self.page_size = page_size
        self.disk: dict[int, bytes] = {
            i: f"PAGE_{i}".encode().ljust(page_size, b"\x00") for i in range(10)
        }
        self.writes = 0

    def read_page(self, page_id: int) -> bytes:
        return self.disk[page_id]

    def write_page(self, page_id: int, data: bytes) -> None:
        self.disk[page_id] = data
        self.writes += 1


class TestBufferPoolManager(unittest.TestCase):
    def test_lru_eviction_order(self) -> None:
        disk = MockDiskManager()
        bpm = BufferPoolManager(pool_size=2, disk_manager=disk, policy="LRU")

        bpm.fetch_page(1)
        bpm.unpin_page(1)
        bpm.fetch_page(2)
        bpm.unpin_page(2)

        # Access page 1 again so page 2 becomes the least recently used
        bpm.fetch_page(1)
        bpm.unpin_page(1)

        # Fetching page 3 must evict page 2, keeping page 1 in cache
        bpm.fetch_page(3)
        bpm.unpin_page(3)

        self.assertIn(1, bpm.frames)
        self.assertIn(3, bpm.frames)
        self.assertNotIn(2, bpm.frames)

    def test_fifo_eviction_order(self) -> None:
        disk = MockDiskManager()
        bpm = BufferPoolManager(pool_size=2, disk_manager=disk, policy="FIFO")

        bpm.fetch_page(1)
        bpm.unpin_page(1)
        bpm.fetch_page(2)
        bpm.unpin_page(2)

        # Accessing page 1 again does NOT change FIFO order
        bpm.fetch_page(1)
        bpm.unpin_page(1)

        # Fetching page 3 must evict page 1 (first-in)
        bpm.fetch_page(3)
        bpm.unpin_page(3)

        self.assertIn(2, bpm.frames)
        self.assertIn(3, bpm.frames)
        self.assertNotIn(1, bpm.frames)

    def test_dirty_page_flushed_on_eviction(self) -> None:
        disk = MockDiskManager()
        bpm = BufferPoolManager(pool_size=1, disk_manager=disk, policy="LRU")

        buf = bpm.fetch_page(1)
        buf[0:8] = b"MODIFIED"
        bpm.unpin_page(1, is_dirty=True)

        # Trigger eviction of page 1 by fetching page 2
        bpm.fetch_page(2)
        bpm.unpin_page(2)

        self.assertEqual(disk.writes, 1)
        self.assertTrue(disk.disk[1].startswith(b"MODIFIED"))

    def test_pinned_page_cannot_be_evicted(self) -> None:
        disk = MockDiskManager()
        bpm = BufferPoolManager(pool_size=1, disk_manager=disk, policy="LRU")

        bpm.fetch_page(1)  # Pinned (pin_count == 1)
        with self.assertRaises(RuntimeError):
            bpm.fetch_page(2)


if __name__ == "__main__":
    unittest.main()