# Runtime architecture and safeguards

This is the implementation model for the Apple Silicon deployment. Use the [operator guide](../README.md), [configuration reference](configuration.md), [CLI reference](cli.md), [operations](operations.md), and [recovery guide](backup-restore.md) for practical steps.

## Deployment and storage

```text
ReadKit / browser → private tailnet HTTPS (optional Tailscale Serve)
                                      ↓
local browser → 127.0.0.1 : RSS_PORT → Caddy / FrankenPHP → upstream FreshRSS
                                                              ↓
                                              selected instance data / SQLite
```

FreshRSS runs as the normal macOS user. The installation's private state root separates pinned code, generated service configuration, instance data and lifecycle evidence. Only the selected instance serves requests and fetches feeds. SQLite stays on the native local filesystem, not in a synced path or against a separate database server. An owned directory is not a VM or a sandbox: the process runs with the user's permissions. The optional Tailscale proxy is not a database or identity provider.

[`cli.py`](../rss_aggregator/cli.py) adapts Click to callable [`Runtime.run`](../rss_aggregator/runtime.py). [`native_install.py`](../rss_aggregator/native_install.py) owns runtime artifact verification, [`native_service.py`](../rss_aggregator/native_service.py) the writer controller, and [`private_access.py`](../rss_aggregator/private_access.py) forwarding and API diagnostics. [`recovery.py`](../rss_aggregator/recovery.py) and [`archives.py`](../rss_aggregator/archives.py) handle instance provenance, snapshot, archive validation, and activation. [`supervision.py`](../rss_aggregator/supervision.py) and [`backup_schedule.py`](../rss_aggregator/backup_schedule.py) implement independent session policies through the same runtime guards.

## Pinned installation and security policy

The reviewed [native manifest](../config/native.json) binds the FreshRSS source revision and checksum to the official FrankenPHP macOS arm64 asset and its checksum, with exact PHP and Caddy version expectations. The installer verifies downloads before using them, refuses unsafe source archive entries and canonical-name collisions, probes the verified executable for versions and SQLite support, and records a digest of the private installed code tree. Setup revalidates a matching installation and refuses unowned paths or a different pin. No shared installation or host PHP is used; metadata probes do not replace the effective web checks. Pin changes and their security review are explicit, not part of startup, migration or ordinary dependency updates.

## Startup security gate

The runtime checks native ownership, activation compatibility and the requested loopback port. For initialized data it configures SQLite FreshRSS with form authentication, anonymous access and anonymous refresh off, Google Reader-compatible API enabled, and upstream app updating disabled. A new uninitialized instance may expose the upstream installer on loopback only. Use [operations](operations.md) for the verification boundary before entering real credentials or forwarding to clients.

The runtime launches a detached Python controller for one generation; that controller starts only the pinned FrankenPHP executable for web serving and scheduled upstream PHP fetching. The generated Caddy config binds IPv4 loopback, serves only FreshRSS's public tree, disables Caddy administration/config persistence/automatic HTTPS, and has generation-bound local readiness and temporary probe routes. The probe checks the effective web SAPI and PHP version, exact INI loading, no ambient INI scan, SQLite availability, that `session.upload_progress.enabled`, `expose_php` and `display_errors` are off, and the effective authentication settings for initialized data. The temporary probe is removed after every check. The runtime also checks loopback application response, scheduler and refusal of anonymous API requests before private publication. A repeated start rechecks current security before retaining a matching generation; failed checks and installer completion take the guarded restart path. Startup performs an immediate upstream fetch for initialized data.

The controller exits if it does not receive the current generation's security approval within the startup window. Once approved, it evaluates UTC 20-minute fetch slots (`*/20 * * * *`); missed slots are not replayed. Manual and periodic fetches share a fetch lease and still honor upstream TTL, cache and backoff. Fetching from publishers is separate from client synchronization. The web and scheduled fetch children inherit the controller's writer lease. Stop requests address a specific service generation; the controller drains/stops its own children, not arbitrary PIDs from disk. A released kernel writer lease, rather than a PID or status receipt alone, is the idle proof required for a snapshot or other guarded mutation.

## Session supervision and operator intent

A nonblocking per-state command lock serializes lifecycle, supervision, scheduling and recovery. The independent writer lease persists past the initiating CLI and through surviving writer children; a separate controller-only lease distinguishes a live controller from surviving orphaned writers. Status observes those leases without taking ownership of the service. Supervision measures owned HTTP readiness and scheduler/forwarding health before attempting recovery. Per-state, per-role LaunchAgent identities and exact capture/ownership checks prevent a rehearsal job from adopting a production or legacy job. The normal GUI session is the service boundary; there is no pre-login startup or host power-policy change.

