import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'retry_snapshot_build', Path(__file__).resolve().parents[1] / 'retry_snapshot_build.py')
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)

APT_FAILURE = ('E: Failed to fetch https://snapshot.ubuntu.com/ubuntu/fixed/pkg.deb '
               '503 Service Unavailable\nERROR: failed to build: apt-get install '
               'did not complete successfully: exit code: 100\n')


class SnapshotRetryTests(unittest.TestCase):
    def execute(self, attempts):
        commands = []
        delays = []
        def start(command, **kwargs):
            commands.append(command[:])
            code, text = attempts.pop(0)
            class Process:
                stdout = io.StringIO(text)
                def wait(self): return code
            return Process()
        command = ['docker', 'build', '-f', 'Dockerfile.p0', '--build-arg',
                   'PG_QDRANT_SOURCE_SHA=exact-sha', '-t', 'test-image', '.']
        with patch('sys.stdout', new=io.StringIO()) as output:
            code = retry.run(command, start=start, sleep=delays.append)
        self.assertTrue(all(value == command for value in commands))
        return code, len(commands), delays, output.getvalue()

    def test_transport_recovery_retains_identical_arguments_and_failed_log(self):
        code, count, delays, output = self.execute([(1, APT_FAILURE), (0, 'build passed\n')])
        self.assertEqual((code, count, delays), (0, 2, [10]))
        self.assertIn(APT_FAILURE, output)
        self.assertIn('build passed', output)

    def test_persistent_transport_failure_is_bounded_and_fails(self):
        code, count, delays, output = self.execute([(100, APT_FAILURE)] * 3)
        self.assertEqual((code, count, delays), (100, 3, [10, 20]))
        self.assertEqual(output.count('503 Service Unavailable'), 3)

    def test_only_selected_snapshot_http_errors_are_retryable(self):
        for status in [500, 502, 503, 504]:
            with self.subTest(status=status):
                self.assertTrue(retry.snapshot_transport_failure(
                    APT_FAILURE.replace('503', str(status)).splitlines(keepends=True)))
        for status in [400, 401, 403, 404, 429, 501]:
            with self.subTest(status=status):
                self.assertFalse(retry.snapshot_transport_failure(
                    APT_FAILURE.replace('503', str(status)).splitlines(keepends=True)))
        self.assertTrue(retry.snapshot_transport_failure([
            'ERROR: download https://snapshot.ubuntu.com/ubuntu/fixed/ca.deb returned response status 503\n']))

    def test_compile_integrity_authentication_and_unrelated_host_are_not_retried(self):
        for failure in [APT_FAILURE + 'ERROR: cargo build failed: exit code: 101\n',
                        APT_FAILURE + 'ERROR: digest mismatch sha256:wrong\n',
                        APT_FAILURE + 'Hash Sum mismatch\n',
                        APT_FAILURE + 'GPG error\n',
                        APT_FAILURE.replace('snapshot.ubuntu.com', 'unrelated.example'),
                        'ERROR: failed to build: syntax error\n']:
            with self.subTest(failure=failure):
                code, count, delays, _ = self.execute([(1, failure)])
                self.assertEqual((code, count, delays), (1, 1, []))

    def test_success_is_not_retried_despite_previous_transport_messages(self):
        self.assertEqual(self.execute([(0, APT_FAILURE)])[:3], (0, 1, []))

    def test_non_build_commands_are_refused_before_execution(self):
        with self.assertRaises(ValueError):
            retry.run(['docker', 'run', 'test-image'], start=lambda *a, **k: self.fail())

    def test_buildkit_digest_requires_matching_live_http_error_bytes_and_pinned_url(self):
        expected = 'a' * 64
        observed = 'b' * 64
        url = 'https://snapshot.ubuntu.com/ubuntu/fixed/ca.deb'
        lines = [f'ADD --checksum=sha256:{expected} {url} /tmp/ca.deb\n',
                 f'ERROR: digest mismatch sha256:{observed}: sha256:{expected}\n']
        probes = []
        def confirmed(value):
            probes.append(value)
            return 503, observed
        with patch('sys.stdout', new=io.StringIO()):
            self.assertTrue(retry.snapshot_transport_failure(lines, probe=confirmed))
        self.assertEqual(probes, [url])
        for response in [None, (503, 'c' * 64), (200, observed)]:
            self.assertFalse(retry.snapshot_transport_failure(lines, probe=lambda u: response))
        self.assertFalse(retry.snapshot_transport_failure(
            [lines[0].replace('snapshot.ubuntu.com', 'unrelated.example'), lines[1]],
            probe=lambda u: self.fail()))
        self.assertFalse(retry.snapshot_transport_failure(
            [lines[0].replace(expected, 'c' * 64), lines[1]], probe=lambda u: self.fail()))
        self.assertFalse(retry.snapshot_transport_failure(
            lines + ['GPG error\n'], probe=lambda u: self.fail()))

    def test_http_observation_never_accepts_success_bad_status_or_large_body(self):
        import hashlib
        import urllib.error
        url = 'https://snapshot.ubuntu.com/ubuntu/fixed/ca.deb'
        for status, data, expected in [(503, b'503 response', (503, hashlib.sha256(b'503 response').hexdigest())),
                                       (404, b'missing', None), (503, b'x' * 65537, None)]:
            with self.subTest(status=status, length=len(data)):
                error = urllib.error.HTTPError(url, status, 'error', {}, io.BytesIO(data))
                with patch.object(retry.urllib.request, 'urlopen', side_effect=error):
                    self.assertEqual(retry.snapshot_error_body(url), expected)
        with patch.object(retry.urllib.request, 'urlopen', side_effect=TimeoutError()):
            self.assertIsNone(retry.snapshot_error_body(url))
        redirected = urllib.error.HTTPError(url + '/redirect', 503, 'error', {}, io.BytesIO(b'error'))
        with patch.object(retry.urllib.request, 'urlopen', side_effect=redirected):
            self.assertIsNone(retry.snapshot_error_body(url))
        with patch.object(retry.urllib.request, 'urlopen', return_value=io.BytesIO(b'archive')):
            self.assertIsNone(retry.snapshot_error_body(url))


if __name__ == '__main__': unittest.main()
