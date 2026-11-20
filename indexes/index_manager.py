from typing import Any


class IndexManager:

    def __init__(
        self,
        bplus_index=None,
        lsm_index=None,
        write_threshold: float = 0.50,
        preferred_index: str | None = None,
    ) -> None:
        self.bplus_index = bplus_index
        self.lsm_index = lsm_index
        self.write_threshold = write_threshold
        self.preferred_index = preferred_index.lower() if preferred_index else None

        self.read_ops = 0
        self.write_ops = 0
        self.range_ops = 0

    def write_ratio(self) -> float:
        total = self.read_ops + self.write_ops
        return self.write_ops / total if total > 0 else 0.0

    def select_index_type(self, is_range: bool = False) -> str:
        if self.preferred_index in ("bplus", "lsm"):
            return self.preferred_index
        if is_range and self.bplus_index is not None:
            return "bplus"
        if self.write_ratio() > self.write_threshold and self.lsm_index is not None:
            return "lsm"
        if self.bplus_index is not None:
            return "bplus"
        if self.lsm_index is not None:
            return "lsm"
        raise RuntimeError("No index implementation registered in IndexManager.")

    def insert(self, key: Any, value: Any) -> str:
        self.write_ops += 1
        chosen = self.select_index_type(is_range=False)
        if chosen == "lsm" and self.lsm_index is not None:
            self.lsm_index.insert(key, value)
        elif self.bplus_index is not None:
            self.bplus_index.insert(key, value)
        else:
            self.lsm_index.insert(key, value)
        return chosen

    def search(self, key: Any) -> tuple[Any, str]:
        self.read_ops += 1
        chosen = self.select_index_type(is_range=False)
        if chosen == "lsm" and self.lsm_index is not None:
            return self.lsm_index.search(key), "lsm"
        if self.bplus_index is not None:
            return self.bplus_index.search(key), "bplus"
        return self.lsm_index.search(key), "lsm"

    def range_search(self, start_key: Any, end_key: Any) -> tuple[list[Any], str]:
        self.read_ops += 1
        self.range_ops += 1
        chosen = self.select_index_type(is_range=True)
        if chosen == "bplus" and self.bplus_index is not None:
            if hasattr(self.bplus_index, "range_query"):
                return self.bplus_index.range_query(start_key, end_key), "bplus"
            return self.bplus_index.range_search(start_key, end_key), "bplus"
        raise NotImplementedError("Range search requires a B+ Tree index.")

    def execute_simple_query(self, query: dict[str, Any]) -> Any:
        op = query.get("op", "").upper()
        if op == "INSERT":
            return self.insert(query["key"], query["value"])
        if op == "SELECT":
            where = query.get("where")
            if not where:
                raise ValueError("SELECT query requires a 'where' clause.")
            operator = where[0].upper()
            if operator == "=":
                result, _ = self.search(where[1])
                return result
            if operator == "BETWEEN":
                results, _ = self.range_search(where[1], where[2])
                return results
            raise ValueError(f"Unsupported WHERE operator: {operator}")
        raise ValueError(f"Unsupported query operation: {op}")