"""
GitHub integration for Anton.

Wraps PyGithub for the operations needed in the workflow:
  - Clone/read repo files
  - Create a fix branch
  - Commit changes
  - Open a pull request
  - Fetch CI run status
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from github import Github, GithubException
    HAS_GITHUB = True
except ImportError:
    HAS_GITHUB = False


@dataclass
class PRResult:
    number: int
    title: str
    url: str
    branch: str


@dataclass
class CIResult:
    run_id: int
    status: str        # queued / in_progress / completed
    conclusion: str    # success / failure / cancelled / None
    html_url: str
    log_url: str


class GitHubClient:
    def __init__(self, token: str, repo_name: str):
        if not HAS_GITHUB:
            raise ImportError("PyGithub not installed. Run: pip install PyGithub")
        self.gh = Github(token)
        self.repo = self.gh.get_repo(repo_name)
        self.repo_name = repo_name

    def get_file_content(self, path: str, ref: str = "main") -> str:
        try:
            content = self.repo.get_contents(path, ref=ref)
            return content.decoded_content.decode("utf-8")
        except GithubException as e:
            raise FileNotFoundError(f"Could not read {path}@{ref}: {e}")

    def list_files(self, directory: str = "", ref: str = "main") -> list[str]:
        try:
            contents = self.repo.get_contents(directory, ref=ref)
            paths = []
            while contents:
                item = contents.pop(0)
                if item.type == "dir":
                    contents.extend(self.repo.get_contents(item.path, ref=ref))
                else:
                    paths.append(item.path)
            return paths
        except GithubException as e:
            logger.error("list_files error: %s", e)
            return []

    def create_branch(self, branch_name: str, base: str = "main") -> None:
        base_sha = self.repo.get_branch(base).commit.sha
        try:
            self.repo.create_git_ref(f"refs/heads/{branch_name}", base_sha)
            logger.info("Created branch %s from %s", branch_name, base)
        except GithubException as e:
            if "Reference already exists" in str(e):
                logger.info("Branch %s already exists", branch_name)
            else:
                raise

    def commit_file(self, path: str, content: str, message: str, branch: str) -> None:
        try:
            existing = self.repo.get_contents(path, ref=branch)
            self.repo.update_file(path, message, content, existing.sha, branch=branch)
        except GithubException:
            self.repo.create_file(path, message, content, branch=branch)
        logger.info("Committed %s on branch %s", path, branch)

    def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        labels: Optional[list[str]] = None,
    ) -> PRResult:
        try:
            pr = self.repo.create_pull(title=title, body=body, head=head, base=base)
        except GithubException as e:
            if "already exists" in str(e):
                # PR already open for this branch — return it
                for open_pr in self.repo.get_pulls(state="open", head=f"{self.repo.owner.login}:{head}"):
                    logger.info("PR already exists: %s", open_pr.html_url)
                    return PRResult(number=open_pr.number, title=open_pr.title, url=open_pr.html_url, branch=head)
            raise
        if labels:
            try:
                pr.add_to_labels(*labels)
            except GithubException:
                pass  # labels may not exist yet
        logger.info("PR #%d created: %s", pr.number, pr.html_url)
        return PRResult(number=pr.number, title=pr.title, url=pr.html_url, branch=head)

    def merge_pull_request(self, number: int, method: str = "squash") -> str:
        pr = self.repo.get_pull(number)
        result = pr.merge(merge_method=method)
        logger.info("Merged PR #%d (%s)", number, method)
        return getattr(result, "sha", "") or ""

    def get_latest_ci_run(self, branch: str) -> Optional[CIResult]:
        try:
            runs = self.repo.get_workflow_runs(branch=branch)
            for run in runs:
                return CIResult(
                    run_id=run.id,
                    status=run.status,
                    conclusion=run.conclusion or "",
                    html_url=run.html_url,
                    log_url=run.logs_url,
                )
        except GithubException as e:
            logger.warning("CI run fetch failed: %s", e)
        return None


# ── Mock client ───────────────────────────────────────────────────────────────

class MockGitHubClient:
    def __init__(self):
        self.repo_name = "demo-org/storefront"
        self._committed_files: dict[str, str] = {}
        self._prs: list[PRResult] = []

    def get_file_content(self, path: str, ref: str = "main") -> str:
        logger.info("[MOCK] Reading %s @ %s", path, ref)
        if path == "orders/calculator.py":
            return open(
                os.path.join(os.path.dirname(__file__), "../demo/sample_repo/orders/calculator.py")
            ).read()
        if path == "tests/test_calculator.py":
            return open(
                os.path.join(os.path.dirname(__file__), "../demo/sample_repo/tests/test_calculator.py")
            ).read()
        return f"# mock content for {path}"

    def list_files(self, directory: str = "", ref: str = "main") -> list[str]:
        return [
            "main.py",
            "orders/__init__.py",
            "orders/models.py",
            "orders/calculator.py",
            "tests/__init__.py",
            "tests/test_calculator.py",
        ]

    def create_branch(self, branch_name: str, base: str = "main") -> None:
        logger.info("[MOCK] Created branch %s from %s", branch_name, base)

    def commit_file(self, path: str, content: str, message: str, branch: str) -> None:
        self._committed_files[path] = content
        logger.info("[MOCK] Committed %s on %s: %s", path, branch, message)

    def create_pull_request(self, title: str, body: str, head: str, base: str = "main", labels=None) -> PRResult:
        pr = PRResult(
            number=len(self._prs) + 1,
            title=title,
            url=f"https://github.com/{self.repo_name}/pull/{len(self._prs)+1}",
            branch=head,
        )
        self._prs.append(pr)
        logger.info("[MOCK] PR created: %s → %s", head, pr.url)
        return pr

    def merge_pull_request(self, number: int, method: str = "squash") -> str:
        logger.info("[MOCK] Merged PR #%d (%s)", number, method)
        return "mockmergesha"

    def get_latest_ci_run(self, branch: str) -> Optional[CIResult]:
        logger.info("[MOCK] Fetching CI run for branch %s", branch)
        return CIResult(
            run_id=99999,
            status="completed",
            conclusion="success",
            html_url=f"https://github.com/demo-org/storefront/actions/runs/99999",
            log_url="",
        )


def get_github_client(repo_name: Optional[str] = None) -> "GitHubClient | MockGitHubClient":
    token = os.getenv("GITHUB_TOKEN")
    repo = repo_name or os.getenv("GITHUB_REPO")
    if token and repo:
        return GitHubClient(token, repo)
    logger.warning("GITHUB_TOKEN or GITHUB_REPO not set — using mock client")
    return MockGitHubClient()
