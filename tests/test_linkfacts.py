"""Advisory DRM link facts: the optional `GET /v1/drm` read and its warning.

Three layers are covered. The transport must forward only the allowlisted facts
and must turn a 404, an error, or an oversize or malformed block into "no facts"
without raising and without touching status handling. The panel's pure functions
are run under Node straight out of `Panel.qml` against the pinned Decky fixtures,
the same way `test_health.py` runs its block. Static checks pin where the read
is triggered and that the markup reads only what that read cached.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import cli, client_core

ROOT = Path(__file__).resolve().parents[1]
DRM_FIXTURES = ROOT / "protocol" / "fixtures" / "contract" / "drm"
STATUS_FIXTURES = ROOT / "protocol" / "fixtures" / "contract" / "status"
DECKY_FRONTEND = Path("/home/hvo/Projects/steamos-companion-decky/host/frontend/index.js")
PIN = "sha256:" + "a" * 64

# The wording and the link line both clients show. Pinned here as literals so the
# check holds without the sibling checkout; the parity test below compares them
# with the Decky frontend when it is present.
ADVISORY_TEXT = (
    "Heads up · This resolution is not in the display link's mode list. "
    "It may still work, or the display may show no signal. "
    "The preview reverts on its own unless you keep it."
)
FIXTURE_NAMES = ["ambiguous-connector", "available", "bridge-unavailable", "no-connectors", "unauthorized"]
LINES = {
    ("available", "DP-1"): "DP-1 · card0 · DELL U2723QE · DEL model 41340",
    ("available", "HDMI-A-1"): "HDMI-A-1 · card0 · LG TV SSCR2 · GSM model 49780",
    ("available", "HDMI-A-2"): "HDMI-A-2 · card0",
    ("available", "eDP-1"): "eDP-1 · card0",
}


def fixture(name: str) -> dict:
    return json.loads((DRM_FIXTURES / f"{name}.json").read_text())


def drm_body(name: str) -> dict:
    return fixture(name)["response"]["body"]


def drm_block(name: str) -> dict:
    return copy.deepcopy(drm_body(name)["drm"])


def with_link(patch: dict, name: str = "available", connector: str = "HDMI-A-1") -> dict:
    block = drm_block(name)
    for item in block["connectors"]:
        if item["connector"] == connector:
            item.update(patch)
    return block


def mode(width, height, refresh=60) -> dict:
    return {"id": f"mode-{width}x{height}", "width": width, "height": height, "refresh_hz": refresh}


def panel_source() -> str:
    return (ROOT / "Panel.qml").read_text()


def panel_block() -> str:
    found = re.findall(r"// link-facts:begin\n(.*?)  // link-facts:end", panel_source(), re.S)
    assert len(found) == 1, "Panel.qml must carry exactly one link-facts block"
    return found[0]


UNDEFINED = {"__undefined__": True}

HARNESS = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'), (key, value) => (value && value.__undefined__ === true ? undefined : value));
const context = vm.createContext({});
vm.runInContext(input.block, context);
const out = input.calls.map(([name, args]) => {
  try {
    const value = context[name](...args);
    return {value: value === undefined ? null : JSON.parse(JSON.stringify(value))};
  } catch (error) {
    return {error: String(error)};
  }
});
process.stdout.write(JSON.stringify(out));
"""

