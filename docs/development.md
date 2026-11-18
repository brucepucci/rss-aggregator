# Development and verification

This is the repository-specific adoption/setup guide for the reusable
[Python standard](python-standards.md). Use the [glossary](../GLOSSARY.md) for domain
vocabulary and [architecture/ADRs](architecture.md) for design constraints.
Agent-only procedures are indexed separately in [AGENTS.md](../AGENTS.md). The [CLI reference](cli.md) owns exhaustive
command/flag/output contracts; the [operator guide](../README.md) and
[operations guide](operations.md) own runtime setup and lifecycle.

## Environment and baseline

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) on
`PATH`; uv 0.13.8 is the CI/tooling reference. From the checkout:

```sh
uv sync --locked
uv run --locked rss-dev check
```

The initial sync creates/synchronizes checkout-local `.venv`, installs locked
runtime/development dependencies and the editable project, and may download the
requested Python, packages and build backend. `.python-version` requests Python
**3.13.18**. uv may reuse a matching installed interpreter; the pin does not force
a uv-downloaded interpreter or authorize upgrading Homebrew Python. No global pip
installation, hook activation or FreshRSS startup occurs. Declared production
compatibility is Python **3.10+**, with Ruff targeting 3.10; compatibility declaration
is not evidence that every supported version has been tested.

The baseline uses installed tools and isolated fixtures without credentials,
external network access, production services or real Podman resources. Disposable
Git fixtures synchronize offline from the warmed cache. Missing caches fail rather
than skip: rerun initial sync instead of allowing network inside tests. No live
runtime setup is needed for the default suite. Initial sync itself can require
network access; `--offline` only works when the needed resources are cached.

The shared check-only gate runs all three tools, even if an earlier one fails:

| Check | Child invocation after the selected `.venv` interpreter |
| --- | --- |
| Complete default tests | `-I -B -m pytest -p no:cacheprovider -m 'not live'` |
| Ruff lint | `-I -B -m ruff check --no-fix --no-fix-only --no-cache` |
| Ruff format check | `-I -B -m ruff format --check --no-cache` |

