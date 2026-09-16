# SteamOS Companion Omarchy design rules

Status: adopted for the 0.5.2 client.

This file materializes the adoption recorded in the vault note
`steamos-companion/08-omarchy-client-design`. The shared rules are at
`/home/hvo/Projects/plugin-docs/rules/omarchy-plugin-design.md`; OmaSafe's
concrete reference is
`/home/hvo/Projects/omasafe-plugin/docs/design/02-design-principles.md`.
The client uses the shared PD1–PD12 rules, but does not inherit OmaSafe's
security-analysis vocabulary, trust graph, glyph assignments, or motion values.

## Recorded M0 stack

- Omarchy: `4.0.3-1` (`omarchy version`)
- Quickshell: `0.3.1` (`quickshell --version`)
- OmaSafe reference checkout: `321b11a` (`git rev-parse --short HEAD`)
- Shared plugin rules: file-backed reference; no git revision is available in
  the supplied `plugin-docs` directory.
- Decky/SteamOS/Decky Sunshine: not installed on this Omarchy workstation, so
  the host-side compatibility is recorded separately and remains a hardware
  validation item.

## Application matrix

| Rule | Application in this client | Gate |
|---|---|---|
| PD1 factual state | “Requested”, “Running”, “Status unavailable”, and “Disabled in Decky” are derived from the v1 response; no decorative success claim | Fixture and copy review |
| PD2 separate authorities | Client store, pinned host identity, Steam bridge, display readback, Sunshine provider, and operation journal stay separate | Source/owner review |
| PD3 unavailable states | Missing, malformed, stale, disabled, and unsupported data remain visible | Contract fixtures |
| PD4 limits/evidence | Observation age, host deadline, and unverified hardware claims are disclosed | Release report |
| PD5 exact mutations | Exact output/generation/mode targets and host-side request IDs; disruptive/security actions are confirmed | Stale-target and cancel tests |
| PD6 host kit | `qs.Ui` `Panel`, `KeyboardPanel`, `PanelKeyCatcher`, `PanelHero`, `Button`, `Toggle`, `TextField`, `ConfirmDialog`, and `BarIconButton` | Installed API inspection |
| PD7 type scale | Data and actions use `body`/`bodySmall`; hints and relative time may use `bodySmall` | Minimum/maximum text-size check |
| PD8 flat hierarchy | One hero, Host/Display/Settings views, flat sections, bounded mode list | Layout screenshots |
| PD9 redundant state cues | Expanded state uses a word plus glyph and theme foreground; no color-only meaning | Monochrome check |
| PD10 theme ownership | Surface, type, spacing, color, and panel geometry come from Omarchy tokens/components | Theme sweep |
| PD11 bounded lifecycle | One helper queue and one 2-second visible poll owner; timers and stale poll callbacks stop on close | Hidden-panel activity check |
| PD12 input ownership | One cursor model shared by pointer and keyboard; modal and text-edit states block background actions | Keyboard/pointer/held-key check |

## Product-specific exceptions

1. PD5’s second confirmation is intentionally omitted for Wake, Display
   Preview/Restore, and explicit stopped-Sunshine recovery so blind rescue
   remains reachable. Each action still has exact-target server preflight,
   request IDs, bounded deadlines, and explicit unknown/failure handling.
2. The client polls while its panel is visible. This is the documented
   exception to OmaSafe's event-driven-only project rule; it follows the
   shared PD11 allowance and stops on close. Each cycle asks for status first
   and requests the mode inventory or an operation result only after status
   succeeds, so an unreachable host costs one short request per cycle. The
   interval backs off to six seconds while unreachable and resets on any
   action. The Decky Monitor Sunshine lifecycle is independent and is never
   toggled by the client.
3. The client uses a small Python helper process for the standard library TLS,
   certificate pinning, private state, and UDP WoL. The helper accepts one
   bounded JSON request on stdin, never receives secrets in argv, and returns
   one bounded JSON response. It is a transport boundary, not a shell command
   runner. Its explicit discovery action scans only local IPv4 subnets, uses a
   bounded address count and short connection deadlines, and sends no pairing
   material.

## Interaction and state gates

- The host certificate fingerprint is checked after TLS connect and before the
  pairing request body is written. The bootstrap request is limited to TLS 1.2
  so the client can send Python's defined `tls-unique` channel binding; Decky
  compares it with the binding on its accepted socket and rejects a
  TLS-terminating relay before creating a pending request. A pairing secret is
  never sent to an unpinned endpoint.
- The eight-digit comparison code is derived, not transmitted. Both sides run
  scrypt over the client nonce salted with the host certificate fingerprint, so
  a listener presenting a different certificate derives different digits and
  the mismatch is visible to the owner before approval. The client-chosen code
  form that preceded it is rejected by the host.
- One in-flight pairing request lives in the private client state rather than
  in the panel. Approval is a sequence of short helper calls, so waiting for
  the owner never occupies the single helper lane, the panel can be closed and
  reopened mid-request, and the pairing secret never travels back out to QML.
- The first pending response contains a high-entropy pairing session handle.
  Approval polls use that handle instead of re-deriving the comparison code.
- Approval persists the endpoint and certificate pin in the private client
  state. The approved response also carries the host wake MAC; the client
  persists it and chooses its own active sender interface, because host and
  client interface names are not expected to match.
