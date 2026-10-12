"""The machine interface contract (review 03, "Machine interface spec"): every --json path, in every exit
path, prints exactly one ASCII JSON object; error.code names the exit status; the document validates against
docs/json/<command>.schema.json. Also unit tests for views.py, which builds the shared objects.

The validator below is a deliberately small subset of JSON Schema 2020-12 (type, const, enum, pattern,
minimum, minItems, required, properties, additionalProperties, items, allOf, anyOf, if/then/else, $ref to a
sibling file or a #/$defs fragment): enough for the schemas this project publishes, with no dependency.
"""
import json
import os
import re
import time
import types
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from agent_resurrect import cli, doctor, exitcodes, jsonout, restore, views
from agent_resurrect.config import Config
from agent_resurrect.model import Candidate, SessionInfo
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc
from tests.test_cli import (AdoptAgent, CliCase, DescribedSetting, GoneAgent, InstallCase, Step, candidate,
                            put, snapshot_state)

REPO = Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "docs" / "json"
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

OK, ERROR, USAGE, NOT_FOUND = exitcodes.OK, exitcodes.ERROR, exitcodes.USAGE, exitcodes.NOT_FOUND
NEEDS_TTY, BUSY, GONE, INTERRUPTED = exitcodes.NEEDS_TTY, exitcodes.BUSY, exitcodes.GONE, exitcodes.INTERRUPTED


# ---------------------------------------------------------------------------------------------- validator

_cache = {}


def load_schema(name: str) -> dict:
    if name not in _cache:
        _cache[name] = json.loads((SCHEMAS / name).read_text())
    return _cache[name]


def _resolve(ref: str, base: str):
    file, _, fragment = ref.partition("#")
    file = file or base
    node = load_schema(file)
    for part in (p for p in fragment.split("/") if p):
        node = node[part]
    return node, file


def _is(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"object": dict, "array": list, "string": str, "boolean": bool,
                              "null": type(None)}[kind])


