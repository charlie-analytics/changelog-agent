"""Deterministic (no-LLM) markdown / JSON rendering."""
import datetime
import json

from .gitlog import SECTIONS


def _refs(c, repo_url):
    def link(text, path):
        return f"[{text}]({repo_url}/{path})" if repo_url else text

    parts = []
    if c.pr:
        parts.append(link(f"#{c.pr}", f"pull/{c.pr}"))
    parts += [link(f"#{n}", f"issues/{n}") for n in c.issues if n != c.pr]
    shas = [c.sha] + c.extra_shas
    parts += [f"`{s}`" if not repo_url else f"[`{s}`]({repo_url}/commit/{s})" for s in shas]
    return " ".join(parts)


def render(version, grouped, date=None, repo_url="", rev_from=None, rev_to="HEAD",
           people=None, total=0):
    date = date or datetime.date.today().isoformat()
    lines = [f"## {version} - {date}", ""]
    titles = dict(SECTIONS)
    for key, items in grouped.items():
        lines.append(f"### {titles[key]}")
        for c in items:
            scope = f"**{c.scope}:** " if c.scope else ""
            desc = c.desc[0].upper() + c.desc[1:] if c.desc else c.subject
            lines.append(f"- {scope}{desc} ({_refs(c, repo_url)})")
            if c.migration:
                lines.append(f"  - **Migration:** {c.migration}")
        lines.append("")
    if not grouped:
        lines += ["_No user-facing changes._", ""]
    if people:
        lines += [f"**Contributors:** {', '.join(people)}", ""]
    if repo_url and rev_from:
        lines += [f"**Full changelog:** [{rev_from}...{rev_to}]({repo_url}/compare/{rev_from}...{rev_to})", ""]
    return "\n".join(lines)


def to_json(version, grouped, people, total, date=None):
    date = date or datetime.date.today().isoformat()
    titles = dict(SECTIONS)
    return json.dumps({
        "version": version, "date": date, "commits": total, "contributors": people,
        "sections": [{
            "key": k, "title": titles[k],
            "items": [{"sha": c.sha, "scope": c.scope, "text": c.desc, "pr": c.pr or None,
                       "issues": c.issues, "migration": c.migration or None,
                       "merged": c.extra_shas} for c in items],
        } for k, items in grouped.items()],
    }, indent=2)