- Discovery is a locator only: a listener must identify as the SteamOS Companion
  HTTPS service, but the selected endpoint still has to match the pairing
  payload's certificate pin before the secret is sent.
- `202 Accepted` is rendered as a requested operation. The client does not
  label a method return as visible picture, completed power transition, or
  streaming health.
- The host owns the 15-second preview deadline and restore. Client close,
  reload, or loss of network does not cancel it.
- Display generation describes the output/mode inventory rather than the
  current mode, so an expected mode switch or Steam mode-ID re-enumeration does
  not invalidate the live preview. Confirmation still requires semantic
  output identity and mode readback.
- Only one helper request runs at a time. Poll jobs carry a generation and are
  dropped after close; action jobs remain bounded and are never repeated by a
  timer. Poll de-duplication is keyed on the whole request, not the action
  name, so two polls that differ only in the operation they name stay distinct.
  Queue overflow discards background reads only: an action job carries a
  callback that owns a busy flag, and dropping it would leave the control that
  set the flag disabled with nothing left to clear it.
- Confirmation sheets select Cancel first. Esc cancels the sheet before the
  panel. Text fields block the cursor action surface. No letter key mutates
  state. The cursor must be revealed before Enter can act: the first arrow
  press only shows it, and Enter on a freshly opened panel does nothing. The
  view tab row does not carry the row cursor, so only one cursor is visible.
  Moving the cursor scrolls the target row into view. Pointing at a row moves
  the cursor to it but does not arm it, because a panel can open under a
  resting pointer.
- The cursor addresses rows by a stable key rather than by position. Rows
  appear and disappear under it — a cancel row when a pairing request opens, a
  Sunshine row when Sunshine stops, a host list emptied by a re-scan — and an
  index captured before one of those names a different action afterwards. When
  the list changes shape the cursor follows the row it named; when that row is
  gone the cursor is disarmed, because whatever now occupies the position is
  not something the owner chose.
- One expression per row decides whether it is available, and both the control
  and the keyboard path read it. Two expressions eventually disagree, and the
  disagreement is always in the dangerous direction: a control that looks
  unavailable but still fires.
- A control that cannot act is visually distinguishable from one that can. The
  host kit paints a disabled control identically, so a gate that only stops the
  click tells the owner nothing.
- Reachability and the last action result are separate lines. A background
  poll failure never overwrites what the owner just did, and transport errno
  text is mapped to a plain statement of what happened. Approval polling runs
  once a second and writes the action line only when the request itself
  changes; rewriting it on every poll would erase what the owner last did.
- Status distinguishes a confirmed failure from an absence of knowledge.
  Nothing polls while the panel is closed, so the last reading ages out and is
  reported as stale rather than presented as current. The bar is lit only for a
  paired host that refused or dropped the last request; an unpaired client and
  an aged-out reading are dimmed. Elapsed time is driven by a ticking clock,
  because a clock read inside a binding is not a reactive dependency and the
  test would never re-evaluate.
- The host's own rate limiter is respected rather than provoked. The approval
  timer does not fire on start, since the request that created the pending
  record has only just returned, and a retry delay the host sends back is
  honoured before the next poll.
- Controls are disabled by a pending mutation, never by a background poll, so
  a two-second refresh does not flicker every button.
- The helper result settles when the process exits, with both streams
  collected, so a helper that cannot start reports why. A command that cannot
  be executed emits neither a start nor an exit, so the job is also reaped when
  the process stops running without having started; otherwise the single lane
  stays claimed and every gated control is disabled for the life of the shell.
  A watchdog escalates from terminate to kill, and releases the controls even
  if neither works, so a wedged helper cannot hold the panel hostage.
- Suspend, restart, and shutdown each use a separate confirmation message and
  fixed protocol action. A method return is shown as requested, never as proof
  that the physical transition completed.
- Mode rows are ordered largest area first, then highest refresh rate, with
  the current mode pinned to the top. Selection is held by mode identity, not
  by list position, so a host-side reordering cannot move the selection to a
  different resolution. The output/mode payload is reassigned only when it
  actually changed, so a two-second poll does not rebuild the list under the
  pointer. That comparison covers the inventory alone: the live preview block
  carries a deadline that moves every poll, and including it would rebuild
  every row every two seconds. Pinning the current mode applies only when the
  host actually names one, because pinning on an absent identifier makes every
  mode compare as the current one and yields a different order on each sort. When more than one output is advertised, the Display view carries
  an output row; a single output is stated as text instead.
- Display rows keep their own Preview, countdown, Save, and Revert controls
  together. Preview is shown only for the selected non-current mode. Save asks
  the host to confirm the visible picture and stores the last known-good mode;
  the current mode remains visible even when non-standard refresh filtering is
  enabled. An active-preview fallback card keeps Save and Revert available
  while Steam re-enumerates a mode or the selected row is temporarily hidden.
- Editable listener settings are treated as a draft. Background status refresh
  never overwrites a dirty draft; Save commits it and Cancel restores the last
  server value.
- Sunshine monitoring is a Decky-owned setting. Omarchy shows the cached
  status and restart action only when Decky reports monitoring enabled; a
  disabled host monitor produces no Sunshine control and no separate client
  poll.
- Multiple monitors may instantiate the widget, but the server-side
  `request_id` and host mutation lane prevent duplicate remote mutations.

No custom visual primitive is used. Product rows are composed from host kit
components and `qs.Commons` tokens.
