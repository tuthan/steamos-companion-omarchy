"""Redacted diagnostic report: the pure composer, the cross-client vector, and the CLI command.

Three layers. `client/diagnostics.py` is a pure port of Decky's reference, so it is
proved against the vendored vector `protocol/fixtures/diagnostic-report.json`
(every case, strict ones byte for byte and in key order), against the scrubber
rule table, and by a forbidden-value scan and a deterministic fuzz. Then
`steamos-companion-ctl diagnostics` is run through the real `ClientCore`, the
real pinned transport (only the socket is faked) and the real CLI, with the real
vendored exchanges as bodies: one read per route, no retry, no state written,
nothing from the local pairing state in the output. Nothing here touches a host.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from client import cli, client_core, diagnostics
from client.diagnostics import (
    MAX_CONNECTORS, MAX_JOURNAL, MAX_METHODS, MAX_MODES, MAX_OUTPUTS, MAX_REPORT_BYTES, MAX_RESOLUTIONS, REPORT_FORMAT, build_report, scrub_text,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "protocol" / "fixtures"
VECTOR = json.loads((FIXTURES / "diagnostic-report.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in VECTOR["cases"]}
SLOTS = ("status", "outputs", "drm", "operations")
SECTIONS = ("host", "outputs", "link", "journal")

SECRET_KEYS = {"host_id", "boot_id", "certificate_fingerprint", "fingerprint_short", "mac", "token", "pairing_session", "request_id", "preview_id", "profile_id", "address", "serial", "edid"}
SECRET_PREFIXES = ("op-", "profile-", "req-", "host-", "boot-", "fixture-token", "sha256:")
SECRET_TEXT = ("192.168", "aa:bb:cc:dd:ee:ff", "fixture-token", "evil.example", "/run/user", "secret", "sha256:", "http://", "https://")

PIN = "sha256:" + "ab12" * 16
NULL_REPORT = {"format": REPORT_FORMAT, "host": None, "outputs": None, "link": None, "journal": None}

REAL_FIXTURES = {
    "status": "contract/status/sunshine-running.json",
    "outputs": "contract/display-outputs/available.json",
    "drm": "contract/drm/available.json",
    "operations": "contract/operations/list-mixed.json",
}
PATHS = {"status": "/v1/status", "outputs": "/v1/display/outputs", "drm": "/v1/drm", "operations": "/v1/operations"}


def load_input(spec: dict[str, Any] | None) -> Any:
    """An input the way the vector defines it: null, an inline body, or a fixture path under protocol/fixtures/."""
    if spec is None:
        return None
    if "body" in spec:
        return copy.deepcopy(spec["body"])
    data = json.loads((FIXTURES / spec["fixture"]).read_text(encoding="utf-8"))
    return data["response"]["body"] if "response" in data else data


def case_bodies(case: dict[str, Any]) -> dict[str, Any]:
    return {slot: load_input(case["inputs"][slot]) for slot in SLOTS}


def real(slot: str) -> Any:
    return load_input({"fixture": REAL_FIXTURES[slot]})


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def sentinels(value: Any, found: set[str] | None = None) -> set[str]:
    """Every string in an input body that a report must never repeat."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and len(item) >= 6 and (key in SECRET_KEYS or item.startswith(SECRET_PREFIXES)):
                found.add(item)
            sentinels(item, found)
    elif isinstance(value, list):
        for item in value:
            sentinels(item, found)
    return found


class Leaks(unittest.TestCase):
    def assert_clean(self, text: str, bodies: Any, extra: tuple[str, ...] = ()) -> None:
        for secret in sorted(sentinels(list(bodies.values()) if isinstance(bodies, dict) else bodies) | set(extra)):
            self.assertNotIn(secret, text)
        for fragment in SECRET_TEXT:
            self.assertNotIn(fragment, text)


# ---------------------------------------------------------------- the cross-client vector


class VectorTests(Leaks):
    def test_the_vector_is_the_pinned_one_and_covers_the_cases_a_client_must_handle(self):
        self.assertEqual((VECTOR["protocol_version"], VECTOR["format"]), (1, REPORT_FORMAT))
        self.assertEqual({"healthy", "host-without-self-check", "degraded-host", "ambiguous-link-with-preview", "nothing-read", "hostile-text", "hostile-shape", "oversize-journal"}, set(CASES))
        self.assertEqual(["hostile-shape"], [name for name, case in CASES.items() if not case["strict"]])
        self.assertEqual(NULL_REPORT, CASES["nothing-read"]["report"])
        self.assertIsNone(CASES["host-without-self-check"]["report"]["host"], "an older host is absent, not empty")
        self.assertEqual([], CASES["degraded-host"]["report"]["journal"]["records"], "an empty journal is present and empty")
        self.assertTrue(CASES["hostile-text"]["report"]["journal"]["truncated"])
        self.assertTrue(CASES["oversize-journal"]["report"]["journal"]["truncated"])

    def test_every_input_the_vector_names_is_vendored(self):
        for name, case in CASES.items():
            for slot, spec in case["inputs"].items():
                if spec is not None and "fixture" in spec:
                    with self.subTest(case=name, slot=slot):
                        self.assertTrue((FIXTURES / spec["fixture"]).is_file(), spec["fixture"])

    def test_every_strict_case_is_rebuilt_exactly_and_in_key_order(self):
        for name, case in CASES.items():
            if not case["strict"]:
                continue
            with self.subTest(case=name):
                report = build_report(**case_bodies(case))
                self.assertEqual(case["report"], report)
                self.assertEqual(json.dumps(case["report"]), json.dumps(report), "the key order is part of the contract")
                self.assertEqual(compact(case["report"]), compact(report))

    def test_the_hostile_shape_case_never_raises_leaks_nothing_and_each_section_is_the_reference_value_or_null(self):
        case = CASES["hostile-shape"]
        bodies = case_bodies(case)
        report = build_report(**bodies)
        self.assertEqual({"format", *SECTIONS}, set(report))
        self.assertEqual(REPORT_FORMAT, report["format"])
        for section in SECTIONS:
            with self.subTest(section=section):
                self.assertIn(report[section], (None, case["report"][section]))
        self.assert_clean(compact(report), bodies)

    def test_this_client_rebuilds_the_hostile_shape_case_exactly_too(self):
        # Stronger than the contract asks: this client has no decoder that refuses a body first.
        case = CASES["hostile-shape"]
        self.assertEqual(case["report"], build_report(**case_bodies(case)))

    def test_no_case_repeats_an_identifier_address_pin_or_token_from_its_inputs(self):
        for name, case in CASES.items():
            bodies = case_bodies(case)
            with self.subTest(case=name):
                self.assert_clean(compact(build_report(**bodies)), bodies)

    def test_the_scan_finds_what_it_is_meant_to_find_in_the_inputs(self):
        # Guard the guard: the sentinel collector must see the planted secrets, or the scan above proves nothing.
        found = sentinels(list(case_bodies(CASES["hostile-shape"]).values()))
        for planted in ("host-secret-id", "boot-secret-id", "aa:bb:cc:dd:ee:ff", "89abcdef", "op-secret-0000xxxxxxxxxxxxxxx", "req-secret-0", "op-secret-preview", "profile-secret", "00ffffffffffff00", "192.168.50.42"):
            self.assertIn(planted, found)
        self.assertIn("req-secret-0", sentinels(list(case_bodies(CASES["hostile-text"]).values())))
        with self.assertRaises(AssertionError):
            self.assert_clean('{"x":"host-secret-id"}', case_bodies(CASES["hostile-shape"]))
        for fragment in SECRET_TEXT:
            with self.assertRaises(AssertionError):
                self.assert_clean(f'{{"x":"{fragment}"}}', [])

    def test_every_report_stays_within_its_byte_limit(self):
        for name, case in CASES.items():
            with self.subTest(case=name):
                self.assertLessEqual(len(compact(case["report"])), MAX_REPORT_BYTES)
                self.assertLessEqual(len(compact(build_report(**case_bodies(case)))), MAX_REPORT_BYTES)

    def test_a_report_is_plain_printable_ascii_and_the_input_is_left_alone(self):
        for name, case in CASES.items():
            bodies = case_bodies(case)
            before = copy.deepcopy(bodies)
            with self.subTest(case=name):
                text = compact(build_report(**bodies))
                self.assertRegex(text, r"^[\x20-\x7e]+$")
                self.assertEqual(before, bodies, "the composer must not edit the bodies it was given")


