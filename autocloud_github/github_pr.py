"""Git workspace remediation writer and GitHub REST API client."""

from collections.abc import Sequence
import os
import re
import subprocess
from typing import Any

from absl import logging
import requests

from autocloud_github import config
from autocloud_github import parser

DEFAULT_GITHUB_API_URL = 'https://api.github.com'
_GITHUB_TIMEOUT_SECONDS = 30
_BRANCH_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,199}$')
_REPO_FULL_NAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$')


class GitHubIntegrationError(RuntimeError):
  """Raised when git operations or GitHub REST API requests fail."""


def validate_branch_name(branch_name: str) -> str:
  """Validates a git branch name against option/command injection.

  Args:
    branch_name: Candidate git branch ref name.

  Returns:
    Validated branch name string.

  Raises:
    ValueError: If `branch_name` contains unsafe characters or `..`.
  """
  cleaned = branch_name.strip()
  if not _BRANCH_NAME_RE.match(cleaned) or '..' in cleaned:
    raise ValueError(f'Unsafe git branch name rejected: {branch_name!r}')
  return cleaned


def validate_repo_full_name(repo_full_name: str) -> str:
  """Validates a GitHub `owner/repo` string.

  Args:
    repo_full_name: Repository identifier in `owner/repo` format.

  Returns:
    Validated `owner/repo` string.

  Raises:
    ValueError: If `repo_full_name` is malformed.
  """
  cleaned = repo_full_name.strip()
  if not _REPO_FULL_NAME_RE.match(cleaned):
    raise ValueError(f'Invalid GitHub repository name: {repo_full_name!r}')
  return cleaned


def companion_branch_for_pr(pr_number: int) -> str:
  """Returns the deterministic Companion PR branch name for `pr_number`."""
  if pr_number <= 0:
    raise ValueError(f'pr_number must be positive, got {pr_number}')
  return f'{config.COMPANION_BRANCH_PREFIX}{pr_number}'


def write_remediated_files_safely(
    workspace_dir: str,
    remediated_files: Sequence[parser.RemediatedFile],
) -> list[str]:
  """Writes remediated files inside `workspace_dir` with path traversal guards.

  Resolves both `workspace_dir` and every target path with `os.path.realpath`
  to guarantee symlinks or relative segments cannot escape the repository root.

  Args:
    workspace_dir: Local git checkout root directory.
    remediated_files: Validated `RemediatedFile` items extracted from MCP.

  Returns:
    List of normalized relative paths written to disk.

  Raises:
    ValueError: If any path escapes `workspace_dir` or targets `.git`/`.github`.
    OSError: If file creation fails.
  """
  workspace_real = os.path.realpath(workspace_dir)
  if not os.path.isdir(workspace_real):
    raise ValueError(f'Workspace directory does not exist: {workspace_dir!r}')

  written_paths: list[str] = []
  for item in remediated_files:
    rel_path = parser.sanitize_relative_repo_path(item.path)
    candidate_abs = os.path.join(workspace_real, rel_path)
    parent_real = os.path.realpath(os.path.dirname(candidate_abs))

    if parent_real != workspace_real and not parent_real.startswith(
        workspace_real + os.sep
    ):
      raise ValueError(
          f'Resolved parent directory escapes workspace: {item.path!r}'
      )

    os.makedirs(parent_real, exist_ok=True)
    target_real = os.path.realpath(candidate_abs)
    if not target_real.startswith(workspace_real + os.sep):
      raise ValueError(f'Resolved file path escapes workspace: {item.path!r}')

    if os.path.islink(candidate_abs):
      raise ValueError(f'Refusing to overwrite symlink path: {item.path!r}')

    content = item.content
    if not content.endswith('\n'):
      content = f'{content}\n'

    with open(target_real, 'w', encoding='utf-8') as out_file:
      out_file.write(content)
    written_paths.append(rel_path)

  return written_paths


