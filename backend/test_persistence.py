"""Unit tests for bag-mode persistence decimation (backend/main.py).

The gate is pure logic (no I/O) so it is tested directly. Run from
backend/:  python -m unittest test_persistence -v
"""
import unittest

from main import _db_write_due, BAG_DB_WRITE_EVERY_S


class TestDbWriteDue(unittest.TestCase):
    def test_first_call_always_writes(self):
        self.assertTrue(_db_write_due({}, "/battery/state", 1000.0))

    def test_suppresses_within_interval(self):
        last = {}
        self.assertTrue(_db_write_due(last, "/t", 1000.0))
        self.assertFalse(_db_write_due(last, "/t", 1000.5))
        self.assertFalse(_db_write_due(last, "/t", 1001.999))

    def test_reopens_after_interval(self):
        last = {}
        _db_write_due(last, "/t", 1000.0)
        self.assertTrue(
            _db_write_due(last, "/t", 1000.0 + BAG_DB_WRITE_EVERY_S))

    def test_per_topic_independence(self):
        last = {}
        _db_write_due(last, "/a", 1000.0)
        # a different topic is unaffected by /a's recent write
        self.assertTrue(_db_write_due(last, "/b", 1000.1))
        self.assertFalse(_db_write_due(last, "/a", 1000.1))

    def test_custom_interval_respected(self):
        last = {}
        _db_write_due(last, "/t", 1000.0, every=10.0)
        self.assertFalse(_db_write_due(last, "/t", 1005.0, every=10.0))
        self.assertTrue(_db_write_due(last, "/t", 1010.0, every=10.0))


if __name__ == "__main__":
    unittest.main()