# ---------------------------------------------------------------- the scrubber


class ScrubTests(unittest.TestCase):
    def test_each_rule(self):
        for text, expected in (
            ("connect to 192.168.50.42:18443 failed", "connect to [ip] failed"),
            ("peer fe80::1a2b:3c4d:5e6f:7a8b refused", "peer [ip] refused"),
            ("peer 2001:db8:85a3:0:0:8a2e:370:7334 x", "peer [ip] x"),
            ("at 2026-09-13T12:00:24Z done", "at 2026-09-13T12:00:24Z done"),
            ("wake mac aa:bb:cc:dd:ee:ff unreachable", "wake mac [mac] unreachable"),
            ("wake mac AA-BB-CC-DD-EE-FF unreachable", "wake mac [mac] unreachable"),
            ("GET https://192.168.50.42:18443/v1/status?x=1 failed", "GET [url] failed"),
            ("bad cert sha256:" + "0123456789abcdef" * 4, "bad cert [pin]"),
            ("token fixture-token-0001-" + "x" * 30 + " rejected", "token [token] rejected"),
            ("cannot open /run/user/1000/steamos-companion/state.json", "cannot open [path]"),
            ("first line\nsecond\tline\x00", "first line second line"),
            ("café ‮evil", "caf evil"),
            ("  spaced    out  ", "spaced out"),
            ("gamescope_output_update_failed on std::string", "gamescope_output_update_failed on std::string"),
            ("version 0.5.18 of the plugin", "version 0.5.18 of the plugin"),
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(expected, scrub_text(text, 160))

    def test_every_rule_is_exercised_on_its_own(self):
        # One marker per scrubber, in table order; removing any rule leaves its secret standing.
        self.assertEqual(["[url]", "[pin]", "[mac]", "[ip]", "[ip]", "[ip]", "[path]", "[token]"], [marker for _pattern, marker in diagnostics._SCRUBBERS])
        for text, secret, marker in (
            ("see http://evil.example/a", "evil.example", "[url]"), ("see https://evil.example/a", "evil.example", "[url]"),
            ("pin sha256:ABCDEF0123456789", "ABCDEF", "[pin]"), ("pin sha256:abcdef01", "abcdef01", "[pin]"),
            ("mac 00:11:22:33:44:55", "00:11", "[mac]"), ("mac 00-11-22-33-44-55", "00-11", "[mac]"),
            ("ip 10.0.0.1", "10.0.0.1", "[ip]"), ("ip 10.0.0.1:8080", "8080", "[ip]"),
            ("ip 2001:db8:85a3:0:0:8a2e:370:7334", "8a2e", "[ip]"), ("ip fe80::1", "fe80", "[ip]"), ("ip 2001:db8::8a2e:370:7334", "8a2e", "[ip]"),
            ("path /etc/passwd", "passwd", "[path]"), ("path /a/b/c.txt", "c.txt", "[path]"),
            ("tok abcdefghijklmnopqrstuvwx1", "abcdefghij", "[token]"), ("tok A1_b2-C3_d4-E5_f6-G7_h8-I9", "A1_b2", "[token]"),
        ):
            with self.subTest(text=text):
                result = scrub_text(text, 160)
                self.assertIn(marker, result)
                self.assertNotIn(secret, result)
                self.assertTrue(result.startswith(text.split(" ", 1)[0] + " "))

    def test_things_that_only_look_like_the_patterns_are_left_alone(self):
        for text in ("1/2 of the way", "one / two", "short-token-1", "abcdefghijklmnopqrstuvwx", "a single /segment", "12:34", "build 1.2.3", "v1.2.3.4a"):
            with self.subTest(text=text):
                self.assertEqual(text, scrub_text(text, 160))

    def test_the_result_is_bounded_and_empty_text_is_absent(self):
        self.assertEqual(("word " * 80).strip()[:159], scrub_text("word " * 80, 159))
        self.assertEqual(160, len(scrub_text("a" * 500, 160)))
        for value in ("", "   ", "\x00\x01", "‮", None, 7, [], {}, b"bytes"):
            self.assertIsNone(scrub_text(value, 160))

    def test_a_token_is_scrubbed_before_it_is_cut(self):
        token = "fixture-token-0001-" + "x" * 30
        self.assertEqual("[token]", scrub_text(token, 160))
        self.assertNotIn("fixture-token", scrub_text("y" * 150 + " " + token, 160) or "")
        # A token cut first would shrink below the 24-character rule and survive as a fragment of itself.
        straddling = "y" * 140 + " " + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
        self.assertEqual("y" * 140 + " [token]", scrub_text(straddling, 160))
        # A secret that straddles the cut would otherwise survive as a fragment of it.
        self.assertNotIn("0123456789abcdef", scrub_text("p " * 20 + "sha256:" + "0123456789abcdef" * 4, 48) or "")

    def test_the_input_is_cut_before_the_rules_so_no_rule_runs_on_unbounded_text(self):
        self.assertEqual(diagnostics._TEXT_PRECAP, 1024)
        late = "x" * 1200 + " 10.0.0.1"
        self.assertEqual(160, len(scrub_text(late, 160)))
        self.assertLessEqual(len(scrub_text(late, 4000)), diagnostics._TEXT_PRECAP)


# ---------------------------------------------------------------- sections, bounds, identifiers


def status_body() -> dict[str, Any]:
    return real("status")


def one_record(**changes: Any) -> dict[str, Any]:
    return {"id": "op-1", "kind": "power.suspend", "state": "failed", **changes}


class SectionTests(unittest.TestCase):
    def test_absent_inputs_are_null_not_empty(self):
        self.assertEqual(NULL_REPORT, build_report())
        for hostile in ("text", 7, [], True, {"unrelated": 1}):
            self.assertIsNone(build_report(status=hostile)["host"])
        for hostile in ("text", 7, [], True, None):
            self.assertIsNone(build_report(drm=hostile)["link"])
            self.assertIsNone(build_report(outputs=hostile)["outputs"])
            self.assertIsNone(build_report(operations=hostile)["journal"])
        self.assertEqual({"available": False, "reason": None, "connectors": []}, build_report(drm={"unrelated": 1})["link"], "a body with no drm block is unavailable, not unread")

    def test_present_but_empty_is_not_null(self):
        outputs = build_report(outputs={"outputs": []})["outputs"]
        self.assertEqual({"available": False, "count": 0, "outputs": [], "saved_profiles": 0, "preview_active": False}, outputs)
        self.assertEqual({"count": 0, "truncated": False, "records": []}, build_report(operations={"operations": []})["journal"])
        self.assertEqual({"available": True, "reason": None, "connectors": []}, build_report(drm={"drm": {"available": True, "connectors": []}})["link"])
        self.assertIsNone(build_report(operations={"operations": None})["journal"])
        self.assertIsNone(build_report(operations={"protocol_version": 1})["journal"])
        self.assertIsNone(build_report(status={"protocol_version": 1})["host"])
        self.assertIsNotNone(build_report(status={"self_check": {}})["host"])

    def test_a_section_that_raises_is_null_and_the_others_survive(self):
        outputs = {"outputs": [{"id": "1", "modes": [{"width": 1920, "height": 1080, "refresh_hz": 10 ** 400}]}]}
        report = build_report(status=status_body(), outputs=outputs, operations={"operations": [one_record()]})
        self.assertIsNone(report["outputs"])
        self.assertIsNotNone(report["host"])
        self.assertEqual(1, report["journal"]["count"])

    def test_every_validated_token_is_matched_whole_so_a_trailing_newline_cannot_pass(self):
        def host(value_path, value):
            body = status_body()
            node = body["self_check"]
            for key in value_path[:-1]:
                node = node[key]
            node[value_path[-1]] = value
            return build_report(status=body)["host"]

        def link(field, value):
            connector = {"connector": "HDMI-A-1", "gpu_id": "drm:card0", "connected": True, "resolutions": ["1920x1080"], "steam_output_id": "1", "monitor_vendor": "GSM"}
            connector[field] = value
            return build_report(drm={"drm": {"available": True, "connectors": [connector]}})["link"]["connectors"]

        def journal(field, value):
            record = one_record(outcome="ok", restore_state="failed", updated_at="2026-09-13T12:00:00Z")
            record[field] = value
            return build_report(operations={"operations": [record]})["journal"]["records"]

        # name -> (probe(value) -> what the report kept, a clean value)
        probes = {
            "_TOKEN": [
                (lambda v: build_report(outputs={"outputs": [{"id": v}]})["outputs"]["count"], "1", 1),
                (lambda v: link("steam_output_id", v)[0]["output_id"], "1", "1"),
                (lambda v: len(journal("id", v)), "op-1", 1),
                (lambda v: len(journal("kind", v)), "power.suspend", 1),
                (lambda v: len(journal("state", v)), "failed", 1),
            ],
            "_CODE": [(lambda v: journal("outcome", v)[0]["outcome"], "gamescope_ok", "gamescope_ok")],
            "_METHOD": [(lambda v: host(("bridge", "methods"), [v])["bridge"]["methods"], "suspend", ["suspend"])],
            "_VERSION": [(lambda v: host(("versions", "plugin"), v)["plugin_version"], "0.5.18", "0.5.18")],
            "_CONTRACT": [(lambda v: host(("provider", "contract_version"), v)["sunshine"]["contract_version"], "1", "1")],
            "_CONNECTOR": [(lambda v: len(link("connector", v)), "DP-1", 1)],
            "_CARD": [(lambda v: link("gpu_id", v)[0]["card"], "drm:card0", "card0")],
            "_VENDOR": [(lambda v: link("monitor_vendor", v)[0]["vendor"], "GSM", "GSM")],
            "_RESOLUTION": [(lambda v: link("resolutions", [v])[0]["resolution_count"], "1920x1080", 1)],
            "_STAMP": [(lambda v: journal("updated_at", v)[0]["updated_at"], "2026-09-13T12:00:00Z", "2026-09-13T12:00:00Z")],
            "_RESTORE_STATE": [(lambda v: journal("restore_state", v)[0]["restore_state"], "failed", "failed")],
        }
        # No validator may be added without a probe here.
        validators = {name for name, value in vars(diagnostics).items()
                      if isinstance(value, re.Pattern) and name not in {"_LINE_BREAKS", "_NON_PRINTABLE", "_SPACES", "_DAY"}}
        self.assertEqual(validators, set(probes))
        for name, cases in probes.items():
            for probe, clean, expected in cases:
                with self.subTest(pattern=name, clean=clean):
                    self.assertEqual(expected, probe(clean), "the control value must be accepted")
                    for suffix in ("\n", "\r\n", "\x00"):
                        rejected = probe(clean + suffix)
                        self.assertIn(rejected, (None, [], 0), (name, repr(suffix), rejected))
                    self.assertIn(probe("\n" + clean), (None, [], 0))

    def test_a_trailing_newline_never_reaches_a_report_through_any_field(self):
        body = status_body()
        body["self_check"]["versions"]["plugin"] = "0.5.18\n"
        body["self_check"]["bridge"]["methods"] = ["display\n", "suspend"]
        host = build_report(status=body)["host"]
        self.assertIsNone(host["plugin_version"])
        self.assertEqual(["suspend"], host["bridge"]["methods"])
        for field in ("id", "kind", "state"):
            hostile = one_record(**{field: one_record()[field] + "\n"})
            self.assertEqual(0, build_report(operations={"operations": [hostile]})["journal"]["count"], field)

    def test_a_boolean_is_never_a_number(self):
        body = status_body()
        body["self_check"]["versions"]["protocol"] = True
        body["self_check"]["listener"]["port"] = True
        host = build_report(status=body)["host"]
        self.assertEqual((None, None), (host["protocol_version"], host["listener"]["port"]))
        link = build_report(drm={"drm": {"available": True, "connectors": [{"connector": "DP-1", "monitor_product_id": True}]}})["link"]
        self.assertIsNone(link["connectors"][0]["model"])
        outputs = build_report(outputs={"outputs": [{"id": "1", "current_mode_id": "a", "modes": [{"id": "a", "width": True, "height": 1080}, {"id": "b", "width": 1920, "height": True}]}]})["outputs"]
        self.assertEqual((0, None), (outputs["outputs"][0]["mode_count"], outputs["outputs"][0]["current_mode"]))
        record = build_report(operations={"operations": [one_record(target={"generation": True, "restart": True, "mode": {"width": True, "height": True}})]})["journal"]["records"][0]
        self.assertEqual({"generation": True, "restart": True}, record["target"], "a boolean target value is kept as a boolean, and never as a mode size")

    def test_a_missing_or_non_boolean_flag_is_false_never_true(self):
        for check in ({}, {"certificate": {"ready": "yes"}, "listener": {"bound": 1}, "bridge": {"ready": "true"}, "provider": {"monitor_enabled": "true", "stale": 1}}):
            host = build_report(status={"self_check": check})["host"]
            self.assertEqual(
                (False, False, False, False, False), (host["certificate"]["ready"], host["listener"]["bound"], host["bridge"]["ready"], host["sunshine"]["monitor_enabled"], host["sunshine"]["stale"]), check)
        item = {"connector": "DP-1", "connected": "yes", "is_internal": 1}
        connector = build_report(drm={"drm": {"available": True, "connectors": [item]}})["link"]["connectors"][0]
        self.assertEqual((False, False), (connector["connected"], connector["internal"]))
        output = build_report(outputs={"available": "yes", "outputs": [{"id": "1", "is_internal": "yes"}], "preview": "x"})["outputs"]
        self.assertEqual((False, False, False), (output["available"], output["outputs"][0]["internal"], output["preview_active"]))
        self.assertEqual({"available": False, "reason": None, "connectors": []}, build_report(drm={"drm": {"available": "yes", "connectors": []}})["link"])

    def test_digits_in_a_validated_field_are_ascii_only(self):
        # Python's \d also matches other scripts' digits; the report is printable ASCII and says so.
        arabic = "".join(chr(0x0660 + int(digit)) for digit in "1920")
        link = build_report(drm={"drm": {"available": True, "connectors": [{"connector": "DP-1", "gpu_id": f"drm:card{arabic[:1]}", "resolutions": [f"{arabic}x{arabic}", "1920x1080"]}]}})["link"]
        self.assertEqual([("DP-1", None, ["1920x1080"])], [(item["connector"], item["card"], item["resolutions"]) for item in link["connectors"]])
        journal = build_report(operations={"operations": [one_record(updated_at=f"{arabic}-09-13T12:00:00Z")]})["journal"]
        self.assertIsNone(journal["records"][0]["updated_at"])
        body = status_body()
        body["self_check"]["certificate"]["not_before"] = f"{arabic}-09-13T11:59:00Z"
        self.assertIsNone(build_report(status=body)["host"]["certificate"]["valid_from"])

    def test_a_journal_record_needs_a_valid_id_kind_and_state_though_its_id_is_never_copied(self):
        good = one_record()
        bad = [{"kind": "power.suspend", "state": "failed"}, {**good, "id": "../etc"}, {**good, "id": None}, {**good, "id": ""}, {**good, "kind": "power suspend"},
               {**good, "kind": None}, {**good, "state": 7}, {key: value for key, value in good.items() if key != "state"}, "x", None, 5, []]
        journal = build_report(operations={"operations": bad + [good]})["journal"]
        self.assertEqual(1, journal["count"])
        self.assertEqual(["power.suspend"], [record["kind"] for record in journal["records"]])
        self.assertNotIn("op-1", compact(journal))
        self.assertNotIn('"id"', compact(journal))
        self.assertEqual(0, build_report(operations={"operations": [{**good, "kind": "k" * 65}]})["journal"]["count"])
        self.assertEqual(0, build_report(operations={"operations": [{**good, "state": "s" * 33}]})["journal"]["count"])
        self.assertEqual(1, build_report(operations={"operations": [{**good, "kind": "k" * 64, "state": "s" * 32}]})["journal"]["count"])

    def test_record_ids_request_ids_and_preview_ids_never_appear(self):
        record = {"id": "op-abc123def456", "request_id": "req-abc123def456", "preview_id": "op-preview123456", "kind": "display.preview", "state": "failed",
                  "target": {"profile_id": "profile-abc123456", "output_id": "1", "token": "tok", "mac": "aa:bb:cc:dd:ee:ff", "address": "10.0.0.1"}}
        text = compact(build_report(operations={"operations": [record]}))
        for secret in ("abc123def456", "preview123456", "profile-abc", "aa:bb", "10.0.0.1", '"token"', '"mac"', '"address"', '"profile_id"'):
            self.assertNotIn(secret, text)
        self.assertIn('"output_id":"1"', text)

    def test_the_target_keeps_only_the_named_keys_in_the_named_order(self):
        wanted = ["output_id", "mode_id", "generation", "action", "restart", "automatic", "connector", "output_key", "target_output_key", "baseline_output_key", "target", "connectors", "output_keys", "mode"]
        target = {key: "v" for key in reversed(wanted)}
        target.update(generation=7, restart=True, automatic=False, connectors=["a", "b"], output_keys=["k"], mode={"width": 1920, "height": 1080, "refresh_hz": 60})
        target.update({"profile_id": "profile-1", "token": "t", "mac": "m", "request_id": "r", "preview_id": "p", "serial": "s", "edid": "e", "address": "a", "host_id": "h", "extra": "x",
                       "profile_ids": ["profile-1"], "tokens": ["t"], "macs": ["aa:bb:cc:dd:ee:ff"], "addresses": ["10.0.0.1"], "output_ids": ["1"], "request_ids": ["req-1"], "profile": {"id": "profile-1"}})
        kept = build_report(operations={"operations": [one_record(target=target)]})["journal"]["records"][0]["target"]
        self.assertEqual(wanted[:11] + ["connectors", "output_keys", "mode"], list(kept))
        self.assertEqual(
            {"output_id": "v", "mode_id": "v", "generation": 7, "action": "v", "restart": True, "automatic": False, "connector": "v", "output_key": "v",
             "target_output_key": "v", "baseline_output_key": "v", "target": "v", "connectors": ["a", "b"], "output_keys": ["k"], "mode": "1920x1080@60"}, kept)

    def test_target_scalars_lists_and_integers_follow_the_contract(self):
        def kept(**target):
            return build_report(operations={"operations": [one_record(target=target)]})["journal"]["records"][0]["target"]

        self.assertEqual({"generation": 2 ** 31 - 1}, kept(generation=2 ** 31 - 1))
        self.assertEqual({"generation": -(2 ** 31 - 1)}, kept(generation=-(2 ** 31 - 1)))
        self.assertIsNone(kept(generation=2 ** 31))
        self.assertIsNone(kept(generation=-(2 ** 31)))
        self.assertIsNone(kept(generation=1.5))
        self.assertIsNone(kept(output_id=None, mode_id=[], action={}, connector=""))
        self.assertEqual({"connectors": ["a", "b", "c", "d"]}, kept(connectors=["a", "b", "c", "d", "e", "f"]))
        self.assertEqual({"connectors": ["a"]}, kept(connectors=["a", 1]), "an entry that is not text is skipped")
        self.assertEqual({"connectors": ["a"]}, kept(connectors=["a", 1, 2, 3, "b"]), "the first four entries are looked at, then the text ones kept")
        self.assertIsNone(kept(connectors=["a", ""]), "an entry that scrubs to nothing drops the whole list")
        self.assertIsNone(kept(connectors=[1, 2]))
        self.assertIsNone(kept(connectors=[]))
        self.assertEqual({"action": "[ip]"}, kept(action="192.168.50.42"))
        self.assertEqual(48, len(kept(action="a" * 100)["action"]))
        self.assertEqual(32, len(kept(output_keys=["k" * 100])["output_keys"][0]))
        self.assertIsNone(kept(mode={"width": 0, "height": 5}))
        self.assertIsNone(build_report(operations={"operations": [one_record(target="x")]})["journal"]["records"][0]["target"])

    def test_refresh_rates_are_formatted_alike_in_every_language(self):
        outputs = {"outputs": [{"id": "1", "current_mode_id": "a", "modes": [
            {"id": "a", "width": 1920, "height": 1080, "refresh_hz": 59.94}, {"id": "b", "width": 1920, "height": 1080, "refresh_hz": 60.0},
            {"id": "c", "width": 1920, "height": 1080, "refresh_hz": 60}, {"id": "d", "width": 1920, "height": 1080, "refresh_hz": 59.9},
            {"id": "e", "width": 1920, "height": 1080, "refresh_hz": None}, {"id": "f", "width": 1920, "height": 1080, "refresh_hz": 0},
            {"id": "g", "width": 1920, "height": 1080, "refresh_hz": float("inf")}, {"id": "h", "width": 1920, "height": 1080, "refresh_hz": True},
            {"id": "j", "width": 1920, "height": 1080, "refresh_hz": 143.999},
            {"id": "k", "width": 1920, "height": 1080, "refresh_hz": 1000}, {"id": "l", "width": 1920, "height": 1080, "refresh_hz": 1000.5},
            {"id": "m", "width": 1920, "height": 1080, "refresh_hz": -60}, {"id": "n", "width": 1920, "height": 1080, "refresh_hz": float("nan")},
            {"id": "o", "width": 1920, "height": 1080, "refresh_hz": "60"}, {"id": "q", "width": 1920, "height": 1080, "refresh_hz": 120.5},
        ]}]}
        output = build_report(outputs=outputs)["outputs"]["outputs"][0]
        self.assertEqual(["1920x1080@59.94", "1920x1080@60", "1920x1080@60", "1920x1080@59.9", "1920x1080", "1920x1080", "1920x1080", "1920x1080",
                          "1920x1080@144", "1920x1080@1000", "1920x1080", "1920x1080", "1920x1080", "1920x1080", "1920x1080@120.5"], output["modes"])
        self.assertEqual("1920x1080@59.94", output["current_mode"])

    def test_the_current_mode_is_the_one_whose_id_the_output_names(self):
        modes = [{"id": "a", "width": 1280, "height": 720, "refresh_hz": 60}, {"id": "b", "width": 1920, "height": 1080, "refresh_hz": 120}, {"id": 7, "width": 640, "height": 480}]
        for current, expected in (("b", "1920x1080@120"), ("a", "1280x720@60"), ("zzz", None), (None, None), (7, None), ("b\n", None)):
            with self.subTest(current=current):
                output = build_report(outputs={"outputs": [{"id": "1", "current_mode_id": current, "modes": modes}]})["outputs"]["outputs"][0]
                self.assertEqual(expected, output["current_mode"])

    def test_output_ids_are_short_tokens_and_free_text_is_scrubbed(self):
        names = build_report(outputs={"outputs": [
            {"id": "1", "name": "TV 192.168.1.5\nroom", "description": "see https://evil.example/x"}, {"id": "x" * 17}, {"id": "x" * 16}, {"id": "../x"}, {"id": ""}, {"id": 5},
        ]})["outputs"]
        self.assertEqual(2, names["count"])
        self.assertEqual(("TV [ip] room", "see [url]"), (names["outputs"][0]["name"], names["outputs"][0]["description"]))

    def test_link_facts_keep_named_fields_only(self):
        item = {"connector": "HDMI-A-1", "gpu_id": "drm:card12", "connected": True, "is_internal": True, "monitor_vendor": "GSM", "monitor_product_id": 65535,
                "steam_output_id": "o" * 16, "resolutions": ["1920x1080", "1920x1080", "bad", 7, "99x99", "123456x1234"], "display_name": "Secret TV", "serial": "SN1", "edid": "00ff"}
        link = build_report(drm={"drm": {"available": True, "reason": "up", "connectors": [item, {**item, "connector": "bad name"}, {**item, "gpu_id": "card1", "monitor_vendor": "toolongvendor", "monitor_product_id": 65536, "steam_output_id": "o" * 17}]}})["link"]
        self.assertEqual(2, len(link["connectors"]))
        self.assertEqual({"connector": "HDMI-A-1", "card": "card12", "connected": True, "internal": True, "vendor": "GSM", "model": 65535, "output_id": "o" * 16,
                          "resolution_count": 1, "resolutions": ["1920x1080"]}, link["connectors"][0])
        second = link["connectors"][1]
        self.assertEqual((None, None, None, None), (second["card"], second["vendor"], second["model"], second["output_id"]))
        self.assertNotIn("Secret", compact(link))
        self.assertNotIn("SN1", compact(link))
        self.assertNotIn("00ff", compact(link))

    def test_an_unavailable_link_block_keeps_only_a_scrubbed_reason(self):
        for drm in ({"available": False, "reason": "no gpu at 10.0.0.1", "connectors": [{"connector": "DP-1"}]}, {"available": "yes", "reason": "no gpu at 10.0.0.1", "connectors": [{"connector": "DP-1"}]},
                    {"available": True, "reason": "no gpu at 10.0.0.1", "connectors": None}):
            self.assertEqual({"available": False, "reason": "no gpu at [ip]", "connectors": []}, build_report(drm={"drm": drm})["link"])

    def test_bounds(self):
        methods = [f"method_{index}" for index in range(12)]
        body = status_body()
        body["self_check"]["bridge"]["methods"] = methods
        self.assertEqual(methods[:MAX_METHODS], build_report(status=body)["host"]["bridge"]["methods"])
        modes = [{"id": str(index), "width": 640 + index, "height": 480, "refresh_hz": 60} for index in range(42)]
        outputs = {"outputs": [{"id": str(index), "name": f"Panel {index}", "modes": modes} for index in range(11)], "profiles": [{}] * 300}
        resolutions = [f"{1000 + index}x{700 + index}" for index in range(40)]
        drm = {"drm": {"available": True, "connectors": [{"connector": f"DP-{index}", "gpu_id": "drm:card0", "resolutions": resolutions} for index in range(11)]}}
        operations = {"operations": [one_record(id=f"op-{index}", updated_at=f"2026-09-13T12:{index:02d}:00Z") for index in range(25)]}
        report = build_report(outputs=outputs, drm=drm, operations=operations)
        self.assertEqual((11, MAX_OUTPUTS), (report["outputs"]["count"], len(report["outputs"]["outputs"])))
        self.assertEqual((42, MAX_MODES), (report["outputs"]["outputs"][0]["mode_count"], len(report["outputs"]["outputs"][0]["modes"])))
        self.assertEqual(256, report["outputs"]["saved_profiles"])
        self.assertEqual(MAX_CONNECTORS, len(report["link"]["connectors"]))
        self.assertEqual((40, MAX_RESOLUTIONS), (report["link"]["connectors"][0]["resolution_count"], len(report["link"]["connectors"][0]["resolutions"])))
        self.assertEqual((25, True, MAX_JOURNAL), (report["journal"]["count"], report["journal"]["truncated"], len(report["journal"]["records"])))
        exact = build_report(operations={"operations": operations["operations"][:20]})["journal"]
        self.assertEqual((20, False, 20), (exact["count"], exact["truncated"], len(exact["records"])))

    def test_the_byte_limit_cuts_the_journal_from_its_oldest_end(self):
        report = build_report(operations=load_input(CASES["oversize-journal"]["inputs"]["operations"]))
        journal = report["journal"]
        self.assertLessEqual(len(compact(report)), MAX_REPORT_BYTES)
        self.assertTrue(journal["truncated"])
        self.assertEqual(30, journal["count"])
        self.assertLess(len(journal["records"]), MAX_JOURNAL, "the byte limit, not the record limit, made the cut")
        self.assertGreater(len(journal["records"]), 0)
        self.assertEqual([f"2026-09-13T12:{index:02d}:00Z" for index in range(len(journal["records"]))], [record["updated_at"] for record in journal["records"]])
        # Minimal: the next record would not have fit, so records were removed one at a time and no more.
        nxt = build_report(operations={"operations": [load_input(CASES["oversize-journal"]["inputs"]["operations"])["operations"][len(journal["records"])]]})["journal"]["records"][0]
        self.assertGreater(len(compact(report)) + 1 + len(compact(nxt)), MAX_REPORT_BYTES)

    def test_a_cut_made_by_bytes_alone_still_says_truncated(self):
        # Twenty records are within the record limit, so only the byte limit can have cut here.
        twenty = {"operations": load_input(CASES["oversize-journal"]["inputs"]["operations"])["operations"][:MAX_JOURNAL]}
        report = build_report(operations=twenty)
        journal = report["journal"]
        self.assertEqual(MAX_JOURNAL, journal["count"])
        self.assertLess(len(journal["records"]), MAX_JOURNAL)
        self.assertTrue(journal["truncated"])
        self.assertLessEqual(len(compact(report)), MAX_REPORT_BYTES)
        fits = {"operations": twenty["operations"][:3]}
        self.assertFalse(build_report(operations=fits)["journal"]["truncated"])

    def test_the_cut_removes_records_only_never_another_section(self):
        report = build_report(status=status_body(), outputs=real("outputs"), drm=real("drm"), operations=load_input(CASES["oversize-journal"]["inputs"]["operations"]))
        self.assertTrue(report["journal"]["truncated"])
        self.assertEqual(build_report(status=status_body(), outputs=real("outputs"), drm=real("drm"))["host"], report["host"])
        self.assertEqual(build_report(outputs=real("outputs"))["outputs"], report["outputs"])
        self.assertEqual(build_report(drm=real("drm"))["link"], report["link"])


class FuzzTests(unittest.TestCase):
    def test_garbage_never_raises_and_always_fits(self):
        rng = random.Random(20261004)
        keys = ["id", "kind", "state", "name", "modes", "width", "height", "connector", "gpu_id", "resolutions", "target", "reason", "outputs", "operations", "drm", "connectors",
                "self_check", "certificate", "bridge", "methods", "versions", "mode", "listener", "provider", "temperature", "current_mode_id", "profiles", "preview", "available",
                "refresh_hz", "updated_at", "outcome", "restore_state", "restored", "output_keys", "steam_output_id", "monitor_vendor", "monitor_product_id"]

        def junk(depth: int = 0) -> Any:
            kind = rng.randrange(9 if depth < 4 else 4)
            if kind == 0:
                return None
            if kind == 1:
                return rng.choice([True, False])
            if kind == 2:
                return rng.choice([0, -1, 7, 2 ** 70, 59.94, float("nan"), float("inf"), -0.0, 10 ** 400, 1e308])
            if kind == 3:
                return rng.choice(["", "x" * 300, "op-1", "0.5.18\n", "‮", "192.168.0.1", "1920x1080", "display.preview", "HDMI-A-1", "drm:card0", "2026-09-13T12:00:00Z", "\x00", "a" * 5000])
            if kind == 4:
                return [junk(depth + 1) for _ in range(rng.randrange(0, 40))]
            if kind == 5:
                return {rng.choice(keys): junk(depth + 1) for _ in range(rng.randrange(0, 12))}
            return {"id": "1", "kind": "power.suspend", "state": "failed", "updated_at": "2026-09-13T12:00:00Z", "reason": junk(depth + 1), "target": junk(depth + 1), "modes": junk(depth + 1)}

        for round_ in range(600):
            report = build_report(junk(), junk(), junk(), junk())
            self.assertEqual({"format", *SECTIONS}, set(report), round_)
            self.assertLessEqual(len(compact(report)), MAX_REPORT_BYTES, round_)
            self.assertRegex(compact(report), r"^[\x20-\x7e]+$")

    def test_structurally_wrong_bodies_of_every_slot_are_null_sections_not_errors(self):
        deep: Any = []
        for _ in range(500):
            deep = [deep]
        for body in (None, True, 0, 1.5, "", "x", [], [1], {}, {"a": deep}, deep, b"bytes", object()):
            with self.subTest(body=str(body)[:20]):
                report = build_report(body, body, body, body)
                self.assertEqual({"format", *SECTIONS}, set(report))


# ---------------------------------------------------------------- the command


class FakeResponse:
    def __init__(self, raw: bytes, status: int):
        self.raw, self.status = raw, status

    def read(self, _limit: int) -> bytes:
        return self.raw

    def getheader(self, _name: str, default: Any = None) -> Any:
        return default


class FakeConnection:
    """Answers each route from a table and records every request, so reads can be counted."""

    def __init__(self, routes: dict[str, tuple[int, Any]]):
        self.sock = mock.Mock()
        self.sock.getpeercert.return_value = b"certificate"
        self.routes = routes
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self._pending: tuple[str, str, dict[str, str]] = ("", "", {})

    def connect(self) -> None:
        return None

    def putrequest(self, method: str, path: str, **_kwargs: Any) -> None:
        self._pending = (method, path, {})

    def putheader(self, key: str, value: str) -> None:
        self._pending[2][key] = value

    def endheaders(self, body: Any = None, **_kwargs: Any) -> None:
        self.requests.append((*self._pending, body))

    def getresponse(self) -> FakeResponse:
        status, body = self.routes[self.requests[-1][1]]
        if isinstance(body, BaseException):
            raise body
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return FakeResponse(raw, status)

    def close(self) -> None:
        return None


STATE = {
    "protocol_version": 1,
    "endpoint": "https://sentinel-host.example:28443",
    "host_id": "host-sentinel-state0001",
    "certificate_fingerprint": PIN,
    "client_id": "client-sentinel-state0001",
    "client_name": "Sentinel laptop",
    "token": "token-sentinel-state0001-xxxxxxxxxxxxxxxx",
    "scopes": ["status.read", "display.control"],
    "wake_mac": "a1b2c3d4e5f6",
    "wake_interface": "wlsentinel9",
    "wake_host_interface": "enpsentinel0",
    "pending_operation": {"id": "op-sentinelpending0001", "request_id": "req-sentinelpending0001", "state": "accepted", "kind": "display-order-save"},
}
STATE_SENTINELS = (
    "sentinel", "a1b2c3d4e5f6", "a1:b2:c3", "ab12ab12", "wlsentinel9", "enpsentinel0", "28443", "Sentinel laptop",
    "host-sentinel", "client-sentinel", "token-sentinel", "op-sentinelpending", "req-sentinelpending", PIN,
)


def paired_core(directory: str, **changes: Any) -> client_core.ClientCore:
    core = client_core.ClientCore(directory)
    state = copy.deepcopy(STATE)
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    core.store.save(state)
    return core


def real_routes(**overrides: Any) -> dict[str, tuple[int, Any]]:
    routes: dict[str, tuple[int, Any]] = {PATHS[slot]: (200, real(slot)) for slot in SLOTS}
    for slot, answer in overrides.items():
        routes[PATHS[slot]] = answer
    return routes


def run_command(core: client_core.ClientCore, routes: dict[str, tuple[int, Any]] | None, *argv: str) -> tuple[int, str, FakeConnection | None]:
    """Run the real CLI against the real core and transport; only the socket is faked."""
    connection = FakeConnection(routes) if routes is not None else None
    factory = mock.Mock(return_value=connection) if connection is not None else mock.Mock(side_effect=AssertionError("no connection may be opened"))
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(client_core.http.client, "HTTPSConnection", factory))
        stack.enter_context(mock.patch.object(client_core, "fingerprint_der", return_value=PIN))
        stack.enter_context(mock.patch.object(cli, "ClientCore", lambda: core))
        stack.enter_context(contextlib.redirect_stdout(output))
        code = cli.main(list(argv))
    return code, output.getvalue(), connection


