# CLI reference

For procedures and safety checks, see [operations](operations.md), [backup and restore](backup-restore.md), and [Tailscale and ReadKit](tailscale-readkit.md). Select the intended [runtime configuration](configuration.md) before operating on an instance.

## Entrypoints and help

Run commands from the selected checkout after `uv sync --locked`:

```sh
uv run --locked rss --help
uv run --locked rss restore --help
uv run --locked rss-dev --help
```

`rss COMMAND [ARGS]` manages the native runtime. `rss-dev` manages Python tooling, not FreshRSS. `uv run --locked python -m rss_aggregator` is equivalent to `rss`. Both groups and their subcommands accept `--help`, which prints text and exits 0 without running the operation. There are no other project-defined group options. Subcommand options belong after the subcommand.

`./scripts/rss COMMAND [ARGS]` is a supported launcher with `uv` on `PATH`. It forwards all arguments (including `--help`) and exits with the launched command's status. You can also invoke `/path/to/checkout/scripts/rss` from another directory: it passes its own checkout to `uv run --locked --project` so, unless `RSS_STATE_DIR` is set, the checkout containing the wrapper determines the default state directory. Relative environment paths still resolve from the process working directory.

`rss` emits one indented JSON object on successful operations (exit 0). Diagnostics and prompts go to stderr. Guard, configuration and operation failures exit 1 and normally have no result JSON; see the exception for `backup-status --check`. Invalid CLI usage exits 2. Commands may initialize private control state even for inspection. Some errors occur after work has started: a nonzero exit does not establish that no files changed, no archive was published, or forwarding was withdrawn. Use the corresponding status commands and follow the runbooks before retrying. JSON can contain local paths, private hostnames, or aggregate counts; redact before sharing.

## Runtime commands and results

Use `uv run --locked` before each command below. There are no arguments or flags beyond those shown and `--help`.

| Command | Purpose, behavior and result |
| --- | --- |
| `rss setup` | Install the checkout's checksum-pinned Apple Silicon runtime, or verify a matching existing installation. Returns the native ownership receipt (`schema`, `token`, `manifest`, `source_tree_sha256`). Does not start services or install jobs. Refuses foreign paths, drift, and implicit upgrades. |
| `rss start` | Record running intent after the maintenance guard and start, reopen, or retain a matching backend after rechecking effective web security. A genuinely new local instance allows the upstream installer. An initialization change uses guarded restart; initialized startup checks authentication/SQLite and fetches feeds. Missing previously provisioned data are refused. A requested private route is published only after the startup gates. Returns `runtime`, `application`, `url`, `instance`, `security`, `scheduler`, `fetch`, and `forwarding` when private; a retained service also includes status fields. `fetch.status` is `installer_pending`, `completed` or `already_running`. |
| `rss status` | Inspect without starting writers. Always reports `runtime` (`installed`/`absent`), `application` (`running`/`stopped`/`unconfirmed`) and `http_ready`. For an installed runtime it includes `instance`, `scheduler` and `forwarding`; `url` and `port` appear when a live generation is observed. A successful status is not a health attestation; check `http_ready`, scheduler and forwarding separately. |
| `rss fetch` | Attempt an immediate upstream fetch on the selected, verified running service, respecting FreshRSS cache, TTL and backoff. JSON `status` is `completed` or `installer_pending`. An overlapping fetch or unready service can be refused. Completion is not a count of new articles. |
| `rss verify` | Reopen the live owned service through the startup checks, preserving its observed private/local mode. Requires a running service; can briefly interrupt access. Returns the `start` result plus `verified: true`. On confirmed public exposure of that backend, requests writer shutdown without changing the foreign route and reports confirmation or uncertainty. Not a trusted-client HTTPS check. |
| `rss stop` | Persist explicit stopped intent, withdraw owned forwarding, stop writers and verify a shutdown backup when selected data exist. Returns `application: "stopped"`, `already_stopped`, and `backup` (a verified archive result or `{"status":"no_data"}` on a pristine installation). Writers are stopped before runtime layout verification for capture. A failed verification, missing previously provisioned data, or other backup failure can leave writers stopped and still return exit 1. Inspect `backup-status`; correct the cause before retrying. A maintenance hold may block further work even when stopped intent was recorded. |
| `rss api-check [--loopback]` | Prompt locally for the FreshRSS username and hidden **separate API password**. Default uses the owned trusted HTTPS origin; `--loopback` explicitly tests only the owned `127.0.0.1` backend, without checking TLS. Both require a verified running service. Returns `diagnostics: "PASS"`, `authenticated`, `subscriptions` and `sample_articles` counts, `transport` (`https`/`loopback`) and `https_verified`; never returns credentials, tokens or response bodies. Refuses redirects, untrusted HTTPS, rejected credentials or unusable responses. |
| `rss backup` | Quiesce writers and publish a complete verified archive to the configured destination. Attempt to resume a previously running backend if allowed by intent; republish only the forwarding withdrawn by this backup. Returns `archive` (absolute ZIP path), `sha256` (whole ZIP), `verified`, `manifest`. Publication and resumption can succeed or fail independently; inspect `backup-status` on failure. |
| `rss restore ARCHIVE --instance NAME` | Read an existing native v2 archive into a **fresh** owned instance without selecting or starting it. `ARCHIVE` must be an existing file path; `--instance` is required and has no default. Returns `restored: true`, `instance`, `selected: false`, `manifest`, `next_step`. Refuses incompatible runtime metadata, unsafe archives, the selected name and existing targets. |
| `rss select-instance NAME` | Select a complete, compatible owned instance by name (not a path) while all writers are stopped and maintenance is clear. Returns `instance`, `selected: true`, `next_step`; starting is separate. A later enabled supervisor may honor retained running intent; explicitly stop before changing selection. |

