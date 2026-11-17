# Instance backup and recovery

Canonical procedure for complete instance backup, restore and host migration,
implementing [ADR 0002](adr/0002-portable-state-with-consistent-external-backups.md).
See the [CLI reference](cli.md) for exact arguments/results,
[configuration](configuration.md) for settings, and
[verification limits](development.md#verification-and-adoption-status) for tested scope.
The supported destination is a compatible Apple Silicon Mac; native Linux,
cross-architecture and physical second-host recovery are not verified.

## Scope and trust

The payload is the entire selected FreshRSS `data` volume, including databases,
users, configuration, subscriptions, articles, reading state, cache and logs.
Extensions are not used initially. Code installed outside `data` would need a
future explicit recovery contract; it is not silently included here.

Archives contain secrets. Keep them outside Git with private permissions and
transport/store them as credentials. Permissions are not encryption. Integrity
checks are not signatures: restore only archives you trust. Neither OPML nor the
VM-native volume itself is an instance backup. The project does not prune archives,
upload to a cloud service, or delete a VM/volume.

## Create a backup

Choose an absolute private directory outside **every Git checkout**. New backup
directories are created with mode `0700`; existing directories must already be
private and owned by you. Archives use `0600`. The destination filesystem must
support hard links for atomic non-overwriting publication. Permissions are not
encryption; keep independent copies on durable storage. A copy on the same Mac
cannot protect against losing that Mac.

```sh
# Explicitly approved for this deployment, not a global default:
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
uv run --locked rss backup
# Or back up as part of normal shutdown:
uv run --locked rss stop
```

Success returns the archive path, whole-ZIP SHA-256 and verified manifest as JSON.
Inspect `backup-status` and private `last-backup.json` if resumption fails after
publication; archive success and resumption are separate.
Do not treat an interrupted unpublished file as a completed archive. Nothing
prunes, overwrites or uploads prior backups. See [failure behavior](#consistency-and-failure-behavior)
before assuming a failed operation left the service stopped.

## Opt-in daily Mac backups

**Owner-coordinated operational change:** obtain a verified instance backup before
production activation. Confirm the stable controlling checkout, original ownership
directory, destination, normal Mac user/session and dependencies. Do not exercise
production or change login, power or security settings merely to test scheduling.
The approved production destination is explicitly `~/FreshRSS-Backups`; disposable
tests use separate private directories. Existing unsafe permissions are refused,
not silently repaired. After coordination, from the synchronized checkout:

```sh
export RSS_STATE_DIR="/absolute/private/existing/ownership-directory"
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
# Also retain the instance's consistent RSS_PORT and RSS_TAILSCALE settings.
uv run --locked rss backup-enable --hour 3 --minute 0
uv run --locked rss backup-status --check
```

The initial check fails until eligible daily work succeeds: installation is not
protection. Enable explicitly bootstraps only the private per-user
`~/Library/LaunchAgents/io.rss-aggregator.backup.plist` in the normal user's GUI
session. It captures absolute checkout, `.venv/bin/python`, original ownership
path, destination, port, forwarding setting and a dependency-only executable PATH.
The job uses no interactive shell, uv, `.env`, aliases or package installation.
Keep the selected checkout/environment available. Identical enable is repeatable;
settings changes require disable then enable. Changed policies/destinations require
new daily evidence; old successes cannot suppress their first eligible capture.
The worker validates the exact owned definition and loaded path/program/arguments/
captured environment before reserving a daily attempt or touching runtime resources.
Missing, conflicting, unowned or changed registration is refused, never adopted.
Unexpected launchd inspection errors fail closed during enable, disable and work:
registration is retained for inspection/retry, not treated as confirmed absence.
Both Mac jobs set `AbandonProcessGroup=true` so launchd's parent-exit cleanup does
not interrupt native export/import leases or destroy safe VM resumption. Explicit
shutdown still stops the VM, including restoring an originally stopped VM after
backup; stopped application intent remains authoritative. For already-installed
jobs, use the [safe removal/update/recapture procedure](operations.md#upgrade-installed-mac-jobs-safely),
retaining independent opt-in, destination and intended UTC policy. Recapture changes
backup policy identity and requires fresh daily evidence, not deletion of history.

Policy is **03:00 UTC daily** by default, adjustable with `--hour`/`--minute`, not
local daylight-saving time. launchd wakes every **15 minutes** and at load. Actual
failures retry no sooner than 15 minutes, at most **three attempts per UTC target**,
then wait for the next day's target; manual `backup` remains available. Contention
records a separate defer and retries on a later wakeup, without consuming the
actual-attempt budget or building an overlapping job backlog.

Sleep, logout/unavailable sessions, unavailable code/dependencies or host loss
prevent guarantees. No wake-from-sleep or pre-login service is installed. After
today's target, a wakeup catches up once for today's target, not every missed day;
before today's target it waits, reporting stale history instead of backfilling.
No archive pruning, transfer, encryption or Time Machine configuration is performed.

### Daily check and failure response

```sh
uv run --locked rss backup-status --check
```

Inspect `enabled`, `registered`, `scheduler_problem`, `daily_attempt`,
`next_expected_attempt`, `daily_due`, `missed`, `latest_defer`, `latest_attempt`,
`latest_success`, `age_seconds`, `overdue`, `archive_available`, `resumption` and
`maintenance_required`. `--check` emits the JSON and exits 1 unless `protected` is
true; ordinary status exits 0 when inspection succeeds even if unhealthy. Status
is available read-only while busy. A recovery point becomes stale after **26
hours** (daily cadence plus a two-hour reporting allowance, not extra promised
protection). A target with no attempt more than 15 minutes after due is `missed`.

`protected` requires a recent successful eligible daily operation, an available
verified archive for the selected volume, registration/dependencies and no due,
maintenance or latest-attempt failure condition. Manual/stop-time backups cannot
substitute for daily evidence. Repeating an already-stopped shutdown creates no
new capture and preserves the prior effective attempt, including a failure; it
neither invalidates otherwise valid protection nor repairs failed protection or
refreshes archive age. Status does not rehash archives on every check.
Manual `backup-due` can exercise eligibility but **does not prove an actual
unattended launchd run**.

- Destination/export/integrity/write failure: preserve source and prior archives;
  inspect private directory permissions, free space and local engine availability.
  Share only sanitized outcomes, never raw engine diagnostics or archived content.
- Missing checkout/Python/dependencies/registration: restore selected paths; if
  needed run `uv sync --locked` in the selected checkout. For deliberate changes,
  disable and re-enable the exact owned job. Do not copy ownership records or
  overwrite another job to force adoption.
- Contention: inspect the ongoing command; defer is not success. A stuck native
  subprocess can still hold the inherited lock. Do not remove locks/holds or kill
  production processes without owner coordination.
- Verified archive but failed resumption: preserve the archive and inspect the
  maintenance hold; use reconciliation below before explicit restart.

Opt out without deleting data, evidence or archives:

```sh
uv run --locked rss backup-disable
```

Disable uses the same nonblocking lock: it does not interrupt an active backup,
and refuses changed/unowned definitions. Manual and normal-stop backups remain.
Record a real unattended mini run after owner-approved activation, with sanitized
scheduled/attempt/completion times, archive verification/checksum and status.
Independently observe safe application/VM/forwarding state **after the invocation
exits**, not only its in-job resumption result. Assess interrupted native-work
lifetime through disposable tests, not production fault injection.
Registration, fake clocks or a manually invoked job are not that evidence.

### Interrupted maintenance and reconciliation

A durable hold precedes backup VM/writer mutations and remains through capture
and resumption. Short-lived native subprocesses inherit the project lock: killing
a parent cannot let supervision restart writers while its export survives.
Only `podman machine start` omits inheritance to long-lived VM helpers; the parent
still holds the lock through startup. Exports/imports retain inheritance. Any
`maintenance.json` presence, including malformed data or dangling links, blocks
managed writer operations and supervision.
Temporary maintenance never changes persisted operator intent; resumption refuses
persisted stopped intent. Inspect `backup-status` and local process/engine state
with the owner. If the app must remain stopped, run `rss stop` **before**
reconciliation: after acquiring the project lock, it persists stopped intent even
when maintenance refuses shutdown. A busy-lock refusal cannot record a new intent;
wait for the active operation/native subprocess to finish and retry `stop` before
reconciling. Never remove the lock to force this change.
Then:

```sh
# Run rss stop first if stopped intent must persist after release.
uv run --locked rss backup-reconcile
uv run --locked rss backup-status
# Only after inspection, if running is intended:
# uv run --locked rss start
```

Reconciliation acquires the lock, validates marker owner/schema and the real
VM/connection, safely stops only owned writers/forwarding and verifies both engines
idle. Foreign workloads, uncertain forwarding or a surviving locked export cause
refusal. It never starts a stopped VM, resumes the app or changes operator intent.
Success clears the hold but does not itself start anything. When supervision
is separately enabled, a later tick may honor persisted running intent after this
confirmed-idle release. Explicit stop before reconciliation instead keeps later
ticks stopped. Corrupt or unrecognized records require deliberate
inspection, not hand-edited JSON.

Hidden `.rss-export-*`/`.rss-backup-*` remnants can survive abrupt death. They are
not completed archives and are never automatically promoted or pruned. A final
archive linked just before death may be valid but absent from success evidence;
after reconciliation, validate a trusted copy through restore into a fresh owned
volume before relying on it. Never rename a temporary file into a backup. Cleanup
of sensitive remnants is a separate owner-approved action after confirming idle.

A daily interval accepts roughly **24 hours of reading-state loss** under normal
successful operation, not during failures. Daily local backup alone does not prove
host-loss protection. #24 separately verifies actual Time Machine inclusion,
access/encryption assessment, retrieval and isolated full-instance recovery of a
scheduled copy. Local publication and private permissions are not that evidence.

## Format version 1

The outer file is a standard ZIP, with two uncompressed, unencrypted members:

- `manifest.json`: bounded UTF-8 JSON with `format: "rss-aggregator-backup"`,
  integer `format_version: 1`, UTC `created_at`, the exact checked-in `image`
  configuration, an `includes` description, and `payload.size` / `payload.sha256`.
- `data.tar`: the full volume export, including ordinary file/directory modes and
  numeric ownership required by the official FreshRSS image.

Use standard ZIP and tar tools to inspect **trusted** archives privately. Do not
extract an untrusted tar manually. Recovery validates the outer structure, CRCs,
payload size/hash, format and exact configured image before changing the target.
It streams the payload to private scratch space; database contents are not loaded
into RAM. Sufficient disk space is needed for export, the ZIP, and restore scratch
files; failure preserves previous good archives and original data.

Recovery admits only ordinary nonsparse files/directories with safe relative POSIX
names. Absolute/traversing/backslash paths, duplicate normalized names, file/child
conflicts, symlinks/hardlinks, devices, FIFOs and sparse files are not accepted.
It rebuilds an ordinary tar with numeric owners and permission
bits, removing special mode bits and untrusted extended metadata before import.
No archive member is extracted onto the host filesystem by its own pathname.

To bound validation memory, normalized paths may have at most **64 non-root
components** (the existing 4,096 UTF-8 byte pathname limit still applies). Before
constructing full ancestor strings, validation charges every normalized member
name and each ancestor **4 × UTF-8 byte length + 256 bytes** against an **8 MiB
(8,388,608 byte) cumulative path-metadata budget**. Shared ancestors are charged
again for each member; even empty/shallow members consume the budget. Backup
creation and restore both reject excessive metadata, without a file-payload byte
cap or silently excluding files. Legitimate state beyond these limits requires
reviewing the limit before changing code; a failed backup leaves previous archives
and source data intact.

The manifest's image fields are required to equal the reviewed configuration in
`config/image.json`. This deliberately conservative compatibility rule avoids
silently restoring with a different release/platform/security policy. Archive
checks do not authorize running a known-vulnerable image. Reassess advisories and
retain the existing effective Apache PHP gate. The image's arm64 platform manifest
is not a portable executable for an x86 machine.

A restored volume remains pending first activation even after selection or backup.
`start` and `replace` recheck its recorded image before writing templates or starting
it; `replace` also checks before stopping/removing the old container. A mismatching
pin is refused without overwriting provenance. Only a completed start through
security, readiness, scheduler and initial-fetch checks records `activated: true`.
Failed starts and snapshots leave this guard pending. Legacy provenance records
without this marker conservatively require one matching start before any image
update. The marker is managed state, not a user-configurable override.

## Consistency and failure behavior

The normal project lock covers backup, restore and explicit volume selection as
well as lifecycle/supervision commands. Short-lived native subprocesses, including
exports/imports, inherit the held lock, so an orphaned export retains serialization
after its Python parent is killed. Only the VM launcher (`podman machine start`)
in startup and Recovery omits inheritance to long-lived helpers; its Python parent
retains the lock throughout the launcher call. This does not exempt capture or
import work from serialization. Supervision also refuses a durable maintenance
hold; never delete one to bypass a consistency window. See
[session recovery response](operations.md#daily-check-and-failure-response).
Both rootless and rootful engine states are inspected
through the verified machine/connection before stopping anything. Foreign running
workloads cause refusal. All owned application and preflight containers stop, exit
is verified, and the engines must be idle before export. This stops both HTTP writes
and cron fetching. Independent processes bypassing project commands are outside the
lock: do not mount or write these volumes independently during a backup.

Exports and ZIP verification use private temporary files. A final unique archive
name is published atomically only after read-back verification. The destination
filesystem must support hard links for non-overwriting atomic publication; otherwise
backup fails safely without publishing an archive. Existing archives are never replaced. The result reports an SHA-256 of the whole ZIP, and private
`last-backup.json` records its location/checksum for local diagnosis. An error
after publication (for example, failed application resumption) does not erase the
valid archive and still returns nonzero.

Explicit backup attempts to preserve the earlier application/forwarding/VM state
where safe, including after snapshot failure. It never resumes an application with
persisted **stopped intent**, even if an earlier failed stop left it running.
Temporary quiescing and internal resumption do not change operator intent. A previously running private service
resumes privately; a loopback-only service stays loopback-only, regardless of the
current `RSS_TAILSCALE` setting. A failed explicit backup is not confirmation that
remote access is closed. It can start an owned stopped VM for export without starting
the application; forwarding-query failure is also covered by VM restoration.
Restoration of a running application goes through the normal security gate.
Explicit `stop` records stopped intent before maintenance refusal or any shutdown
attempt, including already-stopped and failed shutdowns; only explicit `start`,
after its maintenance guard passes, re-enables running intent. Supervision respects that decision across ticks/login/reboot.
Normal shutdown stops the VM only after a successful backup. Shutdown backup failure
after quiescing leaves writers/forwarding stopped and the VM running; fix the cause
and retry. Preflight refusals can leave existing serving state untouched. This
replaces issue #1's former deferral. A repeat stop of an already-stopped VM does
not start it or create another archive. There is no bypass to delete data.

## Recovery on this or a clean host

1. Retain a **trusted** external archive and its reported checksum. Prepare a
   compatible Apple Silicon Mac with Homebrew, uv, virtualization, network access
   and the reviewed project checkout/configuration matching the archive's exact
   `image` configuration. The source VM is not required. Check current advisories;
   an old archive does not authorize bypassing the security gate. Treat the archive
   as a credential when transferring it.
2. On a new host use fresh local ownership records; never copy/fabricate
   `owner.json` from the source. On the existing host retain the original ownership
   directory and record the selected volume from `status`. If the application is
   running, **first** run normal `stop` with a working external backup directory.
   That keeps the original instance recoverable.
3. Install locked tools and initialize/preserve the owned VM, then restore into a
   fresh target. Restore validates the complete archive locally **before** VM start
   or volume creation; it may start the owned VM but never the application.

   ```sh
   uv sync --locked
   uv run --locked rss setup
   uv run --locked rss restore /absolute/private/path/backup.zip --volume rss-recovered
   uv run --locked rss select-volume rss-recovered
   # If there is no application container:
   uv run --locked rss start
   # If an old stopped application container exists, use replace INSTEAD of start:
   # uv run --locked rss replace
   ```

   Every existing target is refused, even an empty owned volume. The currently
   selected name is refused even when absent, preventing implicit activation.
   Failed imports retain incomplete targets unselected; `select-volume` refuses
   them. Choose a different fresh name for retry, not overwrite/delete. Links,
   unsafe paths, special/sparse files and unsupported versions are rejected;
   [format limits](#format-version-1) apply to both backup and restore.
4. Selection requires a running VM and stopped/absent application. Restore leaves
   the volume unselected. If selecting a previously restored volume after a later
   VM shutdown, confirm ownership with `uv run --locked rss status`, then start
   **only** the VM with:

   ```sh
   podman machine start --update-connection=false rss-aggregator
   ```

   This upstream command prints text, not project JSON; errors return nonzero.
   `--update-connection=false` preserves the default connection. It starts the
   named VM, not the application. Then select the volume and use `replace` (or
   `start` if no container exists). The original named volume is preserved. To
   switch back, stop safely and explicitly select that recorded original name;
   never edit ownership JSON to force selection.
5. Complete first activation with the exact recorded pin, then verify authenticated
   login, subscriptions, article content, unread/read and starred state. A pin
   change after selection is still refused before template writes/startup or
   replacement of an old container. Only successful activation through all gates
   releases this compatibility guard; backup alone does not. An expired PHP
   session is not lost data; authenticate again. `replace` preserves stopped intent;
   if you used it after `stop`, explicitly run `rss start` when running intent and
   future supervision recovery are intended. This performs another guarded restart.
6. For a changed port/host, apply the [base-URL configuration](configuration.md#changing-the-loopback-port)
   locally. Recheck [private HTTPS and reader access](tailscale-readkit.md) for a
   moved host; Tailscale configuration and ownership records are not migrated by
   the data archive. Do not print archived settings/secrets as evidence.
7. Set a durable private `RSS_BACKUP_DIR` on the new host before normal shutdown.
   Keep independent copies and retain the original data until recovery is verified.

## Image updates and rollback

First complete the initial recovery activation with its recorded matching pin.
After that, deliberate, owner-coordinated image updates remain supported; no
unattended image update is supported.

1. Work from the stable, synchronized controlling checkout in the normal Mac user's
   session. Retain the **original** `RSS_STATE_DIR`, `RSS_PORT`, `RSS_TAILSCALE` mode
   and private `RSS_BACKUP_DIR`; do not move ownership records or change deployment
   settings as part of this pin-only operation. Record whether supervision was
   enabled and its intended captured checkout, Python/dependency paths, ownership
   directory, port, forwarding mode and backup destination. Retain the original
   `config/image.json` privately outside Git alongside the recovery evidence.
2. Create a private, verified pre-change instance backup before changing the pin:

   ```sh
   uv run --locked rss backup &&
     uv run --locked rss backup-status &&
     uv run --locked rss supervision-status
   ```

   Retain the archive (for example, `/absolute/private/path/pre-update.zip`),
   checksum and original pin. Confirm successful resumption of the prior service/
   forwarding state and no maintenance hold; archive publication alone is not
   enough. Status commands can exit successfully while reporting an unhealthy
   state: inspect their JSON. Stop on failure or uncertainty and use the
   [failure/reconciliation procedure](#interrupted-maintenance-and-reconciliation)
   with the owner; never delete locks, maintenance markers or ownership records.
3. **Before any pin mutation**, successfully remove only owned supervision:

   ```sh
   uv run --locked rss supervision-remove &&
     uv run --locked rss supervision-status
   ```

   Removal is repeatable when supervision is already absent. Require successful
   removal and confirm `enabled: false`, `state: "disabled"` before proceeding.
   Stop on any refusal, partial result or uncertain removal; resolve with the owner,
   not by manually deleting managed jobs or registration files. Supervision captures
   the image-configuration hash: changing the pin while installed leaves it
   `configuration_unavailable`, not ready to supervise the replacement. Daily backup
   scheduling captures the environment, not this image hash; with those settings
   unchanged, do not disable/re-enable backups solely for this pin change.
4. Review the replacement **official** image's current advisories, digest, platform
   and effective Apache PHP policy. See [image configuration](configuration.md) and
   the [security policy](architecture.md#image-and-php-security-policy). Only after
   the removal gate above, explicitly update `config/image.json` to the reviewed
   pin. Then perform guarded replacement and verification:

   ```sh
   uv run --locked rss replace &&
     uv run --locked rss verify &&
     uv run --locked rss status
   ```

   Require the effective PHP/security gate, HTTP readiness, scheduler and initial
   feed-fetching checks to pass. Verify authenticated login, subscriptions, article
   content, persisted read/unread and starred state, and intended private HTTPS/
   reader synchronization where applicable. On any failure, stop and preserve the
   original archive, pin and volumes for owner-led recovery; do not bypass security,
   weaken TLS or force a database downgrade.
5. **Manual operation remains the default.** Only after all checks pass and explicit
   owner approval, optionally reinstall supervision with the intended captured
   settings and available dependencies from step 1:

   ```sh
   uv run --locked rss supervision-install &&
     uv run --locked rss supervision-status
   ```

   Require `state: "healthy"`; installation alone is not health evidence. Do not
   automatically enable a formerly disabled job. Removal/replacement preserves
   persisted intent: if stopped intent must become running, use owner-approved
   `rss start` and repeat the checks before installation, not a hand-edited record.

For rollback, use the retained **pre-update archive** and matching reviewed image/
configuration through [fresh-volume recovery and first activation](#recovery-on-this-or-a-clean-host).
Any rollback pin change likewise requires successful owned `supervision-remove`
**before** mutation and confirmation of disabled status as above, even if removal
is an already-absent no-op. Reassess current advisories; an old archive does not
permit a security bypass. Preserve the original and updated volumes until recovery
is verified. Never point an older release directly at a database migrated by a
newer release; rollback is fresh-volume restore and matching activation, not a
database downgrade. Keep supervision disabled unless explicitly approved and all
recovery checks pass before optional reinstallation.