Only explicit start sets running intent; explicit stop sets stopped intent before the maintenance guard. Backup quiescing does not set stopped intent. Supervision needs positive running intent and a valid captured job and stops retrying after a bounded budget (three recovery attempts with backoff). Status does not authorize a restart. Explicit start or healthy observation resets the budget. Reconciliation never changes intent, so a later healthy session can still retry if intent remains running. To stay stopped, record stopped intent before releasing maintenance. See [captured settings](configuration.md#captured-mac-supervision-configuration) and [operations](operations.md).

## Private HTTPS and authentication

Tailscale Serve is an explicit opt-in, forwarding the node's owned HTTPS port 443 to the loopback backend. Preflight checks the connected node, HTTPS eligibility, effective access policy and route ownership. Reject conflicting or changed 443 listeners and public Funnel routes reaching this backend rather than taking over or globally resetting them. If private preflight confirms public exposure of the active owned backend, request its shutdown without modifying the foreign route; unrelated conflicts don't stop a generation. Tailnet restrictions and TLS supplement, not replace, FreshRSS authentication. The authenticated diagnostic prompts for a separate per-user API password, rejects redirects, checks trusted HTTPS by default, and never returns tokens or response content. `--loopback` cannot verify TLS or actual ReadKit synchronization.

### Conditional forwarding ownership

Serve changes are conditional LocalAPI writes using the observed ETag; the application command lock cannot serialize independent Tailscale clients. The ownership ledger binds the exact route to this runtime. Before publishing, save pending intent; definitive refusal removes new authority before restoring the preceding local record. An ambiguous request retains evidence rather than triggering a global rollback. Before withdrawal, record relinquished authority; unknown withdrawal outcomes cannot justify adopting an identical recreated listener. Unrelated routes and Tailscale installation/configuration are preserved. See [private access](tailscale-readkit.md) for trusted-client and device checks.

### Post-start failure cleanup

Startup attempts to stop the owned writers and remove the temporary probe on failure after it enters the writer boundary. The writer shutdown path does not depend on successful conditional unpublication. A route mutation with uncertain outcome requires local inspection; a failed startup is not proof that Tailscale removed every route. Do not retarget another backend to an uncertain route or bypass an authentication/TLS gate to recover access.

## Daily backup orchestration

Daily backup is an independently opt-in normal-user-session job, not permission for periodic writer recovery. The job evaluates the latest elapsed eligible UTC slot under the command lock, even across midnight, with bounded retry instead of replaying a backlog. Its own archived path and checksum are tied to a distinct registration and checked for availability independently of newer manual or shutdown archives. Explicit and scheduled backup share the same snapshot path; normal shutdown also verifies a complete backup where data exist and never resumes. The attempt, verified local archive and resumption are different facts; backup status reports them separately. Healthy daily publication targets roughly a day of data loss under normal operation, not guaranteed protection against losing the host.

Before quiescing, write a durable maintenance marker that blocks recovery even if the orchestrating process dies. Withdraw owned forwarding, stop the web and fetch writers and prove the writer lease is free before copying the complete data directory. Explicit backup can resume a previously running backend after snapshot work if intent permits, but only republishes forwarding it confirmed withdrawing. Reconcile only an owned, recognized hold after confirming idle writers and no active owned forwarding; no time-based expiration or manual marker deletion. Recorded archive success may survive a failed resumption. Recent, registered daily protection is not evidence of Time Machine inclusion, independent retrieval or usable restored application state.

## Archive, recovery and retirement boundaries

A native v2 backup is a private ZIP (`manifest.json` and `data.tar`). The validator checks archive structure, exact compatibility identity (platform, FreshRSS version and source checksum, FrankenPHP version and executable checksum, PHP and Caddy versions), payload length/digest, and safe paths/types. Publication is atomic and non-overwriting after validation; previously verified archives survive a failed export. Checksums establish integrity, not the origin or independence of a copy. Only use trusted archives. The backup covers FreshRSS data, not the runtime installation, macOS jobs, or the rest of the host.

Restore stages private files under a fresh instance name, then publishes without replacement. Explicit selection while stopped is separate from both restore and startup; incomplete or incompatible instances cannot be activated. Default provisioning is recorded outside the instance directory so loss of previously provisioned data cannot be treated as a pristine install. The transitional v1 importer validates the reviewed legacy image and same FreshRSS release, maps the full payload to private native storage, and never accesses or changes the VM. It is not a fallback restore format or a supported reverse migration.

Upgrade and uninstall use a preview token bound to owned runtime state and the requested operation; they require stopped writers and retired job registrations and cannot delete instance data or backups. Upgrade is limited to PHP/web runtime changes for the same FreshRSS version, source digest and platform. Potential data writes during startup and serving belong to a durable per-instance writer generation. With existing instances, apply independently revalidates the selected instance's recorded shutdown archive against the installed pin and requires it to cover the last writer generation before replacing the runtime. Activation checks the source identity; archive restore still requires the exact full pin. A runtime change is not permission for a data-format conversion or a downgrade. Legacy VM retirement uses the [guarded historical procedure](https://github.com/brucepucci/rss-aggregator/blob/28664ab936f8872da6ff3ddca1c4b0a725dfb0ec/docs/operations.md#preview-and-remove-the-vm) only after the owner-assisted live and independent-copy gates. Nothing in native uninstall retires legacy resources.

## Decisions

- [ADR 0001: Dedicated Podman isolation (superseded for this deployment)](adr/0001-isolate-local-development-with-podman.md)
- [ADR 0002: Local storage and portable recovery](adr/0002-portable-state-with-consistent-external-backups.md)
- [ADR 0003: Private Tailscale access and shared reading state](adr/0003-private-tailscale-access-and-shared-reading-state.md)
- [ADR 0004: Pinned native FreshRSS on Apple Silicon](adr/0004-run-pinned-freshrss-natively-on-apple-silicon.md)
