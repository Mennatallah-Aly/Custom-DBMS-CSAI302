
import os

from storage.disk_manager import DiskManager
from storage.page import SlottedPage
from indexes.bplus_tree import BPlusTree


DB_FILE = "bplus_tree_disk_test.db"




tree = BPlusTree(max_keys=3)

for key in range(1, 201):
    tree.insert(key, f"Value-{key}")

print("Original tree size:", len(tree))




metadata, node_pages = tree.serialize_pages(
    first_page_id=1
)

print("Number of serialized nodes:", len(node_pages))




# Remove the old test database if it exists.
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

disk = DiskManager(DB_FILE)



metadata_page_id = disk.allocate_page()

metadata_page = SlottedPage(metadata_page_id)

metadata_page.insert_record(metadata)

disk.write_page(
    metadata_page_id,
    metadata_page.to_bytes()
)

print("Metadata saved in page:", metadata_page_id)



for page_id, node_data in node_pages.items():

    allocated_page_id = disk.allocate_page()

    assert allocated_page_id == page_id

    page = SlottedPage(page_id)

    page.insert_record(node_data)

    disk.write_page(
        page_id,
        page.to_bytes()
    )

print("All tree nodes saved successfully!")

print("Total database pages:", disk.get_page_count())

disk.close()



disk = DiskManager(DB_FILE)

print("\nDatabase reopened successfully!")




raw_metadata_page = disk.read_page(0)

metadata_page = SlottedPage.from_bytes(
    raw_metadata_page
)

restored_metadata = metadata_page.get_record(0)

print("Metadata loaded from disk!")




restored_node_pages = {}

# Read the page IDs recorded in the metadata.
import json

metadata_info = json.loads(
    restored_metadata.decode("utf-8")
)

for page_id in metadata_info["node_page_ids"]:

    raw_page = disk.read_page(page_id)

    page = SlottedPage.from_bytes(raw_page)

    node_data = page.get_record(0)

    restored_node_pages[page_id] = node_data

print("All tree nodes loaded from disk!")

disk.close()




restored_tree = BPlusTree.deserialize_pages(
    restored_metadata,
    restored_node_pages
)

print("\nRestored tree size:", len(restored_tree))




for key in range(1, 201):

    expected_value = f"Value-{key}"

    assert restored_tree.search(key) == expected_value

print("Search test passed!")




expected_range = [
    (key, f"Value-{key}")
    for key in range(50, 61)
]

actual_range = restored_tree.range_query(50, 60)

assert actual_range == expected_range

print("Range query test passed!")




assert len(restored_tree) == 200

assert restored_tree.search(90) == "Value-90"

assert restored_tree.search(250) is None

print("\n================================")
print("ALL DISK TESTS PASSED!")
print("B+ Tree was saved and restored successfully.")
print("================================")
