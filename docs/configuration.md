# Configuration reference

Runtime settings, checked-in defaults and managed local records. See the
[operator guide](../README.md), [CLI reference](cli.md),
[operations/troubleshooting](operations.md), [backup and recovery](backup-restore.md)
and [private access](tailscale-readkit.md). Developer-tooling configuration is in
[development](development.md#development-configuration); effective-PHP security,
image review and startup internals are in [architecture](architecture.md).

## Secrets and local handling

No secret setting is required in the repository. Required FreshRSS user credentials
are entered locally after security verification and stored as upstream requires in
the private VM data volume. Private HTTPS/API use additionally needs a separate
per-user API password, set locally in FreshRSS **Profile**. Neither password nor a
private feed URL belongs in source files, shell arguments, logs, screenshots,
issues or chat. Private feed URLs are credentials, even when they look like links.

Keep backups and generated application settings out of Git. `.local/`, `.env`,
`.env.*`, SQLite files and `backups/` are ignored, but ignore rules do not replace
inspection of staged files. The runtime reads the process environment; it does
not itself load `.env` files. Export supported settings in the controlling shell
and keep any local shell configuration private.

JSON ownership/inventory/security records use private file modes. New state
directories are created with mode `0700`; preserve private permissions on an
existing directory yourself. Permissions are **not encryption**. Treat VM storage,
archives, application logs, host inventories and private forwarding URLs as sensitive.
The named data volume is not a backup, and `.local/` is not a disposable tool cache.

## Environment settings

| Setting | Required/default and purpose | Change and apply |
| --- | --- | --- |
| `RSS_PORT` | Optional; `8080`. Mac IPv4 loopback TCP port, integer `1024–65535`. No LAN/IPv6/all-interface publication. | Use consistently for operational commands. For an installed instance, take a verified backup with the existing selectors and successfully remove supervision/disable daily backups **before** changing the base URL or port; both jobs retain the captured old port. Follow [Changing the loopback port](#changing-the-loopback-port) for guarded replacement, verification and explicit optional job recapture. Invalid values refuse startup. |
| `RSS_TAILSCALE` | Optional; `0` (loopback-only). Exactly `1` enables private Serve HTTPS on fixed node port 443; any other value fails. Requires an initialized FreshRSS instance, connected existing Tailscale, enabled HTTPS certificates and incoming TCP 443 authorization. | Keep set for `start`/`verify`/`replace`. `stop` removes recorded owned forwarding regardless of this value. Explicit `backup` restores the observed prior forwarding mode, even after snapshot failure, rather than applying this shell's opt-in. **Before enabling private HTTPS**, obtain a verified backup and successfully remove supervision/disable daily backups with existing selectors; old loopback capture can undo private recovery or omit Tailscale dependencies. Follow [guarded private activation](tailscale-readkit.md#connect-and-verify-the-server), verify private access, then [explicitly recapture only wanted jobs](tailscale-readkit.md#optionally-recapture-the-mac-jobs) with `1` and intended dependencies/settings/UTC policy. For local-only use, remove owned supervision **before** changing mode: its captured `1` can reopen forwarding despite this shell's `0`. Preserve the stable checkout, existing `RSS_STATE_DIR` and `RSS_PORT`, then set `0` and run guarded `start`/`verify` to remove the owned route and return the installed instance's base URL to loopback; API enablement/passwords remain. Follow the [return-to-local-only procedure](tailscale-readkit.md#stop-resume-and-return-to-local-only-use); optional supervision reinstallation must explicitly capture the intended settings as [documented below](#captured-mac-supervision-configuration). |
| `RSS_BACKUP_DIR` | Required for explicit/daily backup, shutdown with data and `remove-vm`; **no default**. Absolute host directory outside this and every other Git checkout. New directory mode `0700`, archives `0600`; an existing directory must be owned by you with no group/other permissions. Destination filesystem must support hard links for atomic non-overwriting publication. | Export before `backup`/`stop`/`backup-enable`/`remove-vm`, as below. VM-removal preview validates the resolved private destination without creating it; confirmation binds that directory, and apply must publish and independently validate a fresh full archive there. The owner's approved deployment value is explicitly `$HOME/FreshRSS-Backups`, not an implicit default. Daily jobs capture the resolved value: disable/re-enable to change it. Changing it for manual commands chooses a new destination; existing archives are not moved, pruned or uploaded. Not required to validate/restore an archive, or to repeat shutdown of an already-stopped VM. |
| `RSS_STATE_DIR` | Optional; `<source-checkout>/.local`. Ownership, inventory, lock, recovery-selection and security records; **not** the FreshRSS database location. | Prefer an absolute private path, never tracked by Git; choose any custom location outside Git. Set before setup and use consistently. The implementation expands `~` and resolves relative values against the process working directory; absolute paths avoid ambiguity. Changing it does not adopt or migrate an existing machine. |

This deployment's explicitly approved destination is below, not an automatic
global default. Other deployments must choose their own private durable location:

```sh
# Explicitly approved for this deployment; never created merely by reading config:
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
```

A local archive on the same Mac does not protect against losing that Mac. Keep
independent durable copies. Failure handling differs between explicit backup and
shutdown; follow the [recovery guide](backup-restore.md).

For an instance controlled by another worktree, explicitly set `RSS_STATE_DIR` to
that checkout's existing private ownership directory before operating it. Worktrees
do not namespace Podman resources. Keep a single controlling checkout; never copy,
replace or fabricate ownership records to adopt resources. Preserve the ownership
directory when relocating a checkout.

## Captured Mac supervision configuration

Supervision is optional and disabled until explicit `rss supervision-install`.
Read the [backup/owner-coordination safeguard](operations.md#enable-or-remove-supervision)
before activation. There are no additional user-configurable retry/schedule flags:
the owned job checks at load and every 60 seconds in the normal user GUI session;
three consecutive recovery attempts use minimum 60s/300s backoff. Sleep/login/unlock
and real power-recovery behavior are limitations, not settings changed by this app.

Installation captures absolute checkout, ownership directory, checkout-local
`.venv/bin/python`, image-configuration SHA-256 and executable locations for
`launchctl`, `podman` and `tailscale` when private HTTPS is enabled **or a retained
`serve.json` exists**, including inactive forwarding after a return to loopback.
Previously private health inspection still queries Tailscale, and guarded startup
uses its LocalAPI for ownership-checked cleanup. Loopback mode does not remove
that dependency or authorize deleting forwarding ownership evidence. Child `PATH`
comprises those executable directories plus `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`;
`HOME`, resolved `RSS_STATE_DIR`, effective `RSS_PORT` and `RSS_TAILSCALE` are
explicitly captured. `RSS_BACKUP_DIR` is captured only if explicitly set, expanded
and absolute; it still has **no implicit default**. This deployment's owner-selected
`~/FreshRSS-Backups` must be exported explicitly. The job does not copy arbitrary
environment variables, credentials or shell initialization and does not load `.env`.

`RSS_SUPERVISION_ROOT` is an internal captured identity guard, not a user deployment
selector; do not set it manually. A tick uses captured settings even if a manually
invoking shell has different selectors. Status refuses moved/missing code, changed
image configuration, missing executable/dependency lookup and changed owned jobs
instead of choosing another installation. Keep `.venv` synchronized in the stable
checkout. For deliberate settings/pin/path changes, take a verified backup, remove
supervision, apply/verify the change, then explicitly reinstall with the intended
settings. Updating code does not rewrite an existing job's captured dependencies;
use explicit removal/reinstallation to recapture them. No package installation or
image update is performed by registration.

The supervision service definition is
`~/Library/LaunchAgents/io.rss-aggregator.supervision.plist` (new file `0600`),
loaded into `gui/<uid>` rather than privileged/system launchd. Its arguments select
Python isolated mode (`-I -B -m rss_aggregator supervision-tick`), fixed working
directory and non-secret environment. Job stdout/stderr go to `/dev/null`; use
sanitized `supervision-status` and persisted recovery outcomes, not raw sensitive
application logs. Changed/foreign/symlinked job definitions are preserved/refused.
Removal preserves app, ownership, intent, backup copies and all unrelated jobs.
Both job definitions set fixed `AbandonProcessGroup=true` (no CLI/environment
override). This prevents launchd from killing legitimate descendants when the
bounded parent exits; it is separate from command-lock descriptor inheritance and
does not authorize startup or adoption. Definitions installed before this policy
are not rewritten automatically. Use the [safe job upgrade procedure](operations.md#upgrade-installed-mac-jobs-safely):
remove with matching old code before update, or use the fixed ownership-validated
removal path, then deliberately recapture only wanted jobs. Supervision recognizes
the exact pre-fix definition only for removal, never recovery/installation.
If its plist is missing while the job is loaded, install refuses without recreating
it: writing a new file cannot update launchd's retained policy. Remove the owned
registration, then reinstall; missing-file recreation is allowed only when unloaded.

## Daily backup configuration

No new environment default enables scheduling. Explicit `backup-enable` captures
`RSS_STATE_DIR`, resolved `RSS_BACKUP_DIR`, `RSS_PORT` (8080 when omitted),
`RSS_TAILSCALE` (0 when omitted), normal-user `HOME`, the absolute selected checkout
and its own `.venv/bin/python`. It captures absolute Podman/launchctl and required
Tailscale paths and builds PATH only from their directories plus standard system
bins. It does not inherit arbitrary shell state, aliases or `.env`. Keep executables
and installed code available; status detects missing/mismatching selected code or
captured dependencies. No dependency installation or host setting change is implicit.

The explicit `--hour`/`--minute` policy defaults to **03:00 UTC**, with launchd
15-minute wakeups/RunAtLoad and at most three actual attempts per daily target.
Freshness is **26 hours**, with missed-attempt reporting after 15 minutes; these
are fixed policy constants, not environment overrides. A private `0700` owned state
directory is required for enable. The private `0600` job is
`~/Library/LaunchAgents/io.rss-aggregator.backup.plist`, in the normal user's GUI
session. Existing job conflicts are refused. Use `backup-disable` then enable to
apply deliberate captured-setting/checkout/dependency-path changes; never hand-edit
managed records or move archives/ownership implicitly. Follow the
[owner-coordinated setup and recovery safeguards](backup-restore.md#opt-in-daily-mac-backups).
Linux scheduling, pre-login availability, wake/power changes and external alerts
are unsupported. Installed configuration is not successful unattended protection.
The backup job and supervision job are separately opt-in; neither installs the other.
The same fixed process-group abandonment policy applies to backups. Its changed
job digest changes `policy_id`; existing evidence and archives are retained but
new policy-specific daily evidence is required. Re-enable with the previously
selected destination and UTC hour/minute, not unintentionally reset defaults.

## Checked-in application configuration

| File | Current purpose/default | Change and apply |
| --- | --- | --- |
| [`config/image.json`](../config/image.json) | Official Debian FreshRSS `1.30.4`, digest-pinned OCI index, explicit `linux/arm64`, platform manifest, source revision, advisory-review date and PHP minimum. Required for runtime/recovery operations. | Review current advisories and official registry identity/runtime before changing. Follow [image updates and rollback](backup-restore.md#image-updates-and-rollback): take a verified backup and successfully remove owned supervision **before** pin mutation; apply the reviewed change with guarded `replace`/`verify` and check application/state, then explicitly reinstall only if wanted and require healthy supervision with the new capture. An old job retains the image hash and refuses recovery after a pin change. No floating-tag or unattended update command. Recovery requires the recorded matching configuration through first activation; see below. |
| [`config/config-user.custom.php`](../config/config-user.custom.php) | New-user defaults: `archiving.keep_period=false`, `archiving.keep_max=false` (no age/maximum-article purge), `ttl_default=60` seconds. | Every `start`, including through `verify`/`replace`, copies it into the stopped application's data volume. Both templates are recopied on retry, even with an existing container. **Existing users are not rewritten**; change their settings in FreshRSS and check feed/category overrides. |
| [`config/public-feed.opml`](../config/public-feed.opml) | Exactly one initial public subscription: the [FreshRSS releases feed](https://github.com/FreshRSS/FreshRSS/releases.atom), replacing upstream's default subscription list. | Copied to `data/opml.xml` on every start while stopped; existing subscriptions remain unchanged. Enter private feeds locally, never in this file. Private/paid-feed authorization acceptance remains unverified. |
| [`config/security-probe.php`](../config/security-probe.php) | Temporary runtime-metadata-only Apache PHP check, not a user setting or permanently served endpoint. | Keep aligned with reviewed security policy. Startup removes all exact project-generated probe names before opening the application listener; do not bypass this gate. See [architecture](architecture.md). |

`image.json` fields are all part of the exact recovery image configuration:

| Field | Purpose/current value |
| --- | --- |
| `image` | `docker.io/freshrss/freshrss@sha256:…` OCI index reference; full checked-in digest is authoritative. |
| `version` | FreshRSS OCI version label, `1.30.4`. |
| `platform` | Explicit OS/architecture, `linux/arm64`. |
| `platform_manifest` | Verified architecture-specific `sha256:…` manifest, not interchangeable across architectures. |
| `revision` | Expected official OCI source-revision label. |
| `security_checked` | Recorded advisory-review date, `2026-11-14`; metadata, not a current advisory scan. |
| `php_minimum` | `8.4.27`; the effective runtime must match its major/minor line and meet the minimum. Separate effective-web checks also apply. |

Use the full values in the checked-in file, not the ellipses above, for review.
Only arm64 runtime verification is recorded. A restored volume must first complete
security/readiness/scheduler/fetching startup with the exact configuration recorded
in its archive. Merely selecting or backing it up does not clear that guard;
legacy provenance without an activation marker also needs a matching start.
After first activation, intentional reviewed image updates are supported. Database
rollback requires a pre-update backup and matching image in a fresh volume, not an
assumed downgrade. See [recovery](backup-restore.md) and the
[image/PHP security policy](architecture.md).

## Fixed runtime resources

These values are implementation settings, not environment overrides or CLI flags.

| Setting | Current value and application |
| --- | --- |
| Podman VM/connection | `rss-aggregator`, rootless. Initial 2 CPUs, 2048 MiB RAM, 20 GiB virtual disk capacity; capacity is not immediate physical disk use. No project resize command; preview-first `remove-vm` is the guarded deletion boundary, not `stop`. Machine/SSH/connection identity is ownership-checked. |
| Application / preflight container | `rss-aggregator-freshrss` / disposable `rss-aggregator-check`; ownership label `io.rss-aggregator.owner`. Do not relabel resources to force adoption. |
| Initial data volume | `rss-aggregator-data`, mounted at `/var/www/FreshRSS/data` on the VM's native filesystem. Change active selection with `rss select-volume NAME`, not by editing records. |
| Publication / restart | `127.0.0.1:${RSS_PORT}:80`, `--restart=no`. Official image entrypoint/default command retained. No host cron or default service registration. User-session supervision (`supervision-install`) and daily backups (`backup-enable`) are separately opt-in, independent of container restart policy. |
| Container environment | `CRON_MIN=*/20`, `TZ=UTC`, `ENABLE_ACCESS_LOG=0`, `COPY_LOG_TO_SYSLOG=Off`, `COPY_SYSLOG_TO_STDERR=Off`, `TRUSTED_PROXY=0`, initial internal gate `LISTEN=127.0.0.1:8088`. Apply reviewed changes with `replace`. |
| Homebrew safeguards | Setup child commands force `HOMEBREW_NO_AUTO_UPDATE=1`, `HOMEBREW_NO_INSTALL_UPGRADE=1`, `HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1`, `HOMEBREW_NO_INSTALL_CLEANUP=1`. Not user bypass switches; setup refuses an upgrade/reinstall plan. `remove-package` deliberately retains native uninstall dependent checks (does not inherit the disabled-check setting), rechecks recursive installed formula/cask dependencies and runs only normal `brew uninstall --formula`. |

Fetching runs immediately at startup and at UTC minutes **00, 20, 40**, not 20
minutes after start. The project explicitly enables upstream's otherwise-disabled
cron and checks the installed job plus live daemon. The new-user TTL does not
override upstream caching/backoff; see [scheduling and reading state](operations.md#scheduling-and-reading-state).
Client synchronization is not feed fetching.

`TRUSTED_PROXY=0` disables Apache forwarded-client-IP trust, not FreshRSS's native
handling of Serve's `X-Forwarded-Proto`. Private access retains loopback publication;
see [architecture](architecture.md) for security/startup details.

## Managed local records

Paths below are under `RSS_STATE_DIR` (default `.local/`). They are generated state,
**not configuration to hand-edit**. Preserve them privately; audit inventories are
not permission to uninstall packages that other projects may later use.

| Record | Purpose and handling |
| --- | --- |
| `owner.json` | Owner token plus machine/connection identity, tied to container/volume labels. Never delete, copy, fabricate or replace it to adopt existing resources. Loss needs a deliberate recovery decision, not blind setup. After receipt-confirmed successful VM removal only, `setup` retires this exact generation and creates a fresh owner for absent resources. |
| `command.lock` | One nonblocking lock shared by CLI and callable runtime operations, including supervision and daily backups. Busy `backup-status` alone may return an atomic read-only view. Short-lived native children (including exports/imports) inherit the held descriptor, retaining the lock after parent death until native work exits. Only `podman machine start` in startup and recovery disables inheritance so long-lived gvproxy/krunkit helpers cannot retain the lease indefinitely; the parent holds the lock throughout the launcher call. This private exception has no CLI/environment setting and does not bypass durable maintenance. Another active operation causes refusal. It does not serialize independent Podman or Tailscale clients. |
| `operator-intent.json` | Version 1, `intent: running` or `stopped`. Only public `start`/`stop` write it: stop before maintenance refusal/shutdown, start only after its maintenance guard passes (even if subsequent startup fails). Absence means unset/no automatic startup; backups may preserve legacy observed state but never authorize supervision. Persists through login/reboot; backups only read it, never change it or resume stopped intent. Invalid records fail closed. Do not hand-edit it to authorize recovery. |
| `supervision.json` | Versioned owner-bound registration and captured non-secret checkout/configuration/executable/job identity. Retained on partial activation/removal failure. Use install/remove, not manual ownership adoption. |
| `supervision-recovery.json` | Versioned consecutive attempt count (0–3), next-retry UTC Unix timestamp/null and last ten sanitized attempt outcomes. Healthy tick or explicit start resets attempts/backoff, not history. An interrupted/in-progress event is not successful recovery. |
| `serve.json` | Owner token, private hostname/backend and pending/active/inactive intent; no passwords/API tokens. Preserve alongside ownership. Exact-match and conditional LocalAPI checks reject changed/unowned routes. Rejected starts grant no new authority; inactive intent does not authorize adopting a reappearing identical listener. Resolve uncertain outcomes locally, never hand-edit/copy/adopt. Detailed intent/ETag failure semantics are in [architecture](architecture.md) and [private access](tailscale-readkit.md). |
| `selected-volume.json` | Explicitly selected owned data-volume name; absence means `rss-aggregator-data`. Use `rss select-volume NAME` with running VM/stopped application. |
| `recovery-volumes.json` | Per-volume image provenance, import completeness and completed-first-activation marker. Never edit incomplete imports into ready state or bypass activation markers. Backup refuses unattached volumes without known matching provenance; start with the reviewed matching pin first. |
| `removed-vm.json` | Private version 1 removal receipt: `status: removing` or `removed`, exact `owner`, preview `plan`, fresh verified `backup`, retained `foreign_connections`. Written before VM stop/removal; survives failure and successful recreation as audit evidence. Absent-resource completion requires the exact owner/receipt, matching `stopping_vm` maintenance, independent archive verification and unchanged foreign connections/defaults. Never edit/delete it to force setup or clear a hold. |
| `retired-<hash>/` | Private `0700` directory retaining only the exact successfully removed generation's `owner.json`, selection/provenance, security, Serve, operator-intent and supervision-recovery records. `setup` retires these before fresh ownership; it preserves host baseline/additions, backup locators/evidence, receipt and checkout. Ambiguous/reappearing identities refuse rather than adopt. No owner record fabrication or automatic directory sweep. |
| `last-backup.json` | Most recently published archive location and whole-ZIP checksum. Useful if a later resumption/operation step fails; not a substitute for archive verification or durable storage. |
| `backup-schedule.json` | Version/owner, selected checkout/state/Python/job paths, UTC hour/minute, captured environment and absolute dependencies, job SHA-256, policy identity (definition plus UTC target), destination and activation state. Not user-editable; disable/re-enable for changes. |
| `backup-attempt.json` | Latest manual/daily/normal-stop attempt start, kind, outcome and resumption/action. Running plus a free lock is interrupted, never successful. Secret-safe fixed diagnostics, no engine stderr/content. |
| `backup-success.json` | Latest verified publication result, completion time and selected volume. Written before resumption, retained after later failures. Availability/freshness is not Time Machine inclusion or proof of unattended invocation. |
| `backup-daily.json` | Today's/most recent eligible UTC target, captured policy identity, attempt count/time, volume and running/success/failed outcome. At most three actual tries per target; never a backlog of older daily targets. |
| `backup-defer.json` | Most recent contention time/action, independently atomically recorded without overwriting an active attempt. Deferral is not success. |
| `maintenance.json` | Shared durable version 1 hold: owner token, operation (`backup`/`stop`), UTC start and phase (`preparing`, `quiescing`, `snapshot`, `resuming`, `stopping_vm`). VM removal reuses the stop lifecycle hold and preserves it on interrupted/partial removal. Only exact absent-resource receipt completion may finish a matching removal hold; otherwise inspect and reconcile safely. **Any presence**, including malformed data or dangling links, blocks writer commands and supervision. Never remove by hand. `backup-reconcile` validates owner/schema and idle engines, clearing the hold without starting or changing intent; running intent permits a later supervisor tick. Run public `stop` before reconciliation to remain stopped. See [interruption recovery](backup-restore.md#interrupted-maintenance-and-reconciliation). |
| `security.json` | Last sanitized image/effective-web/CLI-PHP check, which may identify the disposable preflight rather than the app after a failed start. Past audit metadata is not proof of current readiness or a current advisory scan. |
| `host-before.json`, `host-latest.json` | First successful host inventory and most recent later inventory, recorded before installation. `packages` maps formula names to installed-version strings; `casks` is newline-delimited `name version` text; `dependencies` and `podman_formula` retain raw Homebrew output. Failed inventory does not replace previous records. |
| Cask fallback inventory fields | Only when `brew list --cask --versions` fails: `cask_names` retains raw newline-delimited names and `cask_inventory_warning` retains failed query, exit code and original stderr. `casks` contains only known installed versions from JSON, never latest available versions; incomplete names can exist only in `cask_names`. Empty inventories are valid. No manual configuration/bypass; see [operations](operations.md). |
| `install-preview.json`, `host-additions.json` | Proposed Podman CLI installation output and cumulative recorded added formula versions (including partial installation additions). No automatic package uninstall; `remove-package` uses the initial baseline plus additions, unchanged current versions, fresh reverse dependencies/Podman inventories and per-candidate unused-elsewhere attestation/current-plan confirmation. Never edit ledgers to manufacture eligibility; inspect locally before resolving an approval refusal. |
| `podman-before.json`, `podman-latest.json`, `podman-pre-machine.json` | Initial/later machine and connection inventory, and inventory after CLI bootstrap before machine creation. Running machine connections include reachable container/volume/image inventories; stopped machines and non-machine connections are recorded without being started/contacted for resource enumeration. |

Cleanup introduces **no new runtime settings**. `--confirm` and
`--unused-elsewhere` are per-invocation consent, not environment overrides. See the
[cleanup runbook](operations.md#safe-cleanup) for unavailable-source recovery,
separate irreversible unselected-volume deletion and manual exact-file archive
deletion. Audits/retired records remain sensitive and private; permissions are
not encryption, and environment removal never prunes them automatically.

## Changing the loopback port

For an **already installed, running** loopback instance, the example below moves
to port 8081. Coordinate with the owner: both installed jobs retain their captured
`RSS_PORT`. Changing only the shell/container can make a later daily backup publish
a valid archive but fail resumption, leaving a maintenance hold and the app stopped.
Remove/disable the jobs before any base-URL or port mutation, then recapture only
those explicitly wanted. See the [supervision safeguards](operations.md#enable-or-remove-supervision)
and [daily-backup safeguards](backup-restore.md#opt-in-daily-mac-backups).

1. Use the same synchronized **stable controlling checkout**, existing
   `RSS_STATE_DIR`, old `RSS_PORT`, `RSS_TAILSCALE=0` and explicitly chosen private
   `RSS_BACKUP_DIR` throughout preparation. From another worktree, export the
   original ownership directory; never copy or fabricate records. Preserve these
   paths and old settings privately for diagnosis/recovery. Before removal, record
   whether each job was enabled, its captured settings/destination and the intended
   daily **UTC** hour/minute. Inspect the existing private `backup-schedule.json`
   (`hour`, `minute`, `timezone`) or retained enable result; status does not expose
   the hour/minute policy directly. If later re-enabling, set shell variables
   `SAVED_UTC_HOUR` and `SAVED_UTC_MINUTE` to those recorded numeric values, not
   automatically to the 03:00 default. Do not edit the record or enable a job merely
   to inspect it.

   With the **old** selectors still exported:

   ```sh
   ./scripts/rss status && ./scripts/rss supervision-status &&
     ./scripts/rss backup-status && ./scripts/rss backup &&
     ./scripts/rss status && ./scripts/rss backup-status
   ```

   Continue only after successful inspection and backup with `verified: true`;
   retain the private archive/checksum. Confirm the app resumed normally before
   proceeding. A fresh manual archive is a recovery point, **not** proof of daily
   protection or independent host-loss protection. Plain `backup-status` can exit 0
   while unhealthy; inspect its JSON, including resumption and maintenance.
2. Still with those old selectors, remove both owned jobs (safe no-ops if absent):

   ```sh
   ./scripts/rss supervision-remove && ./scripts/rss backup-disable &&
     ./scripts/rss supervision-status && ./scripts/rss backup-status
   ```

   Require successful removal: supervision returns `enabled: false`,
   `state: "disabled"`; backup disable returns `enabled: false`. Confirm status
   still shows supervision disabled and backup `enabled: false`, `registered: false`.
   These operations preserve app, VM, intent, data, archives and backup evidence;
   they remove the owned registrations, not the instance. **Stop on any error**,
   contention, changed/unowned job or uncertain removal; if only one removal
   succeeds, leave it removed and resolve the other before continuing. Never
   hand-edit/delete jobs, ownership records, locks or maintenance holds to bypass
   refusal. Do not change the base URL or export the new port until both removals
   succeed.
3. Keep checkout/state/destination/mode unchanged; choose an unused loopback port
   (do not terminate a foreign listener). Only now apply the new origin/publication:

   ```sh
   export RSS_PORT=8081
   podman --connection rss-aggregator exec --user www-data rss-aggregator-freshrss \
     php cli/reconfigure.php --base-url http://127.0.0.1:8081 &&
     ./scripts/rss replace && ./scripts/rss verify && ./scripts/rss status
   ```

   `podman --connection rss-aggregator` selects the owned rootless engine;
   `exec --user www-data rss-aggregator-freshrss` runs PHP inside that container,
   not on the host. Upstream `php cli/reconfigure.php --base-url URL` changes the
   installed origin, not the listener; it emits text and fails nonzero on engine,
   container or reconfiguration errors. The `&&` chain stops on failure.
   `replace` recreates only the owned application container on the same selected
   named volume; `verify` restarts/rechecks it through the security/readiness/cron/
   fetching gates. Neither changes persisted operator intent.

   Require `replaced: true` and `verified: true` with
   `url: "http://127.0.0.1:8081"`; status must report the application running,
   `http_ready: true` and `forwarding.status: "stopped"` (or `"unconfigured"`).
   Guarded startup checks the sole IPv4 loopback publication. Open the returned URL,
   confirm sign-in, loopback links/redirects on the new port, subscriptions, articles
   and read/star state; sessions may expire. Do not reinstall jobs after a failure.
   Base-URL mutation and replacement are not atomic: an error may leave the old
   listener with the new origin, or an app stopped/running for diagnosis. Preserve
   old settings, source data and the backup; inspect locally before correcting or
   retrying, rather than assuming rollback or deleting the volume.
4. Manual operation can leave both jobs disabled. **Only if explicitly wanted**,
   reinstall supervision and/or re-enable daily backups separately, after successful
   new-port verification. Do not implicitly activate either previously disabled job.
   Use the same stable checkout/state, `RSS_PORT=8081`, `RSS_TAILSCALE=0` and the
   explicitly chosen private `RSS_BACKUP_DIR` (normally the preserved destination;
   a deliberate new destination does not move old archives). Review all intended
   captured settings under [supervision configuration](#captured-mac-supervision-configuration)
   and [daily backup configuration](#daily-backup-configuration).

   ```sh
   # Optional, explicitly authorized supervision only:
   ./scripts/rss supervision-install && ./scripts/rss supervision-status
   # Optional, explicitly authorized daily backups only; use the saved UTC policy:
   ./scripts/rss backup-enable --hour "$SAVED_UTC_HOUR" --minute "$SAVED_UTC_MINUTE" &&
     ./scripts/rss backup-status --check
   ```

   Require healthy supervision observation if installed. Backup enable returns the
   captured `daily_utc`; compare it with the saved intended UTC schedule. On partial
   activation/error, stop and inspect retained registration; do not overwrite it or
   claim success. `backup-status --check` emits JSON then exits 1 until `protected`
   is true. Disabling pauses daily protection without invalidating the retained
   archive; recapturing a changed port requires new eligible daily success, not
   merely a fresh manual backup or registration. Inspect archive age/availability,
   daily evidence, resumption and maintenance using the
   [daily failure response](backup-restore.md#daily-check-and-failure-response).
   Installation/manual checks are not evidence of an actual unattended run.

Keep `RSS_PORT` set consistently afterward. On any backup/resumption failure,
preserve published archives and holds; follow
[interruption recovery](backup-restore.md#interrupted-maintenance-and-reconciliation)
before guarded restart, never delete records to force it. A new installation can
choose the port before first `start`; the browser installer records the origin.
Private HTTPS startup manages its HTTPS base URL itself; keep `RSS_TAILSCALE`
consistent and use its [runbook](tailscale-readkit.md) rather than substituting a
loopback origin.

## Persisted FreshRSS preferences and logs

Other behavior uses pinned upstream defaults unless changed locally. Choose form
login in the installer; anonymous reading/refresh, client API and public
registrations are disabled by default. Private HTTPS startup explicitly enables
client API access while enforcing form login and no anonymous reading/refresh,
then reads effective settings back before forwarding. Set the separate API password
locally in Profile.

Bundled extension enablement is empty; no full-page scraping is configured.
Upstream display/read settings apply: opening an article or its website may mark
it read, while scroll/focus marking is off by default. User browser settings live
in the data volume, not repository configuration. Check **Configuration → Archiving**
for disabled age and maximum-article purge limits; do not introduce feed/category
purge overrides for this experiment. The project does not automatically prune
articles or backups.

Persistent application logs default to warnings/errors. The scheduled job's stdout
is separate from its stderr and persistent user logs; see the canonical
[log locations and inspection commands](operations.md#service-logs-and-feed-errors).
Inspect locally: logs may contain credentials/private feed URLs.