def _run_git(workspace_dir: str, args: Sequence[str]) -> str:
  """Executes a `git` subcommand safely without a shell."""
  cmd = ['git', *args]
  try:
    completed = subprocess.run(
        cmd,
        cwd=workspace_dir,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()
  except subprocess.CalledProcessError as err:
    stderr_summary = (err.stderr or '').strip()
    raise GitHubIntegrationError(
        f'git {args[0]} failed (exit {err.returncode}): {stderr_summary}'
    ) from err


def commit_and_push_companion_branch(
    *,
    workspace_dir: str,
    companion_branch: str,
    written_paths: Sequence[str],
    pr_number: int,
    push_remote: bool = True,
) -> bool:
  """Creates/updates the companion branch, commits changes, and pushes.

  Args:
    workspace_dir: Local git repository root.
    companion_branch: Companion branch name (`autocloud/remediate-pr-<N>`).
    written_paths: Relative file paths written by `write_remediated_files_safely`.
    pr_number: Pull Request number being remediated.
    push_remote: Whether to push the branch to `origin` (False in dry runs).

  Returns:
    True if at least one file changed and was committed; False if no diff.

  Raises:
    ValueError: If `companion_branch` or `written_paths` fail validation.
    GitHubIntegrationError: If any `git` command fails.
  """
  if not written_paths:
    return False

  safe_branch = validate_branch_name(companion_branch)
  safe_paths = [parser.sanitize_relative_repo_path(p) for p in written_paths]

  _run_git(workspace_dir, ['checkout', '-B', safe_branch])
  _run_git(
      workspace_dir, ['config', 'user.name', 'github-actions[bot]']
  )
  _run_git(
      workspace_dir,
      [
          'config',
          'user.email',
          '41898282+github-actions[bot]@users.noreply.github.com',
      ],
  )
  _run_git(workspace_dir, ['add', '--', *safe_paths])

  status_output = _run_git(
      workspace_dir, ['status', '--porcelain', '--', *safe_paths]
  )
  if not status_output:
    logging.info('Remediated files match HEAD; skipping companion commit.')
    return False

  commit_msg = (
      f'fix(autocloud): apply IaC review remediations for PR #{pr_number}'
  )
  _run_git(workspace_dir, ['commit', '-m', commit_msg])

  if push_remote:
    refspec = f'HEAD:refs/heads/{safe_branch}'
    _run_git(
        workspace_dir,
        ['push', '--force-with-lease', 'origin', refspec],
    )
  return True


class GitHubClient:
  """Minimal GitHub REST API v3 client using `GITHUB_TOKEN`."""

  def __init__(
      self,
      github_token: str,
      api_base_url: str = DEFAULT_GITHUB_API_URL,
  ):
    """Initializes `GitHubClient`.

    Args:
      github_token: GitHub Actions `GITHUB_TOKEN` or fine-grained token.
      api_base_url: HTTPS base URL for GitHub REST API.

    Raises:
      ValueError: If `github_token` is empty or `api_base_url` is not HTTPS.
    """
    if not github_token or not github_token.strip():
      raise ValueError('github_token must be non-empty.')
    if not api_base_url.startswith('https://'):
      raise ValueError(
          f'api_base_url must start with https://, got {api_base_url!r}'
      )
    self._token = github_token.strip()
    self._api_base_url = api_base_url.rstrip('/')

  def _headers(self) -> dict[str, str]:
    """Returns standard GitHub REST API request headers."""
    return {
        'Authorization': f'Bearer {self._token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }

  def _request_json(
      self,
      method: str,
      path: str,
      *,
      params: dict[str, Any] | None = None,
      json_body: dict[str, Any] | None = None,
  ) -> Any:
    """Performs an authenticated HTTPS request to GitHub's REST API."""
    url = f'{self._api_base_url}/{path.lstrip("/")}'
    try:
      response = requests.request(
          method=method,
          url=url,
          headers=self._headers(),
          params=params,
          json=json_body,
          timeout=_GITHUB_TIMEOUT_SECONDS,
      )
    except requests.RequestException as err:
      raise GitHubIntegrationError(
          f'GitHub API network error ({method} {path}).'
      ) from err

    if response.status_code >= 400:
      raise GitHubIntegrationError(
          f'GitHub API {method} {path} returned HTTP {response.status_code}.'
      )

    if not response.text:
      return None
    try:
      return response.json()
    except ValueError as err:
      raise GitHubIntegrationError(
          f'GitHub API {method} {path} returned invalid JSON.'
      ) from err

  def list_pr_changed_files(
      self, repo_full_name: str, pr_number: int
  ) -> list[str]:
    """Lists non-deleted repository-relative files changed in a Pull Request."""
    safe_repo = validate_repo_full_name(repo_full_name)
    items = self._request_json(
        'GET',
        f'/repos/{safe_repo}/pulls/{pr_number}/files',
        params={'per_page': 100},
    )
    if not isinstance(items, list):
      return []

    paths: list[str] = []
    for entry in items:
      if not isinstance(entry, dict):
        continue
      if entry.get('status') == 'removed':
        continue
      filename = str(entry.get('filename', ''))
      try:
        paths.append(parser.sanitize_relative_repo_path(filename))
      except ValueError:
        continue
    return paths

  def list_issue_comments(
      self, repo_full_name: str, issue_number: int
  ) -> list[str]:
    """Lists chronological comment body strings on an issue or PR."""
    safe_repo = validate_repo_full_name(repo_full_name)
    items = self._request_json(
        'GET',
        f'/repos/{safe_repo}/issues/{issue_number}/comments',
        params={'per_page': 100},
    )
    if not isinstance(items, list):
      return []
    return [
        str(item.get('body', ''))
        for item in items
        if isinstance(item, dict) and item.get('body')
    ]

  def list_pr_review_bodies(
      self, repo_full_name: str, pr_number: int
  ) -> list[str]:
    """Lists chronological Pull Request review body strings on a PR."""
    safe_repo = validate_repo_full_name(repo_full_name)
    items = self._request_json(
        'GET',
        f'/repos/{safe_repo}/pulls/{pr_number}/reviews',
        params={'per_page': 100},
    )
    if not isinstance(items, list):
      return []
    return [
        str(item.get('body', ''))
        for item in items
        if isinstance(item, dict) and item.get('body')
    ]

  def post_issue_comment(
      self, repo_full_name: str, issue_number: int, body: str
  ) -> str:
    """Posts a comment on a GitHub Pull Request or Issue."""
    safe_repo = validate_repo_full_name(repo_full_name)
    resp = self._request_json(
        'POST',
        f'/repos/{safe_repo}/issues/{issue_number}/comments',
        json_body={'body': body},
    )
    if isinstance(resp, dict):
      return str(resp.get('html_url', ''))
    return ''

  def post_pr_review(
      self,
      repo_full_name: str,
      pr_number: int,
      body: str,
      inline_comments: Sequence[parser.InlineReviewComment] = (),
  ) -> str:
    """Posts a Pull Request Review with optional line-anchored inline comments.

    Uses `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` (`event:
    "COMMENT"`). If GitHub rejects the inline comments array (e.g. HTTP 422 when
    a line number falls outside the PR diff hunk), falls back to posting the
    review summary and inline suggestions in a top-level PR comment so findings
    are never lost.

    Args:
      repo_full_name: GitHub `owner/repo` string.
      pr_number: Pull Request number.
      body: Top-level review summary body (including `context_id` marker).
      inline_comments: Line-anchored `InlineReviewComment` items.

    Returns:
      HTML URL of the created Pull Request review or fallback comment.
    """
    safe_repo = validate_repo_full_name(repo_full_name)
    serialized_comments: list[dict[str, Any]] = []
    for item in inline_comments:
      try:
        safe_path = parser.sanitize_relative_repo_path(item.path)
      except ValueError:
        continue
      if item.line < 1 or not item.body.strip():
        continue
      entry: dict[str, Any] = {
          'path': safe_path,
          'line': item.line,
          'side': 'RIGHT',
          'body': item.body,
      }
      if item.start_line is not None and 1 <= item.start_line < item.line:
        entry['start_line'] = item.start_line
        entry['start_side'] = 'RIGHT'
      serialized_comments.append(entry)

    review_payload: dict[str, Any] = {
        'event': 'COMMENT',
        'body': body,
    }
    if serialized_comments:
      review_payload['comments'] = serialized_comments

    try:
      resp = self._request_json(
          'POST',
          f'/repos/{safe_repo}/pulls/{pr_number}/reviews',
          json_body=review_payload,
      )
      if isinstance(resp, dict):
        return str(resp.get('html_url', ''))
      return ''
    except GitHubIntegrationError as err:
      if not serialized_comments:
        raise
      logging.warning(
          'Inline PR review returned %s; falling back to PR comment.', err
      )
      fallback_sections = [body]
      for cmt in serialized_comments:
        loc = (
            f"{cmt['path']}:L{cmt['start_line']}-L{cmt['line']}"
            if 'start_line' in cmt
            else f"{cmt['path']}:L{cmt['line']}"
        )
        fallback_sections.append(f"#### `{loc}`\n{cmt['body']}")
      return self.post_issue_comment(
          safe_repo, pr_number, '\n\n'.join(fallback_sections)
      )

  def create_or_update_companion_pr(
      self,
      *,
      repo_full_name: str,
      source_pr_number: int,
      pr_head_branch: str,
      companion_branch: str,
      summary_markdown: str,
  ) -> str:
    """Creates or updates the Companion PR targeting the contributor's branch.

    Args:
      repo_full_name: GitHub `owner/repo` string.
      source_pr_number: Original PR number being reviewed.
      pr_head_branch: Contributor's PR head branch (target `base` of companion).
      companion_branch: Companion branch (`autocloud/remediate-pr-<N>`).
      summary_markdown: Markdown summary of findings and remediations.

    Returns:
      HTML URL of the created or updated Companion PR.
    """
    safe_repo = validate_repo_full_name(repo_full_name)
    owner = safe_repo.split('/', 1)[0]
    safe_base = validate_branch_name(pr_head_branch)
    safe_head = validate_branch_name(companion_branch)

    title = f'[AutoCloud] IaC & Code Remediations for PR #{source_pr_number}'
    body = (
        f'Automated Companion PR for #{source_pr_number} generated by '
        '**AutoCloud Operator**.\n\n'
        f'{summary_markdown}'
    )

    existing_prs = self._request_json(
        'GET',
        f'/repos/{safe_repo}/pulls',
        params={
            'head': f'{owner}:{safe_head}',
            'base': safe_base,
            'state': 'open',
        },
    )
    if isinstance(existing_prs, list) and existing_prs:
      first = existing_prs[0]
      if isinstance(first, dict) and first.get('number'):
        companion_num = int(first['number'])
        updated = self._request_json(
            'PATCH',
            f'/repos/{safe_repo}/pulls/{companion_num}',
            json_body={'title': title, 'body': body},
        )
        if isinstance(updated, dict):
          return str(updated.get('html_url', ''))

    created = self._request_json(
        'POST',
        f'/repos/{safe_repo}/pulls',
        json_body={
            'title': title,
            'head': safe_head,
            'base': safe_base,
            'body': body,
        },
    )
    if isinstance(created, dict):
      return str(created.get('html_url', ''))
    return ''
