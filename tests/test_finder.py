"""Tests for the transcript text finder, on synthetic transcripts in a temporary CLAUDE_CONFIG_DIR."""
import json
import os
import textwrap
import unittest
from unittest import mock

from agent_resurrect import finder
from tests.helpers import temp_home

TEXT = 'He said "deploy the uat2 worker first" and then\nwe merged'


def write_session(projects, folder, session_id, records):
    """records: list of dicts (one JSON line each) or raw strings (written as-is, for corrupt lines)."""
    path = projects / folder / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Claude Code writes UTF-8 without escaping non-ASCII characters, so the fixtures do too.
    lines = (r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def user(text, cwd="/Users/me/proj", sid="s1", mode=None):
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": cwd, "sessionId": sid,
            "timestamp": "2026-10-10T10:00:00Z", "permissionMode": mode}


def assistant(text, sid="s1"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "sessionId": sid}


def tool_result(text, sid="s1"):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}, "sessionId": sid}


def set_mtime(path, epoch):
    os.utime(path, (epoch, epoch))


class FinderTestCase(unittest.TestCase):
    def setUp(self):
        self._home = temp_home()
        home = self._home.__enter__()
        self.addCleanup(self._home.__exit__, None, None, None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / "claude-config")
        self.projects = home / "claude-config" / "projects"
        self.projects.mkdir(parents=True)

    def ids(self, candidates):
        return [c.session_id for c in candidates]


