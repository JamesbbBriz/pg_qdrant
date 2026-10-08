# Historical CI hold — unfinished draft branches

The integration branch restores automatic P0 push and pull-request triggers.
Installed source-to-Edge tests run in the managed-helper fault build. The
entries below record the earlier hold, rather than the current CI policy.

As requested on 2026-10-08, the P0 GitHub Actions workflow retains all its
jobs and test scripts. Its automatic push/pull_request events are temporarily
removed. It remains runnable by workflow_dispatch.

This is NOT evidence of passing tests. The resulting missing or skipped checks
must NOT be reported as green. PRs remain Draft until the phase acceptance
criteria, new tests and original P0 regressions pass.

## Restore before any merge/release

Edit .github/workflows/p0.yml and restore these exact triggers:

    on:
      push:
      pull_request:
      workflow_dispatch:

Run all configured P0 tests plus new P1–P5 compile/SQL, concurrency, security,
crash/durability, quality, resource and migration tests against the final merge
candidate. Record exact SHA, logs, artifacts, ignored/failed checks and the
supported host/dependency matrix. Make these jobs required in branch protection
before publication.

A manually dispatched P0 workflow only proves P0 coverage. It is not a
substitute for newly required phase-specific tests.