def schema_errors(value, schema: dict, base: str, where: str = "$") -> List[str]:
    """Every way `value` violates `schema` (whose $refs resolve relative to the file `base`)."""
    found: List[str] = []
    if "$ref" in schema:
        target, file = _resolve(schema["$ref"], base)
        found += schema_errors(value, target, file, where)
    for sub in schema.get("allOf", []):
        found += schema_errors(value, sub, base, where)
    if "anyOf" in schema and all(schema_errors(value, sub, base, where) for sub in schema["anyOf"]):
        found.append(f"{where}: matches no alternative of anyOf")
    if "if" in schema:
        branch = "else" if schema_errors(value, schema["if"], base, where) else "then"
        if branch in schema:
            found += schema_errors(value, schema[branch], base, where)
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(value, kind) for kind in kinds):
            return found + [f"{where}: expected {'/'.join(kinds)}, got {type(value).__name__} {value!r:.60}"]
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        found.append(f"{where}: expected {schema['const']!r}, got {value!r:.60}")
    if "enum" in schema and not any(value == option and type(value) is type(option) for option in schema["enum"]):
        found.append(f"{where}: {value!r:.60} is not one of {schema['enum']}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        found.append(f"{where}: {value!r:.60} does not match {schema['pattern']}")
    if _is(value, "number") and "minimum" in schema and value < schema["minimum"]:
        found.append(f"{where}: {value} is below {schema['minimum']}")
    if isinstance(value, dict):
        found += [f"{where}: missing {key!r}" for key in schema.get("required", []) if key not in value]
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                found += schema_errors(item, properties[key], base, f"{where}.{key}")
            elif extra is False:
                found.append(f"{where}: unexpected {key!r}")
            elif isinstance(extra, dict):
                found += schema_errors(item, extra, base, f"{where}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            found.append(f"{where}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                found += schema_errors(item, schema["items"], base, f"{where}[{index}]")
    return found


def validate(document: dict, command: str) -> List[str]:
    name = f"{command}.schema.json"
    return schema_errors(document, load_schema(name), name)


def json_commands() -> set:
    """Every subcommand whose parser accepts --json."""
    parser = cli._parser()
    (subparsers,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    return {name for name, sub in subparsers.choices.items()
            if any("--json" in action.option_strings for action in sub._actions)}


# ---------------------------------------------------------------------------------------------- schemas

class SchemaFilesTest(unittest.TestCase):
    def test_every_json_command_has_a_schema_and_every_schema_is_used(self):
        commands = json_commands()
        self.assertEqual(commands, {"ls", "find", "doctor", "install", "uninstall", "config", "resume", "here",
                                    "simulate", "adopt", "version"})
        shared = {"envelope", "session", "pane", "cli"}
        files = {p.name[:-len(".schema.json")] for p in SCHEMAS.glob("*.schema.json")}
        self.assertEqual(files, commands | shared)

    def test_schemas_are_well_formed_and_every_ref_resolves(self):
        def refs(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    yield node["$ref"]
                for item in node.values():
                    yield from refs(item)
            elif isinstance(node, list):
                for item in node:
                    yield from refs(item)

        for path in SCHEMAS.glob("*.schema.json"):
            schema = load_schema(path.name)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema", path.name)
            self.assertTrue(schema["$id"].endswith(f"/docs/json/{path.name}"), path.name)
            for ref in refs(schema):
                _resolve(ref, path.name)  # raises if the file or fragment is missing
            command = path.name[:-len(".schema.json")]
            if command not in ("envelope", "session", "pane"):
                self.assertEqual(schema["properties"]["schema"], {"const": f"agent-resurrect.{command}/1"})
                self.assertIn({"$ref": "envelope.schema.json"}, schema["allOf"])

    def test_error_codes_match_exitcodes(self):
        codes = load_schema("envelope.schema.json")["properties"]["error"]["properties"]["code"]["enum"]
        self.assertEqual(sorted(codes), sorted(set(exitcodes.NAMES.values()) - {"ok"}))

    def test_restore_reasons_cover_the_restore_constants(self):
        reasons = load_schema("envelope.schema.json")["$defs"]["restore_reason"]["enum"]
        for name in ("OK", "NOT_LIVE", "TOO_OLD", "AGENT_RUNNING", "SESSION_GONE"):
            self.assertIn(getattr(restore, name), reasons)

    def test_doctor_schema_allows_unknown_boot_time_and_open_check_ids(self):
        document = sample_doctor_document()
        document.update(since_boot=None, records=dict(document["records"], died_since_boot=None))
        document["context"].update(login_shell="/bin/zsh", multiplexer=None)
        document["checks"].append({"id": "shell.exec_before_block", "status": "warn", "detail": "x", "fix": None})
        self.assertEqual(validate(document, "doctor"), [])
        document["context"]["multiplexer"] = 1
        self.assertNotEqual(validate(document, "doctor"), [])

    def test_the_validator_rejects_what_it_should(self):
        good = jsonout.document("version", python="3.9.6", executable="/usr/bin/python3", package_dir="/x")
        self.assertEqual(validate(good, "version"), [])
        bad_cases = {
            "missing field when ok": {k: v for k, v in good.items() if k != "python"},
            "wrong type": dict(good, python=3),
            "wrong schema": dict(good, schema="agent-resurrect.ls/1"),
            "relative path": dict(good, executable="python3"),
            "error while ok": dict(good, error={"code": "error", "message": ""}),
            "no error while not ok": dict(good, ok=False),
            "unknown error code": dict(good, ok=False, error={"code": "oops", "message": ""}),
        }
        for label, document in bad_cases.items():
            self.assertNotEqual(validate(document, "version"), [], label)
        # On failure only the envelope is required.
        self.assertEqual(validate(jsonout.document("version", exitcodes.USAGE, "bad"), "version"), [])
        self.assertNotEqual(validate(dict(jsonout.document("ls"), entries=[{"number": True}],
                                          include_running=False), "ls"), [])


# ---------------------------------------------------------------------------------------------- views

class ViewsTest(unittest.TestCase):
    def test_iso_normalises_to_utc_seconds(self):
        cases = {
            "2026-10-12T10:54:16Z": "2026-10-12T10:54:16Z",
            "2026-10-12T10:54:16.123Z": "2026-10-12T10:54:16Z",
            "2026-10-12T10:54:16.1Z": "2026-10-12T10:54:16Z",
            "2026-10-12T12:54:16.123456+02:00": "2026-10-12T10:54:16Z",
            "2026-10-12T10:54:16": "2026-10-12T10:54:16Z",
            1791785041.851: "2026-10-12T06:04:01Z",
            None: None, "": None, "yesterday": None, True: None,
        }
        for value, expected in cases.items():
            self.assertEqual(views.iso(value), expected, value)

    def test_roles_use_stable_keys(self):
        self.assertEqual(views.roles({"you": 2, "Claude": 3, "tool output": 1, "injected context": 4, "summary": 1,
                                      "system": 2}),
                         {"user": 2, "assistant": 3, "tool_output": 1, "injected_context": 4, "other": 3})
        self.assertEqual(views.roles({}), dict.fromkeys(views.ROLES, 0))

    def test_confidence(self):
        def c(hits, total=4):
            return Candidate(session_id="s", transcript_path="/t", cwd=None, cwd_trusted=False, hits=hits,
                             total_probes=total)

        self.assertEqual(views.confidence([]), "none")
        self.assertEqual(views.confidence([c(3)]), "strong")
        self.assertEqual(views.confidence([c(3), c(2)]), "strong")
        self.assertEqual(views.confidence([c(3), c(3)]), "ambiguous")
        self.assertEqual(views.confidence([c(1)]), "weak")
        self.assertEqual(views.confidence([c(1), c(1)]), "weak")
        self.assertEqual(views.confidence([c(1, total=1)]), "strong")

    def test_session_object(self):
        with mock.patch.object(restore, "plan_resume", lambda record, agent, config: types.SimpleNamespace(
                argv=["claude", "--resume", "s-1", "--dangerously-skip-permissions"], cwd="/tmp", warning=None,
                env={"CLAUDE_CONFIG_DIR": "/cfg"})):
            session = views.session(make_record(permission_mode="bypassPermissions", transcript_path="/t/s-1.jsonl"),
                                    FakeAgent(), Config(),
                                    SessionInfo(title="T", first_prompt="P", started="2026-10-10T10:00:00.5Z",
                                                last_active=1791785041.0))
        self.assertEqual(validate_session(session), [])
        self.assertEqual((session["session_id"], session["agent"], session["title"], session["first_prompt"]),
                         ("s-1", "fake", "T", "P"))
        self.assertEqual((session["cwd"], session["cwd_exists"]), ("/tmp", True))
        self.assertEqual((session["started_at"], session["last_active_at"]), ("2026-10-10T10:00:00Z",
                                                                             "2026-10-12T06:04:01Z"))
        self.assertEqual(session["permission_mode"], "bypassPermissions")
        self.assertEqual(session["resume"]["env"], {"CLAUDE_CONFIG_DIR": "/cfg"})
        self.assertTrue(session["resume"]["skips_permissions"])

    def test_session_without_an_adapter_or_a_folder(self):
        session = views.session(make_record(cwd="", agent="mystery"), None, Config())
        self.assertEqual(validate_session(session), [])
        self.assertEqual((session["cwd"], session["cwd_exists"], session["resume"]), (None, False, None))

    def test_session_resume_follows_the_plan(self):
        session = views.session(make_record(cwd="/definitely/not/here"), FakeAgent(), Config())
        self.assertEqual(validate_session(session), [])
        resume = session["resume"]
        self.assertEqual((resume["argv"], resume["cwd"], resume["command"]),
                         (["fake-agent", "--resume", "s-1"], None, "fake-agent --resume s-1"))
        self.assertIn("no longer exists", resume["warning"])
        self.assertFalse(resume["skips_permissions"])

    def test_pane(self):
        self.assertEqual(views.pane(make_record()), {"terminal": "fake", "key": "fake-p1", "label": "p1"})
        self.assertIsNone(views.pane(make_record(pane_key="", terminal="")))


def validate_session(session: dict) -> List[str]:
    return schema_errors(session, load_schema("session.schema.json"), "session.schema.json")


# ---------------------------------------------------------------------------------------------- the contract

class ContractCase(CliCase):
    def contract(self, *argv, code, command=None, **streams) -> dict:
        """Run argv (which includes --json) and check every rule of the contract; return the document."""
        actual, out, err = self.run_cli(*argv, **streams)
        if code is not None:
            self.assertEqual(actual, code, (argv, out, err))
        self.assertTrue(out.endswith("\n") and out.count("\n") == 1, f"not one line: {out!r}")
        self.assertTrue(out.isascii(), out)
        document = json.loads(out)
        self.assertIsInstance(document, dict)
        command = command or (argv[0] if argv and argv[0] in cli.COMMANDS else "here")
        self.assertEqual(document["schema"], f"agent-resurrect.{command}/1")
        self.assertEqual(document["ok"], actual == OK)
        if actual == OK:
            self.assertIsNone(document["error"])
        else:
            self.assertEqual(document["error"]["code"], exitcodes.name(actual))
            self.assertTrue(document["error"]["message"])
        self.assertEqual(validate(document, command), [], json.dumps(document, indent=1))
        return document


class ListContractTest(ContractCase):
    def test_empty(self):
        document = self.contract("ls", "--json", code=OK)
        self.assertEqual((document["include_running"], document["entries"]), (False, []))

    def test_entries(self):
        put(make_record(pane_key="fake-p1", session_id="s-old", pane_label="p1", agent_pid=None), 30)
        put(make_record(pane_key="fake-p2", session_id="s-run", pane_label="p2", agent_pid=777), 1)
        put(make_record(pane_key="fake-p3", session_id="s-who", agent="mystery", status="skipped"), 40)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))
        document = self.contract("ls", "--json", code=OK)
        self.assertEqual([(e["number"], e["session"]["session_id"]) for e in document["entries"]],
                         [(1, "s-old"), (2, "s-who")])
        document = self.contract("ls", "--json", "--all", "--ids", code=OK)
        entries = {e["session"]["session_id"]: e for e in document["entries"]}
        self.assertTrue(document["include_running"])
        self.assertEqual((entries["s-run"]["number"], entries["s-run"]["running"]), (None, True))
        self.assertEqual(entries["s-run"]["auto_restore"], {"eligible": False, "reason": "agent-running"})
        self.assertEqual(entries["s-old"]["auto_restore"], {"eligible": True, "reason": "ok"})
        self.assertEqual(entries["s-who"]["auto_restore"], {"eligible": False, "reason": "unknown-agent"})
        self.assertEqual(entries["s-old"]["pane"], {"terminal": "fake", "key": "fake-p1", "label": "p1"})
        self.assertRegex(entries["s-old"]["updated_at"], TIMESTAMP)
        self.assertEqual(entries["s-old"]["session"]["resume"]["argv"], ["fake-agent", "--resume", "s-old"])

    def test_non_ascii_is_escaped_and_round_trips(self):
        title = "Café ☕ 修复 \x1b[2J"

        class Unicode(FakeAgent):
            def describe(self, record):
                return SessionInfo(title=title)

        self.agent = Unicode()
        put(make_record(), 5)
        document = self.contract("ls", "--json", code=OK)
        self.assertEqual(document["entries"][0]["session"]["title"], title)  # raw, JSON-escaped on the wire

    def test_argument_error(self):
        document = self.contract("ls", "--json", "--bogus", code=USAGE)
        self.assertIn("--bogus", document["error"]["message"])
        self.assertNotIn("entries", document)


class FindContractTest(ContractCase):
    def results(self, *found, error=None):
        class FindAgent(FakeAgent):
            def find_by_text(self, text, exclude=None, limit=5):
                if error:
                    raise error
                return list(found)[:limit]

        self.agent = FindAgent()

    def test_confidence_levels(self):
        home = str(self.home)
        cases = [
            ((candidate("s-9", home), candidate("s-8", None, hits=2)), "strong"),
            ((candidate("s-9", home, hits=1),), "weak"),
            ((candidate("s-9", home), candidate("s-8", None)), "ambiguous"),
        ]
        for found, confidence in cases:
            self.results(*found)
            document = self.contract("find", "--json", "deploy", "the", "worker", code=OK)
            self.assertEqual(document["confidence"], confidence)
            self.assertEqual(document["query"], {"characters": len("deploy the worker"), "probes": 4})
            self.assertEqual(document["best"], document["candidates"][0])
            self.assertEqual(len(document["candidates"]), len(found))
        best = document["best"]
        self.assertEqual(best["roles"], {"user": 1, "assistant": 2, "tool_output": 0, "injected_context": 0,
                                         "other": 0})
        self.assertTrue(best["in_conversation"])
        self.assertEqual(best["session"]["resume"]["command"], f"cd {home} && fake-agent --resume s-9")
        self.assertEqual(best["session"]["started_at"], "2026-10-10T10:00:00Z")

    def test_text_only_in_tool_output_is_not_in_conversation(self):
        found = candidate("s-9", None)
        found.roles = {"tool output": 3}
        self.results(found)
        self.assertFalse(self.contract("find", "--json", "some words here", code=OK)["best"]["in_conversation"])

    def test_limit(self):
        self.results(*(candidate(f"s-{n}", None, hits=4 - n % 3) for n in range(6)))
        self.assertEqual(len(self.contract("find", "--json", "--limit", "2", "some words", code=OK)["candidates"]), 2)

    def test_not_found(self):
        self.results()
        document = self.contract("find", "--json", "nothing has this", code=NOT_FOUND)
        self.assertEqual((document["confidence"], document["best"], document["candidates"]), ("none", None, []))

    def test_usage_and_errors(self):
        self.results(error=ValueError("Pasted text is too short to identify a session"))
        self.assertIn("too short", self.contract("find", "--json", "ok", code=USAGE)["error"]["message"])
        self.contract("find", "--json", code=USAGE, stdin="   ")
        self.contract("find", "--json", code=USAGE, tty=True)
        self.contract("find", "--json", "--limit", "0", "words", code=USAGE)
        with mock.patch.object(cli, "_read_clipboard", return_value=None):
            self.contract("find", "--json", "--clipboard", code=ERROR)


class ResumeContractTest(ContractCase):
    def test_print(self):
        put(make_record(), 5)
        before = snapshot_state()
        for argv, command in ((("resume", "s-1"), "resume"), (("resume", "1"), "resume"), (("here",), "here"),
                              ((), "here")):
            document = self.contract(*argv, "--json", "--print", code=OK, command=command)
            self.assertEqual(document["action"], "printed")
            self.assertEqual(document["session"]["resume"]["command"], "cd /tmp && fake-agent --resume s-1")
            self.assertEqual(document["pane"], {"terminal": "fake", "key": "fake-p1", "label": "p1"})
        self.assertEqual(snapshot_state(), before)
        self.assertEqual(self.launched, [])

    def test_json_implies_print_with_or_without_a_terminal(self):
        """Lead ruling for 0.2.0: --json never launches. It prints one document with action "printed" and exit 0,
        exactly as --json --print does, whether or not stdin and stdout are terminals."""
        put(make_record(), 5)
        before = snapshot_state()
        for tty in (False, True):
            for argv, command in ((("resume", "s-1"), "resume"), (("resume", "1"), "resume"), (("here",), "here"),
                                  ((), "here")):
                document = self.contract(*argv, "--json", code=OK, command=command, tty=tty)
                self.assertEqual(document["action"], "printed", (argv, tty))
                self.assertEqual(document["session"]["resume"]["argv"], ["fake-agent", "--resume", "s-1"])
                self.assertEqual(document["session"]["resume"]["command"], "cd /tmp && fake-agent --resume s-1")
        self.assertEqual(snapshot_state(), before)  # nothing claimed, no restore event
        self.assertEqual(self.launched, [])

    def test_refusals(self):
        put(make_record(), 5)
        self.procs = FakeProcs(proc(4242, 1, "fake-agent"))
        self.assertEqual(self.contract("resume", "s-1", "--json", code=BUSY, tty=True)["action"], "refused")
        self.procs = FakeProcs()
        self.agent = FakeAgent(exists=False)
        self.assertEqual(self.contract("here", "--json", code=GONE, tty=True)["action"], "refused")
        put(make_record(agent="mystery"), 5)
        document = self.contract("resume", "s-1", "--json", code=ERROR, tty=True)
        self.assertEqual((document["action"], document["session"]["resume"]), ("refused", None))
        self.assertEqual(self.launched, [])

    def test_not_found(self):
        for argv in (("resume", "nope"), ("resume", "7")):
            document = self.contract(*argv, "--json", code=NOT_FOUND)
            self.assertEqual((document["action"], document["session"]), (None, None))
        document = self.contract("here", "--json", code=NOT_FOUND)
        self.assertEqual(document["pane"], {"terminal": "fake", "key": "fake-p1", "label": "p1"})
        del os.environ["FAKE_PANE"]
        self.assertIsNone(self.contract("here", "--json", code=NOT_FOUND)["pane"])
        self.contract("resume", "", "--json", code=USAGE)
        self.contract("resume", "--json", code=USAGE)

class SimulateContractTest(ContractCase):
    def test_panes(self):
        self.agent = GoneAgent()
        put(make_record(pane_key="fake-p1", session_id="s-ready", agent_pid=None), 5)
        put(make_record(pane_key="fake-p2", session_id="s-gone", agent_pid=None), 6)
        put(make_record(pane_key="fake-p3", session_id="s-who", agent="mystery"), 7)
        document = self.contract("simulate", "--json", "--assume-restart", code=OK)
        self.assertTrue(document["assume_restart"])
        decisions = {p["pane"]["key"]: (p["decision"], p["reason"], p["mode"]) for p in document["panes"]}
        self.assertEqual(decisions, {"fake-p1": ("would-resume", "ok", "countdown"),
                                     "fake-p2": ("would-skip", "session-gone", "countdown"),
                                     "fake-p3": ("would-skip", "unknown-agent", "countdown")})
        document = self.contract("simulate", "--json", "--pane-key", "fake-p9", code=OK)
        self.assertEqual(document["panes"], [{"pane": {"terminal": None, "key": "fake-p9", "label": None},
                                              "decision": "no-record", "reason": None, "mode": "countdown",
                                              "session": None}])
        self.assertEqual(self.contract("simulate", "--json", code=OK, tty=True)["assume_restart"], False)

    def test_usage(self):
        self.contract("simulate", "--json", "--pane-key", "a/b", code=USAGE)


class AdoptContractTest(ContractCase):
    def test_adopt(self):
        self.agent = AdoptAgent(make_record(session_id="s-adopted"))
        document = self.contract("adopt", "--json", code=OK)
        self.assertTrue(document["recorded"])
        self.assertEqual(document["session"]["session_id"], "s-adopted")
        self.assertEqual(document["pane"]["key"], "fake-p1")

    def test_not_adoptable(self):
        self.agent = AdoptAgent(None)
        document = self.contract("adopt", "--json", code=NOT_FOUND)
        self.assertFalse(document["recorded"])
        del os.environ["FAKE_PANE"]
        self.assertIsNone(self.contract("adopt", "--json", code=NOT_FOUND)["pane"])

    def test_failure(self):
        for error in (OSError("No space left on device"), RuntimeError("ps exploded")):
            self.agent = AdoptAgent(error=error)
            self.assertIn(str(error), self.contract("adopt", "--json", code=ERROR)["error"]["message"])


class ConfigContractTest(ContractCase):
    def test_config(self):
        document = self.contract("config", "--json", code=OK)
        self.assertEqual(document["values"], {"auto_resume": "countdown", "countdown_seconds": 3,
                                              "permission_flag": "mirror", "max_age_days": 14,
                                              "pane_search": "ask"})
        self.assertEqual(document["choices"]["pane_search"], ["ask", "auto", "off"])
        self.assertEqual(document["choices"]["auto_resume"], ["countdown", "immediate", "ask", "off"])
        self.assertEqual((document["key"], document["value"], document["written"]), (None, None, False))
        document = self.contract("config", "countdown_seconds", "--json", code=OK)
        self.assertEqual((document["key"], document["value"], document["written"]), ("countdown_seconds", 3, False))
        document = self.contract("config", "countdown_seconds", "5", "--json", code=OK)
        self.assertEqual((document["value"], document["written"], document["values"]["countdown_seconds"]),
                         (5, True, 5))
        self.contract("config", "bogus", "--json", code=USAGE)
        self.contract("config", "auto_resume", "maybe", "--json", code=USAGE)


class VersionContractTest(ContractCase):
    def test_version(self):
        document = self.contract("version", "--json", code=OK)
        self.assertTrue(Path(document["package_dir"], "cli.py").is_file())


class InstallContractTest(InstallCase, ContractCase):
    def test_install(self):
        self.fake_installer()
        settings = [DescribedSetting("iterm.keep_windows", "Keep windows."), DescribedSetting("claude.plugin")]
        with mock.patch.object(cli, "all_settings", lambda: list(settings)):
            document = self.contract("install", "--json", "--yes", code=OK)
            self.assertEqual(document["steps"][0], {"id": "iterm.keep_windows", "status": "changed",
                                                    "description": "Keep windows.", "detail": ""})
            self.assertEqual([s["kind"] for s in document["next_steps"]], ["new-shell", "restart-agent"])
            self.assertFalse(document["dry_run"])
            document = self.contract("install", "--json", "--dry-run", "--no-plugin", code=OK)
            self.assertTrue(document["dry_run"])
            self.assertEqual(document["steps"][0]["status"], "would-change")
            for tty in (False, True):  # --json never asks: a needed confirmation is exit 4
                self.contract("install", "--json", code=NEEDS_TTY, tty=tty)

    def test_install_failure(self):
        self.fake_installer([Step("claude.plugin", "failed", "claude exited 1")])
        document = self.contract("install", "--json", "--yes", code=ERROR)
        self.assertEqual(document["steps"][0]["status"], "failed")
        self.assertEqual(document["next_steps"], [])

    def test_uninstall(self):
        self.fake_installer()
        document = self.contract("uninstall", "--json", "--yes", "--purge", code=OK)
        self.assertEqual((document["purge"], document["dry_run"]), (True, False))
        self.contract("uninstall", "--json", "--dry-run", code=OK)
        for tty in (False, True):
            self.contract("uninstall", "--json", code=NEEDS_TTY, tty=tty)
        self.fake_installer(uninstall_results=[Step("manifest", "failed", "manifest is unreadable")])
        self.contract("uninstall", "--json", "--yes", code=ERROR)


class FakeSetting(Setting):
    id = "test.setting"
    fix_hint = "run: agent-resurrect install"

    def check(self):
        return Check(True, "on")

    def apply(self):
        return None

    def revert(self, original):
        pass


def sample_doctor_document() -> dict:
    """A minimal doctor --json document, for checking the schema itself."""
    return jsonout.document(
        "doctor", ready=True, summary={"ok": 1, "warn": 0, "fail": 0},
        checks=[{"id": "python", "status": "ok", "detail": "python 3", "fix": None}],
        context={"terminal": None, "pane_key": None, "launcher": None, "log_path": "/tmp/log", "boot_time": None},
        records={"total": 0, "live": 0, "restorable": 0, "died_since_boot": 0},
        since_boot={"resumed": 0, "skipped": 0, "launch-failed": 0, "offered": 0, "not-a-tty": 0})


class DoctorContractTest(ContractCase):
    def test_doctor(self):
        real_run = doctor.run
        os.environ["PATH"] = f"{REPO / 'tests' / 'fakes'}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["FAKE_DEFAULTS_DB"] = str(self.home / "defaults.json")
        os.environ["FAKE_CLAUDE_DB"] = str(self.home / "claude.json")
        put(make_record(agent_pid=None), 5)
        for patch in (mock.patch.object(doctor, "all_settings", lambda: [FakeSetting()]),
                      mock.patch.object(doctor, "detect_terminal",
                                        lambda env: FakeTerminal() if FakeTerminal().detect(env) else None),
                      mock.patch.object(doctor, "get_agent", lambda name: self.agent if name == "fake" else None)):
            patch.start()
            self.addCleanup(patch.stop)

        def run(**kwargs):
            return real_run(procs=FakeProcs(), boot_time=time.time() - 3600, **kwargs)

        with mock.patch.object(cli, "doctor", types.SimpleNamespace(run=run)):
            for flags in ((), ("--strict",)):
                document = self.contract("doctor", "--json", *flags, code=None)
                self.assertIn(document["error"] and document["error"]["code"], (None, "error"))
        self.contract("doctor", "--json", "--bogus", code=USAGE)


class GeneralContractTest(ContractCase):
    def test_unknown_command(self):
        self.contract("bogus", "--json", code=USAGE, command="cli")

    def test_interrupted(self):
        with mock.patch.dict(cli.COMMANDS, {"ls": mock.Mock(side_effect=KeyboardInterrupt)}):
            self.contract("ls", "--json", code=INTERRUPTED)

    def test_unexpected_error(self):
        with mock.patch.dict(cli.COMMANDS, {"find": mock.Mock(side_effect=RuntimeError("disk on fire"))}):
            document = self.contract("find", "--json", "x", code=ERROR)
        self.assertIn("disk on fire", document["error"]["message"])


if __name__ == "__main__":
    unittest.main()