Python `-I` selects isolated import mode, `-B` suppresses bytecode writes and `-m`
executes a module. Pytest's explicit marker excludes live tests and its cache plugin
is disabled. Ruff's `--no-fix --no-fix-only` overrides configured autofixes;
`--check` checks formatting rather than rewriting it, and `--no-cache` disables
its cache. This gate never silently formats/fixes source. Tool output goes to stderr;
stdout is the [developer CLI JSON result](cli.md#developer-commands-and-results).

For intentional edits, use `uv run --locked rss-dev format` (formatting only) or
`uv run --locked rss-dev fix` (safe lint fixes only, unsafe fixes disabled, no
formatting), then rerun the full baseline. Neither is a prerequisite that a hook
may run silently. Direct `uv run --locked pytest` selects the default suite, with
live tests collected but deselected; paths/marker choices can narrow direct runs,
not replace the required baseline. See [pytest controls](cli.md#pytest-selection).

### Selected-checkout isolation

`rss-dev check`, `format` and `fix` run with the selected checkout's **own `.venv`**,
not the invoking checkout's editable installation. For example, first synchronize
B with `uv sync --locked --project /path/to/B`, then use
`uv run --locked rss-dev check --project /path/to/B` from A. `--project` defaults
to the process working directory. Missing local interpreters/tools fail, without
fallback or automatic environment creation; `UV_PROJECT_ENVIRONMENT` external/shared
environments are not selected by the developer commands.

All developer operations remove inherited `PYTHONPATH`, `PYTHONHOME`, `PYTEST_*`
variables and these repository selectors from child environments:

```text
GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_INDEX_FILE
GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_GRAFT_FILE
GIT_SHALLOW_FILE GIT_REPLACE_REF_BASE GIT_NO_REPLACE_OBJECTS
```

A push hook's Git context must not redirect tests/setup into another repository.
This changes child environments only, not the parent push process. Git
configuration/authentication variables remain, so global/system configuration and
`core.hooksPath` safeguards still apply. `PYTHONDONTWRITEBYTECODE=1` is set in
children as well. Direct pytest retains normal pytest environment handling; the
shared gate is the guarded entrypoint.

Hook-refusal permutations use minimal disposable repositories and installed CLI
code without repeatedly synchronizing environments. Full environments remain for
installation, actual disposable pushes and A/B isolation tests. Fixture-only import
sentinels verify selected-code isolation without rewriting production source or
relying on unrelated test counts. Runtime root/configuration tests are separate. Daily-backup regressions use the
same CLI/Runtime seam, private temporary destinations, synthetic source data,
controlled launchctl/Podman responses and an aware `now` at the public orchestration
boundary. No actual LaunchAgent is installed by the default suite. Coverage includes
opt-in/idempotence, eligibility/missed/stale work, bounded retry and contention,
changed ownership/code/dependencies, stopped intent, archive/resumption separation
and prior-archive preservation. Disposable processes are SIGKILLed during export
and immediately before/after hard-link publication; surviving external exports
retain the lock and block reconciliation until finished. These are real process/
filesystem boundaries, not ordinary exception-unwinding substitutes.
Readiness regressions use controlled Podman responses plus a disposable loopback
HTTP server, not live-VM fault injection.

## Development configuration

These tracked settings are non-secret. Apply deliberate dependency changes with
`uv lock`, then `uv sync --locked` and the full baseline; normal setup/checks do
not implicitly update the lock.

| File | Purpose/default | Change and apply |
| --- | --- | --- |
| [`pyproject.toml`](../pyproject.toml) | Python `>=3.10`; runtime `click>=8.1,<9`; development group `pytest>=8,<10`, `ruff>=0.14,<0.15`, `pre-commit>=4,<5`; Hatchling build backend `>=1.27,<2`; `rss` and `rss-dev` entrypoints; Ruff rules and default pytest selection. | Review metadata changes, intentionally regenerate `uv.lock` when needed, synchronize and run the baseline. Production package is top-level `rss_aggregator/`. |
| [`.python-version`](../.python-version) | Requested Python `3.13.18`; interpreter selection managed through uv. | Update intentionally, retain compatible project/Ruff targets and verify a fresh sync; no silent host-package upgrade. |
| [`uv.lock`](../uv.lock) | Committed application dependency versions/hashes with compatibility markers. | Regenerate only for intentional dependency changes; routine commands use `--locked`. |
| [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) | One local/system `development-check` hook at pre-push; `always_run: true`, `pass_filenames: false`, entry `uv run --locked --offline rss-dev check`. | Keep aligned with the shared baseline; explicitly run guarded setup to activate hooks. File selection must not narrow the complete suite. |
| [`.github/workflows/python.yml`](../.github/workflows/python.yml) | Same baseline on PRs/main updates; `ubuntu-latest`, uv `0.13.8`, immutable action references, read-only contents permission. | Review/verify changes. A workflow definition is not proof of execution or branch protection; see historical evidence below. |

Ruff selects **`E4,E7,E9,F,I,D,UP`**: baseline errors, unused imports/names, import
sorting, NumPy-compatible docstrings and modernization. It uses the NumPy convention
and `py310` target; `tests/**/*.py` is exempt from `D`. Ruff replaces overlapping
formatter/import/docstring tools, not by literally running Black/isort/autoflake/
pydocstyle/pyupgrade. Production signatures, including private callables, must be
annotated and have proportional NumPy-style docstrings. Review must assess usefulness,
proportionality and private-callable coverage beyond Ruff's enforcement. No separate
typechecker or coverage percentage is required/configured.

`.venv` and uv's Python/package caches are development tooling, not FreshRSS data
or backups. Normal runtime stop leaves them alone. Do not confuse them with the
protected ownership directory. Optional `remove-package` removes only one confirmed,
unchanged, unused project addition; it is not a cache prune or blanket uninstall.
See [cleanup verification](#cleanup-verification-and-remaining-native-evidence).

## Managed pre-push hooks

After sync, activate intentionally:

```sh
uv run --locked rss-dev setup
```

With a remote, setup installs uv-managed pre-commit's **pre-push** hook using the
selected checkout's local interpreter and `.pre-commit-config.yaml`. Without a
remote, setup succeeds with `active: false`; add a remote and explicitly rerun
setup. It does not discover future remotes or activate itself later.

Custom `core.hooksPath`, foreign/legacy/symlinked hooks, symlinked/non-directory
hook directories or recognized nonexecutable hooks are preserved and refused with
exit 1/manual-resolution guidance. A recognized executable hook, including its
extensions, stays byte-for-byte unchanged. Resolve stale referenced environments
manually; do not overwrite another hook arrangement. `active` means recognized or
installed, **not proof a push ran**. Setup neither installs dependencies nor starts
FreshRSS.

**Linked worktrees share hooks.** Activate intentionally and push from a branch
containing the configuration. Historical hook tests used disposable repositories
and local bare remotes, not the shared project Git directory. The hook runs the
complete baseline via `uv run --locked --offline rss-dev check`, regardless of
changed filenames or remote provider. Missing environments/caches fail; it does
not silently download or rewrite source. Hooks apply to **all Git remotes**, may
be absent/bypassed and are not a security boundary.

## GitHub Actions

The Python workflow runs `uv sync --locked` and `uv run --locked rss-dev check` on
pull requests and pushes to `main`. It uses read-only contents permission,
immutable action references and uv 0.13.8. Local/CI gates have the same check-only
baseline; setup prerequisites can differ. Linux baseline CI is **not** native
Linux container integration. Historical positive/negative runs below are evidence
for their recorded revisions, not a current run or branch protection.

## Explicit live/platform checks

For the FreshRSS integration tests, only after starting the owned instance and
satisfying the operational prerequisites (the [native launchd control](#post-exit-process-lifetime-regression-and-native-control)
is separately selectable without FreshRSS):

```sh
uv run --locked pytest -m live
```

**The FreshRSS tests below interrupt the project session.** They require macOS, Podman, the
running owned project VM, the cached pinned image, internet access to the public
GitHub feed and the correct existing ownership directory. Missing/wrong
platform/services fail explicitly when selected, not skip as if fully covered.
Set `RSS_BACKUP_DIR` to a private directory outside Git first: VM-restart tests
exercise normal shutdown backups. From another worktree, explicitly use the
existing `RSS_STATE_DIR`; do not transfer/fabricate ownership records.

Use normal `umask 022` for public controlled-feed fixture files. Inherited `077`
can make those files unreadable by Apache; this is a real fixture limitation, not
a claim of umask-independent coverage. Archives remain explicitly `0600`.
Historical live invocations also use
`uv run --locked pytest -p no:cacheprovider -m live -q --tb=short` to disable the
pytest cache and shorten reports; `-s` optionally disables output capture. Their
flags and exits are in the [CLI reference](cli.md#pytest-selection).

The retained live tests cover:

- **Effective Apache security failure/recovery:** temporarily add an Apache INI
  override to the real owned app, verify failed security keeps it inaccessible
  without a diagnostic probe, remove the override and restore verified service.
  Retry must remove stale exact generated-name probes while preserving files
  outside that pattern. Let the recovery handler finish.
- **Synthetic-user HTTP state:** unique labelled disposable containers/volumes test
  installation, authentication, public and controlled articles, purge defaults,
  read/star state, replacement, VM stop/start and port migration. Cleanup removes
  only owned fixtures, not the real user's data.
- **Production Click replacement:** an unchanged production `replace` runs in a
  subprocess with reserved app/data/preflight/selection/provenance names and
  verifies subscriptions, article identity/content and read/star state through
  authenticated HTTP. Real VM/connection ownership and the command lock remain;
  cleanup is anchored to the retained test-labelled volume. It writes normal
  `security.json` in the selected ownership directory; a subsequent normal
  start/verify restores the real app's audit required by API checks.
- **Source-absent recovery:** back up running and stopped synthetic-user instances,
  remove the entire leased source container/volume, restore into a fresh volume,
  explicitly select it and verify login, subscription, content, both read/unread
  states and a star through HTTP. No source data remains during recovery. This is
  an equivalent isolated recovery drill, **not a second physical Mac demonstration**.
  It retains production Click behavior/ownership/locking while redirecting reserved
  resources, selection/provenance, backup-locator and Serve-record names. Fixture
  `test-<token>-last-backup.json` is removed on cleanup and the user's normal
  locator bytes/absence preserved. Backup attempt/publication/daily/intent records
  are also fixture-namespaced; the recovery runner verifies separate publication/
  resumption status and never claims daily protection from manual backup. All
  runners retain the real `command.lock` and shared `maintenance.json` guard, checked
  under the lock. An interrupted fixture can therefore block real writers until
  owner-coordinated reconciliation; fixture cleanup never deletes that hold.
  Separate VM-restart tests still update the real locator with a real instance
  backup in `RSS_BACKUP_DIR`.

- **Disposable application supervision recovery:** the selected test
  `tests/integration/test_live_replace.py::test_supervision_recovers_only_disposable_application_and_preserves_reading_state`
  first prepares the synthetic-user fixture through production `fixture.replace()`
  and logs in again before real Runtime start/health/recovery and HTTP state
  assertions. Production replacement creates an owner-labelled application without
  the test label; the runner validates its identity against the separately retained
  test-labelled-volume lease, rather than requiring a test label on that app.
  It redirects only reserved app/volume/preflight and
  audit/intent/registration names, retaining real machine ownership and command
  lock. A synthetic launchctl and scratch HOME job definition mean **no real host
  service activation**; a Podman wrapper preserves the actual caller's HOME/PATH
  for the existing owned VM. It checks healthy noninterruption, stopped-container
  recovery, preserved article/read/star state and unchanged production intent.
  This selected integration is not evidence of real launchd/reboot/login behavior
  and must be run only with owner coordination and the live prerequisites above.

Replacement/recovery fixture runners force loopback mode and reserved Serve-record
names so they cannot remove/reconfigure the real application's private forwarding.
They also namespace operator-intent/supervision records so public fixture starts
cannot change the real instance's running/stopped intent.
They use the existing real owner record, never a fabricated copy. Other tests can
restart the real project through normal lifecycle commands. Do not publish HTML,
authentication payloads, private URLs or `--showlocals` output as artifacts.

Only the fast controlled-feed regression lowers **fixture** TTL/cache minimums.
Its publisher sends `no-cache, max-age=0`, avoiding long-lived asset caching. Tests
verify installed production `*/20` cron and live daemon, then execute the real cron
command against changed controlled content; they do **not** wait for natural
production schedule intervals or alter the production schedule. The long observer,
its regression and `--observe-cron` option were removed. `RSS_LIVE`/
`RSS_OBSERVE_CRON` unittest invocations are unsupported; use `-m live`.
Production-cadence observation is a separate one-off acceptance/soak check, not a
routine test/publication requirement. Manual cron-command execution is not timing
evidence. See [historical evidence](#historical-evidence-and-remaining-limits).

### Time Machine staged-archive verification

The [owner-assisted recovery runbook](backup-restore.md#time-machine-copy-recovery-drill)
is the canonical procedure. Actual destination inclusion, encryption/access
assessment, completed-copy availability/retrieval and restored-application checks
are mandatory separate evidence for #24; default tests must not depend on Time
Machine availability, mount disks, alter exclusions or request privileged access.

Focused deterministic preparation:

```sh
uv run --locked pytest tests/test_backups.py tests/test_fixture_namespaces.py
uv run --locked rss-dev check
```

`test_recovery_runner_restores_independently_staged_archive_without_source` passes
an independently staged private archive to the existing production-command runner,
removes only the fixture's original archive/source engine state, and asserts import
without activation plus preservation of production selection, locator, intent,
backup-success and forwarding records. It also checks that staged bytes remain
unchanged. This models copy independence, **not a Time Machine retrieval**; the
substitute engine cannot prove a working database or authenticated application.
Existing command tests cover malformed archives, existing targets and incomplete
imports; the explicitly selected source-absent HTTP regression remains:

```sh
uv run --locked pytest tests/integration/test_live_backup.py::test_backup_recovers_http_state_after_source_volume_is_removed -m live -q
```

That live regression creates its own **manual** synthetic backups; it does not
consume an actual Time Machine copy or prove daily timing. It can interrupt the
working application and requires owner coordination, a verified pre-change backup
and all [live prerequisites](#explicit-liveplatform-checks). Do not run the complete
live suite merely to fill #24's Time Machine evidence gap.

For an actual already-retrieved archive, `tests.integration.backup_runner` already
accepts stdin JSON containing `token`, `operation` and (for restore) `archive`.
Use a fresh 16-lowercase-hex token, absent reserved resources/records, the original
`RSS_STATE_DIR`, a free fixture `RSS_PORT` and the running owned VM. Separate
`restore`, `select-volume` and `start` requests invoke unchanged production Click
behavior against namespaced resources. There is no new project CLI/configuration
interface. Never use arbitrary production resource names, copy ownership records,
redirect the command lock/maintenance guard, or bypass the recorded-image/security
gates. Loopback mode prevents real Serve mutations, **not outbound feed fetching**.
Before using real data, obtain consent and establish protection against unauthorized
publisher requests; otherwise activation and application acceptance remain blocked.

The runner is an operation adapter, not a standalone application verifier or an
automatic cleanup tool. Existing `InstanceFixture.login()` expects synthetic
`testreader` credentials from its in-memory fixture; an unrelated production archive
cannot simply be passed to the live test and called verified. Use owner-assisted
fresh login/state comparisons with expectations retained privately, not database
queries/file presence. Verify both read/unread and starred/unstarred examples and
representative settings/users. Retain exact target identity/provenance for any
owner-approved identity-checked cleanup; do not delete archives or shared holds.
`security.json` is shared, as in existing live fixtures; a clone audit is not proof
of the working application's effective runtime. Coordinate production API checks.
Record revision-specific outcomes/unrun checks in the issue, not as permanent
platform-pass claims here.

For real staged archives, adapt only the clone's archived base URL to the fixture
origin before activation, using the retained target lease, real lock and matching
image. Verify fresh unauthenticated login navigation in addition to HTTP readiness:
a 200 response can carry `Error during context user init!`. Diagnose a mismatching
default user read-only before proposing any repair; owner-approved configuration
correction is separate evidence, not an untouched-archive pass. Stop only the
identity-checked test clone after comparison so it does not block production's
idle-engine backup guards; retain data/copies and never bypass those guards.

## Callable Python interface

Click is a thin adapter; Python callers use the same guarded operations:

```python
from rss_aggregator import Runtime

result = Runtime().run("status")
```

`Runtime(root=..., state_dir=...)` accepts explicit `pathlib.Path` values. `root`
defaults to the editable source checkout, not the caller's working directory;
`state_dir` overrides current `RSS_STATE_DIR`, which otherwise defaults to
`<root>/.local`. Paths expand `~` and resolve relative values against the current
working directory. Defaults are resolved per instance, not an import-time singleton.
Construction itself does not inspect or change native resources.

`run(operation, *, archive=None, volume=None, username=None, api_password=None,
loopback=False, now=None, hour=3, minute=0, confirm=None, package=None,
unused_elsewhere=False)` returns the same JSON-compatible
operation dictionaries and sends diagnostics to stderr. It serializes callable as well as CLI callers under the
same nonblocking project lock. Unknown operations/ownership/lock failures raise
`RuntimeFailure`; OS/configuration errors propagate for the caller to handle.
There are twenty-three public operations (cleanup adds three), not a command for every helper. Optional
`now` is an aware `datetime` controlling backup eligibility/freshness tests only;
there is no CLI clock override. Omit it in production. `hour`/`minute` apply only
to explicit `backup-enable` and use UTC.

| Call | Required values and behavior |
| --- | --- |
| `run("setup")`, `run("start")`, `run("status")`, `run("fetch")`, `run("verify")`, `run("replace")`, `run("stop")`, `run("backup")` | Same guards, side effects, defaults and results as their [CLI commands](cli.md#runtime-commands-and-results). |
| `run("supervision-install")`, `run("supervision-remove")`, `run("supervision-status")`, `run("supervision-tick")` | Same opt-in normal-user-session registration, captured environment, intent, health, bounded retry and maintenance semantics as the [CLI commands](cli.md#mac-supervision-commands). All share the existing nonblocking lock; none needs Click context. Installation is explicit; status does not start writers. |
| `run("restore", archive=Path(...), volume="fresh-name")` | Requires archive path and fresh target name; missing values raise `ValueError`. Shares archive validation, ownership and no-activation safeguards. |
| `run("select-volume", volume="owned-name")` | Requires volume name; omission raises `ValueError`. Same stopped-app/running-VM and provenance checks. |
| `run("backup-enable", hour=3, minute=0)`, `run("backup-disable")`, `run("backup-due")`, `run("backup-status")`, `run("backup-reconcile")` | Same opt-in, captured-setting, daily retry, status and maintenance semantics as the [CLI](cli.md#daily-backup-status-results). Status returns `protected` for callable checks; CLI `--check` translates false into exit 1. Busy status alone may return an atomic read-only view without the operation lock. |
| `run("remove-vm", confirm=None)` | Default native inspection-only preview; `confirm` must be the current plan SHA-256 to apply. Same fresh full external backup, ownership/resource/connection guards, durable receipt/maintenance and narrowly scoped absent-resource completion as the [CLI](cli.md#cleanup-commands). |
| `run("delete-volume", volume="inactive-owned-name", confirm=None)` | Explicit unselected/unmounted/unattached labelled volume only; missing name raises `ValueError`. Preview by default, irreversible nonforce deletion with matching digest; no automatic backup. |
| `run("remove-package", package="plain-formula", confirm=None, unused_elsewhere=False)` | Missing package raises `ValueError`. Default preview; apply requires current digest plus `unused_elsewhere=True` for each installed eligible candidate. Fresh baseline/additions/version/dependent/Podman checks and normal native uninstall semantics match CLI; never a sweep. |
| `run("api-check", username="<FRESHRSS-USER>", api_password="<API-PASSWORD>")` | No prompts; supply credentials only in local process memory, never source/shell arguments. Same owned/running/security-checked app and active-forwarding guards; only sanitized counts/diagnostics/transport returned. Optional `loopback=True` selects the verified owned HTTP publication and returns `https_verified: false`; omitted/false retains trusted HTTPS. |

`Operation`, `Runtime` and `RuntimeFailure` are exported by `rss_aggregator`.
Developer callables `check(root=None)`, `setup(root=None)`, `format_code(root=None)`
and `fix(root=None)` in `rss_aggregator.development` require no Click context;
`root` is an optional `Path`, defaulting to the process working directory. They
return the developer result dictionaries rather than translating logical failures
into process exits; callers must inspect `ok`. They preserve the same selected-
checkout isolation, check-only/mutation and hook-refusal behavior as the CLI.

## Supervision test seams and remaining live evidence

The approved feature seam is existing CLI/`Runtime.run`, not supervisor private
helpers. `tests/test_supervision.py` extends controlled-executable fixtures with
scratch HOME/LaunchAgents/ownership state, actual subprocesses and disposable HTTP.
The default suite uses synthetic launchctl/Podman, no host jobs or production data.
It covers repeatable opt-in install/removal, partial/foreign registration,
captured configuration/dependency/session failures, persistent stop/start intent,
healthy no-op, guarded unexpected failure, bounded persisted retries with a
controlled clock, synthetic-secret redaction, lock contention and malformed
maintenance holds. A real SIGKILL of a **disposable** Runtime parent while its
native child waits on FIFOs verifies inherited-lock refusal; it never kills a
production process. Manual backup tests ensure stopped intent is never resumed.
The application-failure fixture substitutes only the socket port-bind boundary
because its disposable HTTP server stands in for Podman's listener; separate
command tests cover actual port-conflict refusal.

`tests/test_supervision_forwarding.py` starts a synthetic previously private
instance through the public Runtime boundary, returns it to loopback and retains
the inactive forwarding record. It compares shared/separate Tailscale executable
directories, removes the interactive PATH after registration, and checks healthy
observation plus guarded recovery. Only Podman stays in-process: Tailscale/launchctl
are disposable executables, and production LocalAPI discovery/client code reaches
a disposable loopback HTTP server while preserving unrelated routes. Missing
Tailscale before capture or afterward must refuse without changing app/ownership
state; a never-private instance still does not require Tailscale. These fixtures
do not demonstrate native daemon/launchd compatibility or trusted HTTPS.

The registration subprocess imports the CLI and standard library under its native
platform **before** selecting the mocked Mac operation policy. Selecting Darwin
before import makes Linux `urllib.request` request the unavailable macOS-only
`_scproxy` extension. Keep the subprocess import-guard regression: it models a
Linux bootstrap without that extension, then exercises owned registration/removal.
Do not add native-module stubs or relax production platform checks. A Mac pass of
this guard is not a substitute for running the actual Linux baseline.

Do not run live tests to fill evidence gaps without owner coordination and a
verified backup. The disposable integration above is separately selected, not
baseline coverage. Actual normal-user launchd registration/removal, reboot/login,
intentional-stop across reboot, sleep/power behavior and trusted HTTPS/device
availability require the [owner-assisted runbook](operations.md#owner-assisted-mac-acceptance-not-automated-evidence).
No pre-unlock service or unchanged real-host settings are inferred from fixture
passes. Use the combined verification guidance below whenever changing either
supervision or backup orchestration. Keep revision-specific command results and
unrun checks in the issue handoff, not as an implied platform pass here.

### Post-exit process-lifetime regression and native control

`tests/test_launchd_lifetime.py` executes the actual captured job command/environment
in an exclusively disposable process group, waits for its parent to exit, then
models launchd's documented default cleanup unless the definition abandons the
group. A finite HTTP helper checks application availability independently **after**
cleanup: the pre-fix policy can return `recovered` successfully while this assertion
fails. Subsequent healthy invocations must preserve availability/container identity
and release the command lease. A captured backup is SIGKILLed while a native export
survives; competing recovery/start/reconciliation must remain locked until it exits.
Other cases cover originally stopped VM/application restoration and exact legacy
registration removal, partial removal failure and deliberate settings-preserving
recapture with new daily policy evidence. Launchctl fixtures retain the definition
snapshot captured at bootstrap, independently of later disk changes. Missing-plist
cases verify that still-loaded legacy/fixed jobs refuse install without changing
ledger, loaded state or application; owned removal and reinstallation must restore
post-exit availability using the newly loaded snapshot. Confirmed-unloaded missing
jobs remain repairable. No real jobs, credentials or VM are used.

The separately selected native control requires only macOS, `launchctl`, and the
normal user's GUI session, **not** Podman/FreshRSS or a production ownership directory:

```sh
uv run --locked pytest tests/integration/test_launchd_process_group.py -m live -q
```

It installs uniquely labelled **nonrecurring** diagnostic jobs with finite harmless
loopback HTTP helpers. The default/control pair checks availability only after
native launchd reports the parent exited successfully. It unloads only the exact
diagnostic labels and lets finite helpers exit; never target a real VM/helper or
unrelated job. Missing platform/session prerequisites fail when selected. This is
an opt-in native policy check, not reboot acceptance or production activation.
The default model and even this native control do not prove actual Podman behavior.
Owner-coordinated backup/upgrade/reboot/login and trusted-client checks remain
[separate acceptance](operations.md#owner-assisted-mac-acceptance-not-automated-evidence).

## Combined supervision and backup verification

Use the existing CLI/`Runtime.run` boundary for deterministic integration regressions,
with scratch HOME/LaunchAgents, private temporary state and destinations, controlled
launchctl/Podman responses, synthetic data and disposable subprocesses. Do not install
either real job, use production ownership/data, create the owner's backup directory,
or change host settings. Keep these tests in the default suite and shared
`rss-dev check` gate; independent feature-suite passes are not combined proof.

Cross-feature regressions live in `tests/test_supervised_backups.py`, alongside
both feature suites. A focused iteration command is:

```sh
uv run --locked pytest tests/test_launchd_lifetime.py tests/test_supervised_backups.py \
  tests/test_vm_start_lock.py tests/test_runtime_cold_start.py \
  tests/test_fixture_namespaces.py tests/test_supervision.py \
  tests/test_supervision_forwarding.py tests/test_backup_schedule.py \
  tests/test_backups.py tests/test_backup_forwarding_state.py
# Always follow focused checks with the complete check-only baseline:
uv run --locked rss-dev check
```

Maintain cross-feature assertions at the public command/callable boundary:

- Only public start/stop write version 1 running/stopped intent. Absent intent
  stays unset for supervision. Stop persists stopped before maintenance refusal;
  blocked start cannot overwrite it. Successful maintenance admission precedes
  running intent even when a later startup gate fails.
- A normal stop retains the `BackupSchedule.shutdown` wrapper around recovery:
  attempt/publication/resumption evidence and maintenance handling must not be
  bypassed by lifecycle dispatch. Repeated already-stopped shutdown preserves
  prior effective capture evidence, including failures, without new publication
  or freshness credit. Backups never write intent, and manual/daily resumption
  never reopens an intentionally stopped app.
- Any maintenance presence, including malformed data or dangling links, blocks
  writer commands and supervisor recovery. One nonblocking `command.lock`
  serializes both features; contention is not queued or reported as backup success.
  Busy backup-status remains an atomic read-only inspection.
- `tests/test_vm_start_lock.py` models a VM launcher with a surviving daemon that
  retains every inherited descriptor. Cover both explicit start and Recovery's
  stopped-VM backup path: the daemon stays alive but a subsequent command can
  acquire the lock. Only `podman machine start` opts out of the private `_run`
  inheritance default; the parent must retain its lease through the launcher.
  Keep orphan-export interruption regressions too: exports/imports and other
  short-lived native work must continue to inherit and serialize after parent death.
- `tests/test_runtime_cold_start.py` exercises the public Runtime boundary with a
  controlled engine. Assert that only this call starting a stopped owned VM can
  recreate an unchanged matching stopped app, without force/volume deletion, on
  the same selected volume. Preserve warm reuse and partial-template-copy retries;
  retain refusal checks for foreign/mismatching/changed resources and failed
  preflight/security gates. Recreation must not become a general error fallback,
  and all post-create gates/cleanup must remain. Synthetic stale-PID behavior tests
  the chosen policy, not the precise real-world corruption mechanism.
- Exercise capture/publication/resumption failures and real disposable parent
  interruption while an export survives: the child retains the lock and the hold
  prevents supervisor recovery. Preserve source data and prior good archives;
  verified publication is independent of successful resumption.
- Reconciliation validates owner/schema and idle engines, refusing foreign work
  and surviving locked exports. It clears only the hold, never starts the app or
  changes intent. With retained running intent a **later** supervisor tick may
  recover; explicit stop before reconcile keeps subsequent ticks stopped.
- Disposable runner regressions in `tests/test_fixture_namespaces.py` verify
  namespaced intent/backup evidence without redirecting the real command lock or
  maintenance guard, including a hold arriving after initial owner inspection.
  Fixture interruption must not bypass shared safety or overwrite real records.
  Retain the deterministic supervision-runner regression accepting an owner-labelled
  production replacement without an app test label only under the validated
  test-labelled-volume lease; removing that lease or changing ownership/volume must
  still refuse. This is a guard regression, not a live recovery pass.
- Preserve both opt-in job identities/captured settings and their independent
  disable/remove behavior. Changed/missing/unsafe definitions and changed/missing
  loaded path/program/arguments/environment must block `backup-due` before daily
  reservation, evidence overwrite or engine mutation. Unexpected launchd inspection
  failures must refuse enable, disable and work while retaining registration;
  confirmed missing domain/service remains distinct. Use controlled executable
  exit faults, never native launchd fault injection. No registration, manual archive or manual `backup-due`
  invocation is proof of unattended daily work or healthy remote availability.

These checks exercise controlled command/process/filesystem boundaries, not real
launchd compatibility or live Podman descriptor behavior. Controlled daemon and
stale-PID regressions do not replace native verification of the VM-launcher
descriptor exception and guarded cold recreation. Record revision-specific native
results and limitations in the issue/PR handoff, not as a blanket platform pass.
Separately selected Mac integration requires owner coordination and a verified
pre-change backup;
actual registration/removal, reboot/login, intentional-stop persistence across
reboot, sleep/power behavior, natural unattended daily timing, trusted reader
availability, Time Machine retrieval and host-loss recovery remain separate live
acceptance limits. For an approved Mac cold-start check, verify subsequent managed
commands acquire the lock while VM helpers remain alive, and confirm the same
selected volume, authenticated subscriptions/articles/read/star state and all
startup gates. Do not kill helpers or remove the lock to obtain a pass. Do not
infer live acceptance from the default suite or simulate it against production
merely to fill evidence gaps.

## Cleanup verification and remaining native evidence

Issue #5 uses the existing public CLI/`Runtime.run` seam and default test workflow,
not private-helper acceptance or a separate test entrypoint:

```sh
uv run --locked pytest tests/test_cleanup.py tests/test_cleanup_context.py tests/test_guest_network.py tests/test_package_cleanup.py
uv run --locked rss-dev check
```

The selected-service/timestamp boundary also has a focused minimum-version check.
Use an actual Python 3.10 interpreter, a separate uv environment, and the locked
project dependencies; do not emulate its parser under a newer interpreter:

```sh
PYTHON310=/absolute/path/to/python3.10
MIN_ENV="$(mktemp -d)/venv"
UV_PROJECT_ENVIRONMENT="$MIN_ENV" uv sync --locked --python "$PYTHON310"
UV_PROJECT_ENVIRONMENT="$MIN_ENV" uv run --locked --python "$PYTHON310" pytest tests/test_cleanup_context.py tests/test_guest_network.py
```

This does not replace the full default check or establish a complete 3.10 test
matrix. The controlled executable emits Go-shaped RFC3339Nano (`Z` and nine-digit
fractions) and itself remains Python-3.10-compatible. Cover zero/one/six/nine-digit
fractions, numeric offsets, invalid/naive timestamps and exact nanosecond ordering;
whole-second plus integer-fraction comparison must retain persisted-network refusal.
This is the **host** parser compatibility contract. The standalone provenance helper
runs in the Linux **guest** and requires Python 3.11+ with `tomllib`, `systemctl`,
readable `/proc`, noninteractive sudo for rootful execution and native `podman unshare`
for rootless execution. It does not raise the host package's Python minimum or add a
runtime flag/setting. Missing guest prerequisites must refuse, never trigger repair
or a service restart. The audited native success profile is the stock Podman
6.2.0/Netavark guest, not arbitrary customized systemd/Podman service equivalence.

Deterministic regressions use private temporary state/destinations, synthetic
archives and controlled Podman/Homebrew executables; they do not remove a real VM,
uninstall host packages or consume production data. Maintain coverage for:

- Native inspection-only previews (except the existing lock), exact targets,
  retained resources and no preview destination creation; stale confirmations
  after resource/ownership/image/directory/connection changes must refuse.
- The actual native-drill setup path must accept its generated disposable machine
  name through a controlled Podman executable enforcing the native 30-character
  limit. Stop at the controlled VM-start boundary; this is setup compatibility,
  not a substitute for native recovery or authenticated-state evidence.
- Running owned source, exactly selected labelled volume, only labelled reserved
  containers/pinned images, no rootful containers/volumes/images including stopped,
  external build containers and intermediate images; derived-root/default-connection
  validation and removed captured jobs. Empty pods are independent resources too.
- Rootless/rootful standalone secret sentinels must survive refusal at preview,
  apply admission and post-export inspection. Model native VM deletion as destroying
  guest secret storage; any secret, failed metadata command or malformed inventory
  must retain the VM/data. Assert only guest-local `podman --remote=false secret ls
  --quiet` through named-machine SSH with an explicitly verified guest username;
  rootful uses `sudo -n --`. `info` must attest local mode and expected rootless/rootful
  privilege before listings: configuration can override `--remote=false`. Accept
  exactly empty stdout only at exit zero with no diagnostics; whitespace is not an
  empty list. No payload reads or names/payloads/native diagnostics in output.
  Empty inventories must permit normal removal. Model native `--format json` as
  literal template text (empty stdout or `json` per row), never a fabricated JSON
  array, and model the remote tunnel's discarded API error as exit-zero/empty.
  [Pinned native quiet/local contracts](https://github.com/containers/podman/blob/v6.2.0/cmd/podman/secrets/list.go)
  are source-verified; remote loopback experiments are not guest-local evidence.
  Name-only secret-storage inspection must also refuse stale/empty metadata DBs,
  driver remnants and unavailable inspection: native loading can swallow failed stat.
- Rootless/rootful artifact, custom-network, pod, external-container, mount-cache
  and secret-storage sentinels must survive preview/apply/post-export refusal,
  including failed/malformed metadata. Model deletion as destroying these stores.
  Inspect effective and stock `/var/tmp`/`/tmp` Buildah cache names, never contents;
  `images --all` covers intermediate image caches/manifest lists, not mount caches.
- Network success must require the complete audited stock default, not name/ID.
  Assert refusal for custom subnets, persisted stock-shaped replacements, extra or
  wrongly typed fields, invalid timestamps and warnings from skipped config files.
  Model selected rootless/rootful **API and local network inventories separately**:
  a custom API network must refuse even when local lists look stock, at preview,
  apply admission and post-export inspection, without deleting source/sentinels or
  prior archives. API failure, malformed shape/time and diagnostics must also refuse.
  Two independent *local* listings must synthesize increasing `created` timestamps;
  persisted configuration retains its timestamp. Accept a stable valid **API**
  timestamp: the service caches its synthetic default, so advancement is not an API
  requirement. Force guest `--log-level=warn` so configuration cannot hide local
  loader warnings; remote success cannot substitute for missing server warnings.
  Supported gate: guest Podman 6.2.0, Netavark and non-transient storage; refuse
  unknown versions/backends/mode/storage. Keep the fixture's dynamic local creation
  distinct from persisted replacement and cached API metadata.
  [Native synthesis/loading behavior](https://github.com/containers/podman/blob/v6.2.0/vendor/go.podman.io/common/libnetwork/netavark/network.go#L198-L337)
  explains the contract; fixture passes do not prove native guest provenance.
  Inventories cover current
  engine contexts/current and stock cache paths. Issue #5's approved scope requires
  an operator-established **project-only VM**: shared/repurposed guests, other
  users' engines, historical/alternate stores/cache locations and uncertainty mean
  retain the VM and recover/migrate the project, not automatic removal. Tests do not
  prove VM-wide exclusivity. Preserve every current resource/identity/backup refusal;
  a currently selected API store cannot be excluded as historical. Unexposed service
  configuration is still not proven by `info`; independent configuration mutation
  is not serialized by the project lock.
- Model selected API and guest-local metadata separately. At preview, apply and
  post-capture, refuse different graph/run/volume roots, driver/options, transient
  mode, effective cache directory, database/network/event backends or target
  privilege; also refuse missing/malformed/unavailable API identity. Rootless info
  must come from the actual selected connection; rootful API info uses the explicit
  guest socket via sudo, without relying on a registered/default root connection.
  Bind the compared identities to confirmation. Never normalize remote secret
  errors to absence. `info` comparison covers exposed identity fields, not all
  effective service configuration; a same-user match is not storage identity.
- Bracketing provenance must bind the actual selected listener to systemd MainPID,
  exact stock executable/argv, its kernel socket inode in process FDs, matching
  root/mount/user/network namespaces and a stable configuration fingerprint. Cover
  missing/unavailable helper proof, service/PID/start/listener swaps during the
  API-plus-two-local-list window and configuration changes. A changed config
  fingerprint must stale consent; an idle stock-service restart between complete
  inspections must not do so merely because its PID/start cookie changed. Refusal
  must leave VM/data intact at preview, apply and post-export, with no corrective
  mutation or restart. Use synthetic config/name/argv/env/diagnostic sentinels to
  assert only fingerprints leave the helper; no Podman secret payload reads.
  Helper controls also need native scrutiny: config main/drop-in candidates and
  absent-path/ancestor witnesses, startup/environment selectors, custom network
  directories, `engine.env` (including native case-insensitive TOML table/key aliases),
  symlinks, config ctime/mtime predating startup and trusted
  boot/clock agreement. The audited Fedora root-home alias is an allowed exception,
  not permission to resolve arbitrary symlinks. Substituted helper responses at the
  public command boundary alone cannot prove these Linux/systemd checks.
- Fresh export plus independent archive/checksum validation before VM deletion,
  backup/inspection/stop/rm failures, unavailable/absent/partially initialized
  sources, resources arriving during export, prior archive preservation and no
  force/reset/prune. A previous archive or `no_data` never permits removal.
- Disconnect the source immediately after export so both post-capture inspection
  and the fallback idle check fail. Assert nonzero exit/empty stdout, fixed
  owner-inspection and guarded `backup-reconcile` guidance, and no raw native
  diagnostic (use only synthetic URL/token sentinels). Source bytes, prior and
  newly verified archive bytes, the owned maintenance hold and running VM must
  survive without native stop/rm or a removal receipt. Unknown idleness must not
  clear the hold; an idle fallback may still retire it after an ordinary failure.
- Resume the unchanged owned writer after export copies its bytes and during the
  final connection inventory (the third post-export connection read: `_vm_plan`
  reads once directly and once through `_owner`). Assert the native-call trace
  places the injection after plan inspection and immediately before the final idle
  check, with the removal receipt retained. Model native VM shutdown as stopping even running
  containers, not as an application-writer safeguard. Assert post-capture changes
  are missing from the archive but retained in the source; refusal must precede
  native VM stop/removal, leave the resumed writer untouched and retain maintenance.
  The hold must block managed start/retry; after guarded reconciliation, a new
  preview/apply must capture the intervening changes in a fresh archive before removal.
- Private versioned removing/removed receipt, maintenance retention after
  interruption/partial removal, and absent-resource completion only for the exact
  owned `stopping_vm` hold/receipt with verified archive/foreign connections.
  Unknown/mismatched state must refuse, not delete records or broaden authority.
- Successful generation retirement during `setup`, preserving baseline/additions,
  locators/evidence/checkout and refusing reappearing collisions. Setup then restore
  to a fresh name before any empty app start; selection/compatible-image gates remain.
- Separate nonforce irreversible deletion only for explicit unselected labelled
  volumes with zero mounts/no stopped-container attachment; no automatic backup,
  other resources/archives retained and already-absent behavior. Verified absent VM,
  volume and package targets return successful absence JSON even with invalid/stale
  confirmation; absent packages do not require unused-elsewhere attestation.
- One plain formula per preview/apply; initial baseline and additions ledgers,
  unchanged installed versions, fresh recursive formula/cask optional/build/test
  dependents and no Podman machines/connections. Require both per-candidate external
  adoption attestation and current full-plan digest; recheck immediately before
  uninstall, retain native dependent checks and preserve ledgers/other packages on
  refusal. Model Homebrew's implicit uninstall-time global autoremove and verify that
  `HOMEBREW_NO_AUTOREMOVE=1` is forced for unset, empty, false and enabled inherited
  settings: both baseline and unconfirmed project-dependency sentinels must survive.
  Native dependent checks must still reject late adoption. Removing Podman first
  does not authorize a dependency sweep.

Native provenance verification is a separate, **read-only** check of the actual
`Cleanup._guest_baseline` and unmodified guest helper on both rootless and rootful
engines of a new disposable stock Podman 6.2.0/Netavark guest. Verify exact helper
success plus custom-network-directory, persisted-default and skipped-loader-warning
refusals with synthetic fixtures, preserving source/evidence. No secret payloads or
real credentials are needed. Record exact commands, revision and performed/unrun
positive and negative cases in the issue/PR, not as permanent branch-status claims
here. A successful native baseline proof is not VM removal, FreshRSS recovery or
atomic protection against independent native/root clients; it does not establish
all effective configuration inputs. The project-only VM policy remains mandatory.

**Not established by these deterministic checks:** actual disposable VM
remove/recreate/restore with authenticated original user, subscriptions, article
content and read/unread plus starred/unstarred state; real host Homebrew uninstall;
or native unrelated-sentinel preservation. Earlier source-absent recovery and
ordinary VM-restart evidence do not prove VM teardown/generation retirement. Default
fixture passes must not be described as these native acceptance results. Select the
native drill separately and retain its revision-specific outcomes in the issue/PR.

An owner-coordinated real check requires explicit approval, an independent verified backup,
exclusively disposable resources, a private expected-state record and a preservation
inventory. Follow [the operator runbook](operations.md#safe-cleanup), including job
removal, fresh removal-time capture, setup/restore-before-start and authenticated
comparison. Do not delete the only real instance or uninstall real host formulae just
to demonstrate a preview. Preserve sensitive archives/audits and record exact revision,
commands, sanitized results and all unrun checks in the issue/PR handoff. Do not infer
real sentinel preservation from substitute inventories. Manual residual/backup review
is not automatic pruning or secure-erasure evidence. No new settings are introduced.

Run only the explicitly selected native drill:

```sh
uv run --locked pytest tests/integration/test_live_cleanup.py::test_disposable_vm_remove_recreate_restore_preserves_authenticated_state -m live -q
```

It requires macOS/Apple Silicon virtualization, preinstalled Homebrew Podman, the
reviewed compatible image configuration, internet/public-feed access and an approved
idle-host window with **no running Podman machine**. Unlike ordinary FreshRSS live
tests, it creates a uniquely named fresh owned VM with private temporary ownership,
backup destination and synthetic user. The `rss-cleanup-<16-hex>` machine name is
28 characters, within Podman's 30-character limit. It never borrows the owner's existing VM or
fabricates its owner record. It uses loopback/namespaced job labels without real
job activation, removes/recreates its disposable VM, restores before empty startup,
and compares authenticated article/read/star state, retained machine names and
connection/default inventories. These comparisons do not inspect every unrelated
guest volume/image or prove preservation of all host state. Failed drills retain
namespaced resources/state for inspection, not
forced fallback cleanup. Keep any retained artifacts private. This is not a real
host-uninstall test, unrelated-sentinel proof, cross-host recovery or permission to
run against production; coordinate prerequisites and preservation before selection.

## Verification and adoption status

The repository implements uv/lockfile management, deterministic default pytest
coverage, callable operations with thin Click/JSON adapters, production annotations
and proportional NumPy docstrings, focused Ruff checks, guarded pre-push setup and
matching GitHub Actions. The reusable [Python standard](python-standards.md) is
policy; this guide describes repository adoption and evidence. **No policy
exceptions are granted beyond the standard's test/trivial-special-method
exemptions.** Review still checks requirements tools cannot fully enforce.

Production code is top-level `rss_aggregator/`; `scripts/rss` is only a shell
compatibility launcher. Hooks are activated only by explicit setup and shared by
linked worktrees. Host tooling does not authorize host PHP/database installation,
unrelated-resource changes or unexpected shared-package upgrades.

### Historical evidence and remaining limits

The following summarizes **previously recorded evidence, not new test results for
this documentation extraction or the current checkout**. For revision-specific
implementation/review context, see [PR #13](https://github.com/brucepucci/rss-aggregator/pull/13),
[PR #15](https://github.com/brucepucci/rss-aggregator/pull/15) and
[PR #18](https://github.com/brucepucci/rss-aggregator/pull/18). Do not interpret an
older pass as complete acceptance of a later revision.

| Capability | Historically verified scope / limit |
| --- | --- |
| Baseline and hook behavior | Default pytest plus Ruff lint/format passed in recorded candidates; disposable-repository pushes, hook refusal/preservation, selected-checkout imports and hostile Git/import/test environments were exercised. Simulated Homebrew incomplete-cask queries and refusal cases are not a real new-Mac installation or cask repair. |
| Mac runtime | Apple Silicon/macOS/Python 3.13.18/Podman 6.2.0 live checks exercised effective Apache security failure/recovery and stale-probe cleanup, authentication, public/controlled fetching, purge defaults, read/star persistence, production Click replacement, VM restart and port migration. Not alternate-platform verification. |
| Recovery | Six-test Mac runs included source-absent authenticated recovery, running/stopped backup, backup-on-shutdown and backup-locator preservation. Trusted archives were independently checked for ZIP integrity, whole-ZIP SHA-256 and mode `0600`. Synthetic image-change regressions do not execute a real database upgrade/downgrade. |
| Natural fetching cadence | Earlier one-off observations collected changed controlled articles on successive natural 20-minute ticks with production TTL/cache policy (2026-11-14 and 2026-11-15). This predates observer removal and is not evidence from today's fast cron-command regression. |
| Private access/ReadKit | Human-assisted Air/iPhone HTTPS authentication, instance-origin account URL, bidirectional read/unread and star/unstar state, normal stop/start persistence, and Air invalid-credential/redirect-origin checks were recorded. Approximate 30-second propagation was observed, not guaranteed. Canonical sanitized device evidence and gaps are in [private access](tailscale-readkit.md). |
| Tailscale failure safeguards | Isolated routing/ownership/conditional-write, persistence/connection-failure and post-start cleanup regressions plus a read-only real-daemon LocalAPI GET/ETag check were recorded. Mocks/read-only discovery are not live fault injection, a daemon collision test or proof of real infrastructure preservation. |

Historical GitHub Actions evidence (2026-11-14) includes both passing and deliberate
negative baseline runs:

| Check | Revision / run |
| --- | --- |
| Passing baseline | `4a61166`: [43641847554](https://github.com/brucepucci/rss-aggregator/actions/runs/43641847554) |
| Controlled pytest failure | `1977d38`: [43644086364](https://github.com/brucepucci/rss-aggregator/actions/runs/43644086364) |
| Controlled lint failure | `9d4f425`: [43644229824](https://github.com/brucepucci/rss-aggregator/actions/runs/43644229824) |
| Controlled format failure | `8104199`: [43644329319](https://github.com/brucepucci/rss-aggregator/actions/runs/43644329319) |
| Restored passing tree | `97b62ae` (same tree as `67cf72b`): [43644411740](https://github.com/brucepucci/rss-aggregator/actions/runs/43644411740) |

Negative experiments used disposable [PR #12](https://github.com/brucepucci/rss-aggregator/pull/12),
closed unmerged. Dependencies installed successfully; each run failed only its
intended check. They demonstrate gate failure semantics for those revisions, not
new CI results or branch protection.

Remaining limits:

- Daily scheduling tests use disposable launchd/engine substitutes; actual per-user
  launchd registration/output compatibility, a natural unattended mini run and
  missing-session/sleep behavior require owner-coordinated evidence with a verified
  pre-change backup. The selected full-state integration test is separate and does
  not prove daily timing or Time Machine inclusion/retrieval. No test may activate a
  production schedule, create the real owner destination or change host settings
  without that coordination.
- Supervision and backup orchestration share operator-intent version 1, any
  maintenance presence and one command lock. Follow the
  [combined deterministic verification guidance](#combined-supervision-and-backup-verification)
  for changes to either feature. Short-lived native work inherits the lease;
  only VM launchers omit inheritance so their daemonizing helpers cannot retain
  it indefinitely. Controlled daemon/orphan regressions are not evidence for
  real Podman descriptor behavior or cold-recreation recovery. These remain
  explicit owner-coordinated Mac checks, not guarantees from fixture passes.
  A stuck lock requires inspection, never unlinking the lock/hold.

- A complete Python 3.10 default-suite/multi-version matrix (the focused cleanup
  identity/native-timestamp entrypoint above does not establish it), other Mac
  providers/versions, alternate image
  architectures and native Linux rootless-Podman integration remain unverified.
  CI's Linux baseline is not Linux container coverage. No separate typechecker or
  visual browser automation is claimed.
- The mini's self-HTTPS diagnostic failure remains unresolved. Explicit loopback
  authentication does not verify HTTPS. Exact ReadKit/app OS versions, optional
  web-originated state edits, real certificate/policy-failure injection and a
  live injected cross-origin redirect were not recorded. See the private-access
  runbook rather than weakening TLS or speculatively changing Tailscale/policy.
- Physical second-host recovery and cross-architecture recovery are unverified.
  Verification archives in temporary private directories do not establish a
  durable user backup configuration; VM-native storage itself is not a backup.
- Private/paid-feed authorization acceptance remains unverified; Linux server
  deployment remains a later phase. Preview-first owned VM removal, separate
  inactive-volume deletion and per-formula removal are implemented with deterministic
  guards. Disposable removal/recreation/authenticated recovery requires the separately
  selected native drill and revision-specific evidence. Actual host uninstall and
  full unrelated-resource preservation are not established by that drill; see
  [cleanup verification](#cleanup-verification-and-remaining-native-evidence).
  Global prune/reset, automatic backup pruning and cache/directory sweeps remain unsupported.
- [Issue #7](https://github.com/brucepucci/rss-aggregator/issues/7) owns broader
  controlled publisher/error cases, interruption-safe fixtures, a coverage matrix,
  Markdown/local-reference automation and Linux/Mac integration responsibilities.
  These are planned, not checks supplied by the current baseline. Extend the
  existing entrypoint/workflow and this developer guide rather than recreating
  exhaustive test documentation in the README; keep the README's operator summary
  and links current. Feature issues retain their own acceptance tests; mocks and
  default-suite passes are not live-service evidence, a security guarantee or
  permission to merge.
