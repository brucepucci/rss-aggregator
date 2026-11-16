# CLI reference

Complete project command syntax, defaults, outputs and failure contracts. Start
with the [operator guide](../README.md); see [operations](operations.md) for
lifecycle/troubleshooting, [configuration](configuration.md) for settings,
[backup and recovery](backup-restore.md), and [private access](tailscale-readkit.md).
Developer setup, verification scope and the callable Python interface are in
[development](development.md).

## Entrypoints and help

Run from a synchronized checkout (`uv sync --locked` first):

| Invocation | Purpose and accepted arguments/options |
| --- | --- |
| `uv run --locked rss [--help] COMMAND` | Runtime group; `COMMAND` is one of the fifteen operations below. |
| `uv run --locked python -m rss_aggregator [--help] COMMAND` | Identical runtime group through the Python module entrypoint; Python's `-m` selects the module. |
| `./scripts/rss [--help] COMMAND` | Compatibility shell launcher; forwards all arguments unchanged to `uv run --locked --project <launcher-checkout> rss`. Works from another directory; requires executable script and uv on `PATH`. It is not a Python module to pass to `python3`. |
| `uv run --locked rss-dev [--help] COMMAND` | Developer group; `COMMAND` is one of the four operations below. |

`--help` at either group or any subcommand prints conventional text usage to
stdout and exits 0 without operating on the instance or installing hooks:

```sh
uv run --locked rss --help
./scripts/rss start --help
uv run --locked rss-dev setup --help
```

Click exposes `--help`, not `-h`. Invoking a group without a command displays usage
and exits 2. Unknown commands, extra positional arguments and unsupported options
also exit 2 with stderr usage/diagnostics, not operation JSON. There is no implicit
operation. uv or launcher failures can occur before Click starts and produce
stderr diagnostics/nonzero status without JSON.

`rss` has no `--project` option. The launcher's `--project` is a **uv** option
selecting its checkout, not a runtime option. In contrast, `rss-dev` subcommands
have their own `--project`, described below. uv options such as `--locked` and
`--offline` precede the executable name; `--offline` controls uv's network access,
not the invoked application's access to live services.

## Runtime commands and results

Use `uv run --locked rss COMMAND` or either runtime alias above. Every subcommand
accepts `--help`. Except for `api-check`, `restore` and `select-volume`, commands
have **no positional arguments or options other than `--help`**.

Successful operations emit exactly one indented JSON object on stdout and exit 0.
Progress/underlying command chatter goes to stderr. Runtime/configuration/external
command failures exit 1 with stderr diagnostics and **no result JSON**; invalid
usage exits 2. Capture stdout separately when consuming results. Results are
operation-specific, not a universal envelope; there is no extra text-output mode.

