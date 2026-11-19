



import json
from bisect import bisect_left, bisect_right

from storage.page import SlottedPage


class BPlusTreeNode:
    """
    Represents a node in a B+ Tree.
    """

    def __init__(self, is_leaf=False):
        self.is_leaf = is_leaf

        self.page_id = None

        self.keys = []

        # Leaf node data
        self.values = []
        self.next_leaf = None

        # Internal node data
        self.children = []

        self.parent = None


class BPlusTree:
    """
    B+ Tree supporting:

    - Insert
    - Search
    - Delete
    - Range Query
    - Leaf splitting
    - Internal node splitting
    - Redistribution
    - Merging
    - Root adjustment
    - Serialization
    - Deserialization
    """

    def __init__(self, max_keys=3):

        if max_keys < 2:
            raise ValueError("max_keys must be at least 2")

        self.max_keys = max_keys

        self.root = BPlusTreeNode(is_leaf=True)

        self.size = 0


    def _find_leaf(self, key):
        """
        Find the leaf where a key should exist.
        """

        current = self.root

        while not current.is_leaf:

            child_index = bisect_right(
                current.keys,
                key
            )

            current = current.children[child_index]

        return current

    def search(self, key):
        """
        Search for a key.

        Returns its value if found,
        otherwise returns None.
        """

        leaf = self._find_leaf(key)

        index = bisect_left(
            leaf.keys,
            key
        )

        if (
            index < len(leaf.keys)
            and leaf.keys[index] == key
        ):
            return leaf.values[index]

        return None


    def insert(self, key, value):
        """
        Insert a key-value pair.

        If the key already exists,
        update its value.
        """

        leaf = self._find_leaf(key)

        index = bisect_left(
            leaf.keys,
            key
        )


        if (
            index < len(leaf.keys)
            and leaf.keys[index] == key
        ):

            leaf.values[index] = value
            return


        leaf.keys.insert(index, key)
        leaf.values.insert(index, value)

        self.size += 1


        if len(leaf.keys) > self.max_keys:

            self._split_leaf(leaf)


        self._refresh_ancestors(leaf.parent)



    def _split_leaf(self, leaf):
        """
        Split an overflowing leaf into two leaves.
        """

        new_leaf = BPlusTreeNode(is_leaf=True)

        split_index = (len(leaf.keys) + 1) // 2

        new_leaf.keys = leaf.keys[split_index:]
        new_leaf.values = leaf.values[split_index:]

        leaf.keys = leaf.keys[:split_index]
        leaf.values = leaf.values[:split_index]


        new_leaf.next_leaf = leaf.next_leaf
        leaf.next_leaf = new_leaf

        new_leaf.parent = leaf.parent

        separator = new_leaf.keys[0]

        self._insert_into_parent(
            leaf,
            separator,
            new_leaf
        )



    def _insert_into_parent(self, left, key, right):
        """
        Insert a new child into the parent node.
        """


        if left.parent is None:

            new_root = BPlusTreeNode(is_leaf=False)

            new_root.children = [
                left,
                right
            ]

            left.parent = new_root
            right.parent = new_root

            self._refresh_node_keys(new_root)

            self.root = new_root

            return

        parent = left.parent

        left_index = parent.children.index(left)


        parent.children.insert(
            left_index + 1,
            right
        )

        right.parent = parent


        self._refresh_node_keys(parent)


        if len(parent.keys) > self.max_keys:

            self._split_internal(parent)



    def _split_internal(self, node):
        """
        Split an overflowing internal node.
        """

        middle_index = len(node.keys) // 2

        promoted_key = node.keys[middle_index]

        new_node = BPlusTreeNode(is_leaf=False)


        new_node.children = node.children[middle_index + 1:]


        node.children = node.children[:middle_index + 1]


        for child in new_node.children:
            child.parent = new_node

        new_node.parent = node.parent

        self._refresh_node_keys(node)
        self._refresh_node_keys(new_node)

        self._insert_into_parent(
            node,
            promoted_key,
            new_node
        )



    def _subtree_min(self, node):
        """
        Return the smallest key in a subtree.
        """

        current = node

        while not current.is_leaf:
            current = current.children[0]

        if not current.keys:
            raise ValueError(
                "Cannot find minimum of an empty subtree"
            )

        return current.keys[0]

    def _refresh_node_keys(self, node):
        """
        Rebuild internal separator keys.

        Each separator is the smallest key
        in the child immediately to its right.
        """

        if node.is_leaf:
            return

        node.keys = [
            self._subtree_min(child)
            for child in node.children[1:]
        ]

    def _refresh_ancestors(self, node):
        """
        Refresh separator keys up to the root.
        """

        current = node

        while current is not None:

            if not current.is_leaf:
                self._refresh_node_keys(current)

            current = current.parent


    def delete(self, key):
        """
        Delete a key from the B+ Tree.

        Returns:
            True if the key was deleted.
            False if the key does not exist.
        """

        leaf = self._find_leaf(key)

        index = bisect_left(
            leaf.keys,
            key
        )

        if (
            index >= len(leaf.keys)
            or leaf.keys[index] != key
        ):
            return False

        # Remove key and its value.
        leaf.keys.pop(index)
        leaf.values.pop(index)

        self.size -= 1

        if leaf is self.root:
            return True

        min_leaf_keys = (self.max_keys + 1) // 2

        if len(leaf.keys) >= min_leaf_keys:

            self._refresh_ancestors(leaf.parent)

            return True

        self._rebalance_leaf(leaf)

        return True


    def _rebalance_leaf(self, leaf):
        """
        Fix an underflowing leaf using
        redistribution or merging.
        """

        parent = leaf.parent

        leaf_index = parent.children.index(leaf)

        min_leaf_keys = (self.max_keys + 1) // 2

        left_sibling = (
            parent.children[leaf_index - 1]
            if leaf_index > 0
            else None
        )

        right_sibling = (
            parent.children[leaf_index + 1]
            if leaf_index + 1 < len(parent.children)
            else None
        )


        if (
            left_sibling is not None
            and len(left_sibling.keys) > min_leaf_keys
        ):

            borrowed_key = left_sibling.keys.pop()
            borrowed_value = left_sibling.values.pop()

            leaf.keys.insert(0, borrowed_key)
            leaf.values.insert(0, borrowed_value)

            self._refresh_ancestors(parent)

            return



        if (
            right_sibling is not None
            and len(right_sibling.keys) > min_leaf_keys
        ):

            borrowed_key = right_sibling.keys.pop(0)
            borrowed_value = right_sibling.values.pop(0)

            leaf.keys.append(borrowed_key)
            leaf.values.append(borrowed_value)

            self._refresh_ancestors(parent)

            return


        if left_sibling is not None:

            left_sibling.keys.extend(leaf.keys)
            left_sibling.values.extend(leaf.values)

            left_sibling.next_leaf = leaf.next_leaf


            parent.children.pop(leaf_index)

            self._refresh_node_keys(parent)

            self._rebalance_internal(parent)

            return



        if right_sibling is not None:

            leaf.keys.extend(right_sibling.keys)
            leaf.values.extend(right_sibling.values)


            leaf.next_leaf = right_sibling.next_leaf


            parent.children.pop(leaf_index + 1)

            self._refresh_node_keys(parent)

            self._rebalance_internal(parent)

            return



    def _rebalance_internal(self, node):
        """
        Fix an internal node underflow using
        redistribution or merging.
        """



        if node is self.root:


            if (
                not node.is_leaf
                and len(node.children) == 1
            ):

                self.root = node.children[0]
                self.root.parent = None

            elif not node.is_leaf:

                self._refresh_node_keys(node)

            return

        min_internal_keys = self.max_keys // 2


        if len(node.keys) >= min_internal_keys:

            self._refresh_ancestors(node)

            return

        parent = node.parent

        node_index = parent.children.index(node)

        left_sibling = (
            parent.children[node_index - 1]
            if node_index > 0
            else None
        )

        right_sibling = (
            parent.children[node_index + 1]
            if node_index + 1 < len(parent.children)
            else None
        )



        if (
            left_sibling is not None
            and len(left_sibling.keys) > min_internal_keys
        ):

            borrowed_child = left_sibling.children.pop()

            node.children.insert(
                0,
                borrowed_child
            )

            borrowed_child.parent = node

            self._refresh_node_keys(left_sibling)
            self._refresh_node_keys(node)
            self._refresh_ancestors(parent)

            return


        if (
            right_sibling is not None
            and len(right_sibling.keys) > min_internal_keys
        ):

            borrowed_child = right_sibling.children.pop(0)

            node.children.append(borrowed_child)

            borrowed_child.parent = node

            self._refresh_node_keys(right_sibling)
            self._refresh_node_keys(node)
            self._refresh_ancestors(parent)

            return



        if left_sibling is not None:

            for child in node.children:
                child.parent = left_sibling

            left_sibling.children.extend(node.children)

            self._refresh_node_keys(left_sibling)

            # Remove the merged node.
            parent.children.pop(node_index)

            self._refresh_node_keys(parent)

            self._rebalance_internal(parent)

            return



        if right_sibling is not None:

            for child in right_sibling.children:
                child.parent = node

            node.children.extend(right_sibling.children)

            self._refresh_node_keys(node)

            # Remove the right sibling.
            parent.children.pop(node_index + 1)

            self._refresh_node_keys(parent)

            self._rebalance_internal(parent)

            return



    def range_query(self, start, end):
        """
        Return all key-value pairs in an inclusive range.
        """

        if start > end:
            return []

        results = []

        leaf = self._find_leaf(start)

        index = bisect_left(
            leaf.keys,
            start
        )

   
        while leaf is not None:

            while index < len(leaf.keys):

                key = leaf.keys[index]
                value = leaf.values[index]

                if key > end:
                    return results

                results.append(
                    (key, value)
                )

                index += 1

            leaf = leaf.next_leaf

            index = 0

        return results


    def __len__(self):
        """
        Return the number of unique keys.
        """

        return self.size



    def serialize_pages(self, first_page_id=1):
        """
        Convert every tree node into a JSON byte record.

        Page 0 is reserved for metadata.
        Node pages start from first_page_id.

        Returns:
            metadata_bytes
            node_pages
        """

        if first_page_id < 1:
            raise ValueError(
                "Node page IDs must start from 1"
            )

        nodes = []

        def collect_nodes(node):

            nodes.append(node)

            for child in node.children:
                collect_nodes(child)

        collect_nodes(self.root)


        for index, node in enumerate(nodes):

            node.page_id = first_page_id + index

        node_pages = {}

        max_record_size = (
            SlottedPage.PAGE_SIZE
            - SlottedPage.HEADER_SIZE
            - SlottedPage.SLOT_SIZE
        )

        for node in nodes:

            record = {
                "format_version": 1,

                "page_id": node.page_id,

                "is_leaf": node.is_leaf,

                "keys": node.keys,

                "values": (
                    node.values
                    if node.is_leaf
                    else []
                ),

                "parent_page_id": (
                    node.parent.page_id
                    if node.parent is not None
                    else None
                ),

                "children_page_ids": [
                    child.page_id
                    for child in node.children
                ],

                "next_leaf_page_id": (
                    node.next_leaf.page_id
                    if node.next_leaf is not None
                    else None
                )
            }

            try:

                payload = json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False
                ).encode("utf-8")

            except (TypeError, ValueError) as error:

                raise ValueError(
                    "Node contains a value that cannot "
                    "be serialized to JSON"
                ) from error

            if len(payload) > max_record_size:

                raise ValueError(
                    f"Node {node.page_id} exceeds "
                    "the available page space"
                )

            node_pages[node.page_id] = payload


        metadata = {
            "format_version": 1,

            "max_keys": self.max_keys,

            "size": self.size,

            "root_page_id": self.root.page_id,

            "node_page_ids": list(node_pages.keys())
        }

        metadata_bytes = json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False
        ).encode("utf-8")

        if len(metadata_bytes) > max_record_size:

            raise ValueError(
                "Tree metadata exceeds the available page space"
            )

        return metadata_bytes, node_pages



    @classmethod
    def deserialize_pages(cls, metadata_bytes, node_pages):
        """
        Reconstruct a B+ Tree from serialized metadata
        and node records.
        """

        # Read metadata.
        try:

            metadata = json.loads(
                metadata_bytes.decode("utf-8")
            )

        except (UnicodeDecodeError, json.JSONDecodeError) as error:

            raise ValueError(
                "Invalid tree metadata"
            ) from error

        if metadata.get("format_version") != 1:

            raise ValueError(
                "Unsupported metadata format version"
            )

        tree = cls(
            max_keys=metadata["max_keys"]
        )

        nodes = {}


        for page_id, payload in node_pages.items():

            try:

                record = json.loads(
                    payload.decode("utf-8")
                )

            except (UnicodeDecodeError, json.JSONDecodeError) as error:

                raise ValueError(
                    f"Invalid node data in page {page_id}"
                ) from error

            if record.get("format_version") != 1:

                raise ValueError(
                    "Unsupported node format version"
                )

            actual_page_id = int(page_id)

            if record["page_id"] != actual_page_id:

                raise ValueError(
                    "Node page ID mismatch"
                )

            if actual_page_id in nodes:

                raise ValueError(
                    "Duplicate node page ID"
                )

            node = BPlusTreeNode(
                is_leaf=record["is_leaf"]
            )

            node.page_id = actual_page_id

            node.keys = record["keys"]

            node.values = record["values"]

            nodes[actual_page_id] = node



        for page_id, payload in node_pages.items():

            record = json.loads(
                payload.decode("utf-8")
            )

            node = nodes[int(page_id)]

            parent_id = record["parent_page_id"]

            if parent_id is not None:

                if parent_id not in nodes:

                    raise ValueError(
                        "Missing parent node"
                    )

                node.parent = nodes[parent_id]

            for child_id in record["children_page_ids"]:

                if child_id not in nodes:

                    raise ValueError(
                        "Missing child node"
                    )

                child = nodes[child_id]

                if child.parent is not None and child.parent is not node:

                    raise ValueError(
                        "Node has multiple parents"
                    )

                child.parent = node

                node.children.append(child)

            next_leaf_id = record["next_leaf_page_id"]

            if next_leaf_id is not None:

                if next_leaf_id not in nodes:

                    raise ValueError(
                        "Missing next leaf node"
                    )

                node.next_leaf = nodes[next_leaf_id]

            if node.is_leaf:

                if len(node.keys) != len(node.values):

                    raise ValueError(
                        "Invalid leaf key-value count"
                    )

                if node.children:

                    raise ValueError(
                        "Leaf node cannot have children"
                    )

                if len(node.keys) > tree.max_keys:

                    raise ValueError(
                        "Leaf node exceeds maximum capacity"
                    )

            else:

                if len(node.children) != len(node.keys) + 1:

                    raise ValueError(
                        "Invalid internal node structure"
                    )

                if node.values:

                    raise ValueError(
                        "Internal node cannot contain values"
                    )

                if len(node.keys) > tree.max_keys:

                    raise ValueError(
                        "Internal node exceeds maximum capacity"
                    )



        root_page_id = metadata["root_page_id"]

        if root_page_id not in nodes:

            raise ValueError(
                "Root page was not found"
            )

        tree.root = nodes[root_page_id]

        tree.size = metadata["size"]

        if tree.root.parent is not None:

            raise ValueError(
                "Root node cannot have a parent"
            )

        if len(nodes) != len(metadata["node_page_ids"]):

            raise ValueError(
                "Node count does not match metadata"
            )

        if set(nodes) != set(metadata["node_page_ids"]):

            raise ValueError(
                "Node page IDs do not match metadata"
            )

        return tree
