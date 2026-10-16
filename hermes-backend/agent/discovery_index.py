"""Elasticsearch-backed, profile-scoped catalog of already-trusted capabilities.

The catalog is advisory: it never loads Python and never grants a capability. Runtime
registries and the dispatcher's current profile permissions remain authoritative.
"""
from __future__ import annotations

import json
import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

INDEX = "hermes-discovery-v1"
BULK_BATCH_SIZE = 200


def _base_url() -> str:
    return os.environ.get("HERMES_DISCOVERY_ES_URL", "http://127.0.0.1:19200").rstrip("/")


def _request(method: str, path: str, payload: dict | bytes | None = None) -> dict[str, Any]:
    data = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    request = urllib.request.Request(
        f"{_base_url()}/{path.lstrip('/')}", data=data, method=method,
        headers={"Content-Type": "application/x-ndjson" if isinstance(payload, bytes) else "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            error = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Elasticsearch HTTP {exc.code}") from exc
        if exc.code == 404 and error.get("error", {}).get("type") == "index_not_found_exception":
            return error
        detail = error.get("error", {}).get("type", "unknown_error")
        raise RuntimeError(f"Elasticsearch HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Discovery Elasticsearch unavailable: {exc}") from exc
    return json.loads(body or b"{}")


def ensure_index() -> None:
    existing = _request("GET", INDEX)
    if not existing.get("error"):
        return
    if existing.get("error", {}).get("type") != "index_not_found_exception":
        raise RuntimeError("Elasticsearch returned an unexpected index lookup response")
    created = _request("PUT", INDEX, {"mappings": {"properties": {
        "profile": {"type": "keyword"}, "kind": {"type": "keyword"},
        "name": {"type": "keyword"}, "description": {"type": "text"},
        "tags": {"type": "keyword"}, "metadata": {"type": "object", "enabled": False},
    }}})
    if not created.get("acknowledged"):
        raise RuntimeError("Elasticsearch did not acknowledge discovery index creation")


def index_records(profile: str, records: list[dict[str, Any]]) -> int:
    """Upsert validated catalog records. IDs are deterministic within profile/kind/name."""
    if not isinstance(profile, str) or not profile.strip() or len(profile) > 256:
        raise ValueError("profile must be a non-empty string of at most 256 characters")
    ensure_index()
    actions: list[bytes] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        kind, name = record.get("kind"), record.get("name")
        if (kind not in {"tool", "skill", "agent"} or not isinstance(name, str)
                or not name.strip() or len(name) > 256):
            continue
        tags = record.get("tags", [])
        metadata = record.get("metadata", {})
        description = record.get("description", "")
        if (not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)
                or not isinstance(metadata, dict) or not isinstance(description, str)):
            continue
        identity = hashlib.sha256(
            json.dumps([profile, kind, name], ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        doc = {"profile": profile, "kind": kind, "name": name,
               "description": description[:4000], "tags": tags[:32], "metadata": metadata}
        actions.extend((
            json.dumps({"index": {"_index": INDEX, "_id": identity}}, ensure_ascii=False).encode(),
            json.dumps(doc, ensure_ascii=False).encode(),
        ))
    if not actions:
        return 0
    count = 0
    for start in range(0, len(actions), BULK_BATCH_SIZE * 2):
        payload = b"\n".join(actions[start:start + BULK_BATCH_SIZE * 2]) + b"\n"
        result = _request("POST", f"{INDEX}/_bulk?refresh=wait_for", payload)
        items = result.get("items", [])
        errors = [item for item in items if any(
            operation.get("error") for operation in item.values() if isinstance(operation, dict)
        )]
        if result.get("errors") or errors:
            detail = json.dumps(errors[:3], ensure_ascii=False)[:1000]
            raise RuntimeError(f"Elasticsearch bulk indexing failed: {detail or 'response reported errors'}")
        count += len(items)
    return count


def search(profile: str, query: str, *, kinds: list[str] | None = None, size: int = 8) -> list[dict[str, Any]]:
    """Return profile-matched candidates; callers must revalidate against runtime authority."""
    ensure_index()
    filters: list[dict[str, Any]] = [{"term": {"profile": profile}}]
    allowed = [k for k in (kinds or ["tool", "skill", "agent"]) if k in {"tool", "skill", "agent"}]
    if allowed:
        filters.append({"terms": {"kind": allowed}})
    result = _request("POST", f"{INDEX}/_search", {
        "size": max(1, min(int(size), 20)),
        "query": {"bool": {"filter": filters, "must": [{"multi_match": {
            "query": query[:1000], "fields": ["name^3", "description", "tags"]}}]}},
    })
    return [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]


def validate_agent_manifest(data: Any) -> dict[str, Any]:
    """Validate a non-executable reusable-agent YAML/JSON data manifest."""
    if not isinstance(data, dict) or set(data) - {"name", "description", "tags", "toolsets", "instructions"}:
        raise ValueError("agent manifest must be a mapping with only name, description, tags, toolsets, instructions")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("agent manifest requires a non-empty name")
    name = data["name"].strip()
    if name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
        raise ValueError("agent manifest name must be a single safe path component")
    for key in ("tags", "toolsets"):
        if key in data and (not isinstance(data[key], list) or not all(isinstance(x, str) for x in data[key])):
            raise ValueError(f"{key} must be a list of strings")
    if "instructions" in data and not isinstance(data["instructions"], str):
        raise ValueError("instructions must be plain text")
    return data


def agent_manifest_dir() -> Path:
    """Profile-isolated store for data-only reusable-agent manifests."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "agents" / "catalog"


def save_agent_manifest(data: Any) -> Path:
    """Persist a validated manifest as JSON data (never executable Python)."""
    manifest = validate_agent_manifest(data)
    path = agent_manifest_dir() / f"{urllib.parse.quote(manifest['name'], safe='')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def load_agent_manifests() -> list[dict[str, Any]]:
    """Read manifests from the active profile's isolated catalog directory."""
    results = []
    for path in agent_manifest_dir().glob("*.json"):
        try:
            results.append(validate_agent_manifest(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return results


def refresh_runtime_catalog(profile: str | None = None) -> int:
    """Upsert current runtime registrations, visible skill metadata and local manifests."""
    from tools.registry import registry as tool_registry
    from tools.skills_tool import _find_all_skills
    from hermes_constants import hermes_home_key
    profile = profile or hermes_home_key()
    from toolsets import resolve_toolset
    visible = set(resolve_toolset("hermes-cli")) | set(resolve_toolset("hermes-gateway"))
    records = [{"kind": "tool", "name": entry.name, "description": entry.description,
                "tags": [entry.toolset]} for entry in tool_registry.get_all_entries()
               if entry.name in visible]
    records += [{"kind": "skill", "name": item["name"], "description": item.get("description", ""),
                 "tags": [item.get("category") or ""]} for item in _find_all_skills()]
    records += [{"kind": "agent", "name": item["name"], "description": item.get("description", ""),
                 "tags": item.get("tags", []), "metadata": {"toolsets": item.get("toolsets", [])}}
                for item in load_agent_manifests()]
    return index_records(profile, records)


def search_runtime_catalog(query: str, *, kinds: list[str] | None = None, profile: str | None = None,
                           available_tool_names: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
    """Refresh then search ES, admitting only tool/skill results still present in authority."""
    from tools.registry import registry as tool_registry
    from hermes_constants import hermes_home_key
    profile = profile or hermes_home_key()
    refresh_runtime_catalog(profile)
    hits = search(profile, query, kinds=kinds)
    tools = set(tool_registry.get_all_tool_names())
    try:
        from tools.skills_tool import _find_all_skills
        skills = {item["name"] for item in _find_all_skills()}
    except Exception:
        skills = set()
    agents = {item["name"] for item in load_agent_manifests()}
    available_tools = set() if available_tool_names is None else tools & set(available_tool_names)
    return [hit for hit in hits if
            (hit.get("kind") == "tool" and hit.get("name") in available_tools) or
            (hit.get("kind") == "skill" and hit.get("name") in skills) or
            (hit.get("kind") == "agent" and hit.get("name") in agents)]
