# Personal RSS aggregator

Collect feeds in one place and keep reading state shared across your devices,
without giving up control of your subscriptions and collected articles.
[FreshRSS](https://freshrss.org/) provides the reading service: subscriptions,
feed fetching, articles, a browser interface and reader APIs. **This repository
manages FreshRSS; it does not implement or fork it.** It adds a pinned container,
guarded start/stop commands, complete instance backups and optional private HTTPS.

The working deployment is a **session-only Apple Silicon Mac instance**. ReadKit
on macOS and iOS has been verified with shared read/unread and starred state.
Always-on Linux deployment and destructive cleanup remain future work; paid-feed
authorization has not been verified. Nothing starts automatically after login or
reboot, and readers cannot synchronize while the instance is stopped.

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
  - [How do I move the aggregator to another machine?](#how-do-i-move-the-aggregator-to-another-machine)
  - [How do I connect a reading app or access the instance from another device?](#how-do-i-connect-a-reading-app-or-access-the-instance-from-another-device)
  - [Why are articles missing or feeds no longer updating?](#why-are-articles-missing-or-feeds-no-longer-updating)
  - [How do I view service logs and investigate feed-fetching errors?](#how-do-i-view-service-logs-and-investigate-feed-fetching-errors)
  - [How do I safely handle private/paid feed credentials?](#how-do-i-safely-handle-privatepaid-feed-credentials)
- [Documentation](#documentation)

## Architecture

```text
Publisher feeds ──feed fetching──> FreshRSS container (official image)
                                  inside a dedicated rootless Podman VM
                                    │
                                    └── SQLite + complete data in named volume
                                          └── verified backup ZIP outside VM/Git

Local browser ──http://127.0.0.1:8080──> FreshRSS
Other devices / ReadKit ──private HTTPS──> Tailscale Serve ──> same loopback listener
                         client synchronization (articles + reading state)
```

FreshRSS fetches feeds at startup and at UTC minutes **00, 20 and 40** while
running, subject to publisher caching/backoff. Synchronizing a reading app exchanges
articles and reading state with FreshRSS; it is not a request to fetch publishers.
The VM's named volume survives container replacement but **not VM deletion**.
External backups are essential.

Only loopback HTTP is published on the Mac; optional Tailscale Serve adds
trusted, tailnet-only HTTPS, not public access. There is no host PHP/database
installation. See the [official FreshRSS manual](https://freshrss.github.io/FreshRSS/en/),
[architecture details](docs/architecture.md) and
[architecture decisions](docs/architecture.md#decisions) for rationale and limits.

## Getting started

You need an Apple Silicon Mac with virtualization, [Homebrew](https://brew.sh/),
Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`;
internet access; a free loopback port (default `8080`); and roughly 2 GiB guest
RAM plus VM overhead. Do not run another Podman VM while starting this one.
The initial VM has 2 CPUs and 20 GiB virtual disk capacity.

```sh
git clone https://github.com/brucepucci/rss-aggregator.git
cd rss-aggregator
uv sync --locked
uv run --locked rss setup
# Choose an absolute private directory outside EVERY Git checkout.
export RSS_BACKUP_DIR="$HOME/Library/Application Support/rss-aggregator/backups"
uv run --locked rss start
uv run --locked rss status
```

`setup` inventories the host, installs missing Podman CLI dependencies and
initializes the dedicated VM. It refuses ownership conflicts and unapproved
upgrades of existing packages; do not bypass these checks. No Podman Desktop,
privileged helper or automatic startup is needed. See
[setup details and refusals](docs/operations.md#installation-and-ownership).

After `start` succeeds:

1. Open **http://127.0.0.1:8080** (or the returned URL). Complete the installer with
   **SQLite**, **Web form authentication** and one FreshRSS user with a strong
   password. Enter credentials only in the local browser; never choose “None” authentication.
2. Sign in. The initial subscription is the public
   [FreshRSS releases feed](https://github.com/FreshRSS/FreshRSS/releases.atom).
   Run `uv run --locked rss fetch` and confirm articles appear.
3. In **Configuration → Archiving**, confirm age and maximum-article purge limits
   are unchecked; avoid feed/category purge overrides.
4. Mark an article read and starred. Run `uv run --locked rss replace` and sign
   in again; confirm the subscription, articles and reading state survived.
   Check again after `uv run --locked rss stop` then `uv run --locked rss start`.

Keep `RSS_BACKUP_DIR` set for shutdown. `stop` creates a verified backup before
stopping the VM. This releases VM memory and stops serving/fetching; it preserves
application data. **Do not delete the VM, prune Podman, or remove `.local/` to clean
up.** See [safe cleanup](docs/operations.md#safe-cleanup).

## Routine CLI commands

Run these from the checkout. Results are indented JSON on stdout; diagnostics go
to stderr and failures return nonzero. `./scripts/rss` is an equivalent launcher.

| Command | Use |
| --- | --- |
| `uv run --locked rss start` | Start/recheck the service; a repeated start briefly restarts it. |
| `uv run --locked rss status` | Inspect the VM, application, scheduler and private forwarding without starting anything. |
| `uv run --locked rss fetch` | Fetch publisher feeds now, subject to caching/backoff. |
| `uv run --locked rss backup` | Make a complete backup, then attempt to restore the previous serving state. |
| `uv run --locked rss stop` | Back up, remove owned forwarding and stop only the project VM. |
| `uv run --locked rss api-check` | Prompt for separate API credentials and check private HTTPS/API access. |
| `uv run --locked rss --help` | Show commands; append `--help` to any subcommand for its syntax. |

For restore, volume selection, replacement and all arguments, flags, defaults,
outputs and failures, use the [complete CLI reference](docs/cli.md).

## Essential configuration

Set environment variables in the terminal before running commands; repeat them in
new shells. The CLI does not load a `.env` file automatically.

| Setting | Default and ordinary use |
| --- | --- |
| `RSS_BACKUP_DIR` | **No default; required for backups and shutdown with data.** Absolute private directory outside Git. Permissions are not encryption; keep durable independent copies. |
| `RSS_PORT` | Optional, `8080`. Choose an unused port in `1024–65535` consistently; an existing container requires `replace` and an installed instance needs the correct base URL. |
| `RSS_TAILSCALE` | Optional, `0` (local browser only). Set `1` for private HTTPS after the [ReadKit prerequisites](docs/tailscale-readkit.md#prerequisites-and-ownership). |
| `RSS_STATE_DIR` | Optional, `<checkout>/.local`. Private ownership records, **not the database**. Keep one controlling checkout; another worktree must explicitly use the original directory, not copy/fabricate it. |

Enter passwords and private feed URLs only locally: in FreshRSS, or the hidden
API-password prompt for diagnostics. Keep backups, logs and generated state out
of Git and shared messages. Use the
[full configuration reference](docs/configuration.md) for applying port/image
changes, managed records and existing-user versus new-user settings.

## Common troubleshooting

Start with `uv run --locked rss status` and the command's stderr message.
A failed operation can leave the VM running; do not assume failure means the
service is closed.

- **Setup/ownership/port conflict:** preserve other workloads and ownership files;
  follow [setup troubleshooting](docs/operations.md#common-troubleshooting), not a global reset.
- **Backup or shutdown failure:** check `RSS_BACKUP_DIR`, permissions and disk space.
  After shutdown quiesces writers, backup failure leaves the application stopped
  and VM running; fix the cause and retry `stop`. A failed explicit `backup` may
  have resumed serving. [Failure behavior](docs/backup-restore.md#consistency-and-failure-behavior).
- **Missing articles:** check the [fetching FAQ](#why-are-articles-missing-or-feeds-no-longer-updating)
  and [logs](#how-do-i-view-service-logs-and-investigate-feed-fetching-errors).
- **ReadKit/certificate/API failure:** use the separate API password; check Tailscale
  on both devices and `/api/` diagnostics. Never disable TLS verification.
  [Private-access runbook](docs/tailscale-readkit.md).
- **Security gate failure:** do not enter credentials or bypass checks.
  [Security policy](docs/architecture.md#image-and-php-security-policy).

## FAQ

### How do I subscribe to a new feed or remove a subscription?

In FreshRSS's browser interface, use **Subscription management → Add a feed or
category**, then **Add a feed**, and enter the URL locally. To unsubscribe, open
the subscription's settings and choose **Remove**. This deletes its collected
articles (including starred articles and reading state) and related user queries;
make an [instance backup](docs/backup-restore.md) first if you may want them back.
Synchronize your reader afterward. Private URLs require the
[credential precautions below](#how-do-i-safely-handle-privatepaid-feed-credentials).

### How do I import or export subscriptions?

Use FreshRSS **Subscription management → Import / export** to import an OPML
file. For an OPML-only export, select **Export list of feeds (OPML)** and deselect
article exports (otherwise you may receive a ZIP). Inspect imported subscriptions
and fetch before expecting articles. OPML transfers subscriptions and some
feed-specific settings, **not** users, complete configuration, article history or
reading state; it is not an instance backup. Exports may contain credential-bearing
URLs, cookies or headers: keep them private. Feedbin migration is unvalidated;
see [upstream import/export help](https://freshrss.github.io/FreshRSS/en/users/04_Subscriptions.html#import-export).

### How do I back up and restore the complete FreshRSS instance?

Set `RSS_BACKUP_DIR` and run `uv run --locked rss backup`. It briefly stops writers
and verifies an external archive before attempting to resume the prior state.
`stop` also backs up. Copy successful archives to independent durable storage;
there is no automatic pruning, encryption or upload.

Restore validates a **trusted archive into a fresh volume**, then requires explicit
selection and startup. Follow the [complete recovery procedure](docs/backup-restore.md#recovery-on-this-or-a-clean-host),
including the matching image requirement; do not overwrite the current volume or
copy ownership records. Verify login, subscriptions, articles and reading state.

### How do I deploy, start, stop, and update the service?

Use [Getting started](#getting-started) for the supported Mac deployment, and
`rss start` / `rss stop` through the launcher above for daily operation.
Always-on Linux deployment is not implemented. A sleeping/stopped host cannot
provide reliable fetching or synchronization.

There is no automatic image update. Back up first, review the official image's
security advisories and exact platform/digest, update the checked-in pin deliberately,
then use `rss replace` and verify persisted state. Follow
[image updates and rollback](docs/backup-restore.md#image-updates-and-rollback);
never assume an older image can open a newer database.

### How do I move the aggregator to another machine?

Make and privately transfer a verified instance backup plus the matching reviewed
project configuration to a compatible Apple Silicon Mac. Follow the
[clean-host recovery procedure](docs/backup-restore.md#recovery-on-this-or-a-clean-host),
using fresh ownership records and rechecking base URL, backup destination and
Tailscale access. Copying the checkout or exporting OPML alone does not move the
instance. Physical second-host and cross-architecture/Linux recovery remain
unverified; the automated recovery drill uses isolated state on one Mac.

### How do I connect a reading app or access the instance from another device?

Follow the [private HTTPS/ReadKit runbook](docs/tailscale-readkit.md). It uses the
existing Tailscale installation on all devices, approved DNS/HTTPS and narrow
TCP 443 access; port 8080 remains loopback-only. Set `RSS_TAILSCALE=1`, run `start`,
and use its private HTTPS URL in a browser.

In ReadKit choose **FreshRSS**, the **instance origin without `/api/greader.php`**,
the same FreshRSS user on both devices, and a separate API password set locally
in FreshRSS **Profile**. The web password is not the API password. Other readers
are not verified here. The server Mac's self-HTTPS diagnostic has a known limitation;
explicit `api-check --loopback` is supplemental and does not verify remote HTTPS.

### Why are articles missing or feeds no longer updating?

Confirm the Mac/VM is awake and running, finish the installer, and check `rss status`.
Run `rss fetch`; it reports that fetching ran, not that a publisher supplied new
content. FreshRSS fetches at startup and UTC minutes 00/20/40, respecting feed TTL,
HTTP caches and publisher backoff. Refreshing ReadKit only synchronizes with the
instance. Check subscription errors and [logs below](#how-do-i-view-service-logs-and-investigate-feed-fetching-errors).
Feeds may offer only excerpts; full-page scraping is not configured. Check user,
category and feed archiving policies if older articles disappear.

### How do I view service logs and investigate feed-fetching errors?

Use FreshRSS's **configuration (gear) menu → Logs** for user/feed warnings and
errors; inspect affected subscriptions in **Subscription management**. For container
startup output and the
latest cron execution output, follow the exact commands in
[service logs](docs/operations.md#service-logs-and-feed-errors). Production logs do
not record every successful fetch, and cron output is transient. Logs can contain
private feed URLs: inspect locally, redact before sharing, and never enable raw
credential tracing just to collect evidence.

### How do I safely handle private/paid feed credentials?

Treat a subscriber-specific URL as a password. Enter it only in the local
FreshRSS subscription form, never in command arguments, Git, chat, screenshots or
issues. Keep exported subscriptions, logs and backups private; permissions alone
do not encrypt them. If exposed, revoke/rotate the credential with the publisher.
Paid entitlement and private authentication are different: not every paid feed uses
the same mechanism. The planned paid-feed acceptance work is not complete; no
claim is made that a particular publisher's authentication or full content works.

## Documentation

- **Operator reference:** [CLI](docs/cli.md), [configuration](docs/configuration.md),
  [operations and logs](docs/operations.md), [backup/recovery](docs/backup-restore.md),
  [private HTTPS/ReadKit](docs/tailscale-readkit.md).
- **Development:** [setup, checks, Python adoption and verification limits](docs/development.md),
  [architecture and ADRs](docs/architecture.md), [domain glossary](GLOSSARY.md).
- **Agents:** [documentation rules and procedure index](AGENTS.md).