Use lowercase names of up to 48 characters for recovery targets: start with a letter or digit, then use letters, digits or hyphens. `default` is initially selected; choose a different fresh name when recovering. See [backup and restore](backup-restore.md) before using an archive. The `scheduler` result reports `running`, `cron` (`*/20 * * * *`), `timezone` (`UTC`) and `last_fetch` (last periodic result for this generation, or null). `security` reports the effective web `sapi`, `php`, `upload_progress`, `expose_php`, `display_errors`, `sqlite`, `loaded_ini`, `scanned_ini`, `initialized`, `authenticated_settings`, and `verified`; it is not just CLI PHP metadata. `forwarding.status` is `unconfigured`, `stopped` or `running`, with a `url` for a recorded route. It is not end-to-end client verification.

### Preview-first runtime changes

| Command | Preview and apply |
| --- | --- |
| `rss upgrade [--confirm TOKEN]` | Explicitly change to this checkout's reviewed PHP/web runtime pin for the **same FreshRSS source and platform**. If instances exist, apply requires the selected instance's verified shutdown archive covering its last writer generation, revalidated against the installed pin. Stages and verifies replacement before publishing; does not start FreshRSS. Matching pins need no replacement. Returns `status: "installed"` and `manifest`. This does not migrate FreshRSS versions or make earlier archives compatible with a different runtime. |
| `rss uninstall [--confirm TOKEN]` | Remove only the verified owned runtime, retaining instance data, ownership and archives. Does not remove global packages, Tailscale or a legacy VM. Returns `status: "uninstalled"` and `preserves_data_and_archives: true`. |

