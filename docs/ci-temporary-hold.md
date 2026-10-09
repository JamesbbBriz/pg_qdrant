# Local CI execution policy

CI runs locally through act. GitHub-hosted workflow execution is disabled; the
workflow entry point lives in [ci/verify.yml](../ci/verify.yml), outside the
GitHub workflow discovery directory. Historical hosted reports remain evidence
only for their recorded revisions.

Run `python scripts/local_ci.py` for the complete engine, installed PostgreSQL,
transaction ledger, crash, disk, kernel OOM and static checks. The step registry
is `ci/steps.json`; both the act workflow and a prepared Linux snapshot call
`scripts/verify.py`. `--profile static`, `engine`, or `ledger` selects a subset;
subset success cannot satisfy full CI or product acceptance.

The wrapper requires Python 3.11+, Git, Docker with Linux containers and act
0.2.89. It builds the diagnostic runner, copies tracked and non-ignored input
files into a separate snapshot, hashes every input and Cargo.lock, and runs act
on an isolated bridge network. No environment or secret file is inherited.
Product dependency and native build pins remain in Dockerfile.p0. The runner's
resolved image ID, act/Docker/Python versions, commands, exit codes, complete
step logs and snapshot manifest are exported under `artifacts/local-ci/<run>/`.
A dirty snapshot is explicitly identified and cannot be used for a release.

Each invocation has unique image tags, labeled test containers and a unique
network. Existing containers and databases are untouched. Successful test
containers are removed by inspected ID after evidence collection. Failed test
containers are retained with their IDs and states in the report for diagnosis;
remove only those inspected IDs after preserving any required evidence. The
wrapper exports the runner's artifacts before removing its exact labeled ID.
Export or cleanup failure fails the run. Automatic cancellation of superseded
local runs is not implemented; avoid overlapping heavy builds on a small host.

The full pipeline includes actual 768 MiB kernel OOM experiments in isolated
containers with no host mounts, swap, network or extra capabilities, as well as
separate disk-exhaustion mounts. A separate observer uses a read-only root
filesystem, 256 MiB with no swap, 16 PIDs, no network, host PID/cgroup visibility
and only SYSLOG capability to read the Docker daemon host kernel. This does not
change the fault container namespace, capability or mount guards. Unavailable
kernel records fail strict attribution; no correlated-only result passes.
Existing strict victim-attribution checks and
failure deadlines remain in force. These tests do not establish production
memory isolation or PostgreSQL/Edge atomic WAL semantics.

Before merge or release, verify the exact final source with all applicable new
P1-P5 tests and original regressions. Missing, skipped, cancelled and failed
gates are not passing. The release checker requires checksum-verified full
local act run/verification receipts plus every formal acceptance record and
artifact; moving execution locally does not waive any of the 45 acceptance
items, 54 capabilities, 70 work items or eight user journeys. Nothing in this
policy authorizes a merge, tag or release.
