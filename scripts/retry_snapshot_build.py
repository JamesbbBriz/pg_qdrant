"""Retry only failed immutable Ubuntu Snapshot downloads; never retry tests."""
import collections
import re
import subprocess
import sys
import time


def snapshot_transport_failure(lines):
    text = ''.join(lines)
    # Integrity and authentication failures require investigation, even when an
    # earlier download in the same attempt also encountered a transport error.
    if re.search(r'digest mismatch|hash sum mismatch|GPG error|NO_PUBKEY', text, re.I):
        return False
    errors = [line for line in lines if 'ERROR:' in line]
    if not errors:
        return False
    terminal = errors[-1]
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
