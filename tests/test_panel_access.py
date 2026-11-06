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
        out = run_node(cases, sources=function_sources("refresh"), stubs=["enqueueHelper", "friendlyError", "refreshDetails", "adoptScopes", "followRequest"], watch=["statusData", "connectionState"])
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
        # Preview, Save and Revert are cursor rows: the row's one expression in rowEnabled gates both the
        # control and the keyboard, and it holds the Display scope check.
        source = panel_source()
        enabled = function_source("rowEnabled")
        for needle in (
            'if (key === "preview:start") return root.displayAllowed() && !root.actionBusy',
            'if (key === "preview:keep") return root.displayAllowed() && !!root.previewData && !root.previewMutationBusy',
            'if (key === "preview:revert") return root.displayAllowed() && !!root.previewData && !root.previewMutationBusy',
        ):
            self.assertEqual(1, enabled.count(needle), needle)
        # Every control that starts, keeps or reverts a preview is such a row, never a plain Button with its own gate.
        for handler, key, count in (("previewModeFor(modelData)", "preview:start", 1), ("keepPreview()", "preview:keep", 2), ("revertPreview()", "preview:revert", 2)):
            clicks = [match.start() for match in re.finditer(rf"onClicked: root\.{re.escape(handler)}\n", source)]
            self.assertEqual(count, len(clicks), handler)
            for at in clicks:
                nearest_cursor = source.rindex("CursorButton {", 0, at)
                nearest_plain = max(match.start() for match in re.finditer(r"(?<![A-Za-z])Button \{", source[:at]))
                self.assertGreater(nearest_cursor, nearest_plain, handler)
                self.assertIn(f'rowKey: "{key}"', source[nearest_cursor:at])
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


# ---- keyboard rows, what each view renders, and the wording of wake, sleep and forget ----------------------------------

def component_source(name: str) -> str:
    """One `Component { id: name ... }` block of Panel.qml, up to the next component."""
    body = panel_source().split(f"id: {name}\n", 1)[1]
    return body.split("\n  Component {", 1)[0]


def property_expression(name: str) -> str:
    """The binding of a `readonly property` that spans a few lines, up to the next declaration."""
    found = re.search(rf"readonly property \w+ {name}: (.*?)\n(?=\n|  readonly property|  property|  function)", panel_source(), re.S)
    assert found, name
    return found.group(1)


MODES = [
    {"id": "1", "width": 1920, "height": 1080, "refresh_hz": 60},
    {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60},
    {"id": "3", "width": 1280, "height": 720, "refresh_hz": 60},
]
LISTED_PREVIEW = {"preview_id": "p1", "target_mode": {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60}}
UNLISTED_PREVIEW = {"preview_id": "p1", "target_mode": {"id": "9", "width": 800, "height": 600, "refresh_hz": 60}}
KEY_FUNCTIONS = (
    "hostRowKeys", "displayRowKeys", "settingsRowKeys", "rowKeys", "displayOrderRowKeys", "displayOrderOutputKey", "previewStartShown", "previewCardShown",
    "previewMatchesMode", "previewMatchesVisibleMode", "previewTargetIsListed", "sunshineRestartAvailable",
)


