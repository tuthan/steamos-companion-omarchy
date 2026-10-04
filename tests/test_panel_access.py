"""What the owner of the host allows this pairing, as the panel shows and enforces it.

The panel cannot be run here, so its functions are run under Node straight out of
`Panel.qml`, the way `test_journal.py` runs its block. Three things are covered. The pure
`access` block must word every set of scopes exactly as Decky's own block does (Decky's
`access:begin` to `access:end` is run beside it) and must read what a status reply reports
as untrusted. Every control and every action must check the scope it needs through the one
`powerAllowed` / `displayAllowed` pair, with every other precondition satisfied so that only
the scope can decide. And a refused change must relearn the scopes without removing anything.
"""
from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "protocol" / "fixtures" / "contract"
DECKY_FRONTEND = Path("/home/hvo/Projects/steamos-companion-decky/host/frontend/index.js")
FULL = ["status.read", "power.control", "display.control"]
VIEW_ONLY = ["status.read"]
POWER_ONLY = ["status.read", "power.control"]
DISPLAY_ONLY = ["status.read", "display.control"]
NO_POWER = "The host owner has not allowed Power for this pairing."
NO_DISPLAY = "The host owner has not allowed Display for this pairing."
NOTE = "The owner of the host chose this and can change it there."
REMOVAL_WORDS = r"(?i)revok|remov|forgot|forgiv|unpair|no longer paired|deleted"


def panel_source() -> str:
    return (ROOT / "Panel.qml").read_text()


def panel_block() -> str:
    found = re.findall(r"// access:begin\n(.*?)  // access:end", panel_source(), re.S)
    assert len(found) == 1, "Panel.qml must carry exactly one access block"
    return found[0]


def function_source(name: str) -> str:
    """One root-level function of Panel.qml, exactly as written."""
    found = re.findall(rf"\n(  function {name}\(.*?\n  }})\n", panel_source(), re.S)
    assert len(found) == 1, f"Panel.qml must define {name} once"
    return found[0]


