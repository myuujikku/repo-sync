#!/usr/bin/env python3
"""Synchronize explicitly selected source branches to a GitHub destination."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class SyncError(Exception):
    """An expected, safe-to-display synchronization error."""


def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    command_env = os.environ.copy()
    command_env.update(env)
    command_env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise SyncError("Could not execute Git. Ensure Git is installed and available on PATH.") from exc
    if result.returncode:
        # Git/server diagnostics may contain URLs or credentials; never expose them.
        raise SyncError(f"Git operation failed (exit code {result.returncode}): {args[0]}.")
    return result.stdout


def _validate_branch(branch: str, env: dict[str, str]) -> None:
    if not branch or branch.startswith("-") or branch != branch.strip():
        raise SyncError(f"Invalid branch name: {branch!r}.")
    # Validate an explicit full ref, preventing options and refspec syntax.
    _git(["check-ref-format", f"refs/heads/{branch}"], env=env)


def parse_branches(value: str, env: dict[str, str]) -> list[str]:
    branches = [line.strip() for line in value.splitlines() if line.strip()]
    if not branches:
        raise SyncError("SYNC_BRANCHES must contain at least one branch name.")
    seen: set[str] = set()
    for branch in branches:
        _validate_branch(branch, env)
        if branch in seen:
            raise SyncError(f"Duplicate branch name: {branch!r}.")
        seen.add(branch)
    return branches


def _validate_url(value: str, label: str, *, require_https: bool) -> str:
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        raise SyncError(f"{label} must be a valid URL without whitespace or control characters.")
    if not require_https and Path(value).is_absolute() and Path(value).exists():
        return value
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() in ({"https"} if require_https else {"https", "file"})
        valid = valid and (bool(parsed.hostname) if parsed.scheme.lower() == "https" else bool(parsed.path))
        valid = valid and parsed.username is None and parsed.password is None and not parsed.fragment and not parsed.query
        if parsed.scheme.lower() == "https":
            # Accessing port also validates malformed port values.
            _ = parsed.port
    except ValueError:
        valid = False
        parsed = None
    if not valid:
        schemes = "HTTPS" if require_https else "HTTPS or a local file URL"
        raise SyncError(f"{label} must be a valid {schemes} URL without embedded credentials or a fragment.")
    return value


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _askpass_file(directory: Path) -> Path:
    path = directory / "askpass.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
        "key = 'GIT_SYNC_USERNAME' if 'username' in prompt else 'GIT_SYNC_PASSWORD'\n"
        "sys.stdout.write(__import__('os').environ.get(key, ''))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _auth_env(askpass: Path, username: str | None, token: str | None) -> dict[str, str]:
    result = {"GIT_ASKPASS": str(askpass), "GIT_SYNC_USERNAME": username or "", "GIT_SYNC_PASSWORD": token or ""}
    return result


def _source_auth_env(askpass: Path, username: str | None, token: str | None) -> dict[str, str]:
    return _auth_env(askpass, username or ("x-access-token" if token else None), token)


def sync(source_url: str, destination_url: str, branches: list[str], destination_token: str,
         source_username: str | None = None, source_token: str | None = None,
         *, require_https: bool = True) -> None:
    source_url = _validate_url(source_url, "SOURCE_REPO_URL", require_https=require_https)
    destination_url = _validate_url(destination_url, "DESTINATION_REPO_URL", require_https=require_https)
    if _canonical_url(source_url) == _canonical_url(destination_url):
        raise SyncError("SOURCE_REPO_URL and DESTINATION_REPO_URL must refer to different repositories.")
    if not branches:
        raise SyncError("At least one branch must be selected.")
    validation_env: dict[str, str] = {}
    for branch in branches:
        _validate_branch(branch, validation_env)
    if not destination_token:
        raise SyncError("DESTINATION_TOKEN is required.")
    if source_username and not source_token:
        raise SyncError("SOURCE_USERNAME requires SOURCE_TOKEN.")

    with tempfile.TemporaryDirectory(prefix="branch-sync-") as temporary:
        directory = Path(temporary)
        askpass = _askpass_file(directory)
        repository = directory / "sync.git"
        _git(["init", "--bare", "--quiet", str(repository)], env={})

        source_auth = _source_auth_env(askpass, source_username, source_token)
        destination_auth = _auth_env(askpass, "x-access-token", destination_token)

        source_refs = _git(["ls-remote", "--heads", source_url,
                            *[f"refs/heads/{branch}" for branch in branches]],
                           env=source_auth, cwd=repository)
        available_source = {
            fields[1][len("refs/heads/"):] for line in source_refs.splitlines()
            if len(fields := line.split("\t", 1)) == 2 and fields[1].startswith("refs/heads/")
        }
        missing_source = [branch for branch in branches if branch not in available_source]
        if missing_source:
            raise SyncError(f"Selected source branch(es) not found: {', '.join(missing_source)}.")

        source_refspecs = [f"+refs/heads/{branch}:refs/heads/{branch}" for branch in branches]
        source_refspecs.append("+refs/tags/*:refs/tags/*")
        _git(["fetch", "--quiet", "--no-recurse-submodules", source_url, *source_refspecs],
             env=source_auth, cwd=repository)

        source_tags = _git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/tags/"],
                           env={}, cwd=repository)
        source_tag_refs: dict[str, str] = {}
        for line in source_tags.splitlines():
            ref, object_id = line.split(" ", 1)
            source_tag_refs[ref] = object_id

        # Discover and fetch only selected destination branches. Missing branches are valid.
        remote_refs = _git(["ls-remote", "--heads", destination_url,
                            *[f"refs/heads/{branch}" for branch in branches]],
                           env=destination_auth, cwd=repository)
        present: set[str] = set()
        for line in remote_refs.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[1].startswith("refs/heads/"):
                present.add(fields[1][len("refs/heads/"):])

        destination_tags_output = _git(["ls-remote", "--tags", "--refs", destination_url],
                                       env=destination_auth, cwd=repository)
        destination_tags: dict[str, str] = {}
        for line in destination_tags_output.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and fields[1].startswith("refs/tags/"):
                destination_tags[fields[1]] = fields[0]
        for ref, object_id in source_tag_refs.items():
            if ref in destination_tags and destination_tags[ref] != object_id:
                tag_name = ref[len("refs/tags/"):]
                raise SyncError(f"Tag {tag_name!r} conflicts with a different destination tag object; refusing to overwrite it.")
        destination_refspecs = [
            f"refs/heads/{branch}:refs/remotes/destination/{branch}" for branch in branches if branch in present
        ]
        if destination_refspecs:
            _git(["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", destination_url,
                  *destination_refspecs], env=destination_auth, cwd=repository)

        for branch in branches:
            source_ref = f"refs/heads/{branch}"
            result = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", source_ref], cwd=repository,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                capture_output=True, check=False,
            )
            if result.returncode != 0:
                raise SyncError(f"Selected source branch {branch!r} was not fetched.")
            if branch in present:
                result = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", f"refs/remotes/destination/{branch}", source_ref],
                    cwd=repository,
                    env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 1:
                    raise SyncError(f"Branch {branch!r} has diverged; refusing to overwrite destination history.")
                if result.returncode != 0:
                    raise SyncError(f"Could not compare selected branch {branch!r} with destination.")

        push_refspecs = [f"refs/heads/{branch}:refs/heads/{branch}" for branch in branches]
        push_refspecs.extend(f"{ref}:{ref}" for ref in source_tag_refs)
        _git(["push", "--quiet", "--atomic", destination_url, *push_refspecs],
             env=destination_auth, cwd=repository)


def main() -> int:
    source_url = os.environ.get("SOURCE_REPO_URL", "")
    destination_url = os.environ.get("DESTINATION_REPO_URL", "")
    branch_config = os.environ.get("SYNC_BRANCHES", "")
    destination_token = os.environ.get("DESTINATION_TOKEN", "")
    source_username = os.environ.get("SOURCE_USERNAME") or None
    source_token = os.environ.get("SOURCE_TOKEN") or None
    try:
        if not source_url:
            raise SyncError("SOURCE_REPO_URL is required.")
        if not destination_url:
            raise SyncError("DESTINATION_REPO_URL is required.")
        if not destination_token:
            raise SyncError("DESTINATION_TOKEN is required.")
        branches = parse_branches(branch_config, {})
        sync(source_url, destination_url, branches, destination_token, source_username, source_token)
    except SyncError as exc:
        print(f"Branch synchronization failed: {exc}", file=sys.stderr)
        return 1
    print(f"Successfully synchronized {len(branches)} selected branch(es) and all source tags.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
