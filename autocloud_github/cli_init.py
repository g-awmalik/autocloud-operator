"""Onboarding CLI wizard (`autocloud-github-init`) for Option 1 setup.

Generates `.github/autocloud.yml` and `.github/workflows/autocloud.yml` in a
target GitHub repository and optionally configures `AUTOCLOUD_OAUTH_CONFIG`
via the GitHub CLI (`gh secret set`) using local human EUC OAuth credentials.
"""

from collections.abc import Sequence
import json
import os
import subprocess

from absl import app
from absl import flags
from absl import logging

from autocloud_github import auth
from autocloud_github import config

_REPO_DIR = flags.DEFINE_string(
    'repo_dir',
    '',
    'Target GitHub repository root directory (defaults to current directory).',
)
_PROJECT_ID = flags.DEFINE_string(
    'project_id',
    '',
    'Target Google Cloud project ID (e.g. g4g-operate-demo).',
)
_AGENT_ID = flags.DEFINE_string(
    'agent_id',
    '',
    'Optional custom AutoCloud Operator agent ID (defaults to personal agent).',
)
_ACTION_REF = flags.DEFINE_string(
    'action_ref',
    'google-cloud/autocloud-operator-action@v1',
    'GitHub Action reference used in .github/workflows/autocloud.yml.',
)
_ADC_PATH = flags.DEFINE_string(
    'adc_path',
    os.path.expanduser(
        '~/.config/gcloud/application_default_credentials.json'
    ),
    'Path to local Google ADC JSON file containing a human refresh_token.',
)
_SET_GITHUB_SECRET = flags.DEFINE_bool(
    'set_github_secret',
    False,
    'If True, uploads AUTOCLOUD_OAUTH_CONFIG to GitHub via `gh secret set`.',
)


def render_autocloud_config_yaml(
    *, project_id: str, agent_id: str = ''
) -> str:
  """Renders and validates `.github/autocloud.yml` content.

  Args:
    project_id: Target GCP project ID.
    agent_id: Optional custom AutoCloud Operator agent ID. When empty, the
      caller's personal operator agent is used.

  Returns:
    Validated YAML configuration string.
  """
  normalized_agent = config.normalize_agent_id(agent_id)
  if normalized_agent:
    agent_line = f'agent_id: "{normalized_agent}"\n\n'
  else:
    agent_line = (
        '# Optional: custom agent ID (defaults to caller\'s personal agent)\n'
        '# agent_id: ""\n\n'
    )

  yaml_str = (
      'version: 1\n'
      f'project_id: "{project_id}"\n'
      f'{agent_line}'
      'scenarios:\n'
      '  # Scenario 1: PR Code & IaC Review (Milestone 1 MVP)\n'
      '  code_changes:\n'
      '    enabled: true\n'
      '    triggers:\n'
      '      - pull_request.opened\n'
      '      - pull_request.synchronize\n'
      '      - issue_comment.created\n'
      '    trigger_command: "/autocloud review"\n'
      '    pr_mode: "companion_pr"\n\n'
      '  # Scenario 2: Issue & Bug Implementation (Milestone 2 / Post-MVP)\n'
      '  issue_implementation:\n'
      '    enabled: false\n'
      '    triggers:\n'
      '      - issues.labeled\n'
      '      - issue_comment.created\n'
      '    trigger_labels:\n'
      '      - "autocloud-implement"\n'
      '    trigger_command: "/autocloud implement"\n'
      '    base_branch: "main"\n'
  )
  config.parse_config_yaml(yaml_str)
  return yaml_str


def render_github_workflow_yaml(
    *, action_ref: str = 'google-cloud/autocloud-operator-action@v1'
) -> str:
  """Renders `.github/workflows/autocloud.yml` content."""
  return (
      'name: AutoCloud Operator\n\n'
      'on:\n'
      '  pull_request:\n'
      '    types: [opened, synchronize, reopened]\n'
      '  issue_comment:\n'
      '    types: [created]\n\n'
      'concurrency:\n'
      '  group: >-\n'
      '    autocloud-${{ github.event.pull_request.number ||\n'
      '    github.event.issue.number || github.ref }}\n'
      '  cancel-in-progress: true\n\n'
      'permissions:\n'
      '  contents: write\n'
      '  pull-requests: write\n'
      '  issues: write\n\n'
      'jobs:\n'
      '  autocloud:\n'
      '    runs-on: ubuntu-latest\n'
      '    if: >-\n'
      "      github.event_name == 'pull_request' ||\n"
      "      (github.event_name == 'issue_comment' &&\n"
      "       contains(github.event.comment.body, '/autocloud'))\n"
      '    steps:\n'
      '      - name: Checkout Repository\n'
      '        uses: actions/checkout@v4\n'
      '        with:\n'
      '          fetch-depth: 0\n\n'
      '      - name: Run AutoCloud Operator\n'
      f'        uses: {action_ref}\n'
      '        env:\n'
      '          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n'
      '          AUTOCLOUD_OAUTH_CONFIG: '
      '${{ secrets.AUTOCLOUD_OAUTH_CONFIG }}\n'
  )