| Command | Purpose and prerequisites | JSON result fields |
| --- | --- | --- |
| `rss setup` | Inventory/install the missing Podman CLI and initialize the owned rootless VM. Does not start or stop services. Existing unowned resource names and installation plans upgrading/reinstalling existing packages are refused; there is no bypass. | `machine: "rss-aggregator"`, `created: boolean`. |
| `rss start` | Persist running intent (even if startup fails), reset recovery attempts, then start the VM if necessary, restart/reuse or create the application, copy both new-user templates while stopped, pass security/readiness/scheduler/fetching gates, and optionally configure private HTTPS/API. Repeated starts interrupt the application, not create duplicate data/users/subscriptions. | `machine: "running"`, `url` (HTTPS when opted in, otherwise loopback), `security`, `scheduler`, `fetch`; private starts also include `forwarding: {status: "running", url}`. |
| `rss status` | Inspect without starting services. Checks scheduler and HTTP readiness when the app runs; unhealthy cron/readiness, ownership or forwarding checks fail rather than return a healthy result. | `machine`, `application`, selected `volume`, `forwarding`; when available `http_ready`, `scheduler`, `security`. An unconfigured checkout returns only machine/application `"absent"`. With a stopped VM, application is `"unknown"`, not a claim its container disappeared. |
| `rss api-check [--loopback]` | Prompt locally; check GReader diagnostics and authenticated read access without changing reading state. Requires an owned running app, a matching application security audit and active owned forwarding in **both** modes. | `diagnostics: "PASS"`, `authenticated: true`, integer `subscriptions`, integer `sample_articles` (at most one requested article, not a total), `transport: "https"` / `"loopback"`, `https_verified: true` / `false`. |
| `rss fetch` | Run feed actualization immediately; requires the owned VM/application to be running. | `status: "installer_pending"` or `"completed"`. Completion means actualization ran, not that new publisher content was guaranteed; upstream cache/TTL/backoff still apply. |
| `rss verify` | Requires a running owned VM; runs the normal start/restart path, including effective Apache PHP, cron and immediate fetching checks. Can start a stopped application; briefly interrupts a running one. | Start fields plus `verified: true`. |
| `rss replace` | Requires a running owned VM and selected owned data volume. Stop/remove only the owned application container, then recreate it with current configuration, preserving named volumes. First-recovery-activation pin and opt-in checks precede removal. | Start fields plus `replaced: true`. |
| `rss backup` | Quiesce writers, create/verify an external archive in `RSS_BACKUP_DIR`, then attempt to restore prior application/forwarding/VM state, even on snapshot failure. Can temporarily start an owned stopped VM without starting the application. | `archive`, `sha256`, `verified: true`, `manifest`. |
| `rss restore ARCHIVE --volume NAME` | Validate locally, then import into a fresh owned volume. May start only the owned VM; leaves it running. Does not activate the imported data, overwrite an existing volume, or require `RSS_BACKUP_DIR`. | `restored: true`, `volume`, `selected: false`, `manifest`, `next_step`. |
| `rss select-volume NAME` | Select an owned volume while the VM runs and the application is stopped/absent. Refuses incomplete imports or recorded-image mismatch. Does not start the app or delete the original volume. | `volume`, `selected: true`, `next_step`. |
| `rss stop` | Persist stopped intent before shutdown (even if already stopped or shutdown fails). Check both engines for foreign workloads, stop writers/owned forwarding, create and verify an external backup when data exists, then stop only the owned VM. Repeating an already-stopped shutdown does not restart it to back up; it still removes stale owned forwarding. | `machine: "stopped"`, `already_stopped: boolean`, `backup` (archive result or `{status: "already_stopped"}` / `{status: "no_data"}`). |

### Mac supervision commands

