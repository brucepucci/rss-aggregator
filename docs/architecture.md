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
falling back to another checkout or adopting resources.

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

## Decisions

- [ADR 0001: isolate local development with Podman](adr/0001-isolate-local-development-with-podman.md)
- [ADR 0002: portable state and consistent external backups](adr/0002-portable-state-with-consistent-external-backups.md)
- [ADR 0003: private Tailscale access and shared reading state](adr/0003-private-tailscale-access-and-shared-reading-state.md)
