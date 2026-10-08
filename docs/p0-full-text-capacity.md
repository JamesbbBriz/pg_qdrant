# P0 full-text tmpfs capacity follow-up

Status: all four bounded disk/capacity profiles passed in
[CI7](evidence/p0-source-capacity-oom-ci.json), head `88885db`. Both independent
384 MiB full-text reopens passed, as did the exact 128 MiB zero-write refusal.
The earlier [local record](evidence/p0-integrated-local.json) separately covers
guard/wrapper tests. These results do not establish dirty-ingestion recovery,
a production capacity recommendation or a complete P0 exit.

The historical 128 MiB full-text runs remain failures. In CI6
([run 37732857051](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37732857051)),
both configuration-save recovery and a separate clean control terminated with
SIGBUS at `recovery_reopen`. Immediately before reopen, each fixture had
34,963,456 allocated bytes and 99,254,272 free bytes. The clean fixture had
289,807,352 logical bytes; the configuration retry increased the other fixture's
logical size by 110 bytes. After either child exited, allocation reached exactly
134,217,728 bytes and the dedicated tmpfs had zero free bytes. The clean control
created no filler. These are distinct observations of logical size, allocated
storage and process mapping behavior, not proof of a repaired loader or safe
dirty-data recovery.

The follow-up changes the fixed full-text capacity allowance to
**exactly 384 MiB (402,653,184 bytes)**. It retains eight fixture rows, all four
representations, tenant/document/SKU keyword indexes, both mutable text indexes,
the original fixture settings, and the existing record/phrase/token-prefix/
MaxSim assertions. The larger allowance supplies bounded headroom for the
observed footprint; it does not predict a passing reopen. No caller chooses an
arbitrary capacity and no failure automatically retries with a larger mount.

| Path | Required dedicated tmpfs capacity | Passing evidence |
| --- | --- | --- |
| Vector/keyword ENOSPC | Greater than zero, at most 64 MiB; CI remains 32 MiB | Existing native successful configuration-save failure/retry/reopen report |
| Full-text clean control | Exactly 384 MiB | Successful native clean reopen, preserved records and query assertions; no filler |
| Full-text ENOSPC | Exactly 384 MiB | Successful native ENOSPC/configuration-retry/reopen report with full-text assertions |
| Full-text 128 MiB capacity characterization | Exactly 128 MiB, guard-only | Both ordinary full-text entry paths produce the exact expected capacity refusal before temporary-directory or engine creation; unchanged empty mount |

The existing realpath, owner, mode 0700, empty directory, dedicated writable
mount-root, current-namespace mount-alias and PostgreSQL-data separation guards
remain in force. Both ordinary full-text paths call the same exact-capacity
predicate before any owned directory or fixture is created. Undersized and
oversized mounts fail at that boundary. The read-only post-exit observer uses
the selected profile's capacity bound: 64 MiB for vector/keyword, 384 MiB for
full-text and exactly 128 MiB for the refusal characterization. Its depth,
file-count, directory-count and 512 MiB logical metadata limits are unchanged.

The filler remains bounded by the validated mount capacity and its existing
15-second deadline. Native wrapper execution remains limited to 90 seconds.
Container memory/CPU limits, sparse-copy limits, source data and every other
budget are unchanged. No existing upstream mmap/load behavior is changed.

## Separate refusal characterization

```sh
python3 scripts/run_disk_probe.py --profile full-text \
  --characterize-128-capacity-refusal
```

This runs `--full-text-128-capacity-refusal-probe` in the native executable. It
first validates an empty dedicated mount of exactly 128 MiB and checks the
shared predicate's exact expected refusal. It then exercises both ordinary
full-text entry functions. The ENOSPC entry must return its exact guard rejection
with code 2 and `disk_writes_attempted=false`; the clean entry must return the
same capacity error. An unrelated refusal is a failure. The root mount identity,
empty contents and storage observations must remain unchanged.

The wrapper accepts only a successful native report of the separate
`edge_full_text_128_capacity_refusal_probe` kind with all required fields and
stages. It independently compares empty pre/post mount observations. SIGBUS,
another signal, nonzero process exit, timeout, malformed output, an unrelated
error, `not_run`, an unexpected stage, changed mount identity, files, directories
or allocation cannot pass. The ordinary clean and ENOSPC wrapper modes still
require their own successful native report kinds and cannot accept this report
as recovery evidence. The characterization explicitly reports no engine open,
ENOSPC verification or recovery verification.

## CI experiment profiles

The workflow keeps the existing 32 MiB vector/keyword step and
replaces the two former 128 MiB full-text execution steps with separate clean and
fault containers using:

```text
--tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=384m,uid=10001,gid=10001,mode=0700
--env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults
```

Their commands remain `scripts/run_disk_probe.py --profile full-text --clean`
and `scripts/run_disk_probe.py --profile full-text`. A third fresh container uses
`size=128m` only for the separately named refusal characterization above.
All containers retain `--memory=5g --cpus=2 --network=none`. The steps depend on
the image build and run independently of unrelated experiment failures; no
`continue-on-error` or success conversion is added. Evidence collection includes
the new `edge-full-text-128-capacity-refusal.json` artifact.

Local Rust validation passes the normal 23-test engine suite, including the new
capacity tests, and all 13 disk-wrapper tests pass as part of the integrated
21-test Python suite. Neither local command uses a positive fault mount. The
exact 128 MiB refusal and both 384 MiB native runs subsequently passed in CI7.
Each reopened full-text fixture used 220,508,160 allocated bytes, leaving
182,145,024 bytes available. Historical CI5/CI6 failures remain failures; the
384 MiB pass cannot
retrospectively make the 128 MiB reopen supported or prove WAL growth, dirty
ingestion, PostgreSQL ACK durability, power-loss recovery or P0 completion.