All four commands accept only `--help`; there are no positional arguments or
additional flags. Install/remove operate on the current normal Mac user's session,
never system launchd. See the [backup-before-install and owner acceptance
procedure](operations.md#opt-in-mac-startup-and-recovery) before real activation.

| Command | Purpose/defaults and failure behavior | JSON result |
| --- | --- | --- |
| `rss supervision-install` | Explicitly capture the selected stable checkout, its `.venv/bin/python`, existing ownership and non-secret configuration; write/activate the owned user LaunchAgent. Requires macOS, non-root user, normal GUI session, owned Podman VM and available captured dependencies. Does not create running intent. Repeated identical installation is safe; changed configuration requires explicit removal/reinstallation. Foreign/changed/symlinked jobs and loaded-label conflicts are preserved/refused. Partial activation retains registration for retry/removal. | Same supervision-status shape below; `enabled: true` means a registration ledger exists, not that the app is healthy or session available. |
| `rss supervision-remove` | Unload/remove only the unchanged owned job; repeatable if absent. Preserve app, VM, data, backups and intent. Failures preserve registration for inspection/retry; changed/unowned jobs are never overwritten/deleted. | Supervision-status shape with `enabled: false`, `state: "disabled"`. |
| `rss supervision-status` | Read registration, captured dependency availability, normal user session, intent and observed service readiness without starting/restarting it. An unavailable/unhealthy state is a successful **query**, not recovery success. Malformed/foreign ownership/state and lock failures still exit 1. | `enabled`, `intent` (`"unset"`, `"running"`, `"stopped"`), `maintenance`, `session` (`"not_checked"`, `"available"`, `"unavailable"`), `observed`, `state`, `action`, `recovery`; registered results add absolute `job`, `root`, `state_dir`. |
| `rss supervision-tick` | One serialized reconciliation. Disabled/stopped/unset intent is a no-op. Healthy observations do not restart service. Otherwise at most three consecutive normal guarded starts, with minimum 60s and 300s backoff. Maintenance, unavailable/inactive registration, backoff, failed/exhausted recovery and lock contention exit 1 with no result JSON; inspect status for response. No shell/.env assumptions or insecure TLS fallback. | Status shape; successful recovery has `state: "recovered"` and updated `observed`/`recovery`. |

`observed.availability` is `"unknown"`, `"healthy"` or `"unhealthy"`; when known,
`machine`/`application` describe the owned resources. Query/guard failures produce
an actionable sanitized observation, not raw underlying output. `state` is one of
`disabled`, `maintenance`, `configuration_unavailable`, `session_unavailable`,
`inactive`, `intentionally_stopped`, `intent_unset`, `healthy`, `unhealthy`,
`recovery_failed`, `retry_exhausted` (plus `recovered` for a successful tick).

`recovery` contains `version: 1`, `attempts` (0–3), `next_retry_at` (UTC Unix seconds
or null), `history` (last ten outcomes). Each event has UTC Unix `at`, `attempt`
and `outcome` (`interrupted_or_in_progress`, `failed`, `recovered`). Failed events and
failed observations also contain sanitized `reason` and `action`, never raw exception
text. Reasons distinguish `ownership_conflict`, `configuration_conflict`,
`private_access_refused`, `security_gate_failed`, `readiness_failed`,
`fetching_unhealthy`, `external_command_failed` and fallback `recovery_guard_failed`.
Reserving an
attempt before mutation survives process interruption. Healthy ticks and explicit
`start` reset the retry count/backoff but retain history. Raw process errors and
credentials are neither recorded nor copied into the job; background stdout/stderr
are discarded. Configuration/job failures before the worker can run are exposed
by status rather than falsely recorded as successful checks.

Only explicit `start` changes stopped/unset intent to running. `verify`, `replace`
and backup resumption do not change it. Maintenance marker presence blocks public
`start`/`replace`/`verify`/`fetch` and supervisor recovery; a malformed marker also
fails closed. A backup never resumes an intentionally stopped app. Default manual
operation installs no job; actual reboot/login evidence remains separately required.

### Arguments and flags

| Command argument/flag | Required/default | Meaning and failure behavior |
| --- | --- | --- |
| `api-check --loopback` | Optional boolean flag; absent means trusted HTTPS. | Select only the owned app's actual sole IPv4 `127.0.0.1` HTTP publication. Prints an explicit stderr warning; **does not verify HTTPS**. No implicit HTTPS-to-HTTP fallback. |
| `restore ARCHIVE` | Required existing local file; no default. Relative paths resolve from the process working directory. | Missing/nonexistent/directory arguments are usage errors (exit 2). Invalid/corrupt/incompatible archive content fails the operation (exit 1) before VM startup or volume creation. |
| `restore --volume NAME` | Required option, no default. | Fresh target name, different from the selected name even when that selected volume is absent. Every existing target is refused, including empty owned volumes. Failed imports retain an incomplete, unselected target; choose a different fresh name to retry. |
| `select-volume NAME` | Required positional volume name, no default. | Existing owned target; selection alone does not activate it. Use `replace` afterward if an old application container exists, otherwise `start`. |
| Volume name syntax | Applies to both commands above. | 1–128 characters, first ASCII letter/digit, remaining ASCII letters/digits/`_`/`.`/`-`. Not a path or option; invalid names fail the operation (exit 1). |

`api-check` accepts no positional arguments, credential flags/environment variables
or URL argument. It prompts on stderr for the FreshRSS username and hidden separate
API password; cancel/EOF fails nonzero without JSON. Invalid credentials,
TLS/connectivity failures, redirects, invalid loopback publication, incompatible
API, oversized/malformed responses or unmet ownership/security/forwarding
requirements fail with sanitized stderr and no result JSON. Both modes POST
credentials, bypass ambient proxies and refuse redirects. Success never includes
passwords, tokens, feed URLs or article content. See the
[HTTPS/ReadKit runbook](tailscale-readkit.md) for the known host-side TLS limitation.

### Nested results and failure safeguards

- `scheduler`: `running`, `cron` (`"*/20 * * * *"`), `timezone` (`"UTC"`).
- `forwarding`: `status` (`"unconfigured"`, `"running"` or `"stopped"`); a
  recorded route also includes its private `url`. Treat origins as private.
- `security`: the sanitized audit stored in `security.json`: `checked_at`,
  `checked_container`, `image`, `image_id`, `platform`, `platform_manifest`,
  `freshrss`, `advisory_review_date`, `web` (PHP/SAPI/INI metadata), `cli_php`,
  `official_command`. A past check is not an advisory scan or a future-security
  guarantee; see [architecture](architecture.md).
- Backup/restore `manifest`: the [versioned archive metadata](backup-restore.md#format-version-1).
  `sha256` is the whole ZIP checksum, not just the payload checksum.

Commands share a nonblocking local lock: concurrent callers fail rather than queue.
Native children inherit it so orphaned external work retains serialization if the
Python parent is killed; it does not serialize unrelated external writers.
Resources are normally retained for diagnosis, not automatically rolled back or
deleted. A failed startup can leave the VM running; first-run loopback readiness
failure can leave the app serving. Private/previously private post-start failures
instead attempt ownership-checked application shutdown; inability to confirm
cleanup explicitly warns of possible remaining exposure. Preflight refusals can
leave existing serving state untouched. Inspect `status` and the
[operations guide](operations.md), not just the exit code.

**Backup and shutdown failures differ:** explicit `backup` attempts prior-mode
resumption even after snapshot failure, regardless of this shell's `RSS_TAILSCALE`,
but never resumes an intentionally stopped application.
A failure is not proof that remote access is closed. Resume failure also exits 1,
even if an archive was published; inspect private `last-backup.json`. Shutdown
backup failure after quiescing leaves writers/forwarding stopped, VM running, data
and prior archives intact; fix the cause and retry `stop`. Foreign-workload or
forwarding-ownership preflight refusals can leave serving untouched. See
[recovery failure handling](backup-restore.md).

## Developer commands and results

Each command has syntax:

```text
uv run --locked rss-dev COMMAND [--project DIRECTORY] [--help]
```

There are no positional arguments. Optional `--project DIRECTORY` belongs **after
`check`, `setup`, `format` or `fix`**, not at group level. It defaults to the
process's **current working directory**, resolving relative paths from there,
not automatically the executable's source checkout. Files are usage errors;
select a synchronized project directory (the Git repository root for `setup`).
There are no other project-defined flags. `--help` prints help only.

| Command | Purpose, defaults and result |
| --- | --- |
| `rss-dev check [--project DIRECTORY]` | Run the complete non-live pytest suite, Ruff lint and Ruff format checks without source fixes. All three run even when an earlier check fails. JSON `checks` names: `pytest`, `lint`, `format`. Pytest gets explicit `-m 'not live'`; Ruff gets `--no-fix --no-fix-only`, overriding configured autofixes. |
| `rss-dev setup [--project DIRECTORY]` | Inspect Git and safely activate pre-push hooks. JSON has `ok`, `active`; no-remote/manual-resolution cases also have `reason`, `next_step`. No remote: success with `active: false`, rerun after adding one. Conflicting/recognized nonexecutable hooks: exit 1, `active: false`, bytes/mode preserved. Recognized executable hooks remain unchanged. See [managed hooks](development.md#managed-pre-push-hooks). |
| `rss-dev format [--project DIRECTORY]` | Explicitly rewrite Python formatting with Ruff, no lint fixes. JSON `checks` name: `format`. |
| `rss-dev fix [--project DIRECTORY]` | Explicitly apply safe Ruff lint fixes only; unsafe fixes disabled, no formatting. JSON `checks` name: `fix`. |

Examples:

```sh
uv run --locked rss-dev check
uv sync --locked --project /path/to/checkout
uv run --locked rss-dev check --project /path/to/checkout
```

`check`, `format` and `fix` emit one indented JSON object like
`{"ok": true, "checks": [{"name": "pytest", "returncode": 0}, ...]}`; entries
vary by command. `ok` is true only when every child succeeds. OS-level child launch
failures are recorded as return code 127. `setup` emits its hook-status object,
not a `checks` array. Logical failures still emit JSON with `ok: false` and exit 1;
success exits 0, including inactive no-remote setup. Child output goes to stderr.
Invalid CLI usage exits 2 without result JSON. Explicit format/fix mutate files;
neither they nor the baseline start FreshRSS.

Tools use the **selected checkout's local `.venv` interpreter**, not the invoking
checkout's editable installation. Python uses isolated import mode; child
environments remove `PYTHONPATH`, `PYTHONHOME`, all `PYTEST_*` variables and
inherited Git repository selectors. This prevents ambient test selection/imports
or a push hook's Git context from redirecting work into another checkout. Git
configuration/authentication variables remain intact; global/system configuration
and `core.hooksPath` safeguards still apply. Full filter details are in
[development](development.md#selected-checkout-isolation).

These commands do not create/synchronize environments or fall back to another
checkout's packages. Missing interpreters/tools fail; rerun the selected checkout's
sync after dependency changes. `UV_PROJECT_ENVIRONMENT` external/shared environments
are not selected: use checkout-local `.venv`. New hook installation also uses that
local interpreter; no-remote setup and existing-hook inspection retain their
behavior. Checks suppress bytecode and pytest/Ruff caches.

## Pytest selection

`uv run --locked pytest [OPTIONS] [file_or_dir ...]` gives normal human-readable
pytest reports, not CLI JSON. With no paths, configuration selects `tests/`;
explicit paths/node IDs narrow a direct run, **not** the required full baseline.

| Argument/option | Purpose and default | Example |
| --- | --- | --- |
| `[file_or_dir ...]` | Optional paths/node IDs; omit for the complete configured suite. | `uv run --locked pytest tests/test_commands.py` |
| `-m EXPRESSION` | Marker expression; project default `not live`. Explicit `live` requires real-service prerequisites. | `uv run --locked pytest -m live` |
| `-s` | Disable normally enabled output capture. | `uv run --locked pytest -m live -s` |
| `-p no:cacheprovider` | Disable pytest's cache plugin; the baseline does this automatically. | `uv run --locked pytest -p no:cacheprovider` |
| `-q`, `--quiet` | Reduce report verbosity; absent by default. | `uv run --locked pytest -q` |
| `--tb=short` | Short failure tracebacks instead of default `auto`. | `uv run --locked pytest --tb=short` |
| `-h`, `--help` | Conventional text help/configuration, exit 0 without running tests. | `uv run --locked --offline pytest --help` |

Other upstream options are in pytest help. Direct runs retain normal pytest
environment handling; use `rss-dev check` for the guarded baseline. Pytest exits
0 on success, 1 for failed tests, 2 for interruption/collection errors, 3 for
internal errors, 4 for usage errors, or 5 when no tests are collected.

Read [live/platform checks](development.md#explicit-liveplatform-checks) before
selecting `live`; they interrupt the project session. `RSS_LIVE`,
`RSS_OBSERVE_CRON` and the old unittest commands are unsupported. The removed
`--observe-cron` option is a pytest usage error; routine tests do not observe
natural production cron intervals. Do not publish `--showlocals` output, which
can expose secrets.

## uv setup and dependency commands

These are upstream uv commands used by this project, not extra `rss` operations.
They produce uv's text progress/diagnostics, not operation JSON. Environment,
resolution, download or build failures exit nonzero before any project command;
`uv run` propagates the invoked command's status when it runs successfully.

| Command/option | Purpose, defaults and failure behavior |
| --- | --- |
| `uv sync --locked` | Synchronize the checkout-local `.venv`, including the default development dependency group and editable project. May download Python/packages/build backend. Refuses a missing/stale lock requiring changes rather than silently updating it. No hook installation or FreshRSS startup. |
| `uv sync --locked --project DIRECTORY` | Synchronize the selected checkout instead of the discovered current project. Needed before targeting another checkout with `rss-dev --project`. |
| `uv run --locked EXECUTABLE [ARGS ...]` | Run the chosen command in the project environment, synchronizing as needed but refusing lock changes. Put executable-specific flags after `EXECUTABLE`. |
| `--offline` (uv option) | Optional; disable uv network access and rely on cached resources. Missing caches fail. Does **not** prevent `pytest -m live` or `rss` from contacting services. Used by the pre-push entrypoint. |
| `uv lock` | Intentionally regenerate `uv.lock` after reviewed dependency changes; may resolve/download metadata. Mutates the lock, not an implicit step in normal setup/checks. Follow with locked sync and the full baseline. |
| `uv sync --help`, `uv run --help`, `uv lock --help` | Upstream text help for further uv options, exit 0 without performing those operations. |

Operational uses of upstream Homebrew/Podman commands are scoped in
[operations](operations.md), [configuration](configuration.md#changing-the-loopback-port)
and [recovery](backup-restore.md). Credential-free curl diagnostics and their
flags/HTTP-status semantics belong to the canonical [private-access runbook](tailscale-readkit.md#real-https-checks--human-assisted).
There is no project command for prune/reset, VM/volume deletion, package uninstall,
automatic image updates, forwarding adoption/reset or an insecure TLS bypass.
