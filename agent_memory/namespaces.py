"""Namespaces from git remotes. A remote on GitHub, or on one of the configured forge hosts,
becomes a repository namespace under its own owner. A configured owner narrows that host to
the owner's repositories; any other remote is refused rather than mapped."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SCP_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^:]+)$")


def normalize_remote(url: str, github_owner: str, forge_owner: str, forge_hosts: tuple[str, ...]) -> str | None:
    """`git@github.com:octo/tools.git` and its https/ssh forms -> github/octo/tools;
    `ssh://forge.example/octo/tools.git`, with forge.example in `forge_hosts` -> gitea/octo/tools.
    An empty owner accepts every owner on that host. None when the remote is not one of these."""
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    else:
        m = SCP_RE.match(url)
        if not m:
            return None
        host, path = m.group("host"), m.group("path")
    host = host.lower()
    path = path.strip("/")
    path = path.split("?")[0].split("#")[0]
    if path.endswith(".git"):
        path = path[:-4]
    segments = [s for s in path.split("/") if s]
    if len(segments) != 2:
        return None
    owner, repo = segments
    if not re.fullmatch(r"[\w.-]+", repo) or not re.fullmatch(r"[\w.-]+", owner):
        return None
    if host in ("github.com", "www.github.com"):
        if not github_owner:
            return f"github/{owner}/{repo}"
        return f"github/{github_owner}/{repo}" if owner.lower() == github_owner.lower() else None
    if host in forge_hosts:
        if not forge_owner:
            return f"gitea/{owner}/{repo}"
        return f"gitea/{forge_owner}/{repo}" if owner.lower() == forge_owner.lower() else None
    return None


MAX_NAMESPACE = 160  # GitHub's owner (39) and repository (100) limits fit


def valid_namespace(ns: str) -> bool:
    if not isinstance(ns, str) or len(ns) > MAX_NAMESPACE:
        return False
    return bool(re.fullmatch(r"(global|github/[\w.-]+/[\w.-]+|gitea/[\w.-]+/[\w.-]+|projects/[\w.-]+|topics/[\w.-]+|local/[\w.-]+)", ns))


def is_repository(ns: str) -> bool:
    return bool(re.fullmatch(r"(github|gitea)/[\w.-]+/[\w.-]+", ns or ""))


def scopes_for(namespace: str, project_links: dict[str, list[str]], topics: list[str]) -> list[str]:
    """Retrieval order: the namespace, its linked projects, the topics, global."""
    order = [namespace]
    for project, members in project_links.items():
        if namespace in members and f"projects/{project}" not in order:
            order.append(f"projects/{project}")
    order += [f"topics/{t}" for t in topics if f"topics/{t}" not in order]
    if "global" not in order:
        order.append("global")
    return order
