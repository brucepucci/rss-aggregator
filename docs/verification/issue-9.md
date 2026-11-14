# #9 local adoption verification

Date: 2026-11-14. Host: Apple Silicon macOS. Baseline runtime: uv 0.13.8,
managed CPython 3.13.18. Locked versions include Click 8.5.1, pytest 9.2.0,
Ruff 0.14.14 and pre-commit 4.7.0. FreshRSS remains in its original rootless
Podman VM with the unchanged official image pin; no host PHP/database or Homebrew
package install/upgrade was performed for this adoption.

The original sections below preserve the prepublication local evidence and its
then-pending work. See [PR #11 review follow-up](#pr-11-review-follow-up) for the
local regression verification and the
[publication/CI checkpoint](#publication-and-ci-checkpoint-2026-11-14) for
subsequent revision-specific evidence. The later
[controlled-failure CI checkpoint](#controlled-failure-ci-checkpoint-2026-11-14)
supplies the negative-CI evidence that earlier checkpoints identify as outstanding.

## Proven locally

| Command/check | Actual result |
| --- | --- |
| Before rewrite, `python3 -m unittest discover -s tests -v` | Original 20 cases passed during standards incorporation. |
| `uv sync --locked` | Created the managed Python/project environment and committed dependency resolution; no global pip installation. |
| `uv run --locked --offline rss-dev check` | All **45 default pytest cases passed**, five live cases explicitly deselected; Ruff lint and format checks passed. |
| Fresh `UV_PROJECT_ENVIRONMENT=<temporary>/venv uv sync --locked --offline`, then the same `rss-dev check` | New isolated environment installed 18 locked packages from already-populated caches; the complete 45-case baseline and both Ruff checks passed. Temporary environment removed. |
| `uv run --locked pytest -m live -q --tb=short`, with explicit original `RSS_STATE_DIR` | All **5 real Mac/HTTP tests passed** against the rewritten runtime; latest run 32.43 seconds. |
| Explicit live security case with a new empty `RSS_STATE_DIR` | Expected nonzero error with an actionable missing-project/ownership-state prerequisite message; no skip or misleading success. |
| `rss status/start/stop`, repeated stop, using the original ownership directory | JSON results/diagnostics verified; final VM stopped, second stop reported `already_stopped: true`. Separate TCP check confirmed loopback listener unavailable. |
| Direct `scripts/rss status` from `/tmp`, with temporary state | Locked-uv compatibility launcher resolved its checkout, emitted indented JSON and did not adopt native resources. |
| Production annotation/docstring AST audit | Every production callable had parameter/return annotations and a docstring. This is a structural check, not a typechecker or a substitute for documentation review. |
| Immutable workflow action references | Both pinned commits resolved in their respective upstream repositories. Source configuration reviewed; not an actual workflow execution. |

The baseline includes 25 process-contract cases covering the original guards,
16 developer-gate cases, and four JSON/callable-interface cases. The rewritten
runtime's meaningful differences are intentional: thin Click commands, typed
callable operations, structured output, instance-local configuration and locking
for Python callers. A stopped VM's application state is `unknown` rather than a
false claim of container absence.

## Actual push/gate evidence

`tests/test_development.py` creates independent disposable Git repositories and
**local bare remotes**, not GitHub branches. Actual test operations demonstrated:

- Setup with no remote returns inactive and writes no hook; adding a remote and
  rerunning setup installs the pre-push hook. A real local push passes the baseline.
- A failing pytest assertion, unused-import lint violation, and formatting
  violation each fail the developer gate **and reject an actual push**. The bare
  remote has no accepted ref after rejection.
- Before/after snapshots prove tracked/working source content and Git status are
  unchanged by check-only gates. The deliberate failures exist only inside the
  disposable fixtures, not the implementation branch.
- Foreign, legacy, symlinked, dangling, custom-path and symlinked-directory hook
  arrangements are refused without changes. A recognized pre-commit hook with
  developer extensions is preserved byte-for-byte on setup rerun.
- Explicit safe fixes and explicit formatting are separate. An unsafe-fix-enabled
  fixture still cannot make `rss-dev fix` apply unsafe edits.
- Live-marker cases are excluded from baseline, but explicitly selected failures
  fail rather than masquerading as complete coverage.

No hooks were installed in the real repository's shared Git directory. Linked
worktree hooks affect other branches, so activation remains an intentional step.

## Real runtime evidence and scope

The five migrated live tests exercised the effective Apache upload-progress
failure gate and cleanup/recovery, HTTP SQLite installation/authentication, public
feed articles, disabled purge settings, deliberate read/star state, container
replacement, VM stop/start persistence, controlled changed-feed collection,
actualization observation, and installed-instance port/base-URL migration.

The actual rewritten runtime verified Apache/CLI PHP **8.4.27**, disabled effective
web upload progress, the same official FreshRSS 1.30.4 image/platform identities,
the installed UTC `*/20` job and live daemon. The original complete data volume
and ownership record were used by explicit path, never copied or recreated.
Disposable test containers/volumes were cleaned up; the application/VM were
stopped normally after verification, without the deferred external backup.

The long natural-cron option was retained as `pytest -m live --observe-cron -s`
but **not rerun for #9**. #1's historical observations at 18:20:01 and 18:40:01 UTC
are not claimed as new evidence for this rewrite. No scheduling, cache or TTL
policy changed; command/real changed-feed and effective scheduler checks were run.

## Limitations and pending acceptance

- **Actual GitHub Actions passing/failing runs remain unperformed.** Workflow
  publication/PR execution requires separate authorization. Matching local gate
  definitions and local push tests do not prove CI execution or branch protection.
- **#9 has not been merged back into #1, and neither implementation has merged
  into main.** Only the explicitly authorized main-to-#1 standards incorporation
  was merged. Later nested integration/publication/review requires authorization;
  the final main PR must resolve both #1 and #9 through one squash implementation.
- Only Python 3.13.18 on this Mac was exercised. The declared 3.10 minimum and
  GitHub-hosted Linux baseline have not separately run here. No mandatory static
  typechecker was added or claimed.
- Real native Linux rootless-container integration, a broader controlled feed/error
  matrix, interrupted cleanup/failure artifacts and Mac-provider automation remain
  #7's distinct responsibilities. Its entrypoint/workflow should extend this
  baseline, not replace or duplicate it. Current live tests still depend on public
  GitHub content and interrupt the real project session.
- Visual browser interaction, real ReadKit/tailnet access, private feeds, portable
  backups/restores and destructive cleanup remain unimplemented/unverified. Test
  users are synthetic; real application credentials remain for local user entry.
- Formatting failures during migration, the new stopped-status JSON regression and
  the modified-generated-hook preservation regression were observed red and then
  fixed; the final baseline is green. Earlier #1 fixture failures remain recorded
  as historical evidence rather than hidden or counted as adoption successes.

The authoritative #9 body/comments and #8 policy were re-fetched before commit.
#9 remained unchanged with only the closed #8 blocker. Compared with historical
#1, its only requirement additions were the already approved standards/integration
sequence and combined-squash gate supplied in this request; operational requirements
were unchanged. Those integration gates remain pending, not silently waived.

This is local implementation/verification evidence, not PR review, approval, or
permission to publish, merge, delete data, or change branch protection.

## PR #11 review follow-up

**Historical prepublication checkpoint: 2026-11-14.** The fixes described below
were later published as `4a6116621efd2c91e3dc11575fcb63028779f74e`; see the next
section for publication/CI evidence. Statements about local/unpublished status
in this section apply only to that prepublication checkpoint.

Draft [PR #11](https://github.com/brucepucci/rss-aggregator/pull/11) was published
with head `eff668189c1ffe3ef740e046ce697baf7e8b267e`, targeting
`feat/issue-1-local-freshrss`, not main. The Actions API confirms that
[Python baseline run 43639445160](https://github.com/brucepucci/rss-aggregator/actions/runs/43639445160)
succeeded on that exact head. This supersedes the original pending-publication
and unperformed-successful-CI statements above; it does not establish CI results
for the subsequent local fixes.

The review's two correctness findings were reproduced with disposable fixtures:
setup reported a generated hook without executable permission as active, and
inherited `PYTEST_ADDOPTS` selected only a passing subset or a synthetic live
case. Four added regression cases were observed failing before the fixes and
passing afterward. The fixes strip inherited `PYTEST_*` variables from developer
tool subprocesses, explicitly select `not live` in the baseline, and refuse an
existing nonexecutable hook without changing its contents or mode.

`uv run --locked --offline rss-dev check` passed **49 default tests**, with five
live cases deselected, plus Ruff lint and format checks. The new tests demonstrate
that subset/live-selection options and an inherited plugin override cannot
change the expected default-test failure, and actual pushes to disposable local
bare remotes are rejected. A synthetic live-case sentinel remains absent; source
snapshots remain unchanged by the gate. The hook regression checks inactive
refusal, manual-resolution guidance and unchanged hook contents/mode.

README CLI coverage was expanded and checked against implementation/help, and
AGENTS now explicitly requires complete, accurate CLI documentation. Historical
live evidence above is not claimed as a new run: no live tests, real hook
activation, runtime changes, GitHub writes or integration/merge occurred during
this follow-up. **Controlled-failing GitHub pytest/lint/format evidence remains
outstanding** and requires separately authorized publication; local failures do
not substitute for it. These local fixes have not yet been published or tested
on GitHub. Python 3.10 and the remaining platform/device checks were not rerun.

## Publication and CI checkpoint (2026-11-14)

The review fixes were published to draft
[PR #11](https://github.com/brucepucci/rss-aggregator/pull/11) as
[`4a6116621efd2c91e3dc11575fcb63028779f74e`](https://github.com/brucepucci/rss-aggregator/commit/4a6116621efd2c91e3dc11575fcb63028779f74e),
tree `b68a21b87fd0be3360b534c0ca3f21a8bb1382c1`, still targeting
`feat/issue-1-local-freshrss`, not main.

The Actions API was queried using
`gh run view 43641847554 --repo brucepucci/rss-aggregator --json headSha,conclusion,status,url,createdAt,updatedAt`.
It confirmed that
[Python baseline run 43641847554](https://github.com/brucepucci/rss-aggregator/actions/runs/43641847554)
completed successfully on that exact revision on 2026-11-14. The job's setup,
`uv sync --locked` and `uv run --locked rss-dev check` steps also reported
success. This supplements the earlier successful `eff6681` run; neither run
establishes CI results for subsequent commits.

**Controlled-failing GitHub pytest/lint/format evidence remained outstanding at
this checkpoint.** The authorized draft exception remained in effect; no complete
acceptance, integration, readiness change or merge is implied. Broader live,
platform and device limitations above remain unchanged. Subsequent publication
and verification results belong in the PR/evidence record with their exact
revision, rather than updating moving phrases such as “current local fixes.”

## Controlled-failure CI checkpoint (2026-11-14)

The maintainer explicitly authorized disposable GitHub CI failure experiments,
restoration, cleanup and publication of the evidence on PR #11. The experiments
used [draft PR #12](https://github.com/brucepucci/rss-aggregator/pull/12), targeting
`feat/issue-9-python-adoption`, from temporary branch
`chore/issue-9-ci-failure-evidence`. Its starting head was PR #11 revision
`67cf72beffc73b7f44aea0374463e26034da1692`, tree
`ce54f4345d958cbc6c7cc60db66882993e5615a6`.

Each intentionally failing candidate was an explicitly authorized draft exception,
not accepted implementation. Only `tests/test_ci_failure_probe.py` differed from
the starting tree: a failing assertion, then a passing test with an unused import,
then a passing test with noncanonical formatting. Blob identities confirmed the
workflow, gate implementation, project configuration and lockfile stayed unchanged.
The same `uv sync --locked` and `uv run --locked rss-dev check` workflow ran for
each PR update; no alternate workflow or failure-suppression setting was added.

| Case | Exact branch head | Actions run | Workflow result | Gate exit codes: pytest / lint / format |
| --- | --- | --- | --- | --- |
| Pytest assertion | [`1977d38d3371cfd8f0f49d81a3e890a605a13727`](https://github.com/brucepucci/rss-aggregator/commit/1977d38d3371cfd8f0f49d81a3e890a605a13727) | [43644086364](https://github.com/brucepucci/rss-aggregator/actions/runs/43644086364) | Failure | `1 / 0 / 0` |
| Ruff unused import (`F401`) | [`9d4f425c19de20e145ca7cedccfa61b4b1369c1b`](https://github.com/brucepucci/rss-aggregator/commit/9d4f425c19de20e145ca7cedccfa61b4b1369c1b) | [43644229824](https://github.com/brucepucci/rss-aggregator/actions/runs/43644229824) | Failure | `0 / 1 / 0` |
| Ruff format check | [`810419960db510ce0b0fb94918a2f4f447aea4bc`](https://github.com/brucepucci/rss-aggregator/commit/810419960db510ce0b0fb94918a2f4f447aea4bc) | [43644329319](https://github.com/brucepucci/rss-aggregator/actions/runs/43644329319) | Failure | `0 / 0 / 1` |
| Probe removed; original tree restored | [`97b62ae2e28e3e6632e8413bb32e0053b4aa9a9c`](https://github.com/brucepucci/rss-aggregator/commit/97b62ae2e28e3e6632e8413bb32e0053b4aa9a9c) | [43644411740](https://github.com/brucepucci/rss-aggregator/actions/runs/43644411740) | Success | `0 / 0 / 0` |

All four GitHub-hosted Linux runs completed with successful dependency setup.
Run metadata was checked against each exact branch head, and the gate's JSON
summary was parsed from `gh run view <run-id> --log`; job/step conclusions and the
actual pytest assertion, `F401` diagnostic or `Would reformat` message were also
verified. Thus the negative results are intended check failures, not environment
or installation failures. The pytest probe run reported one intentional failure
and 49 passing default tests. The lint/format runs each passed 50 tests, including
the probe. Five live tests remained deselected in every run.

Before each publication, `uv run --locked --offline rss-dev check` in the
disposable clone produced the same check-exit pattern, without changing its
source. Removing the probe restored exactly tree
`ce54f4345d958cbc6c7cc60db66882993e5615a6`; local and GitHub baselines then passed
49 default tests plus Ruff lint/format. No deliberately failing file was added to
PR #11, its base or main, and no experimental commit was merged into them.

PR #12 was closed **unmerged**, and `git ls-remote` confirmed its remote branch
was absent after deletion. The intentionally failing commits remain historical
evidence through that closed PR, not changes in the accepted branch. PR #11's
head remained `67cf72b` throughout the experiment; only documentation/evidence
updates were prepared afterward.

This supplies #9's actual successful/controlled-failing CI evidence requirement
and supersedes the historical outstanding-negative-CI statements above. It does
not demonstrate branch protection or the broader #7 live/platform integration
requirements. No live Podman/HTTP, natural-cron, Python 3.10, device/tailnet,
backup or destructive-cleanup tests were run. Review acceptance, readiness,
nested integration, combined verification and the final squash/merge remain
separately authorized actions.
