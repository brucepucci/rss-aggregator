# Local FreshRSS

A session-only FreshRSS instance on an Apple Silicon Mac: Homebrew's Podman CLI,
a dedicated **rootless** Linux VM, the official Debian image pinned by digest,
and SQLite in a named volume on the VM's native filesystem. No host PHP or
database server, Podman Desktop, privileged socket helper, login item, launch
agent, or automatic application startup is installed by this project.

This branch contains the unmerged implementation of [issue #1](https://github.com/brucepucci/rss-aggregator/issues/1).
Main at `d991e39` contains documentation and the canonical Python standard, not this
runtime. The runtime commands below are working branch-specific behavior; Python
tooling adoption is under review in draft [PR #11](https://github.com/brucepucci/rss-aggregator/pull/11),
targeting `feat/issue-1-local-freshrss`, not main. It is not yet merged back into
#1 or main. See [Python development and adoption](#python-development-and-adoption).

Backups, guarded removal, Tailscale, ReadKit and Linux deployment are **not yet
implemented**. See [architecture decisions](docs/adr/).

## Getting Started

### Prerequisites

- Apple Silicon macOS with virtualization available, Homebrew (`https://brew.sh`),
  Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`
  (verified with uv 0.13.8). `uv sync --locked` manages the pinned Python 3.13.18
  runtime and local `.venv`; it does not install global Python packages. Production
  compatibility is declared as Python 3.10+, with Ruff targeting 3.10. This host
  tooling is separate from FreshRSS's containerized PHP/database runtime.
- Internet access to Homebrew, the Podman VM image registry, Docker Hub, and GitHub.
- An unused Mac loopback TCP port (default `8080`) and approximately 2 GiB guest
  RAM plus VM overhead. The VM starts with 2 CPUs and 20 GiB virtual disk capacity;
  this is capacity, not a promise of immediate physical disk consumption.
- Do not run another Podman VM while starting this one. The command explains any
  conflict and **never stops the other VM** for you.

### Install and start

From a checkout of this repository:

```sh
uv sync --locked
uv run --locked rss setup
uv run --locked rss start
uv run --locked rss status
```

The existing executable `./scripts/rss <operation>` remains a compatibility
launcher for `uv run --locked --project <its-checkout> rss <operation>`, including
when invoked outside that checkout. It is now a shell launcher, not a Python
module to pass to `python3`. All operation results are indented JSON on stdout;
progress/underlying command chatter goes to stderr. The bootstrap `rss setup`
operation manages Podman; `rss-dev setup` separately activates developer hooks.

For an existing instance controlled by a different worktree, explicitly set
`RSS_STATE_DIR` to that checkout's existing private `.local` directory before
operating it. Worktrees do not namespace Podman resource names. Do not copy,
replace, or fabricate an ownership record to adopt existing resources.

`setup` inventories Homebrew formula/cask versions/dependencies and Podman
machines/connections, including containers, volumes and images reachable through
running machine connections. Stopped machines and non-machine connections are
recorded but not started/contacted to enumerate their contents. It previews the CLI
installation, records additions, and initializes the dedicated `rss-aggregator` VM. It does not start a stopped VM. Existing name collisions are
not adopted. If the preview includes an upgrade/reinstall of an existing package,
setup stops: obtain explicit approval and resolve the host dependency plan before
retrying. There is deliberately no bypass flag. Automatic Homebrew updates,
installation upgrades, dependent checks and install cleanup are disabled.

Initial verification on 2026-11-14 installed only **podman 6.2.0**, **libkrunfw
5.6.4**, **libepoxy 1.5.10**, **molten-vk 1.4.2**, **libkrun 1.19.8**, and
**krunkit 1.3.2**. Future Homebrew metadata can change this footprint; your local
inventory/preview and `.local/host-additions.json` are authoritative for your run.
Existing Python/SQLite Homebrew dependencies are not additions owned by this
project. No package is automatically uninstalled.

`start` explicitly targets the rootless `rss-aggregator` connection, verifies the
pinned image, and checks the **effective Apache PHP runtime**, first in an
unpublished disposable container and then in the actual application. The actual
application initially listens only on container loopback port 8088, **not** the
published container port 80. Only after the check passes and its temporary probe
is removed does Apache open port 80. The official entrypoint and default command
are retained. Mac publication is **`127.0.0.1:8080` only**, not LAN/IPv6/all interfaces.

Podman may print suggestions to install its privileged helper or Desktop. **Do not
follow those suggestions for this project**; explicit CLI connections need neither.

### First-run verification and local credentials

1. Wait for both security checks and the scheduler check to pass. Inspect the
   sanitized `.local/security.json` if desired; PHP CLI output alone is not the
   Apache security check.
2. Open **http://127.0.0.1:8080** in a local browser. Complete FreshRSS's installer:
   choose a language, confirm checks, select **SQLite**, and create **one FreshRSS
   user with Web form authentication and a strong password**. Enter credentials
   only in the local browser, never in Git, shell arguments, issue comments or chat.
   Do not select “None” authentication. Complete the final installation step.
3. Sign in. The prepared OPML template subscribes the user to exactly the public
   [FreshRSS releases feed](https://github.com/FreshRSS/FreshRSS/releases.atom),
   instead of upstream's default subscription list.
4. Run `./scripts/rss fetch`. Confirm articles appear. In **Configuration →
   Archiving**, verify both the age and maximum-article purge limits are unchecked.
   Do not add feed/category-specific purge policies.
5. Mark an article read and star it. Run `./scripts/rss replace`, sign in again,
   and confirm the subscription, articles and read/star state survived. Then test
   `./scripts/rss stop` followed by `./scripts/rss start` and check again.

The uninitialized application intentionally remains at the installer until **you**
enter your own credentials. Automated live tests use independent, disposable test
users/volumes; they do not create your real account. Replacement can expire PHP
browser sessions; your account/password and reading state persist.

### Normal operation

| Command | Behavior |
| --- | --- |
| `./scripts/rss setup` | Inventory/install missing CLI; initialize an absent owned machine; preserve an existing setup. |
| `./scripts/rss start` | Start the VM if needed; restart an existing application to re-run the security gate; check cron; immediately run feed actualization. |
| `./scripts/rss status` | Report VM/application state, inspect installed cron and its live daemon, show last security-check time. Does not start anything. |
| `./scripts/rss fetch` | Run feed actualization immediately, or explain that the installer is pending. |
| `./scripts/rss verify` | Restart/recheck the running application, including effective Apache PHP, cron and immediate actualization. Brief service interruption. |
| `./scripts/rss replace` | Stop/remove **only the owned application container**, then recreate it using the same named data volume and current checked-in configuration. |
| `./scripts/rss stop` | Stop application writers, verify the containers have exited, then stop only the project VM. Repeating it reports already stopped. |

Commands are serialized by a local lock. Repeated starts restart the application
but do not create another VM, user, subscription, or data volume. Errors return
nonzero; resources are retained for diagnosis rather than automatically rolled
back or deleted. A failed startup can leave the VM running; use `status` and
`stop`. A VM with unrelated **running** containers is not stopped automatically.

FreshRSS runs actualization on startup and via the official image's enabled cron
at **UTC minutes 00, 20 and 40** (`CRON_MIN=*/20`). This is wall-clock scheduling,
not 20 minutes after startup. Actualization respects upstream conditional HTTP,
cache, feed TTL and publisher backoff; it does not guarantee new articles on every
execution. The user default TTL is reduced from 3600 to **60 seconds**, below the
20-minute interval. The image's default cron setting is disabled; this project
explicitly enables it and checks both installed crontab and the running daemon.
Upstream's cache duration is 800 seconds by default. Its normal minimum is 60
seconds; publisher response headers can extend caching up to 86400 seconds, and
`no-cache` still respects the minimum. A low user TTL alone does not override that
HTTP cache policy. Client synchronization is not feed fetching.

Idle guest RAM is **not guaranteed to return to macOS** while the VM remains up.
Stopping the VM releases its memory allocations and stops all serving/fetching.
Nothing starts automatically after login or a Mac reboot; run `start` yourself.

### ReadKit connection — deferred

Do not enable API access or configure ReadKit for this slice. The instance is
browser-only and loopback-only. Private Tailscale HTTPS and the Google
Reader-compatible API with a separate API password are planned in issue #3;
ReadKit's macOS/iOS end-to-end connection is not yet verified.

### Backup and restore — not implemented

The complete FreshRSS `data` directory (SQLite databases, users, settings,
subscriptions, articles, reading state, feed cache and logs) is stored in
**`rss-aggregator-data`**, a project-labelled Podman named volume inside the VM.
It is **not** a Mac bind mount and **not** an external backup. The ordinary Podman
VM may expose its default Mac shared directories, but the database does not use
them. Container replacement and VM stop/start preserve the volume; VM deletion
or loss does not.

Per the approved [ADR 0002 deferral](docs/adr/0002-portable-state-with-consistent-external-backups.md),
normal shutdown does **not** require/create a backup until issue #2's backup
infrastructure exists. There is no supported backup/restore command yet. Do not
store irreplaceable articles here or delete the VM assuming it can be recovered.
OPML is only a subscription export, not an instance backup. Future backups will
contain secrets and require external, private storage.

### Safe cleanup

For now, safe cleanup is **`./scripts/rss stop`**. No VM/volume deletion, prune,
package uninstall, or destructive convenience command is provided. **Deleting the
VM destroys its native named volumes and all FreshRSS data.** Do not run global
Podman prune/reset or remove existing Homebrew dependencies. Do not delete
`.local/owner.json`: it is the safety record associating this checkout with the
machine/connection and container/volume ownership labels. Preserve `.local/` when
moving this checkout, and keep a single controlling checkout. The package-addition
ledger is evidence for later conservative cleanup, not permission to uninstall
packages that other projects may subsequently adopt. `.venv` and uv's managed
Python/package caches are development tooling, not FreshRSS data or backups;
normal stop leaves them alone. Never confuse the protected `.local` ownership
record with a disposable tool cache. No package-uninstall/cache-prune convenience
command is added here.

### Common troubleshooting

- **Another VM is running:** stop it manually only if you own it and it is safe.
  This project will not change it or global Podman defaults for you.
- **Machine/connection/volume/container ownership conflict:** stop and investigate;
  do not delete, relabel, or edit the ownership JSON to force adoption. A lost
  ownership record requires a deliberate recovery decision, not rerunning setup.
- **Port occupied:** use an unused `RSS_PORT` consistently (see Configuration).
  Do not terminate another listener automatically.
- **Homebrew upgrade approval required:** inspect `.local/install-preview.json`;
  no installation has been attempted by that invocation. Resolve the dependency
  plan explicitly before retrying. Partial installation additions are recorded.
- **Registry/VM/network unavailable:** commands return an error without stopping
  other machines. Restore connectivity/virtualization and retry. Startup failure
  does not imply the VM was stopped.
- **PHP/image/security mismatch:** do not enter credentials or bypass checks.
  Review current PHP advisories and the pinned image, then select and verify an
  updated official image. `localhost` and container isolation are not a production
  security guarantee.
- **No articles:** finish installation, sign in, run `fetch`, check the public
  subscription and local FreshRSS logs. Publisher outages/backoff and TTL/cache
  can prevent updates even when cron is healthy. Logs can contain sensitive URLs;
  inspect locally, do not paste them into shared evidence.
- **Cron missing/dead:** `status` fails rather than claiming fetching is healthy.
  Use `start`/`verify` to restart and recheck. Cron is upstream's background daemon,
  not a separately supervised service.
- **Wrong port/image/configuration on an existing container:** check the intended
  settings, then use `replace`. Never remove the named data volume to fix a
  container configuration problem.

## Configuration

No secret setting is required in the repository. Required user credentials are
entered locally into FreshRSS after security verification and stored as upstream
requires in the private VM data volume. Keep private feed URLs, credentials,
backups and generated settings out of Git. `.local/`, `.env*`, SQLite files and
`backups/` are ignored; this is not a substitute for inspecting staged files.
Local inventories/ownership/security records are written with private file modes;
permissions are not encryption. Treat VM storage and application logs as sensitive.

| Setting | Purpose and default | How to change/apply |
| --- | --- | --- |
| `RSS_PORT` (optional environment variable) | Mac IPv4 loopback port; `8080`, allowed range `1024–65535`. | Set the same value for operational commands, e.g. `export RSS_PORT=8081`; use `replace` for an existing container. For an installed instance, also update its base URL as shown below. |
| `RSS_STATE_DIR` (optional environment variable) | Ownership/inventory/lock/security record directory; `<checkout>/.local`. Not the FreshRSS database location. | Set an absolute private path **outside Git** before setup and use it consistently. Changing it does not adopt or migrate an existing machine. |
| `config/image.json` | Official Debian FreshRSS `1.30.4`, digest-pinned OCI index, explicit `linux/arm64`, verified platform manifest and source revision. | Change only after checking current advisories and verifying registry identity/runtime. `replace` applies a pin change. No floating-tag update command. |
| `config/config-user.custom.php` | New-user defaults: no age/maximum article purge; TTL `60` seconds. | `replace` copies the template; **existing users are not rewritten**. Change existing-user settings locally in FreshRSS, including feed/category overrides. |
| `config/public-feed.opml` | One public FreshRSS releases feed for new users. | `replace` copies the import template; existing subscriptions remain unchanged. Enter private feeds locally only in a later slice. |
| VM resources (fixed in command) | Machine `rss-aggregator`, rootless, initial 2 CPUs / 2048 MiB RAM / 20 GiB disk. | No project resize/delete command; do not change machine identity or connection records to work around ownership checks. |
| Container settings (fixed in command) | `CRON_MIN=*/20`, `TZ=UTC`, `ENABLE_ACCESS_LOG=0`, syslog copying off, `TRUSTED_PROXY=0`, `--restart=no`, internal security-gate listener. | Keep the official entrypoint/default command; apply reviewed changes with `replace`. No host cron/service registration. |

Development configuration is non-secret and required for the commands below:

| File | Purpose/default | Change and apply |
| --- | --- | --- |
| `pyproject.toml` | Python 3.10+ compatibility, Click runtime dependency, pytest/Ruff/pre-commit dev group, entrypoints, focused Ruff rules, default `not live` pytest selection. | Make reviewed metadata changes, explicitly regenerate `uv.lock` with `uv lock` when dependencies change, then `uv sync --locked` and the baseline check. |
| `.python-version` | uv-managed Python 3.13.18. | Update intentionally with compatible project/Ruff targets and verify a fresh sync; do not silently upgrade host packages. |
| `uv.lock` | Committed application dependency versions/hashes, including compatibility markers. | Regenerate only for intended dependency changes; normal setup/checks use `--locked`, not an implicit lock update. |
| `.pre-commit-config.yaml` | One complete check-only baseline at pre-push, local/system environment, all files regardless of changed filenames. | Keep it aligned with `rss-dev check`; explicitly run guarded `rss-dev setup` when activating hooks. |
| `.github/workflows/python.yml` | Same baseline on PRs/main updates, uv 0.13.8, immutable action references. | Review and verify changes; see the dated, revision-specific [CI checkpoint](#github-actions-and-7) and PR/evidence record for subsequent results. Controlled-failing CI evidence remains outstanding at that checkpoint. |

For example, to move an **already installed** instance to loopback port 8081:

```sh
export RSS_PORT=8081
podman --connection rss-aggregator exec --user www-data rss-aggregator-freshrss \
  php cli/reconfigure.php --base-url http://127.0.0.1:8081
./scripts/rss replace
```

Keep `RSS_PORT` set consistently for later starts. This PHP command runs **inside
the container**, not on macOS. For a new installation, choose the port before
`start` and the browser installer records the correct base URL automatically.

All other behavior uses the pinned upstream defaults unless changed locally:
form login is selected in the installer; anonymous reading/refresh, client API
and public registrations are disabled by default; bundled extension enablement is
empty; no full-page scraping is configured. Upstream display/read settings still
apply (opening an article or its website may mark it read; scroll/focus marking is
off by default). Per-feed/category policies can override user archiving settings,
so do not enable purge overrides for this experiment. User-configurable browser
settings persist in the named volume rather than becoming repository settings.
Production logs record warnings/errors by default, not notice-level fetch starts.
The official cron job redirects its latest execution output to
`/tmp/FreshRSS.log` **inside the container**; that transient output is overwritten
on the next run and is not a persistent audit log or backup. Treat all logs as
potentially sensitive.

### Image and PHP security policy

The pinned OCI index is
`sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df`;
Apple Silicon resolves to
`sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797`.
Runtime verification observed **Apache `apache2handler`, PHP 8.4.27**, with
`session.upload_progress.enabled` disabled in the effective web configuration.
The CLI version is separately recorded. Package/SBOM metadata is not accepted as
runtime evidence. The upstream entrypoint edits the Apache and CLI INIs before
starting workers; the project does not replace it or use an ineffective
application-level `ini_set()` mitigation.

PHP 8.4.27 meets the October 21 fixed-release minimum, but **does not fix the
additional October 23 upload-progress advisory**; disabled upload progress is
required separately. This gate accepts only PHP 8.4 at or above 8.4.27 for the
reviewed image. It is not an automatic vulnerability/advisory scanner. Reassess
security/support before changing or continuing to use an old pin. See PHP's
[2026 advisories](https://www.php.net/archive/2026.php),
[supported versions](https://www.php.net/supported-versions.php), and the
[official FreshRSS image documentation](https://github.com/FreshRSS/FreshRSS/blob/1.30.4/Docker/README.md).

The pinned index also contains other platforms, but only arm64 is verified here.
For another supported architecture, resolve its platform manifest from this index,
update the platform/manifest in `config/image.json`, and run full security and
integration verification on that host. The Mac lifecycle command is not a Linux
server deployment implementation. Never assume the arm64 manifest is portable.

## CLI reference

### Entrypoints and help

Run from a synchronized checkout (`uv sync --locked` first):

| Invocation | Purpose and accepted arguments/options |
| --- | --- |
| `uv run --locked rss [--help] COMMAND` | Runtime group; `COMMAND` is one of the seven operations below. |
| `uv run --locked python -m rss_aggregator [--help] COMMAND` | Identical runtime group through the Python module entrypoint. |
| `./scripts/rss [--help] COMMAND` | Compatibility shell launcher; forwards all arguments unchanged to `uv run --locked --project <launcher-checkout> rss`. Works from another directory; requires executable script and uv on `PATH`. |
| `uv run --locked rss-dev [--help] COMMAND` | Developer group; `COMMAND` is one of the four operations below. |

`--help` at either group or any subcommand prints conventional text usage to
stdout and exits 0 without operating on the instance or installing hooks. For
example, `uv run --locked rss --help`, `./scripts/rss start --help`, and
`uv run --locked rss-dev setup --help`. Click exposes `--help`, not `-h`.
Invoking a group without a command displays usage and exits 2. Unknown commands,
extra positional arguments and unsupported options also exit 2 with stderr
usage/diagnostics, not operation JSON. There is no implicit operation.

Runtime subcommands have **no positional arguments or options except `--help`**;
in particular, `rss` has no `--project` option. Runtime configuration comes from
the [settings above](#configuration). The launcher's `--project` is an internal
uv option selecting its checkout, not an option accepted by `rss`. Likewise,
`uv run` options such as `--locked` and `--offline` precede the executable name;
`--offline` allows cached-environment execution, not offline live-service access.
Failures in uv or the shell launcher can occur before Click starts and produce
stderr diagnostics/nonzero status without JSON.

### Runtime commands and results

Use `uv run --locked rss COMMAND` (or either runtime alias above) with these exact
subcommands; each also accepts `--help`. Lifecycle effects are detailed in
[Normal operation](#normal-operation).

Successful runtime operations emit exactly one indented JSON object; they do not
emit a result on failure. `--help` is conventional Click help, not operation JSON.
Runtime/configuration/external-command failures exit 1 with stderr diagnostics;
invalid command usage exits 2. Capture stdout separately when consuming results.

| Command | Purpose | JSON result fields |
| --- | --- | --- |
| `rss setup` | Inventory/install missing Podman CLI and initialize the owned VM without starting services. | `machine: "rss-aggregator"`, `created: boolean`. |
| `rss start` | Start/restart the owned application after security checks, verify scheduling and fetch feeds. | `machine: "running"`, `url`, `security`, `scheduler`, `fetch`. |
| `rss status` | Inspect state without starting anything. | `machine`, `application`; when available `http_ready`, `scheduler`, `security`. A stopped VM has application `unknown`, not a claim that its container disappeared. An unconfigured checkout reports machine/application `absent`. |
| `rss fetch` | Run feed actualization immediately. | `status: "installer_pending"` or `"completed"`. Completion means actualization ran, not that new publisher content was guaranteed. |
| `rss verify` | Restart/recheck the running application before reopening service; briefly interrupts service. | Start fields plus `verified: true`. |
| `rss replace` | Recreate only the owned application container, preserving its named data volume. | Start fields plus `replaced: true`. |
| `rss stop` | Stop application writers and the owned VM; no external backup. | `machine: "stopped"`, `already_stopped: boolean`, `backup: "deferred"`. |

`scheduler` contains `running`, `cron` and `timezone`. `security` is the sanitized
image/PHP audit record also stored in private `security.json`: checked time,
container, image identity/platform, FreshRSS version, advisory-review
date, effective web metadata, CLI PHP version and the official command. It is a
record of a check, not an advisory scan or a guarantee of future security. No
universal result envelope or extra text-output mode is required.

### Developer commands and results

Each command has syntax
`uv run --locked rss-dev COMMAND [--project DIRECTORY] [--help]`
and accepts no positional arguments. `--project DIRECTORY` is optional
**on each subcommand**, after `check`, `setup`, `format` or `fix`; it is not a
group-level option. It selects the directory to operate in, defaults to the
process's **current working directory**, and resolves relative paths from there.
It is not automatically the executable's source checkout. Files are rejected as
usage errors; select a synchronized project directory (its Git repository root
for `setup`). `--help` prints help only. There are no other project-defined flags.

| Command | Purpose, defaults and result |
| --- | --- |
| `rss-dev check [--project DIRECTORY]` | Run the complete non-live pytest suite, Ruff lint and Ruff format checks, without fixing source. Always run all three; JSON `checks` names are `pytest`, `lint`, `format`. Inherited `PYTEST_*` variables are stripped and pytest receives explicit `-m 'not live'`, so ambient selection cannot narrow the suite or enable live tests. |
| `rss-dev setup [--project DIRECTORY]` | Inspect Git arrangements and activate a pre-push hook only when safe. JSON contains `ok` and `active`, with `reason` and `next_step` for no-remote/manual-resolution cases. No remote: success but `active: false`; rerun after adding a remote. With a remote, conflicting or recognized nonexecutable hooks: failure, `active: false`, existing bytes/mode preserved, manual resolution required. A recognized executable hook is retained unchanged. See [Managed pre-push hooks](#managed-pre-push-hooks). |
| `rss-dev format [--project DIRECTORY]` | Explicitly rewrite Python formatting using Ruff; no lint fixes. JSON `checks` name: `format`. |
| `rss-dev fix [--project DIRECTORY]` | Explicitly apply safe Ruff lint fixes only; unsafe fixes disabled, no formatting. JSON `checks` name: `fix`. |

Examples: `uv run --locked rss-dev check` checks the current directory;
`uv run --locked rss-dev check --project /path/to/checkout` selects another one.
For `check`, `format` and `fix`, stdout is one indented JSON object shaped as
`{"ok": true, "checks": [{"name": "pytest", "returncode": 0}, ...]}` (entries
vary by command). `ok` is true only if every child succeeds. OS-level child
launch failures are recorded with return code 127. `setup` instead emits its hook-status
object, not a `checks` array. Logical failures still emit JSON with `ok: false`
and exit 1; success exits 0, even for inactive no-remote setup. Child output goes
to stderr. Invalid CLI usage exits 2 without result JSON. Formatting/fixes are
explicitly mutating, unlike the baseline; neither starts FreshRSS.

### Pytest selection and project option

`uv run --locked pytest [OPTIONS] [file_or_dir ...]` runs tests directly, with
human-readable pytest reports rather than the CLI JSON contract. With no paths,
configuration selects `tests/`; explicit paths/node IDs narrow a direct run, not
the required full baseline. Project-specific selection and documented controls:

| Argument/option | Purpose and default | Example |
| --- | --- | --- |
| `[file_or_dir ...]` | Optional test paths/node IDs; omit for the complete configured suite. | `uv run --locked pytest tests/test_commands.py` |
| `-m EXPRESSION` | Marker expression; project default is `not live`. Explicit `-m live` selects real-service tests and requires their prerequisites. | `uv run --locked pytest -m live` |
| `--observe-cron` | Project flag, takes no value, defaults to false. With live tests selected, enable the 20–40 minute observation of two real scheduled executions. Raises a usage error if no live-marked test remains selected; it does not itself select live tests. | `uv run --locked pytest -m live --observe-cron -s` |
| `-s` | Disable pytest output capture (normally enabled), useful for live observation diagnostics. | As above. |
| `-h`, `--help` | Conventional pytest text help and configuration, including `--observe-cron`; exits 0 without running tests. | `uv run --locked --offline pytest --help` |

Pytest's remaining upstream options are listed by that help command. Direct
pytest runs retain pytest's normal environment handling; use `rss-dev check` for
the guarded baseline. Pytest exits 0 on success, 1 for failed tests, 2 for
interruption/collection errors, 3 for internal errors, 4 for usage errors
(including invalid `--observe-cron` selection), or 5 when no tests are collected.
See [Explicit live/platform checks](#explicit-liveplatform-checks) before selecting
live tests; a default-suite pass is not live evidence.

### Python interface

Python callers use the same guarded behavior independently of Click:

```python
from rss_aggregator import Runtime

result = Runtime().run("status")
```

`Runtime(root=..., state_dir=...)` accepts explicit `pathlib.Path` values. Defaults
resolve from the editable source checkout and current `RSS_STATE_DIR`, not an
import-time singleton. `run` serializes callable callers as well as CLI callers;
errors raise `RuntimeFailure` or configuration/OS exceptions for the caller to
handle. There are seven lifecycle operations, not a public command for every helper.

## Development verification

### Environment and baseline

```sh
uv sync --locked
uv run --locked rss-dev check
```

The application lockfile `uv.lock` is committed. Click is a runtime dependency;
pytest, Ruff and pre-commit are development dependencies. `.python-version` pins
Python 3.13.18. Initial sync may download the managed interpreter, packages and
build backend; no Homebrew upgrade or global pip installation is involved. The
baseline itself uses installed tools and isolated fixtures, with no network,
credentials, production service or real Podman resources. Its disposable Git
fixtures use offline uv sync from caches populated by the initial sync. A missing
cache is an actionable failure, not a skipped check; rerun the initial sync rather
than enabling network access inside tests.

`rss-dev check` runs **all default pytest tests**, `ruff check`, and
`ruff format --check`, even if an earlier check fails. It strips inherited
`PYTEST_*` environment variables (including `PYTEST_ADDOPTS` and
`PYTEST_PLUGINS`) and explicitly passes `-m 'not live'`; ambient pytest options
cannot select a subset or opt into live tests in the baseline. Child output is
diagnostic stderr; stdout is `{ "ok": boolean, "checks": [{ "name": ..., "returncode": ... }] }`
formatted with indentation. Any failed check exits 1. These are check-only gates:
pytest cache/bytecode writes and Ruff cache writes are disabled within the gate;
tracked and working source files are not rewritten. A standalone default suite is
`uv run --locked pytest`; live tests are collected but deselected by default.

Explicit, separate mutating commands are:

```sh
uv run --locked rss-dev format
uv run --locked rss-dev fix
```

`format` uses only Ruff's formatter. `fix` applies only safe Ruff lint fixes,
explicitly disables unsafe fixes even if configuration enables them, and does not
format. Their JSON summaries have the same check-name/returncode shape. All
`rss-dev` commands accept `--project <checkout-root>` after the subcommand; the
callable counterparts are `check`, `setup`, `format_code`, and `fix` in
`rss_aggregator.development`. They do not require a Click context.

Ruff selects `E4,E7,E9,F,I,D,UP`: baseline errors, unused names, import sorting,
NumPy-compatible docstrings and modernization, targeting declared Python 3.10.
Ruff replaces overlapping formatter/import/docstring/modernization tools, not by
literally running Black/isort/etc. Tests are exempt from Ruff `D`; no production
exceptions are granted. All production callables, including private methods,
have annotated signatures and proportional NumPy-style docstrings. Review still
assesses usefulness/private-callable documentation beyond what Ruff enforces.
There is no mandatory separate typechecker or coverage-percentage threshold.

### Managed pre-push hooks

After sync, explicitly activate local enforcement:

```sh
uv run --locked rss-dev setup
```

With a Git remote, this installs a uv-managed pre-commit **pre-push** hook using
`.pre-commit-config.yaml`. With no remote, setup succeeds with `active: false`,
explains that hooks are not installed, and instructs you to rerun setup after
adding a remote. There is no automatic later discovery. With an existing foreign,
legacy, symlinked or custom `core.hooksPath` arrangement, setup exits 1, reports
`active: false` and preserves the arrangement. Resolve it manually before retrying;
do not overwrite or delete another developer's hooks. A recognized but
nonexecutable pre-push hook is also refused with `active: false` and manual
resolution guidance; setup preserves both its bytes and mode rather than
claiming it is active or automatically adding execute permission. Rerunning setup
leaves a recognized executable pre-commit hook byte-for-byte unchanged, including
developer extensions. If its referenced environment is stale, explicitly resolve
that existing arrangement before reinstalling; setup does not rewrite it automatically. `active` reports
hook recognition/installation, not proof that a push has run. Setup does not
install dependencies or start FreshRSS.

Linked worktrees share Git hooks. This adoption was verified using **disposable
repositories/local bare remotes**, not by activating hooks in the shared real
repository while #1 still lacks #9's configuration. Activate intentionally when
ready and push from a branch containing the hook configuration. The hook runs the
same full baseline through `uv run --locked --offline rss-dev check`, regardless
of changed filenames or remote name/provider. It applies to **all Git remotes**,
can be absent or bypassed, and is not a security boundary. A missing uv environment
or offline cache fails; it never silently downloads or rewrites source during push.

### GitHub Actions and #7

`.github/workflows/python.yml` runs the same `uv sync --locked` and
`uv run --locked rss-dev check` baseline on pull requests (including nested PRs)
and main-branch pushes, with read-only contents permission, immutable action
references, and uv 0.13.8. Draft [PR #11](https://github.com/brucepucci/rss-aggregator/pull/11)
targets `feat/issue-1-local-freshrss`, not main.

**Checkpoint: 2026-11-14.** Review-fix revision
`4a6116621efd2c91e3dc11575fcb63028779f74e` was published to PR #11, and
[Python baseline run 43641847554](https://github.com/brucepucci/rss-aggregator/actions/runs/43641847554)
completed successfully on that exact revision. This is revision-specific baseline
evidence, not a claim about subsequent commits or live integration. Consult
[PR #11](https://github.com/brucepucci/rss-aggregator/pull/11) and the
[verification record](docs/verification/issue-9.md) for subsequent results; a new
CI run alone does not require rewriting this historical checkpoint.

At this checkpoint, **controlled-failing GitHub pytest/lint/format evidence
remained outstanding**, and the authorized draft exception did not establish
complete acceptance. Local push rejection is not evidence of CI execution.
Integration/merge still requires separate authorization, and no branch protection
changes are made.

[#7](https://github.com/brucepucci/rss-aggregator/issues/7) extends this entrypoint
and workflow with native Linux rootless-Podman coverage, controlled publication
fixtures/failure cases, a coverage matrix and Mac provider/manual-device limits.
It must not create a competing baseline or treat mocked checks as proof of real
PHP security, fetching, persistence, backups, tailnet access or ReadKit behavior.

### Explicit live/platform checks

After starting the owned instance, select the retained real Mac/HTTP checks:

```sh
uv run --locked pytest -m live
uv run --locked pytest -m live --observe-cron -s
```

Missing Podman, the wrong platform or an unavailable project is an explicit
failure when selected, not a skip reported as full coverage. These remain Mac
VM-specific tests; they do not implement #7's native Linux runner. They require
the cached pinned image, internet access to the public GitHub feed, and the
correct existing ownership directory. They interrupt the project session. Use
`RSS_STATE_DIR` explicitly when operating from another worktree; no automatic
ownership-state transfer occurs. The old `RSS_LIVE`/`RSS_OBSERVE_CRON` unittest
commands have been replaced by pytest marker/option selection.

Unique labelled disposable test containers/volumes exercise HTTP installation,
authentication, public articles, read/star state, replacement and VM stop/start.
Cleanup removes only those fixtures. The security test temporarily adds an
Apache INI override to the real owned application, proves failed verification
keeps it inaccessible with no diagnostic left behind, then removes the override
and restores verified service. Let its recovery handler finish. Do not publish
HTML, authentication payloads, private URLs or `--showlocals` output as artifacts.

Only the fast controlled-feed regression reduces fixture TTL/cache minimums. Its
publisher advertises `no-cache, max-age=0`, avoiding the image's long-lived asset
caching. The optional **20–40 minute** observation preserves production settings
and waits for two genuine scheduler executions, never counting manual refreshes.
The historical #1 observation is not claimed as a new #9 run.

See the [rewrite inventory](docs/implementation/issue-9.md),
[#9 verification evidence](docs/verification/issue-9.md), and
[historical #1 evidence](docs/verification/issue-1.md) for actual results and limits.
Those records identify the revisions and checkpoints actually verified; results
for one revision do not establish verification of subsequent commits.

## Python development and adoption

The canonical, copy/paste-friendly policy is [docs/python-standards.md](docs/python-standards.md); [AGENTS.md](AGENTS.md) points agents to it. Repository-specific setup and exceptions belong here, not in the reusable policy.

### Approved rollout

1. **Done:** the documentation-only [#8](https://github.com/brucepucci/rss-aggregator/issues/8) change reached main through PR #10 at `d991e39`, without waiting for #1's closure or merge.
2. **Done on this branch:** incorporate those standards into #1's existing implementation branch, preserving its runtime and history.
3. [#9 — adoption/rewrite](https://github.com/brucepucci/rss-aggregator/issues/9) is implemented from that updated #1 branch. At the **2026-11-14 checkpoint**, review-fix revision `4a61166` was published in draft [PR #11](https://github.com/brucepucci/rss-aggregator/pull/11), targeting `feat/issue-1-local-freshrss`, not main, and passed the baseline workflow. See the [CI checkpoint](#github-actions-and-7) for exact evidence and limitations; further publication and integration/merge require separate authorization.
4. After authorized integration, verify the combined #1/#9 branch against both issues. The final main-targeting PR must resolve **both #1 and #9** and squash-merge them as **one implementation commit**, separately from #8's documentation commit.

This is the approved integration plan, not authorization to publish or merge automatically.

### Enforcement status and exceptions

The #9 branch now implements uv-managed tooling, pytest, independent callable
operations with thin Click adapters, JSON results, typed/documented production
code, Ruff and managed baseline gates. No Python policy exceptions are granted;
tests and trivial special methods retain the canonical exemptions. Branch
adoption does not establish integration into #1/main or hook activation in a
particular checkout. At the **2026-11-14 checkpoint**, published revision `4a61166`
passed the GitHub baseline; controlled-failing CI evidence and integration were
still outstanding. That checkpoint does not establish results for subsequent
commits. See the [CI checkpoint](#github-actions-and-7) and linked PR/evidence
record for revision-specific results and remaining acceptance work.

Host-side Python development tooling does not authorize installing FreshRSS's PHP/database runtime on the host, unexpected host-package upgrades, or changes to unrelated resources. All accepted ADRs remain in force; this change neither changes them nor certifies unmerged runtime behavior.

### Verification responsibilities

- **#8 (merged specification):** delivered the canonical policy and discovery/documentation guidance only; it installed no tools and changed no production Python, tests, hooks or CI workflows.
- **#9 (baseline adoption):** supply uv-managed tooling, pytest, callable APIs with useful Click boundaries, production typing/docstrings, Ruff, pre-push gates, and matching GitHub Actions checks. The complete default suite must be deterministic and isolated, with no credentials, network, or production services. Preserve meaningful command-boundary success/failure coverage and prove checks reject controlled failures without rewriting files. Actual passing/failing local-hook and CI evidence belongs to adoption, not this specification.
- **[#7 (shared integration infrastructure)](https://github.com/brucepucci/rss-aggregator/issues/7):** owns broader real rootless-Podman/runtime/HTTP coverage and platform responsibilities, with separately selected integration suites, safe fixtures, and explicit Mac VM-provider/manual-device limits. Feature issues retain their own acceptance tests. Coordinate shared entrypoints/workflows with #9 rather than creating competing systems or making either a new blocker for the other.

A baseline pass is not proof of effective PHP security, real feed fetching, durable reading state, actual backup recovery, tailnet access, paid-feed authorization, or physical ReadKit client behavior. Record actual checks, unavailable prerequisites, unperformed checks, and platform limitations rather than claiming full coverage.

## Architecture

- [Isolate local development with Podman](docs/adr/0001-isolate-local-development-with-podman.md)
- [Portable state and consistent external backups](docs/adr/0002-portable-state-with-consistent-external-backups.md)
- [Private Tailscale access and shared reading state](docs/adr/0003-private-tailscale-access-and-shared-reading-state.md)

These decisions describe accepted architecture. The local runtime above implements
only #1's initial slice; subsequent features remain explicitly deferred.
