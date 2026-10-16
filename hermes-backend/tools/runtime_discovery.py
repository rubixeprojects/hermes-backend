"""Advisory search over the profile's live runtime capability catalog."""
from tools.registry import registry, tool_result


def _search_runtime_catalog(args: dict, **kwargs) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return tool_result(error="query must not be empty")
    kinds = args.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or not all(
            item in {"tool", "skill", "agent"} for item in kinds)):
        return tool_result(error="kinds must contain only tool, skill, or agent")
    try:
        from agent.discovery_index import search_runtime_catalog
        hits = search_runtime_catalog(query, kinds=kinds,
                                      available_tool_names=kwargs.get("available_tool_names"))
        return tool_result(candidates=hits,
                           authority="Candidates only; availability and permissions are checked at use time.")
    except Exception as exc:
        return tool_result(error=f"Runtime catalog search failed: {type(exc).__name__}")


registry.register(
    name="runtime_discovery",
    toolset="runtime_discovery",
    schema={
        "name": "runtime_discovery",
        "description": "Find currently available tools, skills, or reusable agent manifests by query. Results are advisory candidates; use existing tool dispatch and skill loading paths, which remain authoritative.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What capability you need."},
                "kinds": {"type": "array", "items": {"type": "string", "enum": ["tool", "skill", "agent"]}, "description": "Optional candidate types to search."},
            },
            "required": ["query"],
        },
    },
    handler=_search_runtime_catalog,
)
