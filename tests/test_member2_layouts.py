import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from storage.disk_manager import DiskManager
from storage.layouts import ColumnLayout, RowLayout
from storage.page import SlottedPage
from storage.records import MAX_RECORD_BYTES


class LayoutTests(unittest.TestCase):
    def test_legacy_full_schema_page_survives_group_count_growth(self):
        for layout in (RowLayout, ColumnLayout):
            with self.subTest(layout=layout.__name__):
                path = self.root / layout.__name__
                # Page zero from the original writer, with no room for another digit.
                meta = {"format": layout.kind, "columns": [""], "groups": 0}
                overhead = len(json.dumps(meta, separators=(",", ":")).encode())
                name = "x" * (MAX_RECORD_BYTES - overhead)
                meta["columns"] = [name]
                header = SlottedPage(0)
                header.insert_record(json.dumps(meta, separators=(",", ":")).encode())
                path.write_bytes(header.to_bytes())
                with layout(path) as table:
                    for i in range(12):
                        table.insert((i,))
                        table.flush()
                with layout(path, [name]) as table:
                    self.assertEqual(list(table.scan()), [(i,) for i in range(12)])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_same_typed_rows_and_stable_ids_after_reopen(self):
        columns = ("id", "name", "score", "active", "note")
        rows = [(1, "منى", 4.5, True, None), (2, "", -2.0, False, "x,\ny")]
        for layout in (RowLayout, ColumnLayout):
            with self.subTest(layout=layout.__name__):
                path = self.root / layout.__name__
                with layout(path, columns) as table:
                    self.assertEqual([table.insert(row) for row in rows], [0, 1])
                    self.assertEqual(list(table.scan()), rows)
                with layout(path) as table:
                    self.assertEqual(table.columns, columns)
                    self.assertEqual(list(table.scan()), rows)
                    self.assertIs(type(table.get_row(0)[3]), bool)
                    self.assertEqual(table.get_row(1, ["name", "id"]), ("", 2))
                    self.assertEqual(table.insert((3, "A", 0.0, True, None)), 2)
                with layout(path) as table:
                    self.assertEqual(list(table.scan(["id"])), [(1,), (2,), (3,)])

    def test_multiple_groups_read_only_requested_column_pages(self):
        path = self.root / "columns.db"
        with ColumnLayout(path, ["id", "text"]) as table:
            for i in range(11):
                table.insert((i, "a" * 1300))
        read_pages = []
        original_read = DiskManager.read_page

        def trace_read(disk, page_id):
            raw = original_read(disk, page_id)
            read_pages.append(SlottedPage.from_bytes(raw))
            return raw

        with ColumnLayout(path) as table:
            with patch.object(DiskManager, "read_page", trace_read):
                self.assertEqual(list(table.scan(["id"])), [(i,) for i in range(11)])
            self.assertGreater(len(read_pages), 1)
            for page in read_pages:
                for slot in range(page.slot_count):
                    self.assertNotIn(b"aaaa", page.get_record(slot))
            self.assertEqual(table.get_row(7), (7, "a" * 1300))

    def test_rejects_invalid_row_before_any_column_is_inserted(self):
        for layout in (RowLayout, ColumnLayout):
            with self.subTest(layout=layout.__name__):
                path = self.root / layout.__name__
                with layout(path, ["id", "label"]) as table:
                    table.insert((1, "before"))
                    for invalid in [(2,), (2, object()), (2, "x" * 5000), (2, float("nan"))]:
                        with self.assertRaises((TypeError, ValueError)):
                            table.insert(invalid)
                    self.assertEqual(table.insert((3, "after")), 1)
                    with self.assertRaises(IndexError):
                        table.get_row(-1)
                    with self.assertRaises(ValueError):
                        list(table.scan(["unknown"]))
                with layout(path, ["id", "label"]) as table:
                    self.assertEqual(list(table.scan()), [(1, "before"), (3, "after")])
                with self.assertRaises(ValueError):
                    layout(path, ["label", "id"])
                other = ColumnLayout if layout is RowLayout else RowLayout
                with self.assertRaises(ValueError):
                    other(path)

    def test_unpublished_partial_column_group_is_invisible_and_can_be_retried(self):
        path = self.root / "columns.db"
        with ColumnLayout(path, ["id", "name"]) as writer:
            writer.insert((1, "first"))
            writer.flush()
            writer.insert((2, "pending"))
            original_write = DiskManager.write_page

            def fail_on_metadata(disk, page_id, data):
                if page_id == 0:
                    raise OSError("simulated commit failure")
                original_write(disk, page_id, data)

            with patch.object(DiskManager, "write_page", fail_on_metadata):
                with self.assertRaises(OSError):
                    writer.flush()
            with ColumnLayout(path) as reader:
                self.assertEqual(list(reader.scan()), [(1, "first")])
            writer.flush()
        with ColumnLayout(path) as table:
            self.assertEqual(list(table.scan()), [(1, "first"), (2, "pending")])

    def test_close_can_be_retried_after_io_failure(self):
        path = self.root / "retry.db"
        table = RowLayout(path, ["id"])
        self.addCleanup(table.close)
        table.insert((7,))
        with patch.object(DiskManager, "write_page", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                table.close()
        table.close()
        with RowLayout(path) as table:
            self.assertEqual(list(table.scan()), [(7,)])

    def test_failed_creation_can_be_retried_at_the_same_path(self):
        path = self.root / "new.db"
        with patch.object(DiskManager, "write_page", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ColumnLayout(path, ["id"])
        with ColumnLayout(path, ["id"]) as table:
            self.assertEqual(table.insert((9,)), 0)
        with ColumnLayout(path) as table:
            self.assertEqual(table.get_row(0), (9,))
