# Issue #1 — local verification

Date: 2026-11-14. Host: Apple Silicon macOS (Darwin 27.2.0, arm64).
Executed with Python 3.15 and Homebrew Podman CLI 6.2.0; the dedicated rootless
VM reported Fedora Podman Machine OS 44 / arm64. This is development verification,
not production security certification or a claim of PR review.

## Identity and security

- Official image: `docker.io/freshrss/freshrss`, Debian FreshRSS **1.30.4**.
- OCI index: `sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df`.
- Platform: **linux/arm64**; manifest
  `sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797`.
- Resolved image ID: `60d717ee1433261fce6ed75c0ff8f8bb5e110d2bed395dbcf6cfde5fc934dadc`.
- FreshRSS source revision: `08f97bd497ace733c151ea1c709d85cd5b65cc53`.
- Actual **HTTP-handled Apache PHP**: `PHP_SAPI=apache2handler`, **8.4.27**;
  effective `session.upload_progress.enabled` returned the empty string (disabled).
  Loaded `/etc/php/8.4/apache2/php.ini`, with scanned INIs under
  `/etc/php/8.4/apache2/conf.d/`. Actual CLI PHP separately reported **8.4.27**.
- Both unpublished preflight and the actual application were checked. Actual
  application verification used internal `127.0.0.1:8088` while published port 80
  was not listening. The temporary diagnostic was removed before enabling port 80.
  The official entrypoint and normal Apache/cron startup command were preserved.
- A deliberate Apache-only `conf.d` override enabling upload progress was rejected
  by `start`, despite the clean image preflight passing. HTTP serving remained
  unavailable, no diagnostic remained, and removing the test-only override restored
  successful startup. This checks effective web configuration, not CLI INI text.

