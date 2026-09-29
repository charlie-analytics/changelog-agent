import os
import shutil
import subprocess
import tempfile
import unittest

from changelog_agent.gitlog import classify, group, read_commits
from changelog_agent.render import render


_CACHE = tempfile.mkdtemp()


def setUpModule():
    os.environ["CHANGELOG_CACHE_DIR"] = _CACHE  # never touch the real ~/.cache


def tearDownModule():
    os.environ.pop("CHANGELOG_CACHE_DIR", None)
    shutil.rmtree(_CACHE, ignore_errors=True)


class _Fresh(unittest.TestCase):
    def setUp(self):
        for f in os.listdir(_CACHE):
            os.remove(os.path.join(_CACHE, f))


def sh(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class Classify(unittest.TestCase):
    def test_conventional(self):
        c = classify("a1", "x", "feat(api): add search")
        self.assertEqual((c.type, c.scope, c.desc), ("feat", "api", "add search"))

    def test_breaking_bang_and_footer(self):
        self.assertTrue(classify("a", "x", "feat!: drop v1").breaking)
        self.assertTrue(classify("a", "x", "fix: x", "BREAKING CHANGE: y").breaking)

    def test_freeform_is_other(self):
        self.assertEqual(classify("a", "x", "Update stuff").type, "other")

    def test_noise_dropped(self):
        g = group([classify("a", "x", "chore: bump"), classify("b", "x", "fix: crash")])
        self.assertEqual(list(g), ["fix"])


class EndToEnd(unittest.TestCase):
    def test_repo_to_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            sh(d, "init", "-q")
            sh(d, "config", "user.email", "t@t")
            sh(d, "config", "user.name", "T")
            for i, m in enumerate(["feat: login", "fix(ui): button", "chore: ci"]):
                sh(d, "commit", "-q", "--allow-empty", "-m", m)
            md = render("1.0.0", group(read_commits(d)), date="2026-01-01")
        self.assertIn("## 1.0.0 - 2026-01-01", md)
        self.assertIn("### Features", md)
        self.assertIn("**ui:** Button", md)
        self.assertNotIn("ci", md.split("### Bug Fixes")[1].lower().replace("ui", ""))



class LLMBackend(_Fresh):
    """Exercise the Anthropic path against a local mock server."""

    def _serve(self, text):
        import http.server, json, threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["content-length"])
                H.req = json.loads(self.rfile.read(n))
                H.key = self.headers["x-api-key"]
                out = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
                self.send_response(200); self.send_header("content-length", str(len(out)))
                self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, H

    def test_polish_uses_llm_and_falls_back(self):
        import os
        from changelog_agent.backends import polish
        srv, H = self._serve("## 1.0.0 - x\n\nHighlights: better.\n")
        os.environ.update(ANTHROPIC_API_KEY="k", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_port}")
        try:
            md, used = polish("## 1.0.0 - x\n- a", "anthropic")
            self.assertEqual(used, "anthropic")
            self.assertIn("Highlights", md)
            self.assertEqual(H.key, "k")
            self.assertIn("- a", H.req["messages"][0]["content"])
            srv.shutdown(); srv.server_close()
            md, used = polish("## 1.0.0 - x\n- a", "anthropic", use_cache=False)  # server gone -> fallback
            self.assertEqual((used, md), ("offline", "## 1.0.0 - x\n- a"))
        finally:
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
                os.environ.pop(k, None)



class ProEditor(unittest.TestCase):
    def test_pr_issue_and_migration_parsing(self):
        c = classify("a", "x", "fix(auth): stop login loop (#42)", "Fixes #7\n\nBREAKING CHANGE: sessions are reset")
        self.assertEqual((c.pr, c.issues, c.breaking), (42, [7], True))
        self.assertEqual(c.desc, "stop login loop")
        self.assertEqual(c.migration, "sessions are reset")

    def test_reverts_and_noise_cancel(self):
        from changelog_agent.gitlog import clean
        cs = [classify("1", "x", "feat: shiny"), classify("2", "x", 'Revert "feat: shiny"'),
              classify("3", "x", "WIP stuff"), classify("4", "x", "fix: real bug")]
        self.assertEqual([c.sha for c in clean(cs)], ["4"])

    def test_duplicates_merge(self):
        from changelog_agent.gitlog import clean
        cs = [classify("1", "x", "fix(ui): button misaligned on mobile"),
              classify("2", "x", "fix(ui): button misaligned on mobile!"),
              classify("3", "x", "fix(ui): tooltip flicker")]
        out = clean(cs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].extra_shas, ["2"])

    def test_distinct_prs_never_merge(self):
        from changelog_agent.gitlog import clean
        cs = [classify("1", "x", "feat(export): add PDF export for reports (#18)"),
              classify("2", "x", "feat(export): add CSV export for reports (#15)")]
        self.assertEqual(len(clean(cs)), 2)

    def test_links_and_migration_render(self):
        cs = [classify("abc1234", "Ann", "feat(api)!: rename users (#9)", "BREAKING CHANGE: use /v2/accounts")]
        md = render("2.0.0", group(cs), date="d", repo_url="https://github.com/o/r",
                    rev_from="v1", people=["Ann"])
        self.assertIn("[#9](https://github.com/o/r/pull/9)", md)
        self.assertIn("**Migration:** use /v2/accounts", md)
        self.assertIn("compare/v1...HEAD", md)
        self.assertIn("**Contributors:** Ann", md)

    def test_json_format(self):
        import json
        from changelog_agent.render import to_json
        d = json.loads(to_json("1", group([classify("a", "x", "feat: y")]), ["x"], 1))
        self.assertEqual(d["sections"][0]["items"][0]["sha"], "a")