HARNESS = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = input.cases.map(item => {
  // The QML root: its properties and its functions share one scope, which is also the global scope.
  const root = Object.assign({}, item.state);
  const recorded = [];
  const callbacks = [];
  vm.createContext(root);
  root.root = root;
  root.Qt = {formatDateTime: () => '00:00:00', callLater() {}};
  vm.runInContext(input.block, root);
  for (const source of input.sources) vm.runInContext(source, root);
  for (const name of input.stubs) {
    root[name] = (...args) => {
      recorded.push([name, JSON.parse(JSON.stringify(args.filter(arg => typeof arg !== 'function')))]);
      for (const arg of args) if (typeof arg === 'function') callbacks.push([name, arg]);
      return item.stubReturns && name in item.stubReturns ? item.stubReturns[name] : undefined;
    };
  }
  const results = item.calls.map(([name, args]) => {
    try {
      const value = root[name](...args);
      return {value: value === undefined ? null : JSON.parse(JSON.stringify(value))};
    } catch (error) {
      return {error: String(error)};
    }
  });
  for (const [index, result] of (item.invoke || [])) callbacks[index][1](result);
  const state = {};
  for (const key of input.watch) state[key] = root[key] === undefined ? null : JSON.parse(JSON.stringify(root[key]));
  return {results, recorded, state};
});
process.stdout.write(JSON.stringify(out));
"""


def run_node(cases: list[dict], *, sources: list[str] = (), stubs: list[str] = (), watch: list[str] = ()) -> list[dict]:
    payload = {"block": panel_block(), "sources": list(sources), "stubs": list(stubs), "watch": list(watch), "cases": cases}
    result = subprocess.run(["node", "-e", HARNESS], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout)


def call_block(calls: list[tuple]) -> list[Any]:
    out = run_node([{"state": {}, "calls": [[name, list(args)] for name, *args in calls]}])[0]["results"]
    for index, item in enumerate(out):
        assert "error" not in item, (calls[index], item)
    return [item["value"] for item in out]


WORDING = {
    (): ("No access", "No permissions", NOTE),
    ("status.read",): ("View only", "Status", NOTE),
    ("power.control",): ("Custom", "Power", NOTE),
    ("display.control",): ("Custom", "Display", NOTE),
    ("status.read", "power.control"): ("Custom", "Status · Power", NOTE),
    ("status.read", "display.control"): ("Custom", "Status · Display", NOTE),
    ("power.control", "display.control"): ("Custom", "Power · Display", NOTE),
    ("status.read", "power.control", "display.control"): ("Full access", "Status · Power · Display", ""),
}


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml access functions")
class AccessBlockTests(unittest.TestCase):
    def test_the_block_is_pure_and_es5(self):
        block = panel_block()
        for forbidden in ("root.", "statusData", "localData", "enqueueHelper", "remote(", "Timer", "Date.now", "Qt.", "setInterval", "setTimeout"):
            self.assertNotIn(forbidden, block)
        for name in ("accessTable", "accessHeld", "accessSummary", "accessRole", "accessNote", "accessLine", "accessAllows", "accessReason", "accessReported", "accessSame"):
            self.assertIn(f"function {name}(", block)
        code = "\n".join(line.split("//")[0] for line in block.splitlines())
        self.assertNotRegex(code, r"=>|\blet\b|\bconst\b|`|\.\.\.|\bclass\b|\.padStart|\.includes\(|Number\.isInteger|\.startsWith|\.endsWith|for\s*\(\s*(?:var\s+)?\w+\s+of\b")

    def test_every_set_of_scopes_is_worded_as_the_deck_words_it(self):
        sets = [list(order) for held in WORDING for order in itertools.permutations(held)]
        calls = [(name, scopes) for scopes in sets for name in ("accessRole", "accessSummary", "accessNote", "accessLine")]
        values = call_block(calls)
        for index, scopes in enumerate(sets):
            role, summary, note, line = values[index * 4: index * 4 + 4]
            with self.subTest(scopes=scopes):
                self.assertEqual(WORDING[tuple(sorted(scopes, key=FULL.index))], (role, summary, note))
                self.assertEqual(f"{role} · {summary}", line)

    def test_unknown_repeated_and_odd_values_count_for_nothing(self):
        junk = [None, "status.read", 5, True, {}, {"status.read": True}, [], ["x"], [1, None, {}, []], ["status.read", "status.read", "sunshine.control"], ["status.read\n", "power.control "], ["sunshine.control"]]
        roles = call_block([("accessRole", scopes) for scopes in junk])
        self.assertEqual(["No access", "No access", "No access", "No access", "No access", "No access", "No access", "No access", "No access", "View only", "No access", "No access"], roles)

    @unittest.skipUnless(DECKY_FRONTEND.exists(), "the Decky checkout is not present")
    def test_the_wording_equals_the_decky_frontend(self):
        found = re.search(r"/\* access:begin \*/([\s\S]*?)/\* access:end \*/", DECKY_FRONTEND.read_text())
        self.assertTrue(found, "the Decky access block moved")
        inputs: list[Any] = [list(order) for held in WORDING for order in itertools.permutations(held)]
        inputs += [None, "status.read", 5, True, {}, {"status.read": True}, [], ["x"], [1, None, {}, []], ["status.read", "status.read"], [*FULL, "sunshine.control"], ["sunshine.control", "status.read"], ["status.read\n", "power.control "]]
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const mine = vm.createContext({});
vm.runInContext(input.block, mine);
const decky = vm.runInNewContext(`${input.decky}; ({accessRole, accessSummary, accessNote})`, {});
process.stdout.write(JSON.stringify(input.cases.map(scopes => [
  [mine.accessRole(scopes), decky.accessRole(scopes)],
  [mine.accessSummary(scopes), decky.accessSummary(scopes)],
  [mine.accessNote(scopes), decky.accessNote(scopes)],
])));
"""
        result = subprocess.run(["node", "-e", script], input=json.dumps({"block": panel_block(), "decky": found.group(1), "cases": inputs}), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr[-400:])
        for scopes, pairs in zip(inputs, json.loads(result.stdout)):
            with self.subTest(scopes=scopes):
                for mine, theirs in pairs:
                    self.assertEqual(theirs, mine)

    def test_the_reasons_are_the_exact_plain_words(self):
        values = call_block([
            ("accessReason", VIEW_ONLY, "power.control"), ("accessReason", VIEW_ONLY, "display.control"), ("accessReason", FULL, "power.control"), ("accessReason", FULL, "display.control"),
            ("accessReason", POWER_ONLY, "display.control"), ("accessReason", DISPLAY_ONLY, "power.control"), ("accessReason", None, "power.control"), ("accessReason", [], "x"),
        ])
        self.assertEqual([NO_POWER, NO_DISPLAY, "", "", NO_DISPLAY, NO_POWER, NO_POWER, "The host owner has not allowed this action for this pairing."], values)

    def test_what_a_status_reply_reports_is_decoded_again_and_silence_is_not_no_access(self):
        def status(name: str) -> dict:
            return json.loads((CONTRACT / "status" / f"{name}.json").read_text())["response"]["body"]

        replies = [status("sunshine-running"), status("view-only-credential"), status("scopes-changed-by-owner")]
        older = status("sunshine-running")
        del older["credential"]
        silent = [older, {**older, "credential": None}, {**older, "credential": "all"}, {**older, "credential": ["status.read"]}, {**older, "credential": {}}, {**older, "credential": {"scopes": "status.read"}}, {**older, "credential": {"scopes": None}}, None, "x", [], 5]
        hostile = [{"credential": {"scopes": ["status.read", "sunshine.control", "status.read", "evil\u0000", 5, None, {}]}}, {"credential": {"scopes": [*FULL] * 1000}}]
        values = call_block([("accessReported", item) for item in [*replies, *silent, *hostile]] + [("accessReported", {"credential": {"scopes": []}})])
        self.assertEqual([FULL, VIEW_ONLY, DISPLAY_ONLY], values[:3])
        self.assertEqual([None] * len(silent), values[3: 3 + len(silent)])
        self.assertEqual([VIEW_ONLY, FULL], values[3 + len(silent): 5 + len(silent)])
        self.assertEqual([], values[-1], "an empty list is a real answer")
        self.assertIsNotNone(values[-1])

    def test_same_compares_what_counts(self):
        values = call_block([("accessSame", FULL, ["display.control", "power.control", "status.read"]), ("accessSame", VIEW_ONLY, [*VIEW_ONLY, "sunshine.control"]), ("accessSame", VIEW_ONLY, FULL), ("accessSame", [], None)])
        self.assertEqual([True, True, False, True], values)


