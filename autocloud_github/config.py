"""Configuration parser and event router for AutoCloud GitHub integration."""

from collections.abc import Mapping
import dataclasses
import re
from typing import Any

import yaml

_PROJECT_ID_RE = re.compile(r'^[a-z][a-z0-9-]{4,28}[a-z0-9]$')
_AGENT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{1,62}$')

ALLOWED_CODE_TRIGGERS = frozenset({
    'pull_request.opened',
    'pull_request.synchronize',
    'pull_request.reopened',
    'issue_comment.created',
})

ALLOWED_ISSUE_TRIGGERS = frozenset({
    'issues.opened',
    'issues.labeled',
    'issue_comment.created',
})

COMPANION_BRANCH_PREFIX = 'autocloud/remediate-pr-'


class ConfigValidationError(ValueError):
  """Raised when `.github/autocloud.yml` fails schema validation."""


@dataclasses.dataclass(frozen=True)
class CodeChangesConfig:
  """Configuration for Scenario 1: PR Code & IaC Review (Milestone 1 MVP)."""

  enabled: bool = True
  triggers: tuple[str, ...] = (
      'pull_request.opened',
      'pull_request.synchronize',
      'issue_comment.created',
  )
  trigger_command: str = '/autocloud review'
  pr_mode: str = 'companion_pr'


@dataclasses.dataclass(frozen=True)
class IssueImplementationConfig:
  """Configuration for Scenario 2: Issue & Bug Implementation (Milestone 2)."""

  enabled: bool = False
  triggers: tuple[str, ...] = (
      'issues.labeled',
      'issue_comment.created',
  )
  trigger_labels: tuple[str, ...] = ('autocloud-implement',)
  trigger_command: str = '/autocloud implement'
  base_branch: str = 'main'


@dataclasses.dataclass(frozen=True)
class ScenariosConfig:
  """Container for configured AutoCloud GitHub scenarios."""

  code_changes: CodeChangesConfig = dataclasses.field(
      default_factory=CodeChangesConfig
  )
  issue_implementation: IssueImplementationConfig = dataclasses.field(
      default_factory=IssueImplementationConfig
  )


@dataclasses.dataclass(frozen=True)
class AutoCloudRepoConfig:
  """Top-level configuration parsed from `.github/autocloud.yml`."""

  version: int
  project_id: str
  agent_id: str = ''
  scenarios: ScenariosConfig = dataclasses.field(
      default_factory=ScenariosConfig
  )


def _parse_code_changes(raw_map: Mapping[str, Any]) -> CodeChangesConfig:
  """Parses and validates the `scenarios.code_changes` block."""
  enabled = bool(raw_map.get('enabled', True))
  raw_triggers = raw_map.get(
      'triggers',
      [
          'pull_request.opened',
          'pull_request.synchronize',
          'issue_comment.created',
      ],
  )
  if not isinstance(raw_triggers, list) or not raw_triggers:
    raise ConfigValidationError(
        'scenarios.code_changes.triggers must be a non-empty list.'
    )
  triggers: list[str] = []
  for item in raw_triggers:
    if not isinstance(item, str) or item not in ALLOWED_CODE_TRIGGERS:
      raise ConfigValidationError(
          f'Unsupported code_changes trigger: {item!r}. '
          f'Allowed: {sorted(ALLOWED_CODE_TRIGGERS)}'
      )
    triggers.append(item)

  trigger_command = str(
      raw_map.get('trigger_command', '/autocloud review')
  ).strip()
  pr_mode = str(raw_map.get('pr_mode', 'companion_pr')).strip()
  if pr_mode != 'companion_pr':
    raise ConfigValidationError(
        f'Unsupported pr_mode: {pr_mode!r}. Expected "companion_pr".'
    )
  return CodeChangesConfig(
      enabled=enabled,
      triggers=tuple(triggers),
      trigger_command=trigger_command,
      pr_mode=pr_mode,
  )


def _parse_issue_impl(raw_map: Mapping[str, Any]) -> IssueImplementationConfig:
  """Parses and validates the `scenarios.issue_implementation` block."""
  enabled = bool(raw_map.get('enabled', False))
  raw_triggers = raw_map.get(
      'triggers', ['issues.labeled', 'issue_comment.created']
  )
  if not isinstance(raw_triggers, list):
    raise ConfigValidationError(
        'scenarios.issue_implementation.triggers must be a list.'
    )
  triggers: list[str] = []
  for item in raw_triggers:
    if not isinstance(item, str) or item not in ALLOWED_ISSUE_TRIGGERS:
      raise ConfigValidationError(
          f'Unsupported issue_implementation trigger: {item!r}.'
      )
    triggers.append(item)

  raw_labels = raw_map.get('trigger_labels', ['autocloud-implement'])
  if not isinstance(raw_labels, list):
    raise ConfigValidationError(
        'scenarios.issue_implementation.trigger_labels must be a list.'
    )
  labels = tuple(str(label).strip() for label in raw_labels if str(label))
  trigger_command = str(
      raw_map.get('trigger_command', '/autocloud implement')
  ).strip()
  base_branch = str(raw_map.get('base_branch', 'main')).strip() or 'main'
  return IssueImplementationConfig(
      enabled=enabled,
      triggers=tuple(triggers),
      trigger_labels=labels,
      trigger_command=trigger_command,
      base_branch=base_branch,
  )


