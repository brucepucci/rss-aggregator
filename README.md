# Local FreshRSS

A session-only FreshRSS instance on an Apple Silicon Mac: Homebrew's Podman CLI,
a dedicated **rootless** Linux VM, the official Debian image pinned by digest,
and SQLite in a named volume on the VM's native filesystem. No host PHP or
database server, Podman Desktop, privileged socket helper, login item, launch
agent, or automatic application startup is installed by this project.

This implements [issue #1](https://github.com/brucepucci/rss-aggregator/issues/1).
Backups, guarded removal, Tailscale, ReadKit and Linux deployment are **not yet
implemented**. See [architecture decisions](docs/adr/).

## Getting Started

### Prerequisites

- Apple Silicon macOS with virtualization available, Homebrew (`https://brew.sh`),
  Git, and **Python 3.10+** on `PATH`. Python uses only its standard library; setup
  does not install Python. Check `python3 --version` and `brew --version`.
- Internet access to Homebrew, the Podman VM image registry, Docker Hub, and GitHub.
- An unused Mac loopback TCP port (default `8080`) and approximately 2 GiB guest
  RAM plus VM overhead. The VM starts with 2 CPUs and 20 GiB virtual disk capacity;
  this is capacity, not a promise of immediate physical disk consumption.
- Do not run another Podman VM while starting this one. The command explains any
  conflict and **never stops the other VM** for you.

### Install and start

From a checkout of this repository:

```sh
./scripts/rss setup
./scripts/rss start
./scripts/rss status
```

`setup` inventories Homebrew formula/cask versions/dependencies and Podman
machines/connections, including containers, volumes and images reachable through
running machine connections. Stopped machines and non-machine connections are
recorded but not started/contacted to enumerate their contents. It previews the CLI
installation, records additions, and initializes the dedicated `rss-aggregator` VM. It does not start a stopped VM. Existing name collisions are
not adopted. If the preview includes an upgrade/reinstall of an existing package,
setup stops: obtain explicit approval and resolve the host dependency plan before
retrying. There is deliberately no bypass flag. Automatic Homebrew updates,
installation upgrades, dependent checks and install cleanup are disabled.

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
published container port 80. Only after the check passes and its temporary probe
is removed does Apache open port 80. The official entrypoint and default command
are retained. Mac publication is **`127.0.0.1:8080` only**, not LAN/IPv6/all interfaces.

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
| `./scripts/rss start` | Start the VM if needed; restart an existing application to re-run the security gate; check cron; immediately run feed actualization. |
| `./scripts/rss status` | Report VM/application state, inspect installed cron and its live daemon, show last security-check time. Does not start anything. |
| `./scripts/rss fetch` | Run feed actualization immediately, or explain that the installer is pending. |
| `./scripts/rss verify` | Restart/recheck the running application, including effective Apache PHP, cron and immediate actualization. Brief service interruption. |
| `./scripts/rss replace` | Stop/remove **only the owned application container**, then recreate it using the same named data volume and current checked-in configuration. |
| `./scripts/rss stop` | Stop application writers, verify the containers have exited, then stop only the project VM. Repeating it reports already stopped. |

Commands are serialized by a local lock. Repeated starts restart the application
but do not create another VM, user, subscription, or data volume. Errors return
nonzero; resources are retained for diagnosis rather than automatically rolled
back or deleted. A failed startup can leave the VM running; use `status` and
`stop`. A VM with unrelated **running** containers is not stopped automatically.

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

### ReadKit connection — deferred

Do not enable API access or configure ReadKit for this slice. The instance is
browser-only and loopback-only. Private Tailscale HTTPS and the Google
Reader-compatible API with a separate API password are planned in issue #3;
ReadKit's macOS/iOS end-to-end connection is not yet verified.

### Backup and restore — not implemented

The complete FreshRSS `data` directory (SQLite databases, users, settings,
subscriptions, articles, reading state, feed cache and logs) is stored in
**`rss-aggregator-data`**, a project-labelled Podman named volume inside the VM.
It is **not** a Mac bind mount and **not** an external backup. The ordinary Podman
VM may expose its default Mac shared directories, but the database does not use
them. Container replacement and VM stop/start preserve the volume; VM deletion
or loss does not.

Per the approved [ADR 0002 deferral](docs/adr/0002-portable-state-with-consistent-external-backups.md),
normal shutdown does **not** require/create a backup until issue #2's backup
infrastructure exists. There is no supported backup/restore command yet. Do not
store irreplaceable articles here or delete the VM assuming it can be recovered.
OPML is only a subscription export, not an instance backup. Future backups will
contain secrets and require external, private storage.

### Safe cleanup

For now, safe cleanup is **`./scripts/rss stop`**. No VM/volume deletion, prune,
package uninstall, or destructive convenience command is provided. **Deleting the
VM destroys its native named volumes and all FreshRSS data.** Do not run global
Podman prune/reset or remove existing Homebrew dependencies. Do not delete
`.local/owner.json`: it is the safety record associating this checkout with the
machine/connection and container/volume ownership labels. Preserve `.local/` when
moving this checkout, and keep a single controlling checkout. The package-addition
ledger is evidence for later conservative cleanup, not permission to uninstall
packages that other projects may subsequently adopt.

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
| `RSS_STATE_DIR` (optional environment variable) | Ownership/inventory/lock/security record directory; `<checkout>/.local`. Not the FreshRSS database location. | Set an absolute private path **outside Git** before setup and use it consistently. Changing it does not adopt or migrate an existing machine. |
| `config/image.json` | Official Debian FreshRSS `1.30.4`, digest-pinned OCI index, explicit `linux/arm64`, verified platform manifest and source revision. | Change only after checking current advisories and verifying registry identity/runtime. `replace` applies a pin change. No floating-tag update command. |
| `config/config-user.custom.php` | New-user defaults: no age/maximum article purge; TTL `60` seconds. | `replace` copies the template; **existing users are not rewritten**. Change existing-user settings locally in FreshRSS, including feed/category overrides. |
| `config/public-feed.opml` | One public FreshRSS releases feed for new users. | `replace` copies the import template; existing subscriptions remain unchanged. Enter private feeds locally only in a later slice. |
| VM resources (fixed in command) | Machine `rss-aggregator`, rootless, initial 2 CPUs / 2048 MiB RAM / 20 GiB disk. | No project resize/delete command; do not change machine identity or connection records to work around ownership checks. |
| Container settings (fixed in command) | `CRON_MIN=*/20`, `TZ=UTC`, `ENABLE_ACCESS_LOG=0`, syslog copying off, `TRUSTED_PROXY=0`, `--restart=no`, internal security-gate listener. | Keep the official entrypoint/default command; apply reviewed changes with `replace`. No host cron/service registration. |

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
and public registrations are disabled by default; bundled extension enablement is
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

## Development verification

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/rss tests/*.py
```

Command-boundary tests use controlled external executables and temporary state;
they do not install packages, boot a VM, or contact Homebrew. Optional linting, if
Ruff is already installed: `ruff check --isolated --target-version py310 scripts/rss tests`.

After `setup` and `start`, opt into real Podman/HTTP integration checks:

```sh
RSS_LIVE=1 python3 -m unittest discover -s tests -p 'live_*.py' -v
RSS_LIVE=1 RSS_OBSERVE_CRON=1 python3 -m unittest discover -s tests -p 'live_*.py' -v
```

These checks create uniquely named, labelled **disposable test containers/volumes**,
complete the HTTP installer with an in-memory random test password, authenticate,
fetch the public feed, change read/star state, replace a test container, test a
base-URL/loopback-port change, and stop/start the project VM with test writers
stopped first. They interrupt the
project session: don't run them while using FreshRSS. Cleanup removes only those
unique labelled test fixtures, never the application's named volume. The negative
security test also briefly adds a test-only Apache INI override to the owned
application, verifies that startup fails closed with no diagnostic left behind,
then removes that override and restores verified serving. Let the test finish so
its recovery handler can run. A fixture-only allowlist permits fetching its own controlled loopback test feed; production
retains upstream's empty internal-host allowlist. The cron observation waits
**20–40 minutes** for two natural executions and verifies new controlled articles;
it does not shorten the schedule or count manual refreshes as scheduled fetches.
Fast regression tests check the observer against a known execution and run the
exact cron fetch command against changing feed content. Only that fast fixture
reduces the eligibility TTL and minimum cache floor to avoid minute-long waits.
The natural timed scenario retains production TTL/cache settings. The controlled
publisher explicitly returns `no-cache, max-age=0`; the image otherwise serves
static Atom files with long-lived asset caching, which is unsuitable for this test.

See [sanitized verification evidence](docs/verification/issue-1.md) for actual
results and remaining limitations; an available command is not itself a passed
check. There is no existing application typechecker/build system in this
repository; Python compilation is a syntax check, not a static typecheck.