def keyed_state(scopes: Any = FULL, **changes: Any) -> dict:
    """A panel in which every optional row would be drawn."""
    state = permissive_state(
        scopes,
        visibleModeList=MODES,
        selectedModeId="2",
        selectedMode=MODES[1],
        currentOutput={"id": "1", "current_mode_id": "1", "generation": 4},
        displayOrderRows=[{"output_key": "a"}, {"output_key": "b"}],
        discoveryData={"hosts": [{"endpoint": "https://10.0.0.5:18443"}]},
        pendingPairing={"client_id": "c1"},
        modalOpen=False,
        editingPairing=False,
        cursorActive=True,
        cursorKey="",
        cursorIndex=0,
        journalStartedMs=0,
        nowMs=0,
    )
    state.update(changes)
    return state


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml key functions")
class RenderedRowTests(unittest.TestCase):
    """The keyboard may address exactly the rows a view draws: no phantom key, no unreachable control."""

    def keys(self, view: str, **changes: Any) -> list[str]:
        item = run_node([{"state": keyed_state(view=view, **changes), "calls": [["rowKeys", []]]}], sources=function_sources(*KEY_FUNCTIONS))[0]
        self.assertNotIn("error", item["results"][0], item)
        return item["results"][0]["value"]

    def drawn(self, component: str) -> tuple[set[str], set[str]]:
        text = component_source(component)
        prefixes = set(re.findall(r'rowKey: "([^"]+)" \+', text))
        literals = set(re.findall(r'rowKey: "([^"]+)"(?! \+)', text))
        return literals, prefixes

    def assert_same(self, view: str, component: str, listed: set[str]) -> None:
        literals, prefixes = self.drawn(component)
        static = {key for key in listed if not any(key.startswith(prefix) for prefix in prefixes)}
        self.assertEqual(literals, static, f"{view}: the keys the keyboard can reach and the rows the view draws differ")
        for prefix in prefixes:
            self.assertTrue(any(key.startswith(prefix) for key in listed), f"{view}: {prefix} rows are drawn but never listed")

    def test_each_view_lists_the_rows_it_draws_and_no_others(self):
        self.assert_same("host", "hostComponent", set(self.keys("host")))
        self.assert_same("settings", "settingsComponent", set(self.keys("settings")))
        # Display draws the preview controls in one of two places, depending on whether the previewed mode is listed.
        listed = set(self.keys("display")) | set(self.keys("display", previewData=LISTED_PREVIEW)) | set(self.keys("display", previewData=UNLISTED_PREVIEW))
        self.assert_same("display", "displayComponent", listed)

    def test_settings_has_no_wake_row_and_the_host_view_has_one(self):
        self.assertNotIn("wake", self.keys("settings"))
        self.assertIn("wake", self.keys("host"))
        self.assertNotIn('rowKey: "wake"', component_source("settingsComponent"))
        self.assertIn('rowKey: "wake"', component_source("hostComponent"))
        source = function_source("activateCursor").split("} else {", 1)
        self.assertNotIn('"wake"', source[-1], "the Settings branch of activateCursor has nothing to wake")
        self.assertNotIn('"wake"', function_source("rowEnabled").split("if (key === \"discover\")", 1)[1])

    def test_preview_save_and_revert_follow_the_previewed_mode(self):
        order = self.keys("display", previewData=LISTED_PREVIEW)
        at = order.index("mode:2")
        self.assertEqual(["mode:2", "preview:start", "preview:keep", "preview:revert", "mode:3"], order[at:at + 5])
        self.assertEqual(1, order.count("preview:keep"))

    def test_a_preview_whose_mode_is_not_listed_keeps_its_own_card_with_save_and_revert(self):
        order = self.keys("display", previewData=UNLISTED_PREVIEW)
        self.assertEqual(1, order.count("preview:keep"))
        self.assertEqual(1, order.count("preview:revert"))
        self.assertLess(order.index("preview:keep"), order.index("mode:1"), "the card sits above the mode rows")
        # The selected mode still draws its Preview button, but a preview is already running and it is not that mode's.
        self.assertIn("preview:start", order)
        enabled = run_node(
            [{"state": keyed_state(FULL, view="display", previewData=UNLISTED_PREVIEW), "calls": [["rowEnabled", ["preview:start"]]]}],
            sources=function_sources(*GATE_FUNCTIONS, "previewMatchesMode", "previewTargetIsListed"),
        )[0]["results"][0]
        self.assertEqual(False, enabled["value"])

    def test_preview_is_offered_beside_a_selected_mode_that_is_not_the_current_one(self):
        self.assertEqual(1, self.keys("display").count("preview:start"))
        self.assertEqual(["mode:2", "preview:start"], self.keys("display")[self.keys("display").index("mode:2"):][:2])
        current = self.keys("display", selectedModeId="1", selectedMode=MODES[0])
        self.assertNotIn("preview:start", current, "the current mode cannot be previewed")
        self.assertEqual([], [key for key in self.keys("display") if key in ("preview:keep", "preview:revert")], "nothing to save or revert without a preview")

    def activate(self, key: str, view: str, scopes: Any = FULL, **changes: Any) -> list[tuple[str, list]]:
        keys = self.keys(view, **changes)
        self.assertIn(key, keys)
        stubs = ["previewModeFor", "keepPreview", "revertPreview", "restoreWorking", "wakeHost", "openConfirmation", "restartSunshine", "moveDisplayOrder", "saveDisplayOrder", "resetDisplayOrder",
                 "refreshDisplayOrder", "cycleOutput", "openSaveCurrentConfirmation", "discoverHosts", "selectDiscoveryHost", "startPairing", "cancelPairing", "requestJournal", "syncCursorKey"]
        state = keyed_state(scopes, view=view, cursorKey=key, cursorIndex=keys.index(key), **changes)
        item = run_node([{"state": state, "calls": [["activateCursor", []]]}], sources=function_sources(*KEY_FUNCTIONS, *GATE_FUNCTIONS, "journalMayRead", "journalMinGapMs", "activateCursor"), stubs=stubs)[0]
        self.assertNotIn("error", item["results"][0], item)
        return [(name, args) for name, args in item["recorded"] if name != "syncCursorKey"]

    def test_enter_on_preview_save_and_revert_reaches_the_same_function_as_a_click(self):
        self.assertEqual([("previewModeFor", [MODES[1]])], self.activate("preview:start", "display"))
        self.assertEqual([("keepPreview", [])], self.activate("preview:keep", "display", previewData=LISTED_PREVIEW))
        self.assertEqual([("revertPreview", [])], self.activate("preview:revert", "display", previewData=LISTED_PREVIEW))
        self.assertEqual([("keepPreview", [])], self.activate("preview:keep", "display", previewData=UNLISTED_PREVIEW))
        self.assertEqual([("revertPreview", [])], self.activate("preview:revert", "display", previewData=UNLISTED_PREVIEW))

    def test_the_preview_rows_need_the_display_scope_and_a_free_lane(self):
        for scopes in (VIEW_ONLY, POWER_ONLY, []):
            for key, changes in (("preview:start", {}), ("preview:keep", {"previewData": LISTED_PREVIEW}), ("preview:revert", {"previewData": LISTED_PREVIEW})):
                with self.subTest(scopes=scopes, key=key):
                    self.assertEqual([], self.activate(key, "display", scopes, **changes))
        # Held Enter: a Save or Revert already in flight, or any other mutation queued, answers nothing.
        for key in ("preview:keep", "preview:revert"):
            self.assertEqual([], self.activate(key, "display", previewData=LISTED_PREVIEW, previewMutationBusy=True), key)
        self.assertEqual([], self.activate("preview:start", "display", actionBusy=True))
        # Save and Revert have nothing to act on once the preview is gone from under the cursor.
        enabled = run_node(
            [{"state": keyed_state(FULL, view="display"), "calls": [["rowEnabled", ["preview:keep"]], ["rowEnabled", ["preview:revert"]]]}],
            sources=function_sources(*GATE_FUNCTIONS, "previewMatchesMode", "previewTargetIsListed"),
        )[0]["results"]
        self.assertEqual([False, False], [item["value"] for item in enabled])

    def test_a_busy_lane_disables_every_row_that_changes_the_host_so_a_held_key_cannot_repeat_it(self):
        # The installed key catcher does not filter auto-repeat, so a held Enter reaches activateCursor again and again.
        # What stops the repeat is the row's own gate, which flips the moment the first press is dispatched.
        mutating = [(view, key) for view, key, _scope in ROWS if key not in ("mode:3", "output", "display-order:up:b", "display-order:down:a")]
        mutating.append(("display", "preview:start"))  # Save and Revert hold the same line with previewMutationBusy, tested above
        cases = [{"state": keyed_state(FULL, view=view, actionBusy=True), "calls": [["rowEnabled", [key]]]} for view, key in mutating]
        out = run_node(cases, sources=function_sources(*GATE_FUNCTIONS, "previewMatchesMode", "previewTargetIsListed"))
        for (view, key), item in zip(mutating, out):
            with self.subTest(row=f"{view}/{key}"):
                self.assertEqual(False, item["results"][0]["value"])

    def test_the_other_display_rows_still_fire_from_the_keyboard(self):
        self.assertEqual([("restoreWorking", [])], self.activate("restore", "display"))
        self.assertEqual([("wakeHost", [])], self.activate("wake", "host"))
        self.assertEqual([("discoverHosts", [])], self.activate("discover", "settings"))

    def test_no_row_of_settings_can_wake_the_host(self):
        for key in self.keys("settings"):
            with self.subTest(key=key):
                self.assertNotIn("wakeHost", [name for name, _args in self.activate(key, "settings")])