class FinderSearchTest(FinderTestCase):
    def test_finds_multiline_paste_with_quotes(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user("start"), assistant(TEXT)])
        write_session(self.projects, "-Users-me-proj", "s2", [user("deploy something else entirely", sid="s2")])
        # The raw multi-line paste is not in the file: the transcript holds it JSON-escaped.
        self.assertNotIn(TEXT, (self.projects / "-Users-me-proj" / "s1.jsonl").read_text())
        found = finder.search(TEXT, self.projects)
        self.assertEqual(self.ids(found)[0], "s1")
        self.assertNotIn("s2", self.ids(found))
        self.assertEqual(found[0].transcript_path, str(self.projects / "-Users-me-proj" / "s1.jsonl"))

    def test_short_quoted_text_is_searched_json_escaped(self):
        # No plain run is 12+ characters, so the whole text becomes the probe, quotes and all.
        write_session(self.projects, "-Users-me-proj", "s1", [user('say "hi" now')])
        self.assertEqual(finder.build_probes('say "hi" now'), ['say "hi" now'])
        self.assertEqual(self.ids(finder.search('say "hi" now', self.projects)), ["s1"])

    def test_wrapped_and_markdown_stripped_paste(self):
        original = ("the `cloudbuild_worker_uat2.yaml` file deploys to UAT's agent-worker service, "
                    "which is wrong for this rollout plan entirely")
        write_session(self.projects, "-Users-me-proj", "s1", [user("check the deploy"), assistant(original)])
        write_session(self.projects, "-Users-me-proj", "s2", [user("an unrelated rollout plan", sid="s2")])
        lines = textwrap.wrap(original.replace("`", ""), 30)
        paste = "\n".join(("⏺ " if i == 0 else "  ") + line for i, line in enumerate(lines))
        found = finder.search(paste, self.projects)
        self.assertEqual(found[0].session_id, "s1")
        self.assertGreaterEqual(found[0].hits, 2)
        self.assertEqual(found[0].total_probes, len(finder.build_probes(paste)))
        self.assertEqual(found[0].roles, {"Claude": 1})

    def test_non_ascii_text_matches_unescaped_utf8(self):
        text = "the café worker’s naïve rollout plan"  # under 40 characters: the whole run is the probe
        self.assertEqual(finder.build_probes(text), [text])
        write_session(self.projects, "-Users-me-proj", "s1", [user(text)])
        self.assertEqual(self.ids(finder.search(text, self.projects)), ["s1"])

    def test_excludes_current_session(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user(TEXT)])
        write_session(self.projects, "-Users-me-proj", "s2", [user(TEXT, sid="s2")])
        self.assertEqual(self.ids(finder.search(TEXT, self.projects, exclude="s1")), ["s2"])

    def test_ranks_conversation_over_tool_output(self):
        s1 = write_session(self.projects, "-Users-me-proj", "s1", [
            user("grep the logs"), tool_result(TEXT), {"type": "last-prompt", "lastPrompt": TEXT}])
        s2 = write_session(self.projects, "-Users-me-proj", "s2", [user(TEXT, sid="s2")])
        set_mtime(s1, 1791800000)
        set_mtime(s2, 1791700000)
        found = finder.search(TEXT, self.projects)
        self.assertEqual(self.ids(found), ["s2", "s1"])
        self.assertEqual(found[0].roles, {"you": 1})
        self.assertEqual(found[1].roles, {"tool output": 1})  # the echoed last-prompt is not counted

    def test_more_coverage_beats_recency(self):
        long_text = TEXT + "\nthen the payments service rolled back cleanly overnight"
        s1 = write_session(self.projects, "-Users-me-proj", "s1", [user(long_text)])
        s2 = write_session(self.projects, "-Users-me-proj", "s2", [user(TEXT, sid="s2")])
        set_mtime(s1, 1791700000)
        set_mtime(s2, 1791800000)
        found = finder.search(long_text, self.projects)
        self.assertEqual(self.ids(found), ["s1", "s2"])
        self.assertEqual((found[0].hits, found[1].hits), (2, 1))

    def test_limit_and_no_match(self):
        for i in range(4):
            write_session(self.projects, "-Users-me-proj", f"s{i}", [user(TEXT, sid=f"s{i}")])
        self.assertEqual(len(finder.search(TEXT, self.projects, limit=2)), 2)
        self.assertEqual(finder.search("nothing in any transcript mentions this", self.projects), [])

    def test_missing_projects_directory_finds_nothing(self):
        self.assertEqual(finder.search(TEXT, self.projects / "absent"), [])
        self.assertIsNone(finder.find_transcript("s1", self.projects / "absent"))

    def test_nested_logs_are_not_searched(self):
        nested = self.projects / "-Users-me-proj" / "s1" / "subagents"
        write_session(nested.parent.parent, "s1/subagents", "agent-1", [user(TEXT)])
        self.assertTrue((nested / "agent-1.jsonl").exists())
        self.assertEqual(finder.search(TEXT, self.projects), [])

    def test_too_short_text(self):
        with self.assertRaises(ValueError) as caught:
            finder.build_probes("ok")
        self.assertIn("too short", str(caught.exception))
        with self.assertRaises(ValueError):
            finder.build_probes("   \n  ")
        with self.assertRaises(ValueError):
            finder.search("ok", self.projects)

    def test_probes_are_capped_and_at_most_40_characters(self):
        text = "\n".join(f"alpha{i} bravo{i} charlie{i} delta{i} echo{i} foxtrot{i} golf{i}" for i in range(30))
        probes = finder.build_probes(text)
        self.assertEqual(len(probes), 10)
        self.assertTrue(all(len(p) <= finder.PROBE_WIDTH for p in probes))
        self.assertEqual(len(finder.build_probes(text, max_probes=3)), 3)

    def test_parallel_scan_and_sequential_fallback_agree(self):
        for i in range(3):
            write_session(self.projects, f"-Users-me-p{i}", f"s{i}", [user(TEXT, sid=f"s{i}")])
        with mock.patch.object(finder, "_executor") as unused:
            sequential = self.ids(finder.search(TEXT, self.projects))
        unused.assert_not_called()  # a handful of files is scanned in-process
        self.assertEqual(sorted(sequential), ["s0", "s1", "s2"])
        with mock.patch.object(finder, "PARALLEL_MIN_FILES", 0), mock.patch.object(finder.logs, "log") as log:
            with mock.patch.object(finder, "_executor", wraps=finder._executor) as pool:
                self.assertEqual(self.ids(finder.search(TEXT, self.projects)), sequential)
            pool.assert_called_once_with(1)  # 3 files fit one chunk, so one worker
            log.assert_not_called()  # the pool really ran; no silent fallback
            with mock.patch.object(finder, "_executor", side_effect=OSError("no semaphores")):
                self.assertEqual(self.ids(finder.search(TEXT, self.projects)), sequential)
            self.assertIn("scanning sequentially", log.call_args[0][0])


