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
it is not data deletion. For image changes, follow the
[backup, supervision-removal and recapture procedure](backup-restore.md#image-updates-and-rollback)
**before** changing the pin; an installed supervisor retains the old configuration
hash even after a successful replacement. For port changes, first follow the
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

### Upgrade installed Mac jobs safely

Both owned jobs use `AbandonProcessGroup=true`: launchd must not kill VM helpers
or consistency-critical native work merely because a short invocation exits.
This does not keep FreshRSS running against stopped intent, remove explicit VM
shutdown, or change descriptor/command-lock rules. Old installed definitions are
**not updated by checkout or dependency sync**. Keep manual operation as the
fallback until owner-assisted post-exit/reboot acceptance succeeds.

1. Coordinate with the owner in the normal GUI session. Use the **stable controlling
   checkout and original ownership directory**, with the existing port, image,
   forwarding mode and backup destination exported. Record privately whether each
   job was enabled, its captured paths/dependencies/settings, and the backup's
   intended UTC hour/minute. Disabled jobs must remain disabled.
2. Obtain a fresh `uv run --locked rss backup`; confirm `verified: true`, checksum,
   safe service resumption and no maintenance hold. Preserve the archive privately.
   Inspect `rss supervision-status` and `rss backup-status`; successful queries
   alone do not mean healthy service/protection. If busy or uncertain, stop this
   procedure and use the [backup failure response](backup-restore.md#daily-check-and-failure-response).
3. **Prefer removal with the matching old code before updating.** For each job
   previously enabled, run its removal command (`rss supervision-remove` or
   `rss backup-disable`) with those same selectors. Confirm removal succeeded;
   do not update/reinstall on partial failure. Removal preserves the running app,
   intent, ownership, evidence, archives and unrelated jobs.
4. Update/synchronize the same checkout using the separately approved revision
   (`uv sync --locked`); do not introduce another production worktree or change
   the selected volume, image, host login/security/power settings or dependencies
   merely to obtain recovery. If code was already updated, the fixed
   `supervision-remove` also recognizes **only the exact owned pre-fix definition**
   for removal, not installation/recovery. `backup-disable` validates the old
   definition against its recorded digest and loaded identity. Neither overwrites
   changed/unowned files. A still-loaded supervisor with a missing plist refuses
   installation without recreating the file: its in-memory lifetime policy may
   still be old. Use ownership-validated `supervision-remove`, then explicitly
   reinstall. A missing definition can be recreated by install only when the job
   is confirmed unloaded. Unsafe/conflicting definitions or failed native
   inspection/removal require owner resolution; never hand-edit plists/ledgers,
   clear holds/locks, or adopt a job to force progress.
5. Deliberately reinstall **only the jobs still wanted**, with the recorded settings
   and existing dependencies. Use `rss supervision-install` and/or
   `rss backup-enable --hour H --minute M`, substituting the recorded UTC values,
   not silently accepting defaults. Inspect status and capture before relying on
   them. Activation failure retains registration: inspect/retry or remove, not
   automatic overwrite. Fixed registration repetition is safe.
6. The backup definition changes policy identity. Old archives and attempt/success
   history remain, but new policy-specific daily evidence is required; recapture,
   manual backup or a manual `backup-due` is not unattended protection. Follow
   [daily backup checks](backup-restore.md#daily-check-and-failure-response).
   Coordinate the [native acceptance below](#owner-assisted-mac-acceptance-not-automated-evidence)
   before relying on automatic recovery. No extra reboot or production activation
   is authorized merely by a deterministic test pass.

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
stopped application. Separately, both installed jobs abandon their completed
process groups so launchd does not destroy legitimate surviving native work.
Abandonment grants no ownership/startup authority and does not donate the lock to
VM helpers. Independent external writers are not serialized by this lock.

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
3. A real coordinated reboot and normal login, recording changed host boot time,
   login/unlock prerequisites and automatic job execution. Do **not** manually start
   FreshRSS or invoke a recovery tick. Independently confirm the recovery invocation
   has exited, then check service availability and subsequent healthy automatic
   checks **without a container restart**. An in-job `recovered` result or zero
   parent exit is insufficient: launchd cleanup happens afterward. Confirm the same
   owned selected volume/image pin and intended private/authenticated access, then
   have the owner verify trusted browser/ReadKit availability and preserved reading
   state. Unperformed power/outage scenarios stay explicit.
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

## Time Machine recovery coordination

Follow the canonical [Time Machine copy recovery drill](backup-restore.md#time-machine-copy-recovery-drill)
for inclusion/security assessment, completed-copy retrieval into private staging,
guarded isolated restoration, authenticated full-state comparison and retention.
Do not substitute ordinary production volume selection for the namespaced drill.

Before platform work, agree with the owner:

- The actual unattended scheduled artifact and private expected-state record;
  job registration or a manual backup is insufficient. Preserve its publication
  checksum/correlation before later backups supersede latest-attempt records.
- Who will inspect/retrieve the completed copy. `tmutil` may require Full Disk
  Access/root; access denial is an unavailable check, not proof of no backups.
  Owner-run Time Machine UI/commands are an alternative to granting agent access.
- The destination's encryption, read/unlock access and recoverable key material.
  Do not publish disk/share identifiers, passwords or screenshots with private
  content. Unknown or serious unresolved protection problems remain blockers.
- Which isolated resources, free loopback port and real ownership directory will
  be used; record the working instance's selection/intent/forwarding and unrelated
  resource inventory privately. Never fabricate ownership or use a real source
  volume as the restore target. Leave production data present and unchanged.
- Explicit consent for any real-data archive and the outbound/content protections
  **before activation**: startup fetches and cron can contact publishers even when
  the clone is only reachable on loopback. If safe activation is unresolved,
  stop after validation/import and report application verification unperformed.
- A verified pre-change backup and service-resumption check before significant
  changes or any production interruption. Coordinate shared-lock contention with
  existing jobs; do not disable either job or interrupt production implicitly.
- Approval before configuration/security/permission changes, costs, mounting or
  unlocking storage, deleting real exports/staged recovery copies or cleanup.
  Retaining private artifacts is the default, not automatic pruning.

No power-loss simulation, production volume deletion, Tailscale reset or backup
history deletion is part of this drill. Record the exact revision/commands and
sanitized actual results in #24, distinguishing procedure/default-fixture passes,
real completed-copy retrieval and restored-application acceptance. All mandatory
checks must pass before #21's trial; same-host recovery proves neither physical
second-host/cross-architecture portability nor paid-feed/ReadKit acceptance.

## Safe cleanup

**VM removal destroys every remaining native guest volume/image, including the
FreshRSS database.** The following operations are intentionally separate:

| Task | Operation and boundary |
| --- | --- |
| Release memory / stop serving | `rss stop`: backed-up shutdown, retains VM/data/packages. |
| Recreate application | `rss replace`: retains selected named data volume. |
| Remove owned VM | `rss remove-vm`: preview first; apply requires fresh verified external backup and current plan digest. Retains packages, archives and audit state. |
| Delete inactive data | `rss delete-volume NAME`: separate irreversible preview/confirmation, **no automatic backup**. |
| Optional host formula removal | `rss remove-package NAME`: one proven project addition, fresh confirmation plus unused-elsewhere attestation. |
| Delete an archive | Manual exact-file double review below; never pruning or directory removal. |

Use the stable controlling checkout, existing private `RSS_STATE_DIR` and intended
port/image/forwarding settings throughout. Never copy/fabricate owner records,
relabel resources, remove `.local/`, run global Podman prune/reset, or remove shared
Homebrew dependencies to make cleanup pass. All previews use native read-only
inspection under the existing command lock; they do not stop services, create a
backup/destination or rewrite ledgers. The lock file itself is opened for writing.
An interrupted-removal receipt finalization is the narrowly scoped exception below.
Keep preview output private: it contains local identities/paths, not backup contents.

### Preview and remove the VM

1. Coordinate with the owner; retain needed logs/container-layer diagnostics and an
   independent durable recovery copy. Record selection, compatible image and wanted
   job settings privately. Remove captured jobs **with existing commands**, using
   their existing selectors, before cleanup:

   ```sh
   uv run --locked rss supervision-remove
   uv run --locked rss backup-disable
   ```

   Require both removals to succeed; never delete plists/registrations by hand.
   Removal of jobs does not stop FreshRSS. Do not run normal `stop` before the VM
   removal preview: removal needs an accessible **running** source for a fresh backup.
   If already stopped, start only the verified owned VM locally; do not start an
   empty/replacement application merely to satisfy inspection.
2. Explicitly export an absolute private `RSS_BACKUP_DIR` outside every Git checkout
   (no default), and run:

   ```sh
   uv run --locked rss remove-vm
   ```

   Review exact machine/connections, container names, selected volume, pinned image
   IDs, ownership, backup directory, retained resources and `confirmation`. Preview
   does not create the directory. An existing directory must be user-owned/private;
   apply creates a missing destination through the normal backup path.
3. Resolve every refusal without broadening scope. The owned rootless engine may
   contain only labelled app/preflight containers, **exactly the selected volume**
   and pinned image(s). Additional recovery volumes must be deliberately retained
   elsewhere or separately handled; labels alone do not make them disposable. The
   rootful engine must be completely empty, including stopped containers, volumes
   and images. Inspection failures refuse. Machine/rootless identity and the derived
   root connection are checked; foreign connections/defaults must remain unchanged.
   If a project connection is default among others, explicitly choose the intended
   retained default with native Podman tooling after owner review, then preview anew.
   Cleanup will not choose a default for you or contact unrelated engines.
4. Only after reviewing the current preview, substitute its exact digest:

   ```sh
   uv run --locked rss remove-vm --confirm '<CURRENT-PREVIEW-SHA256>'
   ```

   Apply repeats inspection, takes durable maintenance, quiesces writers and removes
   **only owned** forwarding through existing lifecycle guards. It creates a fresh
   full external archive, independently checks whole-file SHA-256 and archive content,
   then rechecks the removal plan. An older good archive or `no_data` never substitutes
   for this capture. Finally it stops the VM and runs native `machine rm` with
   confirmation stdin, never `--force`, global reset or prune. A changed plan requires
   a newly reviewed preview; no digest authorizes future changes.
5. Retain the successful `backup` result/archive and private `removed-vm.json` receipt.
   Confirm exact targets absent and foreign connections/defaults unchanged. Homebrew,
   Tailscale itself/unrelated services, archives, checkout and audit records remain.
   A repeat with VM and both owned connections fully absent reports `absent`, not a
   wider sweep. A lingering connection or partial setup is a refusal for inspection,
   not permission to delete unverified names. See [recreation below](#recreate-and-restore-after-removal).

### Failed or interrupted removal

Before deletion, failed backup/validation leaves source data and prior archives
intact; writers/owned forwarding may already be stopped while the VM remains running.
Fix the cause before a new preview/apply. Failure does not prove service is closed.
Native exports can outlive Python while retaining `command.lock`; never unlink the
lock or kill real helpers to bypass it. Interrupted or partial removal keeps durable
maintenance and receipt evidence. Inspect `rss status`, `rss backup-status`, native
machine/connection identities and the exact archive privately with the owner.
Use guarded `backup-reconcile` only when its existing owner/schema/idle-engine checks
can establish safety; never delete/override records to clear a hold.

If the process died after native rm, repeat the **same `rss remove-vm` operation**.
It may finish the receipt only when exact VM/connections are absent, its versioned
receipt matches the current owner, the matching hold is at `stopping_vm`, the recorded
external archive independently verifies and foreign connections/defaults still match.
This is receipt completion, not further deletion. Missing/malformed/mismatched evidence,
partial remaining resources or a changed foreign inventory require manual inspection;
there is no automatic adoption, record deletion or data-loss override.

**Unavailable VM:** preserve the VM disk, ownership/selection/provenance, logs and all
archives. Refuse automated removal if the source cannot be inspected/exported. With
the owner and provider-specific Podman recovery guidance, repair access or recover
from a verified external copy. If disk mounting/extraction is needed, obtain explicit
approval, preserve an independent source-disk copy first, keep the original untouched,
and establish a read-only/provider-supported recovery path; do not attach writable
SQLite storage concurrently or guess provider disk paths. The project has no mount
recovery automation. If safe access cannot be established, stop and retain evidence.
A known old archive does not authorize deleting the unavailable source.

### Recreate and restore after removal

Keep the controlling checkout, inventories/additions, backup locators/evidence and
archives. After successful deletion, `setup` retires only the exact prior generation's
owner, selected-volume/provenance, security, Serve, operator intent and supervision
recovery state into private `retired-<hash>/`, then creates fresh ownership. It preserves
the removal receipt and refuses a reappearing colliding machine/connection; never
fabricate an owner record or manually move files to force adoption.

Restore **before any empty application `start`**. Use the archive's recorded compatible
reviewed image configuration, not an assumed database downgrade or floating latest:

```sh
uv run --locked rss setup
uv run --locked rss restore '/absolute/private/ARCHIVE.zip' --volume NEW
uv run --locked rss select-volume NEW
uv run --locked rss start
```

Replace `NEW` with a fresh valid name, not the old selected/original name. The initial
`rss-aggregator-data` selection is refused as a restore target even when absent.
Confirm that the old original volume is absent, not silently recreated. Restore
starts only the owned VM as needed, imports without activation and
leaves the VM running. Check `restored: true`, then explicitly select the complete
volume and start. Follow [full-state recovery](backup-restore.md#recovery-on-this-or-a-clean-host)
for matching first activation, host-specific settings and trusted archive handling.
Independently verify fresh login, original user/settings/subscriptions, representative
article content and both read/unread and starred/unstarred state. HTTP readiness or
empty infrastructure is not recovery acceptance. Re-enable only explicitly wanted
jobs after successful application/private-access verification with fresh captures.
Retain the archive and retired audit evidence.

Use the [separately selected native drill](development.md#cleanup-verification-and-remaining-native-evidence)
to verify disposable VM remove/recreate/authenticated recovery and retain
revision-specific outcomes in the issue/PR. Earlier source-absent recovery and
default deterministic tests do not prove this whole path. The native drill compares
retained machine names and connection/default inventories, but does not uninstall
host packages or fully inspect every unrelated resource.

### Permanently delete an inactive volume

This is **irreversible application-data deletion**, not environment cleanup. It makes
**no automatic backup**. With owner approval, first retain and independently verify
any recovery copy needed. Run `rss delete-volume NAME`, inspect its exact volume,
label ownership, `irreversible`, `backup_created: false`, preserved resources and
`confirmation`; only then run `rss delete-volume NAME --confirm '<CURRENT-PREVIEW-SHA256>'`.
The running owned VM must show an **unselected** labelled volume, zero `MountCount`
and no attachments, including stopped containers. Selected/mounted/attached/foreign
or changed volumes refuse; do not detach or change selection merely to bypass review.
Native `volume rm` is nonforce. Only that target (and its provenance entry) is removed;
containers, VM, all other volumes, packages and archives remain. Already absent reports
absence, not cleanup of another target.

### Optionally remove project-added formulae

Package removal is never automatic after VM deletion. Preserve initial
`host-before.json` and cumulative `host-additions.json`; an installation delta is
not proof of continued exclusive use. Before each candidate, confirm with the owner
that no other project/manual tool has adopted it. Unknown external usage means retain.

1. Remove the owned VM first. **Any** remaining Podman machine or connection, including
   foreign/stopped ones, blocks package removal; preserve them, never delete them to
   obtain eligibility. Remove Podman first only if it is an unchanged recorded addition
   and unused elsewhere: `rss remove-package podman`.
2. Read the one-formula preview's `targets`, ownership/version evidence, current
   `dependents`, `retain` reasons and `confirmation`. A successful preview with retention
   reasons is not uninstall approval. Baseline/pre-existing, unknown or upgraded
   packages stay. Fresh recursive reverse-dependency inspection covers installed
   formulae **and casks**, including optional/build/test dependencies. Query failures
   and unverified Podman absence refuse.
3. Only when eligible and genuinely unused elsewhere, run
   `rss remove-package podman --unused-elsewhere --confirm '<CURRENT-PREVIEW-SHA256>'`.
   Both assertions are required. Apply rechecks native inventories/versions/identity
   before normal `brew uninstall --formula`, with native dependent checks **enabled**.
   There is no force, ignore-dependencies, autoremove or cleanup; native refusal is
   not a reason to bypass dependencies. It does not reverse historical upgrades.
4. Preview each remaining ledger addition **again**, one plain formula name at a time,
   after Podman removal. Attest/confirm each separately only if no current consumer
   needs it. Do not turn the ledger into an uninstall loop. Already absent reports
   absence. Preserve ledgers/evidence and all other packages/resources/backups.

### Residual files and manual archive deletion

There is no automatic cache/configuration/directory sweep. `.venv` and uv caches are
tooling, not FreshRSS data; stop/removal do not clear them. Retain sensitive audits,
retired generations, backup locators and external archives privately. Review residuals
locally one **exact owned path** at a time against ownership evidence and current
consumers; uncertainty means retain. No broad `rm`, recursive cleanup of `.local/`,
Homebrew directories, Podman storage or backup directories is authorized.

Archive deletion is a **separate manual exact-file double-review confirmation**:

1. Obtain owner approval for the specific archive; select its absolute regular-file
   path locally. Review parent directory, symlinks, basename, size/date and checksum
   against private publication evidence. Do not choose a glob, directory or newest/oldest
   shortcut. Archive contents include credentials/private feed URLs.
2. Independently review the exact target a second time with the owner. Verify any
   required retained recovery copy by checksum/archive validation and recovery evidence;
   confirm this is not the only needed recovery point. Unknown copy status means retain.
3. Obtain explicit final confirmation naming **that exact file** after both reviews.
   Use a local file manager's single-file deletion, with no multi-select/recursive
   operation. Do not provide a shell one-liner or remove its parent directory. Deletion
   of a staged copy, independent copy or trash contents needs its own review/approval;
   this procedure is not secure-erasure proof.
4. Confirm only that file is gone and retained archives remain. Keep a private minimal
   audit of consent/identity/outcome, not database contents or credentials. No automatic
   archive pruning, record sweep, encryption or upload is implied.

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
- **HTTP ready but fresh login fails:** a 200 response can contain FreshRSS's
  `Error during context user init!`. Readiness is not authenticated acceptance.
  Compare source/clone effective default-user consistency against existing readable
  user configurations without publishing names or changing credentials. Preserve
  backups and seek owner approval before any correction; see the
  [isolated recovery diagnostic safeguards](backup-restore.md#4-restore-and-deliberately-activate-only-isolated-owned-resources).
  For a separately approved **production** default-user correction, first obtain
  a fresh verified `rss backup` and confirm resumption. Identify the existing
  readable user locally, serialize upstream `cli/reconfigure.php --default-user`
  under the real command lock, and retain the matching image/port/forwarding/job
  settings. Never create a replacement user or reset credentials. Check that the
  existing user configuration is unchanged and that all other effective system
  settings remain identical. Use normal guarded `rss start` with the original
  captured settings, then check fresh-session login and have the owner authenticate
  at the normal trusted production URL. Confirm the original container/data-volume
  identity, intent and private forwarding, and the refreshed production security
  audit. Keep the pre-repair archive; record its known defective setting instead
  of treating verified archive bytes as proof of usable application configuration.
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
