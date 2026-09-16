# 0.5.2 release checklist

## Automated checks completed

- [x] Client unit tests and contract tests
- [x] Native Omarchy manifest validation
- [x] `Panel.qml` QML syntax check
- [x] Decky host unit tests and Python compilation
- [x] Decky frontend JavaScript syntax check
- [x] Omarchy plugin source validated; no archive build is required
- [x] No runtime dependency download or sibling-checkout import
- [x] Bootstrap pairing is bound to the direct TLS channel and uses a one-time polling session
- [x] Pairing listener has bounded threaded connections and request deadlines
- [x] Expired pairing records are pruned and the private state store rolls back failed writes
- [x] Listener settings preserve dirty drafts during background refresh
- [x] Active display previews retain Save/Revert controls during mode re-enumeration
- [x] Comparison code is derived from the pinned certificate and never transmitted
- [x] Client-chosen pairing code form is rejected by the host
- [x] Known-answer derivation vectors are pinned in both test suites
- [x] Pending pairing request is private, resumable, and never occupies the helper lane
- [x] Background polls cannot overwrite the last action result
- [x] A wedged helper process is stopped rather than holding the single lane

## 0.5.2 implementation changes

- [x] Decky and Omarchy package metadata report version `0.5.2`
- [x] Both clients use the compact white linked-device icon style
- [x] Remote Sunshine status and recovery are hidden unless the host reports monitoring enabled
- [x] Client mode does not start a local Sunshine owner watcher or request a separate Sunshine poll

## 0.3.12 implementation changes (carried forward)

- [x] Pair approval automatically stores the endpoint and TLS pin
- [x] Pair approval transfers the host wake MAC; Omarchy selects its active sender interface
- [x] Display generation remains valid across expected mode re-enumeration and preview state mirrors host cleanup
- [x] Suspend, restart/reboot, and shutdown use fixed validated Steam system methods with effect-specific confirmation
- [x] Legacy direct suspend/restart/shutdown helper actions are normalized to the fixed power route
- [x] Display Preview is selected-row-only; Save confirms the visible mode and stores the last known-good recovery mode
- [x] Sunshine monitoring delegates to Decky Sunshine through a guarded Decky Loader owner bridge

## 0.4.0 implementation changes

- [x] Pairing SAS bound to the host certificate; relay impersonation on the LAN closed
- [x] Pairing is non-blocking: `pair-start`, `pair-poll`, `pair-cancel`, resumable across panel close
- [x] Discovery no longer auto-selects when more than one listener answers; fingerprints are shown
- [x] Enter on a freshly opened panel can no longer fire a host mutation
- [x] Helper stderr and exit status are surfaced; missing `python3` is named
- [x] Reachability reported separately from the last action result, with plain-language transport errors
- [x] Status distinguishes not paired, unreachable, checking, stale, bridge unavailable, and ready
- [x] Polls are chained behind status, bounded to 2.5 s, and capped at a 6 s backoff that resets on action
- [x] Mode list sorted largest-first with the current mode pinned; selection tracked by mode identity
- [x] Output selector added when the host advertises more than one output
- [x] Settings reordered to discovery, pairing, then advanced; cursor map follows the layout
- [x] Single cursor at index 0; cursor scrolls into view; controls gated on mutations not polls

## 0.4.0 regression review of the rewritten panel

- [x] A helper process that cannot be spawned settles, frees the lane, and names the missing interpreter
- [x] Watchdog escalates from terminate to kill and releases the controls if neither works
- [x] Keyboard cursor addresses rows by identity, so an inserted or removed row cannot redirect Enter
- [x] A cursor whose row disappeared is disarmed rather than pointed at whatever replaced it
- [x] Activation consults the same enabled expression the control paints itself with
- [x] Disabled controls are visually distinguishable from enabled ones
- [x] Poll de-duplication is keyed on the whole request, so a second operation is not collapsed into the first
- [x] An operation result is discarded unless it answers the operation still being watched
- [x] Output-inventory comparison excludes the live preview, so mode rows survive a poll
- [x] Pairing does not self-inflict a rate-limit refusal, and honours `Retry-After`
- [x] Approval polling no longer rewrites the last action result once per second
- [x] Cancel is applied to the panel immediately; a failed attempt is reported on the pairing control
- [x] Staleness is driven by a ticking clock; an aged-out reading dims the bar rather than lighting it
- [x] Queue overflow spends only background reads, never a mutation and its callback
- [x] Hovering a row moves the cursor without arming it
- [x] Mode sorting is a consistent ordering when the current mode identifier is absent
- [x] No boolean binding assigns a non-boolean; the reloaded panel logs no warnings

## 0.4.0 runtime checks on this workstation

- [x] Plugin synced to `~/.config/omarchy/plugins/` and `omarchy-shell shell rescanPlugins` reloaded it with no QML diagnostics
- [x] Panel opened and closed over IPC in the running Omarchy 4.0.3 shell with no runtime errors
- [x] Installed helper answers `inspect` against the existing 0.3.12-era credential; only the handshake changed, so no re-pairing was required
- [x] `status` against the live host `192.168.199.195` returned a ready Steam bridge, capabilities, wake target, CPU temperature, and running Sunshine within the new 2.5 s poll bound
- [x] `outputs` returned 41 modes on one output; the new ordering pins the current `3440 × 1440 @ 59 Hz` first, then descends by area and refresh
- [x] Cross-project check: the client's real request body passes the host's real validator, both derive identical digits, a relay certificate derives different digits, and the legacy field is refused
- [x] Helper lane driven with an unexecutable command: the job settles, the busy flag clears, and the message names the missing interpreter
- [x] Cursor functions extracted from `Panel.qml` and replayed against three row-shift scenarios; the cursor follows its row, disarms when the row is gone, and holds position when a row is appended
- [x] Re-synced and reloaded after the regression fixes with no QML diagnostics and no binding warnings, where the previous build logged several

These are workstation and live-host reads. They do not close any M5 hardware gate.

## Plugin identities

- Omarchy: `io.github.tuthan.steamoscompanion` / `0.5.2`
- Decky: `steamos-companion-host` / `0.5.2`
- Protocol: `steamos-companion:v1`

## Hardware gates still open

The following require a SteamOS reference host plus a separate Omarchy
computer and are not represented as passed by this source validation:

- real no-signal/black-screen recovery without the TV UI;
- ten suspend/wake cycles, including longer sleep and reboot timing;
- Steam/Decky reload and output hotplug behavior;
- an installed Decky Sunshine owner exposing the narrow provider contract;
- stopped-process recovery, watchdog race, and provider failure;
- theme/text-size/multi-screen screenshots and idle resource measurements.

Until those checks are recorded, call the source an experimental preview for
hardware recovery rather than a validated release.

## Distribution

Install the Omarchy plugin from the reviewed repository or copy the source into
Omarchy's plugin directory with the documented `rsync` workflow. No ZIP or
checksum sidecar is generated or required.
