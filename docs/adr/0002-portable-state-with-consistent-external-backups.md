# Keep database storage native and make recovery portable

Status: Accepted. Local backup/restore implementation: issue #2; opt-in daily Mac orchestration:
issue #23. See
[verification scope](../development.md#verification-and-adoption-status) and
[format/recovery guide](../backup-restore.md) for tested scope and limitations.

Portability means being able to deploy a fresh instance elsewhere and restore its complete reading state, not preserving a particular data location or VM. Use SQLite in a project-owned Podman named volume on the VM's native Linux filesystem, with explicit backup and restore commands exporting complete instance backups outside the VM and Git checkout. This avoids a separate database service and makes recovery independent of the VM's survival.

## Considered options

- A Mac-directory bind mount makes files visible and preserves them automatically when the VM is removed, but introduces a shared-filesystem boundary for SQLite locking and durability.
- A named volume avoids that sharing layer but is lost with the VM unless backed up externally; choose it together with guarded backup and recovery, not on its own.
- An OPML subscription export is useful for importing feed choices, but cannot recover articles, users, configuration, or reading state.

## Consequences

- An instance backup includes the entire required FreshRSS data and recovery metadata, including the application version and image identity/platform. Use a standard portable archive with integrity verification rather than making a VM image the recovery interface.
- Quiesce all application writers, including scheduled fetching, before copying data. Publish completed archives atomically; preserve previous good backups when export fails.
- Issue #2 replaces the initial shutdown deferral: normal shutdown creates and verifies a consistent external backup after writers stop and before the VM stops. The explicitly configured `RSS_BACKUP_DIR` must be private and outside Git; no host destination is chosen silently. Backup failure leaves writers stopped, the VM running, source data intact and prior backups untouched. An already-stopped VM is not restarted just to repeat shutdown. VM removal still requires a fresh successful, verified external backup; no VM deletion command is provided.
- Restore into a fresh, explicitly named volume without implicitly overwriting or activating an existing instance. Activation requires a separate explicit owned-volume selection with the application stopped. Incomplete imports remain unselected and cannot be selected. Verify restored application behavior, including subscriptions, article content, accounts, and reading state.
- Restore a compatible recorded application version first, select an image appropriate to the destination architecture, and adjust host-specific settings such as the external URL. Neither an architecture-specific image digest nor a database downgrade is assumed portable.
- Backups contain credentials and private feed URLs. Keep them outside Git with restrictive permissions; these permissions are not encryption. Environment cleanup preserves external backups, and permanent data deletion is a distinct, explicitly confirmed operation.
- Opt-in Mac daily orchestration reuses the same guarded archive boundary and explicit private destination, rather than adding an export/transfer engine. Daily attempt, verified publication and safe resumption are separate evidence. A durable maintenance hold plus inherited project lock prevents supervision from reopening writers after parent-process death; releasing the hold requires explicit owned-engine reconciliation, not guessing from elapsed time or PID reuse. Backups never change persisted operator intent or resume intentional stopped intent.
- Do not automatically prune backups or articles. The initial production Mac slice adds daily scheduling and failure/freshness reporting, not automatic transfer, archive encryption/key management or database-engine changes. Actual Time Machine inclusion/copy retrieval/full-state recovery remain separate owner-coordinated evidence, not a consequence of local archive publication. Broader portable/Linux production operations remain deferred.

Implementation and verification: [backup and restore](https://github.com/brucepucci/rss-aggregator/issues/2) and [safe removal and recreation](https://github.com/brucepucci/rss-aggregator/issues/5).
