

import os
import json

from storage.disk_manager import DiskManager
from storage.page import SlottedPage
from storage.buffer_pool import BufferPoolManager


class BPlusTreeStorage:
    """
    Save and load B+ Trees using DiskManager and BufferPoolManager.

    Page 0: Tree metadata.
    Pages 1 onwards: B+ Tree nodes.
    """

    def __init__(self, file_path, pool_size=16, policy="LRU"):
        self.file_path = file_path
        self.pool_size = pool_size
        self.policy = policy

    def save(self, tree):
        """
        Save a complete B+ Tree snapshot using the buffer pool.
        """

        metadata, node_pages = tree.serialize_pages(first_page_id=1)

        # Replace the old database file.
        if os.path.exists(self.file_path):
            os.remove(self.file_path)

        disk = DiskManager(
            self.file_path,
            page_size=SlottedPage.PAGE_SIZE
        )

        buffer_pool = BufferPoolManager(
            pool_size=self.pool_size,
            disk_manager=disk,
            policy=self.policy
        )

        try:
            # Allocate metadata page (page 0).
            metadata_page_id = disk.allocate_page()

            if metadata_page_id != 0:
                raise ValueError("Metadata page must be page 0")

            metadata_page = SlottedPage(page_id=metadata_page_id)
            metadata_page.insert_record(metadata)

            # Write metadata through the buffer pool.
            frame = buffer_pool.fetch_page(metadata_page_id)
            frame[:] = metadata_page.to_bytes()
            buffer_pool.unpin_page(metadata_page_id, is_dirty=True)

            # Allocate and write B+ Tree node pages.
            for page_id, node_data in node_pages.items():

                allocated_page_id = disk.allocate_page()

                if allocated_page_id != page_id:
                    raise ValueError(
                        f"Unexpected page ID: {allocated_page_id}"
                    )

                page = SlottedPage(page_id=page_id)
                page.insert_record(node_data)

                frame = buffer_pool.fetch_page(page_id)
                frame[:] = page.to_bytes()
                buffer_pool.unpin_page(page_id, is_dirty=True)

            # Persist all dirty pages to disk.
            buffer_pool.flush_all()

            return {
                "root_page_id": tree.root.page_id,
                "node_count": len(node_pages),
                "page_count": disk.get_page_count(),
                "buffer_pool_hit_ratio": buffer_pool.hit_ratio()
            }

        finally:
            buffer_pool.flush_all()
            disk.close()

    def load(self):
        """
        Load a B+ Tree snapshot using the buffer pool.
        """

        if not os.path.exists(self.file_path):
            raise FileNotFoundError("Database file does not exist")

        disk = DiskManager(
            self.file_path,
            page_size=SlottedPage.PAGE_SIZE
        )

        buffer_pool = BufferPoolManager(
            pool_size=self.pool_size,
            disk_manager=disk,
            policy=self.policy
        )

        try:
            if disk.get_page_count() < 1:
                raise ValueError("Database file is empty")

            # Read metadata page through the buffer pool.
            metadata_frame = buffer_pool.fetch_page(0)
            raw_metadata_page = bytes(metadata_frame)
            buffer_pool.unpin_page(0)

            metadata_page = SlottedPage.from_bytes(raw_metadata_page)

            if metadata_page.page_id != 0:
                raise ValueError("Invalid metadata page ID")

            if metadata_page.slot_count != 1:
                raise ValueError("Invalid metadata page")

            metadata_bytes = metadata_page.get_record(0)

            try:
                metadata = json.loads(metadata_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Invalid metadata format") from error

            node_pages = {}

            for page_id in metadata["node_page_ids"]:

                if not isinstance(page_id, int) or page_id < 1:
                    raise ValueError("Invalid node page ID")

                if page_id >= disk.get_page_count():
                    raise ValueError("Node page does not exist")

                # Read each node page through the buffer pool.
                frame = buffer_pool.fetch_page(page_id)
                raw_page = bytes(frame)
                buffer_pool.unpin_page(page_id)

                page = SlottedPage.from_bytes(raw_page)

                if page.page_id != page_id:
                    raise ValueError("Page ID mismatch")

                if page.slot_count != 1:
                    raise ValueError(
                        f"Invalid node page {page_id}"
                    )

                node_pages[page_id] = page.get_record(0)

            from indexes.bplus_tree import BPlusTree

            tree = BPlusTree.deserialize_pages(
                metadata_bytes,
                node_pages
            )

            return tree

        finally:
            buffer_pool.flush_all()
            disk.close()
