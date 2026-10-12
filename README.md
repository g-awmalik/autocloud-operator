# AutoCloud Operator — GitHub Integration (Phase A / Option 1)

Self-contained GitHub Action (`runner_main.py`, `action.yml`) and onboarding CLI
wizard (`cli_init.py`) for connecting a GitHub repository to the **AutoCloud
Operator** agent via MCP (`ask_cloud_operator`).

## Architecture Overview (Option 1: Thick GitHub Action + CLI Wizard)

1. **Onboarding (`cli_init.py` / `autocloud-github-init`)**:
   - **Zero-Clone Remote Mode (`--github_repo=owner/repo`)** or **Interactive
     Wizard (`--interactive=true`)**:
     1. Validates local Google ADC credentials (`~/.config/gcloud/application_default_credentials.json`)
        or automatically invokes `gcloud auth application-default login`.
     2. Uploads `AUTOCLOUD_OAUTH_CONFIG` to the repository's GitHub Actions
        Secrets (`gh secret set`).
     3. Automatically enables *"Allow GitHub Actions to create and approve pull
        requests"* on the target repository (`PUT /repos/{owner}/{repo}/actions/permissions/workflow`).
     4. Commits `.github/autocloud.yml` and `.github/workflows/autocloud.yml`
        directly to the remote repository via the GitHub Contents API.
   - **Local Directory Mode (`--repo_dir=/path/to/repo`)**:
     - Writes `.github/autocloud.yml` and `.github/workflows/autocloud.yml` to a
       local checkout.
   - Note: **Scenario 2 (Issue & Bug Implementation)** is currently marked as
     **Work in Progress (not available yet)**.
2. **Runtime Execution (`runner_main.py`)**:
   - Exchanges the stored OAuth 2.0 `refresh_token` against
     `https://oauth2.googleapis.com/token` for a short-lived human EUC Bearer
     token.
   - Bundles modified PR text files into `ask_cloud_operator` over HTTPS/SSE
     (`https://geminicloudassist.googleapis.com/mcp`).
   - Delivers review feedback according to `scenarios.code_changes.pr_mode`:
     - **`inline_comments` (Default)**: Posts a GitHub Pull Request Review on
       the existing PR (`POST /repos/{owner}/{repo}/pulls/{pr}/reviews`) with
       line-anchored inline comments and one-click ````suggestion` blocks
       without opening a separate PR.
     - **`companion_pr`**: Writes remediated files inside the workspace with
       path-traversal guards, pushes `autocloud/remediate-pr-<N>`, and opens/
       updates a Companion PR targeting the contributor's PR `head` branch.
     - **`comment_only`**: Posts a top-level summary comment on the existing PR.

## One-Command Remote Repository Onboarding

Onboard any GitHub repository in a single command (no local clone required):

```bash
/google/bin/releases/arca9-local-blaze-cli/blaze-for-agents run \
  //experimental/users/awmalik/autocloud_github:cli_init -- \
  --github_repo=g-awmalik/brownfield-poc \
  --project_id=g4g-operate-demo \
  --agent_id=operator-awmalik \
  --action_ref=g-awmalik/autocloud-operator@v1
```

Or run with no flags to launch the interactive terminal wizard:

```bash
/google/bin/releases/arca9-local-blaze-cli/blaze-for-agents run \
  //experimental/users/awmalik/autocloud_github:cli_init
```

## Tier 1: Running Unit Tests in Google3

```bash
/google/bin/releases/arca9-local-blaze-cli/blaze-for-agents test \
  //experimental/users/awmalik/autocloud_github:all
```

## Tier 2: Local Cloudtop Smoke Testing against AutoCloud MCP

Test the onboarding CLI wizard locally on a scratch directory (`--agent_id` is
optional and defaults to your personal agent when omitted):

```bash
/google/bin/releases/arca9-local-blaze-cli/blaze-for-agents run \
  //experimental/users/awmalik/autocloud_github:cli_init -- \
  --repo_dir=/tmp/autocloud_sample_repo \
  --project_id=g4g-operate-demo \
  --agent_id=operator-awmalik
```

Run a dry-run PR review against the AutoCloud MCP endpoint (using local
workstation ADC without calling GitHub's REST API):

```bash
/google/bin/releases/arca9-local-blaze-cli/blaze-for-agents run \
  //experimental/users/awmalik/autocloud_github:runner_main -- \
  --workspace_dir=/tmp/autocloud_sample_repo \
  --use_local_adc=true \
  --dry_run_github=true \
  --changed_files=main.tf
```

## Syncing Changes to the External GitHub Action Repository

Use `sync_action_repo.sh` to export this package (rewriting internal `google3.*`
imports, committing to `main`, and updating the `v1` tag) to your hosted GitHub
Action repository:

```bash
/google/src/cloud/awmalik/gca-autocloud-skills/google3/experimental/users/awmalik/autocloud_github/sync_action_repo.sh \
  https://github.com/g-awmalik/autocloud-operator
```

