# Selected-branch GitHub repository sync

This repository contains a GitHub Actions workflow that copies explicitly selected Git branches and all source tags from a source repository to a GitHub destination repository. It runs on a schedule and can also be started manually. Branch names are preserved: source `main` syncs to destination `main`. Tags are synchronized automatically and do not depend on `SYNC_BRANCHES`.

Selected branches are **fast-forward-only**: a destination branch is updated only when absent or when it is an ancestor of the corresponding source branch. All source tags are also pushed, including lightweight and annotated tags and tags pointing to commits outside the selected branches. Branches and tags are pushed together in one atomic, non-force operation. If a selected source branch is missing, a selected destination branch has diverged, or a source tag name already exists on the destination with a different tag object, the run fails without updating any refs. Identical tags are accepted. A differing annotated tag object conflicts even if it points to the same commit. Destination-only branches and tags, and unselected branches, are retained. Nothing is pruned or force-updated; removed source tags are not removed from the destination.

## Setup

1. Put this workflow repository on GitHub, with `.github/workflows/sync.yml`, `scripts/sync.py`, and the tests on its **default branch**. Enable GitHub Actions for the repository.
2. In that repository, open **Settings → Secrets and variables → Actions → New repository secret** and add the required secrets below. Add optional secrets there only if needed. Secrets belong to the workflow/control repository, not automatically to the source or destination.
3. Use HTTPS repository URLs without embedded credentials, query strings, fragments, or whitespace, and make sure GitHub-hosted runners can reach the source repository. The destination must be a GitHub repository.
4. Run the workflow manually once and check its run log before relying on the schedule.

| Secret | Required? | Value / example |
| --- | --- | --- |
| `SOURCE_REPO_URL` | Yes | Source HTTPS URL, e.g. `https://github.com/example/source.git` (no username, password, or token in the URL). |
| `DESTINATION_REPO_URL` | Yes | Destination HTTPS URL, e.g. `https://github.com/example/target.git` (no embedded credentials). |
| `DESTINATION_TOKEN` | Yes | A fine-grained personal access token with access to the target repository and **Contents: read and write** permission. If synchronized branches or tags introduce workflow-file changes, the token may also need **Workflows: write** permission. |
| `SYNC_BRANCHES` | Yes | Exact source branch names, one per line. Do not include `refs/heads/`. At least one unique, valid branch is required. All source tags sync regardless of this selection; no tag configuration or extra secret is needed. |
| `SOURCE_TOKEN` | No | Token for a private or otherwise authenticated source. Grant only the read access required by your provider (for GitHub, repository contents read access). Omit for a publicly readable source. |
| `SOURCE_USERNAME` | No | Username paired with `SOURCE_TOKEN` when the source requires a specific HTTPS username. If omitted while `SOURCE_TOKEN` is set, the workflow uses `x-access-token`. Setting this secret without `SOURCE_TOKEN` causes the run to fail. |

Create a fine-grained destination token for the destination repository; the workflow's `GITHUB_TOKEN` is not used for the push and should not be assumed to have permission to write to another repository. Keep tokens in Actions secrets, never in repository URLs or committed files. For a private source, configure the source token and any required username according to the source provider's HTTPS authentication rules.

### Public sources without authentication

For a publicly readable source repository, **do not create either `SOURCE_USERNAME` or `SOURCE_TOKEN`**. GitHub Actions resolves references to unset secrets to empty strings, which this script treats as no source authentication. Leave the workflow's existing secret references unchanged; do not use placeholder values such as `none`, `null`, or `anonymous`.

If these secrets were previously configured, remove them for unauthenticated access and check that organization or environment secrets do not supply values under the same names. Setting `SOURCE_USERNAME` alone causes validation to fail.

You must still configure `SOURCE_REPO_URL`, `DESTINATION_REPO_URL`, `SYNC_BRANCHES`, and `DESTINATION_TOKEN`. Pushing to the destination requires authentication even if that repository is public.

If you adapt the workflow to a multi-repository matrix, omit the corresponding source authentication secrets for each public source (for example, `PROJECT_A_SOURCE_USERNAME` and `PROJECT_A_SOURCE_TOKEN`); their secret-name references can remain in the matrix.

### Branch selection

Example `SYNC_BRANCHES` secret value:

```text
main
develop
release/1.x
```

## Running and configuring syncs

- **Schedule:** `.github/workflows/sync.yml` uses `17 */6 * * *` (at minute 17, every six hours). GitHub Actions cron schedules use UTC. Scheduled runs use the workflow from the default branch, can be delayed during high load, and may be disabled for repositories with extended inactivity; check GitHub's Actions status if a run does not occur as expected.
- **Manual run:** Open **Actions → Synchronize selected branches → Run workflow**. The branch selector chooses the control repository's workflow version, not the branches to synchronize. There are no custom inputs: change `SYNC_BRANCHES` to select source branches. Alternatively, with GitHub CLI, run `gh workflow run sync.yml` in the control repository.
- **Change selection:** Edit the `SYNC_BRANCHES` repository secret to a newline-separated list like the example above. Changes take effect on the next run. To change the interval, edit the `cron` value in `.github/workflows/sync.yml`; the workflow change must be on the default branch for scheduled runs.
- **Divergence:** If a run reports divergence, inspect both histories and decide which changes to keep. Integrate the desired destination changes into the source branch (or otherwise make the destination branch an ancestor of the intended source history) through your normal review process, then rerun. This workflow will not reconcile unrelated histories or overwrite destination commits.
- **Tag conflict:** If a run reports a conflicting tag, inspect the source and destination tag objects and resolve the naming conflict manually. The workflow will not overwrite or delete the destination tag; rerun after the refs are made compatible.

## Repository and branch considerations

Consider keeping this automation in a separate **control repository**. If the workflow repository is also the sync destination and the selected branches include its workflow files, a sync can replace those files and affect future automation runs. Secrets are configured in the control repository. If using the same repository for control and destination, avoid selecting branches containing the control workflow unless that replacement is intentional.

Keep custom destination-only branch names outside the `SYNC_BRANCHES` selection. A branch with a selected name is treated as the corresponding sync destination branch. Renaming or removing a source branch does not trigger destination cleanup: an old destination branch remains until you remove it separately. Destination-only tags are also retained, and deleting a source tag does not delete its destination counterpart. Branch protection rules or rulesets on the destination can reject the push; configure them to permit this token and workflow, or the sync will fail.

This synchronizes Git branches and tag refs, not repository metadata. It does not sync issues, pull requests, or GitHub Release metadata (a Git tag does not create or sync a release). Git LFS pointer files may be present in commits, but this workflow does not transfer LFS object payloads. Submodule references are Git commits; submodule repositories are not synced. The runner must have network access and suitable read credentials for the source.

## Project files and tests

- `.github/workflows/sync.yml` — scheduled and manual Actions workflow, permissions, and secret-to-environment configuration.
- `scripts/sync.py` — URL and branch validation, authenticated Git fetches, branch fast-forward and tag conflict checks, and atomic branch-and-tag push.
- `tests/test_sync.py` — local-repository integration tests for branch and tag synchronization, conflict safety, and configuration validation.

Run the test suite with Python 3:

```sh
python -m unittest discover -s tests -v
```

When changing behavior, update the tests and this README so setup, safety guarantees, and limitations continue to match the implementation.
