"""Contract test: features.require_matching_schema() against the real
ml/train.py artifacts. Deliberately free of fastapi/aiohttp imports so it
runs in minimal environments.

Catches exactly the class of bug found pre-Phase-3: train.py wrote the key
as "schema_version" while require_matching_schema() read "version", so the
gate could never pass against a real metadata.json (train.py never calls
the gate itself, so Phase 2's numbers were unaffected). Run from backend/:
python -m unittest test_schema_contract -v
"""
import json
import sys
import unittest
from pathlib import Path

from features import (
    require_matching_schema,
    FeatureSchemaMismatch,
    FEATURE_SCHEMA_VERSION,
    FEATURE_NAMES,
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.infer import ML_FEATURES as INFER_ML_FEATURES  # noqa: E402

META = Path(__file__).parent.parent / "ml" / "artifacts" / "metadata.json"


@unittest.skipUnless(META.exists(),
                     "ml/artifacts not built - run ml/train.py first")
class TestSchemaContract(unittest.TestCase):
    def test_real_metadata_passes(self):
        meta = json.loads(META.read_text())
        self.assertTrue(require_matching_schema(meta))
        self.assertEqual(meta["schema_version"], FEATURE_SCHEMA_VERSION)
        self.assertEqual(meta["features"], FEATURE_NAMES)

    def test_infer_vector_order_matches_metadata(self):
        # ml/infer.py assembles the 13-vector positionally: a silent
        # reorder would feed every model scrambled columns. Pinned against
        # the trained artifact's own ml_features list.
        meta = json.loads(META.read_text())
        self.assertEqual(INFER_ML_FEATURES, meta["ml_features"])

    def test_version_mismatch_raises(self):
        meta = dict(json.loads(META.read_text()))
        meta["schema_version"] = "0.0"
        with self.assertRaises(FeatureSchemaMismatch):
            require_matching_schema(meta)

    def test_missing_version_raises(self):
        meta = dict(json.loads(META.read_text()))
        del meta["schema_version"]
        with self.assertRaises(FeatureSchemaMismatch):
            require_matching_schema(meta)

    def test_feature_mismatch_raises(self):
        meta = dict(json.loads(META.read_text()))
        meta["features"] = meta["features"][:-1]
        with self.assertRaises(FeatureSchemaMismatch):
            require_matching_schema(meta)


if __name__ == "__main__":
    unittest.main()
