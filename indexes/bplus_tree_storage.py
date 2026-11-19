
import os
import json

from storage.disk_manager import DiskManager
from storage.page import SlottedPage


class BPlusTreeStorage:
    """
    Handles saving and loading a B+ Tree using disk pages.

    Page 0:
        Tree metadata.

    Pages 1 onwards:
        B+ Tree nodes.

    This version saves a complete snapshot of the tree.
    """

    def __init__(self, file_path):
        self.file_path = file_path


    def save(self, tree):
        """
        Save the complete B+ Tree to a database file.

        Existing file contents are replaced.
        """

        metadata, node_pages = tree.serialize_pages(
            first_page_id=1
        )

        # Create a fresh database file.
        if os.path.exists(self.file_path):
            os.remove(self.file_path)

        disk = DiskManager(
            self.file_path,
            page_size=SlottedPage.PAGE_SIZE
        )

        try:



            metadata_page_id = disk.allocate_page()

            if metadata_page_id != 0:
                raise ValueError("Metadata page must be page 0")

            metadata_page = SlottedPage(
                page_id=metadata_page_id
            )

            metadata_page.insert_record(metadata)

            disk.write_page(
                metadata_page_id,
                metadata_page.to_bytes()
            )


            for page_id, node_data in node_pages.items():

                allocated_page_id = disk.allocate_page()

                if allocated_page_id != page_id:
                    raise ValueError(
                        f"Unexpected page ID: {allocated_page_id}"
                    )

                page = SlottedPage(
                    page_id=page_id
                )

                page.insert_record(node_data)

                disk.write_page(
                    page_id,
                    page.to_bytes()
                )

            return {
                "root_page_id": tree.root.page_id,
                "node_count": len(node_pages),
                "page_count": disk.get_page_count()
            }

        finally:
            disk.close()


    def load(self):
        """
        Load a B+ Tree from the database file.
        """

        if not os.path.exists(self.file_path):
            raise FileNotFoundError(
                "Database file does not exist"
            )

        disk = DiskManager(
            self.file_path,
            page_size=SlottedPage.PAGE_SIZE
        )

        try:



            if disk.get_page_count() < 1:
                raise ValueError("Database file is empty")


            raw_metadata_page = disk.read_page(0)

            metadata_page = SlottedPage.from_bytes(
                raw_metadata_page
            )

            if metadata_page.page_id != 0:
                raise ValueError("Invalid metadata page ID")

            if metadata_page.slot_count != 1:
                raise ValueError("Invalid metadata page")

            metadata_bytes = metadata_page.get_record(0)

            try:

                metadata = json.loads(
                    metadata_bytes.decode("utf-8")
                )

            except (UnicodeDecodeError, json.JSONDecodeError) as error:

                raise ValueError(
                    "Invalid metadata format"
                ) from error



            node_pages = {}

            for page_id in metadata["node_page_ids"]:

                if not isinstance(page_id, int) or page_id < 1:
                    raise ValueError("Invalid node page ID")

                if page_id >= disk.get_page_count():
                    raise ValueError("Node page does not exist")

                raw_page = disk.read_page(page_id)

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
            disk.close()
