import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oom_allocation_snapshot import allocation_snapshot


class AllocationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.target = {"pid": 42, "start_ticks": 12345}
        fields = ["0"] * 22
        for index, value in {0: "R", 7: "17", 9: "19", 11: "23", 12: "29", 19: "12345"}.items():
            fields[index] = value
        self.values = {"memory.stat": "anon 101\nfile 202\npgscan_direct 303\nunused 999\n",
            "cpu.stat": "usage_usec 404\nnr_throttled 5\nthrottled_usec 606\nunused 999\n",
            "memory.pressure": "some avg10=0.00 total=707\nfull avg10=0.00 total=808\n",
            "stat": "42 (owner (native) worker) " + " ".join(fields)}

    def read(self, path):
        return self.values[Path(path).name]

    def test_exact_thread_fields_and_selected_cgroup_counters(self):
        seen = []
        def read(path):
            seen.append(str(path))
            return self.read(path)
        with patch("oom_allocation_snapshot.os.sysconf", return_value=100):
            result = allocation_snapshot(self.target, read)
        self.assertEqual(result["target"], {"start_ticks": 12345, "user_ticks": 23,
            "system_ticks": 29, "minor_faults": 17, "major_faults": 19, "clock_ticks": 100})
        self.assertEqual(result["memory_stat"], {"anon": 101, "file": 202, "pgscan_direct": 303})
        self.assertEqual(result["cpu_stat"], {"usage_usec": 404, "nr_throttled": 5, "throttled_usec": 606})
        self.assertEqual(result["pressure_us"], {"some": 707, "full": 808})
        self.assertIn(str(Path("/proc/42/task/42/stat")), seen)

    def test_pid_reuse_never_exports_unrelated_cpu_or_faults(self):
        self.target["start_ticks"] = 54321
        result = allocation_snapshot(self.target, self.read)
        self.assertTrue(result["target_identity_changed"])
        self.assertNotIn("target", result)

    def test_disappeared_target_keeps_global_observation(self):
        for error in (FileNotFoundError, ProcessLookupError):
            def read(path):
                if Path(path).name == "stat":
                    raise error("gone")
                return self.read(path)
            result = allocation_snapshot(self.target, read)
            self.assertTrue(result["target_gone"])
            self.assertNotIn("target", result)
            self.assertEqual(result["memory_stat"]["anon"], 101)

    def test_unknown_read_error_is_not_reported_as_target_gone(self):
        def read(path):
            if Path(path).name == "stat":
                raise PermissionError("observation denied")
            return self.read(path)
        with self.assertRaises(PermissionError):
            allocation_snapshot(self.target, read)

    def test_missing_optional_counters_stay_absent(self):
        self.values["memory.stat"] = "anon 0\n"
        self.values["cpu.stat"] = "usage_usec 0\n"
        with patch("oom_allocation_snapshot.os.sysconf", return_value=100):
            result = allocation_snapshot(self.target, self.read)
        self.assertEqual(result["memory_stat"], {"anon": 0})
        self.assertEqual(result["cpu_stat"], {"usage_usec": 0})

    def test_malformed_pressure_duplicate_or_missing_total_refused(self):
        for value in ("some avg10=0.0\n", "some total=1 total=2\n",
                      "some total=1\nsome total=2\n", "unknown total=1\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.values["memory.pressure"] = value
                allocation_snapshot(self.target, self.read)

    def test_duplicate_selected_counter_refused(self):
        self.values["memory.stat"] = "anon 1\nanon 2\n"
        with self.assertRaises(ValueError):
            allocation_snapshot(self.target, self.read)

    def test_malformed_or_wrong_thread_identity_refused(self):
        original = self.values["stat"]
        for value in ("truncated", "42 (owner) R 0", original.replace("42 (", "43 (", 1)):
            with self.subTest(value=value), self.assertRaises((ValueError, IndexError)):
                self.values["stat"] = value
                allocation_snapshot(self.target, self.read)


if __name__ == "__main__":
    unittest.main()
