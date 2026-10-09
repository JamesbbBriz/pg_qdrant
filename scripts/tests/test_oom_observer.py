"""Daemon-host observation must bind the same native identity and kernel victim."""
import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.oom_observer import KERNEL_BYTES, narrow_kernel, parse_kernel, read_kernel, read_mapping


class Observer(unittest.TestCase):
    def test_no_kernel_messages_stays_empty_and_malformed_data_refuses(self):
        self.assertEqual(parse_kernel(b""), [])
        for data in [b"invalid\n", b"<6>[1.000000] truncated", b"x" * KERNEL_BYTES,
                     b"<6>[1.2] unknown precision\n", b"<192>[1.000000] invalid priority\n"]:
            with self.subTest(data=data[:40]), self.assertRaises(AssertionError):
                parse_kernel(data)
        self.assertEqual(parse_kernel(b"<14>[1.000002] Memory cgroup out of memory: Killed process 77\n"), [])

    def test_fixed_buffer_read_uses_only_nonconsuming_action_and_refuses_full_buffer(self):
        data = b"<6>[1.000002] hello\n"
        class Kernel:
            def __init__(self, size):
                self.size = size

            def __call__(self, action, buffer, budget):
                self.args = action, budget
                ctypes.memmove(buffer, data, len(data))
                return self.size

        for size in [len(data), -1, KERNEL_BYTES]:
            call = Kernel(size)
            library = type("Library", (), {"klogctl": call})()
            with patch("scripts.oom_observer.ctypes.CDLL", return_value=library):
                if size == len(data):
                    self.assertEqual(read_kernel(), [{"msg": "hello", "time": "1.000002", "time_us": 1000002}])
                else:
                    with self.assertRaises(OSError if size == -1 else AssertionError):
                        read_kernel()
            self.assertEqual(call.args, (3, KERNEL_BYTES))

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
        mapping = {"host_pid": 77, "host_cgroup": "/docker/" + container, "kernel_log_cursor_us": 123455000}
        rows = parse_kernel(("<6>[123.456000] " + selector + "\n<3>[123.457000] " + victim
                             + "\n<6>[123.450000] unrelated kernel data\n"
                             + "<3>[123.458000] Memory cgroup out of memory: Killed process 777 (other)\n").encode())
        result = narrow_kernel(rows, mapping, container)
        self.assertEqual([r["MESSAGE"] for r in result], [selector, victim])
        self.assertEqual(result[0]["__MONOTONIC_TIMESTAMP"], "123.456000")
        self.assertNotIn("__REALTIME_TIMESTAMP", result[0])

    def test_pre_barrier_or_equal_cursor_records_cannot_attribute_a_new_fault(self):
        container = "a" * 64
        mapping = {"host_pid": 77, "host_cgroup": "/docker/" + container, "kernel_log_cursor_us": 1000002}
        line = "Memory cgroup out of memory: Killed process 77 (native owner)"
        rows = parse_kernel(("<3>[1.000001] " + line + "\n<3>[1.000002] " + line + "\n").encode())
        self.assertEqual(narrow_kernel(rows, mapping, container), [])

    def test_kernel_projection_refuses_budget_overflow(self):
        container = "a" * 64
        mapping = {"host_pid": 77, "host_cgroup": "/docker/" + container, "kernel_log_cursor_us": 0}
        with self.assertRaises(AssertionError):
            narrow_kernel([{"msg": "Memory cgroup out of memory: Killed process 77 " + "x" * 16384,
                            "time_us": 1}], mapping, container)
        with self.assertRaises(AssertionError):
            narrow_kernel([{"msg": "Memory cgroup out of memory: Killed process 77 (native owner)",
                            "time_us": 1}] * 33, mapping, container)