def function_sources(*names: str) -> list[str]:
    return [function_source(name) for name in names]


GATE_FUNCTIONS = (
    "heldScopes", "powerAllowed", "displayAllowed", "powerReason", "displayReason", "canRestoreWorking", "canSuspendHost", "powerAvailable", "sunshineRestartAvailable",
    "rowEnabled", "displayOrderBlock", "displayOrderSavedKeysFor", "displayOrderCanEdit", "displayOrderCanSave", "displayOrderMoveAvailable", "displayOrderRestartAvailable",
)


def permissive_state(scopes: Any, **changes: Any) -> dict:
    """A panel in which everything but the scope would allow the action."""
    state = {
        "view": "host",
        "paired": True,
        "localData": {"paired": True, "scopes": scopes},
        "reportedScopes": None,
        "statusData": {
            "steam_bridge": "ready",
            "capabilities": {"suspend": "available", "restart": "available", "shutdown": "available", "display_rescue": "available", "sunshine_restart": "available"},
            "sunshine": {"enabled": True, "state": "stopped"},
        },
        "actionBusy": False,
        "wakeMac": "001122334455",
        "previewData": None,
        "previewMutationBusy": False,
        "currentOutput": {"id": "1", "current_mode_id": "2", "generation": 4},
        "outputList": [{"id": "1"}, {"id": "2"}],
        "displayOrderData": {"display_order": {"available": True, "restart_available": True, "output_keys": ["a", "b"], "saved_output_keys": ["a"]}},
        "displayOrderMutation": "",
        "displayOrderRefreshRequested": False,
        "displayOrderPendingKeys": [],
        "displayOrderDraft": ["a", "b"],
        "displayOrderDraftStale": False,
        "displayOrderError": "",
        "actionMessage": "",
        "confirmationKind": "",
        "selectedModeId": "3",
        "selectedMode": {"id": "3"},
        "saveCurrentTarget": None,
        "operationData": None,
    }
    state.update(changes)
    return state


