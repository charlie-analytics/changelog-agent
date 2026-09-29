"""LLM backends + guardrails. All optional: the agent degrades to deterministic output."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request

PROMPT = """You are a senior release-notes editor at a top developer-tools company.
Rewrite the DRAFT below into release notes that customers actually want to read.

Audience: {audience}. Tone: {tone}.

Editorial rules:
1. Lead with a one-sentence "**Highlights:**" line under the version heading naming the 1-3 most important changes.
2. Write each bullet as a benefit the reader feels, in active voice and consistent tense
   ("Export reports as PDF", not "Added PDF export functionality"). One idea per bullet, under ~20 words.
3. Cut internal jargon, file/function names and implementation detail unless the audience is developers.
4. Order bullets by user impact, not alphabetically. Merge duplicates and near-duplicates.
5. Breaking changes come first. Say exactly what breaks and keep every "Migration:" line intact and actionable.
6. Keep the heading structure (## version, ### sections) and every section that has content.
7. Preserve ALL references exactly as written: commit hashes, #PR/#issue numbers, links, the Contributors and Full changelog lines.
   When you merge or group several bullets, keep EVERY one of their commit hashes on the merged bullet (e.g. `abc1234` `def5678`).
   If commits are not categorised (everything under "Other Changes"), you may re-group them into sensible sections such as Features, Fixes, Docs.
8. NEVER invent features, numbers, dates, hashes or PR numbers. If the draft is vague, stay vague.
9. Output only the Markdown, no preamble or commentary.

DRAFT:
{draft}
"""

HASH = re.compile(r"`([0-9a-f]{7,40})`")
NUM = re.compile(r"#(\d+)")


def _anthropic(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({
        "model": os.environ.get("CHANGELOG_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages", body,
        {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["content"][0]["text"].strip()


def _openai_compat(prompt):
    """Any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, LiteLLM, a VPS gateway...)."""
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    body = json.dumps({
        "model": os.environ.get("CHANGELOG_MODEL", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"content-type": "application/json"}
    if os.environ.get("OPENAI_API_KEY"):
        headers["authorization"] = "Bearer " + os.environ["OPENAI_API_KEY"]
    req = urllib.request.Request(base + "/chat/completions", body, headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def _claude_cli(prompt):
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not r.stdout.strip().startswith("## "):
        raise RuntimeError((r.stderr or r.stdout).strip() or "claude CLI returned nothing")
    return r.stdout.strip()


def available():
    names = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        names.append("anthropic")
    if os.environ.get("OPENAI_BASE_URL"):
        names.append("openai")
    if shutil.which("claude"):
        names.append("claude-cli")
    return names


def _cache_path(key):
    d = os.environ.get("CHANGELOG_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "changelog-agent")
    return os.path.join(d, key + ".json")


def _cache_get(key):
    try:
        with open(_cache_path(key)) as f:
            return json.load(f)["markdown"]
    except (OSError, ValueError, KeyError):
        return None


def _cache_put(key, md):
    try:
        os.makedirs(os.path.dirname(_cache_path(key)), exist_ok=True)
        with open(_cache_path(key), "w") as f:
            json.dump({"markdown": md}, f)
    except OSError:
        pass


def validate(draft, out):
    """Guardrail: return a list of problems if `out` invents or drops facts from `draft`."""
    problems = []
    if not out.lstrip().startswith("## "):
        problems.append("must start with the '## version' heading")
    draft_hashes = set(HASH.findall(draft))
    for h in set(HASH.findall(out)) - draft_hashes:
        problems.append(f"invented commit hash {h}")
    for n in set(NUM.findall(out)) - set(NUM.findall(draft)):
        problems.append(f"invented reference #{n}")
    if "### Breaking Changes" in draft and "### Breaking Changes" not in out:
        problems.append("dropped the Breaking Changes section")
    for m in re.findall(r"\*\*Migration:\*\* (.+)", draft):
        if m[:25] not in out:
            problems.append("dropped a Migration note")
    kept = set(HASH.findall(out))
    missing = sorted(draft_hashes - kept)
    if draft_hashes and len(kept & draft_hashes) < len(draft_hashes) * 0.5:
        problems.append(f"dropped {len(missing)} of {len(draft_hashes)} commit hashes (e.g. {', '.join(missing[:4])}); "
                        "every hash from the draft must appear in some bullet, merged bullets keep all their hashes")
    return problems


def polish(draft, backend="auto", audience="end users", tone="clear, confident, friendly", use_cache=True):
    """Return (markdown, backend_used). Validates output; retries once; falls back to the draft.

    Results are cached on disk keyed by (draft, audience, tone, model), so the LLM only runs the
    first time a given input is seen. A cache hit reports backend "cache".
    """
    if backend == "none":
        return draft, "offline"
    order = available() if backend == "auto" else [backend]
    if not order:
        return draft, "offline"
    model = os.environ.get("CHANGELOG_MODEL", "")
    key = hashlib.sha256(json.dumps([draft, audience, tone, model, PROMPT]).encode()).hexdigest()[:32]
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return hit, "cache"
    for name in order:
        fn = {"anthropic": _anthropic, "claude-cli": _claude_cli, "openai": _openai_compat}[name]
        prompt = PROMPT.format(draft=draft, audience=audience, tone=tone)
        for attempt in (1, 2):
            try:
                out = fn(prompt)
            except Exception as e:  # noqa: BLE001 - never crash a release on the LLM
                print(f"[changelog-agent] {name} failed: {e}; falling back", flush=True)
                break
            problems = validate(draft, out)
            if not problems:
                if use_cache:
                    _cache_put(key, out)
                return out, name
            print(f"[changelog-agent] {name} attempt {attempt} rejected: {'; '.join(problems)}", flush=True)
            prompt += "\n\nYour previous answer was rejected for: " + "; ".join(problems) + ". Fix these and try again."
    return draft, "offline"
"""LLM backends + guardrails. All optional: the agent degrades to deterministic output."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request