def normalize_agent_id(raw_agent_id: str) -> str:
  """Normalizes an optional AutoCloud Operator `agent_id`.

  The Gemini Cloud Assist frontend prepends the reserved `autocloud-` prefix
  automatically when routing to backing agents, so any leading `autocloud-`
  prefix is stripped here. An empty string selects the caller's personal agent.

  Args:
    raw_agent_id: Raw `agent_id` value from `.github/autocloud.yml` or CLI.

  Returns:
    Normalized unprefixed `agent_id`, or `''` for the caller's personal agent.

  Raises:
    ConfigValidationError: If `raw_agent_id` is malformed or bare `autocloud-`.
  """
  cleaned = raw_agent_id.strip()
  if not cleaned:
    return ''

  if cleaned.startswith('autocloud-'):
    cleaned = cleaned.removeprefix('autocloud-')
    if not cleaned:
      raise ConfigValidationError(
          'agent_id must not be the bare reserved prefix "autocloud-".'
      )

  if not _AGENT_ID_RE.match(cleaned):
    raise ConfigValidationError(f'Invalid agent_id: {raw_agent_id!r}.')
  return cleaned


def parse_config_yaml(yaml_content: str) -> AutoCloudRepoConfig:
  """Parses and validates `.github/autocloud.yml` YAML string content.

  Args:
    yaml_content: Raw YAML string from `.github/autocloud.yml`.

  Returns:
    Validated `AutoCloudRepoConfig` instance.

  Raises:
    ConfigValidationError: If required fields are missing or invalid.
  """
  try:
    parsed = yaml.safe_load(yaml_content)
  except yaml.YAMLError as err:
    raise ConfigValidationError(f'Invalid YAML syntax: {err}') from err

  if not isinstance(parsed, Mapping):
    raise ConfigValidationError('Configuration root must be a YAML mapping.')

  version = parsed.get('version', 1)
  if version != 1:
    raise ConfigValidationError(
        f'Unsupported config version: {version!r}. Expected 1.'
    )

  project_id = str(parsed.get('project_id', '')).strip()
  if not _PROJECT_ID_RE.match(project_id):
    raise ConfigValidationError(
        f'Invalid or missing project_id: {project_id!r}.'
    )

  raw_agent_id = str(parsed.get('agent_id', '') or '')
  agent_id = normalize_agent_id(raw_agent_id)

  raw_scenarios = parsed.get('scenarios') or {}
  if not isinstance(raw_scenarios, Mapping):
    raise ConfigValidationError('`scenarios` must be a mapping.')

  raw_code = raw_scenarios.get('code_changes') or {}
  if not isinstance(raw_code, Mapping):
    raise ConfigValidationError('`scenarios.code_changes` must be a mapping.')

  raw_issue = raw_scenarios.get('issue_implementation') or {}
  if not isinstance(raw_issue, Mapping):
    raise ConfigValidationError(
        '`scenarios.issue_implementation` must be a mapping.'
    )

  return AutoCloudRepoConfig(
      version=version,
      project_id=project_id,
      agent_id=agent_id,
      scenarios=ScenariosConfig(
          code_changes=_parse_code_changes(raw_code),
          issue_implementation=_parse_issue_impl(raw_issue),
      ),
  )


def load_config(config_path: str) -> AutoCloudRepoConfig:
  """Reads and validates `.github/autocloud.yml` from disk.

  Args:
    config_path: File path to `.github/autocloud.yml`.

  Returns:
    Validated `AutoCloudRepoConfig` instance.

  Raises:
    ConfigValidationError: If the file cannot be read or fails validation.
  """
  try:
    with open(config_path, encoding='utf-8') as config_file:
      content = config_file.read()
  except OSError as err:
    raise ConfigValidationError(
        f'Could not read config file {config_path!r}: {err}'
    ) from err
  return parse_config_yaml(content)


def should_trigger_code_review(
    repo_config: AutoCloudRepoConfig,
    event_name: str,
    event_payload: Mapping[str, Any],
) -> bool:
  """Determines whether a GitHub event should trigger a PR code review.

  Skips draft PRs and Companion PR branches (`autocloud/remediate-pr-*`) to
  prevent recursive review loops.

  Args:
    repo_config: Validated repository configuration.
    event_name: GitHub Actions `GITHUB_EVENT_NAME` (e.g., `pull_request`).
    event_payload: Decoded JSON from `GITHUB_EVENT_PATH`.

  Returns:
    True if the PR code review workflow should execute, False otherwise.
  """
  code_cfg = repo_config.scenarios.code_changes
  if not code_cfg.enabled:
    return False

  action = str(event_payload.get('action', '')).strip()
  qualified_event = f'{event_name}.{action}' if action else event_name
  if qualified_event not in code_cfg.triggers:
    return False

  if event_name == 'pull_request':
    pull_request = event_payload.get('pull_request') or {}
    if not isinstance(pull_request, Mapping):
      return False
    if pull_request.get('draft', False):
      return False
    head_info = pull_request.get('head') or {}
    head_ref = str(head_info.get('ref', ''))
    if head_ref.startswith(COMPANION_BRANCH_PREFIX):
      return False
    return True

  if event_name == 'issue_comment':
    issue = event_payload.get('issue') or {}
    if not isinstance(issue, Mapping) or 'pull_request' not in issue:
      return False
    comment = event_payload.get('comment') or {}
    comment_body = str(comment.get('body', '')).strip()
    return code_cfg.trigger_command in comment_body

  return False
