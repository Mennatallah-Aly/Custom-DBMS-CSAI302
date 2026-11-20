import time
import unittest
from indexes.index_manager import IndexManager
from indexes.bplus_tree import BPlusTree
# Uncomment once Member 2 pushes LSMTree:
# from indexes.lsm_tree import LSMTree


class TestMilestone2Integration(unittest.TestCase):
    def test_bplus_integration_and_range_routing(self) -> None:
        bplus = BPlusTree(order=4)
        mgr = IndexManager(bplus_index=bplus)

        for k in range(1, 21):
            mgr.insert(k, f"value_{k}")

        # Verify point lookup
        val, used_index = mgr.search(10)
        self.assertEqual(val, "value_10")
        self.assertEqual(used_index, "bplus")

        # Verify range query routing to B+ Tree
        results, range_index = mgr.range_search(5, 10)
        self.assertEqual(range_index, "bplus")
        self.assertEqual(len(results), 6)

    def test_query_interface_select_and_insert(self) -> None:
        bplus = BPlusTree(order=4)
        mgr = IndexManager(bplus_index=bplus)

        mgr.execute_simple_query({"op": "INSERT", "key": 100, "value": "Record_100"})
        mgr.execute_simple_query({"op": "INSERT", "key": 105, "value": "Record_105"})

        res = mgr.execute_simple_query({"op": "SELECT", "where": ("=", 100)})
        self.assertEqual(res, "Record_100")

        range_res = mgr.execute_simple_query({"op": "SELECT", "where": ("BETWEEN", 90, 110)})
        self.assertEqual(len(range_res), 2)

    def test_workload_benchmark_timing(self) -> None:
        bplus = BPlusTree(order=16)
        mgr = IndexManager(bplus_index=bplus)

        start_write = time.perf_counter()
        for i in range(1000):
            mgr.insert(i, f"row_{i}")
        write_ms = (time.perf_counter() - start_write) * 1000

        start_read = time.perf_counter()
        for i in range(1000):
            mgr.search(i)
        read_ms = (time.perf_counter() - start_read) * 1000

        print(f"\n[M2 Benchmark] 1,000 Inserts: {write_ms:.2f} ms | 1,000 Point Reads: {read_ms:.2f} ms")
        self.assertGreater(write_ms, 0)
        self.assertGreater(read_ms, 0)


if __name__ == "__main__":
    unittest.main()