def command_data(core: client_core.ClientCore, routes: dict[str, tuple[int, Any]] | None = None, *flags: str) -> tuple[dict[str, Any], FakeConnection | None]:
    code, output, connection = run_command(core, real_routes() if routes is None else routes, *flags, "diagnostics")
    assert code == 0, (code, output)
    parsed = json.loads(output)
    assert parsed["ok"] is True, parsed
    return parsed["data"], connection


class DiagnosticsCommandTests(Leaks):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self.core = paired_core(self.directory)

    def reference(self, **overrides: Any) -> dict[str, Any]:
        return build_report(**{slot: overrides.get(slot, real(slot)) for slot in SLOTS})

    def test_the_report_is_exactly_what_the_composer_builds_from_the_real_exchanges(self):
        data, _ = command_data(self.core)
        self.assertEqual(self.reference(), data["report"])
        self.assertEqual(compact(self.reference()), compact(data["report"]))
        for section in SECTIONS:
            self.assertIsNotNone(data["report"][section], section)
        self.assertEqual("0.0.0-fixture", data["report"]["host"]["plugin_version"])
        self.assertEqual(5, data["report"]["journal"]["count"])

    def test_the_document_has_exactly_the_documented_keys(self):
        data, _ = command_data(self.core)
        self.assertEqual({"format", "generated_at", "client", "local", "report"}, set(data))
        self.assertEqual("steamos-companion-omarchy-diagnostics-v1", data["format"])
        self.assertEqual({"app", "version"}, set(data["client"]))
        self.assertEqual({"paired", "scopes", "wake_configured", "pending_operation_present"}, set(data["local"]))
        self.assertEqual({"format", *SECTIONS}, set(data["report"]))

    def test_the_client_block_names_the_manifest_version(self):
        data, _ = command_data(self.core)
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual({"app": "omarchy", "version": manifest["version"]}, data["client"])
        self.assertRegex(data["client"]["version"], client_core.CLIENT_VERSION_RE)

    def test_an_unreadable_or_odd_manifest_version_is_null_not_an_error(self):
        for content in (b"not json", b"[]", b'{"version": 7}', b'{"version": "1.2.3\\nrm -rf"}', b'{"version": "1.2.3\\n"}', b'{"version": "' + b"9" * 40 + b'"}', b"{" + b" " * 70000 + b"}", b""):
            with self.subTest(content=content[:20]), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_bytes(content)
                with mock.patch.object(client_core, "MANIFEST_PATH", path):
                    self.assertIsNone(client_core._client_version())
        with mock.patch.object(client_core, "MANIFEST_PATH", Path(self.directory) / "missing.json"):
            self.assertIsNone(client_core._client_version())

    def test_generated_at_is_a_utc_instant_from_now(self):
        data, _ = command_data(self.core)
        self.assertRegex(data["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        stamp = datetime.strptime(data["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(abs(datetime.now(timezone.utc) - stamp), timedelta(seconds=30))
        with mock.patch.object(client_core, "_utc_now", return_value="2026-10-04T01:02:03Z"):
            self.assertEqual("2026-10-04T01:02:03Z", command_data(self.core)[0]["generated_at"])

    def test_local_carries_counts_and_flags_only(self):
        # The pinned status exchange says the credential holds all three scopes; the saved pairing says two.
        data, _ = command_data(self.core)
        self.assertEqual({"paired": True, "scopes": ["status.read", "power.control", "display.control"], "wake_configured": True, "pending_operation_present": True}, data["local"])
        bare = paired_core(self.directory, wake_mac=None, wake_interface=None, wake_host_interface=None, pending_operation=None)
        data, _ = command_data(bare)
        self.assertEqual({"paired": True, "scopes": ["status.read", "power.control", "display.control"], "wake_configured": False, "pending_operation_present": False}, data["local"])
        for value in data["local"].values():
            self.assertIsInstance(value, (bool, list))

    def test_local_scopes_are_what_the_host_just_said_else_what_was_saved(self):
        view_only = real("status")
        view_only["credential"] = {"scopes": ["status.read"]}
        older = real("status")
        del older["credential"]
        for label, routes, expected in (
            ("the host reports view only", real_routes(status=(200, view_only)), ["status.read"]),
            ("the host reports nothing is allowed", real_routes(status=(200, {**real("status"), "credential": {"scopes": []}})), []),
            ("an older host does not say", real_routes(status=(200, older)), ["status.read", "display.control"]),
            ("a block that says nothing", real_routes(status=(200, {**real("status"), "credential": "all"})), ["status.read", "display.control"]),
            ("the status read fails", real_routes(status=(503, {"error": "down", "message": "down"})), ["status.read", "display.control"]),
        ):
            with self.subTest(label):
                before = self.core.store.path.read_bytes()
                data, _ = command_data(self.core, routes)
                self.assertEqual(expected, data["local"]["scopes"])
                self.assertEqual(before, self.core.store.path.read_bytes(), "diagnostics still writes nothing")

    def test_nothing_from_the_local_pairing_state_or_the_hosts_secrets_is_in_the_output(self):
        for flags in (("--json",), ("--plain",), ()):
            with self.subTest(flags=flags):
                code, output, _ = run_command(self.core, real_routes(), *flags, "diagnostics")
                self.assertEqual(0, code)
                self.assert_clean(output, [real(slot) for slot in SLOTS], STATE_SENTINELS)
                self.assertNotRegex(output, r"(?i)bearer")
                self.assertNotRegex(output, r"[0-9a-f]{2}[:-][0-9a-f]{2}[:-][0-9a-f]{2}[:-][0-9a-f]{2}")

    def test_inspect_still_shows_the_local_state_it_always_did_and_diagnostics_does_not(self):
        code, output, _ = run_command(self.core, None, "--json", "inspect")
        self.assertEqual(0, code)
        shown = json.loads(output)["data"]
        for key in ("endpoint", "host_id", "client_id", "wake_mac"):
            self.assertEqual(STATE[key], shown[key])
        self.assertEqual("[redacted]", shown["certificate_fingerprint"])
        data, _ = command_data(self.core)
        for key in ("endpoint", "host_id", "client_id", "wake_mac", "wake_interface", "certificate_fingerprint", "token", "client_name"):
            self.assertNotIn(key, data["local"])
            self.assertNotIn(key, data["client"])

    def test_the_plain_format_is_line_oriented_and_flat(self):
        code, output, _ = run_command(self.core, real_routes(), "--plain", "diagnostics")
        self.assertEqual(0, code)
        lines = output.splitlines()
        self.assertIn("data.format: steamos-companion-omarchy-diagnostics-v1", lines)
        self.assertIn("data.client.app: omarchy", lines)
        self.assertIn("data.local.paired: True", lines)
        self.assertIn("data.local.scopes[1]: power.control", lines)
        self.assertIn(f"data.report.format: {REPORT_FORMAT}", lines)
        self.assertIn("data.report.host.plugin_version: 0.0.0-fixture", lines)
        self.assertIn("data.report.journal.count: 5", lines)
        # The one line that is not ASCII is the Access line, worded as the Deck words it, with its middle dot.
        self.assertEqual("Access on this host: Full access \u00b7 Status \u00b7 Power \u00b7 Display", lines[0])
        self.assertTrue(all(re.match(r"^[\x20-\x7e]+$", line) for line in lines[1:]))
        self.assertFalse(output.lstrip().startswith("{"))

    def test_one_authenticated_get_per_route_in_a_fixed_order_with_no_body_and_no_query(self):
        _, connection = command_data(self.core)
        self.assertEqual([("GET", "/v1/status"), ("GET", "/v1/display/outputs"), ("GET", "/v1/drm"), ("GET", "/v1/operations")], [request[:2] for request in connection.requests])
        for _method, _path, headers, body in connection.requests:
            self.assertIsNone(body)
            self.assertEqual("Bearer " + STATE["token"], headers["Authorization"])
            self.assertNotIn("Content-Length", headers)

    def test_a_failing_route_makes_only_its_own_section_null_and_the_exit_code_stays_zero(self):
        failures = {
            "not found": (404, {"error": "not_found", "message": "not found"}),
            "server error": (500, {"error": "boom", "message": "boom"}),
            "unauthorized": (401, {"error": "unauthorized", "message": "no"}),
            "rate limited": (429, {"error": "rate_limited", "message": "slow down"}),
            "garbage": (200, b"<html>nope</html>"),
            "not an object": (200, b"[1,2,3]"),
            "wrong protocol": (200, {"protocol_version": 2}),
            "connection failed": (200, OSError("connection refused")),
            "timeout": (200, TimeoutError("timed out")),
        }
        section_of = {"status": "host", "outputs": "outputs", "drm": "link", "operations": "journal"}
        expected = self.reference()
        for slot in SLOTS:
            for label, answer in failures.items():
                with self.subTest(slot=slot, failure=label):
                    code, output, connection = run_command(self.core, real_routes(**{slot: answer}), "--json", "diagnostics")
                    self.assertEqual(0, code, output)
                    report = json.loads(output)["data"]["report"]
                    self.assertIsNone(report[section_of[slot]])
                    for other in SECTIONS:
                        if other != section_of[slot]:
                            self.assertEqual(expected[other], report[other], other)
                    self.assertEqual(sorted(PATHS.values()), sorted(request[1] for request in connection.requests), "one attempt per route, failed or not")

    def test_every_route_failing_is_still_a_document_with_four_null_sections(self):
        routes = {path: (503, {"error": "down", "message": "down"}) for path in PATHS.values()}
        data, connection = command_data(self.core, routes)
        self.assertEqual(NULL_REPORT, data["report"])
        self.assertTrue(data["local"]["paired"])
        self.assertEqual(4, len(connection.requests))

    def test_a_failing_route_is_not_retried_and_nothing_waits_or_schedules(self):
        routes = real_routes(status=(500, {"error": "x", "message": "x"}), outputs=(200, OSError("down")), operations=(404, {"error": "not_found"}))
        with mock.patch.object(client_core.time, "sleep", side_effect=AssertionError("must not wait")), \
                mock.patch.object(client_core.concurrent.futures, "ThreadPoolExecutor", side_effect=AssertionError("must not run threads")):
            code, output, connection = run_command(self.core, routes, "--json", "diagnostics")
        self.assertEqual(0, code, output)
        counts = {}
        for request in connection.requests:
            counts[request[1]] = counts.get(request[1], 0) + 1
        self.assertEqual({path: 1 for path in PATHS.values()}, counts)

    def test_it_reads_through_the_same_pinned_transport_and_a_wrong_pin_reads_nothing(self):
        with mock.patch.object(client_core, "fingerprint_der", return_value="sha256:" + "0" * 64):
            connection = FakeConnection(real_routes())
            output = io.StringIO()
            with mock.patch.object(client_core.http.client, "HTTPSConnection", mock.Mock(return_value=connection)), \
                    mock.patch.object(cli, "ClientCore", lambda: self.core), contextlib.redirect_stdout(output):
                code = cli.main(["--json", "diagnostics"])
        self.assertEqual(0, code)
        self.assertEqual([], connection.requests, "no request line is written to a host whose certificate does not match the pin")
        self.assertEqual(NULL_REPORT, json.loads(output.getvalue())["data"]["report"])

    def test_the_timeout_comes_from_the_core_defaults(self):
        factory_calls: list[Any] = []
        connection = FakeConnection(real_routes())

        def factory(*args: Any, **kwargs: Any) -> FakeConnection:
            factory_calls.append(kwargs)
            return connection

        with mock.patch.object(client_core.http.client, "HTTPSConnection", factory), mock.patch.object(client_core, "fingerprint_der", return_value=PIN):
            self.core.run({"action": "diagnostics", "timeout": 2.5})
            self.core.run({"action": "diagnostics"})
        self.assertEqual([2.5] * 4 + [client_core.DEFAULT_TIMEOUT] * 4, [call["timeout"] for call in factory_calls])

    def test_unpaired_reports_not_paired_and_a_report_of_nulls_without_opening_a_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            for flags in (("--json",), ()):
                code, output, _ = run_command(core, None, *flags, "diagnostics")
                self.assertEqual(0, code, output)
                data = json.loads(output)["data"]
                self.assertEqual({"format", "generated_at", "client", "local", "report"}, set(data))
                self.assertEqual({"paired": False, "scopes": [], "wake_configured": False, "pending_operation_present": False}, data["local"])
                self.assertEqual(NULL_REPORT, data["report"])
            code, output, _ = run_command(core, None, "--plain", "diagnostics")
            self.assertEqual(0, code)
            self.assertIn("data.local.paired: False", output.splitlines())
            self.assertIn("data.report.host: None", output.splitlines())

    def test_a_status_read_for_diagnostics_does_not_touch_the_saved_pairing(self):
        before = self.core.store.path.read_bytes()
        self.assertEqual("001122334455", real("status")["wake_target"]["mac"], "the fixture would have replaced the saved wake MAC")
        command_data(self.core)
        self.assertEqual(before, self.core.store.path.read_bytes())
        self.assertEqual(sorted(path.name for path in Path(self.directory).iterdir()), ["client.json"])

    def test_a_journal_of_256_large_records_is_not_refused_by_the_generic_budget(self):
        big = [{"id": f"op-{index:04d}", "request_id": f"req-{index}", "state": "failed", "kind": "display.selection.preview", "created_at": "2026-09-13T12:00:00.250000Z",
                "updated_at": f"2026-09-13T12:{index // 60:02d}:{index % 60:02d}.250000Z", "outcome": "o" * 40, "reason": "r" * 256, "restore_state": "failed", "restored": True,
                "resolved_by": "b" * 100, "target": {"output_id": "1", "mode": {"width": 1, "height": 2, "refresh_hz": 60.0}, "connectors": ["v" * 40]}} for index in range(256)]
        body = {"protocol_version": 1, "operations": big}
        self.assertLess(len(json.dumps(body)), client_core.MAX_BODY_BYTES)
        with self.assertRaises(client_core.ClientError):
            client_core.validate_response_limits(body)
        data, _ = command_data(self.core, real_routes(operations=(200, body)))
        journal = data["report"]["journal"]
        self.assertIsNotNone(journal)
        self.assertEqual((256, True), (journal["count"], journal["truncated"]))
        self.assertGreater(len(journal["records"]), 0)
        self.assertEqual(build_report(operations=body)["journal"], journal)
        self.assertLessEqual(len(compact(data["report"])), MAX_REPORT_BYTES)
        self.assertEqual("2026-09-13T12:00:00Z", journal["records"][0]["updated_at"])

    def test_more_than_256_records_is_a_host_schema_violation_the_report_still_summarises(self):
        # The report bounds its own output; it does not enforce the wire limit the panel's reader does.
        body = {"protocol_version": 1, "operations": [{"id": f"op-{index}", "kind": "power.suspend", "state": "failed"} for index in range(300)]}
        data, _ = command_data(self.core, real_routes(operations=(200, body)))
        self.assertEqual((300, True, MAX_JOURNAL), (data["report"]["journal"]["count"], data["report"]["journal"]["truncated"], len(data["report"]["journal"]["records"])))

    def test_the_link_facts_are_read_whole_not_through_the_panels_allowlist(self):
        # The panel's decoder refuses a block whose connector lacks display_name; the report's own rules do not.
        drm = real("drm")
        for item in drm["drm"]["connectors"]:
            item.pop("display_name", None)
        self.assertNotIn("drm", client_core._optional_link_facts(drm))
        data, _ = command_data(self.core, real_routes(drm=(200, drm)))
        self.assertEqual(build_report(drm=drm)["link"], data["report"]["link"])
        self.assertTrue(data["report"]["link"]["available"])
        self.assertGreater(len(data["report"]["link"]["connectors"]), 0)

    def test_every_vector_case_through_the_real_command(self):
        for name, case in CASES.items():
            bodies = case_bodies(case)
            routes = {PATHS[slot]: ((200, bodies[slot]) if bodies[slot] is not None else (404, {"error": "not_found", "message": "not found"})) for slot in SLOTS}
            for flags in (("--json",), ("--plain",)):
                with self.subTest(case=name, flags=flags):
                    code, output, connection = run_command(self.core, routes, *flags, "diagnostics")
                    self.assertEqual(0, code, output)
                    self.assertEqual(4, len(connection.requests))
                    self.assert_clean(output, bodies, STATE_SENTINELS)
                    if flags == ("--json",):
                        report = json.loads(output)["data"]["report"]
                        if case["strict"]:
                            self.assertEqual(case["report"], report)
                        else:
                            for section in SECTIONS:
                                self.assertIn(report[section], (None, case["report"][section]))

    def test_diagnostics_is_not_a_panel_surface_and_the_helper_reaches_it_by_a_fixed_action_only(self):
        for name in ("Panel.qml", "BarWidget.qml"):
            source = (ROOT / name).read_text()
            self.assertNotIn('"diagnostics"', source, name)
            self.assertNotIn("'diagnostics'", source, name)
            self.assertNotRegex(source, r"enqueueHelper\([^)]*iagnostics")
        fake = FakeConnection(real_routes())
        with mock.patch.object(client_core.http.client, "HTTPSConnection", mock.Mock(return_value=fake)), mock.patch.object(client_core, "fingerprint_der", return_value=PIN):
            result = self.core.run({"action": "diagnostics", "path": "/v1/power", "method": "POST", "budget": False, "raw": True, "decode": False})
        self.assertEqual("steamos-companion-omarchy-diagnostics-v1", result["format"])
        self.assertEqual(sorted(PATHS.values()), sorted(request[1] for request in fake.requests))
        self.assertEqual({"GET"}, {request[0] for request in fake.requests})

    def test_the_helper_script_that_the_panel_runs_can_import_the_report_composer(self):
        # remote_client.py runs client_core as a top-level module, where a relative import would fail.
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run([sys.executable, str(ROOT / "client" / "remote_client.py")], input='{"action": "diagnostics"}\n', capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "XDG_STATE_HOME": state})
        self.assertEqual(0, result.returncode, result.stderr[-400:])
        reply = json.loads(result.stdout)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual({"paired": False, "scopes": [], "wake_configured": False, "pending_operation_present": False}, reply["data"]["local"])
        self.assertEqual(NULL_REPORT, reply["data"]["report"])

    def test_the_command_through_the_ctl_wrapper_prints_the_same_document(self):
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run([sys.executable, str(ROOT / "steamos-companion-ctl"), "--json", "diagnostics"], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "XDG_STATE_HOME": state}, cwd=ROOT)
        self.assertEqual(0, result.returncode, result.stderr[-400:])
        reply = json.loads(result.stdout)
        self.assertEqual(NULL_REPORT, reply["data"]["report"])
        self.assertFalse(reply["data"]["local"]["paired"])


