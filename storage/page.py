
import struct
import zlib


class SlottedPage:
    """
    A fixed-size database page using a slotted-page layout.

    Records grow from the end of the page backwards.
    The slot directory grows from the beginning forwards.
    """

    PAGE_SIZE = 4096
    MAGIC = b"DBPG"
    VERSION = 1

    HEADER_FORMAT = "<4sHHIHHHIH"
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

    SLOT_FORMAT = "<HHB3s"
    SLOT_SIZE = struct.calcsize(SLOT_FORMAT)

    ACTIVE = 0
    DELETED = 1

    CHECKSUM_OFFSET = 18

    def __init__(self, page_id=0):
        self.data = bytearray(self.PAGE_SIZE)

        self.page_id = page_id
        self.slot_count = 0
        self.lower = self.HEADER_SIZE
        self.upper = self.PAGE_SIZE
        self.flags = 0

        self._write_header()

    def _write_header(self, checksum=0):
        """Write page metadata into the binary header."""

        struct.pack_into(
            self.HEADER_FORMAT,
            self.data,
            0,
            self.MAGIC,
            self.VERSION,
            self.flags,
            self.page_id,
            self.slot_count,
            self.lower,
            self.upper,
            checksum,
            0
        )

    def _read_slot(self, slot_id):
        """Read a slot entry from the slot directory."""

        if slot_id < 0 or slot_id >= self.slot_count:
            raise ValueError("Invalid slot ID")

        position = self.HEADER_SIZE + slot_id * self.SLOT_SIZE

        return struct.unpack_from(
            self.SLOT_FORMAT,
            self.data,
            position
        )

    def _write_slot(self, slot_id, offset, length, status):
        """Update a slot entry."""

        position = self.HEADER_SIZE + slot_id * self.SLOT_SIZE

        struct.pack_into(
            self.SLOT_FORMAT,
            self.data,
            position,
            offset,
            length,
            status,
            b"\x00" * 3
        )

    def get_free_space(self):
        """Return the current contiguous free space in bytes."""
        return self.upper - self.lower

    def has_space(self, record_size):
        """Check whether a record can fit in the page."""

        if record_size < 0:
            return False

        deleted_slot_exists = any(
            self._read_slot(i)[2] == self.DELETED
            for i in range(self.slot_count)
        )

        required_space = record_size

        if not deleted_slot_exists:
            required_space += self.SLOT_SIZE

        return self.get_free_space() >= required_space

    def insert_record(self, record):
        """
        Insert a bytes record and return its slot ID.

        Deleted slots are reused before creating new slots.
        """

        if not isinstance(record, (bytes, bytearray)):
            raise TypeError("Record must be bytes or bytearray")

        record = bytes(record)

        if len(record) == 0:
            raise ValueError("Empty records are not allowed")

        if len(record) > self.PAGE_SIZE - self.HEADER_SIZE - self.SLOT_SIZE:
            raise ValueError("Record is too large for this page")

        slot_id = None

        for i in range(self.slot_count):
            if self._read_slot(i)[2] == self.DELETED:
                slot_id = i
                break

        new_slot_needed = slot_id is None
        required_space = len(record)

        if new_slot_needed:
            required_space += self.SLOT_SIZE

        # but the free region is fragmented.
        if self.get_free_space() < required_space:
            self.compact()

        if self.get_free_space() < required_space:
            raise MemoryError("Not enough free space in the page")

        if new_slot_needed:
            slot_id = self.slot_count
            self.slot_count += 1
            self.lower += self.SLOT_SIZE

        self.upper -= len(record)
        offset = self.upper

        self.data[offset:offset + len(record)] = record

        self._write_slot(
            slot_id,
            offset,
            len(record),
            self.ACTIVE
        )

        self._write_header()

        return slot_id

    def get_record(self, slot_id):
        """Retrieve a record using its slot ID."""

        offset, length, status, _ = self._read_slot(slot_id)

        if status == self.DELETED:
            raise ValueError("Record has been deleted")

        if status != self.ACTIVE:
            raise ValueError("Invalid slot status")

        return bytes(self.data[offset:offset + length])

    def delete_record(self, slot_id):
        """Mark a record as deleted without moving other records."""

        offset, length, status, _ = self._read_slot(slot_id)

        if status == self.DELETED:
            raise ValueError("Record is already deleted")

        if status != self.ACTIVE:
            raise ValueError("Invalid slot status")

        self._write_slot(
            slot_id,
            0,
            0,
            self.DELETED
        )

        self._write_header()

        return True

    def compact(self):
        """
        Remove fragmented space by moving active records together.
        Slot IDs remain unchanged.
        """

        old_data = self.data
        new_data = bytearray(self.PAGE_SIZE)

        # Preserve the header and slot directory.
        new_data[:self.lower] = old_data[:self.lower]

        new_upper = self.PAGE_SIZE

        for slot_id in range(self.slot_count):
            offset, length, status, _ = self._read_slot(slot_id)

            if status == self.DELETED:
                continue

            record = old_data[offset:offset + length]

            new_upper -= length
            new_data[new_upper:new_upper + length] = record

            position = self.HEADER_SIZE + slot_id * self.SLOT_SIZE

            struct.pack_into(
                self.SLOT_FORMAT,
                new_data,
                position,
                new_upper,
                length,
                self.ACTIVE,
                b"\x00" * 3
            )

        self.data = new_data
        self.upper = new_upper

        self._write_header()

    def to_bytes(self):
        """Serialize the complete page into exactly PAGE_SIZE bytes."""

        self._write_header(checksum=0)

        checksum = zlib.crc32(self.data) & 0xFFFFFFFF

        self._write_header(checksum=checksum)

        return bytes(self.data)

    @classmethod
    def from_bytes(cls, raw_data):
        """Reconstruct a page from serialized bytes."""

        if len(raw_data) != cls.PAGE_SIZE:
            raise ValueError("Invalid page size")

        page = cls.__new__(cls)
        page.data = bytearray(raw_data)

        (
            magic,
            version,
            flags,
            page_id,
            slot_count,
            lower,
            upper,
            stored_checksum,
            reserved
        ) = struct.unpack_from(cls.HEADER_FORMAT, page.data, 0)

        if magic != cls.MAGIC:
            raise ValueError("Invalid page magic")

        if version != cls.VERSION:
            raise ValueError("Unsupported page version")

        if lower != cls.HEADER_SIZE + slot_count * cls.SLOT_SIZE:
            raise ValueError("Invalid slot directory boundary")

        if lower > upper or upper > cls.PAGE_SIZE:
            raise ValueError("Invalid free space boundaries")


        page.data[cls.CHECKSUM_OFFSET:cls.CHECKSUM_OFFSET + 4] = b"\x00" * 4

        calculated_checksum = zlib.crc32(page.data) & 0xFFFFFFFF

        if calculated_checksum != stored_checksum:
            raise ValueError("Page checksum mismatch")

        page.page_id = page_id
        page.slot_count = slot_count
        page.lower = lower
        page.upper = upper
        page.flags = flags

        page.validate()


        struct.pack_into(
            "<I",
            page.data,
            cls.CHECKSUM_OFFSET,
            stored_checksum
        )

        return page

    def validate(self):
        """Check the structural integrity of the page."""

        if self.lower != self.HEADER_SIZE + self.slot_count * self.SLOT_SIZE:
            raise ValueError("Invalid slot directory")

        if self.lower > self.upper:
            raise ValueError("Page boundaries overlap")

        occupied_ranges = []

        for slot_id in range(self.slot_count):
            offset, length, status, _ = self._read_slot(slot_id)

            if status == self.DELETED:
                if offset != 0 or length != 0:
                    raise ValueError("Invalid deleted slot")
                continue

            if status != self.ACTIVE:
                raise ValueError("Unknown slot status")

            if length == 0:
                raise ValueError("Empty active record")

            if offset < self.upper or offset + length > self.PAGE_SIZE:
                raise ValueError("Record is outside the data area")

            occupied_ranges.append((offset, offset + length))

        occupied_ranges.sort()

        for i in range(1, len(occupied_ranges)):
            if occupied_ranges[i][0] < occupied_ranges[i - 1][1]:
                raise ValueError("Overlapping records detected")

        return True
