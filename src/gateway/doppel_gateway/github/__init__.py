"""GitHub as the doppelwerk App: installation tokens, reads for intake, and every write."""

from doppel_gateway.github.client import API, AppAuth, GitHubClient, GitHubError, RateLimited
from doppel_gateway.github.rest import RestGitHub, RestReader

__all__ = ["API", "AppAuth", "GitHubClient", "GitHubError", "RateLimited", "RestGitHub", "RestReader"]