The [October 21 PHP advisory](https://www.php.net/archive/2026.php#2026-10-21-5)
requires fixed releases including 8.4.27. The additional
[October 23 advisory](https://www.php.net/archive/2026.php#2026-10-23-1)
requires upload progress disabled; **8.4.27 alone is not claimed to fix it**.
The current [downloads](https://www.php.net/downloads.php) and
[support policy](https://www.php.net/supported-versions.php) were consulted before
selection. The October 26 Windows rebuild advisory does not establish Linux
package safety. No claim of an exhaustive vulnerability/dependency scan is made.

## Host footprint and ownership

Homebrew package versions and dependencies were inventoried before installation.
`brew install --dry-run podman` reported six additions and no existing upgrades.
The bootstrap's before/after comparison found no existing formula version changes.
Only these additions were recorded for later conservative cleanup:

| Formula | Installed version |
| --- | --- |
| podman | 6.2.0 |
| libkrunfw | 5.6.4 |
| libepoxy | 1.5.10 |
| molten-vk | 1.4.2 |
| libkrun | 1.19.8 |
| krunkit | 1.3.2 |

No pre-existing Podman machines/connections were present. The project created
`rss-aggregator` with **2 CPUs, 2048 MiB guest RAM, 20 GiB virtual disk**, rootless.
Podman reported that its privileged host helper was not installed. No host PHP,
database server, Desktop, login item, launch agent, service registration or
application restart policy was installed. Project container operations explicitly
selected its rootless connection. Named volume storage was in the guest's native
container storage, not a host-shared database directory.

## Commands and results

| Command/check | Actual result |
| --- | --- |
| `./scripts/rss setup` | Passed real CLI installation and VM initialization; repeated setup preserved existing setup without starting/stopping the VM. |
| `./scripts/rss start` / `status` / `verify` | Passed image, actual Apache PHP, loopback HTTP, installed `*/20` crontab and live cron-daemon checks. Repeated start restarted/reverified only the project application. |
| `./scripts/rss replace` | Passed project container replacement, preserving the named volume and installer state. |
| `./scripts/rss stop` followed by another `stop`, then `start` | Passed in live integration: project VM stopped, test listener unreachable; repeated stop reported already stopped; restart restored stored account, subscription, articles and read/star state in the independent test volume. |
| `python3 -m unittest discover -s tests -v` | 20 command-boundary tests passed. Covers simulated-clean/repeated bootstrap, upgrade/reinstall refusal, ownership conflicts, occupied/invalid ports, running-machine conflict, command lock, stop failure propagation, unrelated writers, transitional VM state and TCP TIME_WAIT restart handling. |
| `RSS_LIVE=1 python3 -m unittest discover -s tests -p 'live_*.py' -v` | Passed all 5 tests: effective Apache negative security gate, observer regression, full HTTP/persistence/lifecycle scenario, changed-feed cron-command regression, and installed-instance base-URL/loopback-port migration. |
| `RSS_LIVE=1 RSS_OBSERVE_CRON=1 python3 -m unittest discover -s tests -p 'live_*.py' -v` | Passed all 5 tests in 1702.334 seconds. Two natural executions at **2026-11-14T18:20:01+00:00** and **2026-11-14T18:40:01+00:00** collected newly published controlled articles. The schedule was not shortened; earlier observer/publisher fixture failures are explained below. |
| `python3 -m py_compile scripts/rss tests/*.py` | Passed syntax compilation; not a static typecheck. |
| `ruff check --isolated --target-version py310 scripts/rss tests` | Passed linting. |
| `git diff --check` | Passed. |

The real HTTP fixture completed the SQLite installer, entered a random in-memory
test password, required form authentication, fetched the public FreshRSS releases
feed, checked both purge-limit checkboxes were off, deliberately set read/star
state through HTTP, replaced the test container, reauthenticated, and checked that
state again after project VM stop/start. A controlled test feed demonstrated
immediate article collection. Assertions use HTTP-visible state, not database
queries. Unique labelled test volumes are separate from the application volume;
fixture cleanup deletes only those test resources. Final resource inspection found
only the project application container and its data volume; disposable fixtures
were removed. Final `status → stop → stop → status` passed, reported the VM stopped,
and a separate TCP connection check confirmed the Mac listener was unavailable.
The authoritative issue was re-fetched before commit; its approved requirements
were unchanged and no blockers were present.

## Resolved verification failures

- The first timed cron observation read `data/users/_/log.txt`. Production's
  default `warning` threshold filters the notice-level actualization messages,
  so that observer was blind even to a completed manual fetch. A ~4-second real
  regression test reproduced this independently of cron. Reading the official
  cron stdout file `/tmp/FreshRSS.log` fixed the regression without changing the
  application's logging policy or shortening its schedule. The timed scenario
  excludes earlier/manual timestamps and also requires new HTTP-visible articles.
- The second timed scenario detected a job but did not collect a new controlled
  article. A six-second exact-cron-command regression reproduced this even with
  per-feed eligibility forced, and HTTP inspection found the fixture publisher
  advertising `Cache-Control: max-age=2592000` (30 days). FreshRSS correctly cached
  it, bounded by its 24-hour cache maximum. The fixture now returns
  `no-cache, max-age=0`, asserted through HTTP. Its fast regression alone reduces
  TTL/minimum cache duration; the natural scenario retains production settings.
  A separate real check with **unchanged production TTL/cache policy**, publishing
  a second article then waiting 65 seconds before the exact cron command, passed.
  No production cache policy was changed to make the tests pass.
- Combined lifecycle verification exposed false “port occupied” errors immediately
  after shutdown. A deterministic command-boundary test reproduced a closed TCP
  connection in TIME_WAIT with no live listener. The port availability probe now
  sets `SO_REUSEADDR` (not `SO_REUSEPORT`); recently closed connections no longer
  block restart, while the active-listener refusal test still passes. The complete
  live suite passed after this fix.

## Limitations and deliberately unrun checks

- The real application volume is intentionally left for the user's own local
  installer/credentials. No real password, API credential or private feed URL was
  supplied to this session or committed. Browser behavior was exercised by HTTP
  requests/challenge-response authentication, **not** visual browser automation or
  a human clicking through the UI.
- Existing unrelated-machine/default preservation and failure paths were tested
  with controlled external executables; no unrelated real VM was stopped or
  modified to manufacture a conflict.
- Only the observed arm64 image and host/Python combination were run. Alternate
  architectures, other macOS/Podman versions, and a Python 3.10 interpreter were
  not separately runtime-tested. No repository static typechecker was provided.
- Tailscale, ReadKit, private feeds, Linux deployment, portable backup/restore and
  destructive cleanup were not implemented or tested. Normal stop deliberately
  makes no external backup until issue #2's infrastructure exists. VM deletion
  would destroy this slice's stored data.
- Cron's process/job checks do not alone prove publisher updates; natural recurring
  collection is recorded separately above. Upstream TTL, caching and backoff still
  apply. No assertion that every execution downloads a new public article is made.
