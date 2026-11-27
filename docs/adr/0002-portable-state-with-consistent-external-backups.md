# Keep database storage local and make recovery portable

Status: Accepted; [ADR 0004](0004-run-pinned-freshrss-natively-on-apple-silicon.md) changes the storage location and compatibility metadata, not the recovery contract.

Use SQLite on a local filesystem and make a complete, verified instance backup the recovery interface, rather than relying on a surviving host or exporting a list of subscriptions. The earlier Podman deployment used SQLite on the guest's local filesystem; native runs it on the Mac's local filesystem in private instance data, not on a network or synced mount. This keeps recovery independent of a particular data location or VM without introducing a database server.

## Consequences

- Quiesce web and feed writers before capture. Verify the archive, publish atomically without overwriting, and preserve earlier verified backups on failure. Daily scheduling and normal shutdown reuse that boundary, with durable maintenance preventing premature writer recovery. Archive publication and resumption are separate outcomes.
- A native archive records exact runtime compatibility in v2, not a pretend container identity. Restore into fresh private data and select the recovered instance separately; never use in-place overwrite or a database downgrade as a recovery assumption. Legacy import is transitional and one-way.
- Archive contents include credentials and private feed URLs. Use private storage outside Git and independently retained copies. A successful local archive is not Time Machine evidence. There is no automatic archive transfer, encryption/key management, or pruning; data deletion is a separate explicit decision, not a side effect of uninstall.

See the [backup and restore guide](../backup-restore.md) for procedures and the [runtime architecture](../architecture.md#archive-recovery-and-retirement-boundaries) for implementation boundaries.