# (view, row key, scope it needs or None)
ROWS = [
    ("host", "restore", "display.control"),
    ("host", "wake", None),
    ("host", "suspend", "power.control"),
    ("host", "restart", "power.control"),
    ("host", "shutdown", "power.control"),
    ("host", "sunshine", "power.control"),
    ("display", "save-current", "display.control"),
    ("display", "restore", "display.control"),
    ("display", "display-order:save", "display.control"),
    ("display", "display-order:restart", "display.control"),
    ("display", "display-order:automatic", "display.control"),
    ("display", "display-order:up:b", "display.control"),
    ("display", "display-order:down:a", "display.control"),
    ("display", "display-order:refresh", None),
    ("display", "output", None),
    ("display", "mode:3", None),
]
SCOPE_SETS = [FULL, VIEW_ONLY, [], POWER_ONLY, DISPLAY_ONLY]


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml access functions")
class ControlGateTests(unittest.TestCase):
    def enabled(self, scopes: Any, **changes: Any) -> dict[tuple[str, str], bool]:
        cases = [{"state": permissive_state(scopes, view=view, **changes), "calls": [["rowEnabled", [key]]]} for view, key, _ in ROWS]
        out = run_node(cases, sources=function_sources(*GATE_FUNCTIONS))
        for (view, key, _), item in zip(ROWS, out):
            self.assertNotIn("error", item["results"][0], (view, key, item))
        return {(view, key): item["results"][0]["value"] for (view, key, _), item in zip(ROWS, out)}

    def test_with_every_other_condition_met_a_pairing_with_every_scope_can_use_every_control(self):
        self.assertEqual({(view, key): True for view, key, _ in ROWS}, self.enabled(FULL))

    def test_each_control_needs_exactly_the_scope_it_acts_with(self):
        for scopes in SCOPE_SETS:
            enabled = self.enabled(scopes)
            for view, key, needs in ROWS:
                with self.subTest(scopes=scopes, row=f"{view}/{key}"):
                    self.assertEqual(needs is None or needs in scopes, enabled[(view, key)])

    def test_a_pairing_with_nothing_but_status_keeps_wake_refresh_output_and_mode_selection(self):
        enabled = self.enabled(VIEW_ONLY)
        self.assertEqual({"wake", "display-order:refresh", "output", "mode:3"}, {key for (_view, key), on in enabled.items() if on})

    def test_a_scope_the_poll_just_removed_disables_the_control_at_once(self):
        # The status poll replaces localData wholesale; the same functions then read the new list.
        before = self.enabled(FULL)
        after = self.enabled(DISPLAY_ONLY)
        self.assertTrue(before[("host", "suspend")] and not after[("host", "suspend")])
        self.assertTrue(after[("host", "restore")])

    def test_what_the_status_poll_reported_wins_over_what_was_saved_with_the_pairing(self):
        # Saved with every scope, but the last status read said view only: the controls follow the host.
        enabled = self.enabled(FULL, reportedScopes=VIEW_ONLY)
        self.assertEqual({"wake", "display-order:refresh", "output", "mode:3"}, {key for (_view, key), on in enabled.items() if on})
        # Saved view only, and the owner has since granted Display: the controls follow the host the other way too.
        enabled = self.enabled(VIEW_ONLY, reportedScopes=DISPLAY_ONLY)
        self.assertTrue(enabled[("display", "save-current")] and not enabled[("host", "suspend")])
        # An empty reported list is an answer, not silence.
        enabled = self.enabled(FULL, reportedScopes=[])
        for view, key, needs in ROWS:
            if needs:
                self.assertFalse(enabled[(view, key)], (view, key))

    def test_held_scopes_are_the_reported_ones_else_the_saved_ones_and_nothing_when_unpaired(self):
        cases = [
            ({"reportedScopes": VIEW_ONLY, "localData": {"scopes": FULL}}, VIEW_ONLY),
            ({"reportedScopes": [], "localData": {"scopes": FULL}}, []),
            ({"reportedScopes": None, "localData": {"scopes": POWER_ONLY}}, POWER_ONLY),
            ({"reportedScopes": FULL, "localData": {"scopes": VIEW_ONLY}, "paired": False}, []),
        ]
        out = run_node([{"state": {"paired": True, **state}, "calls": [["heldScopes", []]]} for state, _expected in cases], sources=function_sources("heldScopes"))
        self.assertEqual([expected for _state, expected in cases], [item["results"][0]["value"] for item in out])

    def test_an_unpaired_panel_enables_nothing_that_needs_a_scope(self):
        enabled = self.enabled(FULL, paired=False)
        for (view, key), on in enabled.items():
            if key not in ("mode:3", "output"):
                self.assertFalse(on, (view, key))

    def test_a_missing_or_odd_scope_list_in_the_saved_state_is_no_access_not_full_access(self):
        for odd in (None, "status.read power.control display.control", {"power.control": True}, 7):
            enabled = self.enabled(odd)
            for view, key, needs in ROWS:
                if needs:
                    self.assertFalse(enabled[(view, key)], (odd, view, key))


