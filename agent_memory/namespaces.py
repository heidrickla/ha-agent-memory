"""Namespaces from git remotes. A remote on GitHub under the configured owner or on the
personal forge under its owner becomes a repository namespace; anything else is refused
rather than mapped, so a fork or another owner never inherits the owner's memory."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SCP_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^:]+)$")


def normalize_remote(url: str, github_owner: str, forge_owner: str, forge_hosts: tuple[str, ...]) -> str | None:
    """`git@github.com:heidrickla/homelab.git` and its https/ssh forms -> github/heidrickla/homelab;
    `ssh://forge.example/example/ha-loops.git` -> gitea/example/ha-loops. None when it is not ours."""
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
    if host in ("github.com", "www.github.com") and owner.lower() == github_owner.lower():
        return f"github/{github_owner}/{repo}"
    if host in forge_hosts and owner.lower() == forge_owner.lower():
        return f"gitea/{forge_owner}/{repo}"
    return None


def valid_namespace(ns: str) -> bool:
    return bool(re.fullmatch(r"(global|github/[\w.-]+/[\w.-]+|gitea/[\w.-]+/[\w.-]+|projects/[\w.-]+|topics/[\w.-]+|local/[\w.-]+)", ns or ""))


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