class Guardrails(_Fresh):
    DRAFT = ("## 1.0 - d\n\n### Breaking Changes\n- Rename (`aaaaaaa`)\n  - **Migration:** use /v2/accounts now\n\n"
             "### Features\n- Thing (#5 `bbbbbbb`)\n")

    def test_valid_passes(self):
        from changelog_agent.backends import validate
        self.assertEqual(validate(self.DRAFT, self.DRAFT), [])

    def test_invented_hash_and_pr_rejected(self):
        from changelog_agent.backends import validate
        bad = self.DRAFT + "- Extra (#99 `ccccccc`)\n"
        p = " ".join(validate(self.DRAFT, bad))
        self.assertIn("invented commit hash ccccccc", p)
        self.assertIn("invented reference #99", p)

    def test_dropped_breaking_rejected(self):
        from changelog_agent.backends import validate
        bad = "## 1.0 - d\n\n### Features\n- Thing (#5 `bbbbbbb`)\n"
        self.assertTrue(any("Breaking" in x for x in validate(self.DRAFT, bad)))

    def test_retry_then_accept_and_fallback(self):
        import changelog_agent.backends as B
        calls = []
        orig = B._anthropic
        def flaky(prompt):
            calls.append(prompt)
            return "## 1.0 - d\n- lies `ddddddd`" if len(calls) == 1 else self.DRAFT
        B._anthropic = flaky
        os_env = __import__("os").environ; os_env["ANTHROPIC_API_KEY"] = "k"
        try:
            out, used = B.polish(self.DRAFT, "anthropic")
            self.assertEqual((used, len(calls)), ("anthropic", 2))
            self.assertIn("rejected for", calls[1])
            B._anthropic = lambda p: "## 1.0 - d\n- lies `eeeeeee`"
            out, used = B.polish(self.DRAFT, "anthropic", use_cache=False)
            self.assertEqual((used, out), ("offline", self.DRAFT))
        finally:
            B._anthropic = orig
            os_env.pop("ANTHROPIC_API_KEY", None)



class CacheAndOpenAI(unittest.TestCase):
    DRAFT = "## 1.0 - d\n\n### Features\n- Thing (#5 `bbbbbbb`)\n"

    def test_second_call_hits_cache_and_skips_llm(self):
        import os, tempfile
        import changelog_agent.backends as B
        calls = []
        orig = B._anthropic
        B._anthropic = lambda p: (calls.append(1), self.DRAFT)[1]
        os.environ.update(ANTHROPIC_API_KEY="k", CHANGELOG_CACHE_DIR=tempfile.mkdtemp())
        self.addCleanup(os.environ.__setitem__, "CHANGELOG_CACHE_DIR", _CACHE)
        try:
            self.assertEqual(B.polish(self.DRAFT, "anthropic")[1], "anthropic")
            self.assertEqual(B.polish(self.DRAFT, "anthropic")[1], "cache")
            self.assertEqual(len(calls), 1)
            self.assertEqual(B.polish(self.DRAFT, "anthropic", use_cache=False)[1], "anthropic")
            self.assertEqual(len(calls), 2)
            self.assertEqual(B.polish(self.DRAFT, "anthropic", audience="developers")[1], "anthropic")
        finally:
            B._anthropic = orig
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_openai_compatible_endpoint(self):
        import http.server, json, os, tempfile, threading
        import changelog_agent.backends as B
        draft = self.DRAFT

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                H.path_seen = self.path
                H.auth = self.headers.get("authorization")
                self.rfile.read(int(self.headers["content-length"]))
                out = json.dumps({"choices": [{"message": {"content": draft}}]}).encode()
                self.send_response(200); self.send_header("content-length", str(len(out)))
                self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ.update(OPENAI_BASE_URL=f"http://127.0.0.1:{srv.server_port}/v1", OPENAI_API_KEY="sk-test",
                          CHANGELOG_CACHE_DIR=tempfile.mkdtemp())
        self.addCleanup(os.environ.__setitem__, "CHANGELOG_CACHE_DIR", _CACHE)
        try:
            md, used = B.polish(draft, "openai")
            self.assertEqual(used, "openai")
            self.assertEqual((H.path_seen, H.auth), ("/v1/chat/completions", "Bearer sk-test"))
        finally:
            srv.shutdown(); srv.server_close()
            for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
                os.environ.pop(k, None)



