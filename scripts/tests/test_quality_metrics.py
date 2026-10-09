import importlib.util
import math
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("quality_metrics", Path(__file__).parents[1] / "quality_metrics.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


class QualityMetricsTest(unittest.TestCase):
    def test_empty_and_perfect_rankings(self):
        self.assertTrue(all(v == 0 for v in quality.metrics([], {"a": 1}).values()))
        self.assertTrue(all(v == 1 for v in quality.metrics(["a", "b"], {"a": 1, "b": 1}).values()))

    def test_complete_denominator_and_rank_discount(self):
        result = quality.metrics(["z", "a"], {"a": 1, "b": 1})
        self.assertEqual(result["recall_at_10"], .5)
        self.assertEqual(result["mrr_at_10"], .5)
        self.assertAlmostEqual(result["ndcg_at_10"], 1 / (1 + math.log2(3)))

    def test_graded_ideal_order_and_cutoff(self):
        result = quality.metrics(["a", "b"], {"a": 1, "b": 3}, k=1)
        self.assertEqual(result, {"recall_at_1": .5, "ndcg_at_1": 1 / 7, "mrr_at_1": 1})

    def test_malformed_or_duplicate_rankings_refused(self):
        for ranked, rel, k in [(["a", "a"], {"a": 1}, 10), ([], {}, 10),
                               ([], {"a": True}, 10), ([], {"a": 0}, 10),
                               ([], {"a": 5}, 10), ([], {"a": 1}, 0)]:
            with self.assertRaises(ValueError):
                quality.metrics(ranked, rel, k)

    def test_selection_is_order_independent_and_preserves_all_positives(self):
        documents = {"required": "abcd", "x": "xx", "y": "yyy"}
        a = quality.select_documents(documents, {"q": {"required": 1}}, 2, 8)
        b = quality.select_documents(dict(reversed(list(documents.items()))), {"q": {"required": 1}}, 2, 8)
        self.assertEqual(a, b)
        self.assertIn("required", a[0])
        self.assertEqual(a[1], sum(len(documents[d].encode()) for d in a[0]))

    def test_positive_overflow_and_missing_documents_fail_without_truncation(self):
        for docs, rel, count, size in [({"a": "abc"}, {"q": {"a": 1}}, 1, 2),
                                      ({"a": "a", "b": "b"}, {"q": {"a": 1, "b": 1}}, 1, 10),
                                      ({"a": "a"}, {"q": {"b": 1}}, 1, 10),
                                      ({"a": "a" * 65537}, {"q": {"a": 1}}, 1, 100000)]:
            with self.assertRaises(ValueError):
                quality.select_documents(docs, rel, count, size)


if __name__ == "__main__":
    unittest.main()
