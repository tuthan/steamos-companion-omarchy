import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.tuthan.steamoscompanion"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  readonly property string helperPath: decodeURIComponent(String(Qt.resolvedUrl("client/remote_client.py")).replace(/^file:\/\//, ""))

  // ---- client and host state -------------------------------------------
  property var localData: ({paired: false})
  // What the last status read said the host allows this pairing, or null until
  // it has said. heldScopes() prefers it to the scopes saved with the pairing.
  property var reportedScopes: null
  property var statusData: null
  property var outputsData: null
  property string outputsSignature: ""
  property var operationData: null
  property int operationPolls: 0
  property var previewData: null

  // ---- reachability, kept separate from the last action result ----------
  property string connectionState: "idle"
  property string connectionDetail: ""
  property string lastObservationAt: ""
  property real lastObservationMs: 0

  // ---- panel navigation -------------------------------------------------
  property string view: "host"
  property int cursorIndex: 0
  property string cursorKey: ""
  property bool cursorActive: false
  property string actionMessage: ""
  property string confirmationKind: ""
  property bool showAdvancedPairing: false
  property bool editingPairing: false

  // ---- pairing ----------------------------------------------------------
  property var pendingPairing: null
  property int pairingCountdownSeconds: 0
  property string pairingNotice: ""
  property string pairingFailure: ""
  property real pairingRetryUntilMs: 0
  property string pairingText: ""
  property string pairingName: "Omarchy client"
  property string pairingEndpointOverride: ""
  property bool pairingBusy: false

  // ---- discovery and saved endpoint -------------------------------------
  property string endpointText: ""
  property string discoveryPort: "18443"
  property var discoveryData: null
  property var selectedDiscoveryHost: null

  // ---- wake -------------------------------------------------------------
  property string wakeMac: ""
  property string wakeInterface: ""
  property string wakeHostInterface: ""
  // A wake or sleep request being followed through the status polls, one
  // fact per stage. wakeStage: "sent" (the packet left this machine) then
  // "returned" (an authenticated host answered, Steam not ready yet).
  // sleepStage: "requested" then "unreachable". followNote is the last line
  // this follower wrote, so it never overwrites a line the owner has moved on to.
  property string wakeStage: ""
  property string sleepStage: ""
  property string followNote: ""

  // ---- display ----------------------------------------------------------
  property string selectedOutputId: ""
  property string selectedModeId: ""
  property bool showNonstandardRefreshRates: false
  property bool previewMutationBusy: false
  property var saveCurrentTarget: null
  property int countdownSeconds: 0
  property var displayOrderData: null
  property var displayOrderLatestData: null
  property var displayOrderDraft: []
  property var displayOrderBase: []
  property string displayOrderSignature: ""
  property string displayOrderTopologySignature: ""
  property bool displayOrderDraftDirty: false
  property bool displayOrderDraftStale: false
  property string displayOrderMutation: ""
  property var displayOrderPendingKeys: []
  property bool displayOrderRefreshRequested: false
  property string displayOrderError: ""

  // ---- advisory DRM link facts -------------------------------------------
  // Read once per display interaction (Display opens, or its output topology
  // changes), never with the status poll. They only feed a warning and a
  // details group, so "no facts" is a normal state and never an error.
  property var linkFactsData: null
  property string linkFactsDataKey: ""
  property string linkFactsRequestedKey: ""
  property string linkFactsIdentity: ""
  // The facts answer for the output topology they were read under. Once that
  // has moved on the host may have renumbered its outputs, so they are unused
  // until the next read lands.
  readonly property var linkFacts: root.linkFactsDataKey !== "" && root.linkFactsDataKey === root.linkFactsTopology(root.outputList) ? root.linkFactsData : null

  // ---- host operation journal --------------------------------------------
  // Diagnostics: what the host recorded about operations. Read when Settings
  // opens and when the owner presses Refresh, never on a timer, never while the
  // panel is closed, and never twice within five seconds. Nothing is kept once
  // the panel closes or another host is paired. "idle", "loading", "ready" or
  // "unavailable"; an older host or any failure is "unavailable", never an
  // error, and no read touches the status, connection, or action lines.
  property string journalState: "idle"
  property var journalData: null
  property real journalStartedMs: 0
  property int journalEpoch: 0
  property string journalIdentity: ""

  // ---- helper lifecycle -------------------------------------------------
  property int pollGeneration: 0
  property var helperQueue: []
  property var helperJob: null
  property string helperInput: ""
  property string helperStdout: ""
  property string helperStderr: ""
  property int helperStdoutBytes: 0
  property int helperStderrBytes: 0
  property bool helperOverflow: false
  readonly property int helperStdoutLimit: 2 * 1024 * 1024
  readonly property int helperStderrLimit: 64 * 1024
  property bool helperSettled: false
  property bool helperTimedOut: false
  property bool helperStarted: false
  property bool helperSpawnFailed: false
  property int helperEscalation: 0
  property int pollIntervalMs: 2000
  // Date.now() is not a reactive dependency, so a binding that reads it never
  // re-evaluates. Staleness needs a clock that actually ticks.
  property real nowMs: Date.now()

  // Background polls run every two seconds. Gating controls on them would make
  // every button flicker disabled, so only a pending mutation disables a row.
  readonly property bool actionBusy: {
    if (root.helperJob && root.helperJob.poll !== true) return true
    for (var index = 0; index < root.helperQueue.length; index++) {
      if (root.helperQueue[index].poll !== true) return true
    }
    return false
  }
  // The device in "Is the picture on {device}'s display ...". Decky names it
  // from the device's own name; the protocol gives this client none, so it is
  // named as the panel's hero names it. steamos-companion-ctl words it the same.
  readonly property string saveCurrentDeviceName: "the paired SteamOS host"
  readonly property bool modalOpen: confirmationKind !== ""
  readonly property bool paired: localData.paired === true

  readonly property string hostLabel: paired ? "Paired SteamOS host" : "SteamOS host not paired"
  readonly property string lastObservation: lastObservationAt === "" ? "not observed" : "last observed " + lastObservationAt

  // Six distinct conditions that used to collapse into one word. "stale" is
  // the honest answer while the panel is closed: nothing polls then, so the
  // last reading ages out rather than being presented as current.
  readonly property string statusState: {
    if (!paired) return "unpaired"
    if (!statusData || !lastObservationMs) return connectionState === "unreachable" ? "offline" : "checking"
    if (connectionState === "unreachable") return "offline"
    if (nowMs - lastObservationMs > 15000) return "stale"
    return statusData.steam_bridge === "ready" ? "ready" : "bridge"
  }

  readonly property string statusWord: {
    if (statusState === "unpaired") return "Not paired"
    if (statusState === "offline") return "Host unreachable"
    if (statusState === "checking") return "Checking host"
    if (statusState === "stale") return "Status stale"
    if (statusState === "bridge") return "Steam bridge unavailable"
    return "Ready"
  }

  // The bar only asks for attention for a fact it actually observed: a paired
  // host that refused or dropped the last request. An unpaired widget is not a
  // problem to be solved, and a stale reading is an absence of knowledge, not
  // a failure — both stay quiet and render dimmed instead.
  readonly property bool statusNeedsAttention: statusState === "offline"
  readonly property bool statusUncertain: statusState === "stale" || statusState === "checking"
  // The host as the tooltip names it: the address the pairing pinned, since
  // the protocol carries no device name this client could show instead.
  function endpointHostText(endpoint) {
    var found = /^https:\/\/(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9_.-]{1,253})(?::[0-9]{1,5})?\/?$/.exec(String(endpoint || ""))
    return found ? found[1] : ""
  }
  readonly property string tooltipHost: endpointHostText(localData.endpoint) || "paired host"
  readonly property string barTooltip: paired
    ? "SteamOS Companion · " + tooltipHost + " · " + statusWord + " · " + lastObservation
    : "SteamOS Companion · not paired"

  readonly property var outputList: outputsData && outputsData.outputs ? outputsData.outputs : []
  readonly property var currentOutput: {
    for (var index = 0; index < outputList.length; index++) {
      if (outputList[index].id === root.selectedOutputId) return outputList[index]
    }
    return outputList.length > 0 ? outputList[0] : null
  }
  readonly property var currentSavedProfile: {
    var profiles = root.outputsData && Array.isArray(root.outputsData.profiles) ? root.outputsData.profiles : []
    if (!root.currentOutput) return null
    for (var index = 0; index < profiles.length; index++) {
      if (String(profiles[index].output_id) === String(root.currentOutput.id)) return profiles[index]
    }
    return null
  }
  readonly property var visibleModeList: filteredModeList()
  readonly property var selectedMode: {
    var modes = root.visibleModeList
    for (var index = 0; index < modes.length; index++) {
      if (modes[index].id === root.selectedModeId) return modes[index]
    }
    return modes.length > 0 ? modes[0] : null
  }
  readonly property var displayOrderRows: displayOrderRowsFor(root.displayOrderDraft, root.displayOrderData)

  function displayOrderBlock(data) {
    if (!data || typeof data !== "object") return null
    var nested = data.display_order || data.displayOrder || data.order
    if (nested && typeof nested === "object") return nested
    if (data.output_keys !== undefined || data.saved_output_keys !== undefined || data.restart_required !== undefined)
      return data
    return null
  }

  function displayOrderOutputKey(output) {
    if (!output || typeof output !== "object") return ""
    var value = output.output_key || output.key
    return typeof value === "string" ? value : ""
  }

  function displayOrderOutputsFor(data) {
    var block = root.displayOrderBlock(data)
    return block && Array.isArray(block.outputs) ? block.outputs : []
  }

  function displayOrderKeysFor(data) {
    var block = root.displayOrderBlock(data)
    if (!block) return []
    var keys = block.output_keys
    if (!Array.isArray(keys)) keys = block.ordered_output_keys
    if (!Array.isArray(keys)) {
      keys = []
      var outputs = root.displayOrderOutputsFor(data)
      for (var index = 0; index < outputs.length; index++) {
        var outputKey = root.displayOrderOutputKey(outputs[index])
        if (outputKey !== "") keys.push(outputKey)
      }
    }
    return keys.filter(function(value) { return typeof value === "string" && value !== "" })
  }

  function displayOrderSavedKeysFor(data) {
    var block = root.displayOrderBlock(data)
    if (!block || !Array.isArray(block.saved_output_keys)) return []
    return block.saved_output_keys.filter(function(value) { return typeof value === "string" && value !== "" })
  }

  function displayOrderOutputFor(key, data) {
    var outputs = root.displayOrderOutputsFor(data)
    for (var index = 0; index < outputs.length; index++) {
      if (root.displayOrderOutputKey(outputs[index]) === key) return outputs[index]
    }
    return null
  }

  function displayOrderConnectedKeysFor(data) {
    var connected = []
    var keys = root.displayOrderKeysFor(data)
    for (var index = 0; index < keys.length; index++) {
      var output = root.displayOrderOutputFor(keys[index], data)
      // The host must state connected=true. Missing state is not a reason to
      // guess that a stale or disconnected target can be saved.
      if (output && output.connected === true) connected.push(keys[index])
    }
    return connected
  }

  function displayOrderOrderFor(data) {
    var connectedKeys = root.displayOrderConnectedKeysFor(data)
    var savedKeys = root.displayOrderSavedKeysFor(data)
    var ordered = []
    for (var index = 0; index < savedKeys.length; index++) {
      if (connectedKeys.indexOf(savedKeys[index]) >= 0 && ordered.indexOf(savedKeys[index]) < 0)
        ordered.push(savedKeys[index])
    }
    for (var connectedIndex = 0; connectedIndex < connectedKeys.length; connectedIndex++) {
      if (ordered.indexOf(connectedKeys[connectedIndex]) < 0)
        ordered.push(connectedKeys[connectedIndex])
    }
    return ordered
  }

  function displayOrderMissingSavedKeysFor(data) {
    var savedKeys = root.displayOrderSavedKeysFor(data)
    var connectedKeys = root.displayOrderConnectedKeysFor(data)
    var missing = 0
    for (var index = 0; index < savedKeys.length; index++) {
      if (connectedKeys.indexOf(savedKeys[index]) < 0) missing++
    }
    return missing
  }

  function displayOrderRowsFor(keys, data) {
    var rows = []
    if (!Array.isArray(keys)) return rows
    for (var index = 0; index < keys.length; index++) {
      var output = root.displayOrderOutputFor(keys[index], data)
      if (output && output.connected === true) rows.push(output)
    }
    return rows
  }

  function displayOrderGenerationFor(data) {
    var block = root.displayOrderBlock(data)
    var generation = block ? block.generation : null
    return typeof generation === "number" && isFinite(generation) && Math.floor(generation) === generation ? generation : -1
  }

  function displayOrderTopologySignatureFor(data) {
    var block = root.displayOrderBlock(data)
    var records = root.displayOrderOutputsFor(data).map(function(output) {
      return [root.displayOrderOutputKey(output), output && output.connected === true, output ? output.connector || null : null]
    }).filter(function(record) { return record[0] !== "" })
    records.sort(function(left, right) { return left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0 })
    return JSON.stringify({generation: block ? block.generation : null, outputs: records})
  }

  function displayOrderSignatureFor(data) {
    var block = root.displayOrderBlock(data)
    if (!block) return ""
    return JSON.stringify({
      available: block.available,
      generation: block.generation,
      output_keys: root.displayOrderKeysFor(data),
      outputs: block.outputs,
      saved_output_keys: root.displayOrderSavedKeysFor(data),
      restart_required: block.restart_required,
      restart_available: block.restart_available,
      adapter: block.adapter,
      unsupported: block.unsupported,
      stale: block.stale,
      ambiguous: block.ambiguous,
      previous_reading: block.previous_reading,
      reason: block.reason
    })
  }

  function displayOrderArraysEqual(left, right) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false
    for (var index = 0; index < left.length; index++) {
      if (left[index] !== right[index]) return false
    }
    return true
  }

  function displayOrderAcknowledgePending(orderedKeys, data) {
    var block = root.displayOrderBlock(data)
    if (!block || block.available !== true || block.stale === true || block.ambiguous === true || block.unsupported === true)
      return
    if (root.displayOrderMutation !== "" || root.displayOrderPendingKeys.length === 0
        || (!root.displayOrderArraysEqual(orderedKeys, root.displayOrderPendingKeys)
            && !root.displayOrderArraysEqual(root.displayOrderSavedKeysFor(data), root.displayOrderPendingKeys))) return
    root.displayOrderDraftDirty = false
    root.displayOrderBase = orderedKeys.slice()
    root.displayOrderPendingKeys = []
  }

  function displayOrderStatusText(data) {
    var block = root.displayOrderBlock(data)
    if (!block) return "Loading display order…"
    if (root.displayOrderError !== "") return "Unavailable: " + root.displayOrderError
    if (block.unsupported === true) return "Unavailable: route is not supported"
    if (block.ambiguous === true) return "Ambiguous output identity: " + root.displayOrderReasonText(data)
    if (block.stale === true) return "Stale display inventory: " + root.displayOrderReasonText(data)
    if (block.previous_reading === true) return "Previous display reading: " + root.displayOrderReasonText(data)
    if (block.available !== true) return root.displayOrderReasonText(data)
    if (root.displayOrderDraftStale) return "Display topology changed; refresh before saving."
    if (root.displayOrderDraftDirty) return "Draft order has unsaved changes."
    if (root.displayOrderPendingKeys.length > 0) return "Waiting for host readback of the saved order."
    var count = root.displayOrderRows.length
    var generation = root.displayOrderGenerationFor(data)
    var missingSavedKeys = root.displayOrderMissingSavedKeysFor(data)
    if (missingSavedKeys > 0)
      return missingSavedKeys === 1
        ? "1 saved output is unavailable; showing connected outputs"
        : missingSavedKeys + " saved outputs are unavailable; showing connected outputs"
    return count + " connected " + (count === 1 ? "output" : "outputs") + (generation >= 0 ? " · generation " + generation : "")
  }

  function displayOrderReasonText(data) {
    var block = root.displayOrderBlock(data)
    if (!block) return "Waiting for a fresh host reading."
    var reason = String(block.reason || "").replace(/\s+/g, " ").trim()
    if (reason !== "") return reason.slice(0, 256)
    if (block.unsupported === true) return "The host does not expose remote display ordering."
    if (block.ambiguous === true) return "Refresh and select a host-owned output identity."
    if (block.stale === true || block.previous_reading === true) return "Refresh before changing the order."
    if (block.available !== true) return "Display order is unavailable on this host."
    return ""
  }

  function displayOrderOutputLabel(output) {
    if (!output) return "Output unavailable"
    var name = output.display_name || output.name || output.description || output.connector || root.displayOrderOutputKey(output)
    return String(name || "Output unavailable").slice(0, 160)
  }

  function displayOrderConnectorLabel(output) {
    if (!output || !output.connector) return "connector unavailable"
    return String(output.connector).slice(0, 128)
  }

  function displayOrderActiveLabel(output) {
    if (!output || output.active === null || output.active === undefined) return "Active unknown"
    return output.active === true ? "Active" : "Not active"
  }

  function displayOrderCanEdit() {
    var block = root.displayOrderBlock(root.displayOrderData)
    return root.paired && root.displayAllowed() && !!block && block.available === true && block.unsupported !== true
      && block.stale !== true && block.ambiguous !== true && !root.displayOrderDraftStale
      && root.displayOrderError === "" && !root.displayOrderRefreshRequested
      && root.displayOrderDraft.length > 0 && root.displayOrderMutation === ""
      && root.displayOrderPendingKeys.length === 0
  }

  function displayOrderCanSave() {
    return root.displayOrderCanEdit() && !root.actionBusy
  }

  function displayOrderMoveAvailable(outputKey, delta) {
    if (!root.displayOrderCanEdit()) return false
    var index = root.displayOrderDraft.indexOf(outputKey)
    var nextIndex = index + delta
    return index >= 0 && nextIndex >= 0 && nextIndex < root.displayOrderDraft.length
  }

  function displayOrderRestartAvailable() {
    var block = root.displayOrderBlock(root.displayOrderData)
    return root.displayOrderCanSave() && !!block && block.restart_available === true
  }

  // One cursor row definition reused by every actionable row, so the keyboard
  // cursor, the pointer cursor, and scroll-into-view behave identically.
  component CursorButton: Button {
    id: cursorButton
    property string rowKey: ""
    property string ownerView: ""
    leftAlign: true
    enabled: root.rowEnabled(cursorButton.rowKey)
    // The kit paints a disabled control exactly like an enabled one, so a gate
    // that only stops the click is a gate the owner cannot see.
    opacity: enabled ? 1.0 : 0.45
    hasCursor: root.cursorActive && root.view === ownerView && root.cursorKey === cursorButton.rowKey
    // Pointing at a row moves the cursor to it but does not arm it. Arming on
    // hover would mean a panel opened under a resting pointer answers Enter
    // with a host mutation the owner never aimed at.
    onHovered: function(value) {
      if (value && root.view === cursorButton.ownerView) {
        var index = root.rowIndexOf(cursorButton.rowKey)
        if (index >= 0) {
          root.cursorIndex = index
          root.cursorKey = cursorButton.rowKey
        }
      }
    }
    onHasCursorChanged: if (hasCursor) root.ensureVisible(cursorButton)
  }

  component BodyText: Text {
    textFormat: Text.PlainText
    color: Commons.Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    wrapMode: Text.WordWrap
  }

  component HintText: Text {
    textFormat: Text.PlainText
    color: Commons.Color.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  function describeError(value) {
    if (!value) return "Unavailable"
    return String(value).replace(/\s+/g, " ").slice(0, 256)
  }

  // Transport errno text is not something the owner can act on. Certificate
  // and pairing problems stay verbatim because they carry the instruction.
  function friendlyError(value) {
    // Matched against the whole message. Truncating first would hide the cause
    // whenever the host prefixes it with a long context line.
    var text = String(value || "").replace(/\s+/g, " ").trim()
    if (text === "") return "Unavailable"
    if (/certificate|pinned identity/i.test(text)) return text.slice(0, 256)
    if (/client is not paired/i.test(text)) return "Not paired"
    if (/Errno 111|Connection refused/i.test(text)) return "Host refused the connection"
    if (/Errno 113|No route to host/i.test(text)) return "No route to the host"
    if (/Errno 101|Network is unreachable/i.test(text)) return "Network is unreachable"
    if (/Errno 110|timed out|timeout/i.test(text)) return "Host did not respond in time"
    if (/python3|helper/i.test(text)) return text.slice(0, 256)
    return text.slice(0, 256)
  }

  function ensureVisible(item) {
    if (!item || !flick.interactive) return
    var point = item.mapToItem(mainColumn, 0, 0)
    if (!point) return
    var margin = Style.space(8)
    var top = point.y - margin
    var bottom = point.y + item.height + margin
    if (top < flick.contentY) {
      flick.contentY = Math.max(0, top)
    } else if (bottom > flick.contentY + flick.height) {
      flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, bottom - flick.height))
    }
  }

  // ---- helper queue ------------------------------------------------------

  function enqueueHelper(action, args, callback, poll) {
    var request = {action: action}
    for (var key in (args || {})) request[key] = args[key]
    var job = {request: request, callback: callback, poll: poll === true, generation: root.pollGeneration}
    job.signature = JSON.stringify(request)
    var next = root.helperQueue.slice()
    if (job.poll) {
      // Opening the panel, the interval timer, and a view switch can all ask
      // for the same reading. Queueing it three times only delays the answer.
      // A job from an earlier generation is never a substitute: its result is
      // discarded on arrival, so deduplicating against it would lose the read.
      // The whole request is the identity, not the action name: two operation
      // polls differ only by the operation they name, and collapsing them
      // would drop the second operation's result on the floor.
      for (var index = 0; index < next.length; index++) {
        if (next[index].poll && next[index].signature === job.signature && next[index].generation === root.pollGeneration) return
      }
      if (root.helperJob && root.helperJob.poll && root.helperJob.signature === job.signature
          && root.helperJob.generation === root.pollGeneration) return
      next.push(job)
    } else {
      // Mutations must not sit behind a backlog of background status polls;
      // a display preview has a short host-owned deadline.
      var firstPoll = next.findIndex(function(item) { return item.poll === true })
      if (firstPoll < 0) next.push(job)
      else next.splice(firstPoll, 0, job)
    }
    if (next.length > 12) {
      // Background reads are disposable; the newest one carries the same
      // answer. A mutation is not: its callback owns a busy flag, and dropping
      // it silently would leave the control that set the flag disabled with
      // nothing left to clear it. So the trim only ever spends poll jobs.
      var kept = []
      var budget = next.length - 12
      for (var trim = 0; trim < next.length; trim++) {
        if (budget > 0 && next[trim].poll === true && next[trim] !== job) { budget--; continue }
        kept.push(next[trim])
      }
      next = kept
    }
    root.helperQueue = next
    root.pumpHelper()
  }

  function pumpHelper() {
    if (helper.running || root.helperQueue.length === 0) return
    var next = root.helperQueue[0]
    root.helperQueue = root.helperQueue.slice(1)
    if (next.poll && next.generation !== root.pollGeneration) {
      Qt.callLater(root.pumpHelper)
      return
    }
    root.helperJob = next
    root.helperInput = JSON.stringify(next.request)
    root.helperStdout = ""
    root.helperStderr = ""
    root.helperStdoutBytes = 0
    root.helperStderrBytes = 0
    root.helperOverflow = false
    root.helperSettled = false
    root.helperTimedOut = false
    root.helperStarted = false
    root.helperSpawnFailed = false
    root.helperEscalation = 0
    helperWatchdog.interval = 30000
    helper.running = true
    helperWatchdog.restart()
  }

  // Consume chunks immediately, including output without any newline. Count
  // UTF-8 bytes conservatively (a split surrogate pair costs at most 6 bytes).
  function collectHelper(chunk, stderr) {
    if (root.helperOverflow || root.helperSettled) return
    var used = stderr ? root.helperStderrBytes : root.helperStdoutBytes
    var limit = stderr ? root.helperStderrLimit : root.helperStdoutLimit
    var size = chunk.length
    if (size <= limit - used) {
      size = 0
      for (var i = 0; i < chunk.length && size <= limit - used; i++) {
        var code = chunk.charCodeAt(i)
        size += code < 128 ? 1 : code < 2048 ? 2 : 3
      }
    }
    if (size > limit - used) {
      root.helperOverflow = true
      root.helperStdout = ""
      root.helperStderr = ""
      helper.signal(9)
      return
    }
    if (stderr) {
      root.helperStderrBytes += size
      root.helperStderr += chunk
    } else {
      root.helperStdoutBytes += size
      root.helperStdout += chunk
    }
  }

  // Settling on exit rather than on the stdout stream means stderr is already
  // complete, so a helper that failed to start can say why.
  function settleHelper() {
    if (!root.helperJob || root.helperSettled) return
    root.helperSettled = true
    helperWatchdog.stop()
    var raw = String(root.helperStdout || "").trim()
    var detail = String(root.helperStderr || "").replace(/\s+/g, " ").trim()
    var result
    try {
      if (root.helperOverflow || root.helperTimedOut) throw new Error("helper was stopped")
      result = JSON.parse(raw)
      if (!result || typeof result !== "object") throw new Error("helper response is invalid")
    } catch (error) {
      if (root.helperOverflow) {
        result = {ok: false, error: "Client helper exceeded its output limit and was stopped", unknown: true}
      } else if (root.helperTimedOut) {
        result = {ok: false, error: "Client helper did not finish in time and was stopped", unknown: true}
      } else if (root.helperSpawnFailed) {
        // The process never started, so there is no stderr to quote. This is
        // almost always a missing interpreter.
        result = {ok: false, error: "python3 could not be started; install python3 to use SteamOS Companion", unknown: false}
      } else {
        if (/No such file|not found|command not found/i.test(detail))
          detail = "python3 was not found; install python3 to use SteamOS Companion"
        result = {
          ok: false,
          error: detail ? "Client helper failed: " + detail.slice(0, 200) : "Client helper returned no usable response",
          unknown: false
        }
      }
    }
    var job = root.helperJob
    root.helperJob = null
    if (!(job.poll && job.generation !== root.pollGeneration)) {
      try { job.callback(result) } catch (error) { root.actionMessage = describeError(error) }
    }
    Qt.callLater(root.pumpHelper)
  }

  Process {
    id: helper
    command: ["python3", root.helperPath]
    stdinEnabled: true
    stdout: SplitParser { splitMarker: ""; onRead: data => root.collectHelper(data, false) }
    stderr: SplitParser { splitMarker: ""; onRead: data => root.collectHelper(data, true) }
    onStarted: {
      root.helperStarted = true
      write(root.helperInput + "\n")
    }
    onExited: root.settleHelper()
    // A command that cannot be executed emits neither started nor exited: the
    // process flips running straight back to false. Without this the job would
    // never settle, the single lane would stay claimed, and every gated control
    // would be disabled for the life of the shell with nothing said.
    onRunningChanged: if (!running) Qt.callLater(root.reapHelper)
  }

  // Runs after the process has already had its chance to report an exit. If a
  // job is still claimed at this point, the spawn itself failed.
  function reapHelper() {
    if (helper.running || !root.helperJob || root.helperSettled) return
    if (!root.helperStarted) root.helperSpawnFailed = true
    root.settleHelper()
  }

  // Every helper action is bounded on the Python side. If one wedges anyway,
  // the single lane must not stay blocked for the life of the shell. A polite
  // request is not always honoured, so the escalation is termination, then
  // kill, then giving up on the process and freeing the controls regardless.
  Timer {
    id: helperWatchdog
    interval: 30000
    repeat: false
    onTriggered: {
      if (!root.helperJob || root.helperSettled) return
      root.helperTimedOut = true
      root.helperEscalation++
      if (root.helperEscalation === 1) {
        helper.running = false
        interval = 3000
        restart()
      } else if (root.helperEscalation === 2) {
        helper.signal(9)
        interval = 3000
        restart()
      } else {
        // Unkillable. The lane stays occupied until the kernel releases it,
        // but the owner gets an answer and the controls come back.
        root.settleHelper()
      }
    }
  }

  function remote(action, args, callback) {
    enqueueHelper(action, args || {}, function(result) {
      // The owner changed this pairing since the last status read. The helper has
      // already saved the new scopes; read status now so the controls follow them.
      if (result && result.ok === false && result.status === 403) root.refresh()
      if (callback) callback(result)
    }, false)
  }

  // ---- polling -----------------------------------------------------------

  function localInspect() {
    enqueueHelper("inspect", {}, function(result) {
      if (!result.ok) {
        root.localData = {paired: false}
        root.reportedScopes = null
        root.syncLinkFactsIdentity()
        root.syncJournalIdentity()
        root.actionMessage = friendlyError(result.error)
        return
      }
      root.localData = result.data || {paired: false}
      root.reportedScopes = null
      root.syncLinkFactsIdentity()
      root.syncJournalIdentity()
      root.pendingPairing = root.localData.pending_pairing || null
      if (root.pendingPairing) root.pairingCountdownSeconds = root.pendingPairing.seconds_remaining || 0
      root.endpointText = root.localData.endpoint || ""
      root.wakeMac = root.localData.wake_mac || ""
      root.wakeInterface = root.localData.wake_interface || ""
      root.wakeHostInterface = root.localData.wake_host_interface || ""
      root.showNonstandardRefreshRates = root.localData.show_nonstandard_refresh_rates === true
      var pendingOperation = root.localData.pending_operation
      if (pendingOperation && pendingOperation.id
          && (!root.operationData || root.operationData.id !== pendingOperation.id)) {
        root.operationData = pendingOperation
        root.operationPolls = 0
      }
      if (pendingOperation && pendingOperation.kind === "display-order-restart" && root.displayOrderMutation === "")
        root.displayOrderMutation = "restart"
      else if (pendingOperation && pendingOperation.kind === "display-order-save" && root.displayOrderMutation === "")
        root.displayOrderMutation = "save"
      else if (pendingOperation && pendingOperation.kind === "display-order-reset" && root.displayOrderMutation === "")
        root.displayOrderMutation = "reset"
      if (pendingOperation && Array.isArray(pendingOperation.output_keys))
        root.displayOrderPendingKeys = pendingOperation.output_keys.slice()
      if (root.opened && root.paired) root.refresh()
    }, false)
  }

  function refresh() {
    if (!root.opened || !root.paired) return
    // Status decides reachability. Nothing else is worth asking for until it
    // answers, so an offline host costs one short request per cycle.
    enqueueHelper("status", {timeout: 2.5}, function(result) {
      if (!result.ok) {
        root.statusData = null
        root.connectionState = "unreachable"
        root.connectionDetail = friendlyError(result.error)
        root.pollIntervalMs = Math.min(6000, Math.max(2000, root.pollIntervalMs * 2))
        root.followRequest(false, false)
        return
      }
      root.connectionState = "ok"
      root.connectionDetail = ""
      root.statusData = result.data
      root.followRequest(true, !!result.data && result.data.steam_bridge === "ready")
      root.adoptScopes(result.data)
      var advertisedWake = result.data && result.data.wake_target
      if (advertisedWake && advertisedWake.mac) {
        root.wakeMac = advertisedWake.mac
        root.wakeHostInterface = advertisedWake.interface || ""
      }
      root.lastObservationMs = Date.now()
      root.lastObservationAt = Qt.formatDateTime(new Date(), "HH:mm:ss")
      root.pollIntervalMs = 2000
      root.refreshDetails()
    }, true)
  }

  function refreshDetails() {
    if (!root.opened || !root.paired) return
    // The mode inventory only matters while the Display view is open or a
    // host-owned preview is still running.
    if (root.view === "display" || root.previewData) {
      enqueueHelper("outputs", {timeout: 2.5}, function(result) {
        if (!result.ok) return
        root.applyOutputs(result.data)
      }, true)
    }
    if (root.view === "display") {
      enqueueHelper("display-order", {timeout: 2.5}, function(result) {
        if (!result.ok) {
          // A 404 on an older host is an additive capability miss. Keep the
          // existing resolution controls usable and label only this section.
          root.applyDisplayOrderFailure(result.error)
          root.displayOrderRefreshRequested = false
          return
        }
        root.applyDisplayOrder(result.data, root.displayOrderRefreshRequested)
        root.displayOrderRefreshRequested = false
      }, true)
    }
    if (root.operationData && root.operationData.id && !root.operationIsSettled()) {
      var operationId = root.operationData.id
      enqueueHelper("operation", {operation_id: operationId, timeout: 2.5}, function(result) {
        if (!result.ok || !result.data || !result.data.operation) return
        // A second action can start while this poll is in flight. Its answer
        // describes an operation nobody is watching any more.
        if (!root.operationData || root.operationData.id !== operationId) return
        root.operationPolls++
        root.operationData = result.data.operation
        var state = result.data.operation.state
        if (root.settleDisplayOrderOperation(operationId, result.data.operation)) return
        if (["failed", "unknown"].indexOf(state) >= 0) {
          root.actionMessage = result.data.operation.reason || "Operation result is unavailable"
        } else if (["succeeded", "observed_return"].indexOf(state) >= 0 && root.actionMessage === "Requested; waiting for host observation") {
          root.actionMessage = result.data.operation.outcome || "Host observed the request"
        }
      }, true)
    }
  }

  function settleDisplayOrderOperation(operationId, operation) {
    if (root.displayOrderMutation === "" || !operation) return false
    var state = operation.state
    if (["succeeded", "failed", "unknown"].indexOf(state) < 0) return false
    var mutation = root.displayOrderMutation
    var orderOutcome = String(operation.outcome || "")
    var orderWasSaved = operation.order_saved === true
      || operation.saved === true
      || (/(?:^|[\s_-])(?:configured|saved)(?:$|[\s_-])/i.test(orderOutcome)
          && !/(?:^|[\s_-])not[\s_-]+(?:configured|saved)(?:$|[\s_-])/i.test(orderOutcome))
    if (state === "unknown") {
      root.actionMessage = "Display order result is unknown; reconnecting will reconcile operation " + operationId
      if (root.operationPolls > 30) root.displayOrderMutation = ""
    } else if (state === "failed") {
      var orderSavedDespiteRestartFailure = false
      if (mutation === "restart" && orderWasSaved) {
        root.actionMessage = "Display order remains saved, but Gaming Mode restart failed: "
          + (operation.reason || "restart result unavailable")
        root.displayOrderDraftDirty = false
        root.displayOrderBase = root.displayOrderPendingKeys.slice()
        orderSavedDespiteRestartFailure = root.displayOrderPendingKeys.length > 0
      } else {
        root.actionMessage = operation.reason || (mutation === "reset"
          ? "Automatic display-order reset failed"
          : "Display order save failed")
      }
      if (!orderSavedDespiteRestartFailure) root.displayOrderPendingKeys = []
      root.displayOrderMutation = ""
    } else {
      root.actionMessage = mutation === "restart"
        ? "Display order saved; Gaming Mode restart requested. Reconnect and verify the active screen."
        : mutation === "reset"
          ? "Automatic display order restored."
          : "Display order saved for the next Gaming Mode session."
      if (mutation === "save" || mutation === "restart") {
        root.displayOrderDraftDirty = false
        if (root.displayOrderPendingKeys.length > 0)
          root.displayOrderBase = root.displayOrderPendingKeys.slice()
      }
      if (mutation === "reset") {
        root.displayOrderDraftDirty = false
        root.displayOrderDraftStale = false
        root.displayOrderBase = []
        root.displayOrderPendingKeys = []
        root.displayOrderRefreshRequested = true
      }
      root.displayOrderMutation = ""
    }
    return true
  }

  function operationIsSettled() {
    if (!root.operationData) return true
    if (root.operationPolls > 30) return true
    return ["succeeded", "failed"].indexOf(root.operationData.state) >= 0
  }

  function applyOutputs(data) {
    // Re-assigning an equivalent payload every two seconds destroys and
    // rebuilds every mode delegate, which loses hover and flickers the list.
    // The preview block carries a deadline that moves every poll. Including it
    // would make the signature differ every two seconds, rebuilding every mode
    // delegate and losing hover and selection with it. Preview is mirrored
    // separately, so the inventory alone decides whether anything changed.
    var signature = JSON.stringify({outputs: data ? data.outputs : null, profiles: data ? data.profiles : null, reason: data ? data.reason : null})
    root.previewData = data && data.preview ? data.preview : null
    if (signature === root.outputsSignature) return
    root.outputsSignature = signature
    root.outputsData = data
    root.clampDisplaySelection()
    // Only a changed inventory can change the topology the link facts answer
    // for, so this is the one place a poll can lead to a new read, and it does
    // so only when the topology key differs from the last read's.
    root.requestLinkFacts()
  }

  function applyDisplayOrderFailure(error) {
    var previous = root.displayOrderData
    if (previous && root.displayOrderBlock(previous)) {
      root.displayOrderLatestData = null
      root.displayOrderError = root.friendlyError(error)
      root.actionMessage = "Display order could not be refreshed: " + root.friendlyError(error)
      return
    }
    root.displayOrderData = {
      protocol_version: 1,
      display_order: {
        available: false,
        generation: null,
        observed_at: null,
        output_keys: [],
        outputs: [],
        saved_output_keys: [],
        restart_required: false,
        restart_available: false,
        adapter: null,
        unsupported: true,
        stale: false,
        ambiguous: false,
        previous_reading: false,
        reason: root.friendlyError(error)
      }
    }
    root.displayOrderError = root.friendlyError(error)
    root.displayOrderDraft = []
    root.displayOrderBase = []
    root.displayOrderDraftDirty = false
    root.displayOrderDraftStale = false
  }

  function applyDisplayOrder(data, force) {
    var block = root.displayOrderBlock(data)
    if (!block) {
      root.applyDisplayOrderFailure("host returned no display-order resource")
      return
    }
    var orderedKeys = root.displayOrderOrderFor(data)
    var topology = root.displayOrderTopologySignatureFor(data)
    var hasReading = !!root.displayOrderData && !!root.displayOrderBlock(root.displayOrderData)
    var topologyChanged = hasReading && topology !== root.displayOrderTopologySignature
    if (force === true || !hasReading) {
      root.displayOrderData = data
      root.displayOrderLatestData = null
      root.displayOrderError = ""
      root.displayOrderSignature = root.displayOrderSignatureFor(data)
      root.displayOrderTopologySignature = topology
      root.displayOrderDraft = orderedKeys
      root.displayOrderBase = orderedKeys.slice()
      root.displayOrderDraftDirty = false
      root.displayOrderDraftStale = block.stale === true || block.ambiguous === true || block.unsupported === true
      root.displayOrderAcknowledgePending(orderedKeys, data)
      return
    }
    if (topologyChanged) {
      // Never merge a new host topology into an order the owner may be editing.
      // Keep the old rows and cursor stable until Refresh explicitly accepts it.
      root.displayOrderLatestData = data
      root.displayOrderError = ""
      root.displayOrderDraftStale = true
      return
    }
    var signature = root.displayOrderSignatureFor(data)
    root.displayOrderAcknowledgePending(orderedKeys, data)
    if (signature === root.displayOrderSignature) {
      root.displayOrderError = ""
      return
    }
    root.displayOrderData = data
    root.displayOrderLatestData = null
    root.displayOrderError = ""
    root.displayOrderSignature = signature
    root.displayOrderTopologySignature = topology
    if (!root.displayOrderDraftDirty && root.displayOrderMutation === ""
        && root.displayOrderPendingKeys.length === 0) {
      root.displayOrderDraft = orderedKeys
      root.displayOrderBase = orderedKeys.slice()
    }
    if (block.stale === true || block.ambiguous === true || block.unsupported === true)
      root.displayOrderDraftStale = true
  }

  // Wake and sleep are claims about three different things: a packet leaving
  // this machine, an authenticated answer from the pinned host, and Steam being
  // ready. None implies the next, and a dropped connection after a sleep request
  // is not proof of sleep. Each poll can advance one stage and says only what
  // that poll showed.
  function followNoteSet(text) {
    root.actionMessage = text
    root.followNote = text
  }

  function followNoteMay() {
    return root.actionMessage === "" || root.actionMessage === root.followNote
  }

  function followRequest(reachable, steamReady) {
    if (root.wakeStage === "sent" && reachable) {
      root.wakeStage = steamReady ? "" : "returned"
      if (root.followNoteMay())
        root.followNoteSet(steamReady
          ? "Authenticated host response received · Steam ready"
          : "Authenticated host response received · Steam not ready yet")
    } else if (root.wakeStage === "returned" && reachable && steamReady) {
      root.wakeStage = ""
      if (root.followNoteMay()) root.followNoteSet("Authenticated host response received · Steam ready")
    }
    if (root.sleepStage === "requested" && !reachable) {
      root.sleepStage = "unreachable"
      if (root.followNoteMay()) root.followNoteSet("Sleep requested; host unreachable")
    } else if (root.sleepStage === "unreachable" && reachable) {
      root.sleepStage = ""
      if (root.followNoteMay()) root.followNoteSet("Authenticated host response received")
    }
  }

  function refreshAfterAction(message) {
    root.actionMessage = message || "Requested; waiting for host observation"
    root.operationPolls = 0
    // An action is exactly when the owner is watching, so drop any backoff.
    root.pollIntervalMs = 2000
    if (root.opened) Qt.callLater(root.refresh)
  }

  // ---- views and cursor --------------------------------------------------

  function setView(next) {
    if (["host", "display", "settings"].indexOf(next) < 0) return
    root.view = next
    root.cursorIndex = 0
    root.cursorKey = ""
    root.cursorActive = false
    flick.contentY = 0
    if (next === "display") {
      root.refresh()
      // A new display interaction: read the link facts again for it.
      root.linkFactsRequestedKey = ""
      root.requestLinkFacts()
    } else if (next === "settings" && root.paired) {
      root.refreshConnectionDetails()
      // Entering the view that holds the journal is one of its two read points.
      root.requestJournal()
    }
  }

  function viewByDelta(delta) {
    var views = ["host", "display", "settings"]
    var index = views.indexOf(root.view)
    setView(views[(index + delta + views.length) % views.length])
  }

  // Rows are addressed by a stable key rather than by a position. Positions
  // move underneath the owner constantly — a Cancel row appears when a pairing
  // request opens, a Sunshine row appears when Sunshine stops, a re-scan empties
  // the discovered host list — and an index captured before one of those events
  // names a different action afterwards. Keying the cursor means the worst case
  // is that the row it was on disappeared, which is detectable.
  function hostRowKeys() {
    var keys = ["restore", "wake", "suspend", "restart", "shutdown"]
    if (root.sunshineRestartAvailable()) keys.push("sunshine")
    return keys
  }

  function displayOrderRowKeys() {
    var keys = []
    var rows = root.displayOrderRows
    for (var index = 0; index < rows.length; index++) {
      var outputKey = root.displayOrderOutputKey(rows[index])
      if (outputKey === "") continue
      keys.push("display-order:up:" + outputKey)
      keys.push("display-order:down:" + outputKey)
    }
    keys.push("display-order:save")
    keys.push("display-order:restart")
    keys.push("display-order:automatic")
    keys.push("display-order:refresh")
    return keys
  }

  // Which of the three preview controls are on screen. The key list and the
  // rows both ask these, so a control can never be listed that is not drawn
  // (the keyboard would fire it blind) or drawn that is not listed (the
  // keyboard could never reach it).
  //
  // Preview sits beside the selected mode unless that mode is the current one;
  // the exception is the running preview's own mode, which reads "Preview Ns".
  function previewStartShown(mode) {
    if (!mode || root.selectedModeId !== mode.id) return false
    var isCurrent = !!root.currentOutput && mode.id === root.currentOutput.current_mode_id
    return !isCurrent || root.previewMatchesMode(mode)
  }

  // Save and Revert follow the previewed mode when it is in the list, and
  // otherwise sit in their own card while the mode rows are still updating.
  function previewCardShown() {
    return !!root.previewData && !root.previewMatchesVisibleMode()
  }

  function displayRowKeys() {
    var keys = root.displayOrderRowKeys()
    if (root.outputList.length > 1) keys.push("output")
    keys.push("save-current")
    if (root.previewCardShown()) keys.push("preview:keep", "preview:revert")
    var modes = root.visibleModeList
    for (var index = 0; index < modes.length; index++) {
      keys.push("mode:" + modes[index].id)
      if (root.previewStartShown(modes[index])) keys.push("preview:start")
      if (root.previewMatchesMode(modes[index])) keys.push("preview:keep", "preview:revert")
    }
    keys.push("restore")
    return keys
  }

  function settingsRowKeys() {
    var keys = ["discover"]
    var hosts = root.discoveryData && root.discoveryData.hosts ? root.discoveryData.hosts : []
    for (var index = 0; index < hosts.length; index++) keys.push("host:" + hosts[index].endpoint)
    keys.push("pair")
    if (root.pendingPairing) keys.push("cancel")
    if (root.paired) keys.push("journal")
    keys.push("revoke")
    keys.push("forget")
    return keys
  }

  function rowKeys() {
    if (view === "display") return displayRowKeys()
    if (view === "settings") return settingsRowKeys()
    return hostRowKeys()
  }

  function rowIndexOf(key) { return root.rowKeys().indexOf(key) }
  function rowCount() { return rowKeys().length }

  // One expression per row, read by both the control's enabled state and the
  // keyboard activation path. Two separate expressions would eventually
  // disagree, and the disagreement is always in the dangerous direction: a
  // control that looks unavailable but still fires.
  function rowEnabled(key) {
    if (!key) return false
    if (view === "host") {
      if (key === "restore") return root.canRestoreWorking() && root.displayAllowed() && !root.actionBusy
      if (key === "wake") return root.paired && !!root.wakeMac && !root.actionBusy
      if (key === "suspend") return root.canSuspendHost() && root.powerAllowed() && !root.actionBusy
      if (key === "restart") return root.paired && root.powerAllowed() && root.powerAvailable("restart") && !root.actionBusy
      if (key === "shutdown") return root.paired && root.powerAllowed() && root.powerAvailable("shutdown") && !root.actionBusy
      if (key === "sunshine") return root.sunshineRestartAvailable() && root.powerAllowed() && !root.actionBusy
      return false
    }
    if (view === "display") {
      if (key.indexOf("display-order:up:") === 0)
        return root.displayOrderMoveAvailable(key.slice("display-order:up:".length), -1)
      if (key.indexOf("display-order:down:") === 0)
        return root.displayOrderMoveAvailable(key.slice("display-order:down:".length), 1)
      if (key === "display-order:save") return root.displayOrderCanSave()
      if (key === "display-order:restart") return root.displayOrderRestartAvailable()
      if (key === "display-order:automatic") {
        var orderBlock = root.displayOrderBlock(root.displayOrderData)
        return root.paired && root.displayAllowed() && !root.actionBusy && root.displayOrderMutation === ""
          && !root.displayOrderRefreshRequested && root.displayOrderPendingKeys.length === 0 && !!orderBlock
          && (orderBlock.available === true || root.displayOrderSavedKeysFor(root.displayOrderData).length > 0)
      }
      if (key === "display-order:refresh") return root.paired && !root.actionBusy
        && root.displayOrderMutation === "" && !root.displayOrderRefreshRequested
      if (key === "output") return root.outputList.length > 1 && !root.previewData
      if (key === "save-current") return root.paired && root.displayAllowed() && !!root.currentOutput
        && !!root.currentOutput.current_mode_id && !root.previewData
        && !root.previewMutationBusy && !root.actionBusy
      if (key === "restore") return root.paired && root.displayAllowed() && !root.previewData && !root.actionBusy
      if (key === "preview:start") return root.displayAllowed() && !root.actionBusy
        && (!root.previewData || root.previewMatchesMode(root.selectedMode))
      if (key === "preview:keep") return root.displayAllowed() && !!root.previewData && !root.previewMutationBusy
      if (key === "preview:revert") return root.displayAllowed() && !!root.previewData && !root.previewMutationBusy
      return key.indexOf("mode:") === 0
    }
    if (key === "discover") return !root.actionBusy
    if (key.indexOf("host:") === 0) return !root.actionBusy
    if (key === "pair") return !root.actionBusy && !root.pendingPairing && !root.pairingBusy
    if (key === "cancel") return !!root.pendingPairing
    if (key === "journal") return root.paired && root.journalMayRead(root.journalStartedMs, root.nowMs)
    if (key === "revoke") return root.paired && !root.actionBusy
    if (key === "forget") return root.paired && !root.actionBusy
    return false
  }

  function setCursor(index) {
    var keys = rowKeys()
    if (keys.length === 0) { root.cursorIndex = 0; root.cursorKey = ""; return }
    var next = Math.max(0, Math.min(keys.length - 1, index))
    root.cursorIndex = next
    root.cursorKey = keys[next]
  }

  // Re-anchors the cursor after the row list changes shape. If the row the
  // cursor named still exists it simply moves with it. If it is gone, the
  // cursor is disarmed: whatever now occupies that position is not something
  // the owner chose, and Enter must not fire it.
  function syncCursorKey() {
    var keys = rowKeys()
    if (keys.length === 0) return
    if (root.cursorKey !== "") {
      var found = keys.indexOf(root.cursorKey)
      if (found >= 0) {
        if (root.cursorIndex !== found) root.cursorIndex = found
        return
      }
    }
    root.cursorIndex = Math.max(0, Math.min(keys.length - 1, root.cursorIndex))
    root.cursorKey = keys[root.cursorIndex]
    root.cursorActive = false
  }

  readonly property var cursorRowKeys: rowKeys()
  onCursorRowKeysChanged: syncCursorKey()

  function moveCursor(delta) {
    var count = rowCount()
    if (count <= 0) return
    if (!cursorActive) {
      // The first key press only reveals the cursor; it never fires a row.
      cursorActive = true
      setCursor(cursorIndex)
      return
    }
    setCursor(cursorIndex + delta)
  }

  function activateCursor() {
    if (modalOpen) {
      if (confirmDialog.selectedIndex === 0) cancelConfirmation()
      else confirmConfirmation()
      return
    }
    if (editingPairing) return
    // Enter on a panel that was just opened must not fire a host mutation.
    if (!cursorActive) return
    var keys = rowKeys()
    var key = keys[cursorIndex]
    // The row moved between the last repaint and this key press. Refuse rather
    // than fire whatever landed under the cursor.
    if (!key || key !== root.cursorKey) { syncCursorKey(); return }
    if (!rowEnabled(key)) return
    if (view === "host") {
      if (key === "restore") restoreWorking()
      else if (key === "wake") wakeHost()
      else if (key === "suspend") openConfirmation("suspend")
      else if (key === "restart") openConfirmation("restart")
      else if (key === "shutdown") openConfirmation("shutdown")
      else if (key === "sunshine") restartSunshine()
    } else if (view === "display") {
      if (key.indexOf("display-order:up:") === 0) root.moveDisplayOrder(key.slice("display-order:up:".length), -1)
      else if (key.indexOf("display-order:down:") === 0) root.moveDisplayOrder(key.slice("display-order:down:".length), 1)
      else if (key === "display-order:save") root.saveDisplayOrder(false)
      else if (key === "display-order:restart") root.openConfirmation("display-order-restart")
      else if (key === "display-order:automatic") root.resetDisplayOrder()
      else if (key === "display-order:refresh") root.refreshDisplayOrder()
      else if (key === "output") cycleOutput()
      else if (key === "save-current") openSaveCurrentConfirmation()
      else if (key === "restore") restoreWorking()
      else if (key === "preview:start") root.previewModeFor(root.selectedMode)
      else if (key === "preview:keep") root.keepPreview()
      else if (key === "preview:revert") root.revertPreview()
      else if (key.indexOf("mode:") === 0) root.selectedModeId = key.slice(5)
    } else {
      if (key === "discover") discoverHosts()
      else if (key.indexOf("host:") === 0) {
        var hosts = root.discoveryData && root.discoveryData.hosts ? root.discoveryData.hosts : []
        var wanted = key.slice(5)
        for (var index = 0; index < hosts.length; index++) {
          if (String(hosts[index].endpoint) === wanted) { selectDiscoveryHost(hosts[index]); break }
        }
      }
      else if (key === "pair") startPairing()
      else if (key === "cancel") cancelPairing()
      else if (key === "journal") root.requestJournal()
      else if (key === "revoke") openConfirmation("revoke")
      else if (key === "forget") openConfirmation("forget")
    }
  }

  // ---- host actions ------------------------------------------------------

  function openConfirmation(kind) {
    confirmationKind = kind
    confirmDialog.selectedIndex = 0
  }

  function cancelConfirmation() {
    confirmationKind = ""
    root.saveCurrentTarget = null
  }

  function confirmMessage() {
    if (confirmationKind === "suspend") return "Suspend the exact paired SteamOS host? The session will be interrupted and wake uses the automatically detected LAN path."
    if (confirmationKind === "restart") return "Restart the exact paired SteamOS host? Any running session will be interrupted."
    if (confirmationKind === "shutdown") return "Shut down the exact paired SteamOS host? Wake later requires a powered NIC and the automatically detected LAN path."
    if (confirmationKind === "display-order-restart") return "Save this display order and restart Gaming Mode on the paired SteamOS host? Running games and the Steam UI will close, and the current stream may disconnect."
    if (confirmationKind === "save-current") return "Is the picture on " + root.saveCurrentDeviceName + "'s display correct right now? This is what Restore saved display will return to."
    if (confirmationKind === "revoke") return "Revoke this client's host credential? The Omarchy copy will be forgotten after the host accepts the request."
    return "Forget this host? Omarchy first asks the host to revoke this client's credential. If the host cannot be reached, only the local pairing is removed and the credential stays valid until it is revoked in Decky."
  }

  function confirmConfirmation() {
    var kind = confirmationKind
    confirmationKind = ""
    if (["suspend", "restart", "shutdown"].indexOf(kind) >= 0) {
      if (!root.powerAllowed()) { root.actionMessage = root.powerReason(); return }
      remote("power", {action: kind}, function(result) {
        if (!result.ok) root.actionMessage = friendlyError(result.error)
        else {
          root.operationData = result.data && result.data.operation
          var label = kind === "suspend" ? "Suspend" : kind === "restart" ? "Restart" : "Shutdown"
          refreshAfterAction(label + " requested; physical transition is not confirmed by the method response")
          // Follow only a sleep: the line above is then the one the next poll may replace.
          if (kind === "suspend") { root.wakeStage = ""; root.sleepStage = "requested"; root.followNote = root.actionMessage }
        }
      })
    } else if (kind === "display-order-restart") {
      if (root.displayOrderRestartAvailable()) root.saveDisplayOrder(true)
      else root.actionMessage = "Display order restart is no longer available; refresh before trying again"
    } else if (kind === "save-current") {
      var target = root.saveCurrentTarget
      root.saveCurrentTarget = null
      root.saveCurrentDisplay(target)
    } else if (kind === "revoke") {
      remote("revoke", {}, function(result) {
        if (!result.ok) { root.actionMessage = friendlyError(result.error); return }
        remote("forget", {}, function(localResult) {
          root.resetPairedState()
          root.actionMessage = root.forgetNotice(result, localResult.ok)
        })
      })
    } else {
      // Forgetting removes this machine's copy of the credential, so the host is
      // asked to drop its own first: a credential left valid on a host nobody is
      // watching is the one outcome to avoid. The local pairing goes either way,
      // so Forget can never be stuck behind an unreachable host, and the notice
      // says which of the two happened.
      remote("revoke", {}, function(revokeResult) {
        remote("forget", {}, function(localResult) {
          root.resetPairedState()
          root.actionMessage = root.forgetNotice(revokeResult, localResult.ok)
        })
      })
    }
  }

  // What happened to the host's copy of the credential, in plain words. Only an
  // accepted revocation (or a host that no longer knows the credential) means the
  // host side is closed; everything else names what is left to do in Decky.
  function forgetNotice(revokeResult, localOk) {
    var tail = localOk ? "" : " Local state needs attention."
    if (revokeResult && revokeResult.ok === true)
      return localOk ? "Credential revoked and local pairing forgotten" : "Credential revoked; local state needs attention"
    if (revokeResult && revokeResult.status === 401)
      return "The host no longer accepts this credential, so there was nothing left to revoke. Local pairing forgotten." + tail
    if (revokeResult && revokeResult.status)
      return "The host did not accept the revocation (" + root.friendlyError(revokeResult.error) + "). Only the local pairing was forgotten; the credential stays valid until it is revoked in Decky." + tail
    return "Host unreachable (" + root.friendlyError(revokeResult ? revokeResult.error : "") + "), so only the local pairing was forgotten. Revoke this client in Decky to invalidate the credential on the host." + tail
  }

  function resetPairedState() {
    root.localData = {paired: false}
    root.reportedScopes = null
    root.syncLinkFactsIdentity()
    root.syncJournalIdentity()
    root.statusData = null
    root.outputsData = null
    root.outputsSignature = ""
    root.previewData = null
    root.displayOrderData = null
    root.displayOrderLatestData = null
    root.displayOrderDraft = []
    root.displayOrderBase = []
    root.displayOrderSignature = ""
    root.displayOrderTopologySignature = ""
    root.displayOrderDraftDirty = false
    root.displayOrderDraftStale = false
    root.displayOrderMutation = ""
    root.displayOrderPendingKeys = []
    root.displayOrderRefreshRequested = false
    root.displayOrderError = ""
    root.operationData = null
    root.saveCurrentTarget = null
    root.pendingPairing = null
    root.connectionState = "idle"
    root.connectionDetail = ""
    root.lastObservationMs = 0
    root.lastObservationAt = ""
  }

  function powerAvailable(action) {
    return !!(statusData && statusData.capabilities && statusData.capabilities[action] === "available")
  }

  function powerLabel(action, label) {
    if (!root.paired) return label
    if (!root.statusData || !root.statusData.capabilities) return label + " (checking…)"
    if (root.powerAvailable(action)) return label
    return label + " (" + root.statusData.capabilities[action] + ")"
  }

  function canRestoreWorking() {
    if (!root.paired || !root.statusData || !root.statusData.capabilities) return false
    return root.statusData.capabilities.display_rescue === "available"
  }

  function canSuspendHost() {
    if (!root.paired || !root.statusData || !root.statusData.capabilities) return false
    return root.statusData.capabilities.suspend === "available"
  }

  function capabilityLegendNeeded() {
    var capabilities = root.statusData && root.statusData.capabilities
    if (!capabilities) return false
    var keys = ["suspend", "restart", "shutdown", "display_rescue"]
    for (var index = 0; index < keys.length; index++) {
      if (capabilities[keys[index]] !== "available") return true
    }
    return false
  }

  function openSaveCurrentConfirmation() {
    var output = root.currentOutput
    if (!root.paired || !output || !output.current_mode_id) {
      root.actionMessage = "No current display mode is available to save"
      return
    }
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    if (root.previewData) {
      root.actionMessage = "Waiting for this preview to be kept or reverted"
      return
    }
    root.saveCurrentTarget = {output_id: String(output.id), generation: output.generation}
    root.openConfirmation("save-current")
  }

  function saveCurrentDisplay(target) {
    if (!target || root.previewData || root.previewMutationBusy) return
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    root.previewMutationBusy = true
    root.actionMessage = "Save requested; checking the host read-back"
    remote("save-current", {
      output_id: target.output_id,
      generation: target.generation,
      visible: true
    }, function(result) {
      root.previewMutationBusy = false
      if (!result.ok) {
        if (result.status === 404) {
          var hasAlternative = false
          for (var index = 0; index < root.visibleModeList.length; index++) {
            if (root.currentOutput && root.visibleModeList[index].id !== root.currentOutput.current_mode_id) hasAlternative = true
          }
          if (!root.currentSavedProfile && !hasAlternative)
            root.actionMessage = "No recovery mode can be saved with this server version"
          else if (hasAlternative)
            root.actionMessage = "This server version cannot save the current mode. Preview and keep an advertised alternative mode instead."
          else
            root.actionMessage = root.friendlyError(result.error)
        } else if (result.status === 409 && /preview/i.test(String(result.error || ""))) {
          root.actionMessage = "A display preview is active; keep or revert it first"
        } else if (result.status === 409 && /stale|no longer advertised/i.test(String(result.error || ""))) {
          root.actionMessage = "The display target changed; refresh and try again"
        } else if (result.status === 409) {
          // Another mutation in flight, or a read-back the host could not confirm:
          // neither is a changed target, so the host's own words stand.
          root.actionMessage = root.friendlyError(result.error)
        } else if (result.status === 503) {
          root.actionMessage = "Steam display bridge is unavailable"
        } else if (result.status === 400) {
          root.actionMessage = "Save current was rejected; the explicit confirmation did not reach the host correctly"
        } else if (result.unknown === true) {
          root.actionMessage = "Result not confirmed; reconnect and check the original operation before retrying"
        } else {
          root.actionMessage = root.friendlyError(result.error)
        }
        return
      }
      var data = result.data || {}
      var operation = data.operation
      var profile = data.profile
      root.operationData = operation || null
      if (!operation || typeof operation.id !== "string") {
        root.actionMessage = "Result not confirmed; the host response omitted its operation record"
        return
      }
      if (operation.state === "succeeded" && profile && profile.mode && profile.verified_at) {
        root.actionMessage = "Requested. Host read-back confirmed the recovery mode: "
          + root.modeLabel(profile.mode) + " · verified " + String(profile.verified_at)
      } else if (operation.state === "failed") {
        root.actionMessage = "Save failed: " + root.friendlyError(operation.reason || operation.outcome)
      } else {
        root.actionMessage = "Result not confirmed; check operation " + operation.id + " before retrying"
      }
      root.refreshAfterAction(root.actionMessage)
    })
  }

  function restoreWorking() {
    if (!root.paired) { setView("settings"); actionMessage = "Pair the Omarchy client before restoring a display"; return }
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    remote("restore", {source: "verified"}, function(result) {
      if (!result.ok) root.actionMessage = friendlyError(result.error)
      else {
        root.operationData = result.data && result.data.operation
        refreshAfterAction("Restore requested; waiting for a host readback")
      }
    })
  }

  function wakeHost() {
    if (!wakeMac) { setView("settings"); actionMessage = "The host did not advertise a usable wake target; pair again while Decky is online"; return }
    // The kernel selects the active route for the broadcast. The detected
    // sender interface is shown for diagnostics; forcing SO_BINDTODEVICE can
    // require privileges that the normal Omarchy user does not have.
    remote("wake", {mac: wakeMac}, function(result) {
      if (!result.ok) root.actionMessage = friendlyError(result.error)
      else {
        root.pollIntervalMs = 2000
        root.sleepStage = ""
        root.wakeStage = "sent"
        root.followNoteSet("Wake packet sent — waiting for host")
      }
    })
  }

  // ---- host health: the optional `self_check` block of the status response --
  // The newest protocol this client speaks; the host nudge compares against it.
  readonly property int hostProtocolVersion: 1

  // Pure presentation of the block, kept in step with the Decky card
  // (selfCheckRows / selfCheckNudge in its frontend) so both clients word the
  // same facts the same way. It reads only what status already returned and
  // never asks the host for anything. Every part is optional: a missing or
  // malformed part degrades to a cautious row instead of throwing, and strings
  // are capped at the schema's own bounds. tests/test_health.py runs this block
  // under Node against the pinned fixtures, so keep it free of QML properties.
  // self-check:begin
  function selfCheckText(value, limit) {
    return typeof value === "string" ? value.slice(0, limit) : ""
  }

  function selfCheckPart(check, name) {
    var part = check ? check[name] : null
    return part !== null && typeof part === "object" && !Array.isArray(part) ? part : {}
  }

  function selfCheckInteger(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value
  }

  // The host writes UTC `YYYY-MM-DDTHH:MM:SSZ`; anything else is not a date.
  function selfCheckInstant(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value) ? Date.parse(value) : NaN
  }

  function selfCheckDay(value) {
    return isFinite(selfCheckInstant(value)) ? value.slice(0, 10) : null
  }

  function selfCheckGlyph(state) {
    // Word first, glyph second: every row also says its state in text.
    return state === "ok" ? "✓" : state === "warn" ? "⊘" : "○"
  }

  function selfCheckRows(check, nowMs) {
    if (!check || typeof check !== "object" || Array.isArray(check)) return []
    var rows = []

    var certificate = selfCheckPart(check, "certificate")
    var shortPrint = selfCheckText(certificate.fingerprint_short, 8)
    var identity = shortPrint ? " · …" + shortPrint : ""
    var notBeforeDay = selfCheckDay(certificate.not_before)
    var notAfterDay = selfCheckDay(certificate.not_after)
    // Without a usable clock the validity window cannot be judged, so it is not.
    var clockUsable = typeof nowMs === "number" && isFinite(nowMs)
    if (certificate.ready !== true)
      rows.push({id: "certificate", label: "Certificate", state: "warn", value: "Not ready — pairing is unavailable"})
    else if (clockUsable && notAfterDay && nowMs > selfCheckInstant(certificate.not_after))
      rows.push({id: "certificate", label: "Certificate", state: "warn", value: "Expired " + notAfterDay + identity})
    else if (clockUsable && notBeforeDay && nowMs < selfCheckInstant(certificate.not_before))
      rows.push({id: "certificate", label: "Certificate", state: "warn", value: "Not valid until " + notBeforeDay + " — check this device's clock" + identity})
    else if (notAfterDay)
      rows.push({id: "certificate", label: "Certificate", state: "ok", value: "Valid until " + notAfterDay + identity})
    else
      rows.push({id: "certificate", label: "Certificate", state: "ok", value: "Ready" + identity})

    var listener = selfCheckPart(check, "listener")
    var address = selfCheckText(listener.address, 64) || "all interfaces"
    var port = selfCheckInteger(listener.port) && listener.port >= 1 && listener.port <= 65535 ? listener.port : 18443
    rows.push(listener.bound === true
      ? {id: "listener", label: "Listener", state: "ok", value: "Listening on " + address + ":" + port}
      : {id: "listener", label: "Listener", state: "off", value: "Not listening"})

    var bridge = selfCheckPart(check, "bridge")
    var methods = []
    for (var index = 0; Array.isArray(bridge.methods) && index < bridge.methods.length && methods.length < 8; index++) {
      var method = selfCheckText(bridge.methods[index], 32)
      if (method) methods.push(method)
    }
    rows.push(bridge.ready === true
      ? {id: "bridge", label: "Steam bridge", state: "ok", value: methods.length ? "Ready · " + methods.join(", ") : "Ready"}
      : {id: "bridge", label: "Steam bridge", state: "warn", value: "Not ready — power and display control are unavailable"})

    var source = selfCheckPart(check, "temperature").source
    rows.push(source === "steam" || source === "hwmon"
      ? {id: "temperature", label: "Temperature", state: "ok", value: source === "steam" ? "Read from Steam" : "Read from system sensors"}
      : {id: "temperature", label: "Temperature", state: "off", value: "No sensor reading"})

    // A stale "running" is the last thing the host heard, not a confirmation.
    var provider = selfCheckPart(check, "provider")
    var contract = selfCheckText(provider.contract_version, 64) ? " · " + selfCheckText(provider.contract_version, 64) : ""
    var stale = provider.stale === true ? " (stale)" : ""
    if (provider.monitor_enabled !== true)
      rows.push({id: "sunshine", label: "Sunshine", state: "off", value: "Monitoring off"})
    else if (provider.state === "running")
      rows.push({id: "sunshine", label: "Sunshine", state: provider.stale === true ? "unknown" : "ok", value: "Running" + stale + contract})
    else if (provider.state === "stopped")
      rows.push({id: "sunshine", label: "Sunshine", state: "warn", value: "Stopped" + stale + contract})
    else if (provider.state === "restarting")
      rows.push({id: "sunshine", label: "Sunshine", state: "warn", value: "Restarting" + contract})
    else
      rows.push({id: "sunshine", label: "Sunshine", state: "unknown", value: (provider.state === "unavailable" ? "Owner unavailable" : "State unknown") + contract})

    // "unknown" is the host's own literal for a version it could not read.
    var versions = selfCheckPart(check, "versions")
    var plugin = selfCheckText(versions.plugin, 64)
    var pluginKnown = plugin !== "" && plugin !== "unknown"
    rows.push({
      id: "versions", label: "Host", state: pluginKnown ? "ok" : "unknown",
      value: "Plugin " + (pluginKnown ? "v" + plugin : "version unknown")
        + " · protocol " + (selfCheckInteger(versions.protocol) ? versions.protocol : "unknown")
    })
    return rows
  }

  function selfCheckNudge(check, supported) {
    var protocol = selfCheckPart(check, "versions").protocol
    if (!selfCheckInteger(protocol))
      return {state: "unknown", text: "This host does not report its health. Update the Decky plugin on the host to see it here."}
    if (protocol > supported)
      return {state: "newer", text: "Host protocol " + protocol + " is newer than this client supports — some actions may be unavailable. Update this plugin."}
    if (protocol < supported)
      return {state: "older", text: "Host protocol " + protocol + " is older than this client — update the plugin on the host."}
    return {state: "ok", text: "Host protocol " + protocol + " matches this client."}
  }
  // self-check:end

  // ---- advisory DRM link facts: the optional `GET /v1/drm` read -------------
  // Pure presentation, kept in step with the Decky frontend (linkFactsFor,
  // linkAdvisory, linkFactsLine and LINK_ADVISORY_TEXT) so both clients word
  // the same facts the same way. The kernel list is resolutions only, so
  // nothing here reasons about refresh rate, and it is advisory: a resolution it
  // omits may still work, so the result is a warning and never a gate. The host
  // joins each connector to a display-bridge output (steam_output_id); this code
  // never matches a connector by name. Anything unexpected is simply no facts.
  // tests/test_linkfacts.py runs this block under Node against the pinned
  // fixtures, so keep it free of QML properties.
  // link-facts:begin
  function linkAdvisoryText() {
    return "Heads up · This resolution is not in the display link's mode list. It may still work, or the display may show no signal. The preview reverts on its own unless you keep it."
  }

  function linkAdvisoryGlyph() {
    // Word first, glyph second: the text above carries the meaning on its own.
    return "△"
  }

  function linkFactsInteger(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value
  }

  function linkFactsText(value, limit) {
    return typeof value === "string" ? value.slice(0, limit) : ""
  }

  // The topology a read answers for: every output's id and generation. Facts
  // read under another topology are not used, because ids may have moved.
  function linkFactsTopology(outputs) {
    if (!Array.isArray(outputs) || outputs.length === 0) return ""
    var parts = []
    for (var index = 0; index < outputs.length; index++) {
      var output = outputs[index]
      parts.push(output !== null && typeof output === "object" ? [String(output.id), String(output.generation)] : null)
    }
    return JSON.stringify(parts)
  }

  // The helper answers {ok, data: {drm}}. Anything else, an error included, is
  // no facts.
  function linkFactsFromResult(result) {
    var drm = result && result.ok === true && result.data ? result.data.drm : null
    return drm !== null && typeof drm === "object" && !Array.isArray(drm) && Array.isArray(drm.connectors) ? drm : null
  }

  // The one connector the host joined to this output, or nothing. Zero or two
  // claimants is ambiguity, not a match.
  function linkFactsFor(drm, output) {
    if (!drm || drm.available !== true || !Array.isArray(drm.connectors)) return null
    if (!output || typeof output.id !== "string" || output.id === "") return null
    var match = null
    var count = 0
    for (var index = 0; index < drm.connectors.length; index++) {
      var item = drm.connectors[index]
      if (item !== null && typeof item === "object" && item.steam_output_id === output.id) {
        match = item
        count++
      }
    }
    return count === 1 ? match : null
  }

  // Warn only for a connected, external connector with a non-empty list that
  // lacks the selected resolution. Every other case, an unknown one included,
  // says nothing.
  function linkAdvisory(drm, output, mode) {
    var link = linkFactsFor(drm, output)
    if (!link || link.connected !== true || link.is_internal !== false) return null
    if (!Array.isArray(link.resolutions) || link.resolutions.length === 0) return null
    if (!mode || !linkFactsInteger(mode.width) || !linkFactsInteger(mode.height)) return null
    if (mode.width < 1 || mode.height < 1) return null
    var wanted = mode.width + "x" + mode.height
    for (var index = 0; index < link.resolutions.length; index++) {
      if (link.resolutions[index] === wanted) return null
    }
    return linkAdvisoryText()
  }

  function linkFactsCard(link) {
    return linkFactsText(link.gpu_id, 16).replace(/^drm:/, "")
  }

  // "HDMI-A-1 · card0 · LG TV SSCR2 · GSM model 49780": the display name only
  // when it says more than the connector, the model only when both parts exist.
  function linkFactsLine(link) {
    if (!link || typeof link !== "object" || Array.isArray(link)) return ""
    var connector = linkFactsText(link.connector, 64)
    if (connector === "") return ""
    var parts = [connector + " · " + linkFactsCard(link)]
    var name = linkFactsText(link.display_name, 128)
    if (name && name !== connector) parts.push(name)
    var vendor = linkFactsText(link.monitor_vendor, 3)
    if (vendor && linkFactsInteger(link.monitor_product_id)) parts.push(vendor + " model " + link.monitor_product_id)
    return parts.join(" · ")
  }

  // The rows of the Connection details group, for the joined connector.
  function linkFactsRows(link) {
    if (!link || typeof link !== "object" || Array.isArray(link)) return []
    var connector = linkFactsText(link.connector, 64)
    if (connector === "") return []
    var card = linkFactsCard(link)
    var name = linkFactsText(link.display_name, 128)
    var vendor = linkFactsText(link.monitor_vendor, 3)
    var connected = link.connected === true
    return [
      {id: "connector", label: "Connector", value: connector, state: ""},
      {id: "card", label: "GPU card", value: card || "Not reported", state: ""},
      {id: "display", label: "Display", value: name && name !== connector ? name : "Name not reported", state: ""},
      {id: "monitor", label: "Monitor", value: vendor && linkFactsInteger(link.monitor_product_id) ? vendor + " model " + link.monitor_product_id : "Maker and model not reported", state: ""},
      {id: "connected", label: "Connection", value: (connected ? "Connected" : "Not connected") + (link.is_internal === true ? " · internal panel" : ""), state: connected ? "ok" : "off"}
    ]
  }

  // What the group says when there is no joined connector to describe.
  function linkFactsNote(drm, output) {
    if (!output || typeof output.id !== "string" || output.id === "")
      return "No display output is known yet."
    if (!drm || drm.available !== true)
      return "Link facts are not available. They are read when Display is opened, and older hosts do not report them."
    if (!linkFactsFor(drm, output))
      return "No connector is joined to this display, so the host cannot say which link it uses."
    return ""
  }
  // link-facts:end

  // One read per display interaction: when Display opens (setView) and when its
  // output topology changes (applyOutputs). It rides the poll lane, so it never
  // disables a control, is dropped with the panel, and is never repeated by a
  // timer. The facts are advisory: whatever the helper answers, a failure
  // included, only ever becomes "no facts" and touches no status or action text.
  function requestLinkFacts() {
    if (!root.opened || !root.paired || root.view !== "display") return
    var key = root.linkFactsTopology(root.outputList)
    // Without outputs there is nothing to join; applyOutputs asks again.
    if (key === "" || key === root.linkFactsRequestedKey) return
    root.linkFactsRequestedKey = key
    var identity = root.linkFactsIdentity
    enqueueHelper("link-facts", {timeout: 2.5}, function(result) {
      // Another host was paired while this was in flight: not its facts.
      if (identity !== root.linkFactsIdentity) return
      root.linkFactsData = root.linkFactsFromResult(result)
      root.linkFactsDataKey = root.linkFactsData ? key : ""
      // The topology moved while this was in flight, and an identical request
      // queued meanwhile was merged into it. Read again for the current one.
      if (root.linkFactsRequestedKey !== key) {
        root.linkFactsRequestedKey = ""
        root.requestLinkFacts()
      }
    }, true)
  }

  // Facts belong to the host they came from. A different pairing or endpoint
  // would otherwise match its outputs against another host's connectors.
  function syncLinkFactsIdentity() {
    var identity = root.paired ? String(root.localData.host_id || "") + "|" + String(root.localData.endpoint || "") : ""
    if (identity === root.linkFactsIdentity) return
    root.linkFactsIdentity = identity
    root.linkFactsData = null
    root.linkFactsDataKey = ""
    root.linkFactsRequestedKey = ""
  }

  // ---- host operation journal: the optional `GET /v1/operations` read -------
  // Pure presentation, kept in step with the Decky card (its journal block) so
  // both clients word a record the same way. The wording table below is the
  // protocol's operation-vocabulary.json; tests/test_journal.py fails if the two
  // drift. A record says what the host did and read back. Nothing here turns a
  // completed state into a claim about the picture: only an outcome whose code
  // says the owner confirmed it carries the word "visible". A code this table
  // lacks is shown as sanitised text, never hidden and never guessed at. The
  // helper has already rebuilt every record from an allowlist; this block
  // sanitises again because it is the display boundary. tests/test_journal.py
  // runs it under Node against the pinned fixtures, so keep it free of QML
  // properties.
  // journal:begin
  function journalVocabulary() {
    return {
      states: {
        "accepted": "Requested",
        "dispatched": "In progress",
        "observed_return": "Returned, not confirmed",
        "succeeded": "Completed",
        "failed": "Failed",
        "unknown": "Result not confirmed"
      },
      kinds: {
        "power.suspend": "Suspend",
        "power.restart": "Restart",
        "power.shutdown": "Shut down",
        "display.preview": "Display mode preview",
        "display.confirm": "Display preview confirmation",
        "display.restore": "Display restore",
        "display.save-current": "Save current display mode",
        "display.order": "Gaming Mode display order",
        "display.order.reset": "Reset display order",
        "display.preference": "Gaming Mode display preference",
        "display.selection.preview": "Screen selection preview",
        "display.selection.revert": "Screen selection revert",
        "display.session-restart": "Gaming Mode restart",
        "sunshine.restart": "Start or restart Sunshine",
        "pair.revoke-self": "Revoke this client"
      },
      outcomes: {
        "method_returned": "Steam returned from the call; the physical result is not confirmed",
        "mode_request_returned": "Mode request returned; waiting for confirmation",
        "target_active_read_back": "Selected screen read back as active; waiting for confirmation",
        "target_active_reconciled_after_reload": "Selected screen read back as active after a plugin reload",
        "visible_picture_confirmed": "Owner confirmed the picture was visible",
        "active_screen_visible_and_confirmed": "Owner confirmed the selected screen was visible",
        "current_mode_visible_and_read_back": "Current mode read back; owner confirmed the picture was visible",
        "restored_and_read_back": "Previous mode restored and read back",
        "baseline_screen_restored": "Previous screen restored",
        "gamescope_output_configured": "Gamescope output saved; applies after Gaming Mode restarts",
        "gamescope_output_configured_and_restart_requested": "Gamescope output saved; Gaming Mode restart requested",
        "gamescope_output_configured_restart_failed": "Gamescope output saved; Gaming Mode restart failed",
        "gamescope_output_cleared": "Gamescope output preference cleared",
        "gamescope_session_restart_requested": "Gaming Mode restart requested",
        "already_running": "Sunshine was already running",
        "running": "Sunshine reported running",
        "revoked": "This client's access was revoked"
      },
      restore_states: {
        "dispatched": "Restore in progress",
        "succeeded": "Previous screen restored",
        "failed": "Previous screen not restored",
        "unknown": "Restore result not confirmed"
      }
    }
  }

  function journalNote() {
    return "Entries record what the host did and read back. A completed entry does not by itself show that the picture is visible."
  }

  function journalUnavailableText() {
    return "The host journal is not available from this device."
  }

  // Ten rows, not Decky's twenty: this group sits in Settings above the display
  // preferences and the credential controls, and every row adds roughly 75
  // pixels above them.
  function journalRowLimit() {
    return 10
  }

  function journalMinGapMs() {
    return 5000
  }

  // Two reads, entry or refresh, are never closer than the gap. A clock that
  // moved far backwards must not keep the read shut; a small step back (the
  // shared clock the button reads lags by up to a second) must not reopen it.
  function journalMayRead(lastMs, nowMs) {
    if (typeof nowMs !== "number" || !isFinite(nowMs)) return false
    if (typeof lastMs !== "number" || !isFinite(lastMs) || lastMs <= 0) return true
    var elapsed = nowMs - lastMs
    return elapsed >= journalMinGapMs() || elapsed < -journalMinGapMs()
  }

  // Control, format and line-separator characters (C0 and C1 controls, soft
  // hyphen, Arabic letter mark, zero-width and bidirectional marks, line and
  // paragraph separators, word joiners, the byte order mark and the
  // interlinear annotation marks) are dropped, never kept. Written as code
  // points rather than escapes so the source carries no invisible character.
  function journalPrintable(code) {
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) return false
    if (code === 0xad || code === 0x61c || code === 0xfeff) return false
    if (code >= 0x200b && code <= 0x200f) return false
    if (code >= 0x2028 && code <= 0x202e) return false
    if (code >= 0x2060 && code <= 0x206f) return false
    return code < 0xfff9 || code > 0xfffb
  }

  // The rest is cut at the limit without leaving half a surrogate pair. The
  // loop stops at the limit, so a huge string costs no more than a short one.
  function journalText(value, limit) {
    if (typeof value !== "string") return ""
    var text = ""
    for (var index = 0; index < value.length && text.length < limit; index++) {
      if (journalPrintable(value.charCodeAt(index))) text += value.charAt(index)
    }
    var last = text.charCodeAt(text.length - 1)
    if (last >= 0xd800 && last <= 0xdbff) text = text.slice(0, -1)
    return text.trim()
  }

  function journalClip(text, limit) {
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text
  }

  function journalWord(table, code) {
    if (typeof code !== "string" || code === "") return ""
    return Object.prototype.hasOwnProperty.call(table, code) ? table[code] : journalText(code, 64)
  }

  function journalInteger(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value
  }

  // One request fact: a scalar, a short list, or one level of scalars. Depth is
  // bounded here too, whatever the helper already did.
  function journalValue(value, depth) {
    if (typeof value === "string") return journalText(value, 128)
    if (typeof value === "boolean") return value ? "true" : "false"
    if (typeof value === "number") return isFinite(value) ? String(value) : ""
    if (value === null || typeof value !== "object" || depth >= 2) return ""
    var parts = []
    if (Array.isArray(value)) {
      for (var index = 0; index < value.length && index < 16; index++) {
        var entry = journalValue(value[index], depth + 1)
        if (entry !== "") parts.push(entry)
      }
      return parts.join(", ")
    }
    if (journalInteger(value.width) && journalInteger(value.height)) {
      var rate = typeof value.refresh_hz === "number" && isFinite(value.refresh_hz) ? "@" + Math.round(value.refresh_hz * 100) / 100 : ""
      return value.width + "x" + value.height + rate
    }
    var keys = Object.keys(value).slice(0, 6)
    for (var at = 0; at < keys.length; at++) {
      var inner = journalValue(value[keys[at]], depth + 1)
      var name = journalText(keys[at], 40).replace(/_/g, " ")
      if (inner !== "" && name !== "") parts.push(name + " " + inner)
    }
    return parts.join(", ")
  }

  function journalRequested(target) {
    if (target === null || typeof target !== "object" || Array.isArray(target)) return ""
    var keys = Object.keys(target).slice(0, 8)
    var parts = []
    for (var index = 0; index < keys.length; index++) {
      var value = journalValue(target[keys[index]], 0)
      var name = journalText(keys[index], 40).replace(/_/g, " ")
      if (value !== "" && name !== "") parts.push(name + ": " + value)
    }
    return journalClip(parts.join(" · "), 360)
  }

  // The host writes UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`; anything else is not shown.
  function journalTime(value) {
    var found = typeof value === "string" ? /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d{1,9})?Z$/.exec(value) : null
    return found ? found[1] + " " + found[2] + " UTC" : ""
  }

  function journalRows(operations) {
    if (!Array.isArray(operations)) return []
    var words = journalVocabulary()
    var rows = []
    for (var index = 0; index < operations.length && index < 256 && rows.length < journalRowLimit(); index++) {
      var item = operations[index]
      if (item === null || typeof item !== "object" || Array.isArray(item)) continue
      var id = journalText(item.id, 128)
      if (id === "") continue
      var kind = journalWord(words.kinds, item.kind) || "Operation"
      var state = journalWord(words.states, item.state) || "State unknown"
      var lines = []
      var requested = journalRequested(item.target)
      if (requested !== "") lines.push("Requested · " + requested)
      var outcome = journalWord(words.outcomes, item.outcome)
      if (outcome !== "") lines.push("Recorded · " + outcome)
      var restore = journalWord(words.restore_states, item.restore_state)
      if (restore !== "") lines.push(restore)
      var reason = journalText(item.reason, 256)
      if (reason !== "") lines.push("Reason · " + reason)
      var time = journalTime(item.updated_at)
      if (time !== "") lines.push("Updated · " + time)
      rows.push({id: id, title: kind + " · " + state, lines: lines})
    }
    return rows
  }

  // The helper answers {ok, data: {available, operations, total}}. Anything
  // else, an error included, is not available.
  function journalFromResult(result) {
    var data = result && result.ok === true ? result.data : null
    if (!data || typeof data !== "object" || Array.isArray(data)) return null
    return data.available === true && Array.isArray(data.operations) ? data : null
  }

  function journalEmptyModel(state, message) {
    return {state: state, message: message, rows: [], more: "", note: ""}
  }

  // Everything the group shows for one state: a message in place of rows, the
  // rows, the truncation line, and the standing note once there is a list.
  function journalModel(state, data) {
    if (state === "loading") return journalEmptyModel("loading", "Loading the host journal…")
    if (state === "unavailable") return journalEmptyModel("unavailable", journalUnavailableText())
    if (state !== "ready") return journalEmptyModel("idle", "Not read yet. Choose Refresh journal to read it.")
    var operations = data && Array.isArray(data.operations) ? data.operations : null
    if (!operations) return journalEmptyModel("unavailable", journalUnavailableText())
    var rows = journalRows(operations)
    var total = journalInteger(data.total) && data.total >= operations.length ? data.total : operations.length
    return {
      state: "ready",
      message: rows.length > 0 ? "" : operations.length === 0 ? "The host has recorded no operations for this device yet." : "No entries could be shown.",
      rows: rows,
      more: rows.length > 0 && rows.length < total ? "Showing the newest " + rows.length + " of " + total + "." : "",
      note: journalNote()
    }
  }
  // journal:end

  // ---- what the owner of the host allows this pairing -------------------------
  // Pure wording and checks, kept in step with Decky's accessRole, accessSummary
  // and accessNote so both clients say the same thing the same way. The scopes
  // come from the helper already decoded; this still reads them as untrusted:
  // only the three grantable scopes count, and anything that is not a list holds
  // nothing. tests/test_panel_access.py runs this block under Node against
  // Decky's own, so keep it ES5 and free of QML properties.
  // access:begin
  function accessTable() {
    return [
      {scope: "status.read", label: "Status"},
      {scope: "power.control", label: "Power"},
      {scope: "display.control", label: "Display"}
    ]
  }

  function accessHeld(scopes) {
    var table = accessTable()
    var held = []
    if (!Array.isArray(scopes)) return held
    for (var index = 0; index < table.length; index++) {
      if (scopes.indexOf(table[index].scope) >= 0) held.push(table[index].scope)
    }
    return held
  }

  function accessSummary(scopes) {
    var table = accessTable()
    var held = accessHeld(scopes)
    var labels = []
    for (var index = 0; index < table.length; index++) {
      if (held.indexOf(table[index].scope) >= 0) labels.push(table[index].label)
    }
    return labels.length > 0 ? labels.join(" · ") : "No permissions"
  }

  function accessRole(scopes) {
    var held = accessHeld(scopes)
    if (held.length === accessTable().length) return "Full access"
    if (held.length === 0) return "No access"
    return held.length === 1 && held[0] === "status.read" ? "View only" : "Custom"
  }

  function accessNote(scopes) {
    return accessHeld(scopes).length === accessTable().length ? "" : "The owner of the host chose this and can change it there."
  }

  function accessLine(scopes) {
    return accessRole(scopes) + " · " + accessSummary(scopes)
  }

  function accessAllows(scopes, scope) {
    return accessHeld(scopes).indexOf(scope) >= 0
  }

  // "" only when the scope is held, so a caller can show the reason it returns.
  function accessReason(scopes, scope) {
    if (accessAllows(scopes, scope)) return ""
    var table = accessTable()
    var label = "this action"
    for (var index = 0; index < table.length; index++) {
      if (table[index].scope === scope) label = table[index].label
    }
    return "The host owner has not allowed " + label + " for this pairing."
  }

  // The scopes a status reply reports, or null when it does not say: no block, or
  // one that is not an object holding a list. An empty list is a real answer.
  function accessReported(status) {
    if (!status || typeof status !== "object") return null
    var block = status.credential
    if (!block || typeof block !== "object" || !Array.isArray(block.scopes)) return null
    return accessHeld(block.scopes)
  }

  function accessSame(left, right) {
    return accessHeld(left).join(",") === accessHeld(right).join(",")
  }
  // access:end

  // One source for every control and every action: what the pairing was last
  // told it may do. The status poll replaces it (adoptScopes), the helper saved
  // the same list, and a host that did not report leaves the saved one in place.
  function heldScopes() {
    if (!root.paired) return []
    return root.reportedScopes !== null ? root.reportedScopes : root.localData.scopes
  }

  function powerAllowed() {
    return root.accessAllows(root.heldScopes(), "power.control")
  }

  function displayAllowed() {
    return root.accessAllows(root.heldScopes(), "display.control")
  }

  function powerReason() {
    return root.paired ? root.accessReason(root.heldScopes(), "power.control") : ""
  }

  function displayReason() {
    return root.paired ? root.accessReason(root.heldScopes(), "display.control") : ""
  }

  function adoptScopes(status) {
    var reported = root.accessReported(status)
    if (reported === null || root.accessSame(reported, root.heldScopes())) return
    root.reportedScopes = reported
  }

  // One read when Settings opens and one per explicit Refresh, spaced by
  // journalMayRead. It rides the poll lane, so it never disables a control, is
  // dropped with the panel, and is never repeated by a timer. Whatever the
  // helper answers, a failure included, only ever becomes "not available".
  function requestJournal() {
    if (!root.opened || !root.paired || root.view !== "settings") return
    var now = Date.now()
    if (!root.journalMayRead(root.journalStartedMs, now)) return
    root.journalStartedMs = now
    root.journalState = "loading"
    root.journalData = null
    var epoch = root.journalEpoch
    enqueueHelper("journal", {timeout: 2.5}, function(result) {
      // The panel closed, or another host was paired, while this was in flight.
      if (epoch !== root.journalEpoch) return
      var data = root.journalFromResult(result)
      root.journalData = data
      root.journalState = data ? "ready" : "unavailable"
    }, true)
  }

  // The records are kept in memory for the open panel and the paired host only.
  function resetJournal() {
    root.journalEpoch++
    root.journalState = "idle"
    root.journalData = null
    root.journalStartedMs = 0
  }

  function syncJournalIdentity() {
    var identity = root.paired ? String(root.localData.host_id || "") + "|" + String(root.localData.endpoint || "") : ""
    if (identity === root.journalIdentity) return
    root.journalIdentity = identity
    root.resetJournal()
  }

  function sunshineVisible() {
    // Sunshine is an optional server-owned capability. A disabled monitor is
    // not a stopped process and must not appear as a client recovery control.
    return !!(statusData && statusData.sunshine && statusData.sunshine.enabled === true)
  }

  function sunshineLabel() {
    var item = statusData && statusData.sunshine
    if (!item || item.state === "disabled") return "Disabled in Decky"
    if (item.state === "running") return "● Running"
    if (item.state === "stopped") return "■ Stopped"
    if (item.state === "restarting") return "↻ Restarting"
    return "? Status unavailable"
  }

  function sunshineReason() {
    var item = statusData && statusData.sunshine
    if (!item) return ""
    if (item.state === "disabled") return "Monitoring is off in Decky."
    if (item.reason && /provider is not connected|owner plugin was not reachable|owner bridge is unavailable/i.test(String(item.reason)))
      return "Decky Sunshine is not connected through Decky Loader."
    if (item.reason) return describeError(item.reason)
    return item.age_ms === null || item.age_ms === undefined ? "No provider observation yet." : "Observation age " + item.age_ms + " ms."
  }

  function sunshineRestartAvailable() {
    if (!root.statusData || !root.statusData.sunshine || root.statusData.sunshine.enabled !== true || !root.statusData.capabilities) return false
    return root.statusData.sunshine.state === "stopped"
      && root.statusData.capabilities.sunshine_restart === "available"
  }

  function restartSunshine() {
    if (!root.sunshineRestartAvailable() || !root.powerAllowed()) return
    remote("sunshine-restart", {}, function(result) {
      if (!result.ok) root.actionMessage = friendlyError(result.error)
      else { operationData = result.data && result.data.operation; refreshAfterAction("Restart requested; waiting for a fresh provider observation") }
    })
  }

  // ---- display -----------------------------------------------------------

  function moveDisplayOrder(outputKey, delta) {
    if (!root.displayOrderCanEdit()) return
    var current = root.displayOrderDraft.slice()
    var index = current.indexOf(outputKey)
    var nextIndex = index + delta
    if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return
    var moved = current[index]
    current.splice(index, 1)
    current.splice(nextIndex, 0, moved)
    root.displayOrderDraft = current
    root.displayOrderDraftDirty = !root.displayOrderArraysEqual(current, root.displayOrderBase)
    root.actionMessage = "Display order draft changed; save it for the next Gaming Mode session"
  }

  function saveDisplayOrder(restart) {
    if (restart === true) {
      if (!root.displayOrderRestartAvailable()) return
    } else if (!root.displayOrderCanSave()) {
      return
    }
    var generation = root.displayOrderGenerationFor(root.displayOrderData)
    var keys = root.displayOrderDraft.slice()
    if (generation < 0 || keys.length === 0) {
      root.actionMessage = "Refresh the display order before saving it"
      return
    }
    root.displayOrderPendingKeys = keys
    root.displayOrderMutation = restart === true ? "restart" : "save"
    root.actionMessage = restart === true
      ? "Saving display order; the fixed Gaming Mode restart will be requested next…"
      : "Saving display order for the next Gaming Mode session…"
    remote(restart === true ? "display-order-restart" : "display-order-save", {
      output_keys: keys,
      generation: generation,
    }, function(result) {
      if (!result.ok) {
        root.displayOrderMutation = ""
        root.displayOrderPendingKeys = []
        if (/stale|topolog|generation|ambiguous|connected/i.test(String(result.error || "")))
          root.displayOrderDraftStale = true
        root.actionMessage = result.unknown === true
          ? "Display order result is unknown; do not resend automatically. Refresh and reconcile the host operation."
          : root.friendlyError(result.error)
        return
      }
      var data = result.data || {}
      var operation = data.operation
      if (!operation || !operation.id) {
        root.displayOrderMutation = ""
        root.displayOrderPendingKeys = []
        root.actionMessage = "Host response omitted an operation ID; refresh before making another display-order change"
        return
      }
      root.operationData = operation
      root.operationPolls = 0
      root.refreshAfterAction(restart === true
        ? "Display order save accepted; waiting to reconcile the Gaming Mode restart"
        : "Display order save accepted; waiting for host confirmation")
      root.settleDisplayOrderOperation(operation.id, operation)
    })
  }

  function resetDisplayOrder() {
    var block = root.displayOrderBlock(root.displayOrderData)
    if (!root.paired || !root.displayAllowed() || !block || root.displayOrderMutation !== "" || root.displayOrderRefreshRequested
        || root.displayOrderPendingKeys.length > 0 || root.actionBusy
        || (block.available !== true && root.displayOrderSavedKeysFor(root.displayOrderData).length === 0)) return
    root.displayOrderMutation = "reset"
    root.displayOrderPendingKeys = []
    root.actionMessage = "Restoring automatic display order…"
    remote("display-order-reset", {}, function(result) {
      if (!result.ok) {
        root.displayOrderMutation = ""
        root.actionMessage = result.unknown === true
          ? "Automatic display-order reset is unknown; refresh and reconcile the host operation."
          : root.friendlyError(result.error)
        return
      }
      var operation = result.data && result.data.operation
      if (!operation || !operation.id) {
        root.displayOrderMutation = ""
        root.actionMessage = "Host response omitted an operation ID; refresh before making another change"
        return
      }
      root.operationData = operation
      root.operationPolls = 0
      root.refreshAfterAction("Automatic display order reset accepted; waiting for host confirmation")
      root.settleDisplayOrderOperation(operation.id, operation)
    })
  }

  function refreshDisplayOrder() {
    if (!root.paired || root.displayOrderMutation !== "") return
    root.displayOrderRefreshRequested = true
    root.actionMessage = "Refreshing display order…"
    enqueueHelper("display-order", {timeout: 2.5}, function(result) {
      if (!result.ok) {
        root.applyDisplayOrderFailure(result.error)
        root.displayOrderRefreshRequested = false
        return
      }
      root.applyDisplayOrder(result.data, true)
      root.displayOrderRefreshRequested = false
    }, true)
  }

  function modeLabel(mode) {
    if (!mode) return "Unavailable"
    var refreshRate = mode.refresh_hz === null || mode.refresh_hz === undefined ? "?" : mode.refresh_hz
    return mode.width + " × " + mode.height + " @ " + refreshRate + " Hz"
  }

  function isStandardRefreshRate(mode) {
    if (!mode || mode.refresh_hz === null || mode.refresh_hz === undefined) return true
    var rate = Number(mode.refresh_hz)
    if (!isFinite(rate)) return false
    var standard = [50, 59, 60, 90, 100, 119, 120, 144, 165, 240]
    for (var index = 0; index < standard.length; index++) {
      if (Math.abs(rate - standard[index]) < 0.3) return true
    }
    return false
  }

  function refreshValue(mode) {
    if (!mode || mode.refresh_hz === null || mode.refresh_hz === undefined) return -1
    var rate = Number(mode.refresh_hz)
    return isFinite(rate) ? rate : -1
  }

  // Largest first, then fastest, with the current mode pinned to the top so
  // the reference point is always the first row.
  function sortModes(modes, currentId) {
    var copy = modes.slice()
    // Pinning on an absent id would make every mode compare as "the current
    // one", and a comparator that answers -1 for both orderings produces a
    // different list on every sort.
    var pinned = currentId !== null && currentId !== undefined && String(currentId) !== ""
    copy.sort(function(left, right) {
      if (pinned && left.id === currentId) return -1
      if (pinned && right.id === currentId) return 1
      var leftArea = Number(left.width) * Number(left.height)
      var rightArea = Number(right.width) * Number(right.height)
      if (leftArea !== rightArea) return rightArea - leftArea
      var leftRate = root.refreshValue(left)
      var rightRate = root.refreshValue(right)
      if (leftRate !== rightRate) return rightRate - leftRate
      return String(left.id) < String(right.id) ? -1 : String(left.id) > String(right.id) ? 1 : 0
    })
    return copy
  }

  function filteredModeList() {
    var output = root.currentOutput
    if (!output || !output.modes) return []
    var currentId = output.current_mode_id
    var modes = output.modes.filter(function(mode) {
      return root.showNonstandardRefreshRates || root.isStandardRefreshRate(mode) || mode.id === currentId
    })
    return root.sortModes(modes, currentId)
  }

  function previewTargetIsListed() {
    var target = root.previewData ? root.previewData.target_mode : null
    if (!target || !target.id) return false
    var modes = root.visibleModeList
    for (var index = 0; index < modes.length; index++) {
      if (modes[index].id === target.id) return true
    }
    return false
  }

  function previewMatchesMode(mode) {
    var target = root.previewData ? root.previewData.target_mode : null
    if (!target || !mode) return false
    // Prefer the exact target. Two rows that differ only by 59 vs 60 Hz must
    // not both claim the preview. Geometry matching stays as the fallback for
    // the case Steam re-enumerates mode ids while the preview is live.
    if (target.id && mode.id && root.previewTargetIsListed()) return target.id === mode.id
    if (Number(target.width) !== Number(mode.width) || Number(target.height) !== Number(mode.height)) return false
    var expectedRefresh = Number(target.refresh_hz)
    var actualRefresh = Number(mode.refresh_hz)
    if (!isFinite(expectedRefresh) || !isFinite(actualRefresh)) return target.refresh_hz === mode.refresh_hz
    return Math.abs(expectedRefresh - actualRefresh) < 0.3
      || ((expectedRefresh === 59 || expectedRefresh === 60) && (actualRefresh === 59 || actualRefresh === 60))
  }

  function previewMatchesVisibleMode() {
    if (!previewData || !previewData.target_mode) return false
    for (var index = 0; index < visibleModeList.length; index++) {
      if (previewMatchesMode(visibleModeList[index])) return true
    }
    return false
  }

  function clampDisplaySelection() {
    var outputs = root.outputList
    if (outputs.length === 0) {
      root.selectedOutputId = ""
      root.selectedModeId = ""
      return
    }
    var outputFound = false
    for (var index = 0; index < outputs.length; index++) {
      if (outputs[index].id === root.selectedOutputId) outputFound = true
    }
    if (!outputFound) root.selectedOutputId = outputs[0].id
    var modes = root.visibleModeList
    if (modes.length === 0) {
      root.selectedModeId = ""
      return
    }
    var modeFound = false
    for (var modeIndex = 0; modeIndex < modes.length; modeIndex++) {
      if (modes[modeIndex].id === root.selectedModeId) modeFound = true
    }
    if (modeFound) return
    // Default to the mode the host is actually running.
    var currentId = root.currentOutput ? root.currentOutput.current_mode_id : ""
    for (var currentIndex = 0; currentIndex < modes.length; currentIndex++) {
      if (modes[currentIndex].id === currentId) {
        root.selectedModeId = currentId
        return
      }
    }
    root.selectedModeId = modes[0].id
  }

  function outputLabel(output) {
    if (!output) return "Output state unavailable"
    return output.name || output.description || output.id
  }

  function outputNameForId(outputId) {
    var outputs = root.outputList
    for (var index = 0; index < outputs.length; index++) {
      if (String(outputs[index].id) === String(outputId)) return root.outputLabel(outputs[index])
    }
    return String(outputId || "unknown output")
  }

  function profileIdentityLabel(profile) {
    if (!profile) return "Unavailable"
    var identity = profile.output_identity
    if (identity && typeof identity === "object") {
      try { return JSON.stringify(identity).slice(0, 160) } catch (error) { return "identity unavailable" }
    }
    return String(profile.output_id || "identity unavailable")
  }

  function refreshConnectionDetails() {
    if (!root.opened || !root.paired) return
    enqueueHelper("outputs", {timeout: 2.5}, function(result) {
      if (result.ok) root.applyOutputs(result.data)
    }, true)
  }

  function cycleOutput() {
    var outputs = root.outputList
    if (outputs.length < 2) return
    var index = 0
    for (var search = 0; search < outputs.length; search++) {
      if (outputs[search].id === root.selectedOutputId) index = search
    }
    root.selectedOutputId = outputs[(index + 1) % outputs.length].id
    root.selectedModeId = ""
    root.clampDisplaySelection()
  }

  function previewModeFor(mode) {
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    if (mode) root.selectedModeId = mode.id
    if (!currentOutput || !selectedMode || selectedMode.id === currentOutput.current_mode_id) {
      actionMessage = "Choose an advertised mode different from the current mode"
      return
    }
    if (previewData) {
      actionMessage = "Finish the active preview with Save or Revert"
      return
    }
    remote("preview", {output_id: currentOutput.id, mode_id: selectedMode.id, generation: currentOutput.generation}, function(result) {
      if (!result.ok) { root.actionMessage = friendlyError(result.error); return }
      operationData = result.data && result.data.operation
      previewData = result.data && result.data.preview
      tickCountdown()
      root.actionMessage = "Preview requested; confirm only after the visible picture is verified"
    })
  }

  function keepPreview() {
    if (!previewData || previewMutationBusy) return
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    previewMutationBusy = true
    remote("confirm", {preview_id: previewData.preview_id, visible: true}, function(result) {
      if (!result.ok) {
        root.actionMessage = friendlyError(result.error)
      } else {
        root.operationData = result.data && result.data.operation
        previewData = null
        refreshAfterAction("Display mode saved as the last known-good mode")
      }
      previewMutationBusy = false
    })
  }

  function revertPreview() {
    if (!previewData || previewMutationBusy) return
    if (!root.displayAllowed()) { root.actionMessage = root.displayReason(); return }
    previewMutationBusy = true
    remote("restore", {source: "preview"}, function(result) {
      if (!result.ok) {
        root.actionMessage = friendlyError(result.error)
      } else {
        root.operationData = result.data && result.data.operation
        refreshAfterAction("Revert requested; waiting for the host-owned restore readback")
      }
      previewMutationBusy = false
    })
  }

  // ---- discovery and pairing --------------------------------------------

  function discoveryHostCount() {
    return discoveryData && discoveryData.hosts ? discoveryData.hosts.length : 0
  }

  function shortFingerprint(value) {
    var text = String(value || "")
    if (text.length < 20) return text
    return text.slice(7, 11) + " " + text.slice(11, 15) + " … " + text.slice(-4)
  }

  function discoverHosts() {
    var port = Number(discoveryPort)
    if (!isFinite(port) || Math.floor(port) !== port || port < 1024 || port > 65535) {
      actionMessage = "Enter a discovery port between 1024 and 65535"
      return
    }
    root.discoveryData = null
    root.selectedDiscoveryHost = null
    root.actionMessage = "Scanning the active IPv4 LAN for SteamOS Companion…"
    remote("discover", {port: port}, function(result) {
      if (!result.ok) { root.actionMessage = friendlyError(result.error); return }
      root.discoveryData = result.data || {hosts: []}
      var count = root.discoveryData.hosts ? root.discoveryData.hosts.length : 0
      if (count === 1) {
        // One answer is unambiguous. More than one is a choice the owner has
        // to make, so nothing is selected for them.
        root.selectDiscoveryHost(root.discoveryData.hosts[0])
      } else if (count > 1) {
        root.actionMessage = "Found " + count + " listeners. Select the one that belongs to your Deck, then compare its fingerprint in Decky."
      } else {
        root.actionMessage = root.discoveryData.reason || "No SteamOS Companion listener found"
      }
    })
  }

  function selectDiscoveryHost(candidate) {
    if (!candidate || !candidate.endpoint) return
    selectedDiscoveryHost = candidate
    pairingEndpointOverride = String(candidate.endpoint)
    actionMessage = String(candidate.endpoint) + " selected. Request pairing, then approve the matching code in Decky."
  }

  function pairingStatusText() {
    if (root.pairingBusy) return "Contacting the host"
    if (!root.pendingPairing) return root.pairingFailure !== "" ? "Last attempt failed — " + root.pairingFailure : "No active pairing request"
    if (root.pairingCountdownSeconds <= 0) return "Expired — start a new request"
    if (root.pairingNotice !== "") return "Waiting for Decky approval — " + root.pairingNotice
    return "Waiting for Decky approval"
  }

  function pairingCountdownText() {
    if (!root.pendingPairing) return ""
    if (root.pairingCountdownSeconds <= 0) return "Expired"
    var minutes = Math.floor(root.pairingCountdownSeconds / 60)
    var seconds = String(root.pairingCountdownSeconds % 60)
    if (seconds.length < 2) seconds = "0" + seconds
    return minutes + ":" + seconds
  }

  function pairingPrimaryLabel() {
    if (root.pendingPairing) return "Waiting for Decky approval…"
    if (root.pairingBusy) return "Requesting pairing…"
    if (root.pairingFailure !== "") return "Try pairing again"
    if (root.pairingText.trim()) return root.paired ? "Re-pair from payload" : "Pair from advanced payload"
    return "Request pairing"
  }

  function applyPairingResult(result) {
    root.pairingBusy = false
    if (!result.ok) {
      root.pendingPairing = null
      root.pairingCountdownSeconds = 0
      root.pairingNotice = ""
      root.pairingRetryUntilMs = 0
      var error = friendlyError(result.error)
      root.pairingFailure = error
      if (/expired|not available|already claimed|consumed/i.test(error))
        root.actionMessage = "Pairing request ended: " + error + ". Start a new request."
      else
        root.actionMessage = "Pairing request failed: " + error
      return
    }
    var data = result.data || {}
    if (data.state === "approved") {
      root.pendingPairing = null
      root.pairingCountdownSeconds = 0
      root.pairingNotice = ""
      root.pairingFailure = ""
      root.pairingRetryUntilMs = 0
      root.pairingText = ""
      root.pairingEndpointOverride = ""
      root.editingPairing = false
      root.actionMessage = "Pairing approved. Endpoint, certificate pin, and wake target saved."
      root.localInspect()
      return
    }
    // Approval polling runs once a second. Rewriting the action line on each
    // one would erase whatever the owner last did and make the panel look like
    // it is reacting to something when nothing has happened.
    var previous = root.pendingPairing
    var isNewRequest = !previous || previous.client_id !== data.client_id
    root.pendingPairing = data
    root.pairingFailure = ""
    root.pairingCountdownSeconds = data.seconds_remaining || 0
    root.pairingNotice = data.notice || ""
    // The host asked for room. Honouring it is the difference between waiting
    // and being rate limited for the rest of the request window.
    var retryAfter = Number(data.retry_after)
    root.pairingRetryUntilMs = isFinite(retryAfter) && retryAfter > 0 ? Date.now() + retryAfter * 1000 : 0
    if (isNewRequest) root.actionMessage = "Compare the code below with Decky, then approve it there."
  }

  function startPairing() {
    if (root.pendingPairing || root.pairingBusy) return
    var scopes = ["status.read", "power.control", "display.control"]
    root.pairingBusy = true
    root.pairingFailure = ""
    root.pairingNotice = ""
    root.pairingRetryUntilMs = 0
    if (pairingText.trim()) {
      remote("pair", {payload: pairingText, client_name: pairingName, scopes: scopes, endpoint: pairingEndpointOverride}, applyPairingResult)
      return
    }
    if (!selectedDiscoveryHost) {
      root.pairingBusy = false
      actionMessage = "Find and select the Decky host before requesting pairing"
      return
    }
    actionMessage = "Requesting pairing…"
    remote("pair-start", {host: selectedDiscoveryHost, client_name: pairingName, scopes: scopes}, applyPairingResult)
  }

  // Enqueued as a background read, not a mutation: it must neither disable the
  // Cancel button once a second nor be starved by the status poll it shares the
  // lane with. The queue collapses repeats on its own.
  function pollPairing() {
    if (!root.pendingPairing || root.pairingBusy) return
    if (root.pairingRetryUntilMs > 0 && Date.now() < root.pairingRetryUntilMs) return
    root.enqueueHelper("pair-poll", {}, root.applyPairingResult, true)
  }

  // Dropped from the panel immediately. The owner pressed Cancel; leaving the
  // countdown running until a round trip completes reads as a control that did
  // not work, and the record is cleared locally either way.
  function cancelPairing() {
    if (!root.pendingPairing) return
    root.pendingPairing = null
    root.pairingCountdownSeconds = 0
    root.pairingNotice = ""
    root.pairingFailure = ""
    root.pairingRetryUntilMs = 0
    root.pairingBusy = false
    root.actionMessage = "Cancelling the pairing request…"
    // The host is asked to cancel too. When it does not confirm, the helper's
    // notice says what is left for the owner to do in Decky.
    remote("pair-cancel", {}, function(result) {
      if (!result.ok) root.actionMessage = root.friendlyError(result.error)
      else root.actionMessage = result.data && result.data.notice ? String(result.data.notice) : "Pairing request cancelled"
    })
  }

  function saveEndpoint() {
    if (!root.paired || !root.endpointText.trim()) return
    remote("configure", {endpoint: endpointText}, function(result) {
      root.actionMessage = result.ok ? "Endpoint correction saved after pinned host verification" : friendlyError(result.error)
      if (result.ok) localInspect()
    })
  }

  function formatUptime(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(Number(seconds))) return "Unavailable"
    var total = Math.max(0, Math.floor(Number(seconds)))
    var days = Math.floor(total / 86400)
    var hours = Math.floor((total % 86400) / 3600)
    var minutes = Math.floor((total % 3600) / 60)
    return days + ":" + String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0")
  }

  // ---- lifecycle ---------------------------------------------------------

  function onOpened() {
    var keepOperation = !!root.operationData && !root.operationIsSettled()
    var keepDisplayOrderMutation = root.displayOrderMutation !== ""
    pollGeneration++
    view = "host"
    cursorIndex = 0
    cursorActive = false
    if (!keepOperation && !keepDisplayOrderMutation) actionMessage = ""
    cursorKey = ""
    previewMutationBusy = false
    pairingBusy = false
    if (!keepOperation) operationData = null
    operationPolls = 0
    pollIntervalMs = 2000
    root.resetJournal()
    localInspect()
  }

  function onClosed() {
    pollGeneration++
    // Nothing polls while the panel is closed, so a claim being followed would
    // go stale: the next open starts from what it observes.
    wakeStage = ""
    sleepStage = ""
    followNote = ""
    editingPairing = false
    confirmationKind = ""
    root.saveCurrentTarget = null
    root.resetJournal()
    root.helperQueue = root.helperQueue.filter(function(job) { return job.poll !== true })
  }

  function tickCountdown() {
    countdownSeconds = previewData ? Math.max(0, Math.ceil(Number(previewData.deadline || 0) - Date.now() / 1000)) : 0
  }

  function tickPairing() {
    if (!root.pendingPairing) {
      root.pairingCountdownSeconds = 0
      return
    }
    root.pairingCountdownSeconds = Math.max(0, Math.ceil(Number(root.pendingPairing.expires_at || 0) - Date.now() / 1000))
    if (root.pairingCountdownSeconds <= 0) {
      root.pendingPairing = null
      root.pairingNotice = ""
      root.pairingRetryUntilMs = 0
      root.pairingFailure = "the request expired before Decky approved it"
      root.actionMessage = "Pairing request expired. Start a new request."
      remote("pair-cancel", {}, function() {})
      return
    }
    root.pollPairing()
  }

  onOpenedChanged: if (opened) onOpened(); else onClosed()

  Timer {
    interval: root.pollIntervalMs
    repeat: true
    running: root.opened && root.paired
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 250
    repeat: true
    running: root.opened && !!root.previewData
    onTriggered: root.tickCountdown()
  }

  // Deliberately not triggeredOnStart. The request that created the pending
  // record has just returned; polling it again in the same instant earns a 429
  // from the host's own rate limiter.
  Timer {
    interval: 1000
    repeat: true
    running: root.opened && !!root.pendingPairing
    onTriggered: root.tickPairing()
  }

  // Staleness is a function of elapsed time, so something has to make elapsed
  // time observable to the bindings that report it.
  Timer {
    interval: 1000
    repeat: true
    running: root.paired
    onTriggered: root.nowMs = Date.now()
  }

  KeyboardPanel {
    id: keyboardPanel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: keyboardPanel.fittedContentWidth(Style.space(470))
    contentHeight: keyboardPanel.fittedContentHeight(mainColumn.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.editingPairing

      onMoveRequested: function(dx, dy) {
        if (root.modalOpen) {
          if (dx !== 0) confirmDialog.selectedIndex = confirmDialog.selectedIndex === 0 ? 1 : 0
          return
        }
        if (dx !== 0) root.viewByDelta(dx)
        if (dy !== 0) root.moveCursor(dy)
      }
      onActivateRequested: root.activateCursor()
      onCloseRequested: {
        if (root.modalOpen) root.cancelConfirmation()
        else if (root.editingPairing) root.editingPairing = false
        else root.close()
      }
      onTabRequested: function(direction) { root.viewByDelta(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refresh()
        else if (text === "1") root.setView("host")
        else if (text === "2") root.setView("display")
        else if (text === "3") root.setView("settings")
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: mainColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: mainColumn
          width: flick.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "SteamOS Companion"
            meta: root.hostLabel
            detail: root.statusWord
            foreground: root.bar ? root.bar.barForeground : Commons.Color.foreground
            iconComponent: Component {
              Image {
                width: Style.font.display
                height: width
                source: Qt.resolvedUrl("assets/steamos-companion-icon.svg")
                sourceSize.width: width
                sourceSize.height: height
                fillMode: Image.PreserveAspectFit
                smooth: true
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)
            Repeater {
              model: [{key: "host", label: "Host"}, {key: "display", label: "Display"}, {key: "settings", label: "Settings"}]
              delegate: Button {
                required property var modelData
                width: (parent.width - 2 * Style.space(6)) / 3
                text: modelData.label
                leftAlign: true
                // Deliberately no cursor: the row cursor belongs to the view
                // below, and two highlights at index 0 read as a bug.
                selected: root.view === modelData.key
                onClicked: root.setView(modelData.key)
              }
            }
          }

          Loader {
            id: viewLoader
            width: parent.width
            sourceComponent: root.view === "host" ? hostComponent : root.view === "display" ? displayComponent : settingsComponent
            onLoaded: root.clampDisplaySelection()
          }

          // Reachability and the last action are separate facts. A background
          // poll failure must never overwrite what the owner just did.
          HintText {
            width: parent.width
            visible: root.paired && root.connectionState === "unreachable"
            text: "Host unreachable: " + root.connectionDetail + ". Retrying every " + Math.round(root.pollIntervalMs / 1000) + "s."
          }

          BodyText {
            width: parent.width
            visible: root.actionMessage !== ""
            text: root.actionMessage
            font.pixelSize: Style.font.bodySmall
          }
        }
      }

      ConfirmDialog {
        id: confirmDialog
        anchors.fill: parent
        opened: root.modalOpen
        message: root.confirmMessage()
        cancelText: "Cancel"
        confirmText: root.confirmationKind === "save-current" ? "Save as recovery mode" : "Confirm"
        onCanceled: root.cancelConfirmation()
        onConfirmed: root.confirmConfirmation()
      }
    }
  }

  Component {
    id: hostComponent
    Column {
      width: viewLoader.width
      spacing: Style.space(8)

      PanelSectionHeader { text: "Host"; width: parent.width }
      CursorButton {
        width: parent.width
        ownerView: "host"
        rowKey: "restore"
        text: "Restore last known-good display"
        iconText: "↺"
        onClicked: root.restoreWorking()
      }
      HintText {
        visible: root.paired && !root.displayAllowed()
        width: parent.width
        text: root.displayReason()
      }
      CursorButton {
        width: parent.width
        ownerView: "host"
        rowKey: "wake"
        text: root.wakeMac ? "Wake host" : "Wake target unavailable"
        iconText: "☼"
        onClicked: root.wakeHost()
      }
      CursorButton {
        width: parent.width
        ownerView: "host"
        rowKey: "suspend"
        text: root.powerLabel("suspend", "Suspend host")
        iconText: "⏾"
        onClicked: root.openConfirmation("suspend")
      }
      CursorButton {
        width: parent.width
        ownerView: "host"
        rowKey: "restart"
        text: root.powerLabel("restart", "Restart host")
        iconText: "↻"
        onClicked: root.openConfirmation("restart")
      }
      CursorButton {
        width: parent.width
        ownerView: "host"
        rowKey: "shutdown"
        text: root.powerLabel("shutdown", "Shut down host")
        iconText: "⏻"
        onClicked: root.openConfirmation("shutdown")
      }
      CursorButton {
        visible: root.sunshineRestartAvailable()
        width: parent.width
        ownerView: "host"
        rowKey: "sunshine"
        text: "Restart Sunshine"
        iconText: "↻"
        onClicked: root.restartSunshine()
      }
      HintText {
        visible: root.paired && !root.powerAllowed()
        width: parent.width
        text: root.powerReason()
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Observed host state"; width: parent.width }
      BodyText { width: parent.width; text: "Status: " + root.statusWord }
      HintText { width: parent.width; text: root.paired ? root.lastObservation : "Pair the client from Settings to see host status." }
      BodyText { width: parent.width; text: "Uptime: " + root.formatUptime(root.statusData ? root.statusData.uptime_seconds : null) + " (d:h:m)" }
      BodyText {
        width: parent.width
        text: root.statusData && root.statusData.cpu_temperature
          ? "CPU: " + root.statusData.cpu_temperature.celsius + " °C (" + root.statusData.cpu_temperature.label + ")"
          : "CPU temperature: Unavailable"
      }
      HintText {
        width: parent.width
        text: root.statusData && root.statusData.capabilities
          ? "Power bridge: suspend " + root.statusData.capabilities.suspend + " · restart " + root.statusData.capabilities.restart + " · shutdown " + root.statusData.capabilities.shutdown
          : "Power bridge: checking…"
      }
      HintText {
        width: parent.width
        visible: root.capabilityLegendNeeded()
        text: "available: Steam exposes the method. unverified: not observed yet. unsupported: this Steam build has no such method. unavailable: the bridge answered but the method is missing."
      }
      HintText {
        width: parent.width
        visible: !!root.operationData
        text: root.operationData ? "Operation: " + root.operationData.state + (root.operationData.reason ? " — " + root.operationData.reason : "") : ""
      }

      PanelSectionHeader { visible: root.sunshineVisible(); text: "Sunshine"; width: parent.width }
      BodyText { visible: root.sunshineVisible(); width: parent.width; text: root.sunshineLabel() }
      HintText {
        visible: root.sunshineVisible()
        width: parent.width
        text: root.sunshineReason() + (root.sunshineRestartAvailable() ? " Decky allows a paired client to restart it." : "")
      }
    }
  }

  Component {
    id: displayComponent
    Column {
      width: viewLoader.width
      spacing: Style.space(8)

      PanelSectionHeader { text: "Display"; width: parent.width }
      HintText {
        visible: root.paired && !root.displayAllowed()
        width: parent.width
        text: root.displayReason() + " Status and the display list stay available."
      }
      Column {
        visible: !root.currentSavedProfile
        width: parent.width
        spacing: Style.space(4)
        BodyText { width: parent.width; text: "No recovery mode saved. Check the picture, then save the current display as recovery mode." }
        CursorButton {
          width: parent.width
          ownerView: "display"
          rowKey: "save-current"
          text: "Save current display as recovery mode"
          iconText: "!"
          enabled: root.rowEnabled("save-current")
          onClicked: root.openSaveCurrentConfirmation()
        }
        HintText {
          visible: !!root.previewData
          width: parent.width
          text: "Waiting for this preview to be kept or reverted"
        }
      }
      PanelSectionHeader { text: "Display order"; width: parent.width }
      HintText {
        width: parent.width
        text: "Gaming Mode uses the first available screen in this order when the next session starts. Active is host readback only."
      }
      BodyText {
        width: parent.width
        text: root.displayOrderStatusText(root.displayOrderData)
      }
      HintText {
        width: parent.width
        visible: {
          var block = root.displayOrderBlock(root.displayOrderData)
          return !!block && block.restart_required === true && block.restart_available !== true
        }
        text: "The host requires a Gaming Mode restart, but its fixed restart route is unavailable."
      }
      HintText {
        width: parent.width
        visible: !!root.displayOrderLatestData
        text: "A topology change was observed. Refresh to replace this list; the current draft is retained."
      }
      BodyText {
        width: parent.width
        visible: !!root.displayOrderBlock(root.displayOrderData) && root.displayOrderRows.length === 0
        text: "No connected outputs are available for an order."
      }
      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: root.displayOrderRows.length > 0
        Repeater {
          model: root.displayOrderRows
          delegate: Row {
            required property var modelData
            required property int index
            readonly property string outputKey: root.displayOrderOutputKey(modelData)
            width: parent.width
            spacing: Style.space(4)

            BodyText {
              width: parent.width - moveUpButton.width - moveDownButton.width - 2 * parent.spacing
              text: (index + 1) + ". " + root.displayOrderOutputLabel(modelData)
                + "\n" + root.displayOrderConnectorLabel(modelData)
                + (root.displayOrderActiveLabel(modelData) !== "" ? " · " + root.displayOrderActiveLabel(modelData) : "")
              elide: Text.ElideRight
            }
            CursorButton {
              id: moveUpButton
              width: Style.space(86)
              ownerView: "display"
              rowKey: "display-order:up:" + outputKey
              text: "Move up"
              iconText: "↑"
              onClicked: root.moveDisplayOrder(outputKey, -1)
            }
            CursorButton {
              id: moveDownButton
              width: Style.space(100)
              ownerView: "display"
              rowKey: "display-order:down:" + outputKey
              text: "Move down"
              iconText: "↓"
              onClicked: root.moveDisplayOrder(outputKey, 1)
            }
          }
        }
      }
      CursorButton {
        width: parent.width
        ownerView: "display"
        rowKey: "display-order:save"
        text: "Save for next session"
        iconText: "✓"
        onClicked: root.saveDisplayOrder(false)
      }
      CursorButton {
        width: parent.width
        ownerView: "display"
        rowKey: "display-order:restart"
        text: "Save and restart Gaming Mode"
        iconText: "↻"
        onClicked: root.openConfirmation("display-order-restart")
      }
      CursorButton {
        width: parent.width
        ownerView: "display"
        rowKey: "display-order:automatic"
        text: "Use automatic display order"
        iconText: "A"
        onClicked: root.resetDisplayOrder()
      }
      CursorButton {
        width: parent.width
        ownerView: "display"
        rowKey: "display-order:refresh"
        text: "Refresh display order"
        iconText: "↻"
        onClicked: root.refreshDisplayOrder()
      }
      HintText {
        width: parent.width
        text: "Save changes only the next Gaming Mode start. Restart closes games and Steam UI and may disconnect the current stream."
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Display modes"; width: parent.width }
      CursorButton {
        visible: root.outputList.length > 1
        width: parent.width
        ownerView: "display"
        rowKey: "output"
        text: root.outputLabel(root.currentOutput) + " · output " + (root.outputList.indexOf(root.currentOutput) + 1) + " of " + root.outputList.length
        iconText: "⇆"
        onClicked: root.cycleOutput()
      }
      BodyText {
        width: parent.width
        visible: root.outputList.length <= 1
        text: root.currentOutput ? root.outputLabel(root.currentOutput) + " · generation " + root.currentOutput.generation : "Output state unavailable"
        elide: Text.ElideRight
      }
      HintText {
        width: parent.width
        text: root.showNonstandardRefreshRates
          ? "Largest first. Showing all advertised refresh rates."
          : "Largest first. Showing common refresh rates; enable non-standard rates in Settings."
      }

      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: root.previewCardShown()
        PanelSectionHeader { text: "Active preview"; width: parent.width }
        BodyText {
          width: parent.width
          text: root.previewData && root.previewData.target_mode
            ? root.modeLabel(root.previewData.target_mode) + " · " + root.countdownSeconds + "s remaining"
            : "A display preview is active; mode rows are still updating"
        }
        Row {
          width: parent.width
          spacing: Style.space(6)
          HintText {
            width: parent.width - activeApplyButton.width - activeRevertButton.width - 2 * parent.spacing
            text: "Verify picture, then:"
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
          }
          CursorButton {
            id: activeApplyButton
            width: Style.space(112)
            ownerView: "display"
            rowKey: "preview:keep"
            text: "Save"
            iconText: "✓"
            onClicked: root.keepPreview()
          }
          CursorButton {
            id: activeRevertButton
            width: Style.space(92)
            ownerView: "display"
            rowKey: "preview:revert"
            text: "Revert"
            iconText: "↩"
            onClicked: root.revertPreview()
          }
        }
      }

      Repeater {
        model: root.visibleModeList
        delegate: Column {
          required property var modelData
          required property int index
          readonly property bool isCurrent: !!root.currentOutput && modelData.id === root.currentOutput.current_mode_id
          readonly property bool isSelected: root.selectedModeId === modelData.id
          // Advisory only, for the selected mode that is not the current one,
          // and only when the host joined a connector to this output. Anything
          // else leaves the row exactly as it was.
          readonly property var advisoryText: isSelected && !isCurrent ? root.linkAdvisory(root.linkFacts, root.currentOutput, modelData) : null
          readonly property var advisoryLink: advisoryText ? root.linkFactsFor(root.linkFacts, root.currentOutput) : null
          width: parent.width
          spacing: Style.space(4)

          Column {
            visible: !!advisoryText
            width: parent.width
            spacing: Style.space(2)
            BodyText {
              width: parent.width
              text: root.linkAdvisoryGlyph() + " " + (advisoryText || "")
            }
            HintText {
              width: parent.width
              text: "Link: " + root.linkFactsLine(advisoryLink)
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)
            CursorButton {
              width: parent.width - (modePreviewButton.visible ? modePreviewButton.width + parent.spacing : 0)
              ownerView: "display"
              rowKey: "mode:" + modelData.id
              text: root.modeLabel(modelData) + (isCurrent ? " · current" : "")
              iconText: isCurrent ? "●" : "○"
              selected: isSelected
              onClicked: {
                root.selectedModeId = modelData.id
                root.cursorActive = true
                root.setCursor(root.rowIndexOf("mode:" + modelData.id))
              }
            }
            CursorButton {
              id: modePreviewButton
              width: Style.space(120)
              visible: root.previewStartShown(modelData)
              ownerView: "display"
              rowKey: "preview:start"
              text: root.previewMatchesMode(modelData) ? "Preview " + root.countdownSeconds + "s" : "Preview"
              iconText: "◇"
              onClicked: root.previewModeFor(modelData)
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)
            visible: root.previewMatchesMode(modelData)
            HintText {
              width: parent.width - applyButton.width - revertButton.width - 2 * parent.spacing
              text: "Verify picture, then:"
              verticalAlignment: Text.AlignVCenter
              elide: Text.ElideRight
            }
            CursorButton {
              id: applyButton
              width: Style.space(112)
              ownerView: "display"
              rowKey: "preview:keep"
              text: "Save"
              iconText: "✓"
              onClicked: root.keepPreview()
            }
            CursorButton {
              id: revertButton
              width: Style.space(92)
              ownerView: "display"
              rowKey: "preview:revert"
              text: "Revert"
              iconText: "↩"
              onClicked: root.revertPreview()
            }
          }
        }
      }

      Column {
        visible: !!root.currentSavedProfile
        width: parent.width
        spacing: Style.space(4)
        CursorButton {
          width: parent.width
          ownerView: "display"
          rowKey: "save-current"
          text: "Save current display as recovery mode"
          iconText: "✓"
          enabled: root.rowEnabled("save-current")
          onClicked: root.openSaveCurrentConfirmation()
        }
        HintText {
          width: parent.width
          // `visible` does not stop this binding from evaluating, so guard the null profile.
          text: root.currentSavedProfile
            ? "Saved on " + root.outputLabel(root.currentOutput) + ": "
              + root.modeLabel(root.currentSavedProfile.mode) + " · verified "
              + String(root.currentSavedProfile.verified_at || "time unavailable")
            : ""
        }
        HintText {
          visible: !!root.previewData
          width: parent.width
          text: "Waiting for this preview to be kept or reverted"
        }
      }
      CursorButton {
        width: parent.width
        ownerView: "display"
        rowKey: "restore"
        text: "Restore last known-good mode"
        iconText: "↺"
        onClicked: root.restoreWorking()
      }
      HintText {
        width: parent.width
        text: "Save confirms the visible preview and stores that mode as the last known-good mode. Restore last known-good mode returns to it if the current picture is wrong."
      }
      BodyText {
        visible: !!(root.outputsData && root.outputsData.reason)
        width: parent.width
        text: root.outputsData ? root.outputsData.reason : ""
        font.pixelSize: Style.font.bodySmall
      }
    }
  }

  Component {
    id: settingsComponent
    Column {
      width: viewLoader.width
      spacing: Style.space(8)

      // Discovery comes first: the host has to be found and selected before
      // a pairing request has anywhere to go.
      PanelSectionHeader { text: "1 · Find the host"; width: parent.width }
      HintText {
        width: parent.width
        text: "Scans the active IPv4 LAN. Discovery sends no pairing material; the selected listener is pinned before anything else is sent."
      }
      Row {
        width: parent.width
        spacing: Style.space(6)
        TextField {
          width: parent.width - discoverButton.width - Style.space(6)
          placeholderText: "Port 18443"
          text: root.discoveryPort
          inputMethodHints: Qt.ImhDigitsOnly
          onActiveFocusChanged: root.editingPairing = activeFocus
          onTextChanged: if (activeFocus) root.discoveryPort = text
          onAccepted: root.discoverHosts()
        }
        CursorButton {
          id: discoverButton
          width: Style.space(150)
          ownerView: "settings"
          rowKey: "discover"
          text: "Find hosts"
          iconText: "⌕"
          onClicked: root.discoverHosts()
        }
      }
      Repeater {
        model: root.discoveryData && root.discoveryData.hosts ? root.discoveryData.hosts : []
        delegate: CursorButton {
          required property var modelData
          required property int index
          width: parent.width
          ownerView: "settings"
          rowKey: "host:" + modelData.endpoint
          text: modelData.endpoint + "  ·  " + root.shortFingerprint(modelData.certificate_fingerprint)
          // Reflects the selected listener, not the saved endpoint field.
          iconText: !!root.selectedDiscoveryHost && root.selectedDiscoveryHost.endpoint === modelData.endpoint ? "●" : "○"
          selected: !!root.selectedDiscoveryHost && root.selectedDiscoveryHost.endpoint === modelData.endpoint
          onClicked: root.selectDiscoveryHost(modelData)
        }
      }
      HintText {
        visible: !!root.discoveryData && root.discoveryHostCount() > 1
        width: parent.width
        text: "More than one listener answered. Only the fingerprint shown in Decky identifies your Deck."
      }
      HintText {
        visible: !!root.selectedDiscoveryHost
        width: parent.width
        text: root.selectedDiscoveryHost
          ? "Selected: " + root.selectedDiscoveryHost.endpoint + " · fingerprint " + root.shortFingerprint(root.selectedDiscoveryHost.certificate_fingerprint)
          : ""
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "2 · Request pairing"; width: parent.width }
      HintText {
        width: parent.width
        text: "Omarchy derives a comparison code from the selected host's certificate. The code is never sent, so a different machine answering on this address shows different digits."
      }
      TextField {
        width: parent.width
        placeholderText: "Client name"
        text: root.pairingName
        onActiveFocusChanged: root.editingPairing = activeFocus
        onTextChanged: if (activeFocus) root.pairingName = text
      }
      Text {
        visible: !!root.pendingPairing
        width: parent.width
        textFormat: Text.PlainText
        text: "VERIFICATION CODE\n" + (root.pendingPairing ? root.pendingPairing.pairing_code : "")
        color: Commons.Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body * 1.35
        font.bold: true
        wrapMode: Text.WordWrap
      }
      BodyText {
        visible: !!root.pendingPairing || root.pairingFailure !== "" || root.pairingBusy
        width: parent.width
        font.pixelSize: Style.font.bodySmall
        text: root.pendingPairing
          ? "Status: " + root.pairingStatusText() + "\nExpires in: " + root.pairingCountdownText()
            + "\nApprove in Decky only if the digits match."
          : "Status: " + root.pairingStatusText()
      }
      CursorButton {
        width: parent.width
        ownerView: "settings"
        rowKey: "pair"
        text: root.pairingPrimaryLabel()
        iconText: "⇄"
        onClicked: root.startPairing()
      }
      CursorButton {
        visible: !!root.pendingPairing
        width: parent.width
        ownerView: "settings"
        rowKey: "cancel"
        text: "Cancel pairing request"
        iconText: "×"
        onClicked: root.cancelPairing()
      }
      HintText {
        width: parent.width
        text: "Sunshine is owned by Decky Sunshine. Decky controls monitoring and restart permission; this client only shows what Decky exposes."
      }

      PanelSeparator { width: parent.width }
      Toggle {
        width: parent.width
        label: "Show advanced pairing and endpoint controls"
        description: "Full pairing payload fallback and manual endpoint correction."
        checked: root.showAdvancedPairing
        onClicked: root.showAdvancedPairing = !root.showAdvancedPairing
      }
      Column {
        width: parent.width
        spacing: Style.space(8)
        visible: root.showAdvancedPairing

        PanelSectionHeader { text: "Advanced pairing payload"; width: parent.width }
        HintText { width: parent.width; text: "Fallback for when discovery is not possible. Paste the payload shown by Decky." }
        TextField {
          width: parent.width
          placeholderText: "steamos-companion:v1:…"
          text: root.pairingText
          onActiveFocusChanged: root.editingPairing = activeFocus
          onTextChanged: if (activeFocus) root.pairingText = text
          onAccepted: root.startPairing()
        }

        PanelSectionHeader { text: "Saved endpoint"; width: parent.width }
        HintText { width: parent.width; text: "Saved automatically when pairing is approved. Correct it here only if the host moved to a new address." }
        TextField {
          width: parent.width
          placeholderText: "https://host:18443"
          text: root.endpointText
          onActiveFocusChanged: root.editingPairing = activeFocus
          onTextChanged: {
            if (activeFocus) {
              root.endpointText = text
              root.pairingEndpointOverride = ""
            }
          }
          onAccepted: root.saveEndpoint()
        }
        HintText {
          visible: root.paired && !!root.localData.certificate_fingerprint
          width: parent.width
          wrapMode: Text.WrapAnywhere
          text: "Pinned TLS identity: " + root.localData.certificate_fingerprint
        }
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Connection details"; width: parent.width }
      HintText {
        visible: root.paired
        width: parent.width
        text: "Pinned host: " + String(root.localData.host_id || "identity unavailable")
          + " · endpoint: " + String(root.localData.endpoint || "unavailable")
      }
      // What the owner of the host allows this pairing, worded as the Deck words
      // it. It follows the scopes the last status read reported, or the ones
      // saved at pairing when the host does not say.
      Column {
        id: accessGroup
        visible: root.paired
        width: parent.width
        spacing: Style.space(2)

        readonly property var scopesNow: root.heldScopes()

        BodyText { width: parent.width; text: "Access on this host" }
        HintText { width: parent.width; text: root.accessLine(accessGroup.scopesNow) }
        HintText {
          visible: root.accessNote(accessGroup.scopesNow) !== ""
          width: parent.width
          text: root.accessNote(accessGroup.scopesNow)
        }
      }
      Column {
        visible: root.paired && !!root.outputsData
        width: parent.width
        spacing: Style.space(4)
        Repeater {
          model: root.outputsData && Array.isArray(root.outputsData.profiles) ? root.outputsData.profiles : []
          delegate: Column {
            required property var modelData
            width: parent.width
            spacing: Style.space(2)
            BodyText {
              width: parent.width
              text: "Saved recovery mode · " + root.outputNameForId(modelData.output_id)
                + " · " + root.modeLabel(modelData.mode)
            }
            HintText {
              width: parent.width
              wrapMode: Text.WrapAnywhere
              text: "Output identity: " + root.profileIdentityLabel(modelData)
                + " · verified " + String(modelData.verified_at || "time unavailable")
            }
          }
        }
        HintText {
          visible: !!root.outputsData && (!Array.isArray(root.outputsData.profiles) || root.outputsData.profiles.length === 0)
          width: parent.width
          text: "No recovery mode saved"
        }
      }
      HintText {
        visible: root.paired && !root.outputsData
        width: parent.width
        text: "Saved recovery details are not available yet; open Display to refresh them."
      }

      // Host health is read from the status response the open panel already
      // polls, so this group adds no request and no timer of its own. The
      // block is optional: a host that predates it gets the update nudge.
      Column {
        id: healthGroup
        visible: root.paired
        width: parent.width
        spacing: Style.space(4)

        readonly property var healthCheck: root.statusData && typeof root.statusData.self_check === "object" ? root.statusData.self_check : null
        readonly property var healthRows: root.selfCheckRows(healthCheck, root.nowMs)
        readonly property var healthNudge: root.selfCheckNudge(healthCheck, root.hostProtocolVersion)
        // A fixed model keeps the delegates alive across polls; only their text
        // changes, and an unchanged string does not relayout the scroll view.
        readonly property var healthRowIds: ["certificate", "listener", "bridge", "temperature", "sunshine", "versions"]

        PanelSectionHeader { text: "Host health"; width: parent.width }
        HintText {
          visible: !root.statusData
          width: parent.width
          text: "Host health appears here once the host answers a status read."
        }
        HintText {
          visible: !!root.statusData && healthGroup.healthRows.length > 0
          width: parent.width
          text: "Reported by the host · " + root.lastObservation + (root.statusState === "stale" ? " · stale" : "")
        }
        Repeater {
          model: healthGroup.healthRowIds
          delegate: BodyText {
            required property string modelData
            readonly property var row: {
              var rows = healthGroup.healthRows
              for (var index = 0; index < rows.length; index++) {
                if (rows[index].id === modelData) return rows[index]
              }
              return null
            }
            visible: !!row
            width: parent.width
            text: row ? root.selfCheckGlyph(row.state) + " " + row.label + ": " + row.value : ""
          }
        }
        HintText {
          visible: !!root.statusData && healthGroup.healthNudge.state === "ok"
          width: parent.width
          text: healthGroup.healthNudge.text
        }
        BodyText {
          visible: !!root.statusData && healthGroup.healthNudge.state !== "ok"
          width: parent.width
          text: healthGroup.healthNudge.text
        }
      }

      // Link facts for the display the Display view and its saved recovery mode
      // target. This only shows what the Display view already read, so it adds
      // no request and no timer of its own. The host does the matching; with no
      // joined connector the group says so instead of guessing one.
      Column {
        id: linkFactsGroup
        visible: root.paired
        width: parent.width
        spacing: Style.space(4)

        readonly property var linkRecord: root.linkFactsFor(root.linkFacts, root.currentOutput)
        readonly property var linkRows: root.linkFactsRows(linkRecord)
        // A fixed model keeps the delegates alive; only their text changes.
        readonly property var linkRowIds: ["connector", "card", "display", "monitor", "connected"]

        PanelSectionHeader { text: "Link facts"; width: parent.width }
        HintText {
          visible: linkFactsGroup.linkRows.length > 0
          width: parent.width
          text: "The connector the host matched to " + root.outputLabel(root.currentOutput)
            + (root.currentSavedProfile ? ", the display your saved recovery mode targets" : "")
            + ". Advisory: read when Display was opened."
        }
        HintText {
          visible: linkFactsGroup.linkRows.length === 0
          width: parent.width
          text: root.linkFactsNote(root.linkFacts, root.currentOutput)
        }
        Repeater {
          model: linkFactsGroup.linkRowIds
          delegate: BodyText {
            required property string modelData
            readonly property var row: {
              var rows = linkFactsGroup.linkRows
              for (var index = 0; index < rows.length; index++) {
                if (rows[index].id === modelData) return rows[index]
              }
              return null
            }
            visible: !!row
            width: parent.width
            text: row ? (row.state ? root.selfCheckGlyph(row.state) + " " : "") + row.label + ": " + row.value : ""
          }
        }
      }

      // The host journal: what the host recorded about operations. It shows
      // what the last read returned and asks the host for nothing itself; the
      // Refresh row is the one place the owner asks again. A completed entry is
      // never worded as a visible picture (see the journal block above).
      Column {
        id: journalGroup
        visible: root.paired
        width: parent.width
        spacing: Style.space(4)

        readonly property var journalView: root.journalModel(root.journalState, root.journalData)

        PanelSectionHeader { text: "Host journal"; width: parent.width }
        HintText {
          width: parent.width
          text: "What the host recorded for this device, and the Deck's own display entries. Read when Settings opens and when you refresh, never in the background."
        }
        CursorButton {
          width: parent.width
          ownerView: "settings"
          rowKey: "journal"
          text: root.journalState === "loading" ? "Refreshing…" : "Refresh journal"
          iconText: "↻"
          onClicked: root.requestJournal()
        }
        HintText {
          visible: journalGroup.journalView.message !== ""
          width: parent.width
          text: journalGroup.journalView.message
        }
        Repeater {
          model: journalGroup.journalView.rows
          delegate: Column {
            required property var modelData
            width: parent.width
            spacing: Style.space(2)
            BodyText {
              width: parent.width
              wrapMode: Text.WrapAnywhere
              text: modelData.title
            }
            Repeater {
              model: modelData.lines
              delegate: HintText {
                required property string modelData
                width: parent.width
                wrapMode: Text.WrapAnywhere
                text: modelData
              }
            }
          }
        }
        HintText {
          visible: journalGroup.journalView.more !== ""
          width: parent.width
          text: journalGroup.journalView.more
        }
        HintText {
          visible: journalGroup.journalView.note !== ""
          width: parent.width
          text: journalGroup.journalView.note
        }
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Display preferences"; width: parent.width }
      Toggle {
        width: parent.width
        label: "Show non-standard refresh rates"
        description: "Also show modes such as 24, 30, and 75 Hz. The current mode is always kept visible."
        checked: root.showNonstandardRefreshRates
        onClicked: {
          var next = !root.showNonstandardRefreshRates
          root.showNonstandardRefreshRates = next
          root.clampDisplaySelection()
          root.enqueueHelper("configure-display", {show_nonstandard_refresh_rates: next}, function(result) {
            if (!result.ok) {
              root.showNonstandardRefreshRates = !next
              root.actionMessage = root.friendlyError(result.error)
            } else {
              root.actionMessage = next ? "Non-standard refresh rates are now shown" : "Non-standard refresh rates are hidden"
            }
          }, false)
        }
      }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Wake-on-LAN"; width: parent.width }
      HintText {
        width: parent.width
        text: root.wakeMac ? "Host MAC (received automatically): " + root.wakeMac : "Host MAC unavailable — pair again while Decky is online."
      }
      HintText {
        width: parent.width
        text: root.wakeInterface ? "Send through active Omarchy interface: " + root.wakeInterface : "Send through the OS-selected active LAN interface."
      }
      HintText { visible: !!root.wakeHostInterface; width: parent.width; text: "Host adapter: " + root.wakeHostInterface }

      PanelSeparator { width: parent.width }
      PanelSectionHeader { text: "Credential lifecycle"; width: parent.width }
      CursorButton {
        width: parent.width
        ownerView: "settings"
        rowKey: "revoke"
        text: "Revoke host credential"
        iconText: "!"
        onClicked: root.openConfirmation("revoke")
      }
      CursorButton {
        width: parent.width
        ownerView: "settings"
        rowKey: "forget"
        text: "Forget host"
        iconText: "×"
        onClicked: root.openConfirmation("forget")
      }
      HintText {
        width: parent.width
        text: root.paired
          ? "Paired client: " + root.localData.client_id + " · scopes: " + (root.localData.scopes || []).join(", ")
          : "Not configured"
      }
    }
  }
}
