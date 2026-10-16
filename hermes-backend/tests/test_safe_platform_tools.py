from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions


def _names(toolsets, *, raw=False):
    definitions = get_tool_definitions(
        enabled_toolsets=sorted(toolsets), quiet_mode=True, skip_tool_search_assembly=raw,
    )
    return {definition["function"]["name"] for definition in definitions}


_MUTATING_OR_EXECUTION_TOOLS = {
    "delegate_task", "terminal", "process_manage", "write_file", "patch", "skill_manage",
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_exec",
    "execute_code", "todo_list", "memory", "cronjob_manage", "computer_use",
    "manage_connections", "clarify", "image_generate", "text_to_speech", "desktop_project",
    "plugin_install", "plugin_uninstall", "mcp_server_manage", "cronjob_manage", "schedule_task",
    "todo_write", "memory_write", "file_delete", "file_move", "file_copy", "shell", "terminal_exec",
}


def test_default_platform_surface_is_read_only_and_discoverable():
    for platform in ("cli", "api_server", "telegram", "discord"):
        enabled = _get_platform_tools({}, platform, include_default_mcp_servers=False)
        names = _names(enabled)

        assert {"web_search", "web_extract", "read_file", "search_files", "skills_list", "skill_view"} <= names
        assert "runtime_discovery" in names
        assert not (_MUTATING_OR_EXECUTION_TOOLS & names), (platform, names & _MUTATING_OR_EXECUTION_TOOLS)


def test_saved_platform_default_composites_remain_restrictive():
    config = {
        "platform_toolsets": {
            "cli": ["hermes-cli"],
            "api_server": ["hermes-api-server"],
            "telegram": ["hermes-telegram"],
            "discord": ["hermes-discord"],
        }
    }
    for platform in config["platform_toolsets"]:
        enabled = _get_platform_tools(config, platform, include_default_mcp_servers=False)
        names = _names(enabled)
        assert "runtime_discovery" in names
        assert not (_MUTATING_OR_EXECUTION_TOOLS & names), (platform, names & _MUTATING_OR_EXECUTION_TOOLS)


def test_explicit_toolset_selection_remains_available():
    enabled = _get_platform_tools(
        {"platform_toolsets": {"cli": ["terminal", "file", "web", "delegation", "skills"]}},
        "cli",
        include_default_mcp_servers=False,
    )
    names = _names(enabled, raw=True)

    assert {"terminal", "delegate_task", "write_file", "patch", "skill_manage", "web_search"} <= names


def test_skill_view_default_is_read_only_and_does_not_activate():
    from unittest.mock import patch
    from tools.skills_tool import _skill_readiness

    frontmatter = {
        "required_environment_variables": ["DISCOVERY_TEST_SECRET"],
        "required_credential_files": ["credentials/service.json"],
    }
    with (
        patch("tools.skills_tool.load_env", return_value={}),
        patch("tools.skills_tool._is_env_var_persisted", return_value=False),
        patch("tools.skills_tool._capture_required_environment_variables") as capture,
        patch("tools.env_passthrough.register_env_passthrough") as passthrough,
        patch("tools.credential_files.register_credential_files") as credentials,
    ):
        fields, extras = _skill_readiness(frontmatter, "safe-read", activate=False)

    capture.assert_not_called()
    passthrough.assert_not_called()
    credentials.assert_not_called()
    assert fields["setup_needed"]
    assert fields["missing_credential_files"] == ["credentials/service.json"]
    assert "Read-only skill viewing" in extras["setup_note"]


def test_plugin_toolsets_require_explicit_selection():
    from hermes_cli.tools_config import _enabled_plugin_toolsets

    assert _enabled_plugin_toolsets({}, "cli", ["hermes-cli"], {"sample_plugin"}) == set()
    assert _enabled_plugin_toolsets({}, "cli", ["hermes-cli", "sample_plugin"], {"sample_plugin"}) == {"sample_plugin"}
