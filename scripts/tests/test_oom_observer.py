"""Daemon-host observation must bind the same native identity and kernel victim."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.oom_observer import narrow_kernel, parse_dmesg, read_mapping


class Observer(unittest.TestCase):
    def test_no_kernel_messages_stays_empty_and_malformed_data_refuses(self):
        self.assertEqual(parse_dmesg(b""), [])
        self.assertEqual(parse_dmesg(b'{"dmesg":[]}'), [])
        with self.assertRaises(ValueError):
            parse_dmesg(b"invalid")
        with self.assertRaises(AssertionError):
            parse_dmesg(b'{"dmesg":null}')

    def fixture(self, root, pid, *, state="S", ticks=123, container="a" * 64, nspid="77 9"):
        proc = root / str(pid)
        proc.mkdir()
        (proc / "status").write_text("NSpid:\t" + nspid + "\n")
        fields = [state] + ["0"] * 18 + [str(ticks)]
        (proc / "stat").write_text(str(pid) + " (native owner) " + " ".join(fields))
        (proc / "cgroup").write_text("0::/docker/" + container + "\n")

    def test_exact_pid_start_and_cgroup_and_disappeared_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, 77)
            result = read_mapping([77, 88], {"pid": 9, "start_ticks": 123}, "a" * 64, root)
            self.assertEqual(result, {"host_pid": 77, "container_pid": 9, "start_ticks": 123,
                                      "host_cgroup": "/docker/" + "a" * 64})

    def test_rejects_reused_pid_zombie_wrong_container_or_namespace(self):
        for values in [{"ticks": 999}, {"state": "Z"}, {"container": "b" * 64}, {"nspid": "77"}]:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, 77, **values)
                with self.assertRaises(AssertionError):
                    read_mapping([77], {"pid": 9, "start_ticks": 123}, "a" * 64, root)

    def test_ambiguous_mapping_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, 77)
            self.fixture(root, 88)
            with self.assertRaises(AssertionError):
                read_mapping([77, 88], {"pid": 9, "start_ticks": 123}, "a" * 64, root)

    def test_kernel_projection_keeps_native_text_and_monotonic_clock(self):
        container = "a" * 64
        selector = "oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/docker/" + container + ",pid=77"
        victim = "Memory cgroup out of memory: Killed process 77 (native owner)"
        mapping = {"host_pid": 77, "host_cgroup": "/docker/" + container}
        rows = [{"msg": selector, "time": "123.456"}, {"msg": victim, "time": "123.457"},
                {"msg": "unrelated kernel data", "time": "123.450"},
                {"msg": "Memory cgroup out of memory: Killed process 777 (other)", "time": "123.451"}]
        result = narrow_kernel(rows, mapping, container)
        self.assertEqual([r["MESSAGE"] for r in result], [selector, victim])
        self.assertEqual(result[0]["__MONOTONIC_TIMESTAMP"], "123.456")
        self.assertNotIn("__REALTIME_TIMESTAMP", result[0])

    def test_kernel_projection_refuses_budget_overflow(self):
        container = "a" * 64
        mapping = {"host_pid": 77, "host_cgroup": "/docker/" + container}
        with self.assertRaises(AssertionError):
            narrow_kernel([{"msg": "Memory cgroup out of memory: Killed process 77 " + "x" * 16384}], mapping, container)
