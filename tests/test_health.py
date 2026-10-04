"""Host health: the optional `self_check` block of the authenticated status response.

Two layers are covered. The transport must forward the block unchanged and must
never let an absent or malformed block fail a status read. The panel's pure
presentation functions are run under Node, straight out of `Panel.qml`, against
the pinned Decky fixtures, the same way `test_security.py` replays its helper
functions.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from client import cli, client_core

ROOT = Path(__file__).resolve().parents[1]
STATUS_FIXTURES = ROOT / "protocol" / "fixtures" / "contract" / "status"
PIN = "sha256:" + "a" * 64
# The fixtures' fixed clock; the certificate runs 2026-09-13 11:59 to 2036-09-10 11:59.
NOW_MS = int(datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

LABELS = {
    "certificate": "Certificate",
    "listener": "Listener",
    "bridge": "Steam bridge",
    "temperature": "Temperature",
    "sunshine": "Sunshine",
    "versions": "Host",
}
# What a monitoring-off host with a usable bridge renders; each fixture then
# overrides only the row it exists to exercise.
HEALTHY = {
    "certificate": ("ok", "Valid until 2036-09-10 · …aaaaaaaa"),
    "listener": ("ok", "Listening on 0.0.0.0:18443"),
    "bridge": ("ok", "Ready · display, restart, shutdown, suspend"),
    "temperature": ("ok", "Read from system sensors"),
    "sunshine": ("off", "Monitoring off"),
    "versions": ("ok", "Plugin v0.0.0-fixture · protocol 1"),
}
BRIDGE_DOWN = ("warn", "Not ready — power and display control are unavailable")
FIXTURE_ROWS = {
    "sunshine-disabled": {},
    "sunshine-running": {"sunshine": ("ok", "Running · 1")},
    "sunshine-stopped": {"sunshine": ("warn", "Stopped · 1")},
    "sunshine-restarting": {"sunshine": ("warn", "Restarting · 1")},
    "sunshine-unknown": {"sunshine": ("unknown", "State unknown · 1")},
    "sunshine-owner-unavailable": {"sunshine": ("warn", "Stopped · process-observer-v1")},
    "steam-bridge-not-ready": {"bridge": BRIDGE_DOWN},
    "steam-bridge-unavailable": {"bridge": BRIDGE_DOWN},
    "self-check-certificate-unusable": {"certificate": ("warn", "Not ready — pairing is unavailable")},
    "self-check-plugin-version-unknown": {"versions": ("unknown", "Plugin version unknown · protocol 1")},
    "self-check-steam-temperature": {"temperature": ("ok", "Read from Steam")},
    "view-only-credential": {},
    "scopes-changed-by-owner": {},
}
NUDGE_OK = {"state": "ok", "text": "Host protocol 1 matches this client."}
NUDGE_ABSENT = {
    "state": "unknown",
    "text": "This host does not report its health. Update the Decky plugin on the host to see it here.",
}


def fixture_body(name: str) -> dict:
    return json.loads((STATUS_FIXTURES / f"{name}.json").read_text())["response"]["body"]


def self_check_fixture_names() -> list[str]:
    return sorted(path.stem for path in STATUS_FIXTURES.glob("*.json") if "self_check" in fixture_body(path.stem))


def healthy_check() -> dict:
    return json.loads(json.dumps(fixture_body("sunshine-running")["self_check"]))


def expected_rows(overrides: dict) -> list[tuple]:
    merged = {**HEALTHY, **overrides}
    return [(row_id, LABELS[row_id], *merged[row_id]) for row_id in LABELS]


def panel_block() -> str:
    panel = (ROOT / "Panel.qml").read_text()
    found = re.findall(r"// self-check:begin\n(.*?)  // self-check:end", panel, re.S)
    assert len(found) == 1, "Panel.qml must carry exactly one self-check block"
    return found[0]


HARNESS = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = vm.createContext({});
vm.runInContext(input.block, context);
const out = input.cases.map(item => {
  try {
    const rows = context.selfCheckRows(item.check, item.now);
    return {
      rows: JSON.parse(JSON.stringify(rows)),
      nudge: JSON.parse(JSON.stringify(context.selfCheckNudge(item.check, 1))),
    };
  } catch (error) {
    return {error: String(error)};
  }
});
const glyphs = {};
for (const state of ['ok', 'warn', 'off', 'unknown', 'nonsense']) glyphs[state] = context.selfCheckGlyph(state);
process.stdout.write(JSON.stringify({out, glyphs}));
"""


