"""Live/local contracts for the profile-scoped discovery catalog."""
import unittest

from agent.discovery_index import (index_records, search, validate_agent_manifest,
                                   save_agent_manifest, load_agent_manifests, search_runtime_catalog)


class DiscoveryContracts(unittest.TestCase):
    def test_index_records_uses_bulk_ndjson_and_counts_successes(self):
        import json
        from unittest.mock import patch

        records = [{"kind": "tool", "name": "one", "description": "café"},
                   {"kind": "skill", "name": "two"}, {"kind": "invalid", "name": "skip"}]
        def bulk(method, path, payload=None):
            self.assertEqual((method, path), ("POST", "hermes-discovery-v1/_bulk?refresh=wait_for"))
            self.assertIsInstance(payload, bytes)
            lines = payload.decode().splitlines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(json.loads(lines[0])["index"]["_index"], "hermes-discovery-v1")
            self.assertEqual(json.loads(lines[1])["profile"], "profile-a")
            self.assertEqual(json.loads(lines[1])["description"], "café")
            return {"errors": False, "items": [{"index": {"status": 201}}, {"index": {"status": 201}}]}

        with patch("agent.discovery_index.ensure_index"), patch("agent.discovery_index._request", side_effect=bulk):
            self.assertEqual(index_records("profile-a", records), 2)

    def test_index_records_reports_bulk_item_errors(self):
        from unittest.mock import patch

        response = {"errors": True, "items": [{"index": {"status": 400, "error": {"type": "mapper_parsing_exception"}}}]}
        with patch("agent.discovery_index.ensure_index"), patch("agent.discovery_index._request", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "mapper_parsing_exception"):
                index_records("profile-a", [{"kind": "tool", "name": "bad"}])

    def test_manifest_is_data_only_and_validated(self):
        valid = {"name": "research-helper", "toolsets": ["web"], "instructions": "Research."}
        self.assertEqual(validate_agent_manifest(valid), valid)
        with self.assertRaises(ValueError):
            validate_agent_manifest({"name": "bad", "python": "import os"})
        with self.assertRaises(ValueError):
            validate_agent_manifest({"name": "bad", "toolsets": "terminal"})

    def test_manifest_persistence_and_search_revalidates_authorities(self):
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from tools.registry import registry
        import tools.skills_tool

        with TemporaryDirectory() as home:
            with patch("hermes_constants.get_hermes_home", return_value=Path(home)):
                saved = save_agent_manifest({"name": "stored-agent", "description": "catalog finder"})
                self.assertTrue(saved.is_file())
                self.assertEqual(load_agent_manifests()[0]["name"], "stored-agent")
        with patch("agent.discovery_index.refresh_runtime_catalog", return_value=1), \
             patch("agent.discovery_index.search", return_value=[
                 {"kind": "tool", "name": "no-longer-registered"},
                 {"kind": "agent", "name": "stored-agent"}]), \
             patch.object(registry, "get_all_tool_names", return_value=[]), \
             patch("tools.skills_tool._find_all_skills", return_value=[]), patch("agent.discovery_index.load_agent_manifests", return_value=[{"name": "stored-agent"}]):
            self.assertEqual([x["kind"] for x in search_runtime_catalog("catalog")], ["agent"])

    def test_runtime_search_failure_is_not_silenced(self):
        from unittest.mock import patch
        with patch("agent.discovery_index.refresh_runtime_catalog", return_value=0), \
             patch("agent.discovery_index.search", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                search_runtime_catalog("query")

    def test_live_elasticsearch_profile_isolation_and_search(self):
        import uuid
        from contextlib import suppress
        from agent.discovery_index import INDEX, _request

        profile_a = f"test-profile-a-{uuid.uuid4().hex}"
        profile_b = f"test-profile-b-{uuid.uuid4().hex}"
        records = [
            {"kind": "tool", "name": "discovery_probe_tool", "description": "Find indexed reusable agent capabilities", "tags": ["probe"]},
            {"kind": "agent", "name": "discovery_probe_agent", "description": "Reusable catalog query helper", "tags": ["probe"]},
        ]
        try:
            self.assertEqual(index_records(profile_a, records), 2)
            found = search(profile_a, "reusable catalog query helper")
            self.assertTrue(any(hit["name"] == "discovery_probe_agent" for hit in found), found)
            self.assertEqual(search(profile_b, "reusable catalog query helper"), [])
        finally:
            with suppress(Exception):
                _request("POST", f"{INDEX}/_delete_by_query?refresh=true", {
                    "query": {"terms": {"profile": [profile_a, profile_b]}}
                })


if __name__ == "__main__":
    unittest.main()
