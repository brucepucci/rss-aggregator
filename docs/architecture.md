# Runtime architecture and safeguards

For the deployed component diagram and ordinary use, start with the
[README](../README.md#architecture). This page describes implementation constraints;
[ADRs](#decisions) record the trade-offs. This repository manages an official
FreshRSS image rather than maintaining a fork or custom application image.

## Deployment and storage

The supported runtime is a dedicated rootless Podman Linux VM on Apple Silicon
macOS. Commands explicitly select the owned machine/connection and labelled
container/volume; they do not adopt colliding resources or change global Podman
defaults. Only `127.0.0.1:<RSS_PORT>` is published. There is no host PHP/database,
privileged socket helper or Podman Desktop. Manual operation installs no host
service. User-session startup/recovery and daily backups are separately opted-in
LaunchAgents; enabling backups does not enable application supervision.
Native Linux hosting and other architectures are not implemented/verified.

The complete FreshRSS `data` directory is in a named volume on the VM's native
filesystem, initially `rss-aggregator-data`. SQLite avoids an extra database service;
native storage avoids a host shared-filesystem locking/durability boundary.
Container replacement preserves this volume; VM loss destroys it. External,
quiesced [instance backups](backup-restore.md) are the recovery interface, not VM
images, checkout copies or OPML. Extensions outside `data` are not covered.

[Configuration](configuration.md) defines the fixed VM/container settings and
managed state records. [Operations](operations.md) explains ownership refusals,
scheduling and shutdown. [Development](development.md) separates isolated tests,
Mac integration and human device evidence.

## Session supervision and operator intent

`supervision.py` extends the existing CLI/Runtime boundary rather than providing
another lifecycle implementation. Only public `start`/`stop` persist version 1 operator
running/stopped intent; absence is unset, never implicit authorization to start.
Public stop writes stopped intent before a maintenance-marker refusal; public start
writes running intent only after the maintenance guard passes. Internal `_start`
restores temporary backup state without changing intent, and backup resumption
refuses stopped intent. `verify`/`replace` retain intent.

Explicit registration captures the stable checkout, `.venv` interpreter, ownership,
reviewed image-configuration identity and required non-secret environment into an
owned per-user LaunchAgent/ledger. No shell, automatic `.env`, privileged service,
package installation or host security/power change is involved. The GUI-session
job requests a tick at load and every 60 seconds; normal-session availability is
required. Missing/moved code/dependencies and foreign/changed jobs fail without
falling back to another checkout or adopting resources. Tailscale remains a captured
dependency while forwarding ownership state exists, including inactive intent in
loopback mode: health inspection and guarded LocalAPI cleanup still need it.
Inactive intent is preserved, not removed to avoid dependency capture.

Under the existing nonblocking `command.lock`, a tick first respects intent and
any crash-persistent `maintenance.json`, then checks owned runtime readiness and
private forwarding. Healthy observations do not call start. Recovery calls the
normal start path with all security/readiness/authentication/forwarding guards;
three consecutive attempts use minimum 60s/300s backoff, persisted before mutation.
Healthy ticks/explicit start reset attempts while retaining ten secret-free recent
outcomes. Raw external command errors are not retained or copied into jobs/logs.
Short-lived native child processes inherit the lock descriptor so killing the
Python parent does not let recovery contend with its still-running external work.
The sole exception is `podman machine start`: both explicit startup and Recovery's
VM-start path call the private `Runtime._run(..., inherit_lock=False)` (default
`True`). The Python parent holds `command.lock` throughout the launcher call, but
long-lived gvproxy/krunkit helpers must not inherit and indefinitely retain that
lease. Exports/imports and all other short-lived native work retain inheritance;
durable maintenance still blocks writer recovery after interruption. Independent
Podman/Tailscale clients remain outside this lock.

Both bounded LaunchAgents set `AbandonProcessGroup=true`. By default launchd kills
remaining members of a completed job's process group, including VM helpers after a
successful in-job recovery check. Abandonment preserves the intended native lifetime,
not ownership or startup authority; explicit shutdown and guarded stopped-VM backup
restoration remain unchanged. It also lets consistency-critical native work retain
its inherited lease after orchestration death. Process-group cleanup and descriptor
inheritance are separate policies: neither replaces the other or durable maintenance.
Post-exit independent availability is the regression boundary, not the parent's exit
code. No general subprocess/session policy or foreign-process management is added.

Job identity checks are not weakened for upgrade. Supervision removal recognizes
only the byte-exact prior definition (the current definition without abandonment),
under the same registration/loaded identity guards; install and recovery require
the new definition. Backup removal uses the stored job digest, including for old
registrations. Deliberate recapture changes backup policy identity without erasing
evidence/history. Installation refuses missing-plist recreation while the job is
still loaded: launchd retains its in-memory definition, so a newly written file
would not establish the intended policy. Owned removal/reinstallation is required;
confirmed-unloaded missing definitions remain repairable. See
[safe upgrade](operations.md#upgrade-installed-mac-jobs-safely).

Status distinguishes registration, session, intent, unknown/unhealthy availability,
maintenance and exhausted/failed recovery. It does not claim remote trusted HTTPS
from loopback readiness or establish reboot/power evidence. [Operations](operations.md#opt-in-mac-startup-and-recovery)
owns owner coordination, verified-backup prerequisites, safe reversal and actual
Mac evidence/acceptance; [configuration](configuration.md#captured-mac-supervision-configuration)
owns the exact captured settings and managed records. Native Linux supervision
and networking decoupling remain later work.

## Startup security gate

Startup verifies the pinned image and checks the **effective Apache PHP runtime**
first in an unpublished disposable container and then in the application itself.
The application initially listens only on container loopback port 8088, **not**
published container port 80. Only after the check passes and cleanup removes all
project probes (`.project-security-<32 lowercase hex digits>.php`), including stale
ones, and verifies none remain does Apache open port 80. Cleanup/query failure
keeps serving closed. The official entrypoint and default command are retained.

When `_start` itself starts a stopped owned VM, a matching **stopped** existing
application is recreated on the same selected named volume. Ownership, port,
pinned-image and disposable effective-PHP preflight precede this decision, as do
all existing application image, binding, entrypoint, default command, internal
`LISTEN` and selected-volume checks. Immediately before removal, another owned
container inspection must equal the original and still be stopped. Removal uses
neither force nor volume deletion; creation follows the existing pinned path.
Foreign, mismatching, changed or running applications never qualify for this cold
recreation. A running application follows the existing restart path instead.

This discards disposable container-layer runtime state across a VM shutdown. The
pinned official entrypoint execs the default Apache command without removing a
stale Apache PID file; this is rationale for a fresh layer, not proof of the
precise failure/corruption mechanism. A warm start (VM already running) retains
container reuse, including partial-template-copy retries. Recreation is not a
fallback after arbitrary security/readiness errors and does not weaken any
post-create PHP/probe-cleanup/readiness/authentication/forwarding gate. Container
layer diagnostics/customizations are discarded and web sessions can expire;
subscriptions, articles and reading state remain in the selected data volume.
A verified backup is still required before significant live changes.

Both checked-in new-user templates are copied while the application is stopped
on every start, including container reuse. Copy failure stops startup; retry copies
both again. Existing users are not rewritten.

After publication, HTTP readiness makes at most 30 attempts with a 2-second
request timeout. URL errors, timeouts and transient connection failures (including
dropped responses/resets) retry after 1 second. Only successful readiness proceeds
to scheduler verification and immediate fetching. Exhaustion retains resources for
diagnosis; general loopback readiness failure does not imply HTTP is closed.
Private/previously private post-start failures have the additional cleanup below.

The sanitized `security.json` records effective web metadata and CLI version
separately. PHP CLI, package and SBOM metadata alone are not effective web-runtime
evidence. Replacing a container can expire PHP sessions without losing accounts
or reading state.

## Image and PHP security policy

The checked-in official Debian FreshRSS image is **1.30.4**, with OCI index
`sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df` and
verified arm64 manifest
`sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797`.
`config/image.json` is authoritative. Historical runtime verification observed
Apache `apache2handler`, **PHP 8.4.27**, with
`session.upload_progress.enabled` disabled in the effective web configuration.
The upstream entrypoint edits Apache and CLI INIs before workers start; the
project does not substitute an application-level `ini_set()` mitigation.

PHP 8.4.27 meets the reviewed October 21 fixed-release minimum but does **not**
fix the additional October 23 upload-progress advisory; disabling upload progress
is required separately. The gate accepts only PHP 8.4 at or above 8.4.27 for the
reviewed image. It is not an automatic vulnerability/advisory scanner. Reassess
security/support before changing or continuing to use an old pin:
[PHP 2026 advisories](https://www.php.net/archive/2026.php),
[supported versions](https://www.php.net/supported-versions.php), and
[official image documentation](https://github.com/FreshRSS/FreshRSS/blob/1.30.4/Docker/README.md).
Loopback and container isolation alone are not production security guarantees.

The index includes other platforms, but only arm64 is verified here. A different
architecture requires resolving its manifest, updating platform/manifest and full
security/integration verification on the destination; that is not an implemented
Linux deployment plan. A restored volume requires its exact recorded configuration
for first successful activation. See [updates and rollback](backup-restore.md#image-updates-and-rollback).

## Private HTTPS and authentication

[Tailscale Serve](tailscale-readkit.md) forwards private HTTPS on the node's port
443 to the sole IPv4 loopback publication. FreshRSS natively honors Serve's
`X-Forwarded-Proto`; `TRUSTED_PROXY=0` disables Apache forwarded-client-IP trust,
not HTTPS URL handling. After PHP/probe checks, an installed instance receives
its HTTPS base URL, API enablement, form login and no anonymous reading/refresh.
Startup reads effective settings back and refuses forwarding if overrides or
unsuccessful application leave any required value wrong. Serving starts only after
scheduler/fetching checks. API credentials remain separate from web credentials.

Preflight reserves the node's **entire HTTPS port 443** exclusively. Unowned routes
(even an identical target), subpath handlers, foreground listeners or Funnel on that
listener cause refusal before VM/application changes. Enabled Funnel routes to this
backend on any port also cause refusal, including HTTP subpaths, TCP forwarding
and foreground/virtual-service configurations. Authorization and handlers can span
node/foreground scopes; foreground handlers take precedence. Virtual services stay
in separate routing namespaces. HTTP numeric targets expand to IPv4 loopback;
IP normalization covers IPv4-mapped IPv6, local empty-host and unspecified IPv4
destinations. Numeric TCP forwards do not use HTTP shorthand. Unrelated services
and private other-port forwarding are preserved. There is no reset/adoption/bypass.

The effective incoming TCP 443 policy preflight is coarse, not proof that each
physical reader is authorized. LocalAPI/debug compatibility and real HTTPS must
be checked on the installed version; isolated tests are not live-service evidence.

### Conditional forwarding ownership

Mutations read configuration and its ETag together, validate ownership, then write
through the existing daemon's LocalAPI with that exact `If-Match`. Concurrent
changes cause refusal without automatic retry/global rollback. The project lock
cannot serialize other Tailscale clients. Unrelated configuration is copied unchanged.

`tailscale debug local-creds` discovers the existing daemon's loopback credentials
or Unix socket. Sensitive output stays in memory and is never shell-executed.
Requests remain local, bypass proxies and refuse redirects. Missing credentials,
ETag, unsupported output or denied access fail closed; there is no unconditional
CLI fallback. The debug/netmap formats are version-dependent.

- **Start:** write private pending intent before creating forwarding. A definitive
  rejection (HTTP 401/403, 412, invalid input or connection failure before
  transmission) removes the new intent before restoring a preceding record. If
  restoration fails, e.g. ENOSPC writing `serve.tmp`, no new authority remains;
  report storage/ownership guidance and require local inspection. Do not adopt an
  identical competing route or roll back remote configuration. Uncertain writes
  retain pending intent for recovery.
- **Stop:** persist inactive intent before requesting removal; persistence failure
  prevents the remote request. Definitive refusal restores preceding intent.
  Confirmed/uncertain removal retains inactive intent even if read-back verification
  fails. It cannot authorize adopting/deleting a possibly recreated identical
  listener. Verification errors fail the command and do not prove overall shutdown.
- **Transport:** establish TCP/Unix connections separately from transmission.
  Failed connection is a definite refusal; send/response failures are uncertain
  regardless of exception type, including `ConnectionRefusedError` after sending.
  Inspect local Serve status and resolve availability/ownership before retrying,
  rather than hand-editing records or rolling back someone else's configuration.

### Post-start failure cleanup

For private or previously private instances, application startup and subsequent
PHP/readiness/scheduler/fetching/authenticated-access/forwarding gates share
ownership-checked cleanup. Failure, including a late Funnel conflict, attempts to
stop the application without changing foreign forwarding, data or VM. This also
covers authenticated-access failure during loopback fallback: preserved private
other-port routes must not expose a backend whose authentication gate failed.
Cleanup needs no external-backup destination and retains uncertain-write intent.

If ownership or shutdown cannot be confirmed, the error explicitly warns that the
backend may still be exposed; inspect and close it locally. This is not protection
against arbitrary later changes by other clients, and does not extend cleanup to
failures before application startup. Initial preflight refusals are nonmutating;
first-run unconfigured loopback behavior is unchanged. Tailscale itself and
certificates are never removed/stopped by these commands.

## Daily backup orchestration

`backup_schedule.py` owns daily eligibility, launchd configuration and private
attempt/publication/freshness evidence around `Recovery`, not a second archive
format or export engine. CLI remains a thin adapter; `Runtime.run` serializes
callable and CLI operations under the existing nonblocking `command.lock`.
Normal public stop routes through `BackupSchedule.shutdown`, retaining attempt,
publication and maintenance evidence around `Recovery.shutdown`. launchd uses the
stable selected checkout's installed Python with isolated imports and captured
absolute settings/dependency paths, without an interactive shell or uv downloads.
The UTC daily policy and bounded retries are detailed in the
[backup procedure](backup-restore.md#opt-in-daily-mac-backups).

Temporary maintenance and persisted operator intent are deliberately separate.
A durable, owner-tagged hold precedes quiescing, covers export/publication and
prior-mode resumption, and survives process death. Short-lived Runtime subprocesses
inherit the locked descriptor: an orphaned export retains serialization even after
its parent is SIGKILLed. Only VM startup disables descriptor inheritance to its
long-lived helpers; the parent retains the lock through that launcher call. Public
writer operations and supervision refuse any `maintenance.json` presence, including
malformed data or dangling links, rather
than infer that a stopped container is an application failure. Backups
never write intent, and never resume persisted stopped intent. A failed/uncertain
capture cannot authorize automatic writer recovery.

Reconciliation is an explicit locked, ownership-checked idle-engine operation,
not a timer/PID-based stale-marker cleanup or automatic resume. Clearing a hold does
not start anything, but retained running intent permits a later supervisor tick.
To remain stopped, public stop must persist stopped intent before reconciliation,
even when maintenance refuses its shutdown work. Ordinary completed
shutdown refusals retain the pre-existing forwarding/retry semantics; abrupt death
still retains maintenance. Private JSON writes flush/fsync before atomic replace
and directory fsync. Archive publication remains verified, nonoverwriting hard-link
publication with directory fsync. No new pruning/export/transfer engine is introduced.

Latest attempt, verified publication and safe resumption are separate facts; a
published archive survives failed resumption, and an interrupted/missed/contended
job is never success. `backup-status` can inspect atomic records read-only while
busy. Scheduler registration and manual archives cannot stand in for eligible
daily work or actual unattended evidence. An available local archive is not proof
of Time Machine inclusion/retrieval or host-loss recovery. Existing full-state
live tests remain explicit selections, not default-suite proof of infrastructure.

## Preview-first removal and generation retirement

Issue #5 adds three operations to the existing thin CLI/`Runtime.run` boundary,
sharing `command.lock` and maintenance admission rather than a parallel teardown
engine. `cleanup.py` handles VM removal/inactive-volume deletion and
`package_cleanup.py` handles per-formula eligibility. No new runtime settings,
background service or automatic sweep is introduced. Native read-only preview
binds explicit consent to the current private plan; it does not export, stop,
create a destination or rewrite records (the existing lock is opened for writing).

VM removal requires a running accessible owned machine, exactly the selected
labelled volume, only reserved owner-labelled app/preflight containers and pinned
images, and no rootful containers/volumes/images. Both inventories include external
build containers and intermediate images. Both engines must have no secrets, OCI
artifacts, pods, build caches or custom networks: the selected-volume archive covers
none. Explicit SSH username is cross-checked against owned machine/connection
metadata; rootful commands use noninteractive sudo. Each guest engine's `info` must
attest local mode and expected privilege, non-transient storage, guest Podman 6.2.0
and Netavark before inventory. `--remote=false` alone is insufficient if guest
configuration selects tunnel mode. Secrets use local `secret ls --quiet`, artifacts
`artifact ls --quiet`, pods `pod ps --quiet`: only successful, diagnostic-free,
exactly empty stdout establishes absence. Secret `--format json` is literal template
text, not a JSON mode; the remote secret tunnel can discard API errors. No secret
payload is requested; unrelated names/native diagnostics never enter reports or
receipts. A name-only guest `find` also requires secret-manager storage to contain
only its native lockfile, refusing even empty/stale databases and driver remnants
because native secret loading can swallow a failed database stat. Buildah mount
cache names are inspected in the effective image-copy temp directory and stock
`/var/tmp`/`/tmp`; no cache contents are read.

The only disposable networks are verified *synthetic stock defaults*, listed under
both engine contexts in preview targets. Require the full stock bridge configuration,
fixed ID, subnet/gateway, disabled DNS/IPv6/internal flags and host-local IPAM, with
no extra keys. Two independent local listings must show strictly increasing valid
`created` timestamps: [Netavark 6.2.0](https://github.com/containers/podman/blob/v6.2.0/vendor/go.podman.io/common/libnetwork/netavark/network.go#L198-L337)
synthesizes an absent default with `time.Now()` per process, whereas a persisted
replacement retains its timestamp even if it copies the name/ID/configuration.
Force guest `--log-level=warn` and reject every network warning too: the native
loader can skip malformed/unreadable configuration files while reporting success,
and configured logging must not suppress that evidence. This avoids guessing the effective
network configuration directory. Other guest versions/backends, uncertain clocks,
custom/persisted defaults and failed/malformed inspection retain the VM; there is
no override to convert uncertainty into deletion authority.

These checks repeat at preview, apply admission and post-capture plan inspection.
They cover registered resources in the two current engine contexts and the stated
cache paths, not historical alternate stores/cache locations, other users' engines,
or arbitrary guest files. Manifest lists and image build caches are image objects;
empty pods, external build containers and standalone mount caches require the
separate checks above. Machine/rootless and derived-root connection identities are validated.
Foreign connections/defaults remain unchanged; a project default among other
connections refuses instead of letting native removal select another default.
Captured jobs must first be removed by supervision-remove/backup-disable. Confirmation
binds ownership, immutable resource/image identity, destination and connection
inventory; changes require a newly reviewed preview, not implicit adoption.

Apply reuses lifecycle quiescing and exact owned-forwarding cleanup, durable stop
maintenance and the existing full-instance snapshot engine. It independently
revalidates fresh external archive bytes/checksum and content, then rechecks the
plan. After the final inventory/receipt, a read-only check of both engines must still
establish idle writers immediately before VM shutdown; resource identity alone does
not establish continued quiescence. A resumed writer or inspection failure retains
the VM/data and maintenance, without stopping the writer to reuse stale capture.
Guarded reconciliation and a new preview/apply with fresh quiesced capture are required.
The project lock does not serialize independent native clients. Successful idle
inspection precedes VM stop and native `machine rm` with confirmation stdin. No
old archive, `no_data`, force, global reset/prune or unavailable-source override
permits deletion. Export/validation failure before deletion preserves source and
prior archives; interrupted/partial removal retains maintenance for inspection.

Private `removed-vm.json` version 1 records `status: removing`/`removed`, exact
`owner`, `plan`, fresh `backup` and `foreign_connections` before native stop/rm.
Completion requires absent exact machine/connections and unchanged foreign
identities/defaults. If Python dies after rm, the same operation may finalize only
the exact owned receipt and matching `stopping_vm` hold after independently verifying
the recorded archive. Other absent/partial/mismatched states are not authority to
clear records or broaden deletion. Ordinary repeated absence reports absent only.

After successful removal, setup retires **only the exact old generation**: owner,
selection/provenance, security, Serve, operator intent and supervision recovery
records move to private `retired-<hash>/`. Baseline/additions, backup locators/evidence,
receipt and checkout remain. Absent resource names then receive fresh ownership;
reappearing names refuse adoption. Restore to a fresh explicit volume **before any
empty app start**: setup, restore (may start only the owned VM), select-volume, start
with the recorded compatible image. Use a fresh name, not the initial selected
`rss-aggregator-data` even when absent, and verify the old original volume is absent.
Retirement is not ownership fabrication or a cache sweep.

Permanent volume deletion is a distinct irreversible consent boundary: one explicit
labelled **unselected** volume, zero mounts and no attachments even to stopped
containers. It creates no backup and invokes nonforce native removal, preserving
all other resources/archives. Package removal similarly targets one plain formula:
initial baseline plus additions ledger, unchanged installed version, fresh recursive
installed formula/cask optional/build/test reverse dependencies and no Podman
machines/connections. Apply requires both the exact current-plan digest and operator
unused-elsewhere attestation; metadata cannot prove external adoption. Normal native
`brew uninstall --formula` retains dependent checks, without force/ignore-dependencies/
autoremove/cleanup. Podman goes first; each remaining addition needs a fresh preview.

Backups/audits remain private; manual exact-file double review is the only archive
removal procedure. No recursive directory/cache cleanup is provided. See
[operations](operations.md#safe-cleanup) for unavailable-VM preservation/provider
recovery and interrupted-removal handling, and [configuration](configuration.md#managed-local-records)
for records. Deterministic guards are not real disposable VM remove/recreate/
authenticated-state acceptance, native host uninstall or sentinel-preservation proof.
Select the [native drill separately](development.md#cleanup-verification-and-remaining-native-evidence)
and record revision-specific outcomes in the issue/PR. Its retained machine-name
and connection/default comparisons do not establish full preservation of every
unrelated resource or native package-uninstall behavior.

## Decisions

- [ADR 0001: isolate local development with Podman](adr/0001-isolate-local-development-with-podman.md)
- [ADR 0002: portable state and consistent external backups](adr/0002-portable-state-with-consistent-external-backups.md)
- [ADR 0003: private Tailscale access and shared reading state](adr/0003-private-tailscale-access-and-shared-reading-state.md)
