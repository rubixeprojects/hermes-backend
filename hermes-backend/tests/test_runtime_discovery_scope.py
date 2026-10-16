from unittest.mock import patch

from agent import discovery_index


def test_runtime_catalog_filters_tools_to_allowlist():
    hits = [
        {"kind": "tool", "name": "allowed_tool"},
        {"kind": "tool", "name": "other_session_tool"},
    ]
    with (
        patch.object(discovery_index, "refresh_runtime_catalog"),
        patch.object(discovery_index, "search", return_value=hits),
        patch("tools.registry.registry.get_all_tool_names", return_value=["allowed_tool", "other_session_tool"]),
        patch("tools.skills_tool._find_all_skills", return_value=[]),
        patch.object(discovery_index, "load_agent_manifests", return_value=[]),
    ):
        result = discovery_index.search_runtime_catalog("tool", kinds=["tool"],
                                                        available_tool_names=["allowed_tool"])

    assert result == [{"kind": "tool", "name": "allowed_tool"}]


def test_runtime_catalog_fails_closed_without_tool_scope():
    hits = [
        {"kind": "tool", "name": "globally_registered_tool"},
        {"kind": "skill", "name": "available_skill"},
    ]
    with (
        patch.object(discovery_index, "refresh_runtime_catalog"),
        patch.object(discovery_index, "search", return_value=hits),
        patch("tools.registry.registry.get_all_tool_names", return_value=["globally_registered_tool"]),
        patch("tools.skills_tool._find_all_skills", return_value=[{"name": "available_skill"}]),
        patch.object(discovery_index, "load_agent_manifests", return_value=[]),
    ):
        result = discovery_index.search_runtime_catalog("candidate", kinds=["tool", "skill"],
                                                        available_tool_names=None)

    assert result == [{"kind": "skill", "name": "available_skill"}]


def test_runtime_catalog_returns_allowed_tools():
    hits = [
        {"kind": "tool", "name": "allowed_tool"},
        {"kind": "tool", "name": "other_session_tool"},
    ]
    with (
        patch.object(discovery_index, "refresh_runtime_catalog"),
        patch.object(discovery_index, "search", return_value=hits),
        patch("tools.registry.registry.get_all_tool_names", return_value=["allowed_tool", "other_session_tool"]),
        patch("tools.skills_tool._find_all_skills", return_value=[]),
        patch.object(discovery_index, "load_agent_manifests", return_value=[]),
    ):
        result = discovery_index.search_runtime_catalog("tool", kinds=["tool"],
                                                        available_tool_names=["allowed_tool", "other_session_tool"])

    assert result == hits
