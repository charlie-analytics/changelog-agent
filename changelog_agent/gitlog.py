"""Read commits from git, classify them (Conventional Commits aware) and clean them up."""
import difflib
import re
import subprocess
from dataclasses import dataclass, field

SEP, REC = "\x1f", "\x1e"
CC = re.compile(r"^(?P<type>\w+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")
PR_TAIL = re.compile(r"\s*\(#(\d+)\)\s*$")
ISSUE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.I)
NOISE = re.compile(r"^(wip\b|fixup!|squash!|merge (branch|pull request|remote)|bump version|release\b)", re.I)
DEPS_BUMP = re.compile(r"^(update|bump|upgrade|pin|lock file)\b.*\b(dependenc|deps?\b|to v?\d)|^update (all )?(non-major )?dependencies|^update (compiler|dependency)", re.I)
INLINE_REF = re.compile(r"\s*\((?:fix(?:es)?|close[sd]?|resolve[sd]?)?\s*#\d+\)", re.I)
BOT = re.compile(r"\[bot\]$|^(renovate|dependabot|github-actions)", re.I)
REVERT = re.compile(r'^Revert "(?P<orig>.+)"$')

SECTIONS = [
    ("breaking", "Breaking Changes"),
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("other", "Other Changes"),
]
SKIP_TYPES = {"chore", "ci", "build", "test", "style"}


@dataclass
class Commit:
    sha: str
    author: str
    subject: str
    body: str = ""
    type: str = "other"
    scope: str = ""
    desc: str = ""
    breaking: bool = False
    migration: str = ""
    pr: int = 0
    issues: list = field(default_factory=list)
    extra_shas: list = field(default_factory=list)  # merged duplicates


def classify(sha, author, subject, body=""):
    c = Commit(sha=sha, author=author, subject=subject, body=body, desc=subject)
    pr = PR_TAIL.search(subject)
    if pr:
        c.pr = int(pr.group(1))
        subject = subject[: pr.start()]
    m = CC.match(subject)
    c.desc = subject
    if m:
        c.type = m["type"].lower()
        c.scope = m["scope"] or ""
        c.desc = INLINE_REF.sub("", m["desc"]).strip().rstrip("!. ")
        if c.scope.lower() == "deps" and DEPS_BUMP.search(c.desc):
            c.type = "chore"  # routine dependency bump: not user-facing
        c.breaking = bool(m["bang"])
    mb = re.search(r"BREAKING[ -]CHANGE:?\s*(.+)", body, re.S)
    if mb:
        c.breaking = True
        c.migration = " ".join(mb.group(1).split())
    c.issues = sorted({int(n) for n in ISSUE.findall(subject + "\n" + body)})
    if c.type not in {t for t, _ in SECTIONS} | SKIP_TYPES:
        c.type = "other"
    return c


def read_commits(repo, rev_from=None, rev_to="HEAD"):
    rng = f"{rev_from}..{rev_to}" if rev_from else rev_to
    fmt = f"%h{SEP}%an{SEP}%s{SEP}%b{REC}"
    out = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", f"--pretty=format:{fmt}", rng],
        capture_output=True, text=True, check=True,
    ).stdout
    commits = []
    for rec in out.split(REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, author, subject, body = (rec.split(SEP) + ["", "", "", ""])[:4]
        commits.append(classify(sha, author, subject, body))
    return commits


def drop_reverts_and_noise(commits):
    """Remove WIP/fixup commits, and any commit that was reverted (plus the revert itself)."""
    subjects = {c.subject: c for c in commits}
    dead = set()
    for c in commits:
        m = REVERT.match(c.subject)
        if m and m["orig"] in subjects:
            dead.update({c.sha, subjects[m["orig"]].sha})
    return [c for c in commits if c.sha not in dead and not NOISE.match(c.subject)]


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def merge_duplicates(commits, threshold=0.92):
    """Merge near-identical entries (same type+scope, very similar text).

    Never merges two entries that carry different PR numbers: they are distinct changes.
    """
    kept = []
    for c in commits:
        for k in kept:
            if (k.type, k.scope, k.breaking) == (c.type, c.scope, c.breaking) and \
               not (k.pr and c.pr and k.pr != c.pr) and \
               difflib.SequenceMatcher(None, _norm(k.desc), _norm(c.desc)).ratio() >= threshold:
                k.extra_shas.append(c.sha)
                k.issues = sorted(set(k.issues) | set(c.issues))
                k.pr = k.pr or c.pr
                break
        else:
            kept.append(c)
    return kept


def clean(commits):
    return merge_duplicates(drop_reverts_and_noise(commits))


def group(commits):
    """Return {section_key: [Commit]}; chore/ci/test noise is dropped. Sorted by impact within section."""
    g = {k: [] for k, _ in SECTIONS}
    for c in commits:
        if c.type in SKIP_TYPES and not c.breaking:
            continue
        g["breaking" if c.breaking else c.type].append(c)
    return {k: sorted(v, key=lambda c: (c.scope == "", c.scope)) for k, v in g.items() if v}


def contributors(commits):
    seen = []
    for c in commits:
        if c.author not in seen and not BOT.search(c.author):
            seen.append(c.author)
    return seen


def latest_tag(repo):
    r = subprocess.run(["git", "-C", repo, "describe", "--tags", "--abbrev=0"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None
