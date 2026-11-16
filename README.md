# Local FreshRSS

A session-only FreshRSS instance on an Apple Silicon Mac: Homebrew's Podman CLI,
a dedicated **rootless** Linux VM, the official Debian image pinned by digest,
and SQLite in a named volume on the VM's native filesystem. No host PHP or
database server, Podman Desktop, privileged socket helper, login item, launch
agent, or automatic application startup is installed by this project.

[PR #13](https://github.com/brucepucci/rss-aggregator/pull/13) integrated the initial
local runtime for [#1](https://github.com/brucepucci/rss-aggregator/issues/1) and
[#9's Python adoption](https://github.com/brucepucci/rss-aggregator/issues/9) on main.
[PR #15](https://github.com/brucepucci/rss-aggregator/pull/15) integrated
[#2's backup/recovery](https://github.com/brucepucci/rss-aggregator/issues/2).
[PR #16](https://github.com/brucepucci/rss-aggregator/pull/16) integrated
[#14's incomplete-cask inventory fix](https://github.com/brucepucci/rss-aggregator/issues/14).
This branch implements [#3's private Tailscale/ReadKit path](https://github.com/brucepucci/rss-aggregator/issues/3);
publication and review remain separate. See
[verification and adoption status](#verification-and-adoption-status) for historical
evidence and verification of the changes in this checkout.

Portable backups and guarded restore are implemented locally; see the verification
checkpoint below for their tested scope. Optional project-owned Tailscale Serve
forwarding and sanitized API checks are implemented locally. The iPhone browser's
HTTPS/API diagnostics pass, and ReadKit connects on the Air and iPhone using the
instance origin. Human-assisted checks passed bidirectional read/star synchronization
and normal stop/start persistence; the mini's own HTTPS diagnostic limitation is
recorded below.
Destructive removal and Linux deployment are not implemented. See [architecture decisions](docs/adr/).

## Getting Started

### Prerequisites

- Apple Silicon macOS with virtualization available, Homebrew (`https://brew.sh`),
  Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`
  (verified with uv 0.13.8). `uv sync --locked` manages the pinned Python 3.13.18
  runtime and local `.venv`; it does not install global Python packages. Production
  compatibility is declared as Python 3.10+, with Ruff targeting 3.10. This host
  tooling is separate from FreshRSS's containerized PHP/database runtime.
- Internet access to Homebrew, the Podman VM image registry, Docker Hub, and GitHub.
- An unused Mac loopback TCP port (default `8080`) and approximately 2 GiB guest
  RAM plus VM overhead. The VM starts with 2 CPUs and 20 GiB virtual disk capacity;
  this is capacity, not a promise of immediate physical disk consumption.
- Do not run another Podman VM while starting this one. The command explains any
  conflict and **never stops the other VM** for you.

### Install and start

From a checkout of this repository:

```sh
uv sync --locked
uv run --locked rss setup
# Choose an absolute private directory OUTSIDE this or any other Git checkout.
export RSS_BACKUP_DIR="$HOME/Library/Application Support/rss-aggregator/backups"
uv run --locked rss start
uv run --locked rss status
```

The existing executable `./scripts/rss <operation>` remains a compatibility
launcher for `uv run --locked --project <its-checkout> rss <operation>`, including
when invoked outside that checkout. It is now a shell launcher, not a Python
module to pass to `python3`. All operation results are indented JSON on stdout;
progress/underlying command chatter goes to stderr. The bootstrap `rss setup`
operation manages Podman; `rss-dev setup` separately activates developer hooks.

For an existing instance controlled by a different worktree, explicitly set
`RSS_STATE_DIR` to that checkout's existing private `.local` directory before
operating it. Worktrees do not namespace Podman resource names. Do not copy,
replace, or fabricate an ownership record to adopt existing resources.

`setup` inventories Homebrew formula/cask versions/dependencies and Podman
machines/connections, including containers, volumes and images reachable through
running machine connections. Stopped machines and non-machine connections are
recorded but not started/contacted to enumerate their contents. It previews the CLI
installation, records additions, and initializes the dedicated `rss-aggregator` VM. It does not start a stopped VM. Existing name collisions are
not adopted. If the preview includes an upgrade/reinstall of an existing package,
setup stops: obtain explicit approval and resolve the host dependency plan before
retrying. There is deliberately no bypass flag. Automatic Homebrew updates,
installation upgrades, dependent checks and install cleanup are disabled.

If `brew list --cask --versions` fails (for example, because an unrelated cask has
an interrupted upgrade), setup warns on stderr and uses two read-only queries:
`brew info --json=v2 --installed --cask` for installed versions and
`brew list --cask` for raw cask names. The host audit retains names omitted by the
JSON query and the original warning; it never invents versions for incomplete
records. Failure or unusable output from either fallback query stops setup before
installation or VM initialization. Setup does not repair, reinstall, upgrade or
remove unrelated applications, and the fallback does not bypass installation
approval or ownership safeguards. See the managed host inventory fields under
[Configuration](#configuration).

Initial verification on 2026-11-14 installed only **podman 6.2.0**, **libkrunfw
5.6.4**, **libepoxy 1.5.10**, **molten-vk 1.4.2**, **libkrun 1.19.8**, and
**krunkit 1.3.2**. Future Homebrew metadata can change this footprint; your local
inventory/preview and `.local/host-additions.json` are authoritative for your run.
Existing Python/SQLite Homebrew dependencies are not additions owned by this
project. No package is automatically uninstalled.

`start` explicitly targets the rootless `rss-aggregator` connection, verifies the
pinned image, and checks the **effective Apache PHP runtime**, first in an
unpublished disposable container and then in the actual application. The actual
application initially listens only on container loopback port 8088, **not** the
published container port 80. Only after the check passes and cleanup removes
**all** project probes (`.project-security-<32 lowercase hex digits>.php`), including
stale probes from interrupted attempts, and verifies none remain does Apache open
port 80. Cleanup/query failure keeps serving closed. The official entrypoint and
default command are retained. Mac publication is **`127.0.0.1:8080` only**, not
LAN/IPv6/all interfaces.

Every start copies **both** checked-in new-user templates into the data volume
while the application is stopped, including when reusing an existing container.
If a copy fails, startup stops; retrying repeats both copies before serving.
Existing FreshRSS users' settings and subscriptions are not rewritten.

After publication, HTTP readiness makes at most 30 attempts with a 2-second request
timeout. URL errors, timeouts and transient connection failures (including dropped
responses and resets) retry after 1 second. Only successful readiness proceeds to
scheduler verification and immediate fetching. Exhaustion returns an error and
retains the VM/application for diagnosis; it does not imply HTTP is closed.

Podman may print suggestions to install its privileged helper or Desktop. **Do not
follow those suggestions for this project**; explicit CLI connections need neither.

### First-run verification and local credentials

1. Wait for both security checks and the scheduler check to pass. Inspect the
   sanitized `.local/security.json` if desired; PHP CLI output alone is not the
   Apache security check.
2. Open **http://127.0.0.1:8080** in a local browser. Complete FreshRSS's installer:
   choose a language, confirm checks, select **SQLite**, and create **one FreshRSS
   user with Web form authentication and a strong password**. Enter credentials
   only in the local browser, never in Git, shell arguments, issue comments or chat.
   Do not select “None” authentication. Complete the final installation step.
3. Sign in. The prepared OPML template subscribes the user to exactly the public
   [FreshRSS releases feed](https://github.com/FreshRSS/FreshRSS/releases.atom),
   instead of upstream's default subscription list.
4. Run `./scripts/rss fetch`. Confirm articles appear. In **Configuration →
   Archiving**, verify both the age and maximum-article purge limits are unchecked.
   Do not add feed/category-specific purge policies.
5. Mark an article read and star it. Run `./scripts/rss replace`, sign in again,
   and confirm the subscription, articles and read/star state survived. Then test
   `./scripts/rss stop` followed by `./scripts/rss start` and check again.

The uninitialized application intentionally remains at the installer until **you**
enter your own credentials. Automated live tests use independent, disposable test
users/volumes; they do not create your real account. Replacement can expire PHP
browser sessions; your account/password and reading state persist.

### Normal operation

| Command | Behavior |
| --- | --- |
| `./scripts/rss setup` | Inventory/install missing CLI; initialize an absent owned machine; preserve an existing setup. |
| `./scripts/rss start` | Start the VM if needed; stop/reuse or create the application, copy both new-user templates, re-run the security gate, check cron and immediately run feed actualization. |
| `./scripts/rss status` | Report VM/application and recorded Serve forwarding state, inspect cron and its live daemon, show last security-check time. Does not start anything. |
| `./scripts/rss api-check [--loopback]` | Prompt locally for the FreshRSS user and hidden separate API password; check GReader diagnostics, authentication and sanitized counts. Default verifies trusted HTTPS. Explicit `--loopback` checks only the verified owned `127.0.0.1` publication and does **not** verify HTTPS. Requires an already security-checked private start and active owned forwarding. |
| `./scripts/rss fetch` | Run feed actualization immediately, or explain that the installer is pending. |
| `./scripts/rss verify` | Restart/recheck the running application, including effective Apache PHP, cron and immediate actualization. Brief service interruption. |
| `./scripts/rss replace` | Stop/remove **only the owned application container**, then recreate it using the same named data volume and current checked-in configuration. |
| `./scripts/rss backup` | Quiesce writers, create and verify an external archive in `RSS_BACKUP_DIR`, then restore the prior VM/application running state. |
| `./scripts/rss restore ARCHIVE --volume NAME` | Validate a backup locally, start only the owned VM if necessary, and import into a fresh labelled volume. Does not activate it or overwrite a volume. |
| `./scripts/rss select-volume NAME` | While the application is stopped and VM running, explicitly select an owned volume for later `start`/`replace`. Does not remove the original volume. |
| `./scripts/rss stop` | Verify VM identity and absence of unrelated rootless/rootful workloads, stop writers, create/verify an external backup, then stop only the project VM. Backup failure leaves writers stopped and VM running. Repeating a successful stop reports already stopped. |

Commands are serialized by a local lock. Repeated starts restart the application
but do not create another VM, user, subscription, or data volume. Errors return
nonzero; resources are retained for diagnosis rather than automatically rolled
back or deleted. A failed startup can leave the VM running; use `status` and
`stop`. Before stopping **any** container, `stop` verifies machine/SSH and rootless
connection identity, queries the rootless engine through that connection, and
queries the rootful engine with `podman machine ssh rss-aggregator sudo podman ps`.
It does not trust an unverified root connection. Unrelated running workloads in
either engine, or failure of either query, cause refusal without stopping containers.
Rootful workloads are never project-owned; stop them manually only when safe.

FreshRSS runs actualization on startup and via the official image's enabled cron
at **UTC minutes 00, 20 and 40** (`CRON_MIN=*/20`). This is wall-clock scheduling,
not 20 minutes after startup. Actualization respects upstream conditional HTTP,
cache, feed TTL and publisher backoff; it does not guarantee new articles on every
execution. The user default TTL is reduced from 3600 to **60 seconds**, below the
20-minute interval. The image's default cron setting is disabled; this project
explicitly enables it and checks both installed crontab and the running daemon.
Upstream's cache duration is 800 seconds by default. Its normal minimum is 60
seconds; publisher response headers can extend caching up to 86400 seconds, and
`no-cache` still respects the minimum. A low user TTL alone does not override that
HTTP cache policy. Client synchronization is not feed fetching.

Idle guest RAM is **not guaranteed to return to macOS** while the VM remains up.
Stopping the VM releases its memory allocations and stops all serving/fetching.
Nothing starts automatically after login or a Mac reboot; run `start` yourself.

### ReadKit connection — private HTTPS

First finish the local installer above. Use the pre-existing Tailscale installation
on the Mac and both reader devices; do not install another instance or enable
Funnel. Obtain approval before any tailnet-wide DNS, HTTPS or policy change.
MagicDNS and HTTPS certificates must already be enabled for this Mac. Certificate
hostnames appear in public Certificate Transparency logs, but Serve is tailnet-only.
The reader devices need a narrowly scoped TCP 443 grant to the Mac; **8080 stays
loopback-only**. Existing SSH/SMB/other grants and Funnel permissions are preserved.

```sh
# Keep using the same private ownership directory and RSS_PORT as normal.
export RSS_TAILSCALE=1
./scripts/rss start
./scripts/rss status
```

`start` checks the existing node's HTTPS support, Serve listeners and effective
incoming TCP 443 policy before changing forwarding. It reserves the node's entire
HTTPS port 443 exclusively for this project; an unowned listener (even one pointing
to the same backend), another handler, or Funnel on that listener causes refusal.
This includes foreground-session listeners, checked before any VM/application changes.
Private-start preflight also checks enabled Funnel targets across all ports and
foreground/virtual-service configurations: a public HTTP proxy (including subpaths)
or TCP forward to this FreshRSS loopback backend causes refusal without changing
the foreign route. Endpoint matching normalizes IP literals, including IPv4-mapped
IPv6, and treats local unspecified IPv4 destinations as conflicts too. Genuinely
unrelated ports/services and private forwarding on
other ports are preserved. There is no reset/adoption/bypass flag. This preflight
does not prove each physical client's authorization.

Forwarding mutations use the existing daemon's LocalAPI: read configuration and its
ETag together, validate ownership, then conditionally write with that exact
`If-Match`. A concurrent change causes refusal without an automatic retry or global
rollback, protecting both start and stop. A definitively rejected start (HTTP 412
or invalid input before sending) restores the preceding local ownership record,
or removes only the newly created local intent when none existed. It does not
roll back remote configuration or authorize a later stop to adopt a competing
client's identical route. Uncertain writes retain pending intent for recovery.
The CLI's `tailscale debug local-creds`
provides a loopback port/credential or Unix socket; its output stays private in
memory and is never executed as shell text. Requests stay local, bypass proxies
and refuse redirects. Missing credentials/ETag, unsupported debug output or denied
LocalAPI access cause failure; there is no unconditional CLI fallback. A transport
failure during a write can have an unknown outcome. Inspect `tailscale serve status`
locally before retrying and resolve ownership/availability first; never blindly roll
back someone else's configuration.
LocalAPI/debug compatibility and real HTTPS must be checked on the installed version;
conditional-update regressions are isolated tests, not live-service evidence.

After normal effective-PHP checks and probe removal, an installed instance is
configured with the HTTPS base URL, API access, form login and no anonymous
reading/refresh. Startup reads those effective settings back and refuses forwarding
if an override or unsuccessful application leaves any required value wrong. FreshRSS natively honors Serve's `X-Forwarded-Proto`; `TRUSTED_PROXY=0`
remains in place (it disables Apache forwarded-client-IP trust, not HTTPS URL
handling). Serve is created only after scheduler and fetching checks complete.
No broad bind, automatic Tailscale installation, service shutdown, certificate
removal, or Funnel command is used.

Use the private HTTPS `url` returned by `start` locally. Open it, sign in using
the **web** password, and in **Profile** set a strong, separate **API password**.
Enter it only locally; neither password belongs in Git, command arguments, chat,
logs or screenshots. Visit `<HTTPS-ORIGIN>/api/` and confirm the Google Reader
configuration check passes, then run:

```sh
./scripts/rss api-check
```

The command prompts on stderr and hides the API password. Success is JSON with
`diagnostics: "PASS"`, `authenticated: true`, and subscription/sample-article
counts only—never credentials, tokens, feed URLs or article content. Default mode
uses trusted TLS; JSON includes `transport: "https"`, `https_verified: true`.
Both modes use POST for credentials, no ambient HTTP proxy and no redirects. Try a
wrong API password separately and confirm failure; never paste authentication responses.

On this mini, requests to its own HTTPS origin currently fail during TLS even
though the iPhone's HTTPS diagnostics pass. The approved, explicit local diagnostic is:

```sh
./scripts/rss api-check --loopback
```

It retains the owned/running/security-checked app and active-forwarding requirements,
validates the actual sole IPv4 loopback publication, and reports
`transport: "loopback"`, `https_verified: false`. It never retries HTTPS credentials
over HTTP implicitly and is **not** a certificate/remote-access test. Continue the
actual Air/iPhone HTTPS and ReadKit checks; do not broaden policy, restart/upgrade
Tailscale or disable TLS verification speculatively. The user has now verified
**real-HTTPS invalid-credential rejection and redirect/origin behavior on the Air**:
wrong API password rejected, valid account still synchronizes, compatibility
`PASS`/HTTP 200, root HTTP 302 to same-origin HTTPS `/i/` then HTTP 200, `/api/`
HTTP 200 without redirect, and displayed API links use the same HTTPS origin.
See the [remote-device checklist and sanitized results](docs/tailscale-readkit.md#real-https-checks--human-assisted).
Loopback rejection alone is not HTTPS evidence.

Direct `/api/greader.php/check/compatibility` requests require the dummy diagnostic
header `Authorization: GoogleLogin auth=test/1`; browser address-bar navigation
omits it and can report an Authorization-header failure. The runbook's curl GETs
use `-q` to ignore curl configuration, `-sS` for quiet output/errors,
`--noproxy '*'` to bypass proxies, `--proto '=https'` to require HTTPS, and
`--max-redirs 0` without `--location` to avoid following redirects. `-o /dev/null`
discards pages, `-w`/`--write-out` prints HTTP status/redirect destination, and
`--header` supplies only dummy diagnostic data for the compatibility check. TLS
verification stays enabled; transport failures exit nonzero, while HTTP failures
require inspecting the printed status (no `--fail`). Never use real credentials
in these command arguments or publish private redirect URLs.

On each device select ReadKit's **FreshRSS** account integration (Google
Reader-compatible API), the **same FreshRSS user**, and its separate API password;
not Google credentials or Fever. **Verified account URL entry: `<HTTPS-ORIGIN>`
unchanged, without `/api/greader.php`.** The user confirmed this form in ReadKit on
the Air; ReadKit on the iPhone also connected using the same instructions. Both
display the public feed. The user verified read/unread and star/unstar changes from
each reader in the other reader and FreshRSS, with roughly **30 seconds** of observed
propagation (not a guaranteed bound), no certificate-warning bypass, and saved state
after normal stop/start. FreshRSS's
`<HTTPS-ORIGIN>/api/greader.php` is the underlying API endpoint, not the entry used
for these ReadKit accounts. See the [device runbook](docs/tailscale-readkit.md).

Normal `stop` removes only matching project-owned forwarding, backs up, and stops
the VM; repeating stop also removes a stale owned route when the VM is already
stopped. Keep `RSS_TAILSCALE=1` for later `start`/`verify`/`replace`. An explicit
`backup` temporarily removes forwarding while quiesced and attempts to restore the
previous application/forwarding state **even if snapshot creation fails**, regardless
of this shell's opt-in: a previously private service resumes privately and a
loopback-only service stays loopback-only. A failed `backup` therefore does **not**
confirm remote access is closed. Shutdown export failure instead leaves writers and
forwarding stopped with the VM running. Preflight refusals may leave existing serving
state untouched; failed resumption can leave the application stopped. Stopping the
project or VM makes FreshRSS unavailable even while both devices remain connected
to Tailscale. State persists in the data volume;
ReadKit synchronization is not server feed fetching.

To return deliberately to browser-only operation, set `RSS_TAILSCALE=0` and run
`start`. Recorded project forwarding is removed and an installed instance's base
URL returns to loopback; API enablement/API passwords are not erased. Keep using
the existing ownership directory, including after stop. Never copy or fabricate
`serve.json` to adopt somebody else's listener.

### Backup and restore

The complete FreshRSS `data` directory (SQLite databases, users, settings,
subscriptions, articles, reading state, feed cache and logs) lives in a labelled
Podman named volume on the VM's native filesystem, initially `rss-aggregator-data`.
The selected volume is **not** itself a backup: VM loss destroys it. No extensions
are used initially; extension code outside `data` is not covered by this format.

Set `RSS_BACKUP_DIR` to an absolute directory outside every Git checkout. New
backup directories are created with mode `0700`; existing directories must already
be private and owned by you. Archives use mode `0600`. This is **not encryption**:
treat each archive as a credential, including when copying it to another host.

```sh
export RSS_BACKUP_DIR="$HOME/Library/Application Support/rss-aggregator/backups"
./scripts/rss backup
./scripts/rss stop
```

Both operations stop all owned HTTP/cron writers and verify both VM engines are
idle before exporting the selected volume. The project lock serializes managed
commands; do not mount/write the volume through independent Podman commands or
VM shell sessions during export. Unrelated running workloads cause refusal before
any container is stopped. Explicit backup resumes a previously running application
through the normal security gate, restoring its observed forwarding mode regardless
of `RSS_TAILSCALE`, even when snapshot creation fails. It can instead temporarily
start/stop a previously stopped VM without starting the application. A forwarding
query failure during that operation also attempts VM restoration. A resume failure
is an error even if the archive was published; inspect private `last-backup.json`
and `status` locally.

Normal shutdown now requires a successful external backup before stopping the VM.
After quiescing, if backup destination/export/verification fails, the VM stays
running, application writers and forwarding stay stopped, and source data and prior
archives remain. A preflight refusal (such as foreign workloads or changed forwarding
ownership) can leave the existing serving state untouched. Fix the cause
and retry `stop`. An already-stopped VM is not restarted just to repeat shutdown.
If no selected data volume exists, shutdown reports `backup.status: "no_data"`.
No backups are pruned or uploaded automatically. Keep independent copies on durable
storage; a local archive on the same Mac does not protect against losing that Mac.
OPML is only a subscription export, not an instance backup.

The portable format is a standard, uncompressed ZIP containing only `manifest.json`
and `data.tar`. The manifest records format/version, UTC creation time, exact image
configuration/platform, included state, and the payload size/SHA-256. Publication
is atomic and follows a full read-back/integrity check; JSON also reports the whole
ZIP SHA-256. Checksums detect corruption, not malicious replacement or authenticity.
The destination filesystem must support hard links for atomic non-overwriting
publication; unsupported filesystems fail safely. Interrupted unpublished output is never a valid final archive; prior archives are
never overwritten. See [the format and recovery guide](docs/backup-restore.md).

#### Recovery on this or a clean host

Use a compatible Apple Silicon Mac with Homebrew/uv and virtualization available.
Copy a trusted archive and matching reviewed project configuration to the new host;
the original VM is not required. Install locked dependencies and run `rss setup`
to create a fresh owned VM. Never copy/fabricate `owner.json` from the old host.
For an existing running instance, first run `rss stop` with a working backup directory.

```sh
uv sync --locked
./scripts/rss setup
./scripts/rss restore /absolute/private/path/backup.zip --volume rss-recovered
./scripts/rss select-volume rss-recovered
# If no application container exists:
./scripts/rss start
# If an old, stopped application container exists, use replace INSTEAD of start:
# ./scripts/rss replace
```

Restore checks format, integrity, image compatibility and every tar member **before**
starting a VM or creating a volume. It refuses every existing target, even an empty
owned volume, and the currently selected name even when absent (to prevent implicit
activation). Failed imports retain the incomplete target unselected for diagnosis;
`select-volume` refuses incomplete targets. Choose another fresh name to retry. No implicit overwrite/delete command exists.
Links, special/sparse files, unsafe/duplicate paths and unsupported archive versions
are rejected. Normalized paths are limited to 64 components, with an 8 MiB cumulative
path-metadata budget checked before allocating ancestor strings. Both backup and
restore enforce these limits without capping file payload size; see the
[format guide](docs/backup-restore.md#format-version-1) for accounting. If legitimate
state exceeds a limit, backup fails rather than omitting data; review the limit
before changing code. Numeric ownership and ordinary permissions are preserved; unsafe tar
metadata is not passed through to the engine. A restore never activates itself.
`select-volume` requires a stopped application and running VM; `replace` preserves
the old named volume. `rss status` reports the selected name; record it before
selecting another volume. To select a previously restored volume after later VM
shutdown, first confirm ownership with `rss status`, then start only the VM using
`podman machine start --update-connection=false rss-aggregator`, select the volume,
and use `replace` (or `start` if no container exists). The VM-start command alone
does not start the application. Do not edit ownership JSON to force selection.

Restore initially requires the **exact checked-in image configuration recorded in
the archive**, not merely a newer FreshRSS version. Use the reviewed matching pin
first, check its current advisories, and run the normal effective-PHP security gate
before entering credentials. The recorded image is rechecked at the first `start`
or `replace`, before template writes/startup and before `replace` stops/removes an
old container. Changing the pin after selection does not bypass this guard; refusal
preserves provenance. Only a completed start (security, readiness, scheduler and
initial fetch checks) marks the volume activated; backing it up does not. Older
provenance records without an activation marker require one matching start before
an intentional pin update. Do not edit the marker to bypass recovery compatibility.
An old backup does not authorize bypassing a failed
gate. An arm64 platform manifest is not an x86 deployment plan: other architectures
and native Linux recovery remain unverified. After successful recovery, verify
login, subscriptions, article content and both unread/read and starred state.
If moving ports/hosts, adjust FreshRSS's base URL inside the restored container as
shown under Configuration; do not print archived settings or secrets in evidence.

After completing the matching initial recovery activation, an intentional image
update remains supported: first create and verify a backup, review current
advisories and the new official image/platform identity, explicitly update the pin,
then `replace` and verify security, login, fetching and persisted reading state.
There are no unattended updates. To roll back a database migration, restore its
pre-update backup into a fresh volume with its matching image; never assume an
older FreshRSS can open a newer database.

### Safe cleanup

For now, safe cleanup is **`./scripts/rss stop`**. No VM/volume deletion, prune,
package uninstall, or destructive convenience command is provided. **Deleting the
VM destroys its native named volumes and all FreshRSS data.** Do not run global
Podman prune/reset or remove existing Homebrew dependencies. Do not delete
`.local/owner.json`: it is the safety record associating this checkout with the
machine/connection and container/volume ownership labels. Preserve `.local/` when
moving this checkout, and keep a single controlling checkout. The package-addition
ledger is evidence for later conservative cleanup, not permission to uninstall
packages that other projects may subsequently adopt. `.venv` and uv's managed
Python/package caches are development tooling, not FreshRSS data or backups;
normal stop leaves them alone. Never confuse the protected `.local` ownership
record with a disposable tool cache. No package-uninstall/cache-prune convenience
command is added here.

### Common troubleshooting

- **Another VM is running:** stop it manually only if you own it and it is safe.
  This project will not change it or global Podman defaults for you.
- **Machine/connection/volume/container ownership conflict:** stop and investigate;
  do not delete, relabel, or edit the ownership JSON to force adoption. A lost
  ownership record requires a deliberate recovery decision, not rerunning setup.
- **Port occupied:** use an unused `RSS_PORT` consistently (see Configuration).
  Do not terminate another listener automatically.
- **Homebrew upgrade approval required:** inspect `.local/install-preview.json`;
  no installation has been attempted by that invocation. Resolve the dependency
  plan explicitly before retrying. Partial installation additions are recorded.
- **Incomplete unrelated Homebrew cask:** a cask-version query error is a warning
  if both read-only fallback queries succeed with usable data. Inspect the private
  `host-before.json` (or `host-latest.json` on later runs) for raw names and the
  original warning; an omitted version does not mean the application was removed.
  If either fallback query fails or its data is unusable, setup exits 1 before
  installation/VM initialization. Inspect `brew info --json=v2 --installed --cask`
  (installed-version JSON) and `brew list --cask` (one raw name per line) locally;
  both must succeed, with valid matching records, before retrying setup. Do not
  remove or reinstall an unrelated application just to make project setup pass.
- **Backup failure or missing `RSS_BACKUP_DIR`:** explicit `backup` attempts to
  restore prior serving/forwarding after a snapshot failure; do not assume remote
  access is closed. Shutdown backup failure after quiescing leaves writers and
  forwarding stopped and the VM running; preflight refusals may leave serving
  untouched. Choose a private external directory, check free disk space and engine
  availability, then retry `stop`. Previous good archives and the source volume
  remain. Do not force VM deletion or remove data as a workaround.
- **Restore compatibility/target refusal:** use the archive's exact reviewed image
  configuration and a fresh target name. A failed import target is retained and
  is not automatically selected. Do not overwrite it or the source to force retry.
- **Registry/VM/network unavailable:** commands return an error without stopping
  other machines. Restore connectivity/virtualization and retry. Startup failure
  does not imply the VM was stopped.
- **PHP/image/security mismatch:** do not enter credentials or bypass checks.
  Review current PHP advisories and the pinned image, then select and verify an
  updated official image. `localhost` and container isolation are not a production
  security guarantee.
- **No articles:** finish installation, sign in, run `fetch`, check the public
  subscription and local FreshRSS logs. Publisher outages/backoff and TTL/cache
  can prevent updates even when cron is healthy. Logs can contain sensitive URLs;
  inspect locally, do not paste them into shared evidence.
- **Cron missing/dead:** `status` fails rather than claiming fetching is healthy.
  Use `start`/`verify` to restart and recheck. Cron is upstream's background daemon,
  not a separately supervised service.
- **Private HTTPS prerequisite/policy failure:** connect the existing Tailscale
  installation and inspect its local status. An administrator must approve DNS/HTTPS
  changes and a narrow reader-device-to-Mac TCP 443 grant. The command's effective
  netmap preflight is coarse; authorization from each actual device still needs testing.
- **Serve ownership/443 or public-backend conflict:** inspect `tailscale serve status`
  locally, including Funnel targets on other ports. Preserve
  the other listener, handler or Funnel configuration and resolve ownership manually;
  do not run `serve reset`, edit the ownership record or enable Funnel to bypass it.
  Stop refuses a changed route rather than deleting it. Other applications' ports
  and services are not project resources.
- **Certificate/access failure:** check Tailscale on both devices, MagicDNS, HTTPS
  authorization, device clocks and current trust stores. Never use a self-signed
  certificate bypass or `curl -k`. Check macOS/iOS local-network permissions if
  prompted; they do not replace tailnet authorization. A stopped VM cannot serve.
- **API/ReadKit authentication failure:** use the separate per-user API password,
  confirm API access under Authentication and inspect the GReader check at `/api/`.
  `api-check` refuses redirects and untrusted TLS; a redirect often indicates a bad
  external/base URL. Do not paste raw API responses or tokens into issues.
- **Wrong port/image/configuration on an existing container:** check the intended
  settings, then use `replace`. Never remove the named data volume to fix a
  container configuration problem.

## Configuration

No secret setting is required in the repository. Required user credentials are
entered locally into FreshRSS after security verification and stored as upstream
requires in the private VM data volume. Keep private feed URLs, credentials,
backups and generated settings out of Git. `.local/`, `.env*`, SQLite files and
`backups/` are ignored; this is not a substitute for inspecting staged files.
Local inventories/ownership/security records are written with private file modes;
permissions are not encryption. Treat VM storage and application logs as sensitive.

| Setting | Purpose and default | How to change/apply |
| --- | --- | --- |
| `RSS_PORT` (optional environment variable) | Mac IPv4 loopback port; `8080`, allowed range `1024–65535`. | Set the same value for operational commands, e.g. `export RSS_PORT=8081`; use `replace` for an existing container. For an installed instance, also update its base URL as shown below. |
| `RSS_TAILSCALE` (optional environment variable) | `0` by default: loopback-only. `1`: private Serve HTTPS on the node's fixed port 443. Other values fail before startup changes. Requires an initialized FreshRSS instance, connected existing Tailscale, enabled HTTPS certificates and an incoming TCP 443 grant. | Set consistently for `start`/`verify`/`replace`. `stop` removes recorded owned forwarding regardless of this value; `backup` restores the observed prior forwarding mode, even after snapshot failure; it does not apply this shell's opt-in. Switching to `0` and starting removes the owned route and resets the installed base URL to loopback, but retains API enablement/passwords. |
| `.local/serve.json` (managed ownership record) | Owner token, private hostname/backend target and pending/active/inactive phase. No passwords or API tokens. Written privately before creating forwarding so interrupted/uncertain starts can be recovered; a definitively rejected write restores the preceding local record or removes the new intent. | Preserve with `.local/owner.json`; never hand-edit/copy/adopt. Exact-match checks refuse changed or unowned routes; conditional LocalAPI updates refuse concurrent configuration changes. A rejected start grants no new ownership; an inactive record does not authorize adopting a newly appearing listener. Paths follow `RSS_STATE_DIR`. |
| `RSS_BACKUP_DIR` (required for explicit backup and shutdown with data) | Absolute host backup directory outside every Git checkout; **no default**. New directory mode `0700`, archive mode `0600`; existing directory must be private and owned by you. | Export before `backup`/`stop`, e.g. the path in Getting Started. Change it explicitly to choose a different external destination; existing archives are never moved/pruned. Not required just to validate/restore an archive. |
| `.local/host-before.json`, `.local/host-latest.json` (managed host inventories) | First successful host inventory and most recent later inventory; `packages` maps formula names to installed-version strings; `casks` is newline-delimited `name version` text; `dependencies` and `podman_formula` retain raw Homebrew output. On cask-version fallback only, `cask_names` retains raw newline-delimited names and `cask_inventory_warning` records the failed query, exit code and original stderr. `casks` then contains only known installed versions from JSON, never the latest available version; incomplete names can appear only in `cask_names`. Empty inventories are valid. | Setup records these before any installation; a failed inventory does not replace the previous records. No manual configuration or bypass. Preserve as private audit evidence, not uninstall authorization. Paths follow `RSS_STATE_DIR`. |
| `.local/selected-volume.json` (managed local record) | Explicitly selected owned data-volume name; absent means `rss-aggregator-data`. | Use `rss select-volume NAME` while application stopped and VM running. Do not hand-edit or copy to adopt another project's data. |
| `.local/recovery-volumes.json`, `.local/last-backup.json` (managed local records) | Volume image provenance/import completeness and completed first-activation marker, and most recently published archive location/checksum. | Preserve with ownership state. Do not edit incomplete imports into ready state or bypass activation markers. First activation requires the recorded pin; records without a marker require one matching start before an image update. An unattached volume without known image provenance is refused for backup; start it with its reviewed matching pin first. |
| `RSS_STATE_DIR` (optional environment variable) | Ownership/inventory/lock/security record directory; `<checkout>/.local`. Not the FreshRSS database location. | Set an absolute private path **outside Git** before setup and use it consistently. Changing it does not adopt or migrate an existing machine. |
| `config/image.json` | Official Debian FreshRSS `1.30.4`, digest-pinned OCI index, explicit `linux/arm64`, verified platform manifest and source revision. | Change only after checking current advisories and verifying registry identity/runtime. `replace` applies a pin change. No floating-tag update command. |
| `config/config-user.custom.php` | New-user defaults: no age/maximum article purge; TTL `60` seconds. | Every `start` (also through `verify`/`replace`) copies it while the application is stopped; retry repeats both template copies. **Existing users are not rewritten**: change their settings locally in FreshRSS, including feed/category overrides. |
| `config/public-feed.opml` | One public FreshRSS releases feed for new users. | Every `start` copies it while stopped, including with an existing container; existing subscriptions remain unchanged. Enter private feeds locally only in a later slice. |
| VM resources (fixed in command) | Machine `rss-aggregator`, rootless, initial 2 CPUs / 2048 MiB RAM / 20 GiB disk. | No project resize/delete command; do not change machine identity or connection records to work around ownership checks. |
| Container settings (fixed in command) | `CRON_MIN=*/20`, `TZ=UTC`, `ENABLE_ACCESS_LOG=0`, syslog copying off, `TRUSTED_PROXY=0`, `--restart=no`, internal security-gate listener. | Keep the official entrypoint/default command; apply reviewed changes with `replace`. No host cron/service registration. |

Development configuration is non-secret and required for the commands below:

| File | Purpose/default | Change and apply |
| --- | --- | --- |
| `pyproject.toml` | Python 3.10+ compatibility, Click runtime dependency, pytest/Ruff/pre-commit dev group, entrypoints, focused Ruff rules, default `not live` pytest selection. | Make reviewed metadata changes, explicitly regenerate `uv.lock` with `uv lock` when dependencies change, then `uv sync --locked` and the baseline check. |
| `.python-version` | uv-managed Python 3.13.18. | Update intentionally with compatible project/Ruff targets and verify a fresh sync; do not silently upgrade host packages. |
| `uv.lock` | Committed application dependency versions/hashes, including compatibility markers. | Regenerate only for intended dependency changes; normal setup/checks use `--locked`, not an implicit lock update. |
| `.pre-commit-config.yaml` | One complete check-only baseline at pre-push, local/system environment, all files regardless of changed filenames. | Keep it aligned with `rss-dev check`; explicitly run guarded `rss-dev setup` when activating hooks. |
| `.github/workflows/python.yml` | Same baseline on PRs/main updates, uv 0.13.8, immutable action references. | Review and verify changes; see [verification and adoption status](#verification-and-adoption-status) for revision-specific CI evidence and limits. |

For example, to move an **already installed** instance to loopback port 8081:

```sh
export RSS_PORT=8081
podman --connection rss-aggregator exec --user www-data rss-aggregator-freshrss \
  php cli/reconfigure.php --base-url http://127.0.0.1:8081
./scripts/rss replace
```

Keep `RSS_PORT` set consistently for later starts. This PHP command runs **inside
the container**, not on macOS. For a new installation, choose the port before
`start` and the browser installer records the correct base URL automatically.

All other behavior uses the pinned upstream defaults unless changed locally:
form login is selected in the installer; anonymous reading/refresh, client API
and public registrations are disabled by default. Private HTTPS start explicitly
enables the client API while enforcing form login and no anonymous reading/refresh;
the separate per-user API password is set locally in Profile. Bundled extension enablement is
empty; no full-page scraping is configured. Upstream display/read settings still
apply (opening an article or its website may mark it read; scroll/focus marking is
off by default). Per-feed/category policies can override user archiving settings,
so do not enable purge overrides for this experiment. User-configurable browser
settings persist in the named volume rather than becoming repository settings.
Production logs record warnings/errors by default, not notice-level fetch starts.
The official cron job redirects its latest execution output to
`/tmp/FreshRSS.log` **inside the container**; that transient output is overwritten
on the next run and is not a persistent audit log or backup. Treat all logs as
potentially sensitive.

### Image and PHP security policy

The pinned OCI index is
`sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df`;
Apple Silicon resolves to
`sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797`.
Runtime verification observed **Apache `apache2handler`, PHP 8.4.27**, with
`session.upload_progress.enabled` disabled in the effective web configuration.
The CLI version is separately recorded. Package/SBOM metadata is not accepted as
runtime evidence. The upstream entrypoint edits the Apache and CLI INIs before
starting workers; the project does not replace it or use an ineffective
application-level `ini_set()` mitigation.

PHP 8.4.27 meets the October 21 fixed-release minimum, but **does not fix the
additional October 23 upload-progress advisory**; disabled upload progress is
required separately. This gate accepts only PHP 8.4 at or above 8.4.27 for the
reviewed image. It is not an automatic vulnerability/advisory scanner. Reassess
security/support before changing or continuing to use an old pin. See PHP's
[2026 advisories](https://www.php.net/archive/2026.php),
[supported versions](https://www.php.net/supported-versions.php), and the
[official FreshRSS image documentation](https://github.com/FreshRSS/FreshRSS/blob/1.30.4/Docker/README.md).

The pinned index also contains other platforms, but only arm64 is verified here.
For another supported architecture, resolve its platform manifest from this index,
update the platform/manifest in `config/image.json`, and run full security and
integration verification on that host. The Mac lifecycle command is not a Linux
server deployment implementation. Never assume the arm64 manifest is portable.

## CLI reference

### Entrypoints and help

Run from a synchronized checkout (`uv sync --locked` first):

| Invocation | Purpose and accepted arguments/options |
| --- | --- |
| `uv run --locked rss [--help] COMMAND` | Runtime group; `COMMAND` is one of the eleven operations below. |
| `uv run --locked python -m rss_aggregator [--help] COMMAND` | Identical runtime group through the Python module entrypoint. |
| `./scripts/rss [--help] COMMAND` | Compatibility shell launcher; forwards all arguments unchanged to `uv run --locked --project <launcher-checkout> rss`. Works from another directory; requires executable script and uv on `PATH`. |
| `uv run --locked rss-dev [--help] COMMAND` | Developer group; `COMMAND` is one of the four operations below. |

`--help` at either group or any subcommand prints conventional text usage to
stdout and exits 0 without operating on the instance or installing hooks. For
example, `uv run --locked rss --help`, `./scripts/rss start --help`, and
`uv run --locked rss-dev setup --help`. Click exposes `--help`, not `-h`.
Invoking a group without a command displays usage and exits 2. Unknown commands,
extra positional arguments and unsupported options also exit 2 with stderr
usage/diagnostics, not operation JSON. There is no implicit operation.

Lifecycle commands and `backup` have no positional arguments/options except
`--help`. `api-check [--loopback]` accepts no positional arguments; `--loopback` is
an optional boolean flag, absent by default (trusted HTTPS), selecting only the owned
`127.0.0.1` HTTP backend without HTTPS verification. It prompts on stderr for a
FreshRSS username and hidden API password; there are no credential flags/environment
variables or URL argument.
Cancelled/EOF prompts fail nonzero without JSON. `restore ARCHIVE --volume NAME` requires a local archive file and fresh
target name; `select-volume NAME` requires an owned volume name. In particular,
`rss` has no `--project` option. Runtime configuration comes from
the [settings above](#configuration). The launcher's `--project` is an internal
uv option selecting its checkout, not an option accepted by `rss`. Likewise,
`uv run` options such as `--locked` and `--offline` precede the executable name;
`--offline` allows cached-environment execution, not offline live-service access.
Failures in uv or the shell launcher can occur before Click starts and produce
stderr diagnostics/nonzero status without JSON.

### Runtime commands and results

Use `uv run --locked rss COMMAND` (or either runtime alias above) with these exact
subcommands; each also accepts `--help`. Lifecycle effects are detailed in
[Normal operation](#normal-operation).

Successful runtime operations emit exactly one indented JSON object; they do not
emit a result on failure. `--help` is conventional Click help, not operation JSON.
Runtime/configuration/external-command failures exit 1 with stderr diagnostics;
invalid command usage exits 2. Capture stdout separately when consuming results.

| Command | Purpose | JSON result fields |
| --- | --- | --- |
| `rss setup` | Inventory/install missing Podman CLI and initialize the owned VM without starting services. | `machine: "rss-aggregator"`, `created: boolean`. |
| `rss start` | Start/restart after security checks, verify scheduling/fetching and optionally configure private HTTPS/API. | `machine: "running"`, `url` (HTTPS when opted in, otherwise loopback), `security`, `scheduler`, `fetch`; private starts also include `forwarding: {status: "running", url}`. |
| `rss status` | Inspect state without starting anything. | `machine`, `application`, selected `volume` and `forwarding` when configured (`status: "unconfigured"`, `"running"` or `"stopped"`; a recorded route also includes private `url`); when available `http_ready`, `scheduler`, `security`. A stopped VM has application `unknown`, not a claim that its container disappeared. An unconfigured checkout reports machine/application `absent`. |
| `rss api-check [--loopback]` | Prompt locally; check GReader diagnostics and authenticated read access. Default: trusted private HTTPS. Explicit `--loopback`: only the verified owned IPv4 loopback publication; no HTTPS verification. Requires an owned running app with its matching application security audit and active owned forwarding. Does not change reading state. | `diagnostics: "PASS"`, `authenticated: true`, integer `subscriptions` and `sample_articles` (at most one requested article, not a total), `transport: "https"` / `"loopback"`, `https_verified: true` / `false`. Invalid credentials, TLS/connectivity, invalid loopback publication, stopped service, redirects, incompatible API or oversized/malformed responses fail with sanitized stderr and no result JSON. |
| `rss fetch` | Run feed actualization immediately. | `status: "installer_pending"` or `"completed"`. Completion means actualization ran, not that new publisher content was guaranteed. |
| `rss verify` | Restart/recheck the running application before reopening service; briefly interrupts service. | Start fields plus `verified: true`. |
| `rss replace` | Recreate only the owned application container, preserving its named data volume. | Start fields plus `replaced: true`. |
| `rss backup` | Create a quiesced external archive; attempt to restore prior application/forwarding/VM state even on snapshot failure (exit 1). Preflight refusal may leave serving untouched; resume failure is also an error. | `archive`, `sha256`, `verified: true`, `manifest`. |
| `rss restore ARCHIVE --volume NAME` | Validate and import into a fresh owned volume without activation. | `restored: true`, `volume`, `selected: false`, `manifest`, `next_step`. |
| `rss select-volume NAME` | Select an owned volume while the application is stopped. | `volume`, `selected: true`, `next_step`. |
| `rss stop` | Back up application state before stopping the owned VM. | `machine: "stopped"`, `already_stopped: boolean`, `backup` (archive result or `status: "already_stopped"` / `"no_data"`). |

`scheduler` contains `running`, `cron` and `timezone`. `security` is the sanitized
image/PHP audit record also stored in private `security.json`: checked time,
container, image identity/platform, FreshRSS version, advisory-review
date, effective web metadata, CLI PHP version and the official command. It is a
record of a check, not an advisory scan or a guarantee of future security. No
universal result envelope or extra text-output mode is required.

### Developer commands and results

Each command has syntax
`uv run --locked rss-dev COMMAND [--project DIRECTORY] [--help]`
and accepts no positional arguments. `--project DIRECTORY` is optional
**on each subcommand**, after `check`, `setup`, `format` or `fix`; it is not a
group-level option. It selects the directory to operate in, defaults to the
process's **current working directory**, and resolves relative paths from there.
It is not automatically the executable's source checkout. Files are rejected as
usage errors; select a synchronized project directory (its Git repository root
for `setup`). `--help` prints help only. There are no other project-defined flags.

`check`, `format` and `fix` execute tools with the **selected checkout's local
`.venv` interpreter**, not the invoking checkout's editable installation. For
example, synchronize B with `uv sync --locked --project /path/to/B` before
running A's `rss-dev check --project /path/to/B`. Python runs in isolated import
mode; inherited `PYTHONPATH`/`PYTHONHOME` and `PYTEST_*` overrides are removed
from child environments. Tests therefore import B's installed production code,
not A's.

All developer operations also remove inherited repository selectors from their
child environments: `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`,
`GIT_IMPLICIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_GRAFT_FILE`, `GIT_SHALLOW_FILE`,
`GIT_REPLACE_REF_BASE` and `GIT_NO_REPLACE_OBJECTS`. A hook's Git context must not
override the selected project's directory or redirect disposable tests/hook setup
into another repository. This is a child-environment filter, not a change to the
parent push process. Git configuration/authentication variables remain intact;
global/system configuration and `core.hooksPath` safeguards are still honored.
Bytecode/cache suppression remains enabled.

These commands never create/synchronize an environment or fall back to A's
packages: a missing `.venv` interpreter produces failed check entries with return
code 127; a missing tool also fails nonzero. Re-run the selected checkout's sync
after dependency changes. External/shared environments configured through uv's
`UV_PROJECT_ENVIRONMENT` are not selected by `rss-dev`; use the checkout-local
`.venv`. `setup` also uses that local interpreter when installing a new hook,
so its generated script references the selected environment. No-remote setup and
existing-hook inspection retain their documented behavior.

| Command | Purpose, defaults and result |
| --- | --- |
| `rss-dev check [--project DIRECTORY]` | Run the complete non-live pytest suite, Ruff lint and Ruff format checks, without fixing source. Always run all three; JSON `checks` names are `pytest`, `lint`, `format`. Inherited pytest/import-path overrides are stripped and pytest receives explicit `-m 'not live'`, so ambient selection cannot narrow the suite, enable live tests or substitute the invoking checkout's imports. Ruff receives `--no-fix --no-fix-only`, overriding configured autofixes so lint violations fail without rewriting source. |
| `rss-dev setup [--project DIRECTORY]` | Inspect Git arrangements and activate a pre-push hook only when safe. JSON contains `ok` and `active`, with `reason` and `next_step` for no-remote/manual-resolution cases. No remote: success but `active: false`; rerun after adding a remote. With a remote, conflicting or recognized nonexecutable hooks: failure, `active: false`, existing bytes/mode preserved, manual resolution required. A recognized executable hook is retained unchanged. See [Managed pre-push hooks](#managed-pre-push-hooks). |
| `rss-dev format [--project DIRECTORY]` | Explicitly rewrite Python formatting using Ruff; no lint fixes. JSON `checks` name: `format`. |
| `rss-dev fix [--project DIRECTORY]` | Explicitly apply safe Ruff lint fixes only; unsafe fixes disabled, no formatting. JSON `checks` name: `fix`. |

Examples: `uv run --locked rss-dev check` checks the current directory;
`uv run --locked rss-dev check --project /path/to/checkout` selects another one.
For `check`, `format` and `fix`, stdout is one indented JSON object shaped as
`{"ok": true, "checks": [{"name": "pytest", "returncode": 0}, ...]}` (entries
vary by command). `ok` is true only if every child succeeds. OS-level child
launch failures are recorded with return code 127. `setup` instead emits its hook-status
object, not a `checks` array. Logical failures still emit JSON with `ok: false`
and exit 1; success exits 0, even for inactive no-remote setup. Child output goes
to stderr. Invalid CLI usage exits 2 without result JSON. Formatting/fixes are
explicitly mutating, unlike the baseline; neither starts FreshRSS.

### Pytest selection

`uv run --locked pytest [OPTIONS] [file_or_dir ...]` runs tests directly, with
human-readable pytest reports rather than the CLI JSON contract. With no paths,
configuration selects `tests/`; explicit paths/node IDs narrow a direct run, not
the required full baseline. Project-specific selection and documented controls:

| Argument/option | Purpose and default | Example |
| --- | --- | --- |
| `[file_or_dir ...]` | Optional test paths/node IDs; omit for the complete configured suite. | `uv run --locked pytest tests/test_commands.py` |
| `-m EXPRESSION` | Marker expression; project default is `not live`. Explicit `-m live` selects real-service tests and requires their prerequisites. | `uv run --locked pytest -m live` |
| `-s` | Disable pytest output capture (normally enabled), useful for live diagnostics. | `uv run --locked pytest -m live -s` |
| `-h`, `--help` | Conventional pytest text help and configuration; exits 0 without running tests. | `uv run --locked --offline pytest --help` |

Pytest's remaining upstream options are listed by that help command. Direct
pytest runs retain pytest's normal environment handling; use `rss-dev check` for
the guarded baseline. Pytest exits 0 on success, 1 for failed tests, 2 for
interruption/collection errors, 3 for internal errors, 4 for usage errors,
or 5 when no tests are collected.
See [Explicit live/platform checks](#explicit-liveplatform-checks) before selecting
live tests; a default-suite pass is not live evidence.

### Python interface

Python callers use the same guarded behavior independently of Click:

```python
from rss_aggregator import Runtime

result = Runtime().run("status")
```

`Runtime(root=..., state_dir=...)` accepts explicit `pathlib.Path` values. Defaults
resolve from the editable source checkout and current `RSS_STATE_DIR`, not an
import-time singleton. `run` serializes callable callers as well as CLI callers;
errors raise `RuntimeFailure` or configuration/OS exceptions for the caller to
handle. `run("backup")`, `run("restore", archive=Path(...), volume="fresh-name")`
and `run("select-volume", volume="owned-name")` share the same lock and safeguards.
`run("api-check", username="<FRESHRSS-USER>", api_password="<API-PASSWORD>")`
uses the same lock and returns only sanitized diagnostics/counts/transport metadata.
Optional `loopback=True` selects only the verified owned local backend and returns
`https_verified: false`; omitted/false retains HTTPS. Supply real values
only in local process memory, never source files or shell arguments. There are eleven
operations, not a public command for every helper.

## Development verification

### Environment and baseline

```sh
uv sync --locked
uv run --locked rss-dev check
```

The initial sync installs locked tooling and may download Python 3.13.18,
packages and the build backend; no global pip or Homebrew upgrade is involved.
The baseline then uses installed tools and isolated fixtures without network,
credentials, production services or real Podman resources. Disposable Git fixtures
sync offline from the warmed cache. Missing caches fail rather than skip: rerun
initial sync instead of allowing network inside tests. Hook-refusal permutations
use minimal disposable repositories and the already-installed developer CLI, without
synchronizing environments. Full environments remain for installation, actual pushes
and cross-checkout isolation. Fixture-only A/B import sentinels check selected-code
isolation without rewriting lifecycle source or relying on unrelated test counts;
Runtime root/configuration checks remain separate. Readiness regressions combine
controlled Podman responses with a real disposable loopback HTTP server; they are
not live-VM fault-injection evidence.

The [developer CLI reference](#developer-commands-and-results) defines the complete
check-only baseline, environment isolation, JSON/exit semantics and explicit
mutating `rss-dev format` / `rss-dev fix` commands. Checks disable pytest/Ruff
caches and bytecode writes and do not rewrite source. Direct `uv run --locked pytest`
selects the default suite; live tests are collected but deselected. Callable
counterparts `check`, `setup`, `format_code` and `fix` in
`rss_aggregator.development` require no Click context.

Ruff selects `E4,E7,E9,F,I,D,UP`: baseline errors, unused names, import sorting,
NumPy docstrings and modernization, targeting Python 3.10. It replaces overlapping
formatter/import/docstring tools, not by literally running them. Tests are exempt
from `D`; production signatures (including private callables) must be annotated
and have proportional NumPy-style docstrings. Review assesses usefulness beyond
Ruff's enforcement. No separate typechecker or coverage threshold is required.

### Managed pre-push hooks

After sync, explicitly activate local enforcement:

```sh
uv run --locked rss-dev setup
```

With a remote, setup installs uv-managed pre-commit's **pre-push** hook using
`.pre-commit-config.yaml`. Without a remote it succeeds inactive; rerun after
adding one (no automatic discovery). Foreign/legacy/symlinked hooks, custom
`core.hooksPath`, or recognized nonexecutable hooks are preserved and refused
with exit 1 and manual-resolution guidance. Never overwrite someone else's hooks.
A recognized executable hook, including extensions, stays byte-for-byte unchanged;
resolve stale referenced environments manually. `active` means recognized/installed,
not proof a push ran. Setup neither installs dependencies nor starts FreshRSS.

**Linked worktrees share hooks:** activate intentionally and push from a branch
containing the configuration. Historical tests used disposable repositories/local
bare remotes, not this shared Git directory. The hook runs the complete baseline
through `uv run --locked --offline rss-dev check`, irrespective of changed filenames
or remote provider. Missing environments/caches fail; pushes never silently download
or rewrite source. Hooks cover **all Git remotes**, can be absent/bypassed and are
not a security boundary.

### GitHub Actions

`.github/workflows/python.yml` runs the same `uv sync --locked` and
`uv run --locked rss-dev check` baseline on pull requests and main-branch pushes,
with read-only contents permission, immutable action references and uv 0.13.8.
CI and local gates check; they do not silently format/fix files. Historical
passing/failing runs and current acceptance limits are listed
[below](#verification-and-adoption-status); a workflow definition is not execution
evidence or branch protection.

### Explicit live/platform checks

After starting the owned instance, select the retained real Mac/HTTP checks:

```sh
uv run --locked pytest -m live
```

Missing Podman, the wrong platform or an unavailable project is an explicit
failure when selected, not a skip reported as full coverage. These remain Mac
VM-specific tests; they do not implement #7's native Linux runner. They require
the cached pinned image, internet access to the public GitHub feed, and the
correct existing ownership directory. They interrupt the project session. Set `RSS_BACKUP_DIR` to a private external
directory before the suite: existing VM-restart tests now exercise normal shutdown
backups. Use the usual `umask 022` for public controlled-feed fixture files (backup
archives remain explicitly `0600` regardless); an inherited `077` can make those
public fixture files unreadable by Apache. Use `RSS_STATE_DIR` explicitly when
operating from another worktree; no automatic
ownership-state transfer occurs. The old `RSS_LIVE`/`RSS_OBSERVE_CRON` unittest
commands are unsupported; select live tests with `-m live`.

Unique labelled disposable test containers/volumes exercise HTTP installation,
authentication, public articles, read/star state, replacement and VM stop/start.
The recovery drill backs up a running and a stopped synthetic-user instance,
removes its entire leased source container/volume, restores into a fresh volume,
explicitly selects it, and verifies login, subscription, content, both read/unread
states and a star through HTTP. It redirects only reserved resource, selection,
provenance, backup-locator and Serve-record names, retaining production Click behavior, the real
ownership checks and lock. Fixture backups use `test-<token>-last-backup.json`,
removed during fixture cleanup; the user's `last-backup.json` bytes (or absence)
are preserved by this drill. The separate VM-restart tests still exercise normal
shutdown and update the real locator with a real instance backup in `RSS_BACKUP_DIR`.
No original source data is available during recovery. This is the spec's equivalent
isolated recovery drill, not a second physical Mac demonstration.
Cleanup removes only those fixtures. The installed-state replacement test invokes
unchanged production Click `replace` in a subprocess with application, data volume,
preflight-container, selection and provenance names redirected to reserved test
names. Both fixture runners force loopback mode and use reserved Serve-record names
so they cannot remove/reconfigure the real application's private forwarding. It retains
the real VM/connection ownership checks and command lock, uses the existing owner
record (never a fabricated copy), and verifies subscription, article content and
read/star state through authenticated HTTP after replacement. Cleanup is anchored
to its retained test-labelled volume. This check writes the normal `security.json`
audit in the selected ownership directory; it does not replace the real application
or its data. The security test temporarily adds an Apache INI override to the real
owned application, proves failed verification keeps it inaccessible with no
diagnostic left behind, then removes the override and restores verified service.
It also checks that retry removes a stale generated probe but preserves a file
outside the exact generated-name pattern. Let its recovery handler finish. Do not publish
HTML, authentication payloads, private URLs or `--showlocals` output as artifacts.

Only the fast controlled-feed regression reduces fixture TTL/cache minimums. Its
publisher advertises `no-cache, max-age=0`, avoiding the image's long-lived asset
caching. Tests verify the installed production `*/20` cron configuration and live
daemon, and execute the real cron command against changed controlled content.
They do **not** wait for production schedule intervals or change the production
schedule. The long observer and `--observe-cron` option have been removed, along
with the observer-only regression; that option now returns a pytest usage error.
The live suite has six tests, including the issue #2 recovery drill. Production-cadence observation is a
one-off acceptance/soak check, not a routine test or publication requirement;
previous natural-run evidence below remains historical and is not a claim that
manual cron-command execution proves scheduler timing.

## Verification and adoption status

The canonical policy is [docs/python-standards.md](docs/python-standards.md),
discovered through [AGENTS.md](AGENTS.md). The repository implements uv, pytest,
callable operations with thin Click/JSON adapters, production annotations and
proportional NumPy-style docstrings, Ruff, guarded pre-push setup and matching CI.
No Python policy exceptions are granted beyond the standard's exemptions for
tests and trivial special methods. Production code is in top-level `rss_aggregator/`; `scripts/rss`
is only a compatibility launcher. Host tooling does not authorize host PHP/database
installation, unrelated-resource changes or unexpected shared-package upgrades.

[#8](https://github.com/brucepucci/rss-aggregator/issues/8)'s documentation-only
standard reached main separately through PR #10 (`d991e39`).
[PR #11](https://github.com/brucepucci/rss-aggregator/pull/11) integrated #9 into
#1's branch (`f88ea85`), not main. PR #13 subsequently delivered **both #1 and #9**
to main as squash commit `22ea43e`, separate from #8. PR #15 delivered issue #2
as `3cb1aed`, and PR #16 delivered issue #14 as `cb5ed36`. This branch implements
issue #3; publication, review and merge remain
separately authorized. Hooks are activated
per checkout only by explicit setup (and shared by linked worktrees).

### Historical evidence — not new results

The existing [PR #13 body](https://github.com/brucepucci/rss-aggregator/pull/13)
records head `bf39851538aefa612cf3d21298c7617fd141892e`, tree
`ccab46669276a62109a4e82faccee5c7fc8fc7c8`: fresh archived-checkout sync,
**60 default tests plus Ruff lint/format passed** (five live deselected).
Separately, **five Mac/HTTP live tests passed in 35.01s**: effective Apache
security failure/recovery, synthetic-user authentication, public/controlled feeds,
purge settings, read/star state, fixture replacement, VM restart and port migration.
Apache/CLI PHP 8.4.27, disabled web upload progress, UTC `*/20` daemon/schedule and
final VM/listener shutdown were verified. Source stayed unchanged; no real
credentials or shared hooks were provisioned. These results cover **that candidate,
not these maintainer fixes**.

Historical GitHub CI evidence (2026-11-14):

| Check | Revision / run |
| --- | --- |
| Passing PR #11 baseline | `4a61166`: [43641847554](https://github.com/brucepucci/rss-aggregator/actions/runs/43641847554) |
| Controlled pytest failure | `1977d38`: [43644086364](https://github.com/brucepucci/rss-aggregator/actions/runs/43644086364) |
| Controlled lint failure | `9d4f425`: [43644229824](https://github.com/brucepucci/rss-aggregator/actions/runs/43644229824) |
| Controlled format failure | `8104199`: [43644329319](https://github.com/brucepucci/rss-aggregator/actions/runs/43644329319) |
| Restored tree, all checks passing | `97b62ae` (same tree as `67cf72b`): [43644411740](https://github.com/brucepucci/rss-aggregator/actions/runs/43644411740) |

The failure experiments used disposable [PR #12](https://github.com/brucepucci/rss-aggregator/pull/12),
closed unmerged with its branch deleted. Dependencies installed successfully; each
run failed only the intended check. They supply #9's negative-CI evidence, not
branch protection or results for newer revisions. No new CI result is claimed here.

#1's pre-adoption `1021070` run historically observed two **natural** executions
at 2026-11-14 18:20:01 and 18:40:01 UTC collecting newly published controlled
articles with production TTL/cache policy. That is not an integrated #9/PR #13
rerun. The earlier real Homebrew footprint is listed under installation above;
installation was not repeated for the PR #13 candidate.

### Verification checkpoints and limits

- **PR #18 local review fixes (after `62626a8`, `issue-3`):**
  `uv run --locked rss-dev check` passed **329 default tests in 81.61s**, six live
  deselected; Ruff lint and format checks passed. Regression tests reproduced the
  foreground-listener preflight omission, concurrent Serve mutation race, and backup
  restoration bugs before their fixes. Coverage now includes foreground Serve/Funnel
  refusal before VM/application changes, unrelated foreground/virtual-service
  preservation, conditional start/stop refusal on concurrent changes, both opt-in
  mismatches with successful/failed backups, and stopped-VM forwarding-query failure.
  Wire-level LocalAPI tests cover exact ETag/If-Match, null initial configuration,
  local transport/authentication, redaction, and no retry/rollback on failures.
  A **read-only** GET through the new adapter succeeded against the existing daemon
  with an ETag; no configuration was written and no service was changed. Runtime
  help and `git diff --check` passed; command/settings documentation was checked
  against the changed implementation. No new CLI flags or settings were added.
  **Not verified during implementation:** real conditional writes/start-stop
  lifecycle with the new adapter and fresh live Podman checks. Publication evidence
  is recorded separately in PR #18. The user subsequently completed the Air's
  real-HTTPS invalid-credential and redirect/origin checks in the
  [remote checklist](docs/tailscale-readkit.md#real-https-checks--human-assisted):
  invalid API password rejected, valid account still synchronizes, compatibility
  `PASS`/HTTP 200, root HTTP 302 to same-origin `/i/` then HTTP 200, `/api/` HTTP 200
  without redirect, and displayed API links use the same HTTPS origin. Remote
  invalid-authentication HTTP status was not captured; mini self-HTTPS remains
  unresolved. Older device evidence below is historical, not a new state-matrix
  rerun. No new CI, alternate-platform or typechecker result is claimed; this
  implementation checkpoint alone is not merge approval.

- **Publication verification correction (after `0167ac3`, on `issue-3`):**
  Fresh isolated verification of the prepared issue #3 tree passed 190 tests plus
  Ruff, but its actual pre-push hook failed
  `test_public_operations_work_without_click`: inherited `GIT_DIR` made callable
  developer setup inspect the real repository's remote/hooks instead of the selected
  disposable project. A safe reproduction using only a disposable outer repository
  failed in 2.63s with `GIT_DIR` and passed in 0.92s without it. Child environments
  now exclude repository-location/index/object selectors while retaining Git
  configuration/authentication variables and existing-hook refusal guards. Eight
  focused CLI/callable regressions passed in 3.29s, covering foreign Git contexts,
  test-child isolation and preservation of global `core.hooksPath`/foreign hooks;
  seven had failed before the fix. The original inherited-`GIT_DIR` reproduction
  then passed in 0.95s. No real hooks were overwritten or bypassed. Full isolated
  candidate and actual push-hook results are recorded in the publication PR evidence;
  these focused results alone are not publication verification.

- **Issue #3 implementation checkpoint (`0167ac3`, `issue-3`, based on `cb5ed36`):**
  `uv sync --locked` succeeded. Final `uv run --locked rss-dev check` passed **190
  default tests in 78.40s**, six live deselected, with Ruff lint/format clean.
  Thirty-six new command/runtime checks cover scoped forwarding, repetition,
  ownership/handler/target/Funnel conflicts, certificate/policy/disconnection errors,
  effective authentication/API/base-URL gates, installer/security refusal, backup
  resume, reserved fixture records, hidden prompts, sanitized API results and
  explicit loopback diagnostics/refusal before transmitting credentials to unverified
  publications. Invalid opt-in on `replace` is refused before any service mutation;
  malformed HTTP status lines are sanitized rather than leaking upstream content.
  Tracer regressions failed before fixes. An earlier baseline passed 166 tests but
  failed lint/format; explicit corrections and full reruns passed. `rss --help` and
  `rss api-check --help` were checked against the command reference; `git diff --check`
  passed. No separate typechecker is configured.

  With the existing ownership directory, `RSS_TAILSCALE=1`, a private external
  `RSS_BACKUP_DIR` and normal fixture-file permissions,
  `uv run --locked pytest -p no:cacheprovider -m live -q --tb=short` passed **six
  tests in 66.79s** (189 default deselected). This retained suite exercises real
  security failure/recovery, synthetic-user HTTP state/persistence, replacement and
  source-absent recovery. A real private `start` afterward restored forwarding and
  the application security audit. Both effective Apache PHP gates (PHP 8.4.27,
  upload progress disabled, probes removed), cron, immediate fetching and installed
  HTTPS/API authentication settings passed. Loopback-only publication is unchanged.
  After that live run, an API protocol-error redaction guard was added and the full
  default baseline rerun; the six-test live suite was not repeated for that
  diagnostic-only guard (its failure is covered by the isolated HTTP boundary).

  Human-assisted acceptance: the user completed the installer and set web/API
  credentials locally; both actual ReadKit clients (macOS on the Air and iPhone)
  accept the instance origin unchanged, without `/api/greader.php`, and display the
  public feed. The user confirmed all four read/unread and star/unstar cross-client
  checks with FreshRSS web corroboration, no certificate-warning bypass, and roughly
  **30 seconds** of observed propagation (not an SLA). Normal stop created a verified
  external backup, removed the owned route, closed loopback HTTP and stopped only
  the VM while Tailscale remained connected; the user confirmed both clients could
  not sync. After normal private start, both clients synchronized and the deliberately
  read/starred article retained both markings. Repeated starts/stops and isolated
  unrelated-route preservation checks pass; the real pre-existing Serve config was
  empty. See the [sanitized device checkpoint](docs/tailscale-readkit.md).

  Diagnostics/failures: the iPhone initially exposed HTTP 503 because split optional
  boolean flags stopped PHP getopt parsing, leaving the API off and anonymous reading
  on. Serving was stopped with a verified backup; `--option=value` and effective
  read-back gates fixed it. iPhone HTTPS and loopback GReader diagnostics now PASS.
  The user's real `api-check --loopback` returned authenticated true, one subscription
  and one sampled article; deliberately invalid local credentials returned HTTP 401.
  The mini's own HTTPS-origin requests still fail during TLS, despite successful
  remote clients and certificate-only retrieval. Default HTTPS `api-check` remains
  unverified on this host; the explicitly approved loopback mode truthfully reports
  `https_verified: false`, never an implicit TLS bypass. A certificate probe using
  `/dev/null` as a key destination exited 1; certificate-only retrieval then exited 0,
  with no private key saved by the probe. CLI 1.104.7 and daemon 1.104.2 differ; no
  upgrade, restart or speculative policy broadening was performed. Real certificate/
  policy failure injection and exact reader app/OS versions were not recorded; isolated
  failures are not live infrastructure evidence. No new CI, Python 3.10 or Linux/x86
  result is claimed. Temporary private verification backups are retained outside Git,
  **not durable user backup configuration**. The application is left running privately;
  publication, review and merge remain separate user choices.

- **Issue #14 local checkpoint (`fix/issue-14-cask-inventory`, based on `3cb1aed`):**
  `uv sync --locked` succeeded. Before changes, `uv run --locked rss-dev check`
  passed 128 default tests. After the fix, the same full baseline passed
  **154 default tests in 108.34s**, six live deselected, with Ruff lint/format clean.
  `uv run --locked pytest tests/test_commands.py --tb=short` passed all **57**
  command tests; `uv run --locked rss setup --help` exited 0 with the unchanged
  no-argument/no-option setup interface (except `--help`). The incomplete-cask
  success regression failed before the fallback; unusable-data regressions failed
  before validation was added. Isolated external-command fixtures cover fallback
  success/repetition, both query failures, malformed/inconsistent data, empty
  inventories, original-warning/raw-name retention, unrelated-resource preservation,
  and upgrade/reinstall refusal. These are simulated setup checks, **not an actual
  new-Mac setup or real incomplete-cask repair**. No real Homebrew installation,
  Podman lifecycle operation, live test, new CI run or Python 3.10 check was run
  for this change. No separate typechecker is configured. Work remains local
  until separately published; issue handoff is not PR review or approval.

- **PR #15 feedback-fix checkpoint (2026-11-15, local changes based on `c5778aa`):**
  Addressed C1–C3 with first-activation image checks, bounded archive-path metadata,
  and an isolated recovery-fixture backup locator. On Apple Silicon / macOS 27.2 /
  Python 3.13.18 / Podman 6.2.0, `uv run --locked --offline rss-dev check` passed
  **128 default tests in 108.77s**, six live deselected, with Ruff lint/format clean.
  With private external `RSS_BACKUP_DIR` and `umask 022`,
  `uv run --locked --offline pytest -p no:cacheprovider -m live -q --tb=short`
  passed **six live tests in 57.77s**, including the source-absent authenticated
  recovery drill and backup-locator preservation assertion. Regression failures
  were observed before fixes: a changed first-activation pin launched successfully,
  deep paths amplified retained memory, and fixture backups replaced the normal
  locator (or created it when absent). The bounded deep-path regression's measured
  peak Python allocation fell from **76,318,265 to approximately 7,800,000 bytes**,
  including installed-CLI imports/startup; this is not a universal process-RSS bound.
  Matching first activation, failed-start retry, backup-before-first-start, legacy
  provenance and a later deliberate pin update have controlled lifecycle coverage;
  the synthetic version-change tests do not execute a real database upgrade or
  downgrade. Real start, verify and backup-on-shutdown succeeded. The VM was left
  stopped again; private verification archives are temporary, not durable backup
  configuration. No new CI, physical second-host, Linux/x86 or Python 3.10 result
  is claimed. These fixes remain local until separately published.

- **Issue #2 local checkpoint (2026-11-15, `feat/issue-2-backup-restore` based on `22ea43e`):**
  On Apple Silicon / macOS 27.2 / Python 3.13.18 / Podman 6.2.0,
  `uv run --locked --offline rss-dev check` passed **113 default tests in 105.16s**,
  six live deselected, with Ruff lint/format clean.
  With an explicitly configured private external `RSS_BACKUP_DIR` and `umask 022`,
  `uv run --locked --offline pytest -p no:cacheprovider -m live -q --tb=short`
  passed **all six live tests in 55.90s**. The new drill exercised production
  Click backup while running/stopped, complete removal of its disposable source,
  restore into a fresh volume, explicit selection and HTTP verification of login,
  subscription, article content, read/unread and starred state. Command-boundary
  regressions cover failed/interrupted exports, private external destinations,
  shutdown backup failure, incomplete-target refusal, wrong-image refusal without
  interrupting service, conflicts and malicious/corrupt archives including PAX
  traversal and sparse/link entries. Tracer regressions were observed failing
  before their implementations.
  An earlier full live attempt had **two failures / four passes** because inherited
  `umask 077` made existing public publisher fixture files root-owned `0600`:
  HTTP 403 reproduced, and changing only their mode to `0644` restored HTTP 200.
  Those unrelated fixture implementations were not changed; the normal-umask run
  above is the passing result, not a claim of umask-independent live fixtures.
  Real verify, backup-on-shutdown and explicit backup from an already-stopped VM
  also succeeded. Both resulting archives independently passed `/usr/bin/unzip -tq`,
  whole-ZIP SHA-256 comparisons and mode `0600` checks. Fixture containers/volumes
  and reserved test records were removed; only the real application/data volume
  remained before shutdown. The real security audit was restored, the VM left
  stopped and loopback port 8080 confirmed closed. Verification-only archives were
  retained in a private temporary host directory outside Git; this is **not** a
  durable user backup configuration. No permanent `RSS_BACKUP_DIR`, real credentials,
  new dependency, new CI run, physical second-host recovery or Linux/x86 verification
  is claimed. Publication/review remain separate.

- **Readiness/fixture checkpoint (2026-11-15, local changes based on `00e33a8`):**
  `uv run --locked --offline rss-dev check` passed **75 default tests in 42.09s**,
  five live deselected, with Ruff lint/format clean. Four readiness regressions
  failed before the fix and passed afterward: real HTTP disconnects and controlled
  connection resets, both recoverable and persistent. They verify that recovery
  reaches scheduler/fetch stages and persistent failures stop at 30 attempts.
  Developer tests retain CLI/callable hostile-environment isolation and actual
  push coverage, now using import sentinels and lighter hook-refusal fixtures.
  `uv run --locked --offline pytest -p no:cacheprovider -m live -q --tb=short`
  passed **all five live tests in 38.35s**. Production cron remains `*/20`;
  the real application audit was restored, the VM stopped and loopback port closed.
  No commit/push or new CI run is implied by this local checkpoint.

The earlier review-fix results below describe their exact candidates, before
observer removal, not the changed test suite.

- **Historical local review-fix checkpoint (2026-11-15):** candidate tree
  `18f4f79e054f4b03fe821b9fc36fd4a69bb0b53b`, based on PR head `bf39851`;
  only this README's evidence was updated afterward. No commit/push is implied.
  `uv sync --locked --offline`
  and `uv run --locked --offline rss-dev check` passed: **71 default tests**, six
  live deselected, Ruff lint/format clean. This includes template-copy failure/retry,
  rootful/rootless shutdown refusal/failure and cleanup retry simulations, package
  relocation, and checkout-B isolation under hostile import/home overrides.
  Real owned VM start/stop/start/status and the live security/stale-probe check
  passed on Apple Silicon / Python 3.13.18. No Homebrew installation, hook activation
  or new GitHub CI run was performed.
- **S1 passed locally:** `pytest -m live tests/integration/test_live_replace.py`
  passed in **10.96s**, exercising the real Click replacement implementation under
  isolated resource names and verifying installed subscription, article identity/
  content and deliberate read/star state through HTTP afterward. It used synthetic
  credentials and a disposable volume, not the real application user's state.
- **Historical S2 acceptance (before observer removal):** the former
  `--observe-cron` run passed **all six live tests in 1533.26s**.
  Natural runs at **2026-11-15 02:40:01 and 03:00:01 UTC** collected successive
  controlled articles verified through HTTP, without shortening production
  scheduling/TTL/cache policy. The suite also reverified effective Apache security,
  stale-probe cleanup, public/controlled fetching, purge defaults, read/star state,
  real Click replacement, VM restart and port migration. Fixture teardown passed;
  the real application audit was restored and the VM stopped afterward. An earlier
  interrupted attempt was not counted; its ownership-checked test resources were
  removed before this successful rerun.
- Python 3.10 (the declared minimum), alternate image architectures, native Linux
  rootless-Podman integration and other Mac providers/versions are unverified.
  CI's Linux **baseline** is not native-container integration. No separate
  typechecker or visual browser automation is claimed.
- [#7](https://github.com/brucepucci/rss-aggregator/issues/7) owns broader controlled
  publisher/error cases, interruption-safe fixtures, a platform coverage matrix
  and Linux/Mac integration responsibilities. Extend the existing entrypoint and
  workflow rather than introducing a competing baseline. Feature issues retain
  their own acceptance tests; passing mocks is not real-service evidence.
- Issue #3's local checkpoint records actual HTTPS/ReadKit authentication,
  bidirectional state and restart persistence on the Air and iPhone. The mini's
  own HTTPS diagnostic path remains unresolved; explicit loopback diagnostics do
  not prove HTTPS. Private-feed authorization and destructive cleanup remain
  unimplemented/unverified. Backup/restore verification is separately scoped
  in the issue #2 checkpoint. The volume is not a
  backup. No baseline pass or historical run establishes complete acceptance,
  production security or permission to merge.

## Architecture

- [Isolate local development with Podman](docs/adr/0001-isolate-local-development-with-podman.md)
- [Portable state and consistent external backups](docs/adr/0002-portable-state-with-consistent-external-backups.md)
- [Private Tailscale access and shared reading state](docs/adr/0003-private-tailscale-access-and-shared-reading-state.md)

These decisions describe accepted architecture. The local runtime above implements
#1's local runtime, #2's backup/recovery and #3's optional private forwarding/API
commands; physical-device acceptance remains separately recorded, not implied by implementation.
