"""GitHub integration tools."""

import os
from typing import Any, Optional
from pydantic import BaseModel, Field
from config.configuration import Config
from tools.tool_interface import Tool, ToolInvocation, ToolResult, ToolKind

try:
    from github import Github, GithubException
    HAS_GITHUB = True
except ImportError:
    HAS_GITHUB = False
    Github = None
    GithubException = Exception


class GitHubIssueParams(BaseModel):
    repo: str = Field(..., description="Repository in format 'owner/repo'")
    issue_number: int = Field(..., description="Issue number")
    token: Optional[str] = Field(None, description="GitHub personal access token")


class GitHubIssueTool(Tool):
    name = "analyze_github_issue"
    description = """Fetch and analyze a GitHub issue. Returns issue details including title, description, status, labels, and comments."""
    kind = ToolKind.NETWORK
    schema = GitHubIssueParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        if not HAS_GITHUB:
            return ToolResult.error_result("PyGithub not installed. Run: pip install PyGithub")

        params = GitHubIssueParams(**invocation.params)
        token = params.token or os.getenv("GITHUB_TOKEN")

        if not token:
            return ToolResult.error_result("GitHub token required. Set GITHUB_TOKEN env var or provide token parameter")

        try:
            g = Github(token)
            repo = g.get_repo(params.repo)
            issue = repo.get_issue(params.issue_number)

            output = f"""# Issue #{issue.number}: {issue.title}

**Status:** {issue.state}
**Author:** {issue.user.login}
**Labels:** {', '.join([l.name for l in issue.labels]) or 'None'}

## Description
{issue.body or 'No description'}

## Comments ({issue.comments} total)
"""
            if issue.comments > 0:
                comments = list(issue.get_comments())[-3:]
                for comment in comments:
                    output += f"\n**{comment.user.login}**: {comment.body[:200]}...\n"

            return ToolResult.success_result(output=output, metadata={"url": issue.html_url})

        except Exception as e:
            return ToolResult.error_result(f"Error: {str(e)}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return False


class GitHubPRParams(BaseModel):
    repo: str = Field(..., description="Repository in format 'owner/repo'")
    title: str = Field(..., description="PR title")
    body: str = Field("", description="PR description")
    head: str = Field(..., description="Source branch name")
    base: str = Field("main", description="Target branch")
    token: Optional[str] = Field(None, description="GitHub token")


class GitHubPRTool(Tool):
    name = "create_pull_request"
    description = """Create a GitHub pull request with title, description, and branch information."""
    kind = ToolKind.NETWORK
    schema = GitHubPRParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        if not HAS_GITHUB:
            return ToolResult.error_result("PyGithub not installed")

        params = GitHubPRParams(**invocation.params)
        token = params.token or os.getenv("GITHUB_TOKEN")

        if not token:
            return ToolResult.error_result("GitHub token required")

        try:
            g = Github(token)
            repo = g.get_repo(params.repo)
            pr = repo.create_pull(title=params.title, body=params.body, head=params.head, base=params.base)

            output = f"""✅ Pull Request Created!

**#{pr.number}: {pr.title}**
**URL:** {pr.html_url}
**Branch:** {params.head} → {params.base}"""

            return ToolResult.success_result(output=output, metadata={"url": pr.html_url, "pr_number": pr.number})

        except Exception as e:
            return ToolResult.error_result(f"Error: {str(e)}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return True


class GitHubCodeSearchParams(BaseModel):
    query: str = Field(..., description="Search query")
    repo: Optional[str] = Field(None, description="Limit to specific repo (owner/repo)")
    language: Optional[str] = Field(None, description="Filter by programming language")
    max_results: int = Field(10, description="Maximum results to return")
    token: Optional[str] = Field(None, description="GitHub token")


class GitHubCodeSearchTool(Tool):
    name = "search_github_code"
    description = """Search for code in GitHub repositories. Useful for finding similar code patterns, function definitions, and usage examples."""
    kind = ToolKind.NETWORK
    schema = GitHubCodeSearchParams

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        if not HAS_GITHUB:
            return ToolResult.error_result("PyGithub not installed")

        params = GitHubCodeSearchParams(**invocation.params)
        token = params.token or os.getenv("GITHUB_TOKEN")

        if not token:
            return ToolResult.error_result("GitHub token required")

        try:
            g = Github(token)
            search_query = params.query
            if params.repo:
                search_query += f" repo:{params.repo}"
            if params.language:
                search_query += f" language:{params.language}"

            results = g.search_code(search_query)
            output = f"# Search Results for: {params.query}\n\n"
            
            for i, item in enumerate(list(results)[:params.max_results], 1):
                output += f"{i}. **{item.name}** in {item.repository.full_name}\n"
                output += f"   Path: {item.path}\n"
                output += f"   URL: {item.html_url}\n\n"

            return ToolResult.success_result(output=output)

        except Exception as e:
            return ToolResult.error_result(f"Error: {str(e)}")

    def is_mutating(self, params: dict[str, Any]) -> bool:
        return False
