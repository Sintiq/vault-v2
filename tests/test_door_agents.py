"""The fixed, reviewed CLI profile: no shell, tools, inherited settings or model fallback."""

import json

import pytest

from vault_v2.agent_door import DoorError
from vault_v2.door_agents import claude_command


def test_launch_profile_has_exact_model_and_disables_customizations(tmp_path, monkeypatch):
    executable = tmp_path / "claude.exe"
    executable.touch()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://not-the-provider.invalid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE-TEST-KEY")
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    command = claude_command("claude", {}, executable=executable)
    args = command.argv
    assert args[0] == str(executable)
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowed-tools") + 1] == "*"
    assert args[args.index("--setting-sources") + 1] == ""
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert json.loads(args[args.index("--settings") + 1])["disableAllHooks"] is True
    for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence",
                 "--disable-slash-commands", "--no-chrome", "--print"):
        assert flag in args
    for flag in ("--bare", "--fallback-model", "--resume", "--continue", "--dangerously-skip-permissions"):
        assert flag not in args
    assert not any(k.startswith("ANTHROPIC_") for k in command.env)
    assert "CLAUDE_CODE_SIMPLE" not in command.env


@pytest.mark.parametrize("model", ["opus", "fable", "claude-opus-5", "", None])
def test_model_aliases_and_unapproved_models_are_configuration_refusals(tmp_path, model):
    with pytest.raises(DoorError, match="configuration"):
        claude_command("claude", {"agent_door_claude_model": model})


def test_explicit_fable_setting_uses_only_the_approved_exact_identifier(tmp_path):
    executable = tmp_path / "claude.exe"
    executable.touch()
    command = claude_command("claude", {"agent_door_claude_model": "claude-fable-5-1"},
                             executable=executable)
    assert command.argv[command.argv.index("--model") + 1] == "claude-fable-5-1"
    assert "--fallback-model" not in command.argv


def test_codex_profile_remains_disabled_even_with_an_executable(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.touch()
    with pytest.raises(DoorError, match="not available yet"):
        claude_command("codex", {}, executable=executable)