ACTIONS = [
    # function, arguments, state changes, expected host call, scope it needs
    ("restoreWorking", [], {}, "restore", "display.control"),
    ("restartSunshine", [], {}, "sunshine-restart", "power.control"),
    ("previewModeFor", [{"id": "3"}], {}, "preview", "display.control"),
    ("keepPreview", [], {"previewData": {"preview_id": "p1"}}, "confirm", "display.control"),
    ("revertPreview", [], {"previewData": {"preview_id": "p1"}}, "restore", "display.control"),
    ("saveCurrentDisplay", [{"output_id": "1", "generation": 4}], {}, "save-current", "display.control"),
    ("resetDisplayOrder", [], {}, "display-order-reset", "display.control"),
    ("saveDisplayOrder", [False], {}, "display-order-save", "display.control"),
    ("saveDisplayOrder", [True], {}, "display-order-restart", "display.control"),
    ("confirmConfirmation", [], {"confirmationKind": "suspend"}, "power", "power.control"),
    ("confirmConfirmation", [], {"confirmationKind": "restart"}, "power", "power.control"),
    ("confirmConfirmation", [], {"confirmationKind": "shutdown"}, "power", "power.control"),
]
ACTION_FUNCTIONS = (
    *GATE_FUNCTIONS, "restoreWorking", "restartSunshine", "previewModeFor", "keepPreview", "revertPreview", "saveCurrentDisplay", "resetDisplayOrder",
    "saveDisplayOrder", "confirmConfirmation", "openSaveCurrentConfirmation",
)
ACTION_STUBS = ["remote", "refreshAfterAction", "tickCountdown", "openConfirmation", "setView", "friendlyError", "settleDisplayOrderOperation", "displayOrderGenerationFor", "displayOrderArraysEqual", "modeLabel"]


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml access functions")
class ActionGateTests(unittest.TestCase):
    def run_action(self, scopes: Any, function: str, args: list, changes: dict) -> dict:
        case = {"state": permissive_state(scopes, **changes), "calls": [[function, args]], "stubReturns": {"displayOrderGenerationFor": 3}}
        item = run_node([case], sources=function_sources(*ACTION_FUNCTIONS), stubs=ACTION_STUBS, watch=["actionMessage"])[0]
        self.assertNotIn("error", item["results"][0], item)
        return item

    def host_calls(self, item: dict) -> list[str]:
        return [args[0] for name, args in item["recorded"] if name == "remote"]

    def test_with_every_scope_each_action_reaches_the_host_through_its_one_call(self):
        for function, args, changes, expected, _needs in ACTIONS:
            with self.subTest(function=function, kind=changes.get("confirmationKind"), args=args):
                self.assertEqual([expected], self.host_calls(self.run_action(FULL, function, args, changes)))

    def test_without_the_scope_nothing_is_sent_and_the_reason_is_shown(self):
        for function, args, changes, _expected, needs in ACTIONS:
            held = DISPLAY_ONLY if needs == "power.control" else POWER_ONLY
            for scopes in (VIEW_ONLY, [], held):
                with self.subTest(function=function, kind=changes.get("confirmationKind"), scopes=scopes):
                    item = self.run_action(scopes, function, args, changes)
                    self.assertEqual([], self.host_calls(item))
                    # These return without a message, as they always have: their rows are disabled and the reason sits beside them.
                    if function not in ("saveDisplayOrder", "restartSunshine", "resetDisplayOrder"):
                        self.assertEqual(NO_POWER if needs == "power.control" else NO_DISPLAY, item["state"]["actionMessage"])

    def test_the_save_current_dialog_is_not_even_opened_without_display(self):
        for scopes, opened in ((FULL, ["save-current"]), (VIEW_ONLY, []), (POWER_ONLY, [])):
            with self.subTest(scopes=scopes):
                item = self.run_action(scopes, "openSaveCurrentConfirmation", [], {})
                self.assertEqual(opened, [args[0] for name, args in item["recorded"] if name == "openConfirmation"])

    def test_an_action_for_one_scope_does_not_borrow_the_other(self):
        for function, args, changes, _expected, needs in ACTIONS:
            held = POWER_ONLY if needs == "power.control" else DISPLAY_ONLY
            with self.subTest(function=function, kind=changes.get("confirmationKind")):
                self.assertEqual(1, len(self.host_calls(self.run_action(held, function, args, changes))))


