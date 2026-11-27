# Configuration reference

Runtime settings, checked-in pins, and managed state. For procedures see [operations](operations.md), [backup and restore](backup-restore.md), and [Tailscale and ReadKit](tailscale-readkit.md). For command syntax see the [CLI reference](cli.md); for tooling settings see [development](development.md#development-configuration).

## Environment settings

The runtime reads the process environment; it does not load `.env` files. Export settings in the controlling shell before running `rss`. Keep selectors consistent for an installation. Commands in another checkout can otherwise select a different default state and runtime manifest.

| Setting | Required/default | Meaning and application |
| --- | --- | --- |
| `RSS_STATE_DIR` | Optional; `<source checkout>/.local/native` | Root of one native installation: runtime, generated configuration, instance data, job/ownership records and locks. Use a local, non-synced filesystem for SQLite, a private user-owned directory (`0700`), no state-directory symlink, and a normal non-root account. `~` expands; relative paths resolve from the process working directory. Prefer an absolute path for an intentional override, never add it to Git, and use a separate path for rehearsal. Changing the selector does not migrate, adopt, or copy data; keep legacy state separate. |
| `RSS_PORT` | Optional; `8080` | TCP port on IPv4 loopback `127.0.0.1`; integer `1024–65535`. Choose an unused port. This does not enable LAN, IPv6 or all-interface binding. Use a different port when rehearsing beside another instance; see [port changes](#changing-the-loopback-port). |
| `RSS_TAILSCALE` | Optional; `0` | `0` is loopback-only; `1` requests private Tailscale Serve HTTPS on node port `443`. Other values are invalid for access configuration. Requires initialized FreshRSS, an already connected Tailscale installation, CLI/LocalAPI access, enabled HTTPS certificates, and an appropriate incoming TCP 443 policy. Follow the [private-access runbook](tailscale-readkit.md), including before changing captured job settings. `stop` withdraws recorded owned forwarding even when the current selector is `0`; backup resumption only republishes a route that backup actually withdrew. |
| `RSS_BACKUP_DIR` | Required for `backup`, shutdown backup when data exist, and enabling daily backups; no default | Explicit absolute private directory owned by the operator, outside Git checkouts and neither containing nor contained by the controlling checkout or `RSS_STATE_DIR`. No symlink for the selected directory; use `0700`, with filesystem support for hard links for non-overwriting publication. New directories are created privately and archives use `0600`. Not required to restore from an existing archive. Change the export to choose another destination; existing archives are not moved, transferred or pruned. Disable and explicitly re-enable an owned daily job to change its captured destination. |

Example backup destination; this is not an implicit default:

```sh
export RSS_BACKUP_DIR="$HOME/FreshRSS-Backups"
```

A local copy on the same Mac is not host-loss protection. Use the [independent-copy procedure](backup-restore.md).

## Platform and dependencies

- Production runtime: Apple Silicon macOS (`darwin/arm64`, native rather than Rosetta), macOS 12 or later per the installer gate, normal user. Intel/Linux, a system service before login, and Podman as a permanent second backend are unsupported here.
- Use Git and [uv](https://docs.astral.sh/uv/getting-started/installation/) for the checkout and its local Python environment. First synchronization and first native setup can require download access. No Homebrew PHP, Caddy, separate database server, or Podman is required for the native runtime.
- Tailscale is optional for local use. Private access uses the existing `tailscale` CLI and LocalAPI; obtain owner approval before changing tailnet-wide settings. Optional LaunchAgents require `launchctl` and the normal user's GUI session; no changes to login, FileVault, or sleep policy are part of setup.

## Checked-in application configuration

| File | Purpose and change behavior |
| --- | --- |
| [`config/native.json`](../config/native.json) | Reviewed FreshRSS **1.30.4** source revision, URL and SHA-256, and `darwin/arm64` **FrankenPHP 1.14.0** executable URL and SHA-256 (with **PHP 8.6.0** and **Caddy 2.11.9**). The manifest is the pin source, not a per-shell override. Review both artifact contents and effective PHP/security behavior before changing it; normal setup does not implicitly upgrade. Use the [explicit upgrade workflow](operations.md#upgrade-the-pinned-runtime) for PHP/web changes at the same FreshRSS source and platform, retaining a compatible checkout for recovery from old archives. |
| [`config/security-probe.php`](../config/security-probe.php) | Temporary effective web PHP diagnostic. Startup checks the native SAPI/version, dedicated PHP settings and SQLite, then removes the probe before private publication. Do not install a persistent diagnostic or change PHP through host configuration. |
| [`config/config-user.custom.php`](../config/config-user.custom.php), [`config/public-feed.opml`](../config/public-feed.opml) | Non-secret initial user defaults and public sample feed list. Seeded only for initial preparation, not a mechanism for replacing existing user data. Enter private URLs in FreshRSS itself. |
| [`config/image.json`](../config/image.json) | Retained only for validation of the reviewed legacy v1 archive during the one-time import. Not a native runtime dependency or a way to select another backend. |

The controller generates loopback web configuration and an explicit PHP ini under the state directory; it does not load ambient host INI fragments. Upstream FreshRSS keeps configuration and its SQLite data under `instances/<name>/data`; CLI startup enforces SQLite, form authentication, anonymous access off, API enabled and in-app updates disabled for initialized data. FreshRSS's article retention, per-feed settings and application logs are separate from the project runtime controls. Do not hand-edit generated ownership or job records, install extensions into the pinned code tree, or add credentials to the templates.

New users start with `archiving.keep_period=false` and `archiving.keep_max=false`: the template sets no age cutoff or maximum-article purge limit. It sets `ttl_default=60` seconds, the default minimum interval between checks of an individual feed, not the **20-minute** interval at which the runtime requests feed fetching. FreshRSS can still apply per-feed overrides, caching, and backoff. For an existing or restored user, inspect or change the defaults in **Configuration → Archiving** and check category/feed overrides separately; changing the template does not rewrite existing settings.

## Captured Mac supervision configuration

Optional supervision is off until `rss supervision-install`. It captures one stable checkout, that checkout's `.venv` launcher, native manifest, the absolute state path, selected port, private-access mode, and needed executable paths, plus optional backup destination. Select intended non-secret settings in the installing shell. No interactive profile is sourced and no credentials are captured. The job is a per-user LaunchAgent; its identity is derived from this state directory and its role, separate from daily backup and legacy jobs.

Changing the current terminal does not update the captured job. Before moving the checkout, changing its pin/launcher, or changing port, access mode or backup destination, verify a backup and remove the owned job with its original checkout/selectors. Reconfigure explicitly and reinstall only after the new settings pass verification. A foreign, changed or unreadable definition is an inspection problem, not permission to replace it. See [operations](operations.md) for session behavior and safe reconfiguration.

## Daily backup configuration

Optional daily backup is off until `rss backup-enable`. It is independent of supervision; an installation can choose either, both, or neither. Set `RSS_BACKUP_DIR` and the intended runtime selectors first. The default policy is `03:00 UTC` (`--hour 3 --minute 0`), not local clock time. A user-session wakeup evaluates the latest eligible elapsed slot on job load and at 15-minute intervals, with at most three attempts per slot and no replay of older slots. Exact wall-clock execution is not promised; a sleeping, logged-out or otherwise unavailable Mac can miss the window. The status check distinguishes registration from actual recent backup protection. Change the policy or capture through `backup-disable` and a reviewed `backup-enable`, not by editing a plist; see the [backup schedule procedure](backup-restore.md#opt-in-daily-mac-backups).

## Managed local records

Under `RSS_STATE_DIR`, `runtime/` and `native-owner.json` identify the installed native code; `instances/` holds complete live or restored data; `selected-instance.json` records explicit selection. Initial default provisioning is recorded outside the instance tree. `service/` and service receipts describe generated configuration and probe results; per-instance writer generation and shutdown archive evidence identify the backup required by upgrade. `command.lock`, `controller.lock`, `writers.lock`, `fetch.lock`, `operator-intent.json` and `maintenance.json` participate in serialization and safe recovery. Forwarding, job registrations and recent attempt/archive results have separate ownership/evidence records. These files are durable control state, not a disposable cache. Keep the directory and older independent backups intact when something is interrupted; consult the [architecture](architecture.md) and use the supported CLI to reconcile.

## Changing the loopback port

A port change affects the owned listener and, in private mode, the Serve backend target; it does not change the fixed HTTPS port. Use the existing selectors to take a verified backup, stop FreshRSS, and remove/disable any captured jobs before changing the exported value. Use an unused loopback port, keep the same state selector, and run the normal startup/verification checks. For private mode follow the [private-access procedure](tailscale-readkit.md) and verify trusted client access before re-enabling optional jobs. Do not kill an unrelated listener or repurpose its route. See [operations](operations.md) for the lifecycle procedure.

## Secrets and local handling

Enter the FreshRSS web credentials locally; enter a **separate per-user API password** in the FreshRSS profile for client access. A private feed URL is a credential. No secret environment setting is required. Never place passwords, private URLs, tokens, real feed data or private diagnostics in Git, shell arguments, examples, issues or shared logs/screenshots. Private data, app logs, archives and even diagnostic paths/hostnames require care before sharing. `.local/` is ignored, but ignore rules do not protect paths selected elsewhere or files already tracked. Permissions limit local access but are not encryption or a sandbox.
