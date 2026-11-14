# #9 rewrite inventory and interfaces

## Base and sequencing

The inventory was taken from the actual #1 implementation at `1021070`, not from
an earlier draft. Main's documentation-only #8 change at `d991e39` was merged into
`feat/issue-1-local-freshrss` as `241f90d`, retaining both parents and reconciling
README runtime instructions with standards/adoption guidance. No runtime source
changed in that incorporation. #9 branches from `241f90d` in its own worktree.
The original ignored ownership/runtime state remains in its original checkout.

The adoption was integrated into #1's `feat/issue-1-local-freshrss` branch through
PR #11 at `f88ea85`, not directly into main. The final main-targeting PR must
resolve #1 and #9 together through one squash implementation commit. At this
main-PR preparation checkpoint, main integration remains pending. Publication,
review and the final merge remain separately authorized operations.

## Actual pre-rewrite surface

- Production: one executable `scripts/rss`, argparse, standard-library Python
  3.10+, seven operations: setup, start, status, fetch, verify, replace and stop.
  Helpers used import-time root/state/image globals; operations shared a lock only
  through the CLI dispatcher. Results/progress and selected child stdout were
  mixed into human-readable stdout.
- Callers: 20 unittest process tests invoked the script with `sys.executable`;
  real security/lifecycle tests invoked it directly. No other production Python
  callers or installed package entrypoints were present. README and the historical
  verification record supplied operator/test commands.
- Tests: 20 command cases with fake external executables and temporary state;
  five opt-in real Mac/HTTP tests with synthetic users and labelled disposable
  volumes. The latter also operate the real project's VM/application and require
  a public GitHub feed. Natural cron observation was an additional opt-in mode.
- Configuration: `RSS_STATE_DIR`, `RSS_PORT`, and four tracked non-secret files in
  `config/`. The complete FreshRSS data directory is a native Linux named volume,
  not the checkout's `.local`. That directory holds ownership/audit/lock records.
- No uv metadata/lock, managed hooks, developer commands or CI workflows existed.
  #8 introduced policy/discovery documentation only; #7 has not supplied a competing
  test entrypoint or workflow to reuse.

## Rewrite boundaries

`src/rss_aggregator/runtime.py` is the lifecycle module. `Runtime(root=None,
state_dir=None).run(operation)` returns operation-specific dictionaries or raises
`RuntimeFailure`/configuration/OS errors. Every caller takes the nonblocking
project lock, while composed operations remain internal and do not lock twice.
Instance path resolution replaces mutable import-time globals; even multiple
Python callers in one interpreter have independent state configuration.

`cli.py` is the thin Click adapter; `__main__.py` and installed `rss` expose the
same commands and JSON results. Existing `scripts/rss` becomes a thin locked-uv
shell launcher, preserving direct executable use and checkout-root resolution.
Python-executing that launcher and the prior unittest selection commands are
intentionally replaced, with all known callers updated.

`development.py` exposes callable setup/check/format/safe-fix operations and thin
`rss-dev` adapters. One check definition is reused by developers, pre-push and
GitHub Actions. No per-helper CLI, internal services, mandatory typechecker or
coverage-percentage target was added. Production signatures/private docstrings
follow the canonical standard; tests retain its documentation exemption.

Tool subprocesses use the selected checkout's local `.venv` interpreter in
isolated import mode, with ambient import/pytest overrides removed; changing
`--project` selects both the working directory and the editable package context.
Hook installation uses the same selected environment. Missing environments fail
without implicit synchronization or fallback to the invoking checkout's tools.
External/shared uv environments are not selected by these operations.

The baseline invokes Ruff lint with `--no-fix --no-fix-only`, explicitly
overriding selected-checkout autofix configuration. Configured `fix` or
`fix-only` mode must not turn checks into source rewriting or conceal a lint
failure. The explicit `fix` operation retains safe lint mutation; `format`
remains the separate formatting operation.

## Preserved safety contract

- Homebrew inventories and addition ledger, upgrade/reinstall refusal, no automatic
  update/cleanup/dependent changes; no host PHP/database installation.
- Rootless VM `rss-aggregator`, initially 2 CPUs / 2048 MiB / 20 GiB; explicit
  machine/connection targeting, unchanged defaults and identity comparisons.
- UUID ownership labels and refusal to adopt foreign machines/connections,
  containers or volumes; no stopping another VM or unrelated running containers.
- Official digest/platform/version/revision/entrypoint checks; effective Apache PHP
  and upload-progress verification; private internal 8088 listener until probe
  removal, then Mac-loopback-only publication. No custom image or startup override.
- Named-volume persistence, no volume deletion during container replacement,
  writer termination before VM stop, repeated operation behavior and retained
  failure resources. No automatic startup or destructive convenience operation.
- Startup actualization and `*/20` cron in UTC, with unchanged upstream HTTP
  cache/TTL/backoff policy; no automatic article purge, extra scraping or extensions.
- Approved shutdown backup deferral until #2 exists. Backups, tailnet/ReadKit,
  paid feeds and destructive cleanup remain unimplemented.

The new JSON status uses application `unknown` when the VM is stopped and its
container cannot be inspected; it does not invent container deletion. Runtime
failures emit no success result, diagnostics stay on stderr, and usage errors
retain a distinct nonzero exit. Full result fields are documented in README.

## Coverage migration

| Prior coverage | Current seam/location |
| --- | --- |
| Package upgrades and dependency reinstall refusal | Parameterized process tests in `tests/test_commands.py`. |
| Simulated-clean/repeated setup, original inventory/additions, VM resources/defaults | Stateful external-tool substitute and process/filesystem results in the same suite. |
| Unowned machine/volume and replaced identity | Process tests, including setup and stop refusal. |
| Old PHP, enabled upload progress, wrong web SAPI | Parameterized process failures before publication. |
| Other running machine, occupied/invalid ports, TCP TIME_WAIT | Process/socket tests; no real Podman resources. |
| Repeat stop, command concurrency, stop failure, unrelated containers, transitional VM | Process/lock tests; failure leaves foreign/native resources untouched. |
| Pending installer, unreachable engine, absent status | Parsed JSON plus diagnostics/exit/observable effects; stopped-state accuracy added. |
| Callable configuration/locking without Click | `tests/test_json_contract.py`, at the public Python interface where one-process configuration isolation matters. |
| Effective web security failure/removal/recovery | Explicit `live` test in `tests/integration/test_live_gate.py`. |
| Installer/login/public articles, read/star persistence, replacement/VM restart | Explicit `live` tests in `tests/integration/test_live_instance.py`. |
| Cron observer, changed controlled feed, port migration, optional natural cron | Same live suite and labelled fixture helpers; no original cases removed. |
| Quality gate, actual push success/three failure kinds, hook arrangements | `tests/test_development.py`, using disposable Git repositories/local bare remotes and source snapshots. |

Default tests do not contact Homebrew, Podman, GitHub or production services.
The initial uv sync may download tool prerequisites; disposable quality-gate
fixtures subsequently use only cached/offline dependencies and local Git remotes.
Missing explicitly selected live prerequisites fail rather than producing skips.

The broader #7 contract remains intact: native Linux rootless-container CI,
controlled publication error cases, additional cleanup/interruption checks,
feature coverage matrix and Mac/provider/manual-device responsibilities. The
retained live suite still uses GitHub content and interrupts the project session;
that limitation is visible, not disguised as deterministic integration coverage.
