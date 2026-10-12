"""The docs say what the code does: the install prompt, the shell block, and the house style."""
import re
import subprocess
import unittest
from pathlib import Path

from agent_resurrect.settings.shellrc import ShellRcBlock
from tests.helpers import temp_home

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "docs" / "specs" / "2026-10-12-agent-resurrect-design.md"
README = REPO / "README.md"
INSTALL_WITH_CLAUDE = REPO / "docs" / "install-with-claude.md"
EM_DASH = chr(0x2014)


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


class DocsTest(unittest.TestCase):
    def test_install_prompt_is_identical_everywhere(self):
        readme = fenced_after(README, "### Install with Claude Code", "text")
        guide = fenced_after(INSTALL_WITH_CLAUDE, "## The prompt", "text")
        self.assertEqual(readme, spec_prompt())
        self.assertEqual(guide, spec_prompt())
        # Claude Code's shell may not have ~/.local/bin on PATH until a new terminal, so the prompt uses the full path.
        self.assertIn("run `~/.local/bin/agent-resurrect doctor`", readme)
        self.assertNotRegex(readme, r"(?<!/)agent-resurrect doctor")

    def test_uninstall_instruction_for_claude_uses_the_full_path(self):
        self.assertIn("\n~/.local/bin/agent-resurrect uninstall --yes\n", INSTALL_WITH_CLAUDE.read_text())
        self.assertNotRegex(INSTALL_WITH_CLAUDE.read_text(), r"\n(?!~/\.local/bin/)\S*agent-resurrect uninstall --yes")

    def test_readme_shell_block_is_what_the_installer_writes(self):
        with temp_home():
            block = ShellRcBlock("zsh").block()
        self.assertEqual(fenced_after(README, "The shell block looks like this:", "bash"), block)

    def test_no_em_dashes(self):
        listed = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, timeout=30)
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        # docs/plans holds the implementation plan, whose own em-dash check quotes the character.
        names = [name for name in listed.stdout.splitlines() if not name.startswith("docs/plans/")]
        self.assertIn("README.md", names)
        offenders = [name for name in names
                     if (REPO / name).is_file() and EM_DASH in (REPO / name).read_text(errors="replace")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
