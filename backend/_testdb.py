"""Import FIRST in any test that (indirectly) imports db.py.

Points PGUARD_DB_PATH at a throwaway file before db.py opens its
connection, so test runs never open - or lock, or write accounts into -
the real dashboard.db (which a running live backend may be writing to).
Whichever test module is imported first wins; the rest reuse it.
"""
import atexit
import os
import shutil
import tempfile

if "PGUARD_TEST_DB_DIR" not in os.environ:
    _dir = tempfile.mkdtemp(prefix="pguard_test_db_")
    os.environ["PGUARD_TEST_DB_DIR"] = _dir
    os.environ["PGUARD_DB_PATH"] = os.path.join(_dir, "test.db")
    atexit.register(shutil.rmtree, _dir, True)
os.environ.pop("DASHBOARD_PASSWORD", None)  # no account bootstrap during tests
