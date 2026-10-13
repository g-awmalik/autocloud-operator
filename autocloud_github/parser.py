"""Response parser and remediated-file extractor for AutoCloud GitHub Action.

Handles two critical protocol details of `ask_cloud_operator`:
  1. Tool-level failures arrive as HTTP 200 with `result.isError: true`.
  2. Long-running invocations may answer as an SSE stream (`data: ...`) or a
     single JSON-RPC payload.

Also extracts remediated file blocks emitted by the agent and enforces path
sanitization against directory traversal (`..`) and protected `.git/` or
`.github/` paths.
"""

from collections.abc import Mapping, Sequence
import dataclasses
import difflib
import json
import posixpath
import re
from typing import Any

_CONTEXT_ID_MARKER_RE = re.compile(
    r'<!--\s*autocloud-context-id:\s*([a-zA-Z0-9_./:-]+)\s*-->'
)

# Matches fenced code blocks with an explicit file path annotation:
#   ```hcl title="envs/prod/main.tf"
#   ```terraform:envs/prod/main.tf
#   ```yaml file=k8s/deployment.yaml
_FENCED_FILE_BLOCK_RE = re.compile(
    r'```[a-zA-Z0-9_-]*'
    r'(?:\s+(?:title|file|path)=["\']?([^"\'\n`]+)["\']?|:([^\s\n`]+))'
    r'\s*\n(.*?)```',
    re.DOTALL,
)

# Matches a fenced JSON block containing structured inline PR review comments:
#   ```json title="autocloud-inline-comments"
_INLINE_COMMENTS_BLOCK_RE = re.compile(
    r'(?:^|\n)(?:#{1,4}\s*Line-Anchored[^\n]*\n+\s*)?'
    r'```(?:json)?\s+(?:title|file|path)=["\']?autocloud-inline-comments["\']?'
    r'\s*\n(.*?)```',
    re.DOTALL | re.IGNORECASE,
)

_LINE_NUMBER_PREFIX_RE = re.compile(r'^\s*\d+:\s?(.*)$')

# Fallback: ### File: `path/to/file.tf` immediately followed by a fenced block.
_HEADING_FILE_BLOCK_RE = re.compile(
    r'#{2,4}\s+File:\s*`?([^`\n]+?)`?\s*\n+```[a-zA-Z0-9_-]*\s*\n(.*?)```',
    re.DOTALL,
)


class McpError(Exception):
  """Raised when `ask_cloud_operator` reports a JSON-RPC or tool error."""

  def __init__(self, code: int, status: str, message: str):
    """Initializes `McpError`.

    Args:
      code: Numeric error code (0 if unspecified).
      status: Canonical status string (e.g., `PERMISSION_DENIED`).
      message: Error message reported by the backend.
    """
    self.code = code
    self.status = status
    self.message = message
    super().__init__(f'{status or code}: {message}')


@dataclasses.dataclass(frozen=True)
class McpAnswer:
  """Parsed response from `ask_cloud_operator`."""

  content: str
  context_id: str | None = None


@dataclasses.dataclass(frozen=True)
class RemediatedFile:
  """A remediated repository file extracted from the agent's review output."""

  path: str
  content: str


@dataclasses.dataclass(frozen=True)
class InlineReviewComment:
  """A line-anchored GitHub Pull Request inline review comment."""

  path: str
  line: int
  body: str
  start_line: int | None = None


def parse_sse(raw_body: str) -> str:
  """Extracts the final JSON payload from an SSE response stream.

  Args:
    raw_body: Raw HTTP response body (either plain JSON or `data:` SSE lines).

  Returns:
    JSON string representing the final JSON-RPC response message.
  """
  if 'data:' not in raw_body:
    return raw_body

  chunks: list[str] = []
  for line in raw_body.splitlines():
    stripped = line.strip()
    if stripped.startswith('data:'):
      chunk = stripped[len('data:') :].strip()
      if chunk and chunk != '[DONE]':
        chunks.append(chunk)

  if not chunks:
    return raw_body

  for chunk in reversed(chunks):
    try:
      json.loads(chunk)
      return chunk
    except json.JSONDecodeError:
      continue
  return chunks[-1]


def _maybe_json(text: str) -> Any:
  """Parses `text` as JSON or returns None if it is not valid JSON."""
  try:
    return json.loads(text)
  except (json.JSONDecodeError, TypeError):
    return None


def _extract_text_parts(result_map: Mapping[str, Any]) -> list[str]:
  """Collects text strings from `result.content` items."""
  parts: list[str] = []
  content_list = result_map.get('content')
  if not isinstance(content_list, Sequence):
    return parts
  for item in content_list:
    if isinstance(item, Mapping) and item.get('type') == 'text':
      text = item.get('text')
      if isinstance(text, str) and text:
        parts.append(text)
  return parts


