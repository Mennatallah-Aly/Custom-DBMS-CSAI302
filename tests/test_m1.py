"""Verify M1 persistence using distinct writer and reader processes."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RestartTests(unittest.TestCase):
    def test_layouts_survive_process_restart(self):
        setup = """
from storage.layouts import RowLayout, ColumnLayout
layouts = (RowLayout, ColumnLayout)
def record(i):
    return (i, None if i % 2 else "course-" + str(i) * 100, i / 2)
"""
        writer = """
for layout in layouts:
    with layout(layout.__name__, ["id", "name", "score"]) as table:
        for i in range(160):
            assert table.insert(record(i)) == i
"""
        reader = """
for layout in layouts:
    with layout(layout.__name__) as table:
        assert list(table.scan()) == [record(i) for i in range(160)]
        assert table.get_row(151) == record(151)
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        env.pop("PYTHONOPTIMIZE", None)
        with tempfile.TemporaryDirectory() as directory:
            for program in (writer, reader):
                subprocess.run([sys.executable, "-c", setup + program], cwd=directory,
                               env=env, check=True, timeout=60)
