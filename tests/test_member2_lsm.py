import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from indexes.lsm_tree import LSMTree
from storage.disk_manager import DiskManager
from storage.layouts import ColumnLayout, RowLayout
from storage.page import SlottedPage


class LSMTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_newest_value_across_memory_disk_and_reopen(self):
        with LSMTree(self.root, memtable_limit=2) as index:
            index.insert(8, 0)
            self.assertEqual(index.search(8), 0)
            index.insert(3, (1, 4))
            self.assertEqual(index.search(3), (1, 4))
            index.insert(8, "new")
            self.assertEqual(index.search(8), "new")
            self.assertIsNone(index.search(9))
        with LSMTree(self.root) as index:
            self.assertEqual(index.search(3), (1, 4))
            self.assertEqual(index.search(8), "new")
            self.assertIsNone(index.search(9))

    def test_sparse_lookup_reads_only_one_data_page_in_a_multi_page_run(self):
        keys = list(range(0, 80, 2))
        random.Random(42).shuffle(keys)
        with LSMTree(self.root) as index:
            for key in keys:
                index.insert(key, str(key) + "x" * 1000)
        run, = self.root.glob("L0-*.sst")
        self.assertGreater(run.stat().st_size, SlottedPage.PAGE_SIZE)
        pages = []
        original_read = DiskManager.read_page

        def trace_read(disk, page_id):
            pages.append(page_id)
            return original_read(disk, page_id)

        with LSMTree(self.root) as index:
            with patch.object(DiskManager, "read_page", trace_read):
                self.assertEqual(index.search(58), "58" + "x" * 1000)
            self.assertEqual(len(pages), 1)
            self.assertIsNone(index.search(57))
            self.assertIsNone(index.search(-1))

    def test_new_writes_win_while_background_compaction_is_reading(self):
        entered, resume = threading.Event(), threading.Event()
        caller = threading.current_thread()
        original_read = DiskManager.read_page

        def pause_worker_read(disk, page_id):
            if threading.current_thread() is not caller and not entered.is_set():
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError("Foreground work blocked behind the merge")
            return original_read(disk, page_id)

        with LSMTree(self.root, memtable_limit=3, compaction_threshold=2) as index:
            index.insert(1, "old")
            index.insert(2, "before merge")
            index.insert(5, "unchanged")
            first_run, = self.root.glob("L0-*.sst")
            old_bytes = first_run.read_bytes()
            with patch.object(DiskManager, "read_page", pause_worker_read):
                index.insert(1, "middle")
                index.insert(2, "revised in L1")
                index.insert(3, "also keep")
                try:
                    self.assertTrue(entered.wait(5), "Compaction never started in the background")
                    self.assertEqual(index.search(1), "middle")
                    index.insert(1, "newest")
                    index.insert(4, "later run")
                    index.insert(6, "after snapshot")
                    self.assertEqual(index.search(1), "newest")
                finally:
                    resume.set()
                index.wait_for_compaction()
            self.assertEqual(len(list(self.root.glob("L1-*.sst"))), 1)
            self.assertFalse(first_run.exists())
        # Model a restart after publishing L1 but before deleting an old input.
        first_run.write_bytes(old_bytes)
        with LSMTree(self.root) as index:
            self.assertEqual([index.search(k) for k in range(1, 7)],
                             ["newest", "revised in L1", "also keep", "later run",
                              "unchanged", "after snapshot"])

    def test_validation_and_flush_failure_keep_existing_values(self):
        with LSMTree(self.root) as index:
            index.insert("a", False)
            for key, value in [(1, "mixed"), (True, "boolean key"), ("a", None),
                               ("a", "x" * 5000), ("a", (1, -2))]:
                with self.assertRaises((TypeError, ValueError)):
                    index.insert(key, value)
            self.assertIs(index.search("a"), False)
            with patch.object(DiskManager, "write_page", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    index.flush()
            self.assertEqual(list(self.root.glob("*.sst")), [])
            self.assertIs(index.search("a"), False)
            index.flush()
        with LSMTree(self.root) as index:
            self.assertIs(index.search("a"), False)
            with self.assertRaises(TypeError):
                index.insert(2, "different kind")

    def test_index_matches_full_scan_for_both_persistent_layouts(self):
        for layout in (RowLayout, ColumnLayout):
            path = self.root / layout.__name__
            directory = self.root / (layout.__name__ + "-index")
            with self.subTest(layout=layout.__name__):
                with layout(path, ["id", "name"]) as table:
                    with LSMTree(directory, memtable_limit=4, compaction_threshold=2) as index:
                        for i in range(60):
                            key = i % 19
                            index.insert(key, table.insert((key, f"name-{i}")))
                with layout(path) as table, LSMTree(directory) as index:
                    reference = {row[0]: row for row in table.scan()}
                    for key, row in reference.items():
                        self.assertEqual(table.get_row(index.search(key)), row)
                    self.assertIsNone(index.search(999))

    def test_layout_and_index_route_io_through_the_supplied_page_backend(self):
        opened = []

        def open_disk(path):
            opened.append(Path(path).name)
            return DiskManager(path)

        table_path = self.root / "rows.db"
        index_path = self.root / "index"
        with RowLayout(table_path, ["id"], disk_factory=open_disk) as table:
            with LSMTree(index_path, memtable_limit=1, compaction_threshold=2,
                         disk_factory=open_disk) as index:
                for key in ("a", "b"):
                    index.insert(key, table.insert((key,)))
        opened.clear()
        with LSMTree(index_path, disk_factory=open_disk) as index:
            with RowLayout(table_path, disk_factory=open_disk) as table:
                self.assertEqual(table.get_row(index.search("b")), ("b",))
        self.assertIn("rows.db", opened)
        self.assertTrue(any(name.startswith("L1-") for name in opened))

    def test_background_merge_error_surfaces_and_inputs_remain_readable(self):
        caller = threading.current_thread()
        original_write = DiskManager.write_page

        def fail_worker_write(disk, page_id, data):
            if threading.current_thread() is not caller:
                raise OSError("failed merge")
            return original_write(disk, page_id, data)

        with LSMTree(self.root, memtable_limit=1, compaction_threshold=2) as index:
            with patch.object(DiskManager, "write_page", fail_worker_write):
                index.insert("x", "old")
                index.insert("x", "latest")
                with self.assertRaises(OSError):
                    index.wait_for_compaction()
            self.assertEqual(index.search("x"), "latest")
            self.assertEqual(len(list(self.root.glob("L0-*.sst"))), 2)
            index.flush()
            index.wait_for_compaction()
        with LSMTree(self.root) as index:
            self.assertEqual(index.search("x"), "latest")