def run_panel(cases: list[dict]) -> dict:
    """Run selfCheckRows / selfCheckNudge from Panel.qml. A case without a `check` key passes undefined."""
    result = subprocess.run(
        ["node", "-e", HARNESS],
        input=json.dumps({"block": panel_block(), "cases": cases}),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def render(check, now: int = NOW_MS) -> dict:
    case = {"now": now} if check is Ellipsis else {"check": check, "now": now}
    out = run_panel([case])["out"][0]
    assert "error" not in out, out
    return out


def row_map(rendered: dict) -> dict:
    return {row["id"]: (row["state"], row["value"]) for row in rendered["rows"]}


class FakeResponse:
    status = 200

    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self, _limit):
        return self.raw

    def getheader(self, _name, default=None):
        return default


class FakeConnection:
    """Serves a canned body per request path, so the real transport code runs."""

    def __init__(self, bodies: dict[str, bytes]):
        self.sock = mock.Mock()
        self.sock.getpeercert.return_value = b"certificate"
        self.bodies = bodies
        self.path = ""

    def connect(self):
        return None

    def putrequest(self, _method, path, **_kwargs):
        self.path = path

    def putheader(self, *_args):
        return None

    def endheaders(self, *_args, **_kwargs):
        return None

    def getresponse(self):
        return FakeResponse(self.bodies[self.path])

    def close(self):
        return None


def serve(bodies: dict[str, dict | bytes]):
    encoded = {
        path: body if isinstance(body, bytes) else json.dumps(body).encode()
        for path, body in bodies.items()
    }
    connection = FakeConnection(encoded)
    return (
        mock.patch.object(client_core.http.client, "HTTPSConnection", return_value=connection),
        mock.patch.object(client_core, "fingerprint_der", return_value=PIN),
    )


def get(path: str, body: dict | bytes) -> dict:
    connection, pin = serve({path: body})
    with connection, pin:
        return client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", path)