Both require stopped writers and no running intent, no supervision or daily backup registration, and no active or uncertain owned forwarding. Resolve maintenance first, verify a backup and follow [operations](operations.md#safe-cleanup). Omit `--confirm` for the preview (`status: "preview"`, `operation`, `runtime_path`, `installed`, `preserves_data_and_archives`, `confirmation`, plus `reviewed_manifest` for upgrade). To apply, rerun the same command with `--confirm` and the exact SHA-256 token from the current preview. An incorrect or stale token refuses without applying. Preview is not authorization to delete data or repair ownership records.

### One-time migration command

`rss import-legacy ARCHIVE --instance NAME` is intentionally hidden from group help; its own `--help` works. Its arguments and non-activation behavior match `restore`; its success result adds `imported: true`. It accepts only the reviewed legacy v1 image identity at the same FreshRSS release, validates the full payload and publishes a fresh private native instance. It never operates on the legacy VM. This is **not** ongoing legacy restore support or a reverse importer; follow the [one-time migration handoff](operations.md#one-time-migration-from-podman).

`replace`, `select-volume`, `remove-vm`, `delete-volume`, and `remove-package` are not native commands. The [legacy CLI and guarded removal documentation](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/cli.md#cleanup-commands) stay with their own revision and deployment.

### Mac supervision commands

| Command | Purpose and result |
| --- | --- |
| `rss supervision-install` | Explicitly register/activate the owned normal-user GUI-session job, capturing this checkout and its settings. Returns supervision status; does not set running intent. Refuses a missing session, foreign/changed definitions or changed capture rather than replacing them. |
| `rss supervision-remove` | Unload and remove only unchanged owned supervision; preserve intent, service and data. Returns supervision status; refuses foreign/changed jobs. |
| `rss supervision-status` | Report `enabled`, `state`, `intent`, `maintenance`, `session`, `observed`, `recovery`, and `action`; registered jobs also include `job`, `root`, `state_dir`. Observation does not start/restart writers. Disabled, inactive, stopped, unavailable, maintenance and unhealthy states are reportable results, not necessarily command errors. |
| `rss supervision-tick` | Run one supervision health/recovery cycle from the selected state. No recovery without a matching enabled job, positive running intent, and clear guards. Returns status with the observed state or `recovered` after recovery. Backoff, exhausted attempts, configuration, maintenance or failed recovery can exit nonzero. |

Use [captured configuration](configuration.md#captured-mac-supervision-configuration) and [operations](operations.md) to change job settings. Daily backup is independent.

### Daily backup commands and results

| Command | Purpose and result |
| --- | --- |
| `rss backup-enable [--hour HOUR] [--minute MINUTE]` | Explicitly install/activate the owned user-session daily backup job with a private backup destination. UTC hour `0–23` (default `3`), minute `0–59` (default `0`). Result includes `enabled`, `job`, `daily_utc`, `next_expected_attempt`. Activation is not evidence of protection; changed captures require explicit disable/re-enable. |
| `rss backup-disable` | Remove only the owned daily job; keep evidence and archives. A missing plist is recoverable with a valid ledger and matching live identity if loaded. Returns `enabled: false` and, on removal, `archives_preserved: true`. Foreign/changed jobs are refused. |
| `rss backup-due` | Run only eligible daily work for the configured job. `outcome` is `disabled`, `not_due` (with `next_expected_attempt`) or `success` (with `backup`). Guard, contention or capture/resumption failures can exit 1; inspect `backup-status`. Not a force-backup command; use `backup` for a manual capture. |
| `rss backup-reconcile` | Release an interrupted maintenance hold only after checking ownership and confirming idle writers and no active owned forwarding. Returns `reconciled`, `resumed: false`, `action`. Does not start writers or change intent. Refuses unknown holds and unconfirmed writers/forwarding. |
| `rss backup-status [--check]` | Report daily protection and backup/maintenance evidence without starting writers. Default exits 0 for a report even if unprotected. `--check` emits the same JSON then exits 1 with a stderr diagnostic unless `protected` is true. |

### Daily backup status results

- `enabled`, `registered`, `registered_at`, `registration_id`, `scheduler_problem`, and `next_expected_attempt` describe the captured schedule and session registration, not a successful backup. `registered_at` and `registration_id` are null without a completed registration. An identical disable/re-enable starts a new registration. `busy` means the operation lock is held; status can still report recorded evidence.
- `daily_attempt`, `daily_due`, `missed`, and `latest_defer` describe the most recent daily work and contention. `daily_attempt` includes the slot, attempt time/count, outcome, instance, policy and registration IDs, and, on success, `archive` and `sha256`. `daily_archive_available` checks the scheduled archive for the current registration and instance. `latest_attempt`, `latest_success`, and `resumption` distinguish the latest operation of any kind, verified publication, and restart outcome.
- `age_seconds`, `overdue` and `archive_available` describe the last known archive (no success or age over 26 hours is overdue). File presence does not revalidate archive bytes or prove independent recovery.
- `maintenance_required` indicates a hold; inspect and reconcile rather than removing the record. `protected` is the combined local daily health signal for the selected instance, requiring a matching healthy registration, its available recent scheduled archive, and no outstanding work, failed latest attempt, or maintenance. It is not Time Machine evidence.

## Developer commands and results

Each command has syntax `uv run --locked rss-dev COMMAND [--project DIRECTORY] [--help]`. There are no positional arguments. Optional `--project DIRECTORY` belongs after the subcommand and defaults to the current working directory; relative paths resolve from there. Select a synchronized project directory (the Git repository root for `setup`). Files are usage errors.

| Command | Purpose, defaults and result |
| --- | --- |
| `rss-dev check [--project DIRECTORY]` | Complete non-live pytest, Ruff lint and Ruff format checks. All three run even after an earlier failure. JSON `checks` names: `pytest`, `lint`, `format`. No source fixes. |
| `rss-dev setup [--project DIRECTORY]` | Inspect Git and activate pre-push hooks safely; returns `ok`, `active`, and for no-remote/manual-resolution cases `reason`, `next_step`. No remote: success with `active: false`; rerun after adding one. Refuses foreign/legacy/nonexecutable arrangements without changing them, and preserves recognized executable hooks. See [managed hooks](development.md#managed-pre-push-hooks). |
| `rss-dev format [--project DIRECTORY]` | Rewrite Python formatting with Ruff; no lint fixes. JSON `checks` name: `format`. |
| `rss-dev fix [--project DIRECTORY]` | Apply only safe Ruff lint fixes; no formatting or unsafe fixes. JSON `checks` name: `fix`. |

`check`, `format`, and `fix` emit `{"ok": true, "checks": [{"name": "pytest", "returncode": 0}, ...]}`, with entries appropriate to the command. `ok` is true when every child succeeded; OS-level launch failures are recorded as return code 127. Logical failures still emit JSON with `ok: false` and exit 1. Child output goes to stderr. Invalid CLI usage exits 2; help exits 0. Commands use the selected checkout's local `.venv` and isolated child environment, not the caller's editable installation or a shared environment. They do not create/synchronize environments or start FreshRSS. See [selected-checkout isolation](development.md#selected-checkout-isolation).

## Pytest selection

`uv run --locked pytest [OPTIONS] [file_or_dir ...]` prints normal pytest reports, not CLI JSON. No path selects `tests/`; paths/node IDs narrow direct runs, not the full baseline. The default marker expression is `not live`. Select the native integration explicitly with `uv run --locked pytest -m live tests/integration/test_native.py` after reading the [platform prerequisites and limits](development.md#explicit-liveplatform-checks).

`-m EXPRESSION` selects markers; `-s` disables capture; `-p no:cacheprovider` disables the cache plugin; `-q`/`--quiet` reduces verbosity; `--tb=short` shortens tracebacks; `-h`/`--help` prints upstream help. `rss-dev check` is the guarded baseline; direct runs retain normal pytest environment handling. Pytest exits 0 on success, 1 for test failure, 2 for interruption/collection errors, 3 for internal errors, 4 for usage errors, and 5 for no tests collected. `RSS_LIVE`, `RSS_OBSERVE_CRON`, `--observe-cron` and old unittest paths are unsupported here. Do not publish `--showlocals` output.

## uv setup and dependency commands

These upstream commands produce uv text/progress rather than project JSON. Resolution, environment and build failures exit nonzero before a project command runs.

| Command or option | Purpose |
| --- | --- |
| `uv sync --locked [--project DIRECTORY]` | Synchronize the selected checkout's `.venv` with the lock and default development group. May download Python/packages/build tools; refuses stale or missing lock requiring changes. No hooks or FreshRSS startup. |
| `uv run --locked EXECUTABLE [ARGS ...]` | Use the project environment, syncing if needed without changing the lock. Options after the executable go to that executable. Propagates its exit status. |
| `--offline` (uv option) | Use cached resources only; missing caches fail. Does not block network access by a command such as native setup or selected integration. Used by the pre-push gate. |
| `uv lock` | Intentionally regenerate `uv.lock` after reviewed changes; follow with locked sync and baseline checks. |
| `uv sync --help`, `uv run --help`, `uv lock --help` | Upstream help for additional options; no operation is performed. |
