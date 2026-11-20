"""Store large binary objects separately from rows, using the shared page format."""

import struct
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from storage.disk_manager import DiskManager
from storage.page import SlottedPage
from storage.records import MAX_RECORD_BYTES, DiskFactory, new_page_file, read_slotted_page


class ObjectStore:
    """Put a blob and store the returned ID in a normal row or column field."""

    _header = struct.Struct("<4sBQQ")

    def __init__(self, directory: str | Path, *, encrypt: Callable[[bytes], bytes] | None = None,
                 decrypt: Callable[[bytes], bytes] | None = None, disk_factory: DiskFactory = DiskManager):
        if (encrypt is None) != (decrypt is None):
            raise ValueError("Supply encrypt and decrypt together")
        self._root = Path(directory)
        self._root.mkdir(parents=True, exist_ok=True)
        self._encrypt = encrypt
        self._decrypt = decrypt
        self._disk_factory = disk_factory

    def put(self, data: bytes) -> str:
        """Publish an immutable object only after every page has been written."""
        if not isinstance(data, bytes):
            raise TypeError("Object data must be bytes")
        payload = self._encrypt(data) if self._encrypt is not None else data
        if not isinstance(payload, bytes):
            raise TypeError("Encryption hook must return bytes")
        object_id = uuid4().hex
        with new_page_file(self._root / (object_id + ".db"), self._disk_factory) as disk:
            page = SlottedPage(disk.allocate_page())
            page.insert_record(self._header.pack(b"OBJ1", self._encrypt is not None,
                                                 len(data), len(payload)))
            disk.write_page(page.page_id, page.to_bytes())
            for start in range(0, len(payload), MAX_RECORD_BYTES):
                page = SlottedPage(disk.allocate_page())
                page.insert_record(payload[start:start + MAX_RECORD_BYTES])
                disk.write_page(page.page_id, page.to_bytes())
        return object_id

    def get(self, object_id: str) -> bytes:
        if not isinstance(object_id, str) or UUID(object_id).hex != object_id:
            raise ValueError("Expected a canonical object ID returned by put()")
        path = self._root / (object_id + ".db")
        size = path.stat().st_size  # A missing object must not create an empty database.
        with closing(self._disk_factory(path)) as disk:
            meta = read_slotted_page(disk, 0).get_record(0)
            if len(meta) != self._header.size:
                raise ValueError("Invalid object header")
            magic, encrypted, original_size, stored_size = self._header.unpack(meta)
            pages = (stored_size + MAX_RECORD_BYTES - 1) // MAX_RECORD_BYTES
            if magic != b"OBJ1" or size != (1 + pages) * SlottedPage.PAGE_SIZE:
                raise ValueError("Invalid object format or file length")
            if encrypted != int(self._decrypt is not None):
                raise ValueError("Encryption hooks do not match this object")
            payload = b"".join(read_slotted_page(disk, i).get_record(0) for i in range(1, pages + 1))
        if len(payload) != stored_size:
            raise ValueError("Incomplete object payload")
        data = self._decrypt(payload) if self._decrypt is not None else payload
        if not isinstance(data, bytes) or len(data) != original_size:
            raise ValueError("Invalid decoded object")
        return data
