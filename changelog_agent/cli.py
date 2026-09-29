import argparse
import os
import sys

from . import __version__
from .backends import polish
from .gitlog import clean, contributors, group, latest_tag, read_commits
from .render import render, to_json


def build(repo, rev_from=None, rev_to="HEAD", version="Unreleased", backend="auto",
          repo_url="", audience="end users", tone="clear, confident, friendly", fmt="markdown",
          use_cache=True):
    """Library entry point. Returns (output, backend_used, n_commits, rev_from, draft)."""
    rev_from = rev_from or latest_tag(repo)
    commits = clean(read_commits(repo, rev_from, rev_to))
    grouped, people = group(commits), contributors(commits)
    if fmt == "json":
        return to_json(version, grouped, people, len(commits)), "offline", len(commits), rev_from, ""
    draft = render(version, grouped, repo_url=repo_url.rstrip("/"), rev_from=rev_from,
                   rev_to=rev_to, people=people, total=len(commits))
    md, used = polish(draft, backend, audience, tone, use_cache)
    return md, used, len(commits), rev_from, draft


def main(argv=None):
    p = argparse.ArgumentParser(prog="changelog-agent",
                                description="Turn git history into polished release notes.")
    p.add_argument("--repo", default=".")
    p.add_argument("--from", dest="rev_from", help="start revision (default: latest tag)")
    p.add_argument("--to", dest="rev_to", default="HEAD")
    p.add_argument("--version", dest="ver", default="Unreleased")
    p.add_argument("--repo-url", default="", help="e.g. https://github.com/org/repo (adds links)")
    p.add_argument("--audience", default="end users", help='e.g. "end users", "developers", "enterprise admins"')
    p.add_argument("--tone", default="clear, confident, friendly")
    p.add_argument("--format", default="markdown", choices=["markdown", "json"])
    p.add_argument("--backend", default="auto", choices=["auto", "anthropic", "openai", "claude-cli", "none"])
    p.add_argument("--out", help="write to this file (prepends if it exists)")
    p.add_argument("--no-cache", action="store_true", help="always call the LLM")
    p.add_argument("--print-backend", action="store_true")
    p.add_argument("-V", action="version", version=__version__)
    a = p.parse_args(argv)

    md, used, n, _, _ = build(a.repo, a.rev_from, a.rev_to, a.ver, a.backend, a.repo_url,
                              a.audience, a.tone, a.format, not a.no_cache)
    if a.print_backend or a.out:
        print(f"[changelog-agent] {n} commits, backend={used}", file=sys.stderr)

    if a.out and a.format == "markdown":
        old = open(a.out).read() if os.path.exists(a.out) else ""
        head = "# Changelog\n\n"
        body = old[len(head):] if old.startswith(head) else old
        with open(a.out, "w") as f:
            f.write(head + md.strip() + "\n\n" + body)
    elif a.out:
        open(a.out, "w").write(md + "\n")
    else:
        print(md)


if __name__ == "__main__":
    main()
