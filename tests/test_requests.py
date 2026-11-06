"""How a request that changes the host is named, confirmed and answered.

Three things are covered, all against the real transport with only the socket replaced.
Request IDs: `steamos-companion-ctl` takes `--request-id` on every command that changes the
host, so a scripted retry sends the same ID and the host recognises it (doc 22). The
save-current paths: the six exchanges of doc 21 plus the rule that `visible: true` is never
sent unless the owner asserted it. The exit codes a script reads: 0 ok, 1 failed, 2 unknown.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from client import cli, client_core

PIN = "sha256:" + "a" * 64
FULL = ["status.read", "power.control", "display.control"]
DECKY_SAVE_CURRENT = Path("/home/hvo/Projects/steamos-companion-decky/protocol/fixtures/contract/display-save-current")
DECKY_FRONTEND = Path("/home/hvo/Projects/steamos-companion-decky/host/frontend/index.js")


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

    def putheader(self, key: str, value: str) -> None:
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

    def posts(self) -> list[tuple[str, dict]]:
        return [(path, body) for method, path, body in self.requests if method == "POST"]


@contextlib.contextmanager
def serving(routes: dict[str, Any]):
    """Patch the socket only: the real transport, pin check and core run."""
    connection = FakeConnection(routes)
    with mock.patch.object(client_core.http.client, "HTTPSConnection", return_value=connection), mock.patch.object(client_core, "fingerprint_der", return_value=PIN):
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
}


def paired_core(directory: str) -> client_core.ClientCore:
    core = client_core.ClientCore(directory)
    core.store.save(copy.deepcopy(STATE))
    return core


def operation(state: str, **extra: Any) -> tuple[int, dict]:
    return 202, {"protocol_version": 1, "operation": {"id": "op-test0001", "state": state, **extra}}


# Every command that changes the host: (argv, the one route it posts to, what its body carries besides request_id).
MUTATIONS = [
    (("power", "suspend", "--yes", "--acknowledged"), "/v1/power", {"action": "suspend"}),
    (("power", "restart", "--yes", "--acknowledged"), "/v1/power", {"action": "restart"}),
    (("power", "shutdown", "--yes", "--acknowledged"), "/v1/power", {"action": "shutdown"}),
    (("sunshine", "recover"), "/v1/sunshine/restart", {}),
    (("display", "preview", "--output-id", "1", "--mode-id", "2", "--generation", "4"), "/v1/display/preview", {"output_id": "1", "mode_id": "2", "generation": 4}),
    (("display", "keep", "--preview-id", "preview-1", "--yes", "--acknowledged"), "/v1/display/confirm", {"preview_id": "preview-1", "visible": True}),
    (("display", "revert"), "/v1/display/restore", {"source": "preview"}),
    (("display", "restore"), "/v1/display/restore", {"source": "verified"}),
    (("display", "restore", "--source", "preview"), "/v1/display/restore", {"source": "preview"}),
    (("display", "save-current", "--output-id", "1", "--generation", "4", "--yes", "--acknowledged"), "/v1/display/save-current", {"output_id": "1", "generation": 4, "visible": True}),
    (("display", "order-save", "--output-keys", "drm:card0:HDMI-A-1", "--generation", "3"), "/v1/display/order", {"output_keys": ["drm:card0:HDMI-A-1"], "generation": 3, "restart": False}),
    (("display", "order-save", "--output-keys", "drm:card0:HDMI-A-1", "--generation", "3", "--restart", "--yes", "--acknowledged"), "/v1/display/order", {"output_keys": ["drm:card0:HDMI-A-1"], "generation": 3, "restart": True}),
    (("display", "order-reset"), "/v1/display/order/automatic", {}),
]
# Commands that read, or that act on this machine, or that pair: no operation to name.
UNNAMED = [("status",), ("outputs",), ("inspect",), ("diagnostics",), ("display", "order"), ("display", "link-facts"), ("journal", "list"), ("journal", "view"), ("journal", "get", "op-test0001"), ("wake",), ("sunshine", "status"), ("pair", "poll"), ("pair", "cancel"), ("pair", "discover")]


class CliCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def run_cli(self, table: dict[str, Any], *argv: str, core: client_core.ClientCore | None = None):
        core = core or paired_core(self.directory)
        out, err = io.StringIO(), io.StringIO()
        with serving(table) as connection, mock.patch.object(cli, "ClientCore", lambda: core), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue(), connection


class RequestIdTests(CliCase):
    def table(self, path: str, answer: Any = None) -> dict[str, Any]:
        return {path: operation("accepted") if answer is None else answer}

    def test_every_command_that_changes_the_host_sends_the_id_it_was_given(self):
        for argv, path, body in MUTATIONS:
            with self.subTest(argv=argv):
                code, out, _err, connection = self.run_cli(self.table(path), "--json", *argv, "--request-id", "req-script-0001")
                self.assertEqual(2, code, out)
                self.assertEqual([(path, {**body, "request_id": "req-script-0001"})], connection.posts())

    def test_without_the_flag_each_run_mints_its_own_id_as_before(self):
        for argv, path, body in MUTATIONS:
            with self.subTest(argv=argv):
                ids = []
                for _ in range(2):
                    _code, _out, _err, connection = self.run_cli(self.table(path), "--json", *argv)
                    (_path, sent), = connection.posts()
                    self.assertEqual(body, {key: value for key, value in sent.items() if key != "request_id"})
                    ids.append(sent["request_id"])
                self.assertTrue(all(item.startswith("req-") for item in ids))
                self.assertNotEqual(ids[0], ids[1])

    def test_a_scripted_retry_sends_the_same_id_and_body_every_time_so_the_host_can_run_it_once(self):
        # The host keeps one operation per (client, request ID) and answers a repeat with it; what the client owes
        # that is an identical request, however many times the script retries.
        for argv, path, _body in MUTATIONS:
            with self.subTest(argv=argv):
                core = paired_core(self.directory)
                sent = []
                for _ in range(3):
                    _code, _out, _err, connection = self.run_cli(self.table(path), "--json", *argv, "--request-id", "retry-me-1", core=core)
                    sent.extend(connection.posts())
                self.assertEqual(3, len(sent))
                self.assertEqual(1, len({json.dumps(item, sort_keys=True) for item in sent}), "the three sends are identical")
                self.assertEqual({"retry-me-1"}, {body["request_id"] for _path, body in sent})

    def test_a_retry_after_a_lost_answer_reuses_the_id_and_gets_the_original_operation(self):
        for argv, path, _body in MUTATIONS[:3]:
            with self.subTest(argv=argv):
                core = paired_core(self.directory)
                lost = {path: (200, ConnectionResetError("reset by peer"))}
                first_code, first_out, _e, first = self.run_cli(lost, "--json", *argv, "--request-id", "retry-me-2", core=core)
                self.assertEqual(2, first_code, "a lost answer is unknown, never failed")
                self.assertEqual({"ok": False, "error": "host connection failed: reset by peer", "unknown": True}, json.loads(first_out))
                original = {path: operation("accepted", request_id="retry-me-2")}
                second_code, second_out, _e, second = self.run_cli(original, "--json", *argv, "--request-id", "retry-me-2", core=core)
                self.assertEqual(2, second_code)
                self.assertEqual("retry-me-2", json.loads(second_out)["data"]["operation"]["request_id"])
                self.assertEqual("retry-me-2", first.posts()[0][1]["request_id"])
                self.assertEqual("retry-me-2", second.posts()[0][1]["request_id"])

    def test_an_id_that_would_be_refused_is_refused_before_anything_is_asked_or_sent(self):
        for bad in ("", "bad id", "x" * 129, "-lead", "a;b", "a\nb", "é", "../x"):
            for argv, path, _body in MUTATIONS[:2] + MUTATIONS[9:10]:
                with self.subTest(bad=bad, argv=argv):
                    # No --yes, so a command that got as far as asking would write a prompt and read stdin.
                    base = tuple(item for item in argv if item not in ("--yes", "--acknowledged"))
                    code, out, err, connection = self.run_cli(self.table(path), "--json", *base, "--request-id", bad)
                    self.assertEqual(1, code)
                    result = json.loads(out)
                    self.assertEqual((False, False), (result["ok"], result["unknown"]))
                    # A leading dash is read by argparse as another option; either way the command stops at the flag.
                    self.assertIn("--request-id", result["error"])
                    if not bad.startswith("-"):
                        self.assertIn("request_id is invalid", result["error"])
                    self.assertEqual("", err)
                    self.assertEqual([], connection.requests)

    def test_commands_that_change_nothing_on_the_host_do_not_take_one(self):
        for argv in UNNAMED:
            with self.subTest(argv=argv):
                code, out, _err, connection = self.run_cli({}, "--json", *argv, "--request-id", "req-script-0001")
                self.assertEqual(1, code)
                self.assertIn("unrecognized arguments", json.loads(out)["error"])
                self.assertEqual([], connection.requests)

    def test_each_command_that_changes_the_host_parses_the_flag_and_defaults_to_none(self):
        parser = cli._parser()
        for argv, _path, _body in MUTATIONS:
            with self.subTest(argv=argv):
                self.assertEqual("abc", parser.parse_args([*argv, "--request-id", "abc"]).request_id)
                self.assertIsNone(parser.parse_args(list(argv)).request_id)

    def test_the_id_reaches_the_core_in_the_request_dict_and_only_when_given(self):
        class Recorder:
            def __init__(self):
                self.requests: list[dict] = []

            def require_scope(self, _action: str) -> None:
                return None

            def run(self, request: dict) -> dict:
                self.requests.append(request)
                return {"operation": {"state": "succeeded"}}

        for argv, _path, _body in MUTATIONS:
            with contextlib.redirect_stderr(io.StringIO()):  # the confirmation prompt
                recorder = Recorder()
                args = cli._parser().parse_args([*argv, "--request-id", "abc"])
                cli._dispatch(args, recorder)
                self.assertEqual(["abc"], [request.get("request_id") for request in recorder.requests], argv)
                recorder = Recorder()
                cli._dispatch(cli._parser().parse_args(list(argv)), recorder)
                self.assertNotIn("request_id", recorder.requests[-1], argv)


class ExitCodeTests(CliCase):
    """0 ok, 1 failed (definitive), 2 unknown (accepted, or lost, with the result unconfirmed): what a script branches on."""

    def test_the_exit_code_of_an_operation_follows_its_state(self):
        for state, code in (("succeeded", 0), ("failed", 1), ("accepted", 2), ("dispatched", 2), ("observed_return", 2), ("unknown", 2)):
            with self.subTest(state=state):
                self.assertEqual(code, cli._exit_code({"operation": {"id": "op-1", "state": state}}))

    def test_a_result_with_no_operation_is_ok(self):
        for value in ({}, {"protocol_version": 1, "outputs": []}, {"operations": [{"state": "failed"}]}, {"operation": None}, {"operation": "failed"}, {"operation": []}):
            with self.subTest(value=value):
                self.assertEqual(0, cli._exit_code(value))

    def run_json(self, table: dict[str, Any], *argv: str):
        code, out, err, connection = self.run_cli(table, *argv)
        lines = out.splitlines()
        self.assertEqual(1, len(lines), "one document on every path")
        return code, json.loads(lines[0]), err, connection

    def test_the_matrix_end_to_end(self):
        refused = lambda status, message: (status, {"error": "x", "message": message})  # noqa: E731
        cases = [
            # label, table, argv, exit code, ok, unknown
            ("a read", {"/v1/status": (200, {"protocol_version": 1, "host_id": STATE["host_id"]})}, ("status",), 0, True, None),
            ("accepted", {"/v1/power": operation("accepted")}, ("power", "suspend", "--yes", "--acknowledged"), 2, True, None),
            ("dispatched", {"/v1/power": operation("dispatched")}, ("power", "suspend", "--yes", "--acknowledged"), 2, True, None),
            ("observed return", {"/v1/power": operation("observed_return")}, ("power", "suspend", "--yes", "--acknowledged"), 2, True, None),
            ("unknown", {"/v1/power": operation("unknown")}, ("power", "suspend", "--yes", "--acknowledged"), 2, True, None),
            ("succeeded", {"/v1/display/restore": operation("succeeded")}, ("display", "restore"), 0, True, None),
            ("failed", {"/v1/display/restore": operation("failed", reason="no")}, ("display", "restore"), 1, True, None),
            ("a refusal", {"/v1/display/restore": refused(409, "a display preview owns the mutation lane")}, ("display", "restore"), 1, False, False),
            ("a bad request", {"/v1/display/restore": refused(400, "invalid")}, ("display", "restore"), 1, False, False),
            ("a missing pairing", {"/v1/display/restore": refused(401, "credential has already been revoked")}, ("display", "restore"), 1, False, False),
            ("a lost write", {"/v1/power": (200, ConnectionResetError("reset by peer"))}, ("power", "restart", "--yes", "--acknowledged"), 2, False, True),
            ("a timed-out write", {"/v1/power": (200, TimeoutError("timed out"))}, ("power", "restart", "--yes", "--acknowledged"), 2, False, True),
            ("a lost read", {"/v1/status": (200, ConnectionRefusedError("refused"))}, ("status",), 1, False, False),
            ("a malformed reply", {"/v1/display/restore": (202, b"not json")}, ("display", "restore"), 2, False, True),
            ("an unknown command", {}, ("explode",), 1, False, False),
            ("a missing argument", {}, ("display", "save-current", "--output-id", "1"), 1, False, False),
            ("a bad number", {}, ("display", "preview", "--output-id", "1", "--mode-id", "2", "--generation", "x"), 1, False, False),
        ]
        for label, table, argv, code, ok, unknown in cases:
            with self.subTest(label):
                got_code, result, _err, _connection = self.run_json(table, "--json", *argv)
                self.assertEqual(code, got_code, result)
                self.assertEqual(ok, result["ok"])
                if unknown is not None:
                    self.assertEqual(unknown, result["unknown"])

    def test_a_refused_answer_carries_the_status_and_a_lost_one_does_not(self):
        _code, refused, _e, _c = self.run_json({"/v1/display/restore": (409, {"error": "x", "message": "m"})}, "--json", "display", "restore")
        self.assertEqual(409, refused["status"])
        _code, lost, _e, _c = self.run_json({"/v1/power": (200, ConnectionResetError("reset"))}, "--json", "power", "restart", "--yes", "--acknowledged")
        self.assertNotIn("status", lost)

    def test_a_declined_or_unavailable_confirmation_is_a_failure_and_sends_nothing(self):
        power = {"/v1/power": operation("accepted")}
        code, result, err, connection = self.run_json(power, "--json", "power", "suspend")
        self.assertEqual((1, False, False), (code, result["ok"], result["unknown"]))
        self.assertIn("interactive confirmation is unavailable", result["error"])
        self.assertEqual([], connection.requests)
        code, result, _err, connection = self.run_json(power, "--json", "power", "suspend", "--yes")
        self.assertEqual(1, code)
        self.assertIn("--yes requires --acknowledged", result["error"])
        self.assertEqual([], connection.requests)
        for answer in ("no", "yes", "", "Y"):
            with self.subTest(answer=answer), mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value=answer):
                code, result, _err, connection = self.run_json(power, "--json", "power", "suspend")
                self.assertEqual((1, "cancelled"), (code, result["error"]))
                self.assertEqual([], connection.requests)
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="YES"):
            code, _result, _err, connection = self.run_json(power, "--json", "power", "suspend")
        self.assertEqual((2, ["/v1/power"]), (code, [path for path, _body in connection.posts()]))

    def test_an_interrupt_or_a_closed_input_is_a_cancelled_failure_and_a_crash_is_not_shown(self):
        for raised, error in ((KeyboardInterrupt(), "cancelled"), (EOFError(), "cancelled"), (RuntimeError("secret token-value in a traceback"), "client command failed"), (ValueError("x"), "client command failed")):
            with self.subTest(raised=type(raised).__name__):
                out = io.StringIO()
                with mock.patch.object(cli, "_dispatch", side_effect=raised), mock.patch.object(cli, "ClientCore", lambda: object()), contextlib.redirect_stdout(out):
                    code = cli.main(["--json", "status"])
                self.assertEqual(1, code)
                self.assertEqual({"ok": False, "error": error, "unknown": False}, json.loads(out.getvalue()))
                self.assertNotIn("token-value", out.getvalue())

    def test_plain_output_carries_the_same_code_and_never_a_secret(self):
        code, out, _err, _connection = self.run_cli({"/v1/power": operation("accepted")}, "--plain", "power", "suspend", "--yes", "--acknowledged")
        self.assertEqual(2, code)
        self.assertIn("ok: True", out)
        self.assertNotIn("token-value", out)
        code, out, _err, _connection = self.run_cli({"/v1/power": (200, ConnectionResetError("reset"))}, "--plain", "power", "suspend", "--yes", "--acknowledged")
        self.assertEqual(2, code)
        self.assertIn("unknown: True", out)
        self.assertNotIn("token-value", out)


# The host's own answers to display/save-current, as Decky's `display-save-current` contract fixtures give them. The pinned
# bundle here vendors only part of the contract, so they are written out, and the last test compares them with Decky's.
SAVED = {
    "protocol_version": 1,
    "operation": {
        "id": "op-fixture0001xxxxxxxxxxxxx", "request_id": "save-current-1", "state": "succeeded", "kind": "display.save-current",
        "created_at": "2026-09-13T12:00:00.250000Z", "updated_at": "2026-09-13T12:00:00.250000Z", "outcome": "current_mode_visible_and_read_back",
        "reason": None, "target": {"profile_id": "profile-fixture0001xxxxxxxxxxxxx"},
    },
    "profile": {
        "id": "profile-fixture0001xxxxxxxxxxxxx", "output_identity": {"id": "1", "name": "HDMI-A-1", "description": "Reference", "is_internal": False},
        "output_id": "1", "mode": {"id": "3352", "width": 3440, "height": 1440, "refresh_hz": 59}, "verified_at": "2026-09-13T12:00:00.250000Z",
    },
}
HOST_ANSWERS = {
    "saved": (202, SAVED),
    "mutation-conflict": (409, {"error": "mutation_conflict", "message": "a display preview owns the mutation lane"}),
    "stale-target": (409, {"error": "stale_target", "message": "display target generation is stale"}),
    "bridge-unavailable": (503, {"error": "bridge_unavailable", "message": "Steam display bridge is unavailable"}),
    "readback-not-confirmed": (409, {"error": "readback_not_confirmed", "message": "current display mode could not be read back"}),
    "visible-false": (400, {"error": "invalid_request", "message": "visible must be true to save the current display mode"}),
}
SAVE = {"output_id": "1", "generation": 4, "visible": True}
ROUTE = "/v1/display/save-current"


class SaveCurrentTests(CliCase):
    """Doc 21's six paths of the client core: success, duplicate ID, conflict, stale target, unavailable bridge, missing assertion."""

    def request(self, answer: Any, arguments: dict | None = None, *, core: client_core.ClientCore | None = None):
        core = core or paired_core(self.directory)
        with serving({ROUTE: answer}) as connection:
            try:
                return core.request("save-current", copy.deepcopy(SAVE if arguments is None else arguments)), connection, core
            except client_core.ClientError as exc:
                return exc, connection, core

    def test_success_sends_exactly_the_four_fields_and_returns_the_hosts_profile(self):
        result, connection, core = self.request(HOST_ANSWERS["saved"], {**SAVE, "request_id": "save-current-1"})
        self.assertEqual(SAVED, result)
        self.assertEqual([(ROUTE, {"request_id": "save-current-1", "output_id": "1", "generation": 4, "visible": True})], connection.posts())
        self.assertNotIn("pending_operation", core.store.load(), "a terminal result leaves nothing to reconcile")

    def test_a_request_without_an_id_is_given_a_fresh_one(self):
        first, connection, _core = self.request(HOST_ANSWERS["saved"])
        second, other, _core = self.request(HOST_ANSWERS["saved"])
        ids = [connection.posts()[0][1]["request_id"], other.posts()[0][1]["request_id"]]
        self.assertTrue(all(item.startswith("req-") for item in ids))
        self.assertNotEqual(ids[0], ids[1])

    def test_a_duplicate_id_goes_out_unchanged_and_the_original_operation_comes_back(self):
        core = paired_core(self.directory)
        sent = []
        for _ in range(2):
            result, connection, _core = self.request(HOST_ANSWERS["saved"], {**SAVE, "request_id": "save-current-1"}, core=core)
            sent.append(connection.posts())
            self.assertEqual("op-fixture0001xxxxxxxxxxxxx", result["operation"]["id"])
        self.assertEqual(sent[0], sent[1])

    def refused(self, name: str, status: int, message: str) -> None:
        result, connection, core = self.request(HOST_ANSWERS[name])
        self.assertIsInstance(result, client_core.ClientError)
        self.assertEqual((status, message, False), (result.status, str(result), result.unknown), "a definitive answer is never unknown")
        self.assertEqual(1, len(connection.posts()))
        self.assertNotIn("pending_operation", core.store.load(), "a refusal leaves no operation to reconcile")

    def test_a_conflict_with_a_running_preview_is_the_hosts_refusal(self):
        self.refused("mutation-conflict", 409, "a display preview owns the mutation lane")

    def test_a_stale_target_is_the_hosts_refusal(self):
        self.refused("stale-target", 409, "display target generation is stale")

    def test_an_unavailable_bridge_is_the_hosts_refusal(self):
        self.refused("bridge-unavailable", 503, "Steam display bridge is unavailable")

    def test_a_readback_the_host_could_not_confirm_is_a_refusal_not_a_save(self):
        self.refused("readback-not-confirmed", 409, "current display mode could not be read back")

    def test_a_lost_answer_is_unknown_and_never_a_save(self):
        result, _connection, _core = self.request((200, ConnectionResetError("reset by peer")))
        self.assertIsInstance(result, client_core.ClientError)
        self.assertTrue(result.unknown)

    def test_without_the_owners_assertion_nothing_is_sent(self):
        for visible in (None, False, "true", 1, 0, [], {}, "yes"):
            with self.subTest(visible=visible):
                arguments = {key: value for key, value in SAVE.items() if key != "visible"}
                if visible is not None:
                    arguments["visible"] = visible
                result, connection, _core = self.request(HOST_ANSWERS["saved"], arguments)
                self.assertIsInstance(result, client_core.ClientError)
                self.assertIn("explicit picture confirmation", str(result))
                self.assertEqual([], connection.requests)

    def test_a_target_the_client_would_not_name_is_refused_before_it_is_sent(self):
        for changes in ({"output_id": "bad id"}, {"output_id": ""}, {"output_id": None}, {"generation": -1}, {"generation": True}, {"generation": "4"}, {"generation": 2 ** 31}, {"request_id": "bad id"}):
            with self.subTest(changes=changes):
                result, connection, _core = self.request(HOST_ANSWERS["saved"], {**SAVE, **changes})
                self.assertIsInstance(result, client_core.ClientError)
                self.assertEqual([], connection.requests)

    def test_the_hosts_own_400_for_a_missing_assertion_is_what_the_client_would_show_if_it_ever_got_that_far(self):
        # The client refuses first; this is the host's answer to a body that omitted it, kept so a drift in either is seen.
        status, body = HOST_ANSWERS["visible-false"]
        self.assertEqual((400, "invalid_request"), (status, body["error"]))

    def test_the_answers_above_are_the_ones_decky_pins(self):
        if not DECKY_SAVE_CURRENT.exists():
            self.skipTest("the Decky checkout is not present")
        for name, (status, body) in HOST_ANSWERS.items():
            response = json.loads((DECKY_SAVE_CURRENT / f"{name}.json").read_text())["response"]
            self.assertEqual((status, body), (response["status"], response["body"]), name)
        sent = json.loads((DECKY_SAVE_CURRENT / "saved.json").read_text())["request"]
        self.assertEqual(("POST", ROUTE), (sent["method"], sent["path"]))
        self.assertEqual({"request_id", "output_id", "generation", "visible"}, set(sent["body"]))


