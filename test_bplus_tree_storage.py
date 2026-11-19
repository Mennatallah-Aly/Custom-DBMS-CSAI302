
import os

from indexes.bplus_tree import BPlusTree
from indexes.bplus_tree_storage import BPlusTreeStorage


DB_FILE = "bplus_tree_storage_test.db"




tree = BPlusTree(max_keys=3)

for key in range(1, 201):
    tree.insert(key, f"Value-{key}")

assert len(tree) == 200

print("Initial tree created!")




storage = BPlusTreeStorage(DB_FILE)

save_info = storage.save(tree)

print("Tree saved successfully!")
print("Saved nodes:", save_info["node_count"])
print("Database pages:", save_info["page_count"])




restored_tree = storage.load()

assert len(restored_tree) == 200

for key in range(1, 201):
    assert restored_tree.search(key) == f"Value-{key}"

print("Initial loading test passed!")



for key in range(2, 201, 2):
    assert restored_tree.delete(key) is True


for key in range(201, 251):
    restored_tree.insert(key, f"Value-{key}")


restored_tree.insert(55, "Updated-Value-55")

assert len(restored_tree) == 150

print("Insert, update and delete operations passed!")




save_info = storage.save(restored_tree)

print("Modified tree saved!")
print("Saved nodes:", save_info["node_count"])



final_tree = storage.load()

assert len(final_tree) == 150


for key in range(2, 201, 2):
    assert final_tree.search(key) is None


for key in range(1, 200, 2):

    if key == 55:
        assert final_tree.search(key) == "Updated-Value-55"
    else:
        assert final_tree.search(key) == f"Value-{key}"

for key in range(201, 251):
    assert final_tree.search(key) == f"Value-{key}"

print("Persistence after modifications passed!")




expected = [
    (key, final_tree.search(key))
    for key in range(45, 66)
    if final_tree.search(key) is not None
]

actual = final_tree.range_query(45, 65)

assert actual == expected

print("Range query after persistence passed!")




print("\n================================")
print("ALL STORAGE TESTS PASSED!")
print("================================")