class HashGuardrailMessage(unittest.TestCase):
    def test_dropped_hashes_reported_with_examples(self):
        from changelog_agent.backends import validate
        draft = "## 1 - d\n" + "".join(f"- x (`{c*7}`)\n" for c in "abcdef")
        out = "## 1 - d\n- merged (`aaaaaaa`)\n"
        msg = " ".join(validate(draft, out))
        self.assertIn("dropped 5 of 6 commit hashes", msg)
        self.assertIn("bbbbbbb", msg)

    def test_merged_bullet_keeping_all_hashes_passes(self):
        from changelog_agent.backends import validate
        draft = "## 1 - d\n" + "".join(f"- x (`{c*7}`)\n" for c in "abcdef")
        out = "## 1 - d\n- all merged (" + " ".join(f"`{c*7}`" for c in "abcdef") + ")\n"
        self.assertEqual(validate(draft, out), [])

    def test_full_sha_shortened_in_compare_link(self):
        from changelog_agent.cli import build
        with tempfile.TemporaryDirectory() as d:
            sh(d, "init", "-q"); sh(d, "config", "user.email", "t@t"); sh(d, "config", "user.name", "T")
            sh(d, "commit", "-q", "--allow-empty", "-m", "feat: a"); sh(d, "commit", "-q", "--allow-empty", "-m", "feat: b")
            first = subprocess.run(["git", "-C", d, "rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True).stdout.strip()
            md = build(d, first, "HEAD", "1", "none", "https://github.com/o/r")[0]
        self.assertIn(f"compare/{first[:7]}...HEAD", md)
        self.assertNotIn(first, md)



class GroundingGuardrails(unittest.TestCase):
    DRAFT = "## 1 - d\n\n### Features\n- Add CLI (`aaaaaaa`)\n- Add UI (`bbbbbbb`)\n"

    def test_invented_breaking_and_migration_rejected(self):
        from changelog_agent.backends import validate
        out = "## 1 - d\n\n### Breaking Changes\n- Removed x\n  Migration: do y\n\n### Features\n- CLI (`aaaaaaa`)\n- UI (`bbbbbbb`)\n"
        p = " ".join(validate(self.DRAFT, out))
        self.assertIn("invented a Breaking Changes section", p)
        self.assertIn("invented a Migration note", p)

    def test_unsupported_benefit_claim_rejected(self):
        from changelog_agent.backends import validate
        out = "## 1 - d\n\n### Features\n- CLI with improved performance (`aaaaaaa`)\n- UI (`bbbbbbb`)\n"
        self.assertTrue(any("unsupported claim about 'performance'" in x for x in validate(self.DRAFT, out)))

    def test_claim_allowed_when_draft_states_it(self):
        from changelog_agent.backends import validate
        draft = "## 1 - d\n\n### Performance\n- Cache queries, faster load (`aaaaaaa`)\n"
        out = "## 1 - d\n\n### Performance\n- Queries are cached for faster loading (`aaaaaaa`)\n"
        self.assertEqual(validate(draft, out), [])

    def test_grounded_output_passes(self):
        from changelog_agent.backends import validate
        out = "## 1 - d\n\n### Features\n- New command-line interface (`aaaaaaa`)\n- New web UI (`bbbbbbb`)\n"
        self.assertEqual(validate(self.DRAFT, out), [])



class RealWorldNoise(unittest.TestCase):
    def test_dependency_bumps_are_not_bug_fixes(self):
        for subj in ["fix(deps): update dependency jszip to ^3.10.2 (#15502)",
                     "fix(deps): update compiler (#15247)",
                     "chore(deps): update all non-major dependencies (#15244)"]:
            self.assertEqual(group([classify("a", "x", subj)]), {}, subj)

    def test_real_fix_in_deps_scope_is_kept(self):
        g = group([classify("a", "x", "fix(deps): pin broken polyfill that crashed Safari")])
        self.assertIn("fix", g)

    def test_bots_excluded_from_contributors(self):
        from changelog_agent.gitlog import contributors
        cs = [classify("1", "renovate[bot]", "fix: a"), classify("2", "Ann", "fix: b"),
              classify("3", "dependabot[bot]", "fix: c")]
        self.assertEqual(contributors(cs), ["Ann"])

    def test_inline_fix_ref_stripped_but_linked(self):
        c = classify("a", "x", "fix(sfc): reuse parsed configs (fix #15478) (#15480)")
        self.assertEqual((c.desc, c.pr, c.issues), ("reuse parsed configs", 15480, [15478]))


if __name__ == "__main__":
    unittest.main()