class SaveCurrentCliTests(CliCase):
    """The command asks the owner first, and `visible: true` leaves only after they have said so."""

    ARGV = ("--json", "display", "save-current", "--output-id", "1", "--generation", "4")

    def run_save(self, *extra: str, tty: bool = False, answer: str = ""):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=tty), mock.patch("builtins.input", return_value=answer):
            return self.run_cli({ROUTE: HOST_ANSWERS["saved"]}, *self.ARGV, *extra)

    def test_a_script_must_say_both_yes_and_acknowledged_on_every_call(self):
        for extra in ((), ("--yes",), ("--acknowledged",)):
            with self.subTest(extra=extra):
                code, out, err, connection = self.run_save(*extra)
                self.assertEqual(1, code)
                self.assertFalse(json.loads(out)["ok"])
                self.assertEqual([], connection.requests)
        code, _out, _err, connection = self.run_save("--yes", "--acknowledged")
        self.assertEqual(0, code)
        self.assertEqual([(ROUTE, True)], [(path, body["visible"]) for path, body in connection.posts()])

    def test_the_prompt_is_the_one_wording_decky_uses_with_the_paired_host_as_the_device(self):
        _code, _out, err, _connection = self.run_save("--yes", "--acknowledged")
        expected = "Is the picture on the paired SteamOS host's display correct right now? This is what Restore saved display will return to."
        self.assertEqual(expected, cli._CONFIRM_TEXT)
        self.assertEqual(expected + "\n", err)
        if DECKY_FRONTEND.exists():
            template = "Is the picture on ${identityName()}'s display correct right now? This is what Restore saved display will return to."
            self.assertIn(template, DECKY_FRONTEND.read_text())
            self.assertEqual(template.replace("${identityName()}", "the paired SteamOS host"), expected)

    def test_an_interactive_owner_must_type_the_word(self):
        for answer in ("", "y", "yes", "Y", " YES", "YES "):
            with self.subTest(answer=answer):
                code, out, _err, connection = self.run_save(tty=True, answer=answer)
                self.assertEqual((1, "cancelled", []), (code, json.loads(out)["error"], connection.requests))
        code, _out, _err, connection = self.run_save(tty=True, answer="YES")
        self.assertEqual((0, True), (code, connection.posts()[0][1]["visible"]))

    def test_the_keep_prompt_names_the_same_device(self):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            _code, _out, err, _connection = self.run_cli({"/v1/display/confirm": operation("succeeded")}, "--json", "display", "keep", "--preview-id", "preview-1", "--yes", "--acknowledged")
        self.assertEqual("Can you see the picture on the paired SteamOS host's display? Keeping it saves this mode.\n", err)


if __name__ == "__main__":
    unittest.main()
