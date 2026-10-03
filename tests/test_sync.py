import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sync import SyncError, _askpass_file, _source_auth_env, main, parse_branches, sync


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout.strip()


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.git"
        self.destination = self.root / "destination.git"
        git("init", "--bare", "--quiet", self.source)
        git("init", "--bare", "--quiet", self.destination)
        self.work = self.root / "work"
        git("init", "--quiet", "-b", "main", self.work)
        git("config", "user.name", "Test", cwd=self.work)
        git("config", "user.email", "test@example.invalid", cwd=self.work)
        self._commit("main-1")
        git("remote", "add", "origin", self.source, cwd=self.work)
        git("push", "--quiet", "origin", "main", cwd=self.work)
        git("switch", "--quiet", "-c", "release", cwd=self.work)
        self._commit("release-1")
        git("push", "--quiet", "origin", "release", cwd=self.work)
        git("tag", "v1", cwd=self.work)
        git("push", "--quiet", "origin", "refs/tags/v1", cwd=self.work)
        self.lightweight_tag = git("rev-parse", "refs/tags/v1", cwd=self.work)
        git("switch", "--quiet", "--orphan", "tag-history", cwd=self.work)
        self._commit("outside selected branches")
        git("tag", "-a", "outside-history", "-m", "annotated tag", cwd=self.work)
        git("push", "--quiet", "origin", "refs/tags/outside-history", cwd=self.work)
        self.annotated_tag = git("rev-parse", "refs/tags/outside-history", cwd=self.work)
        git("switch", "--quiet", "main", cwd=self.work)
        (self.work / "destination-tag.txt").write_text("destination tag", encoding="utf-8")
        git("add", "destination-tag.txt", cwd=self.work)
        git("commit", "--quiet", "-m", "destination tag object", cwd=self.work)
        git("tag", "destination-only", cwd=self.work)
        self.destination_only_tag = git("rev-parse", "refs/tags/destination-only", cwd=self.work)
        git("push", "--quiet", self.destination, "refs/tags/destination-only", cwd=self.work)

    def tearDown(self):
        self.temp.cleanup()

    def _commit(self, content):
        (self.work / "tracked.txt").write_text(content, encoding="utf-8")
        git("add", "tracked.txt", cwd=self.work)
        git("commit", "--quiet", "-m", content, cwd=self.work)

    def _sync(self, branches=("main", "release")):
        sync(str(self.source), str(self.destination), list(branches), "destination-secret", require_https=False)

    def _destination_refs(self):
        return git("ls-remote", "--heads", self.destination).splitlines()

    def test_initial_multiple_branches_syncs_all_tags_and_preserves_destination_only_refs(self):
        git("push", "--quiet", self.destination, "HEAD:refs/heads/destination-only", cwd=self.work)
        destination_tag = {line.split("\t")[1]: line.split("\t")[0]
                           for line in git("ls-remote", "--tags", self.destination).splitlines()}
        destination_only_sha = {
            ref: sha for line in self._destination_refs() for sha, ref in [line.split("\t")]
        }["refs/heads/destination-only"]
        self._sync()
        refs = {line.split("\t")[1]: line.split("\t")[0] for line in self._destination_refs()}
        names = set(refs)
        self.assertEqual(names, {"refs/heads/main", "refs/heads/release", "refs/heads/destination-only"})
        self.assertEqual(refs["refs/heads/destination-only"], destination_only_sha)
        for branch in ("main", "release"):
            source_sha = {ref: sha for line in git("ls-remote", "--heads", self.source,
                                                   f"refs/heads/{branch}").splitlines()
                          for sha, ref in [line.split("\t")]}[f"refs/heads/{branch}"]
            self.assertEqual(refs[f"refs/heads/{branch}"], source_sha)
        tags_after = {line.split("\t")[1]: line.split("\t")[0]
                      for line in git("ls-remote", "--tags", "--refs", self.destination).splitlines()}
        self.assertEqual(tags_after["refs/tags/destination-only"], self.destination_only_tag)
        self.assertEqual(tags_after["refs/tags/v1"], self.lightweight_tag)
        self.assertEqual(tags_after["refs/tags/outside-history"], self.annotated_tag)
        self.assertEqual(tags_after["refs/tags/destination-only"], destination_tag["refs/tags/destination-only"])
        self.assertEqual(set(tags_after), {"refs/tags/destination-only", "refs/tags/v1", "refs/tags/outside-history"})

    def test_subsequent_fast_forward_sync(self):
        self._sync()
        git("switch", "--quiet", "main", cwd=self.work)
        self._commit("main-2")
        git("push", "--quiet", "origin", "main", cwd=self.work)
        self._sync()
        source_sha = git("rev-parse", "main", cwd=self.work)
        destination_sha = {ref: sha for line in self._destination_refs()
                           for sha, ref in [line.split("\t")]}["refs/heads/main"]
        self.assertEqual(destination_sha, source_sha)

    def test_new_tags_are_synchronized_and_identical_tags_are_idempotent(self):
        self._sync()
        git("tag", "new-lightweight", "main", cwd=self.work)
        git("tag", "-a", "new-annotated", "release", "-m", "new annotated tag", cwd=self.work)
        git("push", "--quiet", "origin", "refs/tags/new-lightweight", "refs/tags/new-annotated", cwd=self.work)
        self._sync()
        expected = {"refs/tags/v1", "refs/tags/outside-history", "refs/tags/new-lightweight",
                    "refs/tags/new-annotated"}
        tags_before = {line.split("\t")[1]: line.split("\t")[0]
                       for line in git("ls-remote", "--tags", "--refs", self.destination).splitlines()}
        self.assertEqual(set(tags_before), expected | {"refs/tags/destination-only"})
        self._sync()
        tags_after = {line.split("\t")[1]: line.split("\t")[0]
                      for line in git("ls-remote", "--tags", "--refs", self.destination).splitlines()}
        self.assertEqual(tags_after, tags_before)

    def test_no_source_tags_synchronizes_selected_branches(self):
        git("init", "--bare", "--quiet", self.root / "no-tags.git")
        git("push", "--quiet", self.root / "no-tags.git", "main", "release", cwd=self.work)
        sync(str(self.root / "no-tags.git"), str(self.destination), ["main", "release"],
             "destination-secret", require_https=False)
        tags = git("ls-remote", "--tags", "--refs", self.destination).splitlines()
        self.assertEqual({line.split("\t")[1] for line in tags}, {"refs/tags/destination-only"})

    def test_divergence_rejected_without_partial_push(self):
        self._sync()
        git("clone", "--quiet", self.destination, self.root / "dest-work")
        dest_work = self.root / "dest-work"
        git("config", "user.name", "Test", cwd=dest_work)
        git("config", "user.email", "test@example.invalid", cwd=dest_work)
        git("switch", "--quiet", "main", cwd=dest_work)
        (dest_work / "other.txt").write_text("destination", encoding="utf-8")
        git("add", "other.txt", cwd=dest_work)
        git("commit", "--quiet", "-m", "diverge", cwd=dest_work)
        git("push", "--quiet", "origin", "main", cwd=dest_work)
        # Advance another source branch; the divergent branch must prevent all writes.
        git("switch", "--quiet", "release", cwd=self.work)
        self._commit("release-2")
        git("push", "--quiet", "origin", "release", cwd=self.work)
        git("tag", "new-before-divergence", "release", cwd=self.work)
        git("push", "--quiet", "origin", "refs/tags/new-before-divergence", cwd=self.work)
        before = set(self._destination_refs())
        tags_before = git("ls-remote", "--tags", "--refs", self.destination)
        with self.assertRaisesRegex(SyncError, "diverged"):
            self._sync()
        self.assertEqual(set(self._destination_refs()), before)
        self.assertEqual(git("ls-remote", "--tags", "--refs", self.destination), tags_before)

    def test_missing_selected_source_branch_does_not_partially_push(self):
        before = set(self._destination_refs())
        tags_before = git("ls-remote", "--tags", "--refs", self.destination)
        with self.assertRaises(SyncError):
            self._sync(("main", "does-not-exist"))
        self.assertEqual(set(self._destination_refs()), before)
        self.assertEqual(git("ls-remote", "--tags", "--refs", self.destination), tags_before)

    def test_tag_conflict_aborts_branch_and_tag_changes(self):
        git("push", "--quiet", self.destination, "refs/heads/main:refs/heads/destination-only", cwd=self.work)
        git("tag", "-f", "v1", "main", cwd=self.work)
        git("push", "--quiet", self.destination, "refs/tags/v1", cwd=self.work)
        before_branches = set(self._destination_refs())
        before_tags = git("ls-remote", "--tags", "--refs", self.destination)
        # The source v1 tag points to the release branch; destination v1 points to main.
        with self.assertRaisesRegex(SyncError, "Tag 'v1' conflicts"):
            self._sync()
        self.assertEqual(set(self._destination_refs()), before_branches)
        self.assertEqual(git("ls-remote", "--tags", "--refs", self.destination), before_tags)