FOLLOW_FUNCTIONS = ("followNoteSet", "followNoteMay", "followRequest")


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml wake and sleep functions")
class FollowWordingTests(unittest.TestCase):
    """Wake and sleep claim one fact at a time: a packet sent, an authenticated host returned, Steam ready."""

    def steps(self, state: dict, steps: list[tuple[bool, bool]]) -> list[tuple[str, str, str]]:
        cases = [{"state": dict(state), "calls": [["followRequest", list(step)] for step in steps[:count]]} for count in range(1, len(steps) + 1)]
        out = run_node(cases, sources=function_sources(*FOLLOW_FUNCTIONS), watch=["actionMessage", "wakeStage", "sleepStage"])
        return [(item["state"]["actionMessage"], item["state"]["wakeStage"], item["state"]["sleepStage"]) for item in out]

    WAKE_SENT = "Wake packet sent — waiting for host"
    RETURNED = "Authenticated host response received · Steam not ready yet"
    READY = "Authenticated host response received · Steam ready"
    SLEEP = "Sleep requested; host unreachable"

    def test_a_sent_packet_is_not_a_returned_host_and_neither_is_steam_ready(self):
        sent = {"actionMessage": self.WAKE_SENT, "followNote": self.WAKE_SENT, "wakeStage": "sent", "sleepStage": ""}
        seen = self.steps(sent, [(False, False), (False, False), (True, False), (True, False), (True, True), (True, True)])
        self.assertEqual([self.WAKE_SENT, self.WAKE_SENT], [message for message, _w, _s in seen[:2]], "unreachable polls claim nothing")
        self.assertEqual([("sent", "")] * 2, [(wake, sleep) for _m, wake, sleep in seen[:2]])
        self.assertEqual((self.RETURNED, "returned"), seen[2][:2], "an authenticated answer is a fact on its own")
        self.assertEqual((self.RETURNED, "returned"), seen[3][:2], "Steam is still not ready, and nothing new is claimed")
        self.assertEqual((self.READY, ""), seen[4][:2])
        self.assertEqual((self.READY, ""), seen[5][:2], "the follower is finished and says nothing more")

    def test_a_host_that_answers_with_steam_ready_at_once_skips_the_middle_stage(self):
        sent = {"actionMessage": self.WAKE_SENT, "followNote": self.WAKE_SENT, "wakeStage": "sent", "sleepStage": ""}
        self.assertEqual([(self.READY, "")], [item[:2] for item in self.steps(sent, [(True, True)])])

    def test_a_sleep_request_then_a_dropped_connection_says_so_and_proves_nothing_more(self):
        asked = {"actionMessage": "Suspend requested; physical transition is not confirmed by the method response", "followNote": "Suspend requested; physical transition is not confirmed by the method response", "wakeStage": "", "sleepStage": "requested"}
        seen = self.steps(asked, [(True, True), (False, False), (False, False), (True, True)])
        self.assertTrue(seen[0][0].startswith("Suspend requested"), "a poll that still answers is not a disconnect")
        self.assertEqual(("requested", self.SLEEP, "unreachable"), (seen[0][2], seen[1][0], seen[1][2]))
        self.assertEqual(self.SLEEP, seen[2][0])
        self.assertEqual(("Authenticated host response received", ""), (seen[3][0], seen[3][2]))

    def test_a_line_the_owner_has_moved_on_to_is_never_overwritten(self):
        for stage, step in (({"wakeStage": "sent", "sleepStage": ""}, (True, True)), ({"wakeStage": "", "sleepStage": "requested"}, (False, False))):
            with self.subTest(stage=stage):
                state = {"actionMessage": "Restore requested; waiting for a host readback", "followNote": self.WAKE_SENT, **stage}
                seen = self.steps(state, [step])[0]
                self.assertEqual("Restore requested; waiting for a host readback", seen[0])
                self.assertNotEqual(stage, {"wakeStage": seen[1], "sleepStage": seen[2]}, "the stage still advances")

    def test_nothing_is_followed_unless_a_request_started_it(self):
        idle = {"actionMessage": "", "followNote": "", "wakeStage": "", "sleepStage": ""}
        for step in ((True, True), (False, False)):
            self.assertEqual([("", "", "")], self.steps(idle, [step]))

    def request(self, function: str, state: dict, sources: list[str], stubs: list[str], result: dict, calls: list | None = None) -> dict:
        case = {"state": state, "calls": calls or [[function, []]], "invoke": [[0, result]]}
        return run_node([case], sources=sources, stubs=stubs, watch=["actionMessage", "wakeStage", "sleepStage", "followNote"])[0]

    def test_the_wake_button_starts_the_follower_with_the_packet_fact_only(self):
        sources = function_sources("wakeHost", *FOLLOW_FUNCTIONS)
        item = self.request("wakeHost", {"wakeMac": "001122334455", "pollIntervalMs": 6000, "actionMessage": "", "wakeStage": "", "sleepStage": "unreachable", "followNote": ""}, sources, ["remote", "setView"], {"ok": True, "data": {"packet_sent": True}})
        self.assertEqual({"actionMessage": self.WAKE_SENT, "wakeStage": "sent", "sleepStage": "", "followNote": self.WAKE_SENT}, item["state"])
        failed = self.request("wakeHost", {"wakeMac": "001122334455", "actionMessage": "", "wakeStage": "", "sleepStage": "", "followNote": ""}, sources + [function_source("friendlyError")], ["remote", "setView"], {"ok": False, "error": "Wake-on-LAN packet was not sent: x", "unknown": False})
        self.assertEqual("", failed["state"]["wakeStage"], "a packet that did not leave is not followed")

    def test_a_suspend_request_starts_the_sleep_follower_and_a_restart_does_not(self):
        base = permissive_state(FULL, confirmationKind="suspend", wakeStage="sent", sleepStage="", followNote="")
        sources = function_sources(*GATE_FUNCTIONS, "confirmConfirmation", "refreshAfterAction")
        stubs = ["remote", "friendlyError", "openConfirmation"]
        item = self.request("confirmConfirmation", base, sources, stubs, {"ok": True, "data": {"operation": {"id": "op-1"}}})
        self.assertEqual(("requested", ""), (item["state"]["sleepStage"], item["state"]["wakeStage"]))
        self.assertEqual(item["state"]["actionMessage"], item["state"]["followNote"], "the next poll may replace this line")
        for kind in ("restart", "shutdown"):
            other = self.request("confirmConfirmation", {**base, "confirmationKind": kind}, sources, stubs, {"ok": True, "data": {"operation": {"id": "op-1"}}})
            self.assertEqual("", other["state"]["sleepStage"], kind)

    def test_the_follower_is_driven_by_the_status_poll_and_stops_with_the_panel(self):
        refresh = function_source("refresh")
        self.assertEqual(1, refresh.count("root.followRequest(false, false)"))
        self.assertEqual(1, refresh.count('root.followRequest(true, !!result.data && result.data.steam_bridge === "ready")'))
        closed = function_source("onClosed")
        for stage in ('wakeStage = ""', 'sleepStage = ""', 'followNote = ""'):
            self.assertIn(stage, closed)


