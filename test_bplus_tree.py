
from indexes.bplus_tree import BPlusTree


tree = BPlusTree(max_keys=3)

data = [
    (10, "Ahmed"),
    (20, "Sara"),
    (30, "Ali"),
    (40, "Mona"),
    (50, "Omar"),
    (60, "Youssef"),
    (70, "Nour"),
    (80, "Mariam"),
]

for key, value in data:
    tree.insert(key, value)

print("Number of keys:", len(tree))

print("Search 40:", tree.search(40))
print("Search 90:", tree.search(90))

tree.insert(40, "Updated Mona")
print("Updated 40:", tree.search(40))

print("Range 20 to 60:", tree.range_query(20, 60))

print("Invalid range:", tree.range_query(80, 20))

print("Root keys:", tree.root.keys)
print("Root is leaf:", tree.root.is_leaf)

assert tree.search(90) is None

assert tree.search(40) == "Updated Mona"
assert len(tree) == 8

assert tree.range_query(20, 60) == [
    (20, "Sara"),
    (30, "Ali"),
    (40, "Updated Mona"),
    (50, "Omar"),
    (60, "Youssef")
]

print("\nAll B+ Tree tests passed!")
