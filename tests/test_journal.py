"""Host operation journal: the optional `GET /v1/operations` read and its panel view.

Three layers are covered, all against the pinned Decky exchanges under
`protocol/fixtures/contract/operations/`. The transport must rebuild every record
from an allowlist, bound it, and turn a 404, a refusal, a transport failure or a
malformed reply into "not available" without raising. The panel's pure functions
are run under Node straight out of `Panel.qml`, the same way `test_linkfacts.py`
runs its block, and the embedded wording is checked against the pinned
`operation-vocabulary.json`. Static checks pin where the read is triggered: once
on entering Settings and once per explicit Refresh, five seconds apart, never on
a timer and never while the panel is closed.
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
DECKY_FRONTEND = Path("/home/hvo/Projects/steamos-companion-decky/host/frontend/index.js")
OPERATION_FIXTURES = ROOT / "protocol" / "fixtures" / "contract" / "operations"
VOCABULARY = json.loads((ROOT / "protocol" / "operation-vocabulary.json").read_text())
SCHEMA = json.loads((ROOT / "protocol" / "schema.json").read_text())
PIN = "sha256:" + "a" * 64
TABLES = ("states", "kinds", "outcomes", "restore_states")
RECORD_KEYS = {"id", "kind", "state", "created_at", "updated_at", "outcome", "reason", "restore_state", "resolved_by", "restored", "target"}
ROW_LIMIT = 10  # rows the panel shows; Decky shows 20, the panel sits above the credential controls
STANDING_NOTE = "Entries record what the host did and read back. A completed entry does not by itself show that the picture is visible."
UNAVAILABLE_TEXT = "The host journal is not available from this device."
FIXTURE_NAMES = ["list-empty", "list-mixed", "list-unauthorized"]

# Characters that must never reach a screen or a terminal: C0 and C1 controls,
# soft hyphen, Arabic letter mark, zero-width and bidirectional marks, line and
# paragraph separators, word joiners, the byte order mark, annotation marks.
HIDDEN = (
    [chr(code) for code in range(0x00, 0x20)]
    + [chr(0x7F)] + [chr(code) for code in range(0x80, 0xA0)]
    + [chr(code) for code in (0xAD, 0x61C, 0xFEFF)]
    + [chr(code) for code in range(0x200B, 0x2010)]
    + [chr(code) for code in range(0x2028, 0x202F)]
    + [chr(code) for code in range(0x2060, 0x2070)]
    + [chr(code) for code in range(0xFFF9, 0xFFFC)]
)


def fixture(name: str) -> dict:
    return json.loads((OPERATION_FIXTURES / f"{name}.json").read_text())


def fixture_body(name: str) -> dict:
    return fixture(name)["response"]["body"]


def mixed() -> list[dict]:
    return copy.deepcopy(fixture_body("list-mixed")["operations"])


def record(index: int = 0, **changes) -> dict:
    """A minimal valid wire record; `changes` override or add fields."""
    base = {
        "id": f"op-{index:04d}", "request_id": f"req-{index}", "state": "succeeded", "kind": "power.suspend",
        "created_at": "2026-09-13T12:00:00.250000Z", "updated_at": "2026-09-13T12:00:00.250000Z",
        "outcome": "method_returned", "reason": None, "target": {"action": "suspend"},
    }
    base.update(changes)
    return base


def body_of(operations) -> dict:
    return {"protocol_version": 1, "operations": operations}


# ---------------------------------------------------------------- transport helpers


class FakeResponse:
    def __init__(self, raw: bytes, status: int = 200):
        self.raw = raw
        self.status = status

    def read(self, _limit):
        return self.raw

    def getheader(self, _name, default=None):
        return default


class FakeConnection:
    """Serves one canned body per request path and records what was asked."""

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
        return FakeResponse(self.bodies[self.requests[-1][1]], self.status)

    def close(self):
        return None


def serve(bodies: dict[str, dict | bytes], status: int = 200):
    encoded = {path: body if isinstance(body, bytes) else json.dumps(body).encode() for path, body in bodies.items()}
    connection = FakeConnection(encoded, status)
    cls = mock.Mock(return_value=connection)
    return connection, mock.patch.object(client_core.http.client, "HTTPSConnection", cls), mock.patch.object(client_core, "fingerprint_der", return_value=PIN), cls


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


def journal(core: client_core.ClientCore, body: dict | bytes, status: int = 200, timeout=None) -> dict:
    _, connection, pin, _ = serve({"/v1/operations": body}, status)
    arguments = {} if timeout is None else {"timeout": timeout}
    with connection, pin:
        return core.request("journal", arguments)


def view_of(body: dict | bytes, status: int = 200) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        return journal(paired_core(directory), body, status)


def kept(raw_record: dict) -> dict | None:
    """What the transport keeps of one wire record, or None when it drops it."""
    result = view_of(body_of([raw_record]))
    assert result["available"] is True, result
    return result["operations"][0] if result["operations"] else None


def unavailable(reason: str) -> dict:
    return {"protocol_version": 1, "available": False, "operations": [], "total": 0, "truncated": False, "dropped": 0, "unavailable": reason}


class JournalTransportTests(unittest.TestCase):
    def test_the_three_pinned_list_exchanges_are_all_covered(self):
        self.assertEqual(sorted(path.stem for path in OPERATION_FIXTURES.glob("list-*.json")), FIXTURE_NAMES)

    def test_the_empty_fixture_is_an_available_empty_journal(self):
        self.assertEqual(view_of(fixture_body("list-empty")), {
            "protocol_version": 1, "available": True, "operations": [], "total": 0, "truncated": False, "dropped": 0,
        })

    def test_the_mixed_fixture_keeps_every_record_in_host_order_with_only_allowlisted_fields(self):
        wire = fixture_body("list-mixed")["operations"]
        result = view_of(fixture_body("list-mixed"))
        self.assertTrue(result["available"])
        self.assertEqual((result["total"], result["truncated"], result["dropped"]), (5, False, 0))
        self.assertEqual([item["id"] for item in result["operations"]], [item["id"] for item in wire])
        for sent, got in zip(wire, result["operations"]):
            self.assertEqual(set(got), RECORD_KEYS)
            for key in ("id", "kind", "state", "created_at", "updated_at", "outcome", "reason", "restore_state", "resolved_by", "restored"):
                self.assertEqual(got[key], sent.get(key), (sent["id"], key))
            self.assertEqual(got["target"], sent.get("target"))
        # The caller's idempotency key and the preview handle are not display facts.
        self.assertNotIn("request_id", json.dumps(result))
        self.assertNotIn("preview_id", json.dumps(result))

    def test_the_unauthorized_fixture_is_an_error_to_the_transport_and_not_available_to_the_panel(self):
        _, connection, pin, _ = serve({"/v1/operations": fixture_body("list-unauthorized")}, 401)
        with connection, pin, self.assertRaises(client_core.ClientError) as caught:
            client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", "/v1/operations")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(view_of(fixture_body("list-unauthorized"), 401), unavailable("error"))

    def test_a_host_without_the_route_answers_404_and_that_is_not_available_not_an_error(self):
        not_found = {"error": "not_found", "message": "not found"}
        self.assertEqual(view_of(not_found, 404), unavailable("not_found"))
        for status in (400, 403, 429, 500, 503):
            with self.subTest(status):
                self.assertEqual(view_of({"error": "x", "message": "m"}, status), unavailable("error"))

    def test_the_request_is_one_authenticated_get_with_no_body_and_no_query(self):
        connection, patched, pin, cls = serve({"/v1/operations": fixture_body("list-mixed")})
        with tempfile.TemporaryDirectory() as directory, patched, pin:
            paired_core(directory).request("journal", {"timeout": 2.5})
        self.assertEqual(len(connection.requests), 1)
        method, path, headers, body = connection.requests[0]
        self.assertEqual((method, path, body), ("GET", "/v1/operations", None))
        self.assertEqual(headers["Authorization"], "Bearer token-value")
        self.assertNotIn("Content-Length", headers)
        self.assertEqual(cls.call_args.kwargs["timeout"], 2.5)

    def test_a_reply_that_is_not_a_bounded_list_is_invalid_and_never_raises(self):
        for label, operations in {
            "null": None, "text": "garbage", "number": 5, "object": {"a": 1}, "true": True,
            "257 records": [record(index) for index in range(257)],
            "a thousand records": [record(index) for index in range(1000)],
        }.items():
            with self.subTest(label):
                self.assertEqual(view_of(body_of(operations)), unavailable("invalid"))
        self.assertEqual(view_of({"protocol_version": 1}), unavailable("invalid"))
        self.assertEqual(view_of({"protocol_version": 1, "operations": [], "extra": 1})["available"], True)

    def test_a_body_that_is_not_json_or_has_the_wrong_protocol_is_not_available(self):
        raw = {
            "not json": b"<html>nope</html>",
            "not an object": b"[1, 2, 3]",
            "invalid utf-8": b"\xff\xfe\x00",
            "oversize": b"{" + b" " * (client_core.MAX_BODY_BYTES + 10),
        }
        for label, body in raw.items():
            with self.subTest(label):
                self.assertEqual(view_of(body), unavailable("error"))
        for label, body in {"newer protocol": {**fixture_body("list-mixed"), "protocol_version": 2}, "no protocol": {"operations": []}}.items():
            with self.subTest(label):
                self.assertEqual(view_of(body), unavailable("error"))

    def test_a_body_nested_beyond_the_parser_is_not_available(self):
        deep = b"[" * 20000 + b"]" * 20000
        self.assertLess(len(deep), client_core.MAX_BODY_BYTES)
        self.assertEqual(view_of(deep), unavailable("error"))

    def test_any_failure_inside_the_read_is_not_available(self):
        class Boom:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                raise error

        for error in (RuntimeError("boom"), RecursionError(), OSError("down"), client_core.ClientError("nope", status=500), ValueError("x"), KeyError("k")):
            with self.subTest(type(error).__name__), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(client_core, "PinnedTransport", Boom):
                    result = paired_core(directory).request("journal", {})
                self.assertFalse(result["available"])
                self.assertEqual(result["operations"], [])
                self.assertIn(result["unavailable"], {"error", "not_found"})

    def test_a_failed_read_leaves_status_handling_and_private_state_alone(self):
        status = json.loads((ROOT / "protocol" / "fixtures" / "contract" / "status" / "sunshine-running.json").read_text())["response"]["body"]
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            before = core.store.load()
            for body, code in ((b"junk", 200), ({"error": "not_found"}, 404), (fixture_body("list-unauthorized"), 401), (body_of("x" * 5000), 200)):
                journal(core, body, code)
            self.assertEqual(core.store.load(), before)
            self.assertEqual(core.inspect()["host_id"], "host-test")
            _, connection, pin, _ = serve({"/v1/status": status})
            with connection, pin, mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"):
                result = core.request("status", {})
            self.assertEqual(result["steam_bridge"], status["steam_bridge"])


class JournalRebuildTests(unittest.TestCase):
    def test_only_the_allowlisted_fields_are_forwarded(self):
        planted = record(0, token="planted-token", secret="planted-secret", certificate_fingerprint=PIN, client_id="planted-client", preview_id="op-preview", nested={"a": 1}, extra=[1, 2])
        planted["target"] = {"action": "suspend", "token": "planted-token-in-target"}
        got = kept(planted)
        self.assertEqual(set(got), RECORD_KEYS)
        text = json.dumps(got)
        for word in ("planted-secret", "planted-client", "op-preview", PIN):
            self.assertNotIn(word, text)
        # Keys inside a target are the host's own facts; they are bounded, not interpreted.
        self.assertEqual(got["target"], {"action": "suspend", "token": "planted-token-in-target"})
        top = view_of(body_of([planted]))
        self.assertEqual(set(top), {"protocol_version", "available", "operations", "total", "truncated", "dropped"})

    def test_nothing_the_client_holds_is_in_what_the_panel_receives(self):
        with tempfile.TemporaryDirectory() as directory:
            text = json.dumps(journal(paired_core(directory), fixture_body("list-mixed")))
        for secret in ("token-value", PIN, "host-test", "client-test", "sha256:", "Bearer"):
            self.assertNotIn(secret, text)

    def test_the_identity_tokens_must_match_the_pattern_or_the_record_is_dropped(self):
        bad_ids = ["", "-leading", "_leading", ".leading", "has space", "a" * 129, "op\n", "op\x00", "op/1", "op$", "\N{LATIN SMALL LETTER E WITH ACUTE}", "\N{ARABIC-INDIC DIGIT ONE}", None, 5, ["op"], {"a": 1}, True]
        for field in ("id", "kind", "state"):
            for value in bad_ids:
                with self.subTest(field=field, value=value):
                    keeper = record(1)
                    result = view_of(body_of([record(0, **{field: value}), keeper]))
                    self.assertEqual([item["id"] for item in result["operations"]], ["op-0001"])
                    self.assertEqual((result["total"], result["dropped"]), (1, 1))
        for field in ("id", "kind", "state"):
            with self.subTest("missing", field=field):
                broken = record(0)
                del broken[field]
                self.assertEqual(view_of(body_of([broken]))["operations"], [])
        # Boundaries: 128 characters is the longest token, and unknown kinds and states are still tokens.
        longest = "a" + "b" * 127
        for value in (longest, "a", "0", "a.b:c-d_e"):
            with self.subTest("accepted", value=value):
                self.assertEqual(kept(record(0, kind=value, state=value, id=value))["kind"], value)

    def test_records_that_are_not_objects_are_dropped_and_counted(self):
        junk = [None, 5, "op-1", [], [record(0)], True, 1.5]
        result = view_of(body_of(junk + [record(1)] + junk))
        self.assertEqual([item["id"] for item in result["operations"]], ["op-0001"])
        self.assertEqual((result["total"], result["dropped"], result["truncated"]), (1, 2 * len(junk), False))

    def test_exactly_256_records_are_accepted_and_the_newest_64_are_kept_in_host_order(self):
        operations = [record(index) for index in range(256)]
        body = body_of(operations)
        self.assertLess(len(json.dumps(body)), client_core.MAX_BODY_BYTES)
        # The generic node budget would refuse a journal this size; the reader carries its own bounds.
        with self.assertRaises(client_core.ClientError):
            client_core.validate_response_limits(body)
        result = view_of(body)
        self.assertTrue(result["available"])
        self.assertEqual((result["total"], result["truncated"], result["dropped"]), (256, True, 0))
        self.assertEqual(len(result["operations"]), client_core.JOURNAL_MAX_RECORDS)
        self.assertEqual([item["id"] for item in result["operations"]], [f"op-{index:04d}" for index in range(64)])

    def test_a_full_journal_of_the_largest_legitimate_records_is_read_whole_within_the_body_limit(self):
        big = [
            record(
                index, reason="r" * 256, outcome="o" * 128, resolved_by="b" * 128, restore_state="s" * 64, restored=True,
                target={"keys": ["v" * 40], "mode": {"width": 1, "height": 2, "refresh_hz": 60.0}, "generation": 4},
            )
            for index in range(256)
        ]
        body = body_of(big)
        self.assertLess(len(json.dumps(body)), client_core.MAX_BODY_BYTES)
        result = view_of(body)
        self.assertTrue(result["available"], result)
        self.assertEqual((result["total"], len(result["operations"]), result["dropped"]), (256, 64, 0))
        self.assertEqual(result["operations"][0]["reason"], "r" * 256)
        self.assertEqual(result["operations"][0]["target"]["keys"], ["v" * 40])

    def test_a_short_journal_is_not_truncated_and_a_65th_record_is(self):
        self.assertEqual(view_of(body_of([record(index) for index in range(64)]))["truncated"], False)
        result = view_of(body_of([record(index) for index in range(65)]))
        self.assertEqual((result["truncated"], len(result["operations"]), result["total"]), (True, 64, 65))

    def test_dropped_records_do_not_count_toward_the_kept_limit(self):
        operations = [None] * 100 + [record(index) for index in range(100)]
        result = view_of(body_of(operations))
        self.assertEqual((len(result["operations"]), result["total"], result["dropped"], result["truncated"]), (64, 100, 100, True))
        self.assertEqual(result["operations"][0]["id"], "op-0000")

    def test_text_fields_lose_hidden_characters_and_are_cut_at_the_schema_bounds(self):
        limits = {"created_at": 64, "updated_at": 64, "outcome": 128, "reason": 256, "restore_state": 64, "resolved_by": 128}
        operation = SCHEMA["$defs"]["operation"]["properties"]
        for key, limit in limits.items():
            self.assertEqual(operation[key]["maxLength"], limit, key)
            with self.subTest(key):
                self.assertEqual(kept(record(0, **{key: "x" * (limit + 50)}))[key], "x" * limit)
                self.assertEqual(kept(record(0, **{key: "x" * limit}))[key], "x" * limit)
        for char in HIDDEN:
            with self.subTest(char=f"U+{ord(char):04X}"):
                self.assertEqual(kept(record(0, reason=f"a{char}b{char}"))["reason"], "ab")
        self.assertEqual(kept(record(0, reason="line\nbreak\ttab\r"))["reason"], "linebreaktab")
        # Printable text, including accents and spaces inside, is left alone.
        self.assertEqual(kept(record(0, reason="Écran · 1920x1080 — ok"))["reason"], "Écran · 1920x1080 — ok")
        self.assertEqual(kept(record(0, reason="  padded  "))["reason"], "padded")

    def test_an_empty_or_hidden_only_or_non_text_field_is_none_not_an_empty_string(self):
        for key in ("created_at", "updated_at", "outcome", "reason", "restore_state", "resolved_by"):
            for value in ("", "   ", "".join(HIDDEN), None, 5, True, ["x"], {"a": 1}):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(kept(record(0, **{key: value}))[key])
        self.assertIsNone(kept(record(0))["resolved_by"])

    def test_restored_is_a_boolean_or_none(self):
        for value, expected in ((True, True), (False, False), (None, None), (1, None), (0, None), ("true", None), ([], None)):
            with self.subTest(value):
                self.assertIs(kept(record(0, restored=value))["restored"], expected)

    def test_the_target_is_a_bounded_map_of_scalars_short_string_lists_and_one_level_of_scalars(self):
        self.assertEqual(kept(record(0, target={"s": "x", "i": 3, "f": 1.5, "b": False, "neg": -4}))["target"], {"s": "x", "i": 3, "f": 1.5, "b": False, "neg": -4})
        self.assertEqual(kept(record(0, target={"list": ["a", "b"], "mode": {"width": 1920, "height": 1080, "refresh_hz": 59.94, "name": "x"}}))["target"],
                         {"list": ["a", "b"], "mode": {"width": 1920, "height": 1080, "refresh_hz": 59.94, "name": "x"}})
        for label, target in {"null": None, "list": ["a"], "text": "x", "number": 5, "true": True, "empty": {}, "nothing usable": {"a": None, "b": [], "c": {}, "d": ""}}.items():
            with self.subTest(label):
                self.assertIsNone(kept(record(0, target=target))["target"])

    def test_the_target_never_holds_more_than_eight_facts(self):
        target = {f"key{index}": index for index in range(30)}
        got = kept(record(0, target=target))["target"]
        self.assertEqual(list(got), [f"key{index}" for index in range(8)])
        # Facts that fail do not use up the eight.
        mixed_target = {"bad key": 1, "-bad": 2, **{f"ok{index}": index for index in range(10)}}
        self.assertEqual(list(kept(record(0, target=mixed_target))["target"]), [f"ok{index}" for index in range(8)])

    def test_target_keys_must_be_tokens(self):
        target = {"": 1, "has space": 2, "-lead": 3, "a" * 129: 4, "new\nline": 5, "tail\n": 6, "good_key.1:x-y": 7}
        self.assertEqual(kept(record(0, target=target))["target"], {"good_key.1:x-y": 7})

    def test_target_lists_must_be_short_and_all_text(self):
        self.assertEqual(kept(record(0, target={"keys": [f"k{index}" for index in range(16)]}))["target"], {"keys": [f"k{index}" for index in range(16)]})
        for label, value in {
            "17 entries": [f"k{index}" for index in range(17)],
            "a number inside": ["a", 1],
            "null inside": ["a", None],
            "nested list": [["a"]],
            "object inside": [{"a": "b"}],
            "empty entry": ["a", ""],
            "hidden-only entry": ["a", "".join(HIDDEN)],
            "empty": [],
        }.items():
            with self.subTest(label):
                self.assertNotIn("keys", kept(record(0, target={"keys": value, "kept": 1}))["target"])
        self.assertEqual(kept(record(0, target={"keys": ["x" * 400]}))["target"], {"keys": ["x" * 128]})

    def test_a_nested_map_is_one_level_of_scalars_at_most_six_of_them(self):
        nested = {"a": 1, "b": {"deeper": 1}, "c": [1, 2], "d": None, "e": "t", "f": True, "g": 7.5, "h": "z", "i": "w", "bad key": 1}
        self.assertEqual(kept(record(0, target={"m": nested}))["target"], {"m": {"a": 1, "e": "t", "f": True, "g": 7.5, "h": "z", "i": "w"}})
        deep: dict = {"leaf": 1}
        for _ in range(60):
            deep = {"x": deep}
        self.assertIsNone(kept(record(0, target={"m": deep}))["target"])
        self.assertEqual(kept(record(0, target={"m": deep, "k": 1}))["target"], {"k": 1})

    def test_scalars_are_finite_in_range_and_cut_and_hidden_characters_are_stripped(self):
        body = (
            '{"protocol_version":1,"operations":[{"id":"op-1","kind":"power.suspend","state":"failed","target":'
            '{"nan":NaN,"inf":Infinity,"ninf":-Infinity,"big":9007199254740993,"edge":9007199254740991,"huge":1e300,"ok":2}}]}'
        )
        self.assertEqual(view_of(body.encode())["operations"][0]["target"], {"edge": 9007199254740991, "ok": 2})
        got = kept(record(0, target={"t": "x" * 300, "h": f"a{chr(0x202E)}b{chr(0x200B)}c\n"}))["target"]
        self.assertEqual(got, {"t": "x" * 128, "h": "abc"})

    def test_booleans_are_not_numbers_and_numbers_are_not_booleans(self):
        got = kept(record(0, target={"t": True, "f": False, "one": 1, "zero": 0}))["target"]
        self.assertIs(got["t"], True)
        self.assertIs(got["f"], False)
        self.assertEqual((got["one"], got["zero"]), (1, 0))
        self.assertIsNot(got["one"], True)
        self.assertIsNot(got["zero"], False)

    def test_pinned_schema_and_the_client_agree_on_the_record_shape(self):
        operation = SCHEMA["$defs"]["operation"]
        self.assertEqual(SCHEMA["properties"]["operations"]["maxItems"], client_core.JOURNAL_MAX_WIRE_RECORDS)
        self.assertTrue(set(operation["properties"]) >= RECORD_KEYS)
        self.assertEqual(set(operation["required"]), {"id", "request_id", "state", "kind", "created_at", "updated_at"})
        self.assertTrue(operation["additionalProperties"])
        for key in ("id", "kind", "state"):
            self.assertIn(key, operation["required"])
        self.assertEqual(operation["properties"]["id"]["maxLength"], 128)
        self.assertEqual(set(operation["properties"]["state"]["enum"]), set(client_core.OPERATION_STATES))
        self.assertEqual(client_core.JOURNAL_MAX_RECORDS, 64)
        self.assertEqual(client_core.JOURNAL_TARGET_MAX_KEYS, 8)
        for item in fixture_body("list-mixed")["operations"]:
            self.assertTrue(client_core.ID_RE.fullmatch(item["id"]))

    def test_the_leniency_for_this_reply_is_not_borrowed_by_any_other_route(self):
        transport = client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token")
        for method, path in (("GET", "/v1/status"), ("GET", "/v1/drm"), ("GET", "/v1/display/outputs"), ("GET", "/v1/operations/op-1"), ("POST", "/v1/power"), ("POST", "/v1/operations")):
            with self.subTest(method=method, path=path), self.assertRaises(client_core.ClientError) as caught:
                transport.request(method, path, None if method == "GET" else {"a": 1}, budget=False)
            self.assertEqual(str(caught.exception), "route is not allowed")
        source = (ROOT / "client" / "client_core.py").read_text()
        self.assertEqual(source.count("budget=False"), 1)
        self.assertEqual(source.count('"journal"'), 1)
        self.assertEqual(source.count('"/v1/operations"'), 2)  # the allowlist guard and the one reader every journal action shares
        status = json.loads((ROOT / "protocol" / "fixtures" / "contract" / "status" / "sunshine-running.json").read_text())["response"]["body"]
        _, connection, pin, _ = serve({"/v1/status": {**status, "reason": "x" * 2000}})
        with connection, pin, self.assertRaises(client_core.ClientError):
            transport.request("GET", "/v1/status")

    def test_the_raw_operations_action_still_passes_the_host_body_through_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            for name in ("list-empty", "list-mixed"):
                _, connection, pin, _ = serve({"/v1/operations": fixture_body(name)})
                with connection, pin:
                    self.assertEqual(core.request("operations", {}), fixture_body(name))
            # A raw record keeps the fields the panel's rebuild leaves out.
            _, connection, pin, _ = serve({"/v1/operations": fixture_body("list-mixed")})
            with connection, pin:
                self.assertIn("request_id", core.request("operations", {})["operations"][0])

    def test_the_raw_operations_action_reads_a_full_256_record_journal(self):
        full = {"protocol_version": 1, "operations": [record(index) for index in range(256)]}
        with self.assertRaises(client_core.ClientError):
            client_core.validate_response_limits(full)  # the generic node budget alone would refuse this
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            _, connection, pin, _ = serve({"/v1/operations": full})
            with connection, pin:
                self.assertEqual(core.request("operations", {}), full)

    def test_the_helper_reaches_the_read_by_a_fixed_action_only(self):
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            fake, patched, pin, _ = serve({"/v1/operations": fixture_body("list-empty")})
            with patched, pin:
                result = core.run({"action": "journal", "path": "/v1/power", "method": "POST", "budget": False})
            self.assertTrue(result["available"])
            self.assertEqual([request[:2] for request in fake.requests], [("GET", "/v1/operations")])
        source = (ROOT / "client" / "client_core.py").read_text()
        routes = re.search(r"routes = \{(.*?)\n        \}", source, re.S).group(1)
        self.assertNotIn("operations", routes)
        self.assertNotIn("journal", routes)


class JournalCliTests(unittest.TestCase):
    class StubCore:
        def __init__(self, answer):
            self.answer = answer
            self.requests: list[dict] = []

        def run(self, request):
            self.requests.append(request)
            return self.answer

    def run_cli(self, answer, *argv):
        stub = self.StubCore(answer)
        output = io.StringIO()
        with mock.patch.object(cli, "ClientCore", lambda: stub), contextlib.redirect_stdout(output):
            code = cli.main(list(argv))
        return code, output.getvalue(), stub

    def test_journal_list_is_still_the_raw_host_list(self):
        raw = fixture_body("list-mixed")
        code, output, stub = self.run_cli(raw, "--json", "journal", "list")
        self.assertEqual((code, stub.requests), (0, [{"action": "operations"}]))
        self.assertEqual(json.loads(output), {"ok": True, "data": raw})
        self.assertIn("request_id", output)

    def test_journal_list_through_the_real_core_is_byte_for_byte_the_host_body(self):
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            _, connection, pin, _ = serve({"/v1/operations": fixture_body("list-mixed")})
            output = io.StringIO()
            with connection, pin, mock.patch.object(cli, "ClientCore", lambda: core), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--json", "journal", "list"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "data": fixture_body("list-mixed")})

    def test_journal_view_reads_what_the_panel_shows_and_unavailable_is_exit_zero(self):
        for label, answer in {"records": view_of(fixture_body("list-mixed")), "no route": unavailable("not_found")}.items():
            for flag in ("--json", "--plain"):
                with self.subTest(label=label, flag=flag):
                    code, output, stub = self.run_cli(answer, flag, "journal", "view")
                    self.assertEqual((code, stub.requests), (0, [{"action": "journal"}]))
                    if flag == "--json":
                        self.assertEqual(json.loads(output), {"ok": True, "data": answer})
                    elif label == "records":
                        self.assertIn("data.operations[0].kind: display.preview", output)
                        self.assertIn("data.total: 5", output)
                    else:
                        self.assertIn("data.unavailable: not_found", output)

    def test_journal_view_prints_nothing_a_terminal_could_act_on(self):
        hostile = body_of([record(0, reason="red\x1b[31m\x07 text", target={"k": "a\x1b]0;title\x07b"})])
        with tempfile.TemporaryDirectory() as directory:
            core = paired_core(directory)
            _, connection, pin, _ = serve({"/v1/operations": hostile})
            output = io.StringIO()
            with connection, pin, mock.patch.object(cli, "ClientCore", lambda: core), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["--plain", "journal", "view"]), 0)
        self.assertNotRegex(output.getvalue(), r"[\x00-\x08\x0b-\x1f\x7f]")
        self.assertIn("red[31m text", output.getvalue())


# ---------------------------------------------------------------- panel presentation


def panel_source() -> str:
    return (ROOT / "Panel.qml").read_text()


def panel_block() -> str:
    found = re.findall(r"// journal:begin\n(.*?)  // journal:end", panel_source(), re.S)
    assert len(found) == 1, "Panel.qml must carry exactly one journal block"
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
const decky = vm.runInNewContext(`${input.decky}; ({journalRows, JOURNAL_VOCABULARY, JOURNAL_NOTE})`, {});
const plain = value => JSON.parse(JSON.stringify(value));
process.stdout.write(JSON.stringify({
  vocabulary: [plain(mine.journalVocabulary()), plain(decky.JOURNAL_VOCABULARY)],
  note: [mine.journalNote(), decky.JOURNAL_NOTE],
  titles: input.records.map(item => [mine.journalRows([item])[0].title, decky.journalRows([item])[0].title]),
}));
"""


