"""Record encoding and checked page reads shared by layouts and indexes."""

import json
import math
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from storage.page import SlottedPage

Scalar = int | float | bool | str | None
MAX_RECORD_BYTES = SlottedPage.PAGE_SIZE - SlottedPage.HEADER_SIZE - SlottedPage.SLOT_SIZE


class PageFile(Protocol):
    """DiskManager operations used by Member 2, also usable by a buffer-pool adapter."""

    def get_page_count(self) -> int: ...
    def allocate_page(self) -> int: ...
    def read_page(self, page_id: int) -> bytes: ...
    def write_page(self, page_id: int, data: bytes) -> None: ...
    def close(self) -> None: ...


DiskFactory = Callable[[Path], PageFile]


@contextmanager
def new_page_file(path: Path, disk_factory: DiskFactory) -> Iterator[PageFile]:
    """Build a page file beside its destination and publish it after a successful close."""
    with NamedTemporaryFile(dir=path.parent, prefix=".pages-", delete=False) as temp:
        staging = Path(temp.name)
    try:
        with closing(disk_factory(staging)) as disk:
            yield disk
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def _check_values(values: Sequence[Scalar]) -> None:
    for value in values:
        if value is not None and type(value) not in (int, float, bool, str):
            raise TypeError("Fields must be integers, floats, booleans, strings or None")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Float fields must be finite")


def serialize_record(values: Sequence[Scalar]) -> bytes:
    """Encode a flat tuple as one UTF-8 record that fits in a team's page slot."""
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise TypeError("A record must be a sequence of fields")
    _check_values(values)
    data = json.dumps(list(values), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("Encoded record is too large for a slotted page")
    return data


def deserialize_record(data: bytes) -> tuple[Scalar, ...]:
    """Restore the order and scalar types of the encoded fields."""
    values = json.loads(data.decode("utf-8"))
    if not isinstance(values, list):
        raise ValueError("Expected an encoded array of fields")
    _check_values(values)
    return tuple(values)


def read_slotted_page(disk: PageFile, page_id: int) -> SlottedPage:
    page = SlottedPage.from_bytes(disk.read_page(page_id))
    if page.page_id != page_id:
        raise ValueError(f"Page ID mismatch at page {page_id}")
    return page
