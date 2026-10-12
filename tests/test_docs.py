"""The docs say what the code does: the install prompt, the install routes, the shell block, and the house style."""
import re
import subprocess
import unittest
from pathlib import Path
from typing import List

from agent_resurrect.settings.shellrc import ShellRcBlock
from tests.helpers import temp_home

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "docs" / "specs" / "2026-10-12-agent-resurrect-design.md"
README = REPO / "README.md"
INSTALL_WITH_CLAUDE = REPO / "docs" / "install-with-claude.md"
EM_DASH = chr(0x2014)

LATEST_INSTALLER = "https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh"
ONE_LINER = f"curl -fsSL {LATEST_INSTALLER} | bash\n"
DOWNLOADED = '"${TMPDIR:-/tmp}/agent-resurrect-install.sh"'
# Docs that tell people or agents how to install. install.sh itself is not listed: its header comment is the
# installer's own business, and a release replaces it.
INSTALL_DOCS = ("README.md", "docs/install-with-claude.md", "llms.txt", "AGENTS.md", "CONTRIBUTING.md")


def fenced_after(path: Path, heading: str, language: str) -> str:
    """The first ```<language> block after the line `heading`."""
    text = path.read_text()
    start = text.index(heading + "\n")
    match = re.compile(rf"^```{language}\n(.*?)^```$", re.S | re.M).search(text, start)
    return match.group(1)


def spec_prompt() -> str:
    lines = [line for line in SPEC.read_text().splitlines() if line.startswith("   > Install agent-resurrect")]
    if len(lines) != 1:
        raise AssertionError(f"expected one prompt quote in the spec, found {len(lines)}")
    return lines[0][len("   > "):] + "\n"


def tracked_files() -> List[str]:
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, timeout=30)
    if listed.returncode != 0:
        raise unittest.SkipTest("not a git checkout")
    # docs/plans holds the implementation plans, whose own style checks quote the characters they forbid.
    return [name for name in listed.stdout.splitlines() if not name.startswith("docs/plans/")]


def headings(text: str) -> List[str]:
    """Markdown ATX headings outside fenced code blocks."""
    found = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            match = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
            if match:
                found.append(match.group(1))
    return found


def shouts(heading: str) -> bool:
    """An all-caps heading: every letter upper case, and more than one word or a long word.
    Short acronyms on their own ("FAQ", "JSON") are allowed."""
    words = [word for word in re.findall(r"[^\W\d_]+", re.sub(r"`[^`]*`", "", heading))]
    letters = "".join(words)
    if not letters or letters != letters.upper():
        return False
    return len(words) > 1 or len(letters) > 4


class DocsTest(unittest.TestCase):
    def test_install_prompt_is_identical_everywhere(self):
        readme = fenced_after(README, "### Install with Claude Code", "text")
        guide = fenced_after(INSTALL_WITH_CLAUDE, "## The prompt", "text")
        self.assertEqual(readme, spec_prompt())
        self.assertEqual(guide, spec_prompt())

    def test_install_prompt_runs_the_file_it_read(self):
        prompt = spec_prompt()
        # One download, to a file the agent reads; then a dry run, a pause for consent, and that same file runs.
        self.assertEqual(prompt.count(LATEST_INSTALLER), 1)
        self.assertIn(f"-o {DOWNLOADED}", prompt)
        self.assertIn(f"`bash {DOWNLOADED} --dry-run`", prompt)
        self.assertIn(f"`bash {DOWNLOADED}`", prompt)
        self.assertIn("stop and wait for my OK", prompt)
        self.assertNotIn("| bash", prompt)
        self.assertNotIn("raw.githubusercontent.com", prompt)
        self.assertIn("Do not use sudo.", prompt)

    def test_install_prompt_only_runs_fixes_an_agent_may_run(self):
        prompt = spec_prompt()
        self.assertIn("`~/.local/bin/agent-resurrect doctor --json`", prompt)
        self.assertIn("if fix.agent_may_run is true, run fix.argv once", prompt)
        self.assertIn("otherwise tell me fix.text", prompt)

    def test_install_prompt_runs_the_launcher_by_its_full_path(self):
        # Claude Code's shell may not have ~/.local/bin on PATH until a new terminal, so every command the agent
        # runs uses the full path. The bare `agent-resurrect uninstall` is for the person, in a new terminal.
        prompt = spec_prompt()
        self.assertIn("`~/.local/bin/agent-resurrect adopt`", prompt)
        self.assertIn("`~/.local/bin/agent-resurrect uninstall --yes`", prompt)
        self.assertNotRegex(prompt, r"(?<!/)agent-resurrect (doctor|adopt|install|uninstall --yes)")

    def test_uninstall_instruction_for_claude_uses_the_full_path(self):
        self.assertIn("\n~/.local/bin/agent-resurrect uninstall --yes\n", INSTALL_WITH_CLAUDE.read_text())
        self.assertNotRegex(INSTALL_WITH_CLAUDE.read_text(), r"\n(?!~/\.local/bin/)\S*agent-resurrect uninstall --yes")

    def test_install_docs_use_the_latest_release(self):
        text = README.read_text()
        self.assertEqual(fenced_after(README, "# agent-resurrect", "bash"), ONE_LINER)
        self.assertEqual(fenced_after(README, "## Install", "bash"), ONE_LINER)
        self.assertIn(ONE_LINER, fenced_after(README, "### Upgrade", "bash"))
        self.assertIn(LATEST_INSTALLER, (REPO / "llms.txt").read_text())
        for name in INSTALL_DOCS:
            with self.subTest(name):
                self.assertNotIn("/main/install.sh", (REPO / name).read_text())
        self.assertIn("\nbash install.sh --dry-run\nbash install.sh\n", text)

    def test_plugin_install_route(self):
        steps = ("/plugin marketplace add imgovind/agent-resurrect\n"
                 "/plugin install agent-resurrect@agent-resurrect\n"
                 "/agent-resurrect:setup\n")
        self.assertEqual(fenced_after(README, "### Install from inside Claude Code", "text"), steps)
        self.assertEqual(fenced_after(INSTALL_WITH_CLAUDE, "## Install the plugin instead", "text"), steps)

    def test_readme_shell_block_is_what_the_installer_writes(self):
        with temp_home():
            block = ShellRcBlock("zsh").block()
        self.assertEqual(fenced_after(README, "The shell block looks like this:", "bash"), block)

    def test_readme_reserves_the_demo(self):
        self.assertRegex(README.read_text().split("\n## ", 1)[0], r"!\[[^\]]+\]\(docs/demo\.gif\)")

    def test_no_em_dashes(self):
        names = tracked_files()
        self.assertIn("README.md", names)
        offenders = [name for name in names
                     if (REPO / name).is_file() and EM_DASH in (REPO / name).read_text(errors="replace")]
        self.assertEqual(offenders, [])

    def test_no_all_caps_headings(self):
        self.assertTrue(shouts("WHAT IT DOES") and shouts("INSTALLATION"))
        self.assertFalse(shouts("FAQ") or shouts("JSON output") or shouts("AGENTS.md") or shouts("Why `ls` is fast"))
        offenders = []
        for name in tracked_files():
            path = REPO / name
            if name.endswith(".md") and path.is_file():
                offenders += [f"{name}: {heading}" for heading in headings(path.read_text()) if shouts(heading)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
