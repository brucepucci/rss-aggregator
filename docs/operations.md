# Operating the Mac instance

Start with the [README](../README.md#getting-started). This page covers less-common
setup decisions, lifecycle safeguards and diagnostics. For exact arguments and
result fields use the [CLI reference](cli.md); for settings use
[configuration](configuration.md).

## Installation and ownership

`uv sync --locked` selects the pinned Python version and installs checkout-local tools
(it may reuse an installed interpreter or download one);
`uv run --locked rss setup` manages the Podman host footprint. Developer hook
activation is separate and is not required to operate FreshRSS.

Setup inventories Homebrew formula/cask versions/dependencies and Podman
machines/connections, including containers, volumes and images reachable through
running machine connections. Stopped machines and non-machine connections are
recorded but not started/contacted to enumerate their contents. It previews CLI
installation, records additions and initializes the dedicated `rss-aggregator`
VM without starting a stopped VM. Existing name collisions are not adopted.

If the preview includes an upgrade/reinstall of an existing package, setup stops:
obtain explicit approval and resolve the dependency plan before retrying. There is
no bypass flag. Automatic Homebrew updates, installation upgrades, dependent checks
and install cleanup are disabled. No package is automatically uninstalled.

If `brew list --cask --versions` fails, setup warns and uses two read-only queries:
`brew info --json=v2 --installed --cask` for installed versions and
`brew list --cask` for raw names. The private host audit retains omitted names and
the original warning without inventing versions. Failure or unusable output from
either fallback stops setup before installation/VM initialization. It does not
repair, reinstall, upgrade or remove unrelated applications, and does not bypass
installation approval. See [managed inventory fields](configuration.md).

The historical initial install added Podman 6.2.0, libkrunfw 5.6.4, libepoxy 1.5.10,
molten-vk 1.4.2, libkrun 1.19.8 and krunkit 1.3.2. This is not a fixed future
package plan: Homebrew metadata changes. Your preview and private
`host-additions.json` are authoritative for your run. Existing Python/SQLite
Homebrew dependencies are not project-owned additions. Ignore Podman's suggestions
to install its privileged helper or Desktop; explicit CLI connections need neither.

Keep a single controlling checkout and preserve its `.local/` directory. Worktrees
do not namespace Podman resource names. To operate an existing instance from
another worktree, explicitly set `RSS_STATE_DIR` to the original private ownership
directory. Never copy, replace or fabricate ownership records to adopt resources.
Changing the directory is not migration. A new host needs fresh ownership and an
[instance restore](backup-restore.md), not the old host's owner record.

## Start, verify and replace

`start` explicitly targets the owned rootless `rss-aggregator` connection. It starts
the VM if needed, reuses/restarts or creates the application container (with the
cold-start exception below), copies both checked-in new-user templates while
stopped, checks effective Apache PHP security, then verifies HTTP readiness, cron
and immediate feed fetching. Repeated starts
briefly interrupt service but do not duplicate the VM, users, subscriptions or
volume. Existing users' settings/subscriptions are not rewritten by the templates.
A failed template copy stops startup; retry repeats both copies.

**Cold startup:** only when this start call starts a stopped owned VM, a matching
stopped application container is removed and recreated on the **same selected
named data volume**. Ownership, port, pinned image and disposable effective-PHP
preflight must pass, followed by all existing application image, binding,
entrypoint, default-command, internal-`LISTEN` and selected-volume checks. A final
owned inspection must confirm no state/configuration change and that it is still
stopped. Removal uses neither force nor volume deletion. Foreign, mismatching,
changed or running containers never qualify; a running app follows the existing
restart path. Do not use cold startup to migrate a volume or bypass a configuration
refusal: inspect locally and use explicit `replace` only after reviewing the change.
Warm starts (VM already running) retain reuse and partial-copy retry behavior.

Recreation discards container-layer diagnostics/customizations and may expire web
sessions. It preserves subscriptions, articles and read/star state in the named
data volume; sign in again and verify them. Preserve needed diagnostics before a
planned cold start and obtain a verified instance backup before significant live
changes. A fresh container layer avoids carrying disposable runtime files through
VM shutdown, but the precise failure mechanism is not established. There is no
automatic replacement fallback after arbitrary security/readiness failures; every
post-create security, probe-cleanup, readiness, authentication and forwarding gate
still applies. Real-Podman cold-start acceptance remains an
[explicit live check](development.md#combined-supervision-and-backup-verification).

The security gate checks the effective web runtime, not just PHP CLI. See
[the gate design](architecture.md#startup-security-gate). Only enter real
credentials after `start` succeeds. The first installation remains at FreshRSS's
installer until you choose SQLite and authenticated web login locally.

`verify` requires a running owned VM and uses the normal start path: it starts a
stopped application or restarts/rechecks a running one. `replace` removes only
the owned application container and recreates it with the same selected named
volume and current configuration. Use it for reviewed container/image/port changes;
it is not data deletion. For port changes, first follow the
[verified-backup, job removal and recapture procedure](configuration.md#changing-the-loopback-port);
installed jobs retain the old port unless explicitly reconfigured. Browser sessions can expire after replacement; sign in
again and verify subscriptions, articles and reading state. For a restored volume,
[matching first activation](backup-restore.md#format-version-1) is required before
an intentional image update.

For private or previously private instances, failures after backend startup attempt
to close the ownership-checked application. If cleanup cannot be confirmed, the
error warns that exposure may remain. Initial preflight refusals leave existing
state untouched. General loopback startup failures may leave the application and
VM running for diagnosis; failure is not proof HTTP is closed. Inspect `status`.

## Opt-in Mac startup and recovery

Manual operation remains the default: `setup`, `start` and code checkout never
register a host service. After the maintenance guard passes, `start` persists
**running intent**, even if subsequent startup fails. `stop` persists **stopped
intent** before checking maintenance or attempting shutdown, even if already
stopped or shutdown subsequently fails. Only explicit `start` re-enables running intent.
`verify`, `replace`, temporary backup quiescing and login/reboot do not change it.
An older instance without an intent record is **unset**, not implicitly running.

### Enable or remove supervision

**Before real installation:** obtain and verify a private instance backup, coordinate
with the owner, and choose one stable checkout and the existing ownership directory.
Do not use a temporary worktree that will be deleted. Finish the loopback installer
and verify the intended private HTTPS/authentication configuration first. Keep all
existing FileVault, login, power and Tailscale settings unchanged.

From that synchronized checkout, with the instance's existing settings exported:

```sh
export RSS_STATE_DIR="/absolute/private/existing-ownership-directory"
# Explicit owner-selected production destination, not a global default:
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
# Keep RSS_PORT and RSS_TAILSCALE consistent with the verified deployment.
uv run --locked rss backup
# Confirm verified:true and retain the private archive/checksum before continuing.
uv run --locked rss supervision-install
uv run --locked rss supervision-status
```

Installation is explicit and activates `io.rss-aggregator.supervision` in the normal
user's `gui/<uid>` launchd domain. It writes only its owned LaunchAgent; no root
service, login/power change, dependency installation or unrelated job modification.
Repetition with the same captured configuration is safe. Changed/unowned job files,
symlinks, loaded-label collisions and ownership ambiguity are refused, not adopted.
Partial activation retains the registration for inspection and safe retry/removal.

The job uses the selected checkout's `.venv/bin/python`, absolute checkout/state
paths, captured non-secret runtime settings and explicit dependency search paths;
it does not invoke uv, source a shell or read `.env`. Keep that checkout, environment
and ownership directory available. Moving them, changing the image pin or losing a
captured dependency requires local correction or explicit removal/reinstallation;
there is no fallback to another installation. Take a verified backup before
significant changes and remove supervision before intentionally reconfiguring it.

To disable startup/recovery safely:

```sh
uv run --locked rss supervision-remove
```

Removal unloads/removes only the unchanged owned job. It **does not stop FreshRSS**,
erase running/stopped intent, delete data/backups, or stop Tailscale. Use `rss stop`
for an intentional backed-up shutdown. Removal is repeatable; changed/unowned jobs
remain intact for manual resolution.

### Daily check and failure response

Run `rss supervision-status`; use `rss status` for the existing detailed runtime
check. Supervision status separates registration, user session, intent, observed
availability, maintenance and recent recovery outcomes. A successful status query
is not a healthy-service assertion: inspect `state`, `observed`, `recovery` and
`action`. Missing prerequisites are visible, not treated as successful recovery.
No raw FreshRSS/Podman logs are needed for this first response.

- **disabled / inactive / configuration_unavailable:** verify the selected checkout,
  `.venv`, captured dependencies and unchanged owned job. Reinstall explicitly only
  after resolving ownership/configuration; do not replace foreign jobs.
- **session_unavailable:** log in/unlock the normal Mac user session. The job is not
  a pre-login/pre-unlock service. Status cannot run on an unreachable/powered-off
  host; an earlier healthy result is not current remote availability evidence.
- **intentionally_stopped / intent_unset:** no automatic start is authorized. After
  any maintenance inspection, `rss start` explicitly resumes running intent.
- **maintenance:** recovery and public start/fetch/verify/replace refuse to open
  writers on any marker presence, including malformed data or dangling links.
  Preserve the marker and inspect the interrupted operation. `backup-reconcile`
  validates its owner/schema and confirms owned engines are idle
  before clearing a hold; reconciliation does not itself resume writers or change
  intent, but retained running intent permits a later supervisor tick to recover.
  To keep the app stopped, explicitly `rss stop` **before** reconciliation (stopped
  intent persists even if shutdown is refused). If reconciliation refuses, coordinate
  with the owner rather than deleting a marker to bypass the boundary.
- **unhealthy / recovery_failed / retry_exhausted:** inspect dependencies, ownership,
  readiness/security and private forwarding locally. Never bypass TLS or adopt
  colliding resources. Correct the cause, then explicit `rss start` resets the
  retry budget. `observed.reason`/`action` and failed history events distinguish
  ownership/configuration conflicts, private-access refusal, security/readiness/
  fetching failures and dependency errors without copying sensitive process output.
  `stop` remains authoritative even after a failed recovery.

Launchd requests a check at load/login and every **60 seconds** while the session
and host are available; sleep/missed intervals are not continuous availability or
an overlapping queue. Healthy checks inspect HTTP, fetching cron and owned
forwarding without restarting the application. An unhealthy instance gets at most
**three consecutive guarded recovery attempts**, with at least **60 seconds** then
**300 seconds** before attempts two and three. Exhaustion requires operator action;
healthy observation or explicit start resets the budget, retaining the last ten
sanitized outcomes. An attempt is reserved before mutation, so process death does
not reset retries. Ordinary startup ownership/image/PHP/authentication/readiness/
fetching/forwarding gates remain mandatory.

The nonblocking project lock spans each entire check/recovery/backup/lifecycle
operation; contention fails visibly and is retried on a later tick, not queued.
Short-lived native children inherit the lock so orphaned exports/imports keep
serialization after a Python process dies. Only the VM launcher (`podman machine
start`) omits inheritance, in both start and recovery, so long-lived gvproxy/krunkit
helpers cannot retain the lease indefinitely. The Python parent holds the lock
through the launcher; durable maintenance remains mandatory. Never unlink a busy
lock or kill real helpers to make a check pass: inspect locally with the owner.
Backup quiescing does not change intent, and a backup never resumes an intentionally
stopped application. Independent external writers are
not serialized by this lock.

### Owner-assisted Mac acceptance (not automated evidence)

The implementation and controlled tests do **not** establish actual launchd,
reboot, login/unlock, outage, power-recovery or trusted-client behavior. Before
#21's production trial, coordinate and record separately:

1. A fresh verified private backup, selected revision/checkout/settings and a
   preservation inventory of unrelated jobs/resources; never publish credentials,
   private URLs or raw application logs.
2. Owner acceptance of the existing FileVault/unlock/login, sleep and power-recovery
   limitations. Verify the mini's actual settings/behavior without changing them;
   do not silently enable automatic login or simulate physical power loss.
3. A real coordinated reboot, its time and normal-session availability time, then
   successful startup/status and trusted HTTPS/actual reader checks. Locked disks,
   login prerequisites and any unperformed power/outage scenarios stay explicit.
4. A coordinated disposable application-failure recovery check and a healthy-check
   noninterruption check, preserving subscriptions/articles/read/star state and
   unrelated VM/forwarding resources.
5. Explicit `stop`, ticks and another coordinated reboot/login remaining stopped;
   explicit `start` alone resumes service. Record backup quiescing interaction and
   persistent-error response separately from application availability.

The mini's existing self-HTTPS diagnostic limitation is not permission for insecure
TLS. Trusted client checks and loopback diagnostics remain distinct. See the
[private-access runbook](tailscale-readkit.md).

## Scheduling and reading state

FreshRSS actualizes feeds at startup and through the official image's enabled cron
at **UTC minutes 00, 20 and 40** (`CRON_MIN=*/20`), not 20 minutes after startup.
`status` inspects the crontab and running daemon. Cron is upstream's background
daemon, not a separately supervised service; missing/dead cron makes the check fail.

Actualization respects conditional HTTP, feed TTL, caching and publisher backoff.
The new-user TTL is 60 seconds rather than upstream's 3600. The upstream HTTP cache
default is 800 seconds, normally at least 60 seconds; publisher headers can extend
it to 86400 seconds, and `no-cache` still respects the minimum. Reducing TTL does
not bypass HTTP caching. `fetch` completion is not a promise of new articles.

Client synchronization exchanges existing articles and reading-state changes with
FreshRSS; it is separate from publisher fetching. Upstream display/read settings
still apply: opening an article or website may mark it read; scroll/focus marking
is off by default. User/category/feed archiving rules can delete articles; the
project's new-user defaults disable age/count purging, but existing users and
per-feed overrides are not rewritten. Full-page scraping is not configured.

## Backups and stopping

Set `RSS_BACKUP_DIR` before `backup` or normal `stop` with data. Follow the
[backup/recovery guide](backup-restore.md) for privacy, independent copies, archive
validation and restore. The VM volume is not a backup.

Before stopping **any** container, shutdown verifies machine/SSH and rootless
connection identity, queries the rootless engine through that connection, and
queries the rootful engine via `podman machine ssh rss-aggregator sudo podman ps`.
It does not trust an unverified root connection. Foreign running workloads in
either engine, or failure of either query, cause refusal without stopping
containers. Rootful workloads are never project-owned; stop them manually only
when safe. Do not mount/write the data volume independently during export.

Normal `stop` removes matching owned private forwarding, quiesces writers, verifies
an external backup and then stops only the project VM. A repeated successful stop
reports already stopped rather than restarting it to back up. An absent data
volume reports `backup.status: "no_data"`. After quiescing, a backup failure leaves
writers/forwarding stopped and the VM running; fix it and retry. Preflight refusals
may leave serving untouched. Explicit `backup`, unlike shutdown, attempts to restore
previous application/forwarding/VM state even after snapshot failure; its failure
does not prove access is closed.

The lock serializes managed commands, not independent Podman/VM-shell writers.
Resources and prior archives are retained on failure rather than automatically
rolled back or deleted. No backups are pruned or uploaded automatically.

Stopping the VM releases its memory allocations and stops fetching/serving. Idle
guest memory is not guaranteed to return to macOS while it remains up. Nothing
starts the application automatically at login or reboot unless the owner explicitly
installs [session supervision](#opt-in-mac-startup-and-recovery); persisted stopped
intent still prevents recovery. Enabling daily backups alone does not enable
application supervision. Tailscale itself is not stopped by project
shutdown; clients cannot synchronize with a stopped FreshRSS instance.

## Daily backup checks

Daily full-instance protection is separate from container feed-fetch cron and
application supervision. It is disabled until the owner explicitly enables the
Mac backup LaunchAgent; ordinary start/manual backup/stop never enable it. Follow
[the coordinated setup procedure](backup-restore.md#opt-in-daily-mac-backups), with
a verified pre-change backup, stable checkout/dependencies, original ownership
path and the explicitly approved private `~/FreshRSS-Backups` destination.
Never install a test schedule or change login/power settings against production.

Routine check: `uv run --locked rss backup-status --check`. JSON is emitted even
when protection is unhealthy and `--check` exits 1. Registration alone is not a
successful backup. Inspect daily target/attempt, missed/deferred/failed work,
latest verified archive age/availability and separate resumption/maintenance
outcomes. Daily targets are UTC (default 03:00), not the local zone; wakeups may
lag up to 15 minutes and sleep/logout can miss work. Three actual failures per
target exhaust automatic retry until the next day. No backlog/pruning is created.
See [failure response](backup-restore.md#daily-check-and-failure-response).

After interruption, do not force writer restart or remove records/locks. Native
subprocesses may survive their parent while retaining the command lock. Coordinate
with the owner, inspect status and processes, then use
[`backup-reconcile`](backup-restore.md#interrupted-maintenance-and-reconciliation)
to confirm owned writers idle before releasing maintenance. It never itself
resumes or changes intent; separately enabled supervision may subsequently
honor running intent. Run `rss stop` before reconciliation if stopped state should
persist; it records stopped intent even when the hold refuses shutdown. Preserve prior good archives,
source data and sensitive temporary remnants until inspection. A verified archive
and a successfully resumed service are separate outcomes.

A real unattended mini attempt and sanitized verification/status evidence remain
required before the production trial. Manually invoking `backup-due`, registering
a job or passing isolated tests does not supply that evidence. Local publication
does not prove Time Machine protection; actual copy retrieval and isolated restore
are separately owner-coordinated (#24).

## Safe cleanup

For now, safe cleanup is **`uv run --locked rss stop`**. No VM/volume deletion,
prune, package uninstall or destructive convenience command exists. **Deleting the
VM destroys its native volumes and all FreshRSS data.** Preserve external backups.
Do not run global Podman prune/reset or remove shared Homebrew dependencies.

Keep `.local/owner.json` and its companion records: they associate the controlling
checkout with machine/connection identity and resource labels. Preserve this
state when relocating a checkout on the same host; use fresh setup and restore
when moving to another host. The package-addition ledger is evidence for later
conservative cleanup, not permission to uninstall packages adopted by other work.
`.venv` and uv caches are tooling, not FreshRSS data or backups. Normal stop leaves
them alone; do not confuse `.local/` with a disposable tool cache.

## Service logs and feed errors

First run `uv run --locked rss status` to check the owned instance and scheduler.
Then open the FreshRSS browser **configuration (gear) menu → Logs** and inspect
the affected feed in **Subscription management**. The Logs page shows the current
user's log, not all container/system logs. Persistent application logs default to
warnings/errors, not a record of every successful fetch. A healthy scheduler does not prove publisher
availability, subscription authorization or the absence of HTTP caching.

For the already running application, these read-only commands inspect different
layers (they do not start a stopped VM/container):

```sh
# Container stdout/stderr: startup, Apache/PHP errors and scheduled-fetch stderr.
podman --connection rss-aggregator logs --tail 100 rss-aggregator-freshrss
# Most recent scheduled actualization stdout, inside the container.
podman --connection rss-aggregator exec rss-aggregator-freshrss \
  tail -n 100 /tmp/FreshRSS.log
```

`--connection rss-aggregator` explicitly selects the owned rootless connection;
`logs --tail 100` limits output to the latest 100 lines (no follow mode).
`exec` runs `tail -n 100` in the named running container, printing up to 100 lines
of that file. Unlike `rss`, these upstream tools emit text, not JSON. Connection,
container or file errors return nonzero; a missing cron file before its first run
is not proof of a broken scheduler. Both stdout and stderr may contain diagnostics.
There is no `rss logs` command.

The scheduled job overwrites `/tmp/FreshRSS.log` with stdout each run; stderr goes
to container stderr. Immediate `rss fetch` does not write this cron file. The file
is lost on container replacement and is not a persistent audit log or part of the
data-volume backup. Successful scheduled summaries can appear there even when
persistent application logging is limited to warnings/errors.

The current user's persistent log is `data/users/<FRESHRSS-USER>/log.txt` in the
volume. General, API and WebSub logs are respectively `data/users/_/log.txt`,
`log_api.txt` and `log_pshb.txt` under the same `_` directory. These are included in
instance backups; use the browser for user logs to avoid printing private paths.
HTTP access logging is disabled (`ENABLE_ACCESS_LOG=0`), but Apache errors are not.
Application-log copying to syslog and syslog mirroring to stderr are off; this does
not disable persistent logs or direct stderr. Container logs are neither a complete
FreshRSS application-log mirror nor an HTTP request history.

**All logs may contain private URLs or content.** Inspect locally and share only
redacted error descriptions, not raw output, authentication responses or archives.
Locations/UI were checked against FreshRSS 1.30.4 revision
`08f97bd497ace733c151ea1c709d85cd5b65cc53`:
[cron redirection](https://github.com/FreshRSS/FreshRSS/blob/08f97bd497ace733c151ea1c709d85cd5b65cc53/Docker/Dockerfile#L52-L56),
[log paths](https://github.com/FreshRSS/FreshRSS/blob/08f97bd497ace733c151ea1c709d85cd5b65cc53/constants.php#L70-L77)
and [Logs menu](https://github.com/FreshRSS/FreshRSS/blob/08f97bd497ace733c151ea1c709d85cd5b65cc53/app/layout/header.phtml#L116).
This is source verification, not a new live session; see
[verification limits](development.md#verification-and-adoption-status).

## Common troubleshooting

- **Another VM is running:** stop it manually only if you own it and it is safe.
  This project will not stop it or change global Podman defaults.
- **Ownership conflict or lost ownership record:** preserve resources and investigate;
  do not delete, relabel or edit JSON to force adoption. Recovery is a deliberate
  decision, not a reason to rerun setup with a new identity.
- **Port occupied / wrong container configuration:** choose an unused `RSS_PORT`
  consistently and follow [port changes](configuration.md#changing-the-loopback-port). Apply reviewed container
  changes with `replace`; do not terminate foreign listeners or remove the data volume.
- **Homebrew upgrade approval required:** inspect private `install-preview.json`.
  This invocation has not installed anything; resolve the dependency plan explicitly.
  Partial installation additions from other attempts remain recorded.
- **Incomplete unrelated Homebrew cask:** if both fallback queries succeed, the
  warning is recorded and setup continues without repairing that cask. If either
  fails or is unusable, setup exits 1 before installation/VM initialization. Inspect
  `brew info --json=v2 --installed --cask` (installed-version JSON) and
  `brew list --cask` (one name per line) locally. Both must succeed with valid matching
  records; missing version evidence is not proof an application was removed.
- **Backup destination/export failure:** check private external directory permissions,
  free space and engine availability. Follow [failure handling](backup-restore.md#consistency-and-failure-behavior);
  never delete the VM/data as a workaround.
- **Missed/stale daily backup or maintenance hold:** run `backup-status --check`;
  follow [daily response and reconciliation](backup-restore.md#daily-check-and-failure-response).
  Do not mistake a last-archive locator, enabled job or manual backup for daily
  protection, or force supervision/start through a hold.
- **Restore target/image refusal:** use a fresh target and exact reviewed matching
  image configuration. Failed imports stay unselected; choose another fresh name.
- **Cold-start failure:** guarded recreation applies only when `start` itself
  starts a stopped owned VM and the existing app is matching, unchanged and stopped.
  A warm retry does not force recreation or bypass security/readiness errors.
  Preserve diagnostics and follow [startup safeguards](#start-verify-and-replace);
  never delete the selected volume or lock as a workaround.
- **Registry, virtualization or network failure:** restore prerequisites and retry.
  Startup failure need not stop the VM. Other machines are not stopped for you.
- **PHP/image/security mismatch:** do not enter credentials or bypass the gate.
  Review [security policy](architecture.md#image-and-php-security-policy) and current
  advisories. Loopback/container isolation is not a production security guarantee.
- **No articles / dead cron:** finish installation, check subscriptions, run `fetch`
  and inspect [logs](#service-logs-and-feed-errors). `start`/`verify` restart and recheck
  cron; cache, backoff and publisher failures still apply.
- **Private HTTPS/ReadKit failure:** follow the [private-access runbook](tailscale-readkit.md#troubleshooting).
  Preserve foreign listeners and policies; do not reset Serve or weaken TLS.