PARITY_HARNESS = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const mine = vm.createContext({});
vm.runInContext(input.block, mine);
const decky = vm.runInNewContext(`${input.decky}; ({linkFactsFor, linkAdvisory, linkFactsLine, LINK_ADVISORY_TEXT})`, {});
const out = input.cases.map(item => ({
  advisory: [mine.linkAdvisory(item.drm, item.output, item.mode), decky.linkAdvisory(item.drm, item.output, item.mode)],
  line: [mine.linkFactsLine(item.link), decky.linkFactsLine(item.link)],
}));
process.stdout.write(JSON.stringify({text: [mine.linkAdvisoryText(), decky.LINK_ADVISORY_TEXT], out}));
"""


def run_node(harness: str, payload: dict):
    result = subprocess.run(["node", "-e", harness], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def call_all(calls: list[tuple]) -> list:
    """Run (function, *args) calls out of Panel.qml; every one must return rather than throw."""
    out = run_node(HARNESS, {"block": panel_block(), "calls": [[name, list(args)] for name, *args in calls]})
    for index, item in enumerate(out):
        assert "error" not in item, (calls[index], item)
    return [item["value"] for item in out]


def call(name: str, *args):
    return call_all([(name, *args)])[0]


class FakeResponse:
    def __init__(self, raw: bytes, status: int = 200):
        self.raw = raw
        self.status = status

    def read(self, _limit):
        return self.raw

    def getheader(self, _name, default=None):
        return default


class FakeConnection:
    """Serves a canned body per request path and records what was asked."""

    def __init__(self, bodies: dict[str, bytes], status: int = 200):
        self.sock = mock.Mock()
        self.sock.getpeercert.return_value = b"certificate"
        self.bodies = bodies
        self.status = status
        self.requests: list[tuple[str, str, dict, object]] = []
        self._pending: tuple[str, str, dict] = ("", "", {})

    def connect(self):
        return None

    def putrequest(self, method, path, **_kwargs):
        self._pending = (method, path, {})

    def putheader(self, key, value):
        self._pending[2][key] = value

    def endheaders(self, body=None, **_kwargs):
        self.requests.append((*self._pending, body))

    def getresponse(self):
        path = self.requests[-1][1]
        return FakeResponse(self.bodies[path], self.status)

    def close(self):
        return None


def serve(bodies: dict[str, dict | bytes], status: int = 200):
    encoded = {path: body if isinstance(body, bytes) else json.dumps(body).encode() for path, body in bodies.items()}
    connection = FakeConnection(encoded, status)
    cls = mock.Mock(return_value=connection)
    return connection, mock.patch.object(client_core.http.client, "HTTPSConnection", cls), mock.patch.object(client_core, "fingerprint_der", return_value=PIN), cls


def get(path: str, body: dict | bytes, status: int = 200) -> dict:
    _, connection, pin, _ = serve({path: body}, status)
    with connection, pin:
        return client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", path)


def paired_core(directory: str) -> client_core.ClientCore:
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
    return core


def link_facts(core: client_core.ClientCore, body: dict | bytes, status: int = 200, timeout=None) -> dict:
    _, connection, pin, _ = serve({"/v1/drm": body}, status)
    arguments = {} if timeout is None else {"timeout": timeout}
    with connection, pin:
        return core.request("link-facts", arguments)


NO_FACTS = {"protocol_version": 1, "drm": None}


class LinkFactsTransportTests(unittest.TestCase):
    def test_the_five_pinned_fixtures_are_all_covered(self):
        self.assertEqual(sorted(path.stem for path in DRM_FIXTURES.glob("*.json")), FIXTURE_NAMES)

    def test_every_successful_fixture_reaches_the_panel_unchanged(self):
        for name in ("available", "ambiguous-connector", "bridge-unavailable", "no-connectors"):
            with self.subTest(name):
                body = drm_body(name)
                self.assertEqual(get("/v1/drm", body), body)

    def test_the_unauthorized_fixture_is_an_error_to_the_transport_and_no_facts_to_the_panel(self):
        body = drm_body("unauthorized")
        with self.assertRaises(client_core.ClientError) as caught:
            get("/v1/drm", body, 401)
        self.assertEqual(caught.exception.status, 401)
        with tempfile.TemporaryDirectory() as directory:
            result = link_facts(paired_core(directory), body, 401)
        self.assertEqual(result, {**NO_FACTS, "unavailable": "error"})

    def test_a_host_without_the_route_answers_404_and_that_is_no_facts_not_an_error(self):
        not_found = {"error": "not_found", "message": "not found"}
        with self.assertRaises(client_core.ClientError) as caught:
            get("/v1/drm", not_found, 404)
        self.assertEqual(caught.exception.status, 404)
        with tempfile.TemporaryDirectory() as directory:
            result = link_facts(paired_core(directory), not_found, 404)
        self.assertEqual(result, {**NO_FACTS, "unavailable": "not_found"})

    def test_a_good_read_through_the_core_carries_only_the_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            result = link_facts(paired_core(directory), drm_body("available"))
        self.assertEqual(result, drm_body("available"))
        self.assertNotIn("unavailable", result)

    def test_the_request_is_one_authenticated_get_with_no_body_and_no_query(self):
        connection, patched, pin, cls = serve({"/v1/drm": drm_body("available")})
        with tempfile.TemporaryDirectory() as directory, patched, pin:
            paired_core(directory).request("link-facts", {"timeout": 2.5})
        self.assertEqual(len(connection.requests), 1)
        method, path, headers, body = connection.requests[0]
        self.assertEqual((method, path, body), ("GET", "/v1/drm", None))
        self.assertEqual(headers["Authorization"], "Bearer token-value")
        self.assertNotIn("Content-Length", headers)
        # The panel's poll deadline reaches the connection.
        self.assertEqual(cls.call_args.kwargs["timeout"], 2.5)

    def test_the_route_allowlist_admits_only_the_exact_path_and_no_body(self):
        for path in ("/v1/drm?output=1", "/v1/drm/", "/v1/drm/extra", "/v1/drms", "/v1/drm "):
            with self.subTest(path), self.assertRaises(client_core.ClientError):
                client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", path)
        with self.assertRaises(client_core.ClientError):
            client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", "/v1/drm", {"a": 1})
        source = (ROOT / "client" / "client_core.py").read_text()
        # One route string in the transport's own branch and one in the single reader.
        self.assertEqual(source.count('"/v1/drm"'), 2)
        self.assertEqual(source.count('"link-facts"'), 1)

    def test_a_malformed_or_oversized_block_is_dropped_and_never_fails_the_read(self):
        def tweak(**changes):
            block = drm_block("available")
            block.update(changes)
            return block

        def connector(**changes):
            block = drm_block("available")
            block["connectors"][1].update(changes)
            return block

        deep: list = []
        cursor = deep
        for _ in range(40):
            cursor.append([])
            cursor = cursor[0]
        bad = {
            "null": None,
            "string": "garbage",
            "number": 5,
            "list": [{"available": True}],
            "available missing": {key: value for key, value in drm_block("available").items() if key != "available"},
            "available as text": tweak(available="yes"),
            "connectors missing": {key: value for key, value in drm_block("available").items() if key != "connectors"},
            "connectors as object": tweak(connectors={}),
            "17 connectors": tweak(connectors=[drm_block("available")["connectors"][1]] * 17),
            "257 resolutions": connector(resolutions=[f"{1000 + index}x1000" for index in range(257)]),
            "connector entry not an object": tweak(connectors=["HDMI-A-1"]),
            "connector name": connector(connector="HDMI A 1"),
            "connector name too long": connector(connector="H" * 65),
            "gpu id": connector(gpu_id="card0"),
            "gpu id digits": connector(gpu_id="drm:card12345"),
            "display name empty": connector(display_name=""),
            "display name too long": connector(display_name="n" * 129),
            "display name control characters": connector(display_name="line\nbreak"),
            "vendor lowercase": connector(monitor_vendor="gsm"),
            "vendor too long": connector(monitor_vendor="GSMX"),
            "product out of range": connector(monitor_product_id=65536),
            "product negative": connector(monitor_product_id=-1),
            "product bool": connector(monitor_product_id=True),
            "product text": connector(monitor_product_id="49780"),
            "connected as text": connector(connected="true"),
            "connected missing": connector(connected=None),
            "internal as number": connector(is_internal=0),
            "resolutions as text": connector(resolutions="3840x2160"),
            "resolution not a string": connector(resolutions=[3840]),
            "resolution wrong separator": connector(resolutions=["3840×2160"]),
            "resolution zero": connector(resolutions=["0x0"]),
            "resolution trailing space": connector(resolutions=["3840x2160 "]),
            "resolution trailing newline": connector(resolutions=["3840x2160\n"]),
            "resolution too wide": connector(resolutions=["123456x1"]),
            "steam id empty": connector(steam_output_id=""),
            "steam id too long": connector(steam_output_id="s" * 129),
            "steam id number": connector(steam_output_id=1),
            "observed_at too long": tweak(observed_at="t" * 65),
            "reason too long": tweak(reason="r" * 257),
            "reason as list": tweak(reason=["x"]),
            "deeply nested connector entry": tweak(connectors=[deep]),
        }
        # Dropping the block is the whole effect; the rest of the read still answers.
        for label, block in bad.items():
            with self.subTest(label):
                self.assertEqual(get("/v1/drm", {"protocol_version": 1, "drm": block}), {"protocol_version": 1})
                with tempfile.TemporaryDirectory() as directory:
                    reply = link_facts(paired_core(directory), {"protocol_version": 1, "drm": block})
                self.assertEqual(reply, {**NO_FACTS, "unavailable": "invalid"})

    def test_a_body_that_is_not_json_or_has_the_wrong_protocol_is_no_facts(self):
        raw = {
            "not json": b"<html>nope</html>",
            "not an object": b"[1, 2, 3]",
            "invalid utf-8": b"\xff\xfe\x00",
            "oversize": b"{" + b" " * (client_core.MAX_BODY_BYTES + 10),
        }
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            for label, body in raw.items():
                with self.subTest(label):
                    self.assertEqual(link_facts(core, body), {**NO_FACTS, "unavailable": "error"})
            for label, body in {"newer protocol": {**drm_body("available"), "protocol_version": 2}, "no protocol": {"drm": drm_block("available")}}.items():
                with self.subTest(label):
                    self.assertEqual(link_facts(core, body), {**NO_FACTS, "unavailable": "error"})

    def test_any_failure_inside_the_read_is_no_facts(self):
        class Boom:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise error

        for error in (RuntimeError("boom"), RecursionError(), OSError("down"), client_core.ClientError("nope", status=500), ValueError("x")):
            with self.subTest(type(error).__name__), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(client_core, "PinnedTransport", Boom):
                    result = paired_core(directory).request("link-facts", {})
                self.assertEqual(result["drm"], None)
                self.assertIn(result["unavailable"], {"error", "not_found"})

    def test_a_maximum_size_reply_is_forwarded_whole(self):
        # 16 connectors of 256 resolutions overflow the generic 4096-node budget, so
        # the block carries its own bounds instead.
        block = {
            "available": True, "observed_at": "2026-09-13T12:00:00Z", "reason": None,
            "connectors": [
                {
                    "connector": f"HDMI-A-{index}", "gpu_id": "drm:card0", "display_name": f"Display {index}",
                    "monitor_vendor": "GSM", "monitor_product_id": index, "is_internal": False, "connected": True,
                    "resolutions": [f"{1000 + number}x{2000 + number}" for number in range(256)],
                    "steam_output_id": str(index),
                }
                for index in range(16)
            ],
        }
        body = {"protocol_version": 1, "drm": block}
        self.assertLess(len(json.dumps(body)), client_core.MAX_BODY_BYTES)
        with self.assertRaises(client_core.ClientError):
            client_core.validate_response_limits(body)
        self.assertEqual(get("/v1/drm", body), body)

    def test_only_the_allowlisted_fields_are_forwarded(self):
        body = drm_body("available")
        body["token"] = "planted-token"
        body["drm"]["edid_raw"] = "planted-edid-dump"
        body["drm"]["serial_number"] = "planted-serial"
        for item in body["drm"]["connectors"]:
            item.update(edid="planted-edid", raw_edid="AP///wA", serial="planted-connector-serial", monitor_serial="S/N 123", mac="aa:bb:cc:dd:ee:ff", modes=[{"width": 1, "height": 1, "refresh": 60}])
        result = get("/v1/drm", body)
        self.assertEqual(result, drm_body("available"))
        text = json.dumps(result)
        for planted in ("planted", "AP///wA", "S/N 123", "aa:bb:cc:dd:ee:ff"):
            self.assertNotIn(planted, text)
        schema = json.loads((ROOT / "protocol" / "schema.json").read_text())
        allowed = set(schema["$defs"]["drm"]["properties"]["connectors"]["items"]["properties"])
        for item in result["drm"]["connectors"]:
            self.assertEqual(set(item), allowed)
        self.assertEqual(set(result["drm"]), set(schema["$defs"]["drm"]["properties"]))
        self.assertEqual(set(result), {"protocol_version", "drm"})

    def test_nothing_the_client_holds_is_in_what_the_panel_receives(self):
        with tempfile.TemporaryDirectory() as directory:
            text = json.dumps(link_facts(paired_core(directory), drm_body("available")))
        for secret in ("token-value", PIN, "host-test", "client-test", "sha256:", "Bearer"):
            self.assertNotIn(secret, text)

    def test_the_fixtures_carry_no_secret_edid_or_serial_keys(self):
        forbidden = {"edid", "raw_edid", "serial", "serial_number", "monitor_serial", "mac", "token", "secret", "pin", "nonce", "credential", "host_id", "modes"}
        for name in FIXTURE_NAMES:
            keys: set[str] = set()

            def collect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        keys.add(key)
                        collect(item)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)

            collect(fixture(name)["response"]["body"])
            self.assertFalse(keys & forbidden, name)

    def test_a_failed_read_leaves_status_handling_and_private_state_alone(self):
        status = json.loads((STATUS_FIXTURES / "sunshine-running.json").read_text())["response"]["body"]
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            before = core.store.load()
            for body, code in ((b"junk", 200), ({"error": "not_found"}, 404), ({"protocol_version": 1, "drm": "x" * 5000}, 200)):
                link_facts(core, body, code)
            self.assertEqual(core.store.load(), before)
            self.assertEqual(core.inspect()["host_id"], "host-test")
            _, connection, pin, _ = serve({"/v1/status": status})
            with connection, pin, mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"):
                result = core.request("status", {})
            self.assertEqual(result["steam_bridge"], status["steam_bridge"])
            self.assertEqual(result["wake_target"]["interface"], "enp5s0")

    def test_a_status_body_is_still_read_by_the_status_rules_alone(self):
        status = json.loads((STATUS_FIXTURES / "sunshine-running.json").read_text())["response"]["body"]
        self.assertEqual(get("/v1/status", status), status)
        # The drm route's leniency is not borrowed by any other route.
        with self.assertRaises(client_core.ClientError):
            get("/v1/status", {**status, "reason": "x" * 2000})
        with self.assertRaises(client_core.ClientError):
            get("/v1/display/outputs", {"protocol_version": 1, "drm": drm_block("available")["connectors"] * 300})

    def test_the_helper_reaches_the_read_by_a_fixed_action_only(self):
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            _, connection, pin, _ = serve({"/v1/drm": drm_body("available")})
            with connection, pin:
                self.assertEqual(core.run({"action": "link-facts", "path": "/v1/power", "method": "POST"}), drm_body("available"))
        # The only other way in is by name: nothing else may be given a route.
        source = (ROOT / "client" / "client_core.py").read_text()
        routes = re.search(r"routes = \{(.*?)\n        \}", source, re.S).group(1)
        self.assertNotIn("drm", routes)

    def test_pinned_schema_and_the_client_agree_on_the_block_shape(self):
        schema = json.loads((ROOT / "protocol" / "schema.json").read_text())
        block = schema["$defs"]["drm"]
        item = block["properties"]["connectors"]["items"]
        self.assertIn("drm", schema["properties"])
        self.assertNotIn("drm", schema.get("required", []))
        self.assertFalse(block["additionalProperties"])
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(block["properties"]["connectors"]["maxItems"], client_core.LINK_FACTS_MAX_CONNECTORS)
        self.assertEqual(item["properties"]["resolutions"]["maxItems"], client_core.LINK_FACTS_MAX_RESOLUTIONS)
        self.assertEqual(item["properties"]["connector"]["pattern"], client_core.LINK_CONNECTOR_RE.pattern)
        self.assertEqual(item["properties"]["gpu_id"]["pattern"], client_core.LINK_GPU_RE.pattern)
        self.assertEqual(item["properties"]["monitor_vendor"]["pattern"], client_core.LINK_VENDOR_RE.pattern)
        self.assertEqual(item["properties"]["resolutions"]["items"]["pattern"], client_core.LINK_RESOLUTION_RE.pattern)
        self.assertEqual(item["properties"]["display_name"]["maxLength"], 128)
        self.assertEqual(item["properties"]["steam_output_id"]["maxLength"], 128)
        self.assertEqual(item["properties"]["monitor_product_id"]["maximum"], 65535)
        self.assertEqual(block["properties"]["observed_at"]["maxLength"], 64)
        self.assertEqual(block["properties"]["reason"]["maxLength"], 256)
        for name in ("available", "ambiguous-connector", "bridge-unavailable", "no-connectors"):
            facts = drm_body(name)["drm"]
            self.assertEqual(set(facts), set(block["required"]), name)
            for entry in facts["connectors"]:
                self.assertEqual(set(entry), set(item["required"]), name)
                self.assertLessEqual(len(facts["connectors"]), 16)

    def test_cli_evidence_tool_reads_the_facts_without_failing_on_a_missing_route(self):
        class StubCore:
            def __init__(self, answer):
                self.answer = answer

            def run(self, request):
                assert request == {"action": "link-facts"}
                return self.answer

        answers = {"facts": drm_body("available"), "no route": {**NO_FACTS, "unavailable": "not_found"}}
        for label, answer in answers.items():
            for flag in ("--json", "--plain"):
                with self.subTest(label=label, flag=flag):
                    output = io.StringIO()
                    with mock.patch.object(cli, "ClientCore", lambda answer=answer: StubCore(answer)), contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main([flag, "display", "link-facts"]), 0)
                    if flag == "--json":
                        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "data": answer})
                    elif label == "facts":
                        self.assertIn("data.drm.connectors[1].steam_output_id: 1", output.getvalue())
                    else:
                        self.assertIn("data.unavailable: not_found", output.getvalue())


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml link-facts functions")
class LinkFactsPresentationTests(unittest.TestCase):
    def test_the_extracted_block_is_pure(self):
        block = panel_block()
        for forbidden in ("root.", "statusData", "enqueueHelper", "remote(", "Timer", "Date.now", "Qt.", "outputsData", "currentOutput"):
            self.assertNotIn(forbidden, block)
        for name in ("linkFactsFor", "linkAdvisory", "linkFactsLine", "linkFactsRows", "linkFactsNote", "linkFactsTopology", "linkFactsFromResult", "linkAdvisoryText", "linkAdvisoryGlyph"):
            self.assertIn(f"function {name}(", block)

    def test_the_block_never_matches_by_name_and_never_reads_a_refresh_rate(self):
        code = "\n".join(line.split("//")[0] for line in panel_block().splitlines())
        # The host joins; the only comparison with the selected output is on its id.
        self.assertIn("item.steam_output_id === output.id", code)
        self.assertNotRegex(code, r"\.name\b")
        self.assertLessEqual(set(re.findall(r"output\.(\w+)", code)), {"id", "generation"})
        self.assertNotRegex(code, r"refresh")

    def test_the_real_fixture_warns_only_for_a_listed_gap_on_the_joined_output(self):
        drm = drm_block("available")
        hdmi = {"id": "1"}
        results = call_all([
            ("linkAdvisory", drm, hdmi, mode(3440, 1440)),
            ("linkAdvisory", drm, hdmi, mode(3840, 2160)),
            ("linkAdvisory", drm, hdmi, mode(1920, 1080)),
            ("linkAdvisory", drm, hdmi, mode(1280, 720)),
            ("linkAdvisory", drm, {"id": "2"}, mode(640, 480)),
            ("linkAdvisory", drm, {"id": "2"}, mode(3840, 2160)),
            ("linkAdvisory", drm, {"id": "output-1"}, mode(640, 480)),
        ])
        self.assertEqual(results, [ADVISORY_TEXT, None, None, ADVISORY_TEXT, None, None, None])

    def test_refresh_rate_is_never_part_of_the_decision(self):
        drm = drm_block("available")
        hdmi = {"id": "1"}
        for refresh in (None, 0, 24, 59, 60, 119.88, 144, 240, "60"):
            listed, unlisted, extra = call_all([
                ("linkAdvisory", drm, hdmi, {"width": 3840, "height": 2160, "refresh_hz": refresh}),
                ("linkAdvisory", drm, hdmi, {"width": 3440, "height": 1440, "refresh_hz": refresh}),
                ("linkAdvisory", drm, hdmi, {"width": 1920, "height": 1080}),
            ])
            self.assertIsNone(listed, refresh)
            self.assertEqual(unlisted, ADVISORY_TEXT, refresh)
            self.assertIsNone(extra)

    def test_the_other_fixtures_never_warn_for_the_unjoined(self):
        probes = [{"id": "1"}, {"id": "2"}, {"id": "output-1"}, {"id": "DP-1", "name": "DP-1"}, {"id": "x", "name": "HDMI-A-1"}]
        for name in ("bridge-unavailable", "no-connectors"):
            for output in probes:
                with self.subTest(name, output=output):
                    self.assertIsNone(call("linkAdvisory", drm_block(name), output, mode(640, 480)))
        ambiguous = drm_block("ambiguous-connector")
        for output in probes[1:]:
            with self.subTest("ambiguous-connector", output=output):
                self.assertIsNone(call("linkAdvisory", ambiguous, output, mode(640, 480)))
        # The unambiguous connector in that same reply is still joined.
        self.assertEqual(call("linkAdvisory", ambiguous, {"id": "1"}, mode(3440, 1440)), ADVISORY_TEXT)

    def test_a_connector_name_is_never_a_join(self):
        # Steam's output id is "1"; naming the output after its connector must not matter.
        drm = drm_block("available")
        self.assertIsNone(call("linkAdvisory", drm, {"id": "HDMI-A-1", "name": "HDMI-A-1"}, mode(3440, 1440)))
        unjoined = with_link({"steam_output_id": None})
        self.assertIsNone(call("linkAdvisory", unjoined, {"id": "1", "name": "HDMI-A-1"}, mode(3440, 1440)))

    def test_internal_disconnected_and_empty_lists_say_nothing(self):
        hdmi = {"id": "1"}
        for patch in ({"is_internal": True}, {"connected": False}, {"resolutions": []}):
            with self.subTest(patch):
                self.assertIsNone(call("linkAdvisory", with_link(patch), hdmi, mode(640, 480)))
        self.assertEqual(call("linkAdvisory", with_link({"is_internal": False, "connected": True}), hdmi, mode(640, 480)), ADVISORY_TEXT)

    def test_malformed_or_missing_facts_say_nothing(self):
        hdmi = {"id": "1"}
        for patch in ({"is_internal": None}, {"is_internal": "false"}, {"connected": None}, {"connected": "true"}, {"connected": 1}, {"resolutions": None}, {"resolutions": "3840x2160"}, {"resolutions": {}}):
            with self.subTest(patch):
                self.assertIsNone(call("linkAdvisory", with_link(patch), hdmi, mode(640, 480)))
        missing = drm_block("available")
        del missing["connectors"][1]["is_internal"]
        self.assertIsNone(call("linkAdvisory", missing, hdmi, mode(640, 480)))
        self.assertEqual(call("linkAdvisory", {**drm_block("available"), "available": False}, hdmi, mode(3440, 1440)), None)
        junk_drm = [None, UNDEFINED, True, 5, "x", [], [1], {}, {"available": True}, {"available": True, "connectors": "nope"}, {"available": "true", "connectors": drm_block("available")["connectors"]}, {"connectors": drm_block("available")["connectors"]}]
        for drm in junk_drm:
            with self.subTest(drm=drm):
                self.assertIsNone(call("linkAdvisory", drm, hdmi, mode(640, 480)))
        self.assertIsNone(call("linkAdvisory", {"available": True, "connectors": [None, 5, "x", [], True]}, hdmi, mode(640, 480)))

    def test_the_selected_output_and_mode_must_be_usable(self):
        drm = drm_block("available")
        for output in (None, UNDEFINED, {}, {"id": 1}, {"id": None}, {"id": ""}, {"id": ["1"]}, "1", 1, []):
            with self.subTest(output=output):
                self.assertIsNone(call("linkAdvisory", drm, output, mode(640, 480)))
        hdmi = {"id": "1"}
        for selected in (None, UNDEFINED, {}, "3440x1440", [3440, 1440], {"width": 3440}, {"height": 1440},
                         {"width": "3440", "height": 1440}, {"width": 3440, "height": "1440"}, {"width": 3440.5, "height": 1440},
                         {"width": 3440, "height": 1440.5}, {"width": None, "height": None}, {"width": 0, "height": 1440},
                         {"width": 3440, "height": 0}, {"width": -3440, "height": 1440}, {"width": True, "height": True}):
            with self.subTest(mode=selected):
                self.assertIsNone(call("linkAdvisory", drm, hdmi, selected))

    def test_two_connectors_claiming_one_output_is_ambiguity_not_a_match(self):
        drm = drm_block("available")
        for item in drm["connectors"]:
            if item["connector"] in {"DP-1", "HDMI-A-1"}:
                item["steam_output_id"] = "1"
        self.assertIsNone(call("linkFactsFor", drm, {"id": "1"}))
        self.assertIsNone(call("linkAdvisory", drm, {"id": "1"}, mode(3440, 1440)))
        self.assertEqual(call("linkFactsFor", drm_block("available"), {"id": "1"})["connector"], "HDMI-A-1")

    def test_a_resolution_matches_as_a_whole_element_only(self):
        hdmi = {"id": "1"}
        self.assertEqual(call("linkAdvisory", with_link({"resolutions": ["13440x1440", "3440x14400", "x3440x1440"]}), hdmi, mode(3440, 1440)), ADVISORY_TEXT)
        self.assertEqual(call("linkAdvisory", with_link({"resolutions": [3440, 1440, "3440x1440 "]}), hdmi, mode(3440, 1440)), ADVISORY_TEXT)
        self.assertIsNone(call("linkAdvisory", with_link({"resolutions": ["1280x720", "3440x1440"]}), hdmi, mode(3440, 1440)))

    def test_the_wording_matches_decky_and_never_calls_the_mode_invalid(self):
        text = call("linkAdvisoryText")
        self.assertEqual(text, ADVISORY_TEXT)
        self.assertRegex(text, r"not in the display link's mode list")
        self.assertRegex(text, r"may still work")
        self.assertNotRegex(text, r"(?i)invalid|unsupported|not supported|error|cannot|can't|blocked|refus")
        self.assertTrue(text.startswith("Heads up"))
        glyph = call("linkAdvisoryGlyph")
        self.assertTrue(glyph and glyph.strip() == glyph)
        self.assertNotIn(glyph, text)

    def test_the_link_line_follows_the_decky_format(self):
        facts = drm_block("available")
        for (name, connector), expected in LINES.items():
            link = next(item for item in facts["connectors"] if item["connector"] == connector)
            self.assertEqual(call("linkFactsLine", link), expected, connector)
        ambiguous = drm_block("ambiguous-connector")
        second = [item for item in ambiguous["connectors"] if item["connector"] == "DP-1"][1]
        self.assertEqual(call("linkFactsLine", second), "DP-1 · card1")
        link = next(item for item in facts["connectors"] if item["connector"] == "HDMI-A-1")
        # The model needs both halves; the display name only when it adds something.
        self.assertEqual(call("linkFactsLine", {**link, "monitor_product_id": None}), "HDMI-A-1 · card0 · LG TV SSCR2")
        self.assertEqual(call("linkFactsLine", {**link, "monitor_vendor": None}), "HDMI-A-1 · card0 · LG TV SSCR2")
        self.assertEqual(call("linkFactsLine", {**link, "display_name": "HDMI-A-1"}), "HDMI-A-1 · card0 · GSM model 49780")
        self.assertEqual(call("linkFactsLine", {**link, "monitor_product_id": 0}), "HDMI-A-1 · card0 · LG TV SSCR2 · GSM model 0")
        for junk in (None, UNDEFINED, "x", 5, [], True):
            self.assertEqual(call("linkFactsLine", junk), "")

    def test_text_is_capped_at_the_schema_bounds(self):
        link = {**drm_block("available")["connectors"][1], "connector": "C" * 500, "display_name": "N" * 500, "monitor_vendor": "VVVVV", "gpu_id": "drm:card" + "9" * 500}
        self.assertEqual(call("linkFactsLine", link), "C" * 64 + " · card" + "9" * 8 + " · " + "N" * 128 + " · VVV model 49780")
        rows = {row["id"]: row for row in call("linkFactsRows", link)}
        self.assertEqual(rows["connector"]["value"], "C" * 64)
        self.assertEqual(rows["display"]["value"], "N" * 128)

    def test_the_group_rows_state_each_fact_in_words(self):
        facts = drm_block("available")

        def rows(connector):
            link = next(item for item in facts["connectors"] if item["connector"] == connector)
            return [(row["id"], row["label"], row["value"], row["state"]) for row in call("linkFactsRows", link)]

        self.assertEqual(rows("HDMI-A-1"), [
            ("connector", "Connector", "HDMI-A-1", ""),
            ("card", "GPU card", "card0", ""),
            ("display", "Display", "LG TV SSCR2", ""),
            ("monitor", "Monitor", "GSM model 49780", ""),
            ("connected", "Connection", "Connected", "ok"),
        ])
        self.assertEqual(rows("eDP-1")[2:], [
            ("display", "Display", "Name not reported", ""),
            ("monitor", "Monitor", "Maker and model not reported", ""),
            ("connected", "Connection", "Connected · internal panel", "ok"),
        ])
        self.assertEqual(rows("HDMI-A-2")[-1], ("connected", "Connection", "Not connected", "off"))
        # The state never rests on its glyph alone: the value says it too.
        self.assertTrue(all(value.startswith(("Connected", "Not connected")) for _, _, value, state in (rows("HDMI-A-1")[-1], rows("HDMI-A-2")[-1]) if state))

    def test_rows_degrade_instead_of_throwing_on_junk(self):
        base = drm_block("available")["connectors"][1]
        junk = [None, True, False, 0, 1.5, -1, "", "x", [], [None], [{}], {}, {"a": 1}]
        calls = [("linkFactsRows", value) for value in junk] + [("linkFactsRows", UNDEFINED)]
        for key in ("connector", "gpu_id", "display_name", "monitor_vendor", "monitor_product_id", "is_internal", "connected"):
            for value in junk:
                calls.append(("linkFactsRows", {**base, key: value}))
                calls.append(("linkFactsLine", {**base, key: value}))
        for value in call_all(calls):
            text = json.dumps(value)
            for bad in ("undefined", "NaN", "[object"):
                self.assertNotIn(bad, text)
            if isinstance(value, list):
                self.assertIn(len(value), {0, 5})
                for row in value:
                    self.assertNotIn("null", row["value"])
        self.assertEqual(call("linkFactsRows", {**base, "connector": ""}), [])

    def test_the_group_note_says_why_there_is_nothing_to_show(self):
        drm = drm_block("available")
        output = {"id": "1"}
        no_output = "No display output is known yet."
        unavailable = "Link facts are not available. They are read when Display is opened, and older hosts do not report them."
        unjoined = "No connector is joined to this display, so the host cannot say which link it uses."
        self.assertEqual(call_all([
            ("linkFactsNote", drm, output),
            ("linkFactsNote", drm, {"id": "2"}),
            ("linkFactsNote", None, output),
            ("linkFactsNote", drm_block("no-connectors"), output),
            ("linkFactsNote", drm_block("bridge-unavailable"), output),
            ("linkFactsNote", drm, None),
            ("linkFactsNote", drm, {"id": ""}),
            ("linkFactsNote", UNDEFINED, UNDEFINED),
        ]), ["", unjoined, unavailable, unavailable, unjoined, no_output, no_output, no_output])

    def test_the_topology_key_follows_ids_and_generations_only(self):
        a = [{"id": "1", "generation": 12, "name": "A", "modes": []}]
        same = [{"id": "1", "generation": 12, "name": "renamed", "modes": [{"id": "m"}]}]
        newer = [{"id": "1", "generation": 13}]
        more = a + [{"id": "2", "generation": 1}]
        keys = call_all([("linkFactsTopology", value) for value in (a, same, newer, more, [], None, UNDEFINED, "x", {}, [None], [5])])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len({keys[0], keys[2], keys[3]}), 3)
        self.assertEqual(keys[4:8] + keys[8:9], [""] * 5)
        self.assertTrue(keys[9] and keys[10])

    def test_only_a_clean_helper_answer_becomes_facts(self):
        drm = drm_block("available")
        out = call_all([
            ("linkFactsFromResult", {"ok": True, "data": {"protocol_version": 1, "drm": drm}}),
            ("linkFactsFromResult", {"ok": True, "data": {"protocol_version": 1, "drm": None, "unavailable": "not_found"}}),
            ("linkFactsFromResult", {"ok": True, "data": {"protocol_version": 1}}),
            ("linkFactsFromResult", {"ok": False, "error": "host rejected the request", "status": 404}),
            ("linkFactsFromResult", {"ok": False, "data": {"drm": drm}}),
            ("linkFactsFromResult", {"ok": "yes", "data": {"drm": drm}}),
            ("linkFactsFromResult", {"ok": True, "data": {"drm": [drm]}}),
            ("linkFactsFromResult", {"ok": True, "data": {"drm": {"available": True}}}),
            ("linkFactsFromResult", {"ok": True, "data": "x"}),
            ("linkFactsFromResult", {"ok": True}),
            ("linkFactsFromResult", {}),
            ("linkFactsFromResult", None),
            ("linkFactsFromResult", UNDEFINED),
            ("linkFactsFromResult", "oops"),
        ])
        self.assertEqual(out[0], drm)
        self.assertEqual(out[1:], [None] * 13)

    @unittest.skipUnless(DECKY_FRONTEND.exists(), "the Decky checkout is not present")
    def test_wording_and_decisions_match_the_decky_frontend(self):
        source = DECKY_FRONTEND.read_text()
        found = re.search(r"/\* link-facts:begin \*/([\s\S]*?)/\* link-facts:end \*/", source)
        self.assertTrue(found, "the Decky link-facts block moved")
        cases = []
        for name in ("available", "ambiguous-connector", "bridge-unavailable", "no-connectors"):
            drm = drm_block(name)
            for output in ({"id": "1"}, {"id": "2"}, {"id": "output-1"}):
                for width, height in ((3440, 1440), (3840, 2160), (1920, 1080), (1280, 800), (640, 480)):
                    cases.append({"drm": drm, "output": output, "mode": mode(width, height), "link": None})
            for link in drm["connectors"]:
                cases.append({"drm": drm, "output": {"id": "1"}, "mode": mode(1, 1), "link": link})
        for patch in ({"is_internal": True}, {"connected": False}, {"resolutions": []}):
            cases.append({"drm": with_link(patch), "output": {"id": "1"}, "mode": mode(640, 480), "link": None})
        result = run_node(PARITY_HARNESS, {"block": panel_block(), "decky": found.group(1), "cases": cases})
        self.assertEqual(result["text"][0], result["text"][1])
        self.assertEqual(result["text"][0], ADVISORY_TEXT)
        for case, item in zip(cases, result["out"]):
            self.assertEqual(item["advisory"][0], item["advisory"][1], case)
            if case["link"] is not None:
                self.assertEqual(item["line"][0], item["line"][1], case["link"])


def function_body(source: str, name: str) -> str:
    start = source.index(f"\n  function {name}(")
    return source[start:source.index("\n  }\n", start)]


def blocks(source: str, header: str) -> list[str]:
    """Every `header { ... }` in a QML file, matched by braces."""
    found = []
    for match in re.finditer(re.escape(header) + r"\s*\{", source):
        depth, index = 1, match.end()
        while depth and index < len(source):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            index += 1
        found.append(source[match.start():index])
    return found


class PanelWiringTests(unittest.TestCase):
    """Static checks that the read is one per display interaction and the markup reads only its cache."""

    def test_the_fixed_action_is_requested_in_exactly_one_place_on_the_poll_lane(self):
        panel = panel_source()
        self.assertEqual(panel.count('"link-facts"'), 1)
        body = function_body(panel, "requestLinkFacts")
        self.assertIn('enqueueHelper("link-facts", {timeout: 2.5}', body)
        # Poll lane: it never sets the busy flag that disables Preview, and it is dropped with the panel.
        self.assertTrue(body.rstrip().endswith("}, true)"), body[-80:])
        self.assertIn("if (!root.opened || !root.paired || root.view !== \"display\") return", body)
        self.assertNotIn("remote(", body)

    def test_the_read_is_triggered_by_opening_display_and_by_a_changed_inventory_only(self):
        panel = panel_source()
        callers = []
        for match in re.finditer(r"\n  function (\w+)\(", panel):
            body = function_body(panel, match.group(1))
            if "requestLinkFacts()" in body and match.group(1) != "requestLinkFacts":
                callers.append(match.group(1))
        self.assertEqual(sorted(callers), ["applyOutputs", "setView"])
        # setView asks only for the Display view, once per opening, after resetting the previous read.
        view = function_body(panel, "setView")
        display = view.index('if (next === "display")')
        settings = view.index('else if (next === "settings"')
        self.assertLess(display, view.index("root.linkFactsRequestedKey = \"\""))
        self.assertLess(view.index("root.linkFactsRequestedKey = \"\""), view.index("root.requestLinkFacts()"))
        self.assertLess(view.index("root.requestLinkFacts()"), settings)
        self.assertEqual(view.count("requestLinkFacts()"), 1)
        # applyOutputs only reaches it after the unchanged-inventory early return, and the key guard makes
        # an unchanged topology a no-op.
        applied = function_body(panel, "applyOutputs")
        self.assertGreater(applied.index("root.requestLinkFacts()"), applied.index("if (signature === root.outputsSignature) return"))
        guard = function_body(panel, "requestLinkFacts")
        self.assertIn('if (key === "" || key === root.linkFactsRequestedKey) return', guard)

    def test_no_timer_poll_or_open_handler_reads_the_facts(self):
        panel = panel_source()
        for timer in blocks(panel, "Timer"):
            self.assertNotIn("linkFacts", timer)
            self.assertNotIn("LinkFacts", timer)
        for name in ("refresh", "refreshDetails", "refreshConnectionDetails", "onOpened", "onClosed", "tickCountdown", "tickPairing", "localInspect", "refreshAfterAction", "viewByDelta"):
            body = function_body(panel, name)
            self.assertNotIn("link-facts", body, name)
            self.assertNotIn("requestLinkFacts", body, name)
        self.assertNotIn("onTriggered: root.requestLinkFacts", panel)

    def test_a_failed_read_touches_neither_status_nor_the_action_line(self):
        panel = panel_source()
        for name in ("requestLinkFacts", "syncLinkFactsIdentity"):
            body = function_body(panel, name)
            for forbidden in ("actionMessage", "connectionState", "connectionDetail", "friendlyError", "statusData", "lastObservation", "pollIntervalMs", "root.refresh(", "describeError", "operationData"):
                self.assertNotIn(forbidden, body, f"{name}: {forbidden}")
        # The only state the callback writes is the facts and their key.
        callback = function_body(panel, "requestLinkFacts").split("enqueueHelper(", 1)[1]
        written = sorted(set(re.findall(r"root\.(\w+) = ", callback)))
        self.assertEqual(written, ["linkFactsData", "linkFactsDataKey", "linkFactsRequestedKey"])

    def test_facts_belong_to_one_host_and_one_topology(self):
        panel = panel_source()
        self.assertIn("readonly property var linkFacts: root.linkFactsDataKey !== \"\" && root.linkFactsDataKey === root.linkFactsTopology(root.outputList) ? root.linkFactsData : null", panel)
        body = function_body(panel, "requestLinkFacts")
        self.assertIn("if (identity !== root.linkFactsIdentity) return", body)
        # Every place the pairing changes clears them.
        for name in ("resetPairedState", "localInspect"):
            self.assertIn("root.syncLinkFactsIdentity()", function_body(panel, name), name)
        self.assertEqual(panel.count("root.localData = "), 3)
        sync = function_body(panel, "syncLinkFactsIdentity")
        for field in ("host_id", "endpoint"):
            self.assertIn(field, sync)

    def test_the_markup_reads_the_cache_through_the_freshness_gate_only(self):
        panel = panel_source()
        markup = panel[panel.index("Component {\n    id: displayComponent"):]
        self.assertNotIn("linkFactsData", markup)
        self.assertGreaterEqual(markup.count("root.linkFacts,"), 2)
        # The markup asks the host for nothing: the read lives in requestLinkFacts alone.
        for forbidden in ("link-facts", "requestLinkFacts"):
            self.assertNotIn(forbidden, markup)

    def test_the_advisory_sits_directly_above_the_preview_row_of_the_selected_mode(self):
        panel = panel_source()
        delegate = panel[panel.index("model: root.visibleModeList"):]
        delegate = delegate[:delegate.index("id: applyButton")]
        self.assertIn("isSelected && !isCurrent ? root.linkAdvisory(root.linkFacts, root.currentOutput, modelData) : null", delegate)
        advisory = delegate.index("visible: !!advisoryText")
        preview = delegate.index("id: modePreviewButton")
        self.assertLess(advisory, preview)
        self.assertLess(advisory, delegate.index("CursorButton"))
        # Words and a glyph, not colour: the markup adds no colour of its own.
        shown = delegate[advisory:preview]
        self.assertIn('text: root.linkAdvisoryGlyph() + " " + (advisoryText || "")', shown)
        self.assertNotIn("color:", shown)
        self.assertIn('"Link: " + root.linkFactsLine(advisoryLink)', shown)
        # Nothing about it can disable or reorder the controls beneath it.
        self.assertNotIn("advisoryText", delegate[preview:])

    def test_the_group_sits_in_connection_details_after_host_health(self):
        panel = panel_source()
        details = panel.index('PanelSectionHeader { text: "Connection details"')
        health = panel.index('PanelSectionHeader { text: "Host health"')
        link = panel.index('PanelSectionHeader { text: "Link facts"')
        preferences = panel.index('PanelSectionHeader { text: "Display preferences"')
        self.assertLess(details, health)
        self.assertLess(health, link)
        self.assertLess(link, preferences)
        group = panel[panel.index("id: linkFactsGroup"):preferences]
        group = group[:group.index("PanelSeparator")]
        for forbidden in ("enqueueHelper", "remote(", "Timer", "Qt.callLater", "refresh(", "requestLinkFacts"):
            self.assertNotIn(forbidden, group)
        self.assertIn("root.linkFactsFor(root.linkFacts, root.currentOutput)", group)
        self.assertIn("root.linkFactsNote(root.linkFacts, root.currentOutput)", group)
        self.assertIn("visible: root.paired", group)
        for row in ("connector", "card", "display", "monitor", "connected"):
            self.assertIn(f'"{row}"', group)

    def test_the_group_glyph_is_the_hosts_own_state_glyph(self):
        panel = panel_source()
        self.assertIn('(row.state ? root.selfCheckGlyph(row.state) + " " : "") + row.label + ": " + row.value', panel)


if __name__ == "__main__":
    unittest.main()