class TranscriptMetaTest(FinderTestCase):
    def test_cwd_trusted_only_when_it_encodes_to_folder(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user(TEXT)])
        write_session(self.projects, "-Users-me-other", "s2", [user(TEXT, sid="s2")])
        by_id = {c.session_id: c for c in finder.search(TEXT, self.projects)}
        self.assertEqual((by_id["s1"].cwd, by_id["s1"].cwd_trusted), ("/Users/me/proj", True))
        self.assertEqual((by_id["s2"].cwd, by_id["s2"].cwd_trusted), ("/Users/me/proj", False))
        meta = finder.transcript_meta(self.projects / "-Users-me-other" / "s2.jsonl")
        self.assertEqual((meta.cwd, meta.cwd_trusted), ("/Users/me/proj", False))

    def test_trusted_cwd_is_preferred_over_an_earlier_one(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            {"type": "system", "cwd": "/Users/me/elsewhere", "timestamp": "2026-10-10T09:00:00Z"},
            user("hello there")])
        meta = finder.transcript_meta(path)
        self.assertEqual((meta.cwd, meta.cwd_trusted), ("/Users/me/proj", True))
        self.assertEqual(meta.info.started, "2026-10-10T09:00:00Z")

    def test_last_permission_mode(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            user("first", mode="default"), assistant("ok"),
            {"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": "s1"},
            user("second", mode="bypassPermissions"), assistant("done"),
            user("third"),  # permissionMode null: not a mode change
            tool_result('{"permissionMode": "plan"}')])  # escaped inside content: not a record key
        self.assertEqual(finder.last_permission_mode(path), "bypassPermissions")
        self.assertEqual(finder.transcript_meta(path).permission_mode, "bypassPermissions")

    def test_last_permission_mode_reads_only_the_tail(self):
        filler = [assistant("x" * 1000) for _ in range(300)]  # about 300 KB, more than the 256 KB tail
        path = write_session(self.projects, "-Users-me-proj", "s1", [user("a", mode="bypassPermissions")] + filler)
        self.assertIsNone(finder.last_permission_mode(path))
        self.assertIsNone(finder.last_permission_mode(self.projects / "missing.jsonl"))
        # transcript_meta reads the whole file anyway, so it still knows the mode.
        self.assertEqual(finder.transcript_meta(path).permission_mode, "bypassPermissions")

    def test_title_and_first_prompt(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            {"type": "ai-title", "aiTitle": "Old title"},
            user("hello   there\n  friend"), assistant("hi"),
            {"type": "ai-title", "aiTitle": "Fix login"}])
        meta = finder.transcript_meta(path)
        self.assertEqual(meta.info.title, "Fix login")
        self.assertEqual(meta.info.first_prompt, "hello there friend")
        self.assertEqual(meta.info.started, "2026-10-10T10:00:00Z")
        self.assertAlmostEqual(meta.info.last_active, path.stat().st_mtime)

    def test_first_prompt_skips_tool_output_and_injected_context(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            dict(user("<system-reminder>context</system-reminder>"), isMeta=True),
            tool_result("ls output"), user("x" * 300)])
        self.assertEqual(finder.transcript_meta(path).info.first_prompt, "x" * 100)

    def test_corrupt_and_unexpected_lines_are_skipped(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            "{not json", "[1, 2]", '"a string"', {"type": "user", "message": "not a dict"}])
        with open(path, "ab") as fh:
            fh.write(b'{"type": "user", "bad utf-8": "\xff\xfe"}\n')
        prompt = {"type": "user", "cwd": "/Users/me/proj",
                  "message": {"content": ["not a block", {"type": "text", "text": "hello there"}]}}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(prompt) + "\n" + json.dumps({"type": "ai-title", "aiTitle": "Survives garbage"}) + "\n")
        meta = finder.transcript_meta(path)
        self.assertEqual((meta.info.title, meta.info.first_prompt), ("Survives garbage", "hello there"))
        self.assertTrue(meta.cwd_trusted)

    def test_find_transcript(self):
        path = write_session(self.projects, "-Users-me-proj", "83fc435f-73ea-41b2-b2e3-076ee2c3f7b0", [user("a")])
        self.assertEqual(finder.find_transcript("83fc435f-73ea-41b2-b2e3-076ee2c3f7b0", self.projects), path)
        self.assertIsNone(finder.find_transcript("nope", self.projects))
        for unsafe in ["", "*", "../-Users-me-proj/83fc435f-73ea-41b2-b2e3-076ee2c3f7b0", "a/b", None]:
            self.assertIsNone(finder.find_transcript(unsafe, self.projects), unsafe)


if __name__ == "__main__":
    unittest.main()
