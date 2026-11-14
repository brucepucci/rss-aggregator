# Issue tracker: GitHub

Issues and specs live in this repository's GitHub Issues.
Use the `gh` CLI; infer the repository from the Git remote.

Before publishing, verify the target with:
`gh repo view --json nameWithOwner`.

Read issue bodies and comments before acting.
Publish approved tickets as individual issues.

Represent blockers using GitHub's native issue dependencies.
If unavailable, list blocking issue references in the issue body.
Work only on tickets whose blockers are all closed.

When deriving tickets from an existing issue, link them as sub-issues.
Apply labels according to `triage-labels.md`.

PRs as a request surface: no.
