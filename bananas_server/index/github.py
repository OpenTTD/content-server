import base64
import click
import git
import logging
import tempfile
import os

from openttd_helpers import click_helper

from .local import Index as LocalIndex

log = logging.getLogger(__name__)

_github_branch = None
_github_private_key = None
_github_url = None


class Index(LocalIndex):
    def __init__(self):
        super().__init__()

        # We need to write the private key to disk: GitPython can only use
        # SSH-keys that are written on disk.
        if _github_private_key:
            self._github_private_key_file = tempfile.NamedTemporaryFile()
            self._github_private_key_file.write(_github_private_key)
            self._github_private_key_file.flush()

            self._ssh_command = f"ssh -i {self._github_private_key_file.name}"
        else:
            self._ssh_command = None

        try:
            self._git = git.Repo(self._folder)
        except git.exc.NoSuchPathError:
            self._git = git.Repo.init(self._folder)
        except git.exc.InvalidGitRepositoryError:
            self._git = git.Repo.init(self._folder)

        # Make sure the origin is set correctly
        if "origin" not in self._git.remotes:
            self._git.create_remote("origin", _github_url)
        origin = self._git.remotes.origin
        if origin.url != _github_url:
            origin.set_url(_github_url)

    def _remove_empty_folders(self, parent_folder):
        removed = False
        for root, folders, files in os.walk(parent_folder, topdown=False):
            if root.startswith(".git"):
                continue

            if not folders and not files:
                os.rmdir(root)
                removed = True

        return removed

    def _fetch_latest(self, branch):
        log.info("Updating index to latest version from GitHub")

        origin = self._git.remotes.origin

        # Checkout the latest default branch, removing and commits/file
        # changes local might have.
        with self._git.git.custom_environment(GIT_SSH_COMMAND=self._ssh_command):
            try:
                origin.fetch()
            except git.exc.BadName:
                # When the garbage collector kicks in, GitPython gets confused and
                # throws a BadName. The best solution? Just run it again.
                origin.fetch()

        origin.refs[branch].checkout(force=True, B=branch)
        for file_name in self._git.untracked_files:
            os.unlink(f"{self._folder}/{file_name}")

        # We might end up with empty folders, which the rest of the
        # application doesn't really like. So remove them. Keep repeating the
        # function until no folders are removed anymore.
        while self._remove_empty_folders(self._folder):
            pass

    def reload(self, application):
        self._fetch_latest(_github_branch)
        return super().reload(application)


@click_helper.extend
@click.option(
    "--index-github-url",
    help="Repository URL on GitHub. (index=github only)",
    default="https://github.com/OpenTTD/BaNaNaS",
    show_default=True,
    metavar="URL",
)
@click.option(
    "--index-github-branch",
    help="Branch of the GitHub repository to use.",
    default="main",
    show_default=True,
    metavar="branch",
)
@click.option(
    "--index-github-private-key",
    help="Base64-encoded private key to access GitHub."
    "Always use this via an environment variable!"
    "(index=github only)",
)
def click_index_github(index_github_url, index_github_branch, index_github_private_key):
    global _github_url, _github_branch, _github_private_key

    _github_url = index_github_url
    _github_branch = index_github_branch
    if index_github_private_key:
        _github_private_key = base64.b64decode(index_github_private_key)
