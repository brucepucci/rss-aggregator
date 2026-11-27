# Personal RSS aggregator

Collect feeds in one place and keep reading state shared across your devices.
[FreshRSS](https://freshrss.org/) supplies the browser interface, subscriptions,
articles and APIs. **This repository manages FreshRSS; it does not fork it.** It
runs FreshRSS natively on Apple Silicon macOS, with guarded lifecycle commands,
complete instance backups and optional private HTTPS for ReadKit.

Manual operation is the default. Normal-user-session recovery and daily backups
are independent opt-ins. The native deployment does not require Podman or a VM.
Moving an existing Podman instance is a separate [owner-assisted migration](docs/operations.md#one-time-migration-from-podman),
not part of ordinary setup or an in-place update. Real archive rehearsal,
launchd/login, cutover, both ReadKit clients and independently retrieved Time
Machine recovery are open native acceptance gates; historical VM checks do not
certify the native service.

## Contents

- [Architecture](#architecture)
- [Getting started](#getting-started)
- [Routine CLI commands](#routine-cli-commands)
- [Essential configuration](#essential-configuration)
- [Common troubleshooting](#common-troubleshooting)
- [FAQ](#faq)
  - [How do I subscribe to a new feed or remove a subscription?](#how-do-i-subscribe-to-a-new-feed-or-remove-a-subscription)
  - [How do I import or export subscriptions?](#how-do-i-import-or-export-subscriptions)
  - [How do I back up and restore the complete FreshRSS instance?](#how-do-i-back-up-and-restore-the-complete-freshrss-instance)
  - [How do I deploy, start, stop, and update the service?](#how-do-i-deploy-start-stop-and-update-the-service)
  - [How do I enable automatic Mac startup and check recovery?](#how-do-i-enable-automatic-mac-startup-and-check-recovery)
  - [How do I safely remove and recreate the environment?](#how-do-i-safely-remove-and-recreate-the-environment)
  - [How do I move the aggregator to another machine?](#how-do-i-move-the-aggregator-to-another-machine)
  - [How do I connect a reading app or access the instance from another device?](#how-do-i-connect-a-reading-app-or-access-the-instance-from-another-device)
  - [Why are articles missing or feeds no longer updating?](#why-are-articles-missing-or-feeds-no-longer-updating)
  - [How do I view service logs and investigate feed-fetching errors?](#how-do-i-view-service-logs-and-investigate-feed-fetching-errors)
  - [How do I safely handle private/paid feed credentials?](#how-do-i-safely-handle-privatepaid-feed-credentials)
- [Documentation](#documentation)

## Architecture

```text
Publisher feeds ──feed fetching──> FreshRSS on native FrankenPHP/Caddy
                                     └── SQLite and complete local instance data
                                           └── verified backup ZIP outside Git
Local browser ──HTTP 127.0.0.1:8080──> same instance
Browser / ReadKit ──private HTTPS──> Tailscale Serve ──> same loopback listener
```

The [reviewed pin](config/native.json) is FreshRSS **1.30.4** at upstream revision
[`08f97bd497ace733c151ea1c709d85cd5b65cc53`](https://github.com/FreshRSS/FreshRSS/commit/08f97bd497ace733c151ea1c709d85cd5b65cc53),
FrankenPHP **1.14.0** with PHP **8.6.0** and Caddy **2.11.9**. They run as a normal
Mac user from project-managed files, without global PHP or a separate database
server. A private directory is not a sandbox. SQLite stays on local, non-synced
storage. FreshRSS fetches on startup and at UTC minutes **00, 20 and 40** while
running; reader synchronization exchanges articles and markings, not publisher
fetch requests. When the instance is stopped, clients cannot synchronize.

## Getting started

Use a normal user on an Apple Silicon Mac (macOS 12+), with Git, native
[uv](https://docs.astral.sh/uv/getting-started/installation/), internet access
and an unused loopback port. **Before the first `start`, verify the port is not
already a target of Serve, Funnel, or any other proxy or forwarding rule.** A
free port and `RSS_TAILSCALE=0` do not prove isolation. See the
[private-access safeguards](docs/tailscale-readkit.md#prerequisites-and-ownership).
This path is for a **new local instance** without jobs or forwarding. For existing
data, use [native recovery](docs/backup-restore.md#recovery-on-this-or-a-clean-host)
or the [one-time legacy procedure](docs/operations.md#one-time-migration-from-podman)
before an empty start.

```sh
git clone https://github.com/brucepucci/rss-aggregator.git
cd rss-aggregator
uv sync --locked
uv run --locked rss setup
# Explicit private destination outside every Git checkout; choose your own.
export RSS_BACKUP_DIR="$HOME/Library/Application Support/rss-aggregator/backups"
export RSS_TAILSCALE=0
uv run --locked rss start
```

Open the returned loopback URL (normally **http://127.0.0.1:8080**). After startup
passes its PHP checks, complete the installer with **SQLite**, **Web form
authentication** and one FreshRSS user. Enter a strong password only in the local
browser. This interactive browser step is separate from the [native integration
check](docs/development.md#explicit-liveplatform-checks), which initializes a
synthetic user via the upstream CLI. Sign in, check the initial public FreshRSS
releases subscription, and run `uv run --locked rss fetch`. In
**Configuration → Archiving**, check the age and
maximum-article limits before you rely on keeping history. To finish, run
`uv run --locked rss stop` and confirm the returned backup was verified. Keep the
backup directory configured for subsequent shutdowns. A local backup is not an
independent copy. See [setup and ownership safeguards](docs/operations.md#installation-and-ownership).

## Routine CLI commands

From the checkout, run `uv run --locked rss <command>` (or `./scripts/rss
<command>`). Results are JSON on stdout; diagnostics are on stderr and failures
return nonzero. A successful status query alone does not establish health.

| Command | Use |
| --- | --- |
| `setup` | Install or check the reviewed native runtime without starting it. |
| `start` / `verify` | Start and record running intent, or restart/recheck an already running service without changing intent. |
| `status` / `fetch` | Inspect the selected instance, listener and scheduler, or fetch publisher feeds now subject to cache/backoff. |
| `stop` / `backup` | Stop writers and verify a shutdown backup, or back up and attempt to resume the prior serving mode. |
| `backup-status --check` | Check eligible daily protection, archive freshness, unresolved shutdowns, maintenance and resumption. |
| `supervision-status` | Inspect optional recovery, intent and failures. |
| `api-check` | Prompt locally for separate API credentials and diagnose private HTTPS/API. |
| `restore ARCHIVE --instance NAME` / `select-instance NAME` | Restore into a fresh named target, then explicitly select while stopped. |
| `upgrade` / `uninstall` | Preview owned runtime changes. Read the [safeguards](docs/operations.md#safe-cleanup) before approving. |

Use `rss --help`, `rss COMMAND --help` and the [complete CLI reference](docs/cli.md)
for exact syntax, results and failures.

## Essential configuration

Export settings in the shell that runs the CLI; `.env` is not automatically read.

| Setting | Default and ordinary use |
| --- | --- |
| `RSS_STATE_DIR` | `<checkout>/.local/native`. Private owned directory for native runtime, instance data and records. Legacy `.local` remains separate; do not point native commands at it. Use a fresh private path for each rehearsal. |
| `RSS_BACKUP_DIR` | **No default.** Explicit absolute private directory outside Git and all native state directories, required for backups and shutdown with data. Rehearsal uses a separate destination. |
| `RSS_PORT` | `8080`; unused and unforwarded IPv4 loopback port in `1024–65535`. Follow the [port-change procedure](docs/configuration.md#changing-the-loopback-port) for an existing deployment. |
| `RSS_TAILSCALE` | `0`. Follow the [private-HTTPS procedure](docs/tailscale-readkit.md#connect-and-verify-the-server) before setting `1`. `0` does not turn off unrelated forwarding to the same port. |

Keep passwords, private URLs, logs and archives out of Git and shared messages.
See the [full configuration reference](docs/configuration.md).

## Common troubleshooting

- **Setup, ownership or port refusal:** leave existing files, jobs and listeners
  intact; see [operator troubleshooting](docs/operations.md#common-troubleshooting).
- **Backup/shutdown failure:** inspect destination, free space and status. A failed
  explicit `backup` can have resumed service; shutdown export failure after
  quiescing leaves writers stopped. An older verified archive does not clear a
  failed or interrupted shutdown; inspect `backup-status` and retry `stop` after
  correcting the problem. [Failure behavior](docs/backup-restore.md#consistency-and-failure-behavior).
- **Lost instance selection:** do not start or back up a substitute instance.
  Use [operator troubleshooting](docs/operations.md#common-troubleshooting) to
  confirm and reselect the intended data while stopped.
- **Stale daily backups or interrupted maintenance:** use `backup-status --check`
  and the [reconciliation procedure](docs/backup-restore.md#interrupted-maintenance-and-reconciliation).
  Do not remove locks or holds to force startup.
- **HTTP is ready but you cannot sign in:** check the selected instance, URL and
  credentials locally; preserve the source archive rather than resetting a user
  to make recovery appear to pass.
- **Fetching or reader trouble:** see the [fetching FAQ](#why-are-articles-missing-or-feeds-no-longer-updating)
  and [private-access diagnostics](docs/tailscale-readkit.md#troubleshooting).
  Do not bypass TLS or the startup security checks.

## FAQ

### How do I subscribe to a new feed or remove a subscription?

Use **Subscription management → Add a feed or category** in FreshRSS. To unsubscribe,
open the subscription's settings and choose **Remove**. This can delete collected
articles and their markings; make a complete backup first if you might need them.
Synchronize your reader afterward.

### How do I import or export subscriptions?

Use **Subscription management → Import / export** for OPML. For an OPML-only
export select the feed list and deselect article exports. OPML is **not** an
instance backup: it cannot restore users, complete settings, article history or
reading state. Even a subscription export can include private URLs or tokens.
Feedbin migration is not covered.

### How do I back up and restore the complete FreshRSS instance?

Export `RSS_BACKUP_DIR` and run `backup`; `stop` also backs up. For restore, use a
trusted native ZIP and the exact recorded pin, then explicitly select the fresh
target and start. See [backup and recovery](docs/backup-restore.md), including
[opt-in daily backups](docs/backup-restore.md#opt-in-daily-mac-backups) and the
[Time Machine copy recovery drill](docs/backup-restore.md#time-machine-copy-recovery-drill).
There is no automatic pruning, encryption or upload.

### How do I deploy, start, stop, and update the service?

Use [Getting started](#getting-started) and `start`/`stop`. Updating the checkout
or rerunning `setup` does not upgrade the native runtime. Use the
[explicit reviewed upgrade procedure](docs/operations.md#upgrade-the-pinned-runtime);
do not assume a new runtime can activate old data or an older release can read
newer data. No Linux or Intel Mac deployment is supported here.

### How do I enable automatic Mac startup and check recovery?

After an owner-approved setup and verified backup, follow
[opt-in startup and recovery](docs/operations.md#opt-in-mac-startup-and-recovery).
Install only the wanted job. `supervision-status` distinguishes explicit stop,
maintenance and failed recovery; only explicit `start` changes stopped intent.
There is no service before login/unlock and no change to sleep or FileVault.

### How do I safely remove and recreate the environment?

Follow [safe cleanup](docs/operations.md#safe-cleanup). Stop and back up, remove
both owned jobs if installed, then preview and explicitly confirm `uninstall`.
It preserves data and archives; `setup` with the same pin can reinstall the
runtime. Permanent data deletion is a separate decision. Do **not** delete legacy
state or its VM: [retirement](docs/operations.md#legacy-retirement-gate) requires
separate owner approval and an access interruption to hand off forwarding safely.

### How do I move the aggregator to another machine?

Transfer a verified native archive privately, keep the matching reviewed pin and
follow the [clean-host recovery procedure](docs/backup-restore.md#recovery-on-this-or-a-clean-host)
on a compatible Apple Silicon Mac. Make fresh ownership records and recheck
host-specific configuration. Copying a checkout or OPML alone is not migration.

### How do I connect a reading app or access the instance from another device?

Follow the [private HTTPS/ReadKit runbook](docs/tailscale-readkit.md). In ReadKit
choose **FreshRSS**, enter the HTTPS **origin** (without `/api/greader.php`), and use
the same FreshRSS user and its **separate API password** on both devices. Native
client acceptance is owner-assisted; loopback API checks are not HTTPS evidence.

### Why are articles missing or feeds no longer updating?

Check `status`, finish the installer and try `fetch`. FreshRSS respects feed TTL,
HTTP caching and publisher backoff; a successful fetch need not produce new
articles. ReadKit synchronization does not fetch publisher feeds. Check the
subscription's errors and archiving rules; feeds can contain excerpts only.

### How do I view service logs and investigate feed-fetching errors?

Open FreshRSS **Configuration → Logs** or the affected subscription, and inspect
`status` and the command's stderr. See [service logs](docs/operations.md#service-logs-and-feed-errors)
for retained diagnostics and secret-handling rules.

### How do I safely handle private/paid feed credentials?

Treat a private URL as a password. Enter it locally in FreshRSS, never in command
arguments, Git, chat, screenshots or shared evidence. Keep exports, logs and
backups private; permissions are not encryption. If exposed, rotate the credential
with the publisher. Paid access is an entitlement, not an authentication mechanism;
no particular paid publisher is certified here.

## Documentation

- **Operator:** [CLI reference](docs/cli.md), [configuration](docs/configuration.md),
  [operations](docs/operations.md), [backup/recovery](docs/backup-restore.md),
  [private HTTPS/ReadKit](docs/tailscale-readkit.md).
- **Developer:** [setup, checks and limits](docs/development.md),
  [architecture and decisions](docs/architecture.md), [glossary](GLOSSARY.md).
- **Agent:** [documentation rules and procedure index](AGENTS.md).