REFRESH_FUNCTIONS = ("remote", "adoptScopes", "refresh", "powerAllowed", "displayAllowed")


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml access functions")
class RelearnTests(unittest.TestCase):
    def test_a_refused_change_asks_for_status_again_and_still_hands_over_the_result(self):
        refused = {"ok": False, "error": "The host owner has not allowed this action for this pairing.", "unknown": False, "status": 403}
        other = [{"ok": False, "error": "host connection failed", "unknown": True}, {"ok": False, "error": "x", "status": 409}, {"ok": False, "error": "x", "status": 401}, {"ok": True, "data": {}}]
        cases = [{"state": {"opened": True, "paired": True}, "calls": [["remote", ["power", {"action": "suspend"}, None]]], "invoke": [[0, result]]} for result in [refused, *other]]
        out = run_node(cases, sources=function_sources("remote"), stubs=["enqueueHelper", "refresh"], watch=[])
        refreshed = [any(name == "refresh" for name, _ in item["recorded"]) for item in out]
        self.assertEqual([True, False, False, False, False], refreshed)

    def test_the_callback_still_runs_for_every_result(self):
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = {refreshed: 0, seen: [], enqueueHelper(action, args, wrapped) { root.wrapped = wrapped }, refresh() { root.refreshed++ }};
vm.createContext(root); root.root = root;
vm.runInContext(input.source, root);
for (const result of input.results) {
  root.remote('power', {}, value => root.seen.push(value.status || 'ok'));
  root.wrapped(result);
}
root.remote('power', {});
root.wrapped({ok: false, status: 403});
process.stdout.write(JSON.stringify({seen: root.seen, refreshed: root.refreshed}));
"""
        results = [{"ok": False, "status": 403}, {"ok": False, "status": 409}, {"ok": True}]
        run = subprocess.run(["node", "-e", script], input=json.dumps({"source": function_source("remote"), "results": results}), capture_output=True, text=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr[-400:])
        self.assertEqual({"seen": [403, 409, "ok"], "refreshed": 2}, json.loads(run.stdout))

    def test_the_status_poll_adopts_what_the_host_reports_and_only_that(self):
        view_only = {"credential": {"scopes": ["status.read"]}}
        cases = [
            # name, saved, reported before, reply, reported after
            ("a downgrade", FULL, None, view_only, VIEW_ONLY),
            ("an upgrade", VIEW_ONLY, None, {"credential": {"scopes": FULL}}, FULL),
            ("an empty list", FULL, None, {"credential": {"scopes": []}}, []),
            ("nothing reported", DISPLAY_ONLY, None, {}, None),
            ("a block that says nothing", DISPLAY_ONLY, None, {"credential": "all"}, None),
            ("no list", DISPLAY_ONLY, None, {"credential": {"scopes": "status.read"}}, None),
            ("unknown and repeated values", VIEW_ONLY, None, {"credential": {"scopes": ["display.control", "x", "display.control", "status.read"]}}, DISPLAY_ONLY),
            ("a later silence keeps the last word", FULL, VIEW_ONLY, {}, VIEW_ONLY),
            ("a later change replaces it", FULL, VIEW_ONLY, {"credential": {"scopes": POWER_ONLY}}, POWER_ONLY),
            ("the owner restoring what was saved after a downgrade", FULL, VIEW_ONLY, {"credential": {"scopes": FULL}}, FULL),
            ("the same as saved changes nothing", FULL, None, {"credential": {"scopes": FULL}}, None),
            ("the same as reported changes nothing", FULL, VIEW_ONLY, view_only, VIEW_ONLY),
        ]
        out = run_node(
            [{"state": {"paired": True, "reportedScopes": before, "localData": {"paired": True, "scopes": saved, "client_id": "c1", "endpoint": "https://h:1"}}, "calls": [["adoptScopes", [reply]]]} for _n, saved, before, reply, _e in cases],
            sources=function_sources("heldScopes", "adoptScopes"), watch=["reportedScopes", "localData"],
        )
        for (name, saved, _before, _reply, expected), item in zip(cases, out):
            with self.subTest(name):
                self.assertNotIn("error", item["results"][0])
                self.assertEqual(expected, item["state"]["reportedScopes"])
                self.assertEqual({"paired": True, "scopes": saved, "client_id": "c1", "endpoint": "https://h:1"}, item["state"]["localData"], "the pairing itself is never rewritten here")

    def test_the_reported_scopes_are_cleared_wherever_the_saved_pairing_is_read_again_or_reset(self):
        source = panel_source()
        self.assertEqual(4, source.count("root.reportedScopes = "), "adoptScopes, both branches of localInspect, resetPairedState")
        self.assertEqual(2, function_source("localInspect").count("root.reportedScopes = null"))
        self.assertEqual(1, function_source("resetPairedState").count("root.reportedScopes = null"))
        self.assertIn("root.reportedScopes = reported", function_source("adoptScopes"))
        self.assertEqual(3, source.count("root.localData = "), "a scope change is not a pairing change: no other place assigns the pairing")

    def test_a_successful_status_poll_adopts_the_scopes_and_a_failed_one_leaves_them(self):
        view_only = {"steam_bridge": "ready", "credential": {"scopes": ["status.read"]}}
        base = {"opened": True, "paired": True, "localData": {"paired": True, "scopes": FULL}, "pollIntervalMs": 2000, "connectionState": "ok", "statusData": None}
        cases = [
            {"state": dict(base), "calls": [["refresh", []]], "invoke": [[0, {"ok": True, "data": view_only}]]},
            {"state": dict(base), "calls": [["refresh", []]], "invoke": [[0, {"ok": False, "error": "host connection failed: [Errno 111] Connection refused", "unknown": False}]]},
        ]
        out = run_node(cases, sources=function_sources("refresh"), stubs=["enqueueHelper", "friendlyError", "refreshDetails", "adoptScopes"], watch=["statusData", "connectionState"])
        adopted = [[args for name, args in item["recorded"] if name == "adoptScopes"] for item in out]
        self.assertEqual([[[view_only]], []], adopted)
        self.assertIsNone(out[1]["state"]["statusData"])
        self.assertEqual("unreachable", out[1]["state"]["connectionState"])


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml access functions")
class WordingTests(unittest.TestCase):
    def test_an_unreachable_host_never_reads_as_a_removed_pairing(self):
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = {};
vm.createContext(root); root.root = root;
vm.runInContext(input.source, root);
process.stdout.write(JSON.stringify(input.messages.map(message => root.friendlyError(message))));
"""
        messages = [
            "host connection failed: [Errno 111] Connection refused", "host connection failed: [Errno 113] No route to host", "host connection failed: [Errno 101] Network is unreachable",
            "host connection failed: timed out", "host connection failed: [Errno 110] Connection timed out", "host connection failed: _ssl.c:983: The handshake operation timed out",
            "host connection failed: Remote end closed connection without response", "remote access is locked", "host_locked",
        ]
        run = subprocess.run(["node", "-e", script], input=json.dumps({"source": function_source("friendlyError"), "messages": messages}), capture_output=True, text=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr[-400:])
        for message, shown in zip(messages, json.loads(run.stdout)):
            with self.subTest(message):
                self.assertNotRegex(shown, REMOVAL_WORDS)
        self.assertEqual("Host refused the connection", json.loads(run.stdout)[0])

    def test_the_offline_path_of_the_panel_says_nothing_about_a_removed_pairing(self):
        source = panel_source()
        for name in ("refresh", "friendlyError", "describeError"):
            self.assertNotRegex(function_source(name), REMOVAL_WORDS, name)
        self.assertRegex(source, r'text: "Host unreachable: " \+ root\.connectionDetail')
        offline = re.search(r'text: "Host unreachable: .*', source).group(0)
        self.assertNotRegex(offline, REMOVAL_WORDS)

    def test_a_refused_change_is_shown_as_the_helper_words_it(self):
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = {};
vm.createContext(root); root.root = root;
vm.runInContext(input.source, root);
process.stdout.write(JSON.stringify(root.friendlyError(input.message)));
"""
        message = "The host owner has not allowed this action for this pairing."
        run = subprocess.run(["node", "-e", script], input=json.dumps({"source": function_source("friendlyError"), "message": message}), capture_output=True, text=True, timeout=20)
        self.assertEqual(message, json.loads(run.stdout))


class SourceTests(unittest.TestCase):
    def test_the_connection_details_carry_the_access_line_from_the_one_place_that_words_it(self):
        source = panel_source()
        group = source.split('id: accessGroup', 1)[1].split("\n      }\n", 1)[0]
        self.assertIn('BodyText { width: parent.width; text: "Access on this host" }', group)
        self.assertIn("root.accessLine(accessGroup.scopesNow)", group)
        self.assertIn("root.accessNote(accessGroup.scopesNow)", group)
        self.assertIn("readonly property var scopesNow: root.heldScopes()", group)
        details = source.index('PanelSectionHeader { text: "Connection details"')
        self.assertLess(details, source.index("id: accessGroup"))
        self.assertLess(source.index("id: accessGroup"), source.index("id: healthGroup"))

    def test_each_reason_is_shown_beside_the_controls_it_explains(self):
        source = panel_source()
        host = source.split("id: hostComponent", 1)[1].split("id: displayComponent", 1)[0]
        display = source.split("id: displayComponent", 1)[1].split("id: settingsComponent", 1)[0]
        self.assertRegex(host, r"visible: root\.paired && !root\.displayAllowed\(\)\s+width: parent\.width\s+text: root\.displayReason\(\)")
        self.assertRegex(host, r"visible: root\.paired && !root\.powerAllowed\(\)\s+width: parent\.width\s+text: root\.powerReason\(\)")
        self.assertRegex(display, r"visible: root\.paired && !root\.displayAllowed\(\)\s+width: parent\.width\s+text: root\.displayReason\(\)")
        # The reason follows the control it explains: Restore, then its reason; Sunshine, then the power reason.
        self.assertLess(host.index('rowKey: "restore"'), host.index("root.displayReason()"))
        self.assertLess(host.index('rowKey: "sunshine"'), host.index("root.powerReason()"))

    def test_every_preview_button_checks_display_and_there_is_no_other_way_to_start_one(self):
        source = panel_source()
        for needle in (
            "enabled: root.displayAllowed() && !root.actionBusy && (!root.previewData || root.previewMatchesMode(modelData))",
            "enabled: root.displayAllowed() && !!root.previewData && !root.previewMutationBusy",
        ):
            self.assertEqual(1, source.count(needle), needle)
        self.assertEqual(3, source.count("enabled: root.displayAllowed() && !root.previewMutationBusy"), "Save and Revert of the active preview, and Revert beside the mode")
        self.assertEqual(1, len(re.findall(r'remote\("preview"', source)))
        self.assertEqual(1, len(re.findall(r'remote\("confirm"', source)))

    def test_every_call_that_changes_the_host_sits_in_a_function_that_checks_its_scope(self):
        source = panel_source()
        gated = {
            "confirmConfirmation": ("power.control", 'remote("power"'),
            "restartSunshine": ("power.control", 'remote("sunshine-restart"'),
            "restoreWorking": ("display.control", 'remote("restore"'),
            "previewModeFor": ("display.control", 'remote("preview"'),
            "keepPreview": ("display.control", 'remote("confirm"'),
            "revertPreview": ("display.control", 'remote("restore"'),
            "saveCurrentDisplay": ("display.control", 'remote("save-current"'),
            "resetDisplayOrder": ("display.control", 'remote("display-order-reset"'),
        }
        for name, (scope, call) in gated.items():
            body = function_source(name)
            check = "powerAllowed()" if scope == "power.control" else "displayAllowed()"
            self.assertIn(call, body, name)
            self.assertLess(body.index(check), body.index(call), f"{name} must check the scope before it sends")
        # saveDisplayOrder names its action in a variable; it is held by displayOrderCanEdit, which the control tests run.
        self.assertIn("root.displayAllowed()", function_source("displayOrderCanEdit"))
        # A new call must be classified here: it either needs a scope or it is one of these.
        unscoped = {"revoke", "forget", "wake", "discover", "pair", "pair-start", "pair-cancel", "configure"}
        literal = set(re.findall(r'\bremote\("([a-z-]+)"', source))
        self.assertEqual(literal, {"power", "sunshine-restart", "restore", "preview", "confirm", "save-current", "display-order-reset"} | unscoped)

if __name__ == "__main__":
    unittest.main()