class ValidationTests(unittest.TestCase):
    def test_branch_config_exact_names_and_rejects_unsafe_values(self):
        self.assertEqual(parse_branches("main\n\nrelease\n", {}), ["main", "release"])
        for value in ("", "--upload-pack=x", "main:other", "a*b", "main\nmain"):
            with self.subTest(value=value), self.assertRaises(SyncError):
                parse_branches(value, {})

    def test_missing_secrets_and_unsafe_urls_are_rejected(self):
        for values, expected in [({}, "SOURCE_REPO_URL"),
                                 ({"SOURCE_REPO_URL": "https://source.invalid/repo.git"}, "DESTINATION_REPO_URL"),
                                 ({"SOURCE_REPO_URL": "https://source.invalid/repo.git",
                                   "DESTINATION_REPO_URL": "https://dest.invalid/repo.git",
                                   "SYNC_BRANCHES": "main"}, "DESTINATION_TOKEN")]:
            with patch.dict(os.environ, values, clear=True), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(), 1)
                self.assertIn(expected, error.getvalue())
        for url in ("https://user:password@host.invalid/repo", "http://host.invalid/repo",
                    "https://host.invalid/repo#fragment", "https://host.invalid/repo?token=secret",
                    "https://host.invalid/repo\n?token=secret", "https://host.invalid/repo\ttoken"):
            with self.subTest(url=url), self.assertRaises(SyncError):
                sync(url, "https://dest.invalid/repo", ["main"], "token")

    def test_same_repository_url_and_source_username_without_token_are_rejected(self):
        with self.assertRaisesRegex(SyncError, "different repositories"):
            sync("https://host.invalid/repo.git", "https://HOST.invalid/repo", ["main"], "token")
        with self.assertRaisesRegex(SyncError, "SOURCE_USERNAME requires SOURCE_TOKEN"):
            sync("https://source.invalid/repo", "https://dest.invalid/repo", ["main"], "token",
                 source_username="source-user")

    def test_source_token_without_username_uses_access_token_username(self):
        with tempfile.TemporaryDirectory() as temporary:
            askpass = _askpass_file(Path(temporary))
            auth = _source_auth_env(askpass, None, "source-secret")
            self.assertEqual(auth["GIT_SYNC_USERNAME"], "x-access-token")
            self.assertEqual(auth["GIT_SYNC_PASSWORD"], "source-secret")
            self.assertEqual(auth["GIT_ASKPASS"], str(askpass))
            self.assertNotIn("source-secret", askpass.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