def initialize_repository(
    *,
    repo_dir: str,
    project_id: str,
    agent_id: str = '',
    action_ref: str = 'google-cloud/autocloud-operator-action@v1',
) -> tuple[str, str]:
  """Writes `.github/autocloud.yml` and `.github/workflows/autocloud.yml`.

  Args:
    repo_dir: Target GitHub repository root directory.
    project_id: Target GCP project ID.
    agent_id: Optional target AutoCloud Operator agent ID.
    action_ref: GitHub Action reference string.

  Returns:
    Tuple of `(config_file_path, workflow_file_path)`.
  """
  repo_real = os.path.realpath(repo_dir)
  if not os.path.isdir(repo_real):
    raise ValueError(f'Repository directory does not exist: {repo_dir!r}')

  config_yaml = render_autocloud_config_yaml(
      project_id=project_id, agent_id=agent_id
  )
  workflow_yaml = render_github_workflow_yaml(action_ref=action_ref)

  github_dir = os.path.join(repo_real, '.github')
  workflows_dir = os.path.join(github_dir, 'workflows')
  os.makedirs(workflows_dir, exist_ok=True)

  config_path = os.path.join(github_dir, 'autocloud.yml')
  workflow_path = os.path.join(workflows_dir, 'autocloud.yml')

  with open(config_path, 'w', encoding='utf-8') as cfg_file:
    cfg_file.write(config_yaml)
  with open(workflow_path, 'w', encoding='utf-8') as wf_file:
    wf_file.write(workflow_yaml)

  return config_path, workflow_path


def upload_oauth_secret_via_gh(*, repo_dir: str, adc_path: str) -> None:
  """Validates local ADC user credentials and sets `AUTOCLOUD_OAUTH_CONFIG`.

  Reads `client_id`, `client_secret`, and `refresh_token` from `adc_path`,
  verifies that a fresh access token can be minted against Google OAuth, and
  pipes the JSON payload into `gh secret set AUTOCLOUD_OAUTH_CONFIG` over
  stdin without logging or echoing secrets.

  Args:
    repo_dir: Target repository directory where `gh` should execute.
    adc_path: Path to local `application_default_credentials.json`.

  Raises:
    auth.TokenSourceError: If ADC file lacks valid human OAuth credentials.
    RuntimeError: If `gh secret set` fails.
  """
  source = auth.OAuthRefreshTokenSource.from_adc_file(adc_path)
  source.token()

  with open(adc_path, encoding='utf-8') as adc_file:
    raw_adc = json.load(adc_file)
  secret_payload = json.dumps({
      'client_id': str(raw_adc['client_id']).strip(),
      'client_secret': str(raw_adc['client_secret']).strip(),
      'refresh_token': str(raw_adc['refresh_token']).strip(),
  })

  try:
    subprocess.run(
        ['gh', 'secret', 'set', 'AUTOCLOUD_OAUTH_CONFIG'],
        cwd=os.path.realpath(repo_dir),
        input=secret_payload,
        text=True,
        shell=False,
        check=True,
        capture_output=True,
    )
  except subprocess.CalledProcessError as err:
    raise RuntimeError(
        f'gh secret set AUTOCLOUD_OAUTH_CONFIG failed (exit {err.returncode}).'
    ) from err


def main(argv: Sequence[str]) -> None:
  """CLI entrypoint for `autocloud-github-init`."""
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')
  if not _PROJECT_ID.value:
    raise app.UsageError('--project_id is required.')

  repo_dir = _REPO_DIR.value or os.getcwd()
  config_path, workflow_path = initialize_repository(
      repo_dir=repo_dir,
      project_id=_PROJECT_ID.value,
      agent_id=_AGENT_ID.value,
      action_ref=_ACTION_REF.value,
  )
  logging.info('Generated AutoCloud config: %s', config_path)
  logging.info('Generated GitHub workflow: %s', workflow_path)

  if _SET_GITHUB_SECRET.value:
    upload_oauth_secret_via_gh(repo_dir=repo_dir, adc_path=_ADC_PATH.value)
    logging.info(
        'Uploaded AUTOCLOUD_OAUTH_CONFIG secret via `gh secret set`.'
    )


def run() -> None:
  """Zero-argument console script entrypoint for `pyproject.toml`."""
  app.run(main)


if __name__ == '__main__':
  run()
