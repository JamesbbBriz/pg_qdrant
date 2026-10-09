import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from prepare_quality import verified_file


class QualityInputTest(unittest.TestCase):
    def record(self, data=b"fixed input"):
        return {"local_name": "input.parquet", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "origin": "https://example.invalid/pinned"}

    def test_verified_cache_does_not_use_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "input.parquet").write_bytes(b"fixed input")
            with patch("urllib.request.urlopen") as network:
                self.assertEqual(verified_file(directory, self.record(), True), directory / "input.parquet")
                network.assert_not_called()

    def test_corrupt_cache_is_refused_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "input.parquet"
            target.write_bytes(b"corrupted!!")
            with patch("urllib.request.urlopen") as network, self.assertRaises(ValueError):
                verified_file(directory, self.record(), True)
            self.assertEqual(target.read_bytes(), b"corrupted!!")
            network.assert_not_called()

    def test_download_requires_exact_size_and_digest_before_publication(self):
        for data in [b"wrong", b"wrong input", b"oversized input"]:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                with patch("urllib.request.urlopen", return_value=io.BytesIO(data)), self.assertRaises(ValueError):
                    verified_file(directory, self.record(), True)
                self.assertEqual(list(directory.iterdir()), [])

    def test_missing_cache_does_not_download_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen") as network:
            with self.assertRaises(FileNotFoundError):
                verified_file(Path(temporary), self.record())
            network.assert_not_called()

    def test_valid_download_is_published_after_hash_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"fixed input")):
                target = verified_file(directory, self.record(), True)
            self.assertEqual(target.read_bytes(), b"fixed input")
            self.assertEqual(list(directory.iterdir()), [target])

    def test_flat_cache_name_required(self):
        record = self.record()
        record["local_name"] = "../outside"
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            verified_file(Path(temporary), record, True)


if __name__ == "__main__":
    unittest.main()