def run_node(harness: str, payload: dict):
    result = subprocess.run(["node", "-e", harness], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout)


def call_all(calls: list[tuple]) -> list:
    """Run (function, *args) calls out of Panel.qml; every one must return rather than throw."""
    out = run_node(HARNESS, {"block": panel_block(), "calls": [[name, list(args)] for name, *args in calls]})
    for index, item in enumerate(out):
        assert "error" not in item, (calls[index], item)
    return [item["value"] for item in out]


def call(name: str, *args):
    return call_all([(name, *args)])[0]


def rows_of(operations) -> list[dict]:
    return call("journalRows", operations)


def lines_of(item: dict) -> list[str]:
    return rows_of([item])[0]["lines"]


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml journal functions")
class JournalVocabularyTests(unittest.TestCase):
    def test_the_embedded_wording_equals_the_pinned_vocabulary_file(self):
        embedded = call("journalVocabulary")
        self.assertEqual(set(embedded), set(TABLES))
        # A table added upstream, or a renamed one, must fail here rather than be ignored.
        self.assertEqual(set(VOCABULARY) - {"version", "description"}, set(TABLES))
        for table in TABLES:
            self.assertEqual(embedded[table], VOCABULARY[table], table)
        self.assertEqual(VOCABULARY["version"], 1)

    def test_only_codes_that_say_visible_produce_wording_that_does(self):
        embedded = call("journalVocabulary")
        for table in TABLES:
            for code, label in embedded[table].items():
                with self.subTest(table=table, code=code):
                    self.assertEqual("visible" in code.lower(), "visible" in label.lower())
        # And there are such codes, so the rule is not vacuous.
        self.assertTrue([code for code in embedded["outcomes"] if "visible" in code])
        self.assertEqual(call("journalVocabulary")["states"]["succeeded"], "Completed")

    def test_a_completed_state_is_never_worded_as_a_visible_picture(self):
        words = call("journalVocabulary")
        pairs = [(state, outcome) for state in words["states"] for outcome in [None, *words["outcomes"]]]
        results = call_all([("journalRows", [{"id": "op-1", "kind": "display.preview", "state": state, "outcome": outcome}]) for state, outcome in pairs])
        for (state, outcome), rows in zip(pairs, results):
            text = " ".join([rows[0]["title"], *rows[0]["lines"]]).lower()
            with self.subTest(state=state, outcome=outcome):
                self.assertEqual("visible" in text, bool(outcome) and "visible" in outcome)

    def test_a_restore_state_is_never_worded_as_a_visible_picture(self):
        words = call("journalVocabulary")
        for code in words["restore_states"]:
            self.assertNotIn("visible", " ".join(lines_of({"id": "op-1", "kind": "display.restore", "state": "succeeded", "restore_state": code})).lower())

    def test_unknown_codes_are_shown_as_sanitised_text_never_hidden(self):
        bell, esc, zwsp = chr(7), chr(27), chr(0x200B)
        rows = rows_of([{
            "id": "op-x", "kind": f"future.kind{bell}", "state": f"weird{esc}[31m", "outcome": f"new_outcome{zwsp}",
            "restore_state": "mystery", "reason": f"bad{chr(0)}text{zwsp}", "updated_at": "not a time", "target": "nope",
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "future.kind · weird[31m")
        self.assertEqual(rows[0]["lines"], ["Recorded · new_outcome", "mystery", "Reason · badtext"])
        # Even a prototype name is an unknown code, not a table lookup.
        for code in ("__proto__", "constructor", "toString", "hasOwnProperty"):
            self.assertEqual(rows_of([{"id": "op-1", "kind": code, "state": code, "outcome": code}])[0]["title"], f"{code} · {code}")
        self.assertEqual(lines_of({"id": "op-1", "kind": "power.suspend", "state": "failed", "outcome": "constructor"}), ["Recorded · constructor"])
        # Unknown codes are cut at 64 characters.
        self.assertEqual(rows_of([{"id": "op-1", "kind": "k" * 200, "state": "failed"}])[0]["title"], "k" * 64 + " · Failed")

    def test_a_missing_code_gets_a_plain_word_not_a_blank(self):
        self.assertEqual(rows_of([{"id": "op-1"}])[0]["title"], "Operation · State unknown")
        self.assertEqual(rows_of([{"id": "op-1", "kind": 5, "state": None}])[0]["title"], "Operation · State unknown")
        self.assertEqual(rows_of([{"id": "op-1", "kind": "", "state": ""}])[0]["lines"], [])


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml journal functions")
class JournalPresentationTests(unittest.TestCase):
    def test_the_extracted_block_is_pure(self):
        block = panel_block()
        for forbidden in ("root.", "statusData", "enqueueHelper", "remote(", "Timer", "Date.now", "Qt.", "journalState", "journalData", "localData", "setInterval", "setTimeout"):
            self.assertNotIn(forbidden, block)
        for name in ("journalVocabulary", "journalNote", "journalUnavailableText", "journalRowLimit", "journalMinGapMs", "journalMayRead", "journalPrintable", "journalText", "journalWord", "journalValue", "journalRequested", "journalTime", "journalRows", "journalFromResult", "journalModel"):
            self.assertIn(f"function {name}(", block)
        # ES5 only: it runs in Qt's engine and under Node alike.
        code = "\n".join(line.split("//")[0] for line in block.splitlines())
        self.assertNotRegex(code, r"=>|\blet\b|\bconst\b|`|\.\.\.|\bclass\b|\.padStart|\.includes\(|Number\.isInteger|\.startsWith|\.endsWith|for\s*\(\s*(?:var\s+)?\w+\s+of\b")

    def test_the_source_carries_no_invisible_or_bidirectional_character(self):
        source = panel_source()
        for char in HIDDEN:
            if char not in "\n\t":
                self.assertNotIn(char, source, f"U+{ord(char):04X}")

    def test_the_real_mixed_fixture_rows(self):
        rows = rows_of(fixture_body("list-mixed")["operations"])
        self.assertEqual([row["title"] for row in rows], [
            "Display mode preview · Returned, not confirmed",
            "Gaming Mode display preference · Completed",
            "Screen selection preview · Failed",
            "Suspend · Returned, not confirmed",
            "Save current display mode · Completed",
        ])
        self.assertEqual(rows[0]["lines"], [
            "Requested · output id: 1 · mode id: 2 · generation: 4",
            "Recorded · Mode request returned; waiting for confirmation",
            "Reason · Preview is waiting for visible-picture confirmation or host timeout",
            "Updated · 2026-09-13 12:00:24 UTC",
        ])
        self.assertEqual(rows[1]["lines"][:2], [
            "Requested · output keys: drm:card0:HDMI-A-1, drm:card0:DP-1 · connectors: HDMI-A-1, DP-1 · generation: 4 · restart: false",
            "Recorded · Gamescope output saved; applies after Gaming Mode restarts",
        ])
        self.assertEqual(rows[2]["lines"], [
            "Requested · target output key: drm:card0:HDMI-A-1 · baseline output key: drm:card0:DP-1 · generation: 4",
            "Previous screen restored",
            "Reason · host preview deadline expired; baseline screen restored",
            "Updated · 2026-09-13 12:00:19 UTC",
        ])
        self.assertEqual(rows[3]["lines"][1], "Recorded · Steam returned from the call; the physical result is not confirmed")
        self.assertEqual(rows[4]["lines"], [
            "Requested · profile id: profile-fixture0001xxxxxxxxxxxxx",
            "Recorded · Current mode read back; owner confirmed the picture was visible",
            "Updated · 2026-09-13 12:00:00 UTC",
        ])
        for row in rows:
            self.assertNotRegex(row["title"], r"(?i)visible")
        self.assertEqual(len({row["id"] for row in rows}), 5)

    def test_what_the_transport_hands_over_renders_the_same_rows_as_the_raw_fixture(self):
        raw = rows_of(fixture_body("list-mixed")["operations"])
        rebuilt = rows_of(view_of(fixture_body("list-mixed"))["operations"])
        self.assertEqual(raw, rebuilt)

    def test_the_mixed_fixture_model_has_the_note_and_no_truncation_line(self):
        data = view_of(fixture_body("list-mixed"))
        model = call("journalModel", "ready", data)
        self.assertEqual((model["state"], model["message"], model["more"], model["note"]), ("ready", "", "", STANDING_NOTE))
        self.assertEqual(len(model["rows"]), 5)

    def test_the_states_of_the_group(self):
        data = view_of(fixture_body("list-mixed"))
        empty = view_of(fixture_body("list-empty"))
        idle, loading, unavailable_model, none, ready_empty, ready_bad = call_all([
            ("journalModel", "idle", None), ("journalModel", "loading", data), ("journalModel", "unavailable", data),
            ("journalModel", "nonsense", data), ("journalModel", "ready", empty), ("journalModel", "ready", {"operations": "x"}),
        ])
        self.assertEqual((idle["state"], idle["message"], idle["rows"], idle["note"]), ("idle", "Not read yet. Choose Refresh journal to read it.", [], ""))
        self.assertEqual((loading["state"], loading["message"], loading["rows"], loading["note"]), ("loading", "Loading the host journal…", [], ""))
        self.assertEqual((unavailable_model["message"], unavailable_model["rows"], unavailable_model["note"], unavailable_model["more"]), (UNAVAILABLE_TEXT, [], "", ""))
        self.assertEqual(none["state"], "idle")
        self.assertEqual((ready_empty["message"], ready_empty["rows"], ready_empty["note"]), ("The host has recorded no operations for this device yet.", [], STANDING_NOTE))
        self.assertEqual((ready_bad["state"], ready_bad["message"], ready_bad["note"]), ("unavailable", UNAVAILABLE_TEXT, ""))
        for junk in (None, UNDEFINED, "x", 5, [], {}, {"operations": None}):
            self.assertEqual(call("journalModel", "ready", junk)["state"], "unavailable")

    def test_the_row_limit_and_the_truncation_line(self):
        sixty_four = view_of(body_of([record(index) for index in range(64)]))
        model = call("journalModel", "ready", sixty_four)
        self.assertEqual(call("journalRowLimit"), ROW_LIMIT)
        self.assertEqual(len(model["rows"]), ROW_LIMIT)
        self.assertEqual(model["more"], f"Showing the newest {ROW_LIMIT} of 64.")
        self.assertEqual([row["id"] for row in model["rows"]], [f"op-{index:04d}" for index in range(ROW_LIMIT)])
        full = view_of(body_of([record(index) for index in range(256)]))
        self.assertEqual(call("journalModel", "ready", full)["more"], f"Showing the newest {ROW_LIMIT} of 256.")
        # Exactly the row limit is not truncated; one more is.
        self.assertEqual(call("journalModel", "ready", view_of(body_of([record(index) for index in range(ROW_LIMIT)])))["more"], "")
        self.assertEqual(call("journalModel", "ready", view_of(body_of([record(index) for index in range(ROW_LIMIT + 1)])))["more"], f"Showing the newest {ROW_LIMIT} of {ROW_LIMIT + 1}.")
        # A total that disagrees with the list is never smaller than what is shown.
        self.assertEqual(call("journalModel", "ready", {**sixty_four, "total": 3})["more"], f"Showing the newest {ROW_LIMIT} of 64.")
        self.assertEqual(call("journalModel", "ready", {**sixty_four, "total": "many"})["more"], f"Showing the newest {ROW_LIMIT} of 64.")

    @unittest.skipUnless(DECKY_FRONTEND.exists(), "the Decky checkout is not present")
    def test_wording_and_titles_match_the_decky_frontend(self):
        found = re.search(r"/\* journal:begin \*/([\s\S]*?)/\* journal:end \*/", DECKY_FRONTEND.read_text())
        self.assertTrue(found, "the Decky journal block moved")
        words = VOCABULARY
        records = [
            {"id": "op-1", "kind": kind, "state": state}
            for kind in [*words["kinds"], "future.kind"] for state in [*words["states"], "weird"]
        ] + fixture_body("list-mixed")["operations"]
        result = run_node(PARITY_HARNESS, {"block": panel_block(), "decky": found.group(1), "records": records})
        self.assertEqual(result["vocabulary"][0], result["vocabulary"][1])
        self.assertEqual(result["note"][0], result["note"][1])
        for record_, (mine, theirs) in zip(records, result["titles"]):
            self.assertEqual(mine, theirs, record_)

    def test_the_standing_note_and_the_unavailable_wording_are_exact(self):
        self.assertEqual(call("journalNote"), STANDING_NOTE)
        self.assertEqual(call("journalUnavailableText"), UNAVAILABLE_TEXT)

    def test_reads_are_spaced_at_least_five_seconds_apart(self):
        gap = call("journalMinGapMs")
        self.assertEqual(gap, 5000)
        base = 1_800_000_000_000
        cases = [
            (0, base, True), (None, base, True), (-5, base, True), (UNDEFINED, base, True), ("x", base, True),
            (base, base, False), (base, base + 1, False), (base, base + 4999, False), (base, base + 5000, True), (base, base + 60_000, True),
            # A clock that stepped back a little (or lags) keeps the gate shut; one that jumped far back opens it.
            (base, base - 1, False), (base, base - 4999, False), (base, base - 5001, True), (base, base - 86_400_000, True),
            # No usable clock, no read.
            (base, None, False), (base, UNDEFINED, False), (base, "now", False),
        ]
        results = call_all([("journalMayRead", last, now) for last, now, _ in cases])
        for (last, now, expected), got in zip(cases, results):
            self.assertEqual(got, expected, (last, now))

    def test_text_is_cleaned_cut_and_never_split_mid_pair(self):
        for char in HIDDEN:
            self.assertEqual(call("journalText", f"a{char}b", 10), "ab", f"U+{ord(char):04X}")
        self.assertEqual(call("journalText", "x" * 500, 128), "x" * 128)
        self.assertEqual(call("journalText", "  hi  ", 10), "hi")
        self.assertEqual(call("journalText", "Écran · 画面", 20), "Écran · 画面")
        for junk in (None, UNDEFINED, 5, True, [], {}, ["a"]):
            self.assertEqual(call("journalText", junk, 10), "")
        pair = chr(0x1F5A5) + chr(0xFE0F)
        self.assertEqual(call("journalText", "ab" + chr(0x1F5A5), 3), "ab")
        self.assertEqual(call("journalText", "ab" + chr(0x1F5A5), 4), "ab" + chr(0x1F5A5))
        self.assertEqual(call("journalText", pair * 100, 7), pair * 2)
        # A huge string costs a bounded amount.
        self.assertEqual(len(call("journalText", "x" * 1_000_000, 64)), 64)

    def test_the_requested_line_follows_the_targets_facts(self):
        results = call_all([
            ("journalRequested", {"output_id": "1", "mode_id": "2", "generation": 4}),
            ("journalRequested", {"mode": {"width": 1920, "height": 1080, "refresh_hz": 59.94}}),
            ("journalRequested", {"mode": {"width": 1920, "height": 1080}}),
            ("journalRequested", {"mode": {"width": 1920.5, "height": 1080, "refresh_hz": 60}}),
            ("journalRequested", {"restart": False, "profile_id": "p"}),
            ("journalRequested", {"keys": ["a", "b", 3, None, {"x": 1}]}),
            ("journalRequested", {"nested": {"a": 1, "b": "x"}}),
            ("journalRequested", {"nested": {"a": {"b": {"c": 1}}}}),
            ("journalRequested", {"a": None, "b": "", "c": [], "d": {}}),
            ("journalRequested", {"weird_key name": 1}),
        ])
        self.assertEqual(results, [
            "output id: 1 · mode id: 2 · generation: 4",
            "mode: 1920x1080@59.94",
            "mode: 1920x1080",
            "mode: width 1920.5, height 1080, refresh hz 60",
            "restart: false · profile id: p",
            "keys: a, b, 3, x 1",
            "nested: a 1, b x",
            "",
            "",
            "weird key name: 1",
        ])

    def test_the_requested_line_is_bounded_whatever_the_target_holds(self):
        wide = {f"key{index}": ["v" * 128] * 16 for index in range(8)}
        line = call("journalRequested", wide)
        self.assertEqual(len(line), 360)
        self.assertTrue(line.endswith("…"))
        self.assertEqual(call("journalRequested", {f"k{index}": index for index in range(100)}), " · ".join(f"k{index}: {index}" for index in range(8)))
        deep: dict = {"leaf": 1}
        for _ in range(500):
            deep = {"x": deep}
        self.assertEqual(call("journalRequested", {"a": deep, "b": 1}), "b: 1")
        for junk in (None, UNDEFINED, "x", 5, [], ["a"], True):
            self.assertEqual(call("journalRequested", junk), "")

    def test_the_time_line_is_the_hosts_utc_format_or_nothing(self):
        good = ["2026-09-13T12:00:24.250000Z", "2026-09-13T12:00:24Z", "2026-09-13T12:00:24.5Z", "2026-09-13T12:00:24.123456789Z"]
        bad = ["not a time", "", None, 5, "2026-09-13", "2026-09-13T12:00:24", "2026-09-13T12:00:24+07:00", "2026-09-13 12:00:24Z", "x2026-09-13T12:00:24Z", "2026-09-13T12:00:24Zjunk", "2026-09-13T12:00:24Z\n"]
        results = call_all([("journalTime", value) for value in good + bad])
        self.assertEqual(results[: len(good)], ["2026-09-13 12:00:24 UTC"] * len(good))
        self.assertEqual(results[len(good):], [""] * len(bad))

    def test_rows_degrade_instead_of_throwing_on_junk(self):
        junk = [None, True, False, 0, 1.5, -1, "", "x", [], [None], [{}], {}, {"a": 1}, UNDEFINED]
        calls = [("journalRows", value) for value in junk]
        calls.append(("journalRows", junk))
        for key in ("id", "kind", "state", "outcome", "restore_state", "reason", "updated_at", "target"):
            for value in junk:
                calls.append(("journalRows", [{"id": "op-1", "kind": "power.suspend", "state": "failed", **({key: value} if value != UNDEFINED else {})}]))
        for value in call_all(calls):
            text = json.dumps(value)
            for bad in ("undefined", "NaN", "[object", "null"):
                self.assertNotIn(bad, text)
        self.assertEqual(rows_of([None, 5, "x", [], {"kind": "power.suspend"}, {"id": ""}, {"id": 5}, {"id": chr(7)}]), [])
        self.assertEqual(rows_of("nope"), [])
        self.assertEqual(rows_of(None), [])

    def test_the_row_loop_is_bounded_by_the_row_limit_and_by_the_list(self):
        self.assertEqual(len(rows_of([{"id": f"op-{index}"} for index in range(1000)])), ROW_LIMIT)
        self.assertEqual(rows_of([None] * 100_000 + [{"id": "late"}]), [])

    def test_only_a_clean_helper_answer_becomes_a_journal(self):
        data = view_of(fixture_body("list-mixed"))
        out = call_all([
            ("journalFromResult", {"ok": True, "data": data}),
            ("journalFromResult", {"ok": True, "data": unavailable("not_found")}),
            ("journalFromResult", {"ok": True, "data": {"available": True}}),
            ("journalFromResult", {"ok": True, "data": {"available": True, "operations": "x"}}),
            ("journalFromResult", {"ok": True, "data": {"available": "true", "operations": []}}),
            ("journalFromResult", {"ok": True, "data": [data]}),
            ("journalFromResult", {"ok": False, "error": "host rejected the request", "status": 404}),
            ("journalFromResult", {"ok": False, "data": data}),
            ("journalFromResult", {"ok": "yes", "data": data}),
            ("journalFromResult", {"ok": True, "data": "x"}),
            ("journalFromResult", {"ok": True}),
            ("journalFromResult", {}),
            ("journalFromResult", None),
            ("journalFromResult", UNDEFINED),
            ("journalFromResult", "oops"),
        ])
        self.assertEqual(out[0], data)
        self.assertEqual(out[1:], [None] * 14)


# ---------------------------------------------------------------- panel wiring (static)


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


def functions_calling(panel: str, needle: str, exclude: str) -> list[str]:
    return sorted(
        name for name in re.findall(r"\n  function (\w+)\(", panel)
        if name != exclude and needle in function_body(panel, name)
    )


class JournalWiringTests(unittest.TestCase):
    """Static checks: one read on entry and one per Refresh, never a timer, never while closed."""

    def test_the_fixed_action_is_requested_in_exactly_one_place_on_the_poll_lane(self):
        panel = panel_source()
        self.assertEqual(panel.count('enqueueHelper("journal"'), 1)
        body = function_body(panel, "requestJournal")
        self.assertIn('enqueueHelper("journal", {timeout: 2.5}', body)
        # Poll lane: it never sets the busy flag that disables the controls, and it is dropped with the panel.
        self.assertTrue(body.rstrip().endswith("}, true)"), body[-80:])
        self.assertNotIn("remote(", body)
        self.assertIn('if (!root.opened || !root.paired || root.view !== "settings") return', body)

    def test_the_read_is_spaced_before_it_is_queued(self):
        body = function_body(panel_source(), "requestJournal")
        gate = body.index("if (!root.journalMayRead(root.journalStartedMs, now)) return")
        stamp = body.index("root.journalStartedMs = now")
        queued = body.index("enqueueHelper(")
        self.assertLess(body.index("var now = Date.now()"), gate)
        self.assertLess(gate, stamp)
        self.assertLess(stamp, queued)
        self.assertIn("root.journalMayRead(root.journalStartedMs, root.nowMs)", function_body(panel_source(), "rowEnabled"))

    def test_the_read_is_triggered_by_entering_settings_and_by_the_refresh_row_only(self):
        panel = panel_source()
        self.assertEqual(functions_calling(panel, "requestJournal()", "requestJournal"), ["activateCursor", "setView"])
        self.assertEqual(panel.count("onClicked: root.requestJournal()"), 1)
        # Every textual use: the two functions, the one markup handler, and the definition.
        self.assertEqual(len(re.findall(r"requestJournal", panel)), 4)
        view = function_body(panel, "setView")
        self.assertEqual(view.count("requestJournal()"), 1)
        settings = view.index('} else if (next === "settings" && root.paired) {')
        self.assertGreater(view.index("root.requestJournal()"), settings)
        self.assertGreater(view.index("root.requestJournal()"), view.index("root.refreshConnectionDetails()"))
        self.assertLess(view.index('if (next === "display")'), settings)
        self.assertIn('else if (key === "journal") root.requestJournal()', function_body(panel, "activateCursor"))

    def test_no_timer_poll_or_open_handler_reads_the_journal(self):
        panel = panel_source()
        for timer in blocks(panel, "Timer"):
            self.assertNotIn("ournal", timer)
        for name in ("refresh", "refreshDetails", "refreshConnectionDetails", "onOpened", "onClosed", "tickCountdown", "tickPairing", "localInspect", "refreshAfterAction", "viewByDelta", "applyOutputs", "requestLinkFacts", "pollPairing", "pumpHelper", "settleHelper"):
            body = function_body(panel, name)
            self.assertNotIn('"journal"', body, name)
            self.assertNotIn("requestJournal", body, name)
        self.assertNotRegex(panel, r"onTriggered:[^\n]*[Jj]ournal")
        self.assertNotRegex(panel, r"Qt\.callLater\([^)]*[Jj]ournal")
        self.assertNotRegex(panel, r"(?:interval|running|repeat)[^\n]*[Jj]ournal")

    def test_the_panel_forgets_the_records_when_it_closes_opens_or_the_host_changes(self):
        panel = panel_source()
        self.assertIn("root.resetJournal()", function_body(panel, "onClosed"))
        self.assertIn("root.resetJournal()", function_body(panel, "onOpened"))
        reset = function_body(panel, "resetJournal")
        for line in ("root.journalEpoch++", 'root.journalState = "idle"', "root.journalData = null", "root.journalStartedMs = 0"):
            self.assertIn(line, reset)
        sync = function_body(panel, "syncJournalIdentity")
        for field in ("host_id", "endpoint"):
            self.assertIn(field, sync)
        self.assertIn("root.resetJournal()", sync)
        self.assertEqual(function_body(panel, "resetPairedState").count("root.syncJournalIdentity()"), 1)
        self.assertEqual(function_body(panel, "localInspect").count("root.syncJournalIdentity()"), 2)
        self.assertEqual(panel.count("root.localData = "), 3)
        # The records are never written anywhere that outlives the panel.
        for forbidden in ("localStorage", "FileView", "writeFile", "Qt.labs.settings", "Settings {"):
            self.assertNotIn(forbidden, panel)

    def test_a_failed_read_touches_neither_status_nor_the_action_line(self):
        panel = panel_source()
        for name in ("requestJournal", "resetJournal", "syncJournalIdentity"):
            body = function_body(panel, name)
            for forbidden in ("actionMessage", "connectionState", "connectionDetail", "friendlyError", "statusData", "lastObservation", "pollIntervalMs", "root.refresh(", "describeError", "operationData", "localData ="):
                self.assertNotIn(forbidden, body, f"{name}: {forbidden}")
        callback = function_body(panel, "requestJournal").split("enqueueHelper(", 1)[1]
        self.assertEqual(sorted(set(re.findall(r"root\.(\w+) = ", callback))), ["journalData", "journalState"])
        # The answer is dropped when the panel closed or another host was paired meanwhile.
        self.assertIn("if (epoch !== root.journalEpoch) return", callback)
        self.assertIn("var epoch = root.journalEpoch", function_body(panel, "requestJournal"))

    def test_the_refresh_row_exists_only_when_paired_and_is_gated_like_the_read(self):
        panel = panel_source()
        keys = function_body(panel, "settingsRowKeys")
        self.assertIn('if (root.paired) keys.push("journal")', keys)
        self.assertLess(keys.index('keys.push("pair")'), keys.index('keys.push("journal")'))
        self.assertLess(keys.index('keys.push("journal")'), keys.index('keys.push("revoke")'))
        self.assertIn('if (key === "journal") return root.paired && root.journalMayRead(root.journalStartedMs, root.nowMs)', function_body(panel, "rowEnabled"))

    def test_the_group_sits_in_connection_details_after_link_facts(self):
        panel = panel_source()
        link = panel.index('PanelSectionHeader { text: "Link facts"')
        host = panel.index('PanelSectionHeader { text: "Host journal"')
        preferences = panel.index('PanelSectionHeader { text: "Display preferences"')
        self.assertLess(panel.index('PanelSectionHeader { text: "Connection details"'), link)
        self.assertLess(link, host)
        self.assertLess(host, preferences)
        group = panel[panel.index("id: journalGroup"):preferences]
        group = group[:group.index("PanelSeparator")]
        # The markup shows what the last read returned and asks the host for nothing itself.
        for forbidden in ("enqueueHelper", "remote(", "Timer", "Qt.callLater", "refresh(", "journalData.", "helperQueue"):
            self.assertNotIn(forbidden, group)
        self.assertIn("root.journalModel(root.journalState, root.journalData)", group)
        self.assertIn("visible: root.paired", group)
        self.assertEqual(group.count("root.requestJournal()"), 1)
        self.assertIn('rowKey: "journal"', group)
        self.assertIn('"Refresh journal"', group)
        self.assertEqual(group.count("Text.WrapAnywhere"), 2)
        # Plain text only: the row and hint components are PlainText, and this group adds no rich text.
        self.assertNotIn("textFormat", group)
        self.assertNotIn("Text.RichText", group)
        self.assertNotIn("Text.MarkdownText", group)
        self.assertNotIn("StyledText", group)
        for component in ("BodyText", "HintText"):
            self.assertIn("textFormat: Text.PlainText", panel[panel.index(f"component {component}: Text"):][:200])

    def test_the_group_says_what_it_shows_and_the_standing_note_comes_from_the_model(self):
        panel = panel_source()
        group = panel[panel.index("id: journalGroup"):]
        group = group[:group.index("PanelSeparator")]
        self.assertIn("never in the background", group)
        for field in ("journalView.message", "journalView.rows", "journalView.more", "journalView.note", "modelData.title", "modelData.lines"):
            self.assertIn(field, group)

    def test_the_helper_action_is_not_a_route_the_panel_can_point_elsewhere(self):
        panel = panel_source()
        body = function_body(panel, "requestJournal")
        for forbidden in ("/v1/", "https:", "endpoint", "token"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
