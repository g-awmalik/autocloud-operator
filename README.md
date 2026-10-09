# AutoCloud Operator — GitHub Integration (Phase A / Option 1)

Self-contained GitHub Action (`runner_main.py`, `action.yml`) and onboarding CLI
wizard (`cli_init.py`) for connecting a GitHub repository to the **AutoCloud
Operator** agent via MCP (`ask_cloud_operator`).

## Architecture Overview (Option 1: Thick GitHub Action + CLI Wizard)

1. **Onboarding (`cli_init.py` / `autocloud-github-init`)**:
   - Generates `.github/autocloud.yml` (project ID, agent ID, and enabled review
     scenarios) and `.github/workflows/autocloud.yml`.
   - Optionally validates local human EUC credentials (`~/.config/gcloud/application_default_credentials.json`)
     and uploads `AUTOCLOUD_OAUTH_CONFIG` to GitHub Secrets via `gh secret set`.
2. **Runtime Execution (`runner_main.py`)**:
   - Exchanges the stored OAuth 2.0 `refresh_token` against
     `https://oauth2.googleapis.com/token` for a short-lived human EUC Bearer
     token.
   - Bundles modified PR text files into `ask_cloud_operator` over HTTPS/SSE
     (`https://autopush-geminicloudassist.sandbox.googleapis.com/mcp`).
   - Parses findings, extracts remediated file blocks (`title="<path>"`), writes
     them inside the workspace with strict path-traversal guards, pushes
     `autocloud/remediate-pr-<N>`, and opens/updates a **Companion PR**
     targeting the contributor's PR `head` branch.

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