def raise_for_tool_error(result_map: Mapping[str, Any]) -> None:
  """Raises `McpError` if `result.isError` is truthy.

  Args:
    result_map: The `result` dictionary from a JSON-RPC `tools/call` response.

  Raises:
    McpError: When `result_map.get('isError')` is True.
  """
  if not result_map.get('isError'):
    return

  code = 0
  status = ''
  message = ''
  for text in _extract_text_parts(result_map):
    parsed = _maybe_json(text)
    if isinstance(parsed, Mapping) and isinstance(parsed.get('error'), Mapping):
      err = parsed['error']
      code = int(err.get('code', 0) or 0)
      status = str(err.get('status', '') or '')
      message = str(err.get('message', '') or '')
      details = err.get('details')
      if isinstance(details, Sequence):
        for detail in details:
          if isinstance(detail, Mapping) and detail.get('detail'):
            message = f"{message} {detail['detail']}".strip()
      break
    if not message:
      message = text

  raise McpError(code, status, message.strip() or 'Unknown MCP tool error.')


def extract_answer(payload: Mapping[str, Any]) -> McpAnswer:
  """Extracts the text content and `context_id` from a JSON-RPC response.

  Args:
    payload: Decoded JSON-RPC response dictionary.

  Returns:
    `McpAnswer` with `content` and optional `context_id`.

  Raises:
    McpError: If the payload contains a JSON-RPC or tool-level error, or if the
      response content is empty.
  """
  if 'error' in payload and isinstance(payload['error'], Mapping):
    err = payload['error']
    raise McpError(
        int(err.get('code', 0) or 0),
        str(err.get('status', '') or ''),
        str(err.get('message', 'JSON-RPC error')),
    )

  result_map = payload.get('result')
  if not isinstance(result_map, Mapping):
    raise McpError(0, '', 'Malformed MCP response: missing `result` object.')

  raise_for_tool_error(result_map)

  structured = result_map.get('structuredContent')
  if isinstance(structured, Mapping) and structured.get('content'):
    context_id = (
        structured.get('context_id') or structured.get('contextId') or None
    )
    return McpAnswer(
        content=str(structured['content']).strip(),
        context_id=str(context_id) if context_id else None,
    )

  content_chunks: list[str] = []
  context_id_val: str | None = None
  for text in _extract_text_parts(result_map):
    parsed = _maybe_json(text)
    if isinstance(parsed, Mapping):
      inner = (
          parsed['structuredContent']
          if isinstance(parsed.get('structuredContent'), Mapping)
          else parsed
      )
      if 'content' in inner or 'context_id' in inner or 'contextId' in inner:
        if inner.get('content'):
          content_chunks.append(str(inner['content']))
        cand = inner.get('context_id') or inner.get('contextId')
        if cand and not context_id_val:
          context_id_val = str(cand)
      else:
        content_chunks.append(text)
    else:
      content_chunks.append(text)

  if not context_id_val:
    top_cand = result_map.get('context_id') or result_map.get('contextId')
    if top_cand:
      context_id_val = str(top_cand)

  combined = '\n\n'.join(
      chunk.strip() for chunk in content_chunks if chunk and chunk.strip()
  )
  if not combined:
    raise McpError(0, '', 'AutoCloud returned an empty response.')
  return McpAnswer(content=combined, context_id=context_id_val)


def sanitize_relative_repo_path(raw_path: str) -> str:
  """Validates and normalizes a repository-relative file path.

  Rejects absolute paths, parent-directory traversals (`..`), backslashes, and
  protected `.git/` or `.github/` paths.

  Args:
    raw_path: Candidate file path extracted from LLM markdown output.

  Returns:
    Normalized POSIX relative path within the repository.

  Raises:
    ValueError: If the path is unsafe or targets protected directories.
  """
  cleaned = raw_path.strip().replace('\\', '/')
  if not cleaned or cleaned.startswith('/'):
    raise ValueError(f'Path must be non-empty and relative: {raw_path!r}')

  normalized = posixpath.normpath(cleaned)
  if (
      normalized == '.'
      or normalized == '..'
      or normalized.startswith('../')
      or '/..' in normalized
  ):
    raise ValueError(f'Path traversal rejected: {raw_path!r}')

  if (
      normalized == '.git'
      or normalized.startswith('.git/')
      or normalized == '.github'
      or normalized.startswith('.github/')
  ):
    raise ValueError(
        f'Protected repository metadata path rejected: {raw_path!r}'
    )
  return normalized


