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
export RSS_BACKUP_DIR="$HOME/Library/Application Support/rss-aggregator/backups"
uv run --locked rss backup
# Or back up as part of normal shutdown:
uv run --locked rss stop
```

Success returns the archive path, whole-ZIP SHA-256 and verified manifest as JSON.
Inspect `status` and private `last-backup.json` if resumption fails after publication.
Do not treat an interrupted unpublished file as a completed archive. Nothing
prunes, overwrites or uploads prior backups. See [failure behavior](#consistency-and-failure-behavior)
before assuming a failed operation left the service stopped.

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
well as lifecycle/supervision commands. Native subprocesses inherit the held lock,
so an orphaned export retains serialization after its Python parent is killed.
Supervision also refuses a durable maintenance hold; never delete one to bypass a
consistency window. See [session recovery response](operations.md#daily-check-and-failure-response).
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
Explicit `stop` records stopped intent before any shutdown attempt, including
already-stopped and failed shutdowns; only explicit `start` re-enables running
intent. Supervision respects that decision across ticks/login/reboot.
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
   session is not lost data; authenticate again.
6. For a changed port/host, apply the [base-URL configuration](configuration.md#changing-the-loopback-port)
   locally. Recheck [private HTTPS and reader access](tailscale-readkit.md) for a
   moved host; Tailscale configuration and ownership records are not migrated by
   the data archive. Do not print archived settings/secrets as evidence.
7. Set a durable private `RSS_BACKUP_DIR` on the new host before normal shutdown.
   Keep independent copies and retain the original data until recovery is verified.

## Image updates and rollback

First complete the initial recovery activation with its recorded matching pin.
After that, deliberate image updates remain supported. Create a verified pre-update
backup. Review the replacement official image's current
advisories, digest, platform and actual PHP configuration, update the pin explicitly,
then run `uv run --locked rss replace` and verify security, login, fetching and
persisted reading state. See [image configuration](configuration.md) and the
[security policy](architecture.md#image-and-php-security-policy). No unattended image
update is supported. If rollback is required after a database migration, restore
the pre-update archive into a fresh volume with the matching image/configuration;
never point an older release at an arbitrarily newer database.
