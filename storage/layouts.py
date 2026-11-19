"""Append-only NSM and DSM tables using the team's DiskManager and SlottedPage."""

import json
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Self

from storage.disk_manager import DiskManager
from storage.page import SlottedPage
from storage.records import (
    DiskFactory, Scalar, deserialize_record, new_page_file, read_slotted_page, serialize_record,
)


def _column_names(columns: Sequence[str]) -> tuple[str, ...]:
    if isinstance(columns, str) or not isinstance(columns, Sequence) or not columns:
        raise ValueError("Supply a non-empty sequence of column names")
    names = tuple(columns)
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Column names must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("Duplicate column names")
    return names


class _Layout:
    kind: str

    def __init__(self, path: str | Path, columns: Sequence[str] | None = None, *,
                 disk_factory: DiskFactory = DiskManager):
        path = Path(path)
        requested = _column_names(columns) if columns is not None else None
        size = path.stat().st_size if path.exists() else 0
        if size % SlottedPage.PAGE_SIZE:
            raise ValueError("Table file ends in a partial page")
        if size == 0:
            if requested is None:
                raise ValueError("Supply column names when creating a table")
            self.columns = requested
            with new_page_file(path, disk_factory) as disk:
                disk.allocate_page()
                disk.write_page(0, self._header(0))
        self._disk = disk_factory(path)
        self._closed = False
        try:
            header = read_slotted_page(self._disk, 0)
            meta = json.loads(header.get_record(0))
            if not isinstance(meta, dict) or meta.get("format") != self.kind:
                raise ValueError("Table layout does not match the requested layout")
            self.columns = _column_names(meta.get("columns", ()))
            if requested is not None and requested != self.columns:
                raise ValueError("Column names or order differ from the stored schema")
            groups = meta.get("groups")
            data_pages = self._disk.get_page_count() - 1
            self._width = 1 if self.kind == "NSM/1" else len(self.columns)
            if type(groups) is not int or groups < 0 or groups * self._width > data_pages:
                raise ValueError("Invalid number of committed page groups")
            # Each entry is the first logical row ID in a group. The last is the open group.
            self._starts = [0]
            for group in range(groups):
                page = read_slotted_page(self._disk, self._page_id(group, 0))
                if not page.slot_count:
                    raise ValueError("Committed group is empty")
                self._starts.append(self._starts[-1] + page.slot_count)
            self._pending = self._new_group()
        except BaseException:
            self._disk.close()
            raise

    def _header(self, groups: int) -> bytes:
        info = {"format": self.kind, "columns": self.columns, "groups": groups}
        page = SlottedPage(0)
        page.insert_record(json.dumps(info, separators=(",", ":")).encode("utf-8"))
        return page.to_bytes()

    def _page_id(self, group: int, stream: int) -> int:
        return 1 + group * self._width + stream

    def _new_group(self) -> list[SlottedPage]:
        group = len(self._starts) - 1
        return [SlottedPage(self._page_id(group, col)) for col in range(self._width)]

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("Table is closed")

    def __len__(self) -> int:
        return self._starts[-1] + self._pending[0].slot_count

    def insert(self, row: Sequence[Scalar]) -> int:
        """Append a row and return its stable, zero-based logical row ID."""
        self._check_open()
        if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
            raise TypeError("A row must be a sequence of fields")
        if len(row) != len(self.columns):
            raise ValueError("Row width differs from the schema")
        # Validate every field before touching any column's page.
        if self.kind == "NSM/1":
            records = [serialize_record(row)]
        else:
            records = [serialize_record((value,)) for value in row]
        if any(not p.has_space(len(data)) for p, data in zip(self._pending, records)):
            self.flush()
        row_id = len(self)
        for page, data in zip(self._pending, records):
            page.insert_record(data)
        return row_id

    def flush(self) -> None:
        """Publish a complete group. Readers use only groups named by page zero."""
        self._check_open()
        if not self._pending[0].slot_count:
            return
        end = len(self)
        header = self._header(len(self._starts))
        for page in self._pending:
            if page.page_id == self._disk.get_page_count():
                self._disk.allocate_page()
            self._disk.write_page(page.page_id, page.to_bytes())
        self._disk.write_page(0, header)
        self._starts.append(end)
        self._pending = self._new_group()

    def _page(self, group: int, stream: int) -> SlottedPage:
        if group == len(self._starts) - 1:
            return self._pending[stream]
        page = read_slotted_page(self._disk, self._page_id(group, stream))
        if page.slot_count != self._starts[group + 1] - self._starts[group]:
            raise ValueError("Mismatched row counts in a committed group")
        return page

    def _positions(self, columns: Sequence[str] | None) -> tuple[int, ...]:
        if columns is None:
            return tuple(range(len(self.columns)))
        return tuple(self.columns.index(name) for name in _column_names(columns))

    def _project(self, group: int, slots: range, positions: tuple[int, ...]) -> Iterator[tuple]:
        if self.kind == "NSM/1":
            page = self._page(group, 0)
            for slot in slots:
                row = deserialize_record(page.get_record(slot))
                yield tuple(row[pos] for pos in positions)
        else:
            pages = [self._page(group, pos) for pos in positions]
            for slot in slots:
                yield tuple(deserialize_record(page.get_record(slot))[0] for page in pages)

    def scan(self, columns: Sequence[str] | None = None) -> Iterator[tuple]:
        """Yield rows in insertion order, optionally projecting named columns in order."""
        self._check_open()
        positions = self._positions(columns)
        for group, (first, end) in enumerate(zip(self._starts, self._starts[1:] + [len(self)])):
            if first < end:
                yield from self._project(group, range(end - first), positions)

    def get_row(self, row_id: int, columns: Sequence[str] | None = None) -> tuple:
        self._check_open()
        if type(row_id) is not int or not 0 <= row_id < len(self):
            raise IndexError("Row ID is outside the table")
        group = bisect_right(self._starts, row_id) - 1
        slot = row_id - self._starts[group]
        return next(self._project(group, range(slot, slot + 1), self._positions(columns)))

    def close(self) -> None:
        if not self._closed:
            self.flush()
            self._disk.close()
            self._closed = True

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RowLayout(_Layout):
    """NSM: all fields of one row share a slot in one page."""

    kind = "NSM/1"


class ColumnLayout(_Layout):
    """DSM: each column has its own page in a group. Matching slots form a row."""

    kind = "DSM/1"
