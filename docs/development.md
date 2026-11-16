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
protected ownership directory. No package-uninstall/cache-prune convenience
command is provided.

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

Only after starting the owned instance and satisfying the operational prerequisites:

```sh
uv run --locked pytest -m live
```

**These tests interrupt the project session.** They require macOS, Podman, the
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

The six retained live tests cover:

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
  locator bytes/absence preserved. Backup attempt/publication/daily/maintenance/
  intent records are also fixture-namespaced; the recovery runner verifies separate
  publication/resumption status and never claims daily protection from manual backup. Separate VM-restart tests still update the real
  locator with a real instance backup in `RSS_BACKUP_DIR`.

Replacement/recovery fixture runners force loopback mode and reserved Serve-record
names so they cannot remove/reconfigure the real application's private forwarding.
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
loopback=False, now=None, hour=3, minute=0)` returns the same JSON-compatible operation dictionaries and sends
diagnostics to stderr. It serializes callable as well as CLI callers under the
same nonblocking project lock. Unknown operations/ownership/lock failures raise
`RuntimeFailure`; OS/configuration errors propagate for the caller to handle.
There are sixteen public operations, not a command for every helper. Optional
`now` is an aware `datetime` controlling backup eligibility/freshness tests only;
there is no CLI clock override. Omit it in production. `hour`/`minute` apply only
to explicit `backup-enable` and use UTC.

| Call | Required values and behavior |
| --- | --- |
| `run("setup")`, `run("start")`, `run("status")`, `run("fetch")`, `run("verify")`, `run("replace")`, `run("stop")`, `run("backup")` | Same guards, side effects, defaults and results as their [CLI commands](cli.md#runtime-commands-and-results). |
| `run("restore", archive=Path(...), volume="fresh-name")` | Requires archive path and fresh target name; missing values raise `ValueError`. Shares archive validation, ownership and no-activation safeguards. |
| `run("select-volume", volume="owned-name")` | Requires volume name; omission raises `ValueError`. Same stopped-app/running-VM and provenance checks. |
| `run("backup-enable", hour=3, minute=0)`, `run("backup-disable")`, `run("backup-due")`, `run("backup-status")`, `run("backup-reconcile")` | Same opt-in, captured-setting, daily retry, status and maintenance semantics as the [CLI](cli.md#daily-backup-status-results). Status returns `protected` for callable checks; CLI `--check` translates false into exit 1. Busy status alone may return an atomic read-only view without the operation lock. |
| `run("api-check", username="<FRESHRSS-USER>", api_password="<API-PASSWORD>")` | No prompts; supply credentials only in local process memory, never source/shell arguments. Same owned/running/security-checked app and active-forwarding guards; only sanitized counts/diagnostics/transport returned. Optional `loopback=True` selects the verified owned HTTP publication and returns `https_verified: false`; omitted/false retains trusted HTTPS. |

`Operation`, `Runtime` and `RuntimeFailure` are exported by `rss_aggregator`.
Developer callables `check(root=None)`, `setup(root=None)`, `format_code(root=None)`
and `fix(root=None)` in `rss_aggregator.development` require no Click context;
`root` is an optional `Path`, defaulting to the process working directory. They
return the developer result dictionaries rather than translating logical failures
into process exits; callers must inspect `ok`. They preserve the same selected-
checkout isolation, check-only/mutation and hook-refusal behavior as the CLI.

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
- #22 supervision shares operator-intent version 1, maintenance presence (including
  dangling links) and the same inherited command lock. Combined-revision tests must
  verify supervisor/backup interaction after integration; independent branch passes
  are not combined proof. Native daemonizing Podman helpers must close unrelated
  inherited descriptors; the controlled orphan test is not evidence for their real
  descriptor behavior. A stuck inherited lock requires inspection, never unlinking
  the lock/hold. Live integration of that behavior remains an explicit Mac check.

- Python 3.10 (declared minimum), other Mac providers/versions, alternate image
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
- Private/paid-feed authorization acceptance and destructive cleanup are not
  implemented/verified project capabilities; Linux server deployment remains a
  later phase. No VM/volume deletion, global prune/reset or package-uninstall
  convenience command exists.
- [Issue #7](https://github.com/brucepucci/rss-aggregator/issues/7) owns broader
  controlled publisher/error cases, interruption-safe fixtures, a coverage matrix,
  Markdown/local-reference automation and Linux/Mac integration responsibilities.
  These are planned, not checks supplied by the current baseline. Extend the
  existing entrypoint/workflow and this developer guide rather than recreating
  exhaustive test documentation in the README; keep the README's operator summary
  and links current. Feature issues retain their own acceptance tests; mocks and
  default-suite passes are not live-service evidence, a security guarantee or
  permission to merge.