class SelfCheckTransportTests(unittest.TestCase):
    def test_every_status_fixture_reaches_the_panel_unchanged(self):
        names = self_check_fixture_names()
        self.assertGreaterEqual(len(names), 11)
        for name in names:
            with self.subTest(name):
                body = fixture_body(name)
                self.assertEqual(get("/v1/status", body), body)

    def test_a_host_without_the_block_is_read_as_before(self):
        body = fixture_body("sunshine-running")
        del body["self_check"]
        result = get("/v1/status", body)
        self.assertEqual(result, body)
        self.assertNotIn("self_check", result)

    def test_absent_or_error_status_fixtures_carry_no_block(self):
        names = {path.stem for path in STATUS_FIXTURES.glob("*.json")}
        self.assertEqual(names - set(self_check_fixture_names()), {"rate-limited", "unauthorized-invalid", "unauthorized-missing"})

    def test_a_malformed_or_oversized_block_is_dropped_and_never_fails_status(self):
        deep = {}
        cursor = deep
        for _ in range(20):
            cursor["next"] = {}
            cursor = cursor["next"]
        bad = {
            "null": None,
            "string": "garbage",
            "number": 5,
            "list": [{"ready": True}],
            "long text": {"certificate": {"fingerprint_short": "x" * 2000}},
            "long list": {"bridge": {"ready": True, "methods": ["display"] * 300}},
            "wide": {f"k{index}": 0 for index in range(140)},
            "deep": deep,
            "many nodes": {"bridge": {"methods": [[0] * 200 for _ in range(30)]}},
        }
        for label, block in bad.items():
            with self.subTest(label):
                body = fixture_body("sunshine-running")
                body["self_check"] = block
                result = get("/v1/status", body)
                self.assertNotIn("self_check", result)
                expected = fixture_body("sunshine-running")
                del expected["self_check"]
                self.assertEqual(result, expected)

    def test_a_block_with_malformed_parts_inside_the_budgets_is_forwarded_for_the_panel_to_judge(self):
        body = fixture_body("sunshine-running")
        body["self_check"] = {"certificate": "x", "listener": 5, "bridge": [], "versions": {"protocol": "1"}}
        self.assertEqual(get("/v1/status", body), body)

    def test_response_limits_still_apply_everywhere_else(self):
        # The exemption is the block on the status route and nothing more.
        with self.assertRaises(client_core.ClientError):
            get("/v1/status", {"protocol_version": 1, "host_id": "x" * 2000, "self_check": "ok"})
        with self.assertRaises(client_core.ClientError):
            get("/v1/status", {"protocol_version": 1, "reason": "x" * 2000})
        with self.assertRaises(client_core.ClientError):
            get("/v1/display/outputs", {"protocol_version": 1, "self_check": {"note": "x" * 2000}})

    def test_status_wake_target_pairing_check_and_endpoint_correction_ignore_the_block(self):
        status = fixture_body("sunshine-running")
        status["self_check"] = "garbage"
        status["host_id"] = "host-test"
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read"],
            })
            connection, pin = serve({"/v1/status": status})
            with connection, pin, mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"):
                result = core.request("status", {})
                saved = core.configure_endpoint("https://host.example:18443")
            self.assertNotIn("self_check", result)
            self.assertEqual(result["wake_target"]["interface"], "enp5s0")
            self.assertTrue(saved["saved"])
            self.assertEqual(core.store.load()["wake_mac"], "001122334455")
            self.assertEqual(core.inspect()["host_id"], "host-test")

    def test_no_action_path_reads_the_block(self):
        source = (ROOT / "client" / "client_core.py").read_text()
        outside = re.sub(r"def _optional_self_check\(.*?\n\n\n", "", source, flags=re.S)
        self.assertEqual([line.strip() for line in outside.splitlines() if "self_check" in line], ["value = _optional_self_check(value)"])
        for name in ("cli.py", "remote_client.py"):
            self.assertNotIn("self_check", (ROOT / "client" / name).read_text(), name)

    def test_cli_status_prints_the_block_unchanged(self):
        body = fixture_body("sunshine-running")

        class StubCore:
            def run(self, request):
                assert request == {"action": "status"}
                return body

        for flag in ("--json", "--plain"):
            output = io.StringIO()
            with mock.patch.object(cli, "ClientCore", StubCore), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main([flag, "status"]), 0)
            if flag == "--json":
                self.assertEqual(json.loads(output.getvalue())["data"]["self_check"], body["self_check"])
            else:
                text = output.getvalue()
                self.assertIn("self_check.certificate.fingerprint_short: aaaaaaaa", text)
                self.assertIn("self_check.versions.plugin: 0.0.0-fixture", text)
                self.assertIn("self_check.bridge.methods[0]: display", text)

    def test_the_block_never_carries_secrets(self):
        forbidden = {
            "token", "secret", "mac", "pin", "nonce", "credential", "host_id", "boot_id", "pairing_session",
            "certificate_fingerprint", "wake_target", "interface", "source_address",
        }
        for name in self_check_fixture_names():
            check = fixture_body(name)["self_check"]
            keys = set()

            def collect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        keys.add(key)
                        collect(item)

            collect(check)
            self.assertFalse(keys & forbidden, name)
            text = json.dumps(check)
            for value in ("001122334455", "sha256:", "fixture-token", "host-fixture", "00000000-0000-4000"):
                self.assertNotIn(value, text, name)

    def test_pinned_schema_and_fixtures_agree_on_the_block_shape(self):
        schema = json.loads((ROOT / "protocol" / "schema.json").read_text())
        block = schema["$defs"]["self_check"]
        self.assertIn("self_check", schema["properties"])
        self.assertNotIn("self_check", schema.get("required", []))
        self.assertFalse(block["additionalProperties"])
        self.assertEqual(set(block["required"]), {"certificate", "listener", "bridge", "temperature", "provider", "versions"})
        for name in self_check_fixture_names():
            check = fixture_body(name)["self_check"]
            self.assertEqual(set(check), set(block["required"]), name)
            for part, definition in block["properties"].items():
                self.assertEqual(set(check[part]), set(definition["required"]), f"{name}/{part}")


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml presentation functions")
class SelfCheckPresentationTests(unittest.TestCase):
    def test_the_extracted_block_is_pure(self):
        block = panel_block()
        for forbidden in ("root.", "statusData", "enqueueHelper", "remote(", "Timer", "Date.now", "Qt."):
            self.assertNotIn(forbidden, block)
        self.assertIn("function selfCheckRows(check, nowMs)", block)
        self.assertIn("function selfCheckNudge(check, supported)", block)

    def test_every_status_fixture_renders_the_expected_rows(self):
        names = self_check_fixture_names()
        # A fixture added by a later re-pin must be given an expectation here.
        self.assertEqual(set(names), set(FIXTURE_ROWS))
        results = run_panel([{"check": fixture_body(name)["self_check"], "now": NOW_MS} for name in names])["out"]
        for name, rendered in zip(names, results):
            with self.subTest(name):
                actual = [(row["id"], row["label"], row["state"], row["value"]) for row in rendered["rows"]]
                self.assertEqual(actual, expected_rows(FIXTURE_ROWS[name]))
                self.assertEqual(rendered["nudge"], NUDGE_OK)

    def test_sunshine_running_is_confirmed_only_when_fresh(self):
        check = healthy_check()
        self.assertEqual(row_map(render(check))["sunshine"], ("ok", "Running · 1"))
        check["provider"]["stale"] = True
        self.assertEqual(row_map(render(check))["sunshine"], ("unknown", "Running (stale) · 1"))
        check["provider"]["state"] = "stopped"
        self.assertEqual(row_map(render(check))["sunshine"], ("warn", "Stopped (stale) · 1"))
        check["provider"].update(state="unavailable", stale=False)
        self.assertEqual(row_map(render(check))["sunshine"], ("unknown", "Owner unavailable · 1"))
        check["provider"]["contract_version"] = None
        self.assertEqual(row_map(render(check))["sunshine"], ("unknown", "Owner unavailable"))
        check["provider"].update(monitor_enabled=False, state="running")
        self.assertEqual(row_map(render(check))["sunshine"], ("off", "Monitoring off"))

    def test_certificate_rows_follow_the_clock_and_say_what_to_do(self):
        check = healthy_check()
        after = int(datetime(2037, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        before = int(datetime(2026, 9, 13, 11, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(row_map(render(check, after))["certificate"], ("warn", "Expired 2036-09-10 · …aaaaaaaa"))
        self.assertEqual(
            row_map(render(check, before))["certificate"],
            ("warn", "Not valid until 2026-09-13 — check this device's clock · …aaaaaaaa"),
        )
        check["certificate"].update(not_before=None, not_after=None)
        self.assertEqual(row_map(render(check))["certificate"], ("ok", "Ready · …aaaaaaaa"))
        check["certificate"]["fingerprint_short"] = None
        self.assertEqual(row_map(render(check))["certificate"], ("ok", "Ready"))
        # Unparseable dates are not dates: no invented expiry, no claim of validity.
        check["certificate"].update(not_before="2026-13-45T99:99:99Z", not_after="soon")
        self.assertEqual(row_map(render(check))["certificate"], ("ok", "Ready"))
        # An unusable clock cannot judge the window; it must not call the certificate not yet valid.
        check = healthy_check()
        for clock in (None, "now"):
            self.assertEqual(row_map(render(check, clock))["certificate"], ("ok", "Valid until 2036-09-10 · …aaaaaaaa"))

    def test_listener_bridge_and_temperature_rows(self):
        check = healthy_check()
        check["listener"] = {"bound": False, "address": None, "port": None}
        check["bridge"] = {"ready": True, "methods": []}
        check["temperature"]["source"] = "unavailable"
        rows = row_map(render(check))
        self.assertEqual(rows["listener"], ("off", "Not listening"))
        self.assertEqual(rows["bridge"], ("ok", "Ready"))
        self.assertEqual(rows["temperature"], ("off", "No sensor reading"))
        # Same fallback wording as the Decky card when a bound listener omits its address.
        check["listener"] = {"bound": True, "address": None, "port": None}
        self.assertEqual(row_map(render(check))["listener"], ("ok", "Listening on all interfaces:18443"))

    def test_the_plugin_version_is_never_guessed(self):
        check = healthy_check()
        for plugin in ("unknown", "", None, 3):
            check["versions"]["plugin"] = plugin
            self.assertEqual(row_map(render(check))["versions"], ("unknown", "Plugin version unknown · protocol 1"), plugin)
        check["versions"].update(plugin="1.2.3", protocol=None)
        self.assertEqual(row_map(render(check))["versions"], ("ok", "Plugin v1.2.3 · protocol unknown"))

    def test_the_protocol_nudge_compares_with_what_this_client_speaks(self):
        check = healthy_check()
        expected = {
            2: ("newer", "Host protocol 2 is newer than this client supports — some actions may be unavailable. Update this plugin."),
            1: ("ok", "Host protocol 1 matches this client."),
            0: ("older", "Host protocol 0 is older than this client — update the plugin on the host."),
        }
        for protocol, (state, text) in expected.items():
            check["versions"]["protocol"] = protocol
            self.assertEqual(render(check)["nudge"], {"state": state, "text": text}, protocol)
        for protocol in ("1", None, 1.5, [1], True):
            check["versions"]["protocol"] = protocol
            self.assertEqual(render(check)["nudge"], NUDGE_ABSENT, protocol)

    def test_a_stripped_block_renders_no_rows_and_the_update_nudge(self):
        for check in (Ellipsis, None):
            rendered = render(check)
            self.assertEqual(rendered["rows"], [])
            self.assertEqual(rendered["nudge"], NUDGE_ABSENT)
        for name in ("rate-limited", "unauthorized-invalid", "unauthorized-missing"):
            body = fixture_body(name)
            self.assertNotIn("self_check", body)
            self.assertEqual(render(body.get("self_check", Ellipsis))["rows"], [])

    def test_a_malformed_block_never_throws(self):
        for check in ("garbage", 5, True, [], [1, 2], {}):
            rendered = render(check)
            self.assertEqual(rendered["nudge"], NUDGE_ABSENT, check)
            self.assertEqual([row["id"] for row in rendered["rows"]], [] if not isinstance(check, dict) else list(LABELS), check)
        cautious = row_map(render({"certificate": "x", "listener": 5, "bridge": [], "temperature": None, "provider": [1], "versions": "v"}))
        self.assertEqual(cautious, {
            "certificate": ("warn", "Not ready — pairing is unavailable"),
            "listener": ("off", "Not listening"),
            "bridge": ("warn", "Not ready — power and display control are unavailable"),
            "temperature": ("off", "No sensor reading"),
            "sunshine": ("off", "Monitoring off"),
            "versions": ("unknown", "Plugin version unknown · protocol unknown"),
        })
        junk = [None, True, False, 0, 1.5, -1, "", "x", [], [None], [{}], {}, {"ready": "yes"}, {"source": ["steam"]}, {"methods": "display"}]
        cases = []
        for part in ("certificate", "listener", "bridge", "temperature", "provider", "versions"):
            for value in junk:
                check = healthy_check()
                check[part] = value
                cases.append({"check": check, "now": NOW_MS})
        for field in ("not_before", "not_after", "fingerprint_short", "ready"):
            for value in junk:
                check = healthy_check()
                check["certificate"][field] = value
                cases.append({"check": check, "now": NOW_MS})
        cases.append({"check": healthy_check(), "now": None})
        for index, rendered in enumerate(run_panel(cases)["out"]):
            self.assertNotIn("error", rendered, index)
            self.assertEqual([row["id"] for row in rendered["rows"]], list(LABELS), index)
            for row in rendered["rows"]:
                self.assertIn(row["state"], {"ok", "warn", "off", "unknown"})
                self.assertNotIn("undefined", row["value"])
                self.assertNotIn("null", row["value"])
                self.assertNotIn("NaN", row["value"])

    def test_text_is_capped_at_the_schema_bounds(self):
        check = healthy_check()
        check["certificate"]["fingerprint_short"] = "f" * 5000
        check["listener"]["address"] = "a" * 5000
        check["bridge"]["methods"] = ["display", 3, None, {}, "m" * 500] + ["extra"] * 300
        check["provider"].update(contract_version="c" * 5000)
        check["versions"]["plugin"] = "p" * 5000
        rows = {row["id"]: row for row in render(check)["rows"]}
        self.assertEqual(rows["certificate"]["value"], "Valid until 2036-09-10 · …" + "f" * 8)
        self.assertEqual(rows["listener"]["value"], "Listening on " + "a" * 64 + ":18443")
        self.assertEqual(rows["bridge"]["value"], "Ready · display, " + "m" * 32 + ", " + ", ".join(["extra"] * 6))
        self.assertEqual(rows["sunshine"]["value"], "Running · " + "c" * 64)
        self.assertEqual(rows["versions"]["value"], "Plugin v" + "p" * 64 + " · protocol 1")

    def test_every_state_has_a_glyph_and_the_row_text_states_it_too(self):
        glyphs = run_panel([])["glyphs"]
        self.assertEqual(glyphs, {"ok": "✓", "warn": "⊘", "off": "○", "unknown": "○", "nonsense": "○"})
        # The glyph never carries a state alone: warn and off rows say it in words.
        for name in self_check_fixture_names():
            for row in render(fixture_body(name)["self_check"])["rows"]:
                if row["state"] in {"warn", "off"}:
                    self.assertRegex(row["value"], r"Not |Monitoring off|No sensor|Stopped|Restarting|Expired", row["id"])


class PanelWiringTests(unittest.TestCase):
    """Static checks that the group stays a read of the polled status, and sits where the brief put it."""

    def group(self) -> str:
        panel = (ROOT / "Panel.qml").read_text()
        return panel.split("id: healthGroup", 1)[1].split("PanelSeparator", 1)[0]

    def test_group_is_in_connection_details_and_reads_only_polled_status(self):
        panel = (ROOT / "Panel.qml").read_text()
        details = panel.index('PanelSectionHeader { text: "Connection details"')
        health = panel.index('PanelSectionHeader { text: "Host health"')
        preferences = panel.index('PanelSectionHeader { text: "Display preferences"')
        self.assertLess(details, health)
        self.assertLess(health, preferences)
        group = self.group()
        self.assertIn("root.statusData.self_check", group)
        self.assertIn("root.selfCheckNudge(healthCheck, root.hostProtocolVersion)", group)
        for forbidden in ("enqueueHelper", "remote(", "Timer", "Qt.callLater", "refresh("):
            self.assertNotIn(forbidden, group)

    def test_group_never_claims_the_host_lacks_the_block_before_the_host_has_answered(self):
        group = self.group()
        self.assertIn("visible: !root.statusData", group)
        self.assertIn("!!root.statusData && healthGroup.healthNudge.state", group)

    def test_client_protocol_matches_the_transport(self):
        panel = (ROOT / "Panel.qml").read_text()
        self.assertIn(f"readonly property int hostProtocolVersion: {client_core.PROTOCOL_VERSION}", panel)


if __name__ == "__main__":
    unittest.main()
