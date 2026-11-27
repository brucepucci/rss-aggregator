# Operating the native Mac instance

Use the [README](../README.md#getting-started) for a new local instance, the
[CLI reference](cli.md) for syntax/results and [configuration](configuration.md)
for settings. The one-time Podman migration below is an owner-assisted procedure,
not a second supported runtime.

## Installation and ownership

Run a native arm64 shell on Apple Silicon macOS 12 or later as a normal user.
`uv sync --locked` installs the checkout's Python environment. `uv run --locked
rss setup` installs or verifies the exact [reviewed manifest](../config/native.json):
FreshRSS 1.30.4 at `08f97bd497ace733c151ea1c709d85cd5b65cc53`, FrankenPHP 1.14.0,
PHP 8.6.0 and Caddy 2.11.9. It verifies downloaded SHA-256 digests, runtime versions
and SQLite support; it does not start the service or install jobs. There are no
project-installed global PHP, Caddy, Podman, Homebrew or privileged services.
Do not substitute a nearby release, global packages or a checksum override to
work around a refused install.

Default state is `<checkout>/.local/native`. Keep that directory private (`0700`),
owned by the controlling user, and on local, non-synced macOS storage for SQLite.
It holds the managed runtime, `instances/NAME/data`, service settings, locks and
ownership/recovery records. The parent legacy `.local` still belongs to the old
deployment; do not repurpose it or remove it recursively. A private directory is
not a sandbox. Keep one stable controlling checkout. If you must use another
checkout to operate the **same** instance, use the exact matching reviewed pin and
explicitly set the original `RSS_STATE_DIR`; installed jobs still use their
captured checkout. Never copy or fabricate receipts or adopt an unowned runtime.
For another instance rehearsal or recovery drill, use **new** private state, an
unused and unforwarded loopback port and a separate explicit backup destination.
Check for existing Serve, Funnel or other forwarding to the port before starting
an installer or rehearsal, even with `RSS_TAILSCALE=0`.

## Start and verify

Use the owning checkout and consistent `RSS_PORT`, `RSS_TAILSCALE`, `RSS_STATE_DIR`
and `RSS_BACKUP_DIR`. Commands below use `uv run --locked rss`; `./scripts/rss` is
an equivalent launcher.

- `start` records running intent after the maintenance guard. Startup checks
  ownership and exact installed pin, prepares the selected data, checks effective
  **web** PHP (not only PHP CLI), readiness and scheduling, and verifies rejection
  of anonymous API access on an initialized instance before opening private
  forwarding. It removes the temporary PHP probe after each check. Repeated
  `start` rechecks effective settings and anonymous API refusal before retaining
  an initialized service. Installer completion, reconfiguration or a failed
  health check can trigger a guarded restart. Use `status`
  for routine health checks.
- On first start finish the installer locally with SQLite and form authentication
  before enabling forwarding. Do not enter credentials if a security check fails.
- `status` inspects the selected instance, HTTP readiness, scheduler and forwarding
  without starting it. `verify` requires the owned service running and restarts it
  through its current serving mode to recheck the gates; it does not change intent.
  If preflight confirms the owned backend is publicly reachable through Funnel,
  it attempts to stop that backend and reports whether shutdown was confirmed.
  It does not change the foreign route. A readiness result is not proof of
  authenticated browser/reader use.
- Watch for a port conflict, unexpected forwarding or a warning that writer shutdown
  could not be confirmed. Preserve other listeners/routes. Never infer that a failed
  command closed access; inspect service and forwarding locally before retrying.
  See [private-access failures](tailscale-readkit.md#troubleshooting).

## Scheduling and reading state

Once configured, FreshRSS performs an initial fetch on startup and requests feed
fetching at UTC minutes **00, 20 and 40** while running. Sleep and missed slots are
not a queued backlog. `fetch` requests immediate upstream fetching; TTL, publisher
backoff and caches still apply. A completed run does not guarantee new articles.
Startup (including a restart via `verify` or backup resumption) and manual
`fetch` allow up to **10 minutes** for the complete foreground fetch. A timeout
fails the operation without emitting upstream output; inspect logs and slow feeds
locally. On a restart timeout, serving may remain stopped; check `status` and, if
resuming a backup, `backup-status` before retrying.
The scheduler and manual fetch serialize to avoid overlapping fetches. When the
instance is stopped, neither the service nor its feed scheduler runs. Reader
synchronization does not fetch feeds. Archiving policies can remove older articles;
check the user/category/feed settings before changing them.

## Backups and stopping

Export an explicit private `RSS_BACKUP_DIR` outside Git. `stop` records stopped
intent, removes only owned forwarding, and proves web and fetch writers idle
before reading the selected instance, backup attempt or runtime layout for an
archive. Missing previously provisioned data or a lost explicit selection is a
failure, not an empty install. The command preserves the runtime, data and
archives. A failed shutdown export after quiescing leaves writers stopped;
correct the cause and retry `stop`. Use the [backup procedure](backup-restore.md#consistency-and-failure-behavior)
for resumption, daily checks and interrupted maintenance; a failed explicit
`backup` has different resumption behavior. A local archive alone does not prove
host-loss recovery.

## Opt-in Mac startup and recovery

### Enable or remove supervision

**Obtain owner approval and a verified private backup** before real job
installation. Choose a stable synchronized checkout, preserve its `.venv`, select
the original native state and export the reviewed settings. Finish the local
installer and verify the intended serving mode first. Do not alter login,
FileVault, sleep or power policy. From the selected checkout run:

```sh
uv run --locked rss supervision-install
uv run --locked rss supervision-status
# To opt out independently of daily backups:
uv run --locked rss supervision-remove
```

Supervision and [daily backups](backup-restore.md#opt-in-daily-mac-backups) use
separate opt-in per-state labels in the user's GUI launchd domain. The jobs capture
absolute checkout, Python, state and approved non-secret settings; they do not
load the interactive shell or `.env`. Neither `setup` nor `start` installs jobs.
Only the unchanged owned registration is removable; refusal or partial activation
requires inspection, not plist/receipt edits. Removal does not stop FreshRSS,
change intent or delete data/archives. An intentionally stopped or unset instance
is not eligible for automatic start. Only `start` enables running intent; `stop`
records stopped intent even when a later step fails, once the command lock is held.
Temporary maintenance/verification does not change it.

### Upgrade installed Mac jobs safely

Checkout/dependency updates do not update an installed definition. With owner
approval, record which jobs are installed and their captured selectors, destination
and UTC backup schedule. Obtain a verified backup with safe resumption, then remove
both owned jobs using matching code/settings (`supervision-remove`, `backup-disable`).
Stop on a busy, partial or uncertain result; never overwrite a changed or foreign
job. Only then apply the separately approved checkout, port, forwarding or runtime
change. Verify service/security and intended access; explicitly recapture only the
jobs still wanted with the preserved schedule and intended selectors. A newly
captured backup policy needs fresh daily evidence. Manual-only use stays disabled.
This is not the procedure for recapturing a legacy job as native; use the
[migration runbook](#one-time-migration-from-podman).

### Daily check and failure response

Use `supervision-status` for captured registration, intent, observed health and
recovery outcomes. The job requests a check at load and every 60 seconds in an
available normal user session. Healthy checks do not restart the service;
unhealthy recovery uses the usual safety gates, with at most three consecutive
attempts and waits of at least 60 then 300 seconds before attempts two and three.
This is policy, not measured availability. Check session/dependencies if a job
is unavailable. A maintenance hold blocks automatic recovery. If the
service must stay stopped, record stopped intent before [reconciling](backup-restore.md#interrupted-maintenance-and-reconciliation).
After correcting a failed/exhausted recovery, explicitly `start` if running is
wanted. Check daily protection separately with
[`backup-status --check`](backup-restore.md#daily-check-and-failure-response).

### Owner-assisted Mac acceptance (not automated evidence)

After approval, use disposable native state and jobs to verify real normal
login/unlock, healthy ticks without interruption, guarded recovery and service
availability **after** the job invocation exits. In a separate check, explicit
stop must persist across tick and reboot/login. Observe actual scheduled fetching
and an unattended daily archive, then restore that archive privately. Preserve
unrelated jobs/routes and only report sanitized timing and outcomes. No pre-login,
power-loss or sleep behavior is implied. The explicitly selected synthetic check,
`uv run --locked pytest -m live tests/integration/test_native.py`, is not launchd,
real data or client acceptance; see the [developer verification guide](development.md).

## Safe cleanup

Uninstall removes only the verified owned runtime; data, archives and private
records remain. It does not stop unrelated software, delete jobs on your behalf,
or sweep dependencies. Use the owning checkout and selectors throughout. With
owner approval for an installed service:

1. Verify a current private instance backup and preserve an independent copy.
   Remove supervision and disable daily backup, if installed, before changing the
   runtime. Review both statuses; stop on any refusal or uncertainty.
2. Run `uv run --locked rss stop`. Confirm stopped intent, idle writers, verified
   shutdown archive when data exist, no unresolved maintenance and no active
   project forwarding. Do not move past an unverified shutdown.
3. Run `uv run --locked rss uninstall` and review the current runtime path,
   installed identity and preservation plan. Only if it is the intended change,
   run `uv run --locked rss uninstall --confirm TOKEN`, substituting the exact
   returned `confirmation`. A stale/mismatching preview is not authorization;
   inspect and preview again. On partial removal preserve records and inspect.

To reinstall runtime files against preserved state, use `setup` with the same
reviewed pin and verify the selection before starting. To recover into new state,
use [fresh-target restore](backup-restore.md#recovery-on-this-or-a-clean-host).
Permanent removal of data, records or archives is a **separate** explicit
owner-reviewed action. Preserve needed archives and evidence, review exact paths,
and never delete `.local`, a whole backup directory, or lock/hold/ownership files
as a shortcut. No native command removes the legacy VM.

### Upgrade the pinned runtime

Before changing checkout pin or version, obtain a verified native backup while
still on the old pin and keep its matching checkout and independent copy. Disable
both jobs, stop with a verified backup, and inspect for an unresolved hold or
active forwarding as above. Use only an explicitly reviewed target manifest;
`setup`, `start` and a new checkout do not silently upgrade.

`upgrade` supports a PHP/web-runtime change only: FreshRSS release, source digest
and platform must stay the same. From the reviewed target checkout with the same
state, run `uv run --locked rss upgrade`. Inspect installed/target identity and
preservation plan, then apply only the current preview with `uv run --locked rss
upgrade --confirm TOKEN`. With instance data present, apply requires the selected
instance's verified shutdown archive covering its last writer generation and
revalidates it against the installed pin. An older archive does not cover writes
from a later session. If shutdown capture failed, correct the cause and retry
`stop` before applying. It does not rewrite old archives: each
ZIP still needs its exact recorded pin for restore. Start with `RSS_TAILSCALE=0`,
verify application state under the reviewed runtime and take a new native backup;
only then restore [intended private access](tailscale-readkit.md#connect-and-verify-the-server)
and explicitly recapture wanted jobs.

Changing FreshRSS itself is not supported by this command. Never hand-edit
compatibility metadata or assume an old release can read newer data. If activation
refuses, stop and preserve the data and records. Restoring the pre-change archive
into fresh state with the old reviewed pin is recovery, not a downgrade of live data.

## One-time migration from Podman

[Issue #30 and owner handoff](https://github.com/brucepucci/rss-aggregator/issues/30#issuecomment-7209555964)
authorize synthetic local verification without jobs or production data. **No real
archive rehearsal, job installation, production writes, Serve switch, ReadKit
changes, Time Machine work or VM removal without separate owner coordination.**
Keep the legacy instance, archives and checkout available. Keep the pinned
[legacy tests and run instructions](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/development.md#explicit-liveplatform-checks)
and the [matching legacy backup procedure](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/backup-restore.md#create-a-backup).
There is no permanent dual backend or legacy restore on the native branch.

### Rehearse with isolated native state

1. With owner approval, obtain a trusted, verified **legacy v1** backup from the
   pinned legacy revision. Record its checksum privately, keep the original and
   work from a private copy. Agree how to prevent unauthorized outbound fetching
   from a real-data clone before activating it: startup fetches even on loopback.
2. In the native checkout choose a **fresh** private local `RSS_STATE_DIR`, a
   different free `RSS_PORT` and a separate private `RSS_BACKUP_DIR` outside Git.
   Set `RSS_TAILSCALE=0`. Inspect existing Serve/Funnel routes and other proxies
   locally: **the port must not be the target of any known forwarding**, even
   when no local listener is running. Choose another port instead of changing
   someone else's route. Never use production job definitions/selectors. Leave
   all jobs uninstalled for local handoff; any owner-approved disposable job
   trial uses the isolated state's own labels.
3. Run the following only with those isolated selectors. Use a fresh name and
   the private path to the verified archive; do not start an empty default first.

   ```sh
   uv run --locked rss setup &&
     uv run --locked rss import-legacy "$LEGACY_ARCHIVE" --instance rehearsal &&
     uv run --locked rss select-instance rehearsal &&
     uv run --locked rss start &&
     uv run --locked rss status
   ```

   `import-legacy` is a hidden, one-time migration command. It validates the
   trusted legacy archive against the recorded legacy image and same FreshRSS
   release, safely maps files to the Mac user and refuses unsafe/colliding paths,
   existing targets and overwrites. It never writes to the legacy source or
   automatically activates the import. Any failed check blocks activation.
4. Use the returned loopback origin and a fresh browser login to verify users,
   subscriptions, representative article content and both positive/negative
   read/star markings. Keep identities/content private. Check repeatable native
   installation and complete native backup/restore into another fresh target
   with the exact native pin. Actual scheduled fetching, login recovery, stopped
   intent and unattended daily archive recovery are distinct owner-assisted
   checks, not established by a synthetic integration pass. Stop the rehearsal
   when done and preserve its evidence privately.

### Cut over only in an approved maintenance window

1. Record the expected private origin, existing owned route, old job settings and
   preservation inventory. Disable legacy supervision and daily backup using
   matching legacy code. Stop old writers; use the verified final legacy shutdown
   archive or the guarded legacy backup procedure with writers kept stopped.
   Confirm old service/forwarding is stopped, the VM is idle/stopped and automatic
   restart is prevented. Preserve the source, final archive and its checksum.
2. Import the **final** legacy archive afresh, not the rehearsal database. Use
   fresh native state and the rehearsed procedure on an isolated loopback port;
   locally check authenticated state, pin and security before publishing anything.
   Failed pre-switch checks do not authorize switching or changing foreign routes.
3. Only on owner approval, activate the same existing private HTTPS origin using
   [guarded private access](tailscale-readkit.md#connect-and-verify-the-server).
   Do not reset Serve or adopt foreign routes; confirm the legacy owned route was
   cleanly withdrawn before enabling native forwarding. Verify trusted browser
   access and both actual ReadKit clients, including changes in both directions.
   Confirm the legacy VM is stopped/intact and not required by native jobs. Agree
   independently whether to recapture either native job after checks pass.
4. Keep legacy intact. Before native accepts writes, any return to legacy requires
   explicit owner coordination, native stopped, and only the intended old route
   restored by its owner. Once native accepts live writes, legacy is **not current**.
   There is no reverse import, dual-writing or supported rollback to Podman; use
   the native backup/recovery procedure and coordinate any service decision.

### Legacy retirement gate

Do not remove the VM or retire the importer until live acceptance and
[independent Time Machine restore](backup-restore.md#time-machine-copy-recovery-drill)
of an unattended native archive have passed. Historical issue #24 evidence is
for the old runtime. Record sanitized outcomes and any outstanding gates in the
owner's migration handoff, regardless of the implementation issue's status.
Final removal needs **separate owner approval** using the
[pinned guarded legacy removal runbook](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/operations.md#preview-and-remove-the-vm)
and its existing ownership/inventory/fresh-archive prerequisites. The gate does
not waive those checks. If there is any uncertainty about exclusivity, sources,
unrelated contents or current backup, keep the VM stopped and intact; never run
a global prune/reset or a raw machine deletion. Retire the importer separately
after the owner gates pass.

## Service logs and feed errors

Start with `status`, stderr, FreshRSS **Configuration → Logs** and the affected
subscription. Persisted FreshRSS user logs are under `instances/NAME/data/users/`
(for example `<FRESHRSS-USER>/log.txt`, with general/API/WebSub logs under `_`).
These and SQLite journals/sidecars that are present inside `data` are retained
with the complete payload; do not delete them to diagnose or sanitize a restore.
Logs may not record successful fetches. CLI/scheduler results are summaries, not
a raw response dump or a guaranteed durable HTTP/PHP process journal. Inspect
private service/backup/recovery records locally; operational records outside
`data` remain local, not part of an instance snapshot. No `rss logs` command.
Never share raw logs, URLs, tokens, database files or copied journals.

## Common troubleshooting

- **Setup/pin drift:** preserve receipts and paths. Confirm native arm64 execution,
  reviewed manifest, expected permissions and available storage; do not substitute
  host software or delete a record to adopt a path.
- **Port/forwarding ownership:** inspect the port and route locally, including
  known proxy targets. Preserve unrelated software. Follow the reviewed port
  change and job recapture procedures rather than forcing a bind.
- **Lost or unreadable selection:** `stop` still attempts to quiesce owned
  resources but refuses a backup until the intended instance is known. Preserve
  data and records, confirm which complete instance is intended, and run
  `rss select-instance NAME` while idle with no maintenance hold. Retry `stop`
  for a verified archive before restarting. Do not remove the selection guard
  or use `default` as a substitute for a missing reader.
- **Writer or backup hold:** confirm idle state via the supported
  [reconciliation](backup-restore.md#interrupted-maintenance-and-reconciliation).
  Do not unlink lock files, clear holds or kill unidentified processes.
- **HTTP ready but login fails:** verify the intended instance, exact pin, local
  browser origin and credentials. Review logs locally. An installer on a recovered
  clone, missing article state or an unexpected redirect is a failed check; do not
  create a replacement user or reset the source password to make it pass.