def _strip_line_number_prefixes(code_text: str) -> str:
  """Strips accidental `1: ` line-number prefixes from LLM code blocks."""
  lines = code_text.splitlines()
  non_empty = [line for line in lines if line.strip()]
  if not non_empty:
    return code_text
  prefixed_count = sum(
      1 for line in non_empty if _LINE_NUMBER_PREFIX_RE.match(line)
  )
  if prefixed_count * 2 <= len(non_empty):
    return code_text
  stripped_lines: list[str] = []
  for line in lines:
    match = _LINE_NUMBER_PREFIX_RE.match(line)
    stripped_lines.append(match.group(1) if match else line)
  trailing_nl = '\n' if code_text.endswith('\n') else ''
  return '\n'.join(stripped_lines) + trailing_nl


def extract_remediated_files(markdown_text: str) -> list[RemediatedFile]:
  """Extracts remediated file blocks from the AutoCloud Operator's response.

  Supports both inline fenced attributes (e.g., ````hcl title="main.tf"`) and
  `### File: main.tf` headings followed by a fenced block. Unsafe paths are
  skipped. When multiple blocks target the same path, the last block wins.

  Args:
    markdown_text: Full markdown response from `ask_cloud_operator`.

  Returns:
    Ordered list of validated `RemediatedFile` objects.
  """
  by_path: dict[str, RemediatedFile] = {}

  for match in _FENCED_FILE_BLOCK_RE.finditer(markdown_text):
    raw_path = (match.group(1) or match.group(2) or '').strip()
    if raw_path == 'autocloud-inline-comments':
      continue
    code_body = _strip_line_number_prefixes(match.group(3))
    try:
      safe_path = sanitize_relative_repo_path(raw_path)
    except ValueError:
      continue
    by_path[safe_path] = RemediatedFile(path=safe_path, content=code_body)

  for match in _HEADING_FILE_BLOCK_RE.finditer(markdown_text):
    raw_path = match.group(1) or ''
    code_body = _strip_line_number_prefixes(match.group(2))
    try:
      safe_path = sanitize_relative_repo_path(raw_path)
    except ValueError:
      continue
    if safe_path not in by_path:
      by_path[safe_path] = RemediatedFile(path=safe_path, content=code_body)

  return list(by_path.values())


def extract_inline_comments(markdown_text: str) -> list[InlineReviewComment]:
  """Extracts structured inline PR review comments from the agent's response.

  Parses `title="autocloud-inline-comments"` JSON code blocks containing a list
  of comment objects (`path`, `line`, optional `start_line`, `body`, and
  optional `suggestion`).

  Args:
    markdown_text: Full markdown response from `ask_cloud_operator`.

  Returns:
    List of validated `InlineReviewComment` objects.
  """
  comments: list[InlineReviewComment] = []
  for match in _INLINE_COMMENTS_BLOCK_RE.finditer(markdown_text):
    raw_json = match.group(1).strip()
    parsed = _maybe_json(raw_json)
    if isinstance(parsed, Mapping) and isinstance(
        parsed.get('comments'), Sequence
    ):
      items = parsed['comments']
    elif isinstance(parsed, Sequence):
      items = parsed
    else:
      continue

    for item in items:
      if not isinstance(item, Mapping):
        continue
      raw_path = str(item.get('path') or item.get('file') or '').strip()
      try:
        safe_path = sanitize_relative_repo_path(raw_path)
      except ValueError:
        continue
      try:
        line = int(item.get('line', 0) or 0)
      except (TypeError, ValueError):
        continue
      if line < 1:
        continue

      start_line_val: int | None = None
      raw_start = item.get('start_line')
      if raw_start is not None:
        try:
          cand_start = int(raw_start)
          if 1 <= cand_start < line:
            start_line_val = cand_start
        except (TypeError, ValueError):
          start_line_val = None

      body = str(item.get('body') or item.get('comment') or '').strip()
      suggestion = item.get('suggestion')
      if isinstance(suggestion, str) and '```suggestion' not in body:
        clean_sugg = _strip_line_number_prefixes(suggestion).rstrip('\n')
        body = (
            f'{body}\n\n```suggestion\n{clean_sugg}\n```'
            if body
            else f'```suggestion\n{clean_sugg}\n```'
        )
      if not body:
        continue

      comments.append(
          InlineReviewComment(
              path=safe_path,
              line=line,
              body=body,
              start_line=start_line_val,
          )
      )
  return comments


