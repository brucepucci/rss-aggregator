# Instance backup and recovery

Canonical procedure for complete native FreshRSS instance backup and recovery on
Apple Silicon macOS. See [CLI syntax and results](cli.md) and [settings](configuration.md).
For a trusted Podman archive, use the separate [one-time migration](operations.md#one-time-migration-from-podman),
not `restore`. Physical replacement-host recovery is owner-assisted; Linux and
Intel Mac are not supported destinations.

## Scope and trust

Save the **entire FreshRSS `data` directory**: SQLite databases, users, settings,
subscriptions, articles, reading state, logs, cache and any SQLite journal/WAL/SHM
files present when quiesced. Keep these sidecars with the database; do not strip
journals or replace this procedure with a live SQLite-file copy. Operational
journals/receipts outside `data` are kept locally for troubleshooting, not restored
as instance data; neither runtime files, LaunchAgents, forwarding ownership nor
external extension code are part of the archive.

Archives contain credentials, private feed URLs and user data, as can logs and
staging files. Store/transfer them as secrets, outside Git with private permissions;
permissions are not encryption. Checksums verify integrity, not provenance: restore
only trusted archives and compare the whole-file checksum via a trusted record.
OPML is a subscription export, not an instance backup. No automatic pruning,
upload or encryption is performed.

## Create a backup

Choose an absolute private destination outside **every Git checkout**. It must
neither overlap nor contain the controlling checkout or `RSS_STATE_DIR`; do not
place it under a managed path such as `runtime/`. Set `RSS_BACKUP_DIR` explicitly; there is no default.
Use an owned `0700` directory on a filesystem supporting hard links for
non-overwriting publication (`0600` files). A new directory may be created; unsafe existing
permissions are refused rather than repaired. Keep an independent durable copy.

```sh
# Example only; choose and approve your destination separately.
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
uv run --locked rss backup
# Or take a verified backup and leave the instance stopped:
uv run --locked rss stop
```

Confirm `verified: true`, the archive path and whole-ZIP SHA-256 (`stop` returns
archive evidence under `backup` when data exist). Retain the checksum privately.
Inspect `status` and `backup-status` to confirm the intended serving state and no
maintenance hold; publication and resumption are separate. An interrupted staging
file is not a completed archive. Previous good copies are not overwritten.

## Format version 2

The native snapshot is a ZIP with `manifest.json` and `data.tar`. The manifest
records format v2, UTC creation time, scope, payload byte length and SHA-256 and
an exact native runtime identity: FreshRSS version and source archive digest,
FrankenPHP version and asset digest, embedded PHP and Caddy versions and platform.
Keep the matching reviewed [runtime manifest](../config/native.json) (including
upstream revision) and checkout revision. The same FreshRSS version alone does
**not** make a different native runtime compatible. ZIP structure, manifest,
payload integrity and safe data paths are validated before publication or restore.
Paths are limited to **4,096 UTF-8 bytes** and **64 components** each; cumulative
path metadata is limited to **8 MiB**. A legitimate but exceptionally large or
deep tree can be refused even with free disk. Preserve the source and prior
archives; do not bypass validation.

Allow room for several full-size copies at the backup destination: the original
staged tar, uncompressed ZIP, and additional raw and sanitized tars during
verification. Existing archives stay in place. Restore needs room for the raw
and sanitized tars in the system temporary directory as well as the extracted
data on the target filesystem. If those locations share a volume, budget their
combined working space.

The old v1 Podman image identity is not a native runtime pin; ordinary native
`restore` rejects v1. Import through the temporary migration command only.

## Consistency and failure behavior

- Commands serialize; backup maintenance is durable across interruption. Owned
  forwarding and all managed writers, including active web requests and in-flight
  feed fetching, must be quiesced before copying. Do not point external writers at
  managed SQLite data: the project's leases cannot control them.
- Explicit `backup` preserves persisted intent and attempts to resume a previously
  running backend after a snapshot, including a failed snapshot when safe. It
  only republishes forwarding that it confirmed withdrawing; if the route already
  disappeared, it can resume on loopback only after rechecking that no Funnel
  reaches the port of the previously private service. If safety cannot be
  confirmed, writers stay stopped and the foreign route is preserved. It never
  resumes intentional stop.
  A preflight refusal can leave existing serving state untouched. A resumption
  failure can leave a verified archive with the app stopped; check both results
  before retrying.
- Normal `stop` sets stopped intent and quiesces before reading selection or
  backup bookkeeping. It verifies a complete archive when selected data exist.
  An archive failure **after** quiescing leaves writers stopped. Fix the reported
  cause and retry `stop`; for selection loss see [operations](operations.md#common-troubleshooting).
  Preserve an unreadable backup record and prior archives for local inspection.
  A preflight or uncertain shutdown is not proof that writers or forwarding are stopped.
- After failure inspect `status`, `backup-status`, stderr and private records.
  Never discard a good archive, promote `.rss-export-*`/`.rss-backup-*` remnants,
  or remove locks or maintenance records to force a retry. Hidden remnants may
  contain complete secrets. If publishing may have succeeded but evidence was not
  recorded, verify a trusted copy by restoring into a fresh isolated target before
  relying on it. Inspect failed staging and stale copies locally; any deletion is an
  owner-approved exact-path action after confirming idle.

### Interrupted maintenance and reconciliation

A durable `maintenance.json` hold blocks writer operations and recovery. Even a
malformed marker or dangling link is a hold, not permission to restart. Wait for
active commands/children and inspect `backup-status` with the owner. If the app
must stay stopped, first run `rss stop` to persist stopped intent. It can record
intent even when maintenance prevents completion, but **not** when another command
holds the lock; wait and retry, never unlink the lock.

```sh
uv run --locked rss backup-reconcile
uv run --locked rss backup-status
```

Reconciliation validates the hold and ownership, and requires proof that writers
and owned forwarding are already stopped; it does not stop or restart anything.
If it reports active resources, use guarded `stop` to quiesce before retrying;
`stop` records stopped intent and leaves the hold for reconciliation. A later
supervisor tick may honor an existing running intent after release, if that intent
was retained. Stop on a busy lease, uncertain forwarding or invalid record and
inspect locally. Do not edit journals, kill unidentified processes or force-clear
a hold.

## Opt-in daily Mac backups

**Owner-coordinated change:** obtain a verified private backup and confirm the
stable checkout, Python environment, native state, port, forwarding mode and
backup destination before enabling. This separate per-state GUI LaunchAgent does
not install application supervision. Use previously approved `~/FreshRSS-Backups`
only when it is the intended production destination; disposable checks need
separate storage. After approval, with those selectors exported:

```sh
uv run --locked rss backup-enable --hour 3 --minute 0
uv run --locked rss backup-status --check
# Opt out independently, preserving archives and evidence:
uv run --locked rss backup-disable
```

Default policy is **03:00 UTC daily**, adjustable with `--hour`/`--minute`.
Launchd requests checks at load and every 15 minutes while the session and host
are available. Eligible failures retry after at least 15 minutes, at most three
attempts for a UTC target; contention is a defer, not success or a spent attempt.
Wakeup considers only the most recent elapsed slot since registration, including
one on the preceding UTC date if the wake crosses midnight. It does not replay
older slots or work scheduled before registration. No wake-from-sleep, pre-login
or power setting is installed. A job captures absolute checkout/Python, private
state and non-secret settings; it does not use `.env` or invoke uv. The owned job
can be disabled without erasing archives or intent. A missing plist is recoverable
if the ledger is valid and any loaded job matches it; a busy, changed or foreign
job is not overwritten. Follow
[removal and recapture safeguards](operations.md#upgrade-installed-mac-jobs-safely)
for changes. Recapture starts a new `registration_id`, even with identical
settings. Wait for a successful `daily_attempt` with that ID after `registered_at`
and verify `daily_archive_available`; an earlier registration's evidence is
retained but does not count.

### Daily check and failure response

`backup-status --check` emits status and returns nonzero unless `protected` is
true. Ordinary `backup-status` is inspection, not an assurance of protection.
Check registration, `scheduler_problem`, due/missed/deferred work, latest attempt
and verified publication, archive availability, age/overdue, `resumption` and
`maintenance_required`. A damaged runtime layout does not erase readable archive
or attempt evidence; `scheduler_problem` identifies the unhealthy configuration
and protection is false. Daily protection needs successful eligible daily work for
the current registration and selected instance, availability of **that scheduled
archive**, and no unresolved failure/hold. `archive_available` and `latest_success`
can instead describe a newer manual or shutdown archive; neither can mask a
missing scheduled copy. A recovery point more than **26 hours** old is overdue;
the daily goal is about a day of loss under healthy operation, not a guarantee
during outages. Status is not a fresh archive
rehash or proof of independent copies. Manually invoking `backup-due` does not
establish that launchd ran unattended.

- Destination or integrity failure: verify private directory/access/free space,
  preserve the prior archive and retry by the applicable procedure.
- Missing checkout/Python/job or changed policy: repair the selected prerequisites
  or explicitly disable and recapture the exact owned definition. Preserve evidence
  of previous attempts. Never use another job label as a substitute.
- Failed resumption or interruption: preserve the archive if published and follow
  [reconciliation](#interrupted-maintenance-and-reconciliation). For owner acceptance,
  observe safe state **after** a naturally scheduled invocation exits, not just
  its in-job result. Avoid real production fault injection.

## Recovery on this or a clean host

1. Privately obtain a trusted, complete native ZIP and checksum plus the matching
   reviewed checkout and manifest. For real data use owner approval; agree expected
   authenticated users/settings, subscriptions, representative articles and
   read/unread and starred/unstarred markings before the drill. Plan to prevent
   unauthorized outbound fetching from real-data clones: loopback does not block
   startup fetches or scheduled publisher requests.
2. On a compatible Apple Silicon Mac use an isolated **fresh** private local
   `RSS_STATE_DIR`, different free `RSS_PORT`, separate external `RSS_BACKUP_DIR`
   and `RSS_TAILSCALE=0`; leave both jobs disabled. Inspect Serve/Funnel and other
   known forwarding before choosing the port: it must **not** already be a target,
   even with no listener yet. Do not change someone else's route to obtain isolation.
   Never copy ownership, selection, job or forwarding records over from the old Mac.
3. From the matching checkout, with those settings exported and `NATIVE_ARCHIVE`
   pointing at the trusted local copy, restore **before starting an empty default**:

   ```sh
   uv sync --locked &&
     uv run --locked rss setup &&
     uv run --locked rss restore "$NATIVE_ARCHIVE" --instance recovery &&
     uv run --locked rss select-instance recovery &&
     uv run --locked rss start &&
     uv run --locked rss status
   ```

   `restore` validates the exact pin and publishes a **fresh named** private
   target; it neither overwrites nor activates it. Use an unused name such as
   `recovery`, not the current selection or a path. Names are 1–48 lowercase letters,
   digits or hyphens, starting with a letter or digit. `select-instance`
   requires verified owned data, all writers idle and no unresolved maintenance.
   For deliberate selection inside already used state, first disable jobs, take a
   verified backup and record stopped intent with `stop`; leave existing data intact.
   A mismatching pin, target collision or incomplete target is a refusal; do not
   hand-edit receipts or try an arbitrary alternate runtime.
4. At the isolated loopback URL, check fresh sign-in, settings, subscriptions,
   article content and reading state. An installer, failed login or missing state
   is failure, not a reason to reset credentials or initialize the recovered data.
   Keep the original archive for another attempt. Stop and verify the isolated
   shutdown backup before treating the drill as complete. Make any production
   activation a separate owner decision. On a replacement host, recheck port,
   access policy, backups and [private HTTPS](tailscale-readkit.md) before recapturing
   wanted jobs.

## Time Machine copy recovery drill

This owner-assisted gate tests a **completed independent copy** of an unattended
native archive, not just current inclusion or successful local publication. The
[historical issue #24](https://github.com/brucepucci/rss-aggregator/issues/24) is
legacy evidence; it does not pass native [retirement](operations.md#legacy-retirement-gate).
Under healthy daily protection the goal is roughly a day's maximum data loss
and recovery by the next day once a replacement Mac and the independent copy are
available; this is a target, not measured native timing.

1. **Agree scope.** Without separate approval, do not install/recapture jobs, run
   backup on production for a test, modify exclusions, grant Full Disk Access,
   mount/unlock storage, change encryption settings or start a Time Machine copy.
   Select an archive from a naturally scheduled native run and correlate the
   private daily, attempt and success records (UTC policy/time, instance, archive
   checksum and verification). Preserve the correlation before later operations
   supersede it. Do not relabel manual or stop-time evidence as scheduled.
2. **Inspect inclusion and destination.** On the Mac, after approval, run read-only
   `tmutil isexcluded "$RSS_BACKUP_DIR"` and check the selected archive itself;
   use `tmutil destinationinfo` and `tmutil listbackups` to identify the intended
   destination and completed copies. Assess encryption at rest, physical/network
   access and the ability to unlock without the old Mac. FileVault on the source
   Mac and owner-only file modes do not prove destination encryption. A permissions
   refusal or serious unresolved protection gap leaves the gate open; have the
   owner review the installed `man tmutil`, not automatically escalate access.
3. **Retrieve independently.** Locate the actual archive inside a completed copy
   on the separate destination (use Time Machine/Finder or the installed tmutil).
   Do not use a source APFS snapshot, current source file, symlink to it, or an
   in-progress copy. Retrieve into a fresh owner-only staging directory outside
   Git without overwriting anything. For `tmutil restore SOURCE DESTINATION`, set
   `SOURCE` to the actual archive **inside** that copy and `DESTINATION` to a
   nonexistent file in that directory. Compare its whole-file SHA-256 (for example,
   `shasum -a 256` on the retrieved file) to the privately recorded publication
   checksum; a later copy timestamp alone is not provenance. Preserve sources.
4. **Restore and authenticate.** Follow [clean-host recovery](#recovery-on-this-or-a-clean-host)
   with the retrieved ZIP and exact pin; use dedicated private state, an unforwarded
   loopback port, no production jobs and a separate backup destination. Confirm
   fresh login and complete representative application state. Synthetic data can
   prepare the workflow; it does not certify the chosen production recovery
   point. Restore-only inspection is not the authenticated recovery gate.
5. **Record and retain.** Report only sanitized attempt/copy/recovery times,
   provenance, checksum match and application outcomes, including any failed or
   unperformed gate. Keep private comparisons, archive and access details local;
   do not share raw paths, logs, credentials or publisher URLs. Stop the isolated
   service, preserve relevant evidence and leave production/Time Machine history
   unchanged. Dispose of any staging copies only with separate explicit consent.