FORGET_FUNCTIONS = ("confirmConfirmation", "forgetNotice", "friendlyError")
UNREACHABLE = {"ok": False, "error": "host connection failed: [Errno 113] No route to host", "unknown": True}
FORGOTTEN = {"ok": True, "data": {"forgotten": True}}


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml forget functions")
class ForgetTests(unittest.TestCase):
    """Forgetting a host asks it to revoke first and never leaves the host's copy behind without saying so."""

    def forget(self, kind: str, revoked: dict, local: dict = FORGOTTEN, answered: int = 2) -> dict:
        case = {"state": permissive_state(FULL, confirmationKind=kind, actionMessage=""), "calls": [["confirmConfirmation", []]], "invoke": [[0, revoked], [1, local]][:answered]}
        item = run_node([case], sources=function_sources(*FORGET_FUNCTIONS), stubs=["remote", "resetPairedState", "refreshAfterAction"], watch=["actionMessage"])[0]
        self.assertNotIn("error", item["results"][0], item)
        return item

    def calls(self, item: dict) -> list[str]:
        return [args[0] for name, args in item["recorded"] if name == "remote"]

    def test_the_host_is_asked_to_revoke_before_anything_is_forgotten_locally(self):
        waiting = self.forget("forget", {"ok": True}, answered=0)
        self.assertEqual(["revoke"], self.calls(waiting), "the local pairing is still there while the host is being asked")
        self.assertEqual([], [name for name, _args in waiting["recorded"] if name == "resetPairedState"])
        done = self.forget("forget", {"ok": True, "data": {}})
        self.assertEqual(["revoke", "forget"], self.calls(done))
        self.assertEqual(1, [name for name, _args in done["recorded"]].count("resetPairedState"))
        self.assertEqual("Credential revoked and local pairing forgotten", done["state"]["actionMessage"])

    def test_an_unreachable_host_still_lets_the_owner_forget_and_the_notice_says_what_is_left(self):
        for revoked in (UNREACHABLE, {"ok": False, "error": "host connection failed: timed out", "unknown": True}, {"ok": False, "error": "x"}):
            with self.subTest(revoked=revoked):
                item = self.forget("forget", revoked)
                self.assertEqual(["revoke", "forget"], self.calls(item))
                message = item["state"]["actionMessage"]
                self.assertRegex(message, r"^Host unreachable \(.+\), so only the local pairing was forgotten\.")
                self.assertIn("Revoke this client in Decky", message)
        self.assertIn("(No route to the host)", self.forget("forget", UNREACHABLE)["state"]["actionMessage"])

    def test_a_host_that_answered_but_refused_is_named_and_the_credential_is_not_called_revoked(self):
        for status, error in ((403, "forbidden"), (409, "mutation_conflict"), (500, "boom"), (429, "request rate limit exceeded")):
            with self.subTest(status=status):
                message = self.forget("forget", {"ok": False, "error": error, "status": status, "unknown": False})["state"]["actionMessage"]
                self.assertIn(f"The host did not accept the revocation ({error})", message)
                self.assertIn("the credential stays valid until it is revoked in Decky", message)
                self.assertNotIn("Credential revoked", message)

    def test_a_credential_the_host_no_longer_knows_leaves_nothing_to_revoke(self):
        message = self.forget("forget", {"ok": False, "error": "credential has already been revoked", "status": 401, "unknown": False})["state"]["actionMessage"]
        self.assertEqual("The host no longer accepts this credential, so there was nothing left to revoke. Local pairing forgotten.", message)

    def test_a_local_store_that_cannot_be_cleared_is_reported_in_every_outcome(self):
        broken = {"ok": False, "error": "private client state is unavailable", "unknown": False}
        for revoked in ({"ok": True}, UNREACHABLE, {"ok": False, "error": "x", "status": 409}, {"ok": False, "error": "x", "status": 401}):
            with self.subTest(revoked=revoked):
                self.assertRegex(self.forget("forget", revoked, broken)["state"]["actionMessage"], r"[Ll]ocal state needs attention")

    def test_revoke_stays_strict_and_forgets_only_what_the_host_accepted(self):
        refused = self.forget("revoke", {"ok": False, "error": "host connection failed: timed out", "unknown": True}, answered=1)
        self.assertEqual(["revoke"], self.calls(refused))
        self.assertEqual("Host did not respond in time", refused["state"]["actionMessage"])
        accepted = self.forget("revoke", {"ok": True})
        self.assertEqual(["revoke", "forget"], self.calls(accepted))
        self.assertEqual("Credential revoked and local pairing forgotten", accepted["state"]["actionMessage"])

    def test_the_confirmation_says_what_forget_does_before_it_does_it(self):
        text = function_source("confirmMessage")
        self.assertIn("Omarchy first asks the host to revoke this client's credential", text)
        self.assertIn("only the local pairing is removed and the credential stays valid until it is revoked in Decky", text)
        self.assertNotIn("Forget this local pairing", panel_source())
        self.assertIn('text: "Forget host"', component_source("settingsComponent"))


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml wording functions")
class LabelTests(unittest.TestCase):
    def test_the_display_view_restores_the_last_known_good_mode_in_the_docs_words(self):
        display = component_source("displayComponent")
        self.assertIn('text: "Restore last known-good mode"', display)
        self.assertIn("Restore last known-good mode returns to it if the current picture is wrong.", display)
        self.assertNotIn("last saved mode", panel_source())
        self.assertIn('text: "Restore last known-good display"', component_source("hostComponent"))

    def tooltip(self, **state: Any) -> str:
        script = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = input.cases.map(state => {
  const root = Object.assign({}, state);
  vm.createContext(root); root.root = root;
  vm.runInContext(input.source, root);
  root.tooltipHost = vm.runInContext(input.host, root);
  return vm.runInContext(input.tooltip, root);
});
process.stdout.write(JSON.stringify(out));
"""
        return script

    def test_the_bar_tooltip_names_the_host_the_status_and_when_it_was_last_observed(self):
        script = self.tooltip()
        cases = [
            {"paired": True, "localData": {"endpoint": "https://192.168.50.24:18443"}, "statusWord": "Ready", "lastObservation": "last observed 12:00:03"},
            {"paired": True, "localData": {"endpoint": "https://[fe80::1]:18443"}, "statusWord": "Status stale", "lastObservation": "last observed 12:00:03"},
            {"paired": True, "localData": {"endpoint": "https://deck.local:18443"}, "statusWord": "Host unreachable", "lastObservation": "not observed"},
            {"paired": True, "localData": {}, "statusWord": "Checking host", "lastObservation": "not observed"},
            {"paired": True, "localData": {"endpoint": "javascript:alert(1)"}, "statusWord": "Ready", "lastObservation": "not observed"},
            {"paired": True, "localData": {"endpoint": "https://a b:1"}, "statusWord": "Ready", "lastObservation": "not observed"},
            {"paired": False, "localData": {}, "statusWord": "Not paired", "lastObservation": "not observed"},
        ]
        host = property_expression("tooltipHost")
        run = subprocess.run(["node", "-e", script], input=json.dumps({"source": function_source("endpointHostText"), "host": host, "tooltip": property_expression("barTooltip"), "cases": cases}), capture_output=True, text=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr[-400:])
        self.assertEqual([
            "SteamOS Companion · 192.168.50.24 · Ready · last observed 12:00:03",
            "SteamOS Companion · [fe80::1] · Status stale · last observed 12:00:03",
            "SteamOS Companion · deck.local · Host unreachable · not observed",
            "SteamOS Companion · paired host · Checking host · not observed",
            "SteamOS Companion · paired host · Ready · not observed",
            "SteamOS Companion · paired host · Ready · not observed",
            "SteamOS Companion · not paired",
        ], json.loads(run.stdout))


# ---- Save current display as recovery mode (doc 21): the confirmation, the host's answers, and the assertion ----------

# The host's own answers, as Decky's `display-save-current` contract fixtures give them (the pinned bundle here
# vendors only part of the contract, so they are written out; the test below compares them with Decky's when it is present).
SAVE_ANSWERS = {
    "mutation-conflict": (409, "a display preview owns the mutation lane"),
    "stale-target": (409, "display target generation is stale"),
    "bridge-unavailable": (503, "Steam display bridge is unavailable"),
    "readback-not-confirmed": (409, "current display mode could not be read back"),
    "visible-false": (400, "visible must be true to save the current display mode"),
}
DECKY_SAVE_CURRENT = Path("/home/hvo/Projects/steamos-companion-decky/protocol/fixtures/contract/display-save-current")
SAVE_FUNCTIONS = (*GATE_FUNCTIONS, "saveCurrentDisplay", "openSaveCurrentConfirmation", "friendlyError", "confirmMessage", "confirmConfirmation", "modeLabel")


@unittest.skipUnless(shutil.which("node"), "Node is required to run the Panel.qml save-current functions")
class SaveCurrentPanelTests(unittest.TestCase):
    TARGET = {"output_id": "1", "generation": 4}

    def save(self, result: dict, **changes: Any) -> dict:
        state = permissive_state(FULL, currentOutput={"id": "1", "current_mode_id": "2", "generation": 4}, visibleModeList=[{"id": "2"}], currentSavedProfile=None, **changes)
        case = {"state": state, "calls": [["saveCurrentDisplay", [self.TARGET]]], "invoke": [[0, result]]}
        item = run_node([case], sources=function_sources(*SAVE_FUNCTIONS), stubs=["remote", "refreshAfterAction", "openConfirmation"], watch=["actionMessage", "operationData", "previewMutationBusy"])[0]
        self.assertNotIn("error", item["results"][0], item)
        return item

    def test_each_answer_of_the_host_is_worded_for_what_it_says(self):
        wanted = {
            "mutation-conflict": "A display preview is active; keep or revert it first",
            "stale-target": "The display target changed; refresh and try again",
            "bridge-unavailable": "Steam display bridge is unavailable",
            "visible-false": "Save current was rejected; the explicit confirmation did not reach the host correctly",
            # Not a changed target: the host could not read the mode back, and its own words say so.
            "readback-not-confirmed": "current display mode could not be read back",
        }
        for name, (status, message) in SAVE_ANSWERS.items():
            with self.subTest(name):
                item = self.save({"ok": False, "error": message, "status": status, "unknown": False})
                self.assertEqual(wanted[name], item["state"]["actionMessage"])
                self.assertEqual(False, item["state"]["previewMutationBusy"], "the lane is freed whatever the host said")

    def test_a_conflict_that_is_not_a_preview_is_not_called_a_changed_target(self):
        message = self.save({"ok": False, "error": "another mutation is still in progress", "status": 409, "unknown": False})["state"]["actionMessage"]
        self.assertEqual("another mutation is still in progress", message)
        message = self.save({"ok": False, "error": "output is no longer advertised", "status": 409, "unknown": False})["state"]["actionMessage"]
        self.assertEqual("The display target changed; refresh and try again", message)

    def test_a_lost_answer_is_not_a_failure_and_the_original_operation_is_checked_first(self):
        message = self.save({"ok": False, "error": "host connection failed: timed out", "unknown": True})["state"]["actionMessage"]
        self.assertEqual("Result not confirmed; reconnect and check the original operation before retrying", message)

    def test_a_saved_mode_is_reported_with_the_hosts_read_back_and_never_as_a_bare_saved(self):
        profile = {"mode": {"id": "2", "width": 1920, "height": 1080, "refresh_hz": 60}, "verified_at": "2026-09-13T12:00:00Z"}
        done = self.save({"ok": True, "data": {"operation": {"id": "op-1", "state": "succeeded"}, "profile": profile}})
        self.assertRegex(done["state"]["actionMessage"], r"^Requested\. Host read-back confirmed the recovery mode: .*verified 2026-09-13T12:00:00Z$")
        for data in ({"operation": {"id": "op-1", "state": "accepted"}}, {"operation": {"id": "op-1", "state": "succeeded"}}, {}):
            unconfirmed = self.save({"ok": True, "data": data})["state"]["actionMessage"]
            self.assertNotIn("confirmed the recovery mode", unconfirmed)
            self.assertRegex(unconfirmed, r"^Result not confirmed")
        failed = self.save({"ok": True, "data": {"operation": {"id": "op-1", "state": "failed", "reason": "no"}}})["state"]["actionMessage"]
        self.assertRegex(failed, r"^Save failed")

    def test_the_assertion_is_sent_only_from_the_confirmation_dialog(self):
        source = panel_source()
        # Opening the dialog sends nothing: it records the target and asks.
        opened = run_node(
            [{"state": permissive_state(FULL, currentOutput={"id": "1", "current_mode_id": "2", "generation": 4}), "calls": [["openSaveCurrentConfirmation", []]]}],
            sources=function_sources(*SAVE_FUNCTIONS), stubs=["remote", "refreshAfterAction", "openConfirmation"], watch=["saveCurrentTarget"],
        )[0]
        self.assertEqual([("openConfirmation", ["save-current"])], [(name, args[0:1]) for name, args in opened["recorded"]])
        self.assertEqual({"output_id": "1", "generation": 4}, opened["state"]["saveCurrentTarget"])
        # `saveCurrentDisplay` is reached from the confirmed branch only, and `visible: true` is written in exactly
        # two functions: it, and Save of a running preview (the owner has just been asked to verify that picture).
        self.assertEqual(1, source.count("root.saveCurrentDisplay("))
        self.assertIn("root.saveCurrentDisplay(target)", function_source("confirmConfirmation").split('kind === "save-current"', 1)[1].split("} else if", 1)[0])
        holders = {name for name in re.findall(r"\n  function (\w+)\(", source) if "visible: true" in function_source(name)}
        self.assertEqual({"saveCurrentDisplay", "keepPreview"}, holders)
        # And a dialog answered Cancel drops the target with nothing sent.
        self.assertRegex(function_source("cancelConfirmation"), r'root\.saveCurrentTarget = null')

    def test_the_confirmation_reads_as_decky_asks_it_with_the_paired_host_as_the_device(self):
        asked = run_node(
            [{"state": {"confirmationKind": "save-current", "saveCurrentDeviceName": "the paired SteamOS host"}, "calls": [["confirmMessage", []]]}],
            sources=function_sources("confirmMessage"),
        )[0]["results"][0]["value"]
        template = "Is the picture on {device}'s display correct right now? This is what Restore saved display will return to."
        self.assertEqual(template.replace("{device}", "the paired SteamOS host"), asked)
        self.assertIn('readonly property string saveCurrentDeviceName: "the paired SteamOS host"', panel_source())
        self.assertIn('confirmText: root.confirmationKind === "save-current" ? "Save as recovery mode" : "Confirm"', panel_source())
        if DECKY_FRONTEND.exists():
            text = DECKY_FRONTEND.read_text()
            self.assertIn("Is the picture on ${identityName()}'s display correct right now? This is what Restore saved display will return to.", text)

    @unittest.skipUnless(DECKY_SAVE_CURRENT.exists(), "the Decky checkout is not present")
    def test_the_answers_above_are_the_ones_decky_pins(self):
        for name, (status, message) in SAVE_ANSWERS.items():
            response = json.loads((DECKY_SAVE_CURRENT / f"{name}.json").read_text())["response"]
            self.assertEqual((status, message), (response["status"], response["body"]["message"]), name)

if __name__ == "__main__":
    unittest.main()