def clamp_inline_comments_to_files(
    inline_comments: Sequence[InlineReviewComment],
    changed_files: Mapping[str, str],
) -> list[InlineReviewComment]:
  """Clamps inline comment line numbers to the actual line count of each file.

  If an LLM slightly miscounts lines near the end of a file (e.g. emitting
  `start_line=21, line=25` for a 23-line file), this shifts and clamps the
  line range into `[1, max_line]` so GitHub's Pull Request Review API does not
  reject the entire review batch with HTTP 422.

  Args:
    inline_comments: Candidate `InlineReviewComment` items.
    changed_files: Mapping of relative file paths to file content strings.

  Returns:
    List of `InlineReviewComment` items with line numbers clamped to file bounds.
  """
  clamped: list[InlineReviewComment] = []
  for item in inline_comments:
    file_text = changed_files.get(item.path)
    if file_text is None:
      clamped.append(item)
      continue
    max_line = len(file_text.splitlines())
    if max_line < 1:
      continue
    if item.line > max_line:
      overshoot = item.line - max_line
      new_line = max_line
      new_start_line: int | None = None
      if item.start_line is not None:
        shifted_start = max(1, item.start_line - overshoot)
        if shifted_start < new_line:
          new_start_line = shifted_start
      clamped.append(
          InlineReviewComment(
              path=item.path,
              line=new_line,
              body=item.body,
              start_line=new_start_line,
          )
      )
    else:
      clamped.append(item)
  return clamped


def build_inline_comments_from_remediated_files(
    original_files: Mapping[str, str],
    remediated_files: Sequence[RemediatedFile],
) -> list[InlineReviewComment]:
  """Computes line-anchored GitHub `suggestion` comments by diffing files.

  When the agent emits complete remediated file blocks (`title="<path>"`), this
  function diffs each remediated file against its original PR content and builds
  inline review comments with native GitHub ````suggestion` blocks anchored to
  the exact 1-based line ranges in the existing PR.

  Args:
    original_files: Mapping of relative file paths to original PR file contents.
    remediated_files: Sequence of `RemediatedFile` objects from the agent.

  Returns:
    List of `InlineReviewComment` objects with GitHub ````suggestion` blocks.
  """
  inline_comments: list[InlineReviewComment] = []
  for rem in remediated_files:
    orig_text = original_files.get(rem.path)
    if orig_text is None:
      continue
    orig_lines = orig_text.splitlines()
    rem_lines = rem.content.splitlines()
    if orig_lines == rem_lines or not orig_lines:
      continue

    matcher = difflib.SequenceMatcher(a=orig_lines, b=rem_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
      if tag == 'equal':
        continue
      replacement = '\n'.join(rem_lines[j1:j2])
      if tag in ('replace', 'delete'):
        start_1 = i1 + 1
        end_1 = max(start_1, i2)
        start_line = start_1 if start_1 < end_1 else None
        line = end_1
        sugg_body = (
            '**AutoCloud Suggested Remediation:**\n'
            f'```suggestion\n{replacement}\n```'
        )
      else:
        # Pure insertion (`tag == 'insert'`): anchor to the line at i1 (or 1)
        # and include that anchor line in the suggestion block so GitHub
        # replaces 1 line with (anchor line + inserted lines).
        anchor_idx = min(max(i1 - 1, 0), len(orig_lines) - 1)
        line = anchor_idx + 1
        start_line = None
        if i1 == 0:
          combined = (
              f'{replacement}\n{orig_lines[0]}'
              if replacement
              else orig_lines[0]
          )
        else:
          combined = (
              f'{orig_lines[anchor_idx]}\n{replacement}'
              if replacement
              else orig_lines[anchor_idx]
          )
        sugg_body = (
            '**AutoCloud Suggested Addition:**\n'
            f'```suggestion\n{combined}\n```'
        )

      inline_comments.append(
          InlineReviewComment(
              path=rem.path,
              line=line,
              body=sugg_body,
              start_line=start_line,
          )
      )
  return inline_comments


def strip_inline_comments_json_block(markdown_text: str) -> str:
  """Removes the machine-readable `autocloud-inline-comments` block."""
  cleaned = _INLINE_COMMENTS_BLOCK_RE.sub('', markdown_text).strip()
  return cleaned or markdown_text.strip()


def embed_context_id_marker(
    comment_body: str, context_id: str | None
) -> str:
  """Appends a hidden HTML comment marker with the AutoCloud `context_id`.

  Args:
    comment_body: Markdown text to post on the GitHub PR.
    context_id: Optional AutoCloud conversation ID returned by MCP. When None,
      `comment_body` is returned unchanged.

  Returns:
    Markdown comment string with embedded `<!-- autocloud-context-id: ... -->`.
  """
  if not context_id:
    return comment_body
  return f'{comment_body.rstrip()}\n\n<!-- autocloud-context-id: {context_id} -->'


def extract_context_id_marker(comments: Sequence[str]) -> str | None:
  """Finds the most recent AutoCloud `context_id` from GitHub PR comments.

  Args:
    comments: Sequence of PR comment body strings in chronological order.

  Returns:
    The latest `context_id` string if present, or None if not found.
  """
  for body in reversed(comments):
    match = _CONTEXT_ID_MARKER_RE.search(body)
    if match:
      return match.group(1)
  return None
