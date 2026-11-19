
from indexes.bplus_tree import BPlusTree



tree = BPlusTree(max_keys=3)


for key in range(1, 201):
    tree.insert(key, f"Value-{key}")


metadata, node_pages = tree.serialize_pages()

print("Original tree size:", len(tree))
print("Number of serialized nodes:", len(node_pages))
print("Root page ID:", tree.root.page_id)


restored_tree = BPlusTree.deserialize_pages(
    metadata,
    node_pages
)


assert len(restored_tree) == 200

for key in range(1, 201):
    assert restored_tree.search(key) == f"Value-{key}"


expected = [
    (key, f"Value-{key}")
    for key in range(50, 61)
]

assert restored_tree.range_query(50, 60) == expected


leaf = restored_tree.root

while not leaf.is_leaf:
    leaf = leaf.children[0]

leaf_count = 0

while leaf is not None:
    leaf_count += 1
    leaf = leaf.next_leaf

print("Restored leaf count:", leaf_count)
print("Restored tree size:", len(restored_tree))
print("Serialization and reconstruction tests passed!")
