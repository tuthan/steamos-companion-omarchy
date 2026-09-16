from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ContractTests(unittest.TestCase):
    def test_pinned_contract_bundle_is_complete_and_versioned(self):
        schema = json.loads((ROOT / "protocol" / "schema.json").read_text())
        self.assertEqual(schema["$id"], "https://steamos-remote.local/protocol/v1/schema.json")
        fixtures = sorted((ROOT / "protocol" / "fixtures").glob("*.json"))
        self.assertGreaterEqual(len(fixtures), 6)
        for fixture in fixtures:
            self.assertEqual(json.loads(fixture.read_text())["protocol_version"], 1, fixture.name)

    def test_manifest_uses_native_bar_widget_entry_point(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["version"], "0.5.3")
        self.assertEqual(manifest["kinds"], ["bar-widget"])
        self.assertEqual(manifest["entryPoints"]["barWidget"], "BarWidget.qml")
        self.assertFalse(manifest["barWidget"]["allowMultiple"])

    def test_remote_icon_is_packaged_and_sunshine_is_capability_gated(self):
        icon = ROOT / "assets" / "steamos-remote-icon.svg"
        self.assertTrue(icon.is_file())
        self.assertIn("#f2f4f5", icon.read_text())
        self.assertNotIn("linearGradient", icon.read_text())
        panel = (ROOT / "Panel.qml").read_text()
        self.assertIn("statusData.sunshine.enabled === true", panel)

    def test_display_order_is_additive_and_draft_safe(self):
        schema = json.loads((ROOT / "protocol" / "schema.json").read_text())
        self.assertIn("display_order", schema["properties"])
        order = schema["$defs"]["display_order"]
        self.assertIn("output_keys", order["required"])
        self.assertIn("saved_output_keys", order["required"])
        panel = (ROOT / "Panel.qml").read_text()
        for text in (
            "Display order",
            "Move up",
            "Move down",
            "Save for next session",
            "Save and restart Gaming Mode",
            "Use automatic display order",
            "Refresh display order",
            "displayOrderDraftStale",
            "display-order:up:",
        ):
            self.assertIn(text, panel)

    def test_display_order_fixtures_cover_capability_and_fallback_states(self):
        fixtures = {
            path.stem: json.loads(path.read_text())["display_order"]
            for path in (ROOT / "protocol" / "fixtures").glob("display-order-*.json")
        }
        self.assertTrue(fixtures["display-order-supported"]["available"])
        self.assertTrue(fixtures["display-order-stale"]["stale"])
        self.assertTrue(fixtures["display-order-unsupported"]["unsupported"])
        fallback = fixtures["display-order-saved-fallback"]
        self.assertTrue(any(output["connected"] is False for output in fallback["outputs"]))
        self.assertIn(fallback["outputs"][2]["output_key"], fallback["saved_output_keys"])


if __name__ == "__main__":
    unittest.main()
