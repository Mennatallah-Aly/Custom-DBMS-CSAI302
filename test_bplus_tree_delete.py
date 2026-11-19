
import random

from indexes.bplus_tree import BPlusTree


def validate_tree(tree):
    """
    Validate B+ Tree structure after insertions and deletions.
    """

    leaf_nodes = []
    leaf_depths = []

    def check_node(node, depth, is_root=False):

        assert node.keys == sorted(node.keys), "Keys are not sorted"

        assert len(node.keys) <= tree.max_keys, "Node overflow"

        if node.is_leaf:

            assert len(node.keys) == len(node.values)

            assert len(node.children) == 0

            if not is_root:
                min_keys = (tree.max_keys + 1) // 2
                assert len(node.keys) >= min_keys, "Leaf underflow"

            leaf_nodes.append(node)
            leaf_depths.append(depth)

            return node.keys[0] if node.keys else None

        # Internal node checks.
        assert len(node.children) == len(node.keys) + 1

        if is_root:
            assert len(node.children) >= 2, "Invalid internal root"
        else:
            min_keys = tree.max_keys // 2
            assert len(node.keys) >= min_keys, "Internal node underflow"

        child_minimums = []

        for child in node.children:

            assert child.parent is node, "Incorrect parent reference"

            child_minimums.append(
                check_node(child, depth + 1)
            )

        expected_keys = child_minimums[1:]

        assert node.keys == expected_keys, "Incorrect separator keys"

        return child_minimums[0]

    check_node(tree.root, 0, True)


    assert len(set(leaf_depths)) <= 1, "Leaves have different depths"


    current = tree.root

    while not current.is_leaf:
        current = current.children[0]

    linked_leaves = []

    while current is not None:
        linked_leaves.append(current)
        current = current.next_leaf

    assert linked_leaves == leaf_nodes, "Incorrect leaf links"

    # Check total number of keys.
    total_keys = sum(len(leaf.keys) for leaf in leaf_nodes)

    assert total_keys == len(tree), "Incorrect tree size"

    return True



tree = BPlusTree(max_keys=3)

reference = {}

for key in range(1, 301):

    value = f"Value-{key}"

    tree.insert(key, value)
    reference[key] = value

assert len(tree) == 300

validate_tree(tree)

print("Insertion test passed!")




random.seed(42)

keys = list(range(1, 301))
random.shuffle(keys)

deleted_keys = keys[:150]

for key in deleted_keys:

    assert tree.delete(key) is True

    reference.pop(key)

    validate_tree(tree)

assert len(tree) == 150

print("Random deletion test passed!")




for key in range(1, 301):

    if key in reference:
        assert tree.search(key) == reference[key]
    else:
        assert tree.search(key) is None

print("Search after deletion test passed!")




expected = sorted(
    (key, value)
    for key, value in reference.items()
    if 50 <= key <= 250
)

actual = tree.range_query(50, 250)

assert actual == expected

print("Range query after deletion test passed!")




assert tree.delete(999) is False

print("Non-existing key deletion test passed!")




remaining_keys = list(reference.keys())
random.shuffle(remaining_keys)

for key in remaining_keys:

    assert tree.delete(key) is True

    reference.pop(key)

    validate_tree(tree)

assert len(tree) == 0

assert tree.root.is_leaf

assert tree.root.keys == []

assert tree.root.values == []

print("Delete all keys test passed!")



print("\n================================")
print("ALL B+ TREE DELETION TESTS PASSED!")
print("================================")
