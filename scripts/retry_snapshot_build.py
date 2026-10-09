"""Retry only failed immutable Ubuntu Snapshot downloads; never retry tests."""
import collections
import hashlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


def snapshot_error_body(url):
    try:
        with urllib.request.urlopen(url, timeout=10):
            return None  # A successful response cannot explain rejected bytes.
    except urllib.error.HTTPError as error:
        with error:
            if error.code not in (500, 502, 503, 504) or error.geturl() != url:
                return None
            data = error.read(65537)
            if len(data) > 65536:
                return None
            return error.code, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError):
        return None


def snapshot_transport_failure(lines, probe=snapshot_error_body):
    text = ''.join(lines)
    # Integrity and authentication failures require investigation, even when an
    # earlier download in the same attempt also encountered a transport error.
    if re.search(r'hash sum mismatch|GPG error|NO_PUBKEY', text, re.I):
        return False
    errors = [line for line in lines if 'ERROR:' in line]
    if not errors:
        return False
    terminal = errors[-1]
    if 'digest mismatch' in text.lower():
        mismatch = re.search(r'digest mismatch sha256:([0-9a-f]{64}): sha256:([0-9a-f]{64})', terminal)
        if not mismatch:
            return False
        observed, expected = mismatch.groups()
        # BuildKit may hash an HTTP error body before reporting its status.
        # Explain this only with a fresh HTTPS error response whose bytes match
        # the rejected digest. Neither response is used as an archive.
        adds = re.findall(r'ADD --checksum=sha256:' + expected +
                          r'\s+(https://snapshot\.ubuntu\.com/[^\s"<>]+)', text)
        if len(set(adds)) != 1:
            return False
        response = probe(adds[0])
        if response is None or response[0] not in (500, 502, 503, 504) or response[1] != observed:
            return False
        print(f'Rejected download matches an HTTP {response[0]} error body; '
              'no archive was accepted.', flush=True)
        return True
    if 'apt-get' in terminal and 'exit code: 100' in terminal:
        return bool(re.search(
            r'Failed to fetch https://snapshot\.ubuntu\.com/\S+\s+(500|502|503|504)\b', text))
    return bool(re.search(
        r'https://snapshot\.ubuntu\.com/\S+.*(?:status|response).*\b(500|502|503|504)\b',
        terminal, re.I))


def run(command, start=subprocess.Popen, sleep=time.sleep):
    if command[:2] != ['docker', 'build']:
        raise ValueError('Only docker build is supported')
    for attempt in range(1, 4):
        print(f'Immutable build attempt {attempt}/3', flush=True)
        process = start(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8', errors='replace', bufsize=1)
        tail = collections.deque(maxlen=500)
        for line in process.stdout:
            print(line, end='', flush=True)
            tail.append(line)
        code = process.wait()
        if code == 0:
            return 0
        if attempt == 3 or not snapshot_transport_failure(tail):
            return code if code > 0 else 1
        delay = attempt * 10
        print(f'Ubuntu Snapshot transport failed; retrying identical build in {delay}s. '
              'Versions, checksums and build arguments are unchanged.', flush=True)
        sleep(delay)
    raise AssertionError('unreachable')


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
