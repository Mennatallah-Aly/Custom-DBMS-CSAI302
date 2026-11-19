
import random
from indexes.bplus_tree import BPlusTree


def validate_tree(tree):
    """Check the structure and consistency of the B+ Tree."""

    leaf_depths = set()
    leaves = []

    def check_node(node, depth):
        assert node.keys == sorted(node.keys), "Keys are not sorted"

        if node.is_leaf:
            assert len(node.keys) == len(node.values)
            leaf_depths.add(depth)
            leaves.append(node)
            return node.keys[0], node.keys[-1]

        assert len(node.children) == len(node.keys) + 1
        assert len(node.keys) <= tree.max_keys

        for child in node.children:
            assert child.parent is node

        ranges = [
            check_node(child, depth + 1)
            for child in node.children
        ]

        for i, key in enumerate(node.keys):
            assert ranges[i][1] < key
            assert ranges[i + 1][0] == key

        return ranges[0][0], ranges[-1][1]

    check_node(tree.root, 0)

    assert len(leaf_depths) == 1

    for i in range(len(leaves) - 1):
        assert leaves[i].next_leaf is leaves[i + 1]

    assert leaves[-1].next_leaf is None

    print("Tree structure is valid!")


tree = BPlusTree(max_keys=3)

keys = list(range(1, 201))
random.Random(42).shuffle(keys)

for key in keys:
    tree.insert(key, f"Value-{key}")

assert len(tree) == 200

for key in range(1, 201):
    assert tree.search(key) == f"Value-{key}"

result = tree.range_query(50, 60)
expected = [(key, f"Value-{key}") for key in range(50, 61)]

assert result == expected

tree.insert(100, "Updated-Value")
assert tree.search(100) == "Updated-Value"
assert len(tree) == 200

# Check missing key.
assert tree.search(500) is None

validate_tree(tree)

print("Number of keys:", len(tree))
print("Range query test passed!")
print("Duplicate update test passed!")
print("All stress tests passed!")
