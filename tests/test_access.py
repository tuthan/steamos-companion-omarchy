"""What the host owner allows this pairing: the optional `credential.scopes` of a status reply.

Three layers are covered, all against the pinned Decky exchanges under
`protocol/fixtures/contract/` plus inline older-host and hostile bodies. The decoder keeps
only grantable scopes, once each, in the host's order, and tells "the host did not say"
(None) from "the host says nothing is allowed" ([]). The transport rebuilds the block from
that decoder so it can never fail a status read. A successful status read replaces the
saved scopes with the reported list, in either direction, and leaves them alone when the
host did not report.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from client import cli, client_core

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "protocol" / "fixtures" / "contract"
PIN = "sha256:" + "a" * 64
FULL = ["status.read", "power.control", "display.control"]
VIEW_ONLY = ["status.read"]
SCHEMA = json.loads((ROOT / "protocol" / "schema.json").read_text())


def fixture(group: str, name: str) -> dict:
    return json.loads((CONTRACT / group / f"{name}.json").read_text())


def status_body(name: str = "sunshine-running", **changes: Any) -> dict:
    body = copy.deepcopy(fixture("status", name)["response"]["body"])
    for key, value in changes.items():
        if value is Ellipsis:
            body.pop(key, None)
        else:
            body[key] = value
    return body


class FakeResponse:
    def __init__(self, raw: bytes, status: int):
        self.raw = raw
        self.status = status

    def read(self, _limit: int) -> bytes:
        return self.raw

    def getheader(self, _name: str, default: Any = None) -> Any:
        return default


class FakeConnection:
    """Answers each path from a table (a list is consumed in order, its last answer repeating) and records requests."""

    def __init__(self, routes: dict[str, Any]):
        self.sock = mock.Mock()
        self.sock.getpeercert.return_value = b"certificate"
        self.routes = {path: list(answer) if isinstance(answer, list) else [answer] for path, answer in routes.items()}
        self.requests: list[tuple[str, str, Any]] = []
        self._pending = ("", "")

    def connect(self) -> None:
        return None

    def putrequest(self, method: str, path: str, **_kwargs: Any) -> None:
        self._pending = (method, path)

    def putheader(self, *_args: Any) -> None:
        return None

    def endheaders(self, body: Any = None, **_kwargs: Any) -> None:
        self.requests.append((*self._pending, None if body is None else json.loads(body)))

    def getresponse(self) -> FakeResponse:
        answers = self.routes[self._pending[1]]
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(body, BaseException):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode(), status)

    def close(self) -> None:
        return None

    def paths(self) -> list[tuple[str, str]]:
        return [request[:2] for request in self.requests]


@contextlib.contextmanager
def serving(routes: dict[str, Any]):
    """Patch the socket only: the real transport, pin check and core run."""
    connection = FakeConnection(routes)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(client_core.http.client, "HTTPSConnection", return_value=connection))
        stack.enter_context(mock.patch.object(client_core, "fingerprint_der", return_value=PIN))
        stack.enter_context(mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"))
        yield connection


STATE = {
    "protocol_version": 1,
    "endpoint": "https://steam-host.example:18443",
    "host_id": "host-fixture0001xxxxxxxxxxxxx",
    "certificate_fingerprint": PIN,
    "client_id": "client-test",
    "client_name": "Omarchy client",
    "token": "token-value",
    "scopes": FULL,
    "wake_mac": "aabbccddeeff",
    "wake_interface": "wlan0",
    "wake_host_interface": "enp5s0",
    "pending_operation": {"id": "op-1", "request_id": "req-1", "state": "accepted", "kind": "power"},
}


def paired_core(directory: str, scopes: list[str] | None = None) -> client_core.ClientCore:
    core = client_core.ClientCore(directory)
    state = copy.deepcopy(STATE)
    state["scopes"] = list(FULL if scopes is None else scopes)
    core.store.save(state)
    return core


def run_status(core: client_core.ClientCore, body: Any, status: int = 200) -> dict:
    with serving({"/v1/status": (status, body)}):
        return core.request("status", {})


class DecoderTests(unittest.TestCase):
    def test_the_vendored_fixtures_decode_to_what_they_say(self):
        names = sorted(path.stem for path in (CONTRACT / "status").glob("*.json"))
        expected = {"view-only-credential": VIEW_ONLY, "scopes-changed-by-owner": ["status.read", "display.control"]}
        for name in names:
            body = fixture("status", name)["response"]["body"]
            with self.subTest(name):
                if name in ("rate-limited", "unauthorized-invalid", "unauthorized-missing"):
                    self.assertIsNone(client_core.credential_scopes(body))
                else:
                    self.assertEqual(expected.get(name, FULL), client_core.credential_scopes(body))
        self.assertEqual(VIEW_ONLY, client_core.credential_scopes(status_body("view-only-credential")))

    def test_the_decoder_allows_exactly_the_schemas_three_scopes(self):
        listed = SCHEMA["$defs"]["credential"]["properties"]["scopes"]["items"]["enum"]
        self.assertEqual(listed, FULL)
        self.assertEqual(frozenset(listed), client_core.GRANTABLE_SCOPES)
        self.assertNotIn("sunshine.control", client_core.GRANTABLE_SCOPES)
        self.assertEqual(listed, client_core.credential_scopes({"credential": {"scopes": listed}}))

    def test_a_host_that_did_not_say_is_none_never_no_access(self):
        silent: list[Any] = [
            {},
            {"protocol_version": 1},
            {"credential": None},
            {"credential": "status.read"},
            {"credential": 5},
            {"credential": True},
            {"credential": ["status.read"]},
            {"credential": {}},
            {"credential": {"role": "view_only"}},
            {"credential": {"scopes": None}},
            {"credential": {"scopes": "status.read"}},
            {"credential": {"scopes": 3}},
            {"credential": {"scopes": {"status.read": True}}},
        ]
        for body in silent:
            with self.subTest(body=body):
                self.assertIsNone(client_core.credential_scopes(body))
        for body in (None, [], "status", 7, ["credential"]):
            with self.subTest(body=body):
                self.assertIsNone(client_core.credential_scopes(body))

    def test_an_empty_list_is_a_real_answer(self):
        self.assertEqual([], client_core.credential_scopes({"credential": {"scopes": []}}))
        self.assertIsNotNone(client_core.credential_scopes({"credential": {"scopes": []}}))

    def test_values_the_client_does_not_know_are_dropped(self):
        cases = [
            (["status.read", "sunshine.control", "power.control"], ["status.read", "power.control"]),
            (["admin", "root", "*", ""], []),
            (["power.control", "future.scope", "display.control"], ["power.control", "display.control"]),
            (["STATUS.READ", "Status.Read", "status_read", "status.read.extra"], []),
        ]
        for scopes, kept in cases:
            with self.subTest(scopes=scopes):
                self.assertEqual(kept, client_core.credential_scopes({"credential": {"scopes": scopes}}))

    def test_each_scope_is_kept_once_in_the_hosts_order(self):
        cases = [
            (["power.control", "power.control", "status.read"], ["power.control", "status.read"]),
            (["display.control", "status.read", "display.control", "power.control", "status.read"], ["display.control", "status.read", "power.control"]),
            (["display.control", "power.control", "status.read"], ["display.control", "power.control", "status.read"]),
        ]
        for scopes, kept in cases:
            with self.subTest(scopes=scopes):
                self.assertEqual(kept, client_core.credential_scopes({"credential": {"scopes": scopes}}))

    def test_four_or_more_entries_never_make_more_than_three(self):
        many = [*FULL, "status.read", "power.control", "x", "display.control"]
        self.assertEqual(FULL, client_core.credential_scopes({"credential": {"scopes": many}}))
        self.assertEqual(FULL, client_core.credential_scopes({"credential": {"scopes": FULL * 100_000}}))
        self.assertEqual(VIEW_ONLY, client_core.credential_scopes({"credential": {"scopes": ["status.read"] * 1_000_000}}))

    def test_entries_that_are_not_strings_are_dropped_not_fatal(self):
        scopes = [1, None, True, 1.5, {}, [], ["status.read"], {"status.read": 1}, "status.read"]
        self.assertEqual(VIEW_ONLY, client_core.credential_scopes({"credential": {"scopes": scopes}}))

    def test_non_ascii_and_control_characters_never_match_a_scope(self):
        near = [
            "status.read\n", "status.read\r\n", "status.read\x00", "\x00status.read", " status.read", "status.read ", "\tstatus.read",
            "status.read\x1b[31m", "status.read\x7f", "status.read​", "status​.read", "﻿status.read", "status.read ",
            "power.control‮", "power\u0000.control", "status.read\x00power.control", "status.read,power.control", "status.read\npower.control",
            "ѕtatus.read", "status.rеad", "power.cоntrol", "ｐower.control", "display.control\u0085", "display.control\U0001f512",
        ]
        self.assertEqual([], client_core.credential_scopes({"credential": {"scopes": near}}))
        self.assertEqual(VIEW_ONLY, client_core.credential_scopes({"credential": {"scopes": [*near, "status.read"]}}))
        for scope in near:
            self.assertNotIn(scope, client_core.GRANTABLE_SCOPES)

    def test_the_decoder_does_not_change_its_input(self):
        body = {"credential": {"scopes": ["power.control", "power.control", "x"]}}
        before = copy.deepcopy(body)
        client_core.credential_scopes(body)
        self.assertEqual(before, body)


def read(path: str, body: Any, status: int = 200) -> dict:
    with serving({path: (status, body)}):
        return client_core.PinnedTransport("https://steam-host.example:18443", PIN, "token").request("GET", path)


class TransportTests(unittest.TestCase):
    def test_the_real_exchanges_reach_the_client_with_their_block_intact(self):
        for name in ("sunshine-running", "view-only-credential", "scopes-changed-by-owner"):
            with self.subTest(name):
                body = status_body(name)
                self.assertEqual(body, read("/v1/status", body))

    def test_the_block_is_rebuilt_from_the_allowlist(self):
        body = status_body(credential={"scopes": ["status.read", "sunshine.control", "status.read", "evil\u0000"], "role": "owner", "extra": {"deep": [1]}})
        result = read("/v1/status", body)
        self.assertEqual({"scopes": VIEW_ONLY}, result["credential"])
        self.assertEqual({key: value for key, value in body.items() if key != "credential"}, {key: value for key, value in result.items() if key != "credential"})
        self.assertEqual(list(body), list(result), "the block keeps its place in the reply")

    def test_a_block_that_says_nothing_is_dropped_and_never_fails_status(self):
        for label, block in {"null": None, "string": "all", "number": 5, "list": ["status.read"], "empty": {}, "no list": {"scopes": "status.read"}, "dict": {"scopes": {"a": 1}}}.items():
            with self.subTest(label):
                body = status_body(credential=block)
                result = read("/v1/status", body)
                self.assertNotIn("credential", result)
                self.assertEqual({key: value for key, value in body.items() if key != "credential"}, result)

    def test_an_oversized_block_cannot_fail_a_status_read(self):
        # Left alone, 1000 entries would trip the generic 256-item list budget and refuse the whole reply.
        for scopes in (["status.read"] * 1000, [{"a": [1] * 50}] * 1000, ["x" * 2000], [*FULL] * 5000):
            with self.subTest(size=len(scopes)):
                result = read("/v1/status", status_body(credential={"scopes": scopes}))
                self.assertEqual(client_core.credential_scopes({"credential": {"scopes": scopes}}), result.get("credential", {}).get("scopes"))
        deep: Any = []
        for _ in range(40):
            deep = [deep]
        result = read("/v1/status", status_body(credential={"scopes": deep}))
        self.assertEqual([], result["credential"]["scopes"])

    def test_an_empty_list_survives_the_transport(self):
        self.assertEqual({"scopes": []}, read("/v1/status", status_body(credential={"scopes": []}))["credential"])

    def test_the_budgets_still_apply_to_the_rest_of_the_reply(self):
        with self.assertRaises(client_core.ClientError):
            read("/v1/status", status_body(credential={"scopes": FULL}, reason="x" * 2000))

    def test_only_the_status_route_is_rebuilt(self):
        body = {"protocol_version": 1, "credential": {"scopes": ["status.read", "evil"], "role": "owner"}}
        self.assertEqual(body, read("/v1/display/outputs", body))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def saved(self, core: client_core.ClientCore) -> list[str]:
        return core.store.load()["scopes"]

    def test_a_downgrade_replaces_the_saved_scopes(self):
        core = paired_core(self.directory, FULL)
        run_status(core, status_body("view-only-credential"))
        self.assertEqual(VIEW_ONLY, self.saved(core))
        self.assertEqual(VIEW_ONLY, core.inspect()["scopes"])

    def test_an_upgrade_replaces_the_saved_scopes(self):
        core = paired_core(self.directory, VIEW_ONLY)
        run_status(core, status_body("sunshine-running"))
        self.assertEqual(FULL, self.saved(core))

    def test_the_owner_removing_one_scope_after_pairing_is_learned_from_the_fixture(self):
        core = paired_core(self.directory, FULL)
        run_status(core, status_body("scopes-changed-by-owner"))
        self.assertEqual(["status.read", "display.control"], self.saved(core))

    def test_a_host_that_did_not_report_leaves_the_saved_scopes_alone(self):
        for label, body in {
            "older host": status_body(credential=Ellipsis),
            "null": status_body(credential=None),
            "not an object": status_body(credential="status.read"),
            "no list": status_body(credential={"scopes": "status.read"}),
        }.items():
            with self.subTest(label):
                core = paired_core(self.directory, ["status.read", "display.control"])
                run_status(core, body)
                self.assertEqual(["status.read", "display.control"], self.saved(core))

    def test_an_empty_list_is_saved_as_no_access_and_the_state_still_loads(self):
        core = paired_core(self.directory, FULL)
        run_status(core, status_body(credential={"scopes": []}))
        self.assertEqual([], self.saved(core))
        self.assertEqual([], core.inspect()["scopes"])
        self.assertTrue(core.inspect()["paired"])

    def test_unknown_and_repeated_values_never_reach_the_saved_state(self):
        core = paired_core(self.directory, VIEW_ONLY)
        run_status(core, status_body(credential={"scopes": ["display.control", "sunshine.control", "display.control", "x\u0000", "status.read"]}))
        self.assertEqual(["display.control", "status.read"], self.saved(core))
        self.assertNotIn("sunshine", core.store.path.read_text())

    def test_nothing_else_in_the_saved_pairing_is_lost(self):
        core = paired_core(self.directory, FULL)
        before = core.store.load()
        run_status(core, status_body("view-only-credential"))
        after = core.store.load()
        self.assertEqual({**before, "scopes": VIEW_ONLY, "wake_mac": "001122334455"}, after)
        for key in ("token", "client_id", "pending_operation", "wake_interface", "client_name"):
            self.assertEqual(before[key], after[key], key)

    def test_the_wake_target_and_the_scopes_are_both_kept_by_one_status_read(self):
        core = paired_core(self.directory, FULL)
        run_status(core, status_body("view-only-credential"))
        state = core.store.load()
        self.assertEqual(("001122334455", VIEW_ONLY), (state["wake_mac"], state["scopes"]))

    def test_a_failed_status_read_changes_nothing(self):
        core = paired_core(self.directory, FULL)
        before = core.store.path.read_bytes()
        for status, body in ((401, {"error": "unauthorized", "message": "no"}), (503, {"error": "host_locked", "message": "locked"}), (200, b"garbage")):
            with self.subTest(status=status), self.assertRaises(client_core.ClientError):
                run_status(core, body, status)
        with serving({"/v1/status": (200, OSError("connection refused"))}), self.assertRaises(client_core.ClientError):
            core.request("status", {})
        self.assertEqual(before, core.store.path.read_bytes())

    def test_an_unchanged_report_does_not_rewrite_the_state(self):
        core = paired_core(self.directory, FULL)
        with mock.patch.object(core.store, "save", side_effect=AssertionError("must not write")):
            core._remember_scopes(status_body("sunshine-running"))
            core._remember_scopes(status_body(credential=Ellipsis))

    def test_the_endpoint_check_learns_the_scopes_too(self):
        core = paired_core(self.directory, FULL)
        body = status_body("view-only-credential")
        with serving({"/v1/status": (200, body)}):
            core.configure_endpoint("https://other-address.example:18443")
        state = core.store.load()
        self.assertEqual((VIEW_ONLY, "https://other-address.example:18443"), (state["scopes"], state["endpoint"]))

    def test_the_status_the_panel_gets_carries_only_the_decoded_block(self):
        core = paired_core(self.directory, FULL)
        reply = run_status(core, status_body(credential={"scopes": ["status.read", "evil"], "role": "owner"}))
        self.assertEqual({"scopes": VIEW_ONLY}, reply["credential"])

    def test_an_old_state_file_with_scopes_still_loads_and_nothing_else_about_validation_loosened(self):
        core = paired_core(self.directory, FULL)
        for bad in ("status.read", None, ["status.read", "admin"], ["status.read", 5], {"status.read": 1}):
            state = core.store.load()
            state["scopes"] = bad
            with self.subTest(bad=bad), self.assertRaises(client_core.ClientError):
                core.store.save(state)
        state = core.store.load()
        state["scopes"] = []
        core.store.save(state)


ACCEPTED = (202, {"protocol_version": 1, "operation": {"id": "op-test0001", "state": "accepted"}})
# Every action that changes the host or the pairing: (core action, arguments, the one route it posts to, the scope it needs).
POSTS = [
    ("power", {"action": "suspend"}, "/v1/power", "Power"),
    ("sunshine-restart", {}, "/v1/sunshine/restart", "Power"),
    ("preview", {"output_id": "1", "mode_id": "2", "generation": 4}, "/v1/display/preview", "Display"),
    ("confirm", {"preview_id": "preview-1", "visible": True}, "/v1/display/confirm", "Display"),
    ("restore", {"source": "verified"}, "/v1/display/restore", "Display"),
    ("save-current", {"output_id": "1", "generation": 4, "visible": True}, "/v1/display/save-current", "Display"),
    ("display-order-save", {"output_keys": ["drm:card0:HDMI-A-1"], "generation": 3}, "/v1/display/order", "Display"),
    ("display-order-restart", {"output_keys": ["drm:card0:HDMI-A-1"], "generation": 3}, "/v1/display/order", "Display"),
    ("display-order-reset", {}, "/v1/display/order/automatic", "Display"),
    ("revoke", {}, "/v1/pair/revoke-self", None),
]
READS = [
    ("outputs", {}, "/v1/display/outputs"),
    ("display-order", {}, "/v1/display/order"),
    ("link-facts", {}, "/v1/drm"),
    ("journal", {}, "/v1/operations"),
    ("operations", {}, "/v1/operations"),
    ("operation", {"operation_id": "op-test0001"}, "/v1/operations/op-test0001"),
]
READ_BODY = (200, {"protocol_version": 1, "outputs": [], "operations": [], "operation": {"id": "op-test0001", "state": "succeeded"}})


def routes(status: Any = None, **extra: Any) -> dict[str, Any]:
    table: dict[str, Any] = {"/v1/status": (200, status_body("sunshine-running") if status is None else status)}
    for _name, _args, path, _scope in POSTS:
        table[path] = ACCEPTED
    for _name, _args, path in READS:
        table[path] = READ_BODY
    table.update(extra)
    return table


def not_allowed(label: str) -> str:
    return f"The host owner has not allowed {label} for this pairing."


class GateTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def posts(self, connection: FakeConnection) -> list[tuple[str, str]]:
        return [path for path in connection.paths() if path[0] == "POST"]

    def attempt(self, core: client_core.ClientCore, action: str, arguments: dict, table: dict[str, Any]):
        with serving(table) as connection:
            try:
                return core.request(action, copy.deepcopy(arguments)), connection
            except client_core.ClientError as exc:
                return exc, connection

    def test_a_pairing_with_every_scope_reaches_the_host_unasked(self):
        for action, arguments, path, _label in POSTS:
            with self.subTest(action):
                core = paired_core(self.directory, FULL)
                result, connection = self.attempt(core, action, arguments, routes())
                self.assertNotIsInstance(result, Exception)
                self.assertEqual([("POST", path)], connection.paths(), "no status read, one request to its fixed route")

    def test_a_view_only_pairing_is_refused_before_anything_is_sent(self):
        for action, arguments, path, label in POSTS:
            if label is None:
                continue
            with self.subTest(action):
                core = paired_core(self.directory, VIEW_ONLY)
                result, connection = self.attempt(core, action, arguments, routes(status_body("view-only-credential")))
                self.assertIsInstance(result, client_core.ClientError)
                self.assertEqual(not_allowed(label), str(result))
                self.assertFalse(result.unknown)
                self.assertIsNone(result.status)
                self.assertEqual([], self.posts(connection))
                self.assertEqual(VIEW_ONLY, core.store.load()["scopes"])

    def test_power_and_display_are_separate_scopes(self):
        cases = {
            ("status.read", "power.control"): {"Display"},
            ("status.read", "display.control"): {"Power"},
            ("status.read",): {"Power", "Display"},
            (): {"Power", "Display"},
        }
        for held, refused in cases.items():
            for action, arguments, path, label in POSTS:
                if label is None:
                    continue
                with self.subTest(held=held, action=action):
                    core = paired_core(self.directory, list(held))
                    result, connection = self.attempt(core, action, arguments, routes(status_body(credential={"scopes": list(held)})))
                    if label in refused:
                        self.assertEqual(not_allowed(label), str(result))
                        self.assertEqual([], self.posts(connection))
                    else:
                        self.assertNotIsInstance(result, Exception)
                        self.assertEqual([("POST", path)], self.posts(connection))

    def test_sunshine_recovery_needs_power_not_display(self):
        core = paired_core(self.directory, ["status.read", "display.control"])
        result, connection = self.attempt(core, "sunshine-restart", {}, routes(status_body(credential={"scopes": ["status.read", "display.control"]})))
        self.assertEqual(not_allowed("Power"), str(result))
        self.assertEqual([], self.posts(connection))

    def test_revoking_ones_own_pairing_and_every_read_need_no_scope(self):
        core = paired_core(self.directory, VIEW_ONLY)
        for action, arguments, path, label in POSTS:
            if label is None:
                result, connection = self.attempt(core, action, arguments, routes(status_body("view-only-credential")))
                self.assertNotIsInstance(result, Exception)
                self.assertEqual([("POST", path)], connection.paths())
        for action, arguments, path in READS:
            with self.subTest(action):
                result, connection = self.attempt(core, action, arguments, routes(status_body("view-only-credential")))
                self.assertNotIsInstance(result, Exception)
                self.assertEqual([("GET", path)], connection.paths())
        result, connection = self.attempt(core, "status", {}, routes(status_body("view-only-credential")))
        self.assertEqual([("GET", "/v1/status")], connection.paths())

    def test_a_pairing_with_no_scope_at_all_can_still_read_status(self):
        core = paired_core(self.directory, [])
        result, connection = self.attempt(core, "status", {}, routes(status_body(credential={"scopes": []})))
        self.assertNotIsInstance(result, Exception)
        self.assertEqual([], core.store.load()["scopes"])

    def test_a_scope_the_owner_has_just_granted_is_learned_before_refusing(self):
        core = paired_core(self.directory, VIEW_ONLY)
        result, connection = self.attempt(core, "power", {"action": "suspend"}, routes(status_body("sunshine-running")))
        self.assertNotIsInstance(result, Exception)
        self.assertEqual([("GET", "/v1/status"), ("POST", "/v1/power")], connection.paths())
        self.assertEqual(FULL, core.store.load()["scopes"])
        self.assertEqual("op-test0001", core.store.load()["pending_operation"]["id"], "the operation handle is saved on top of the new scopes")

    def test_the_request_is_judged_by_what_the_host_says_now_not_by_what_was_saved_at_pairing(self):
        core = paired_core(self.directory, FULL)
        run_status(core, status_body("view-only-credential"))
        result, connection = self.attempt(core, "restore", {"source": "verified"}, routes(status_body("view-only-credential")))
        self.assertEqual(not_allowed("Display"), str(result))
        self.assertEqual([("GET", "/v1/status")], connection.paths(), "the host is asked once, and still says no")

    def test_a_host_that_does_not_report_scopes_is_judged_by_what_was_saved(self):
        core = paired_core(self.directory, VIEW_ONLY)
        result, connection = self.attempt(core, "power", {"action": "restart"}, routes(status_body(credential=Ellipsis)))
        self.assertEqual(not_allowed("Power"), str(result))
        self.assertEqual([], self.posts(connection))
        self.assertEqual(VIEW_ONLY, core.store.load()["scopes"])

    def test_an_unreachable_host_is_a_connection_error_never_a_claim_about_access(self):
        for answer in (OSError("connection refused"), TimeoutError("timed out")):
            with self.subTest(answer=answer):
                core = paired_core(self.directory, VIEW_ONLY)
                result, connection = self.attempt(core, "power", {"action": "suspend"}, routes(status=None, **{"/v1/status": (200, answer)}))
                self.assertIsInstance(result, client_core.ClientError)
                self.assertIn("host connection failed", str(result))
                self.assertNotRegex(str(result), r"(?i)allowed|revoked|removed|forgot")
                self.assertEqual([], self.posts(connection))
                self.assertEqual(VIEW_ONLY, core.store.load()["scopes"])
                self.assertTrue(core.inspect()["paired"])

    def test_a_refusal_removes_nothing_and_marks_nothing_invalid(self):
        core = paired_core(self.directory, VIEW_ONLY)
        before = core.store.path.read_bytes()
        result, _ = self.attempt(core, "display-order-reset", {}, routes(status_body("view-only-credential", wake_target=Ellipsis)))
        self.assertEqual(not_allowed("Display"), str(result))
        self.assertEqual(before, core.store.path.read_bytes())
        self.assertEqual(sorted(path.name for path in Path(self.directory).iterdir()), ["client.json"])

    def test_the_helper_actions_that_name_a_power_action_are_gated_too(self):
        for name in ("suspend", "restart", "shutdown", "reboot"):
            with self.subTest(name):
                core = paired_core(self.directory, VIEW_ONLY)
                with serving(routes(status_body("view-only-credential"))) as connection, self.assertRaises(client_core.ClientError) as raised:
                    core.run({"action": name})
                self.assertEqual(not_allowed("Power"), str(raised.exception))
                self.assertEqual([], self.posts(connection))

    def test_the_gate_lists_every_scope_the_routes_need(self):
        self.assertEqual({"power", "sunshine-restart", "preview", "confirm", "restore", "save-current", "display-order-save", "display-order-restart", "display-order-reset"}, set(client_core.ACTION_SCOPES))
        self.assertEqual({action: {"Power": "power.control", "Display": "display.control"}[label] for action, _args, _path, label in POSTS if label}, client_core.ACTION_SCOPES)
        self.assertTrue(set(client_core.ACTION_SCOPES.values()) <= client_core.GRANTABLE_SCOPES)
        self.assertEqual({"status.read": "Status", "power.control": "Power", "display.control": "Display"}, client_core.SCOPE_LABELS)


class CliGateTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def run_cli(self, core: client_core.ClientCore, table: dict[str, Any], *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with serving(table) as connection, mock.patch.object(cli, "ClientCore", lambda: core), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue(), connection

    def test_each_command_that_changes_the_host_refuses_a_view_only_pairing_in_plain_words(self):
        view_only = routes(status_body("view-only-credential"))
        commands = [
            (("power", "suspend", "--yes", "--acknowledged"), "Power"),
            (("power", "restart"), "Power"),
            (("power", "shutdown"), "Power"),
            (("sunshine", "recover"), "Power"),
            (("display", "preview", "--output-id", "1", "--mode-id", "2", "--generation", "4"), "Display"),
            (("display", "keep", "--preview-id", "preview-1", "--yes", "--acknowledged"), "Display"),
            (("display", "revert"), "Display"),
            (("display", "restore"), "Display"),
            (("display", "save-current", "--output-id", "1", "--generation", "4", "--yes", "--acknowledged"), "Display"),
            (("display", "order-save", "--output-keys", "drm:card0:HDMI-A-1", "--generation", "3"), "Display"),
            (("display", "order-save", "--output-keys", "drm:card0:HDMI-A-1", "--generation", "3", "--restart", "--yes", "--acknowledged"), "Display"),
            (("display", "order-reset"), "Display"),
        ]
        for argv, label in commands:
            for flag in ("--json", "--plain"):
                with self.subTest(argv=argv, flag=flag):
                    core = paired_core(self.directory, VIEW_ONLY)
                    code, out, err, connection = self.run_cli(core, view_only, flag, *argv)
                    self.assertEqual(1, code)
                    if flag == "--json":
                        self.assertEqual({"ok": False, "error": not_allowed(label), "unknown": False}, json.loads(out))
                    else:
                        self.assertIn(not_allowed(label), out)
                    self.assertEqual([], [path for path in connection.paths() if path[0] == "POST"])

    def test_the_owner_is_not_asked_to_type_yes_for_something_that_will_be_refused(self):
        view_only = routes(status_body("view-only-credential"))
        for argv in (
            ("power", "suspend", "--yes", "--acknowledged"),
            ("display", "save-current", "--output-id", "1", "--generation", "4", "--yes", "--acknowledged"),
            ("display", "keep", "--preview-id", "preview-1", "--yes", "--acknowledged"),
            ("display", "order-save", "--output-keys", "drm:card0:HDMI-A-1", "--generation", "3", "--restart", "--yes", "--acknowledged"),
        ):
            with self.subTest(argv=argv):
                code, _out, err, _connection = self.run_cli(paired_core(self.directory, VIEW_ONLY), view_only, *argv)
                self.assertEqual(1, code)
                self.assertEqual("", err, "no confirmation prompt is written for a refused command")

    def test_status_and_the_journal_stay_available_to_a_view_only_pairing(self):
        view_only = routes(status_body("view-only-credential"))
        for argv in (("status",), ("journal", "list"), ("journal", "view"), ("journal", "get", "op-test0001"), ("outputs",), ("display", "order"), ("sunshine", "status"), ("inspect",)):
            with self.subTest(argv=argv):
                code, out, _err, _connection = self.run_cli(paired_core(self.directory, VIEW_ONLY), view_only, "--json", *argv)
                self.assertEqual(0, code, out)

    def test_a_full_pairing_still_confirms_and_sends(self):
        code, out, err, connection = self.run_cli(paired_core(self.directory, FULL), routes(), "--json", "power", "suspend", "--yes", "--acknowledged")
        self.assertEqual((2, [("POST", "/v1/power")]), (code, connection.paths()), "an accepted operation exits 2, as before")
        self.assertIn("Suspend the exact paired SteamOS host?", err)


if __name__ == "__main__":
    unittest.main()
