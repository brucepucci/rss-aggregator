# Instance backup and recovery

Issue #2 implements [ADR 0002](adr/0002-portable-state-with-consistent-external-backups.md).
See [README](../README.md#backup-and-restore) for commands, configuration and actual
verification evidence. This document describes format version 1, not a promise of
native Linux or cross-architecture recovery support.

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

The manifest's image fields are required to equal the reviewed configuration in
`config/image.json`. This deliberately conservative compatibility rule avoids
silently restoring with a different release/platform/security policy. Archive
checks do not authorize running a known-vulnerable image. Reassess advisories and
retain the existing effective Apache PHP gate. The image's arm64 platform manifest
is not a portable executable for an x86 machine.

## Consistency and failure behavior

The normal project lock covers backup, restore and explicit volume selection as
well as lifecycle commands. Both rootless and rootful engine states are inspected
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

Explicit backup preserves the earlier application/VM running state where safe.
It can start an owned stopped VM for export without starting the application;
restoration of a running application goes through the normal security gate.
Normal shutdown stops the VM only after a successful backup. Failed shutdown
backup leaves writers stopped and the VM running; fix the cause and retry. This
replaces issue #1's former deferral. A repeat stop of an already-stopped VM does
not start it or create another archive. There is no bypass to delete data.

## Non-overwriting recovery drill

1. Retain an external trusted archive and its reported checksum. Prepare a fresh
   supported Mac with the reviewed project checkout/configuration, Homebrew, uv,
   virtualization and network access. The source VM need not exist or be reachable.
2. Run `uv sync --locked` and `rss setup`. Use a fresh local ownership directory;
   never fabricate/copy ownership records to adopt another VM.
3. Run `rss restore /absolute/private/backup.zip --volume rss-recovered`. Validation
   happens locally first. The command may start the owned VM, but not the app. A
   fresh target receives the local owner label and imported numeric ownership.
   Every existing target is refused, even if apparently empty. The currently
   selected name is also refused even if absent, preventing implicit activation.
   An incomplete target after import failure is retained unselected and cannot be
   selected; choose a new name for retry.
4. If an existing app is running, first stop it using normal `rss stop` with a
   configured external backup directory before restoring. For an already-restored
   target after later shutdown, confirm ownership with `rss status` and start only
   the VM with `podman machine start --update-connection=false rss-aggregator`.
   Then explicitly run `rss select-volume rss-recovered`.
   Selection requires a running VM and stopped/absent app. `rss start` creates an
   absent app; `rss replace` replaces an old stopped app without deleting its
   original named volume. Restore itself never switches a working instance.
5. Verify the effective PHP gate, authenticated login, subscription, exact article
   content, unread/read states and starred state through the restored web instance.
   Reconfigure a changed base URL inside the restored container as documented in
   README. An expired PHP browser session is not lost user data; authenticate again.
6. Configure `RSS_BACKUP_DIR` for the new host before normal shutdown. Keep independent
   durable copies; no cleanup/pruning command is provided.

## Image updates and rollback

Create a verified pre-update backup. Review the replacement official image's current
advisories, digest, platform and actual PHP configuration, update the pin explicitly,
then replace and verify application behavior and reading state. No unattended image
update is supported. If rollback is required after a database migration, restore
the pre-update archive into a fresh volume with the matching image/configuration;
never point an older release at an arbitrarily newer database.