PROMPT = """You are a senior release-notes editor at a top developer-tools company.
Rewrite the DRAFT below into release notes that customers actually want to read.

Audience: {audience}. Tone: {tone}.

Editorial rules:
1. Lead with a one-sentence "**Highlights:**" line under the version heading naming the 1-3 most important changes.
2. Write each bullet as a benefit the reader feels, in active voice and consistent tense
   ("Export reports as PDF", not "Added PDF export functionality"). One idea per bullet, under ~20 words.
3. Cut internal jargon, file/function names and implementation detail unless the audience is developers.
4. Order bullets by user impact, not alphabetically. Merge duplicates and near-duplicates.
5. Breaking changes come first. Say exactly what breaks and keep every "Migration:" line intact and actionable.
6. Keep the heading structure (## version, ### sections) and every section that has content.
7. Preserve ALL references exactly as written: commit hashes, #PR/#issue numbers, links, the Contributors and Full changelog lines.
8. NEVER invent features, numbers, dates, hashes or PR numbers. If the draft is vague, stay vague.
9. Output only the Markdown, no preamble or commentary.

DRAFT:
{draft}
"""

HASH = re.compile(r"`([0-9a-f]{7,40})`")
NUM = re.compile(r"#(\d+)")


def _anthropic(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({
        "model": os.environ.get("CHANGELOG_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages", body,
        {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["content"][0]["text"].strip()


def _openai_compat(prompt):
    """Any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, LiteLLM, a VPS gateway...)."""
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    body = json.dumps({
        "model": os.environ.get("CHANGELOG_MODEL", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"content-type": "application/json"}
    if os.environ.get("OPENAI_API_KEY"):
        headers["authorization"] = "Bearer " + os.environ["OPENAI_API_KEY"]
    req = urllib.request.Request(base + "/chat/completions", body, headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def _claude_cli(prompt):
    r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not r.stdout.strip().startswith("## "):
        raise RuntimeError((r.stderr or r.stdout).strip() or "claude CLI returned nothing")
    return r.stdout.strip()


def available():
    names = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        names.append("anthropic")
    if os.environ.get("OPENAI_BASE_URL"):
        names.append("openai")
    if shutil.which("claude"):
        names.append("claude-cli")
    return names


def _cache_path(key):
    d = os.environ.get("CHANGELOG_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "changelog-agent")
    return os.path.join(d, key + ".json")


def _cache_get(key):
    try:
        with open(_cache_path(key)) as f:
            return json.load(f)["markdown"]
    except (OSError, ValueError, KeyError):
        return None


def _cache_put(key, md):
    try:
        os.makedirs(os.path.dirname(_cache_path(key)), exist_ok=True)
        with open(_cache_path(key), "w") as f:
            json.dump({"markdown": md}, f)
    except OSError:
        pass


def validate(draft, out):
    """Guardrail: return a list of problems if `out` invents or drops facts from `draft`."""
    problems = []
    if not out.lstrip().startswith("## "):
        problems.append("must start with the '## version' heading")
    draft_hashes = set(HASH.findall(draft))
    for h in set(HASH.findall(out)) - draft_hashes:
        problems.append(f"invented commit hash {h}")
    for n in set(NUM.findall(out)) - set(NUM.findall(draft)):
        problems.append(f"invented reference #{n}")
    if "### Breaking Changes" in draft and "### Breaking Changes" not in out:
        problems.append("dropped the Breaking Changes section")
    for m in re.findall(r"\*\*Migration:\*\* (.+)", draft):
        if m[:25] not in out:
            problems.append("dropped a Migration note")
    kept = set(HASH.findall(out))
    if draft_hashes and len(kept) < len(draft_hashes) * 0.5:
        problems.append("dropped more than half of the changes")
    return problems


def polish(draft, backend="auto", audience="end users", tone="clear, confident, friendly", use_cache=True):
    """Return (markdown, backend_used). Validates output; retries once; falls back to the draft.

    Results are cached on disk keyed by (draft, audience, tone, model), so the LLM only runs the
    first time a given input is seen. A cache hit reports backend "cache".
    """
    if backend == "none":
        return draft, "offline"
    order = available() if backend == "auto" else [backend]
    if not order:
        return draft, "offline"
    model = os.environ.get("CHANGELOG_MODEL", "")
    key = hashlib.sha256(json.dumps([draft, audience, tone, model, PROMPT]).encode()).hexdigest()[:32]
    if use_cache:
        hit = _cache_get(key)
        if hit:
            return hit, "cache"
    for name in order:
        fn = {"anthropic": _anthropic, "claude-cli": _claude_cli, "openai": _openai_compat}[name]
        prompt = PROMPT.format(draft=draft, audience=audience, tone=tone)
        for attempt in (1, 2):
            try:
                out = fn(prompt)
            except Exception as e:  # noqa: BLE001 - never crash a release on the LLM
                print(f"[changelog-agent] {name} failed: {e}; falling back", flush=True)
                break
            problems = validate(draft, out)
            if not problems:
                if use_cache:
                    _cache_put(key, out)
                return out, name
            print(f"[changelog-agent] {name} attempt {attempt} rejected: {'; '.join(problems)}", flush=True)
            prompt += "\n\nYour previous answer was rejected for: " + "; ".join(problems) + ". Fix these and try again."
    return draft, "offline"