class UndecodedRouteSwitchTests(unittest.TestCase):
    """The one new transport switch is as narrow as the budget switch before it."""

    def transport(self) -> client_core.PinnedTransport:
        return client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token")

    def test_undecoded_is_refused_on_every_route_but_the_drm_read(self):
        for method, path in (("GET", "/v1/status"), ("GET", "/v1/display/outputs"), ("GET", "/v1/operations"), ("GET", "/v1/operations/op-1"), ("GET", "/v1/display/order"),
                             ("POST", "/v1/power"), ("POST", "/v1/drm"), ("POST", "/v1/operations")):
            with self.subTest(method=method, path=path), self.assertRaises(client_core.ClientError) as caught:
                self.transport().request(method, path, None if method == "GET" else {"a": 1}, decode=False)
            self.assertEqual("route is not allowed", str(caught.exception))

    def test_the_default_drm_read_is_still_the_panels_allowlisted_rebuild(self):
        body = real("drm")
        body["drm"]["connectors"][0]["edid"] = "00ffffffffffff00"
        body["extra"] = "kept out"
        for decode, expects_edid in ((True, False), (False, True)):
            connection = FakeConnection({"/v1/drm": (200, body)})
            with mock.patch.object(client_core.http.client, "HTTPSConnection", mock.Mock(return_value=connection)), mock.patch.object(client_core, "fingerprint_der", return_value=PIN):
                answer = self.transport().request("GET", "/v1/drm", decode=decode)
            self.assertEqual(expects_edid, "edid" in answer["drm"]["connectors"][0], decode)
            self.assertEqual(not decode, "extra" in answer, decode)

    def test_there_is_one_reader_of_each_route_the_report_shares_with_the_panel(self):
        source = (ROOT / "client" / "client_core.py").read_text()
        self.assertEqual(1, source.count("decode=decode"))
        self.assertEqual(1, source.count("decode=False"), "only the diagnostics reader asks for the undecoded reply")
        self.assertEqual(1, source.count("budget=False"))
        self.assertEqual(1, source.count('"diagnostics"'))


class DocumentationTests(unittest.TestCase):
    def test_the_readme_says_inspect_is_not_for_sharing_and_diagnostics_is(self):
        readme = (ROOT / "README.md").read_text()
        self.assertTrue("steamos-companion-ctl --json diagnostics" in readme, "the README lists the command")
        warning = [paragraph for paragraph in readme.split("\n\n") if "inspect" in paragraph and "not for sharing" in paragraph]
        self.assertEqual(1, len(warning), len(warning))
        self.assertTrue("diagnostics" in warning[0])


if __name__ == "__main__":
    unittest.main()
