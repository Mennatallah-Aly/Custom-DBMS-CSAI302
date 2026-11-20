import unittest
from indexes.index_manager import IndexManager


class MockIndex:
    def __init__(self, name: str) -> None:
        self.name = name
        self.data = {}

    def insert(self, key, value) -> None:
        self.data[key] = value

    def search(self, key):
        return self.data.get(key)

    def range_query(self, start, end):
        return [v for k, v in sorted(self.data.items()) if start <= k <= end]


class TestIndexManager(unittest.TestCase):
    def test_workload_auto_selection(self) -> None:
        bplus = MockIndex("bplus")
        lsm = MockIndex("lsm")
        mgr = IndexManager(bplus_index=bplus, lsm_index=lsm)

        # Write-heavy workload routes to LSM
        self.assertEqual(mgr.insert(1, "A"), "lsm")
        self.assertEqual(mgr.insert(2, "B"), "lsm")

        # Range query always routes to B+ Tree
        _, chosen = mgr.range_search(1, 2)
        self.assertEqual(chosen, "bplus")


if __name__ == "__main__":
    unittest.main()