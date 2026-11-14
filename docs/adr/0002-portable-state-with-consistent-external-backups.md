# Keep database storage native and make recovery portable

Status: Accepted; implementation pending.

Portability means being able to deploy a fresh instance elsewhere and restore its complete reading state, not preserving a particular data location or VM. Use SQLite in a project-owned Podman named volume on the VM's native Linux filesystem, with explicit backup and restore commands exporting complete instance backups outside the VM and Git checkout. This avoids a separate database service and makes recovery independent of the VM's survival.

## Considered options

- A Mac-directory bind mount makes files visible and preserves them automatically when the VM is removed, but introduces a shared-filesystem boundary for SQLite locking and durability.
- A named volume avoids that sharing layer but is lost with the VM unless backed up externally; choose it together with guarded backup and recovery, not on its own.
- An OPML subscription export is useful for importing feed choices, but cannot recover articles, users, configuration, or reading state.

## Consequences

- An instance backup includes the entire required FreshRSS data and recovery metadata, including the application version and image identity/platform. Use a standard portable archive with integrity verification rather than making a VM image the recovery interface.
- Quiesce all application writers, including scheduled fetching, before copying data. Publish completed archives atomically; preserve previous good backups when export fails.
- Normal shutdown includes a consistent external backup after writers stop. VM removal requires a fresh successful, verified external backup and refuses automated deletion when that cannot be obtained.
- Restore into a fresh, explicitly selected volume without implicitly overwriting an existing instance. Verify restored application behavior, including subscriptions, article content, accounts, and reading state.
- Restore a compatible recorded application version first, select an image appropriate to the destination architecture, and adjust host-specific settings such as the external URL. Neither an architecture-specific image digest nor a database downgrade is assumed portable.
- Backups contain credentials and private feed URLs. Keep them outside Git with restrictive permissions; these permissions are not encryption. Environment cleanup preserves external backups, and permanent data deletion is a distinct, explicitly confirmed operation.
- Do not automatically prune backups or articles in the initial setup. Offsite backup, archive encryption/key management, database-engine changes, and production deployment remain out of scope.

Implementation and verification: [backup and restore](https://github.com/brucepucci/rss-aggregator/issues/2) and [safe removal and recreation](https://github.com/brucepucci/rss-aggregator/issues/5).
