# SteamOS Companion protocol v1

This directory is the pinned client copy of the v1 contract. The Decky project
under `/home/hvo/Projects/steamos-companion-decky/protocol/` remains the source of
truth; this copy is reviewed and bundled with the Omarchy client so runtime
behavior never depends on a sibling checkout.

Pinned to Decky `main` at `0219c27` (fourteen commits after `v0.5.18`). Vendored:
`schema.json`; `operation-vocabulary.json`; this file, where everything after
this note is Decky's text unchanged, including its "DRM link facts", "Operation
journal", "Diagnostic report" and "Credential scopes and the remote-access lock"
sections; the flat `fixtures/*.json` that the tests load;
`fixtures/diagnostic-report.json`, the cross-client vector for the diagnostic
report; `fixtures/contract/status/`, the captured `GET /v1/status` exchanges,
including `view-only-credential.json` and `scopes-changed-by-owner.json`;
`fixtures/contract/drm/`, the captured `GET /v1/drm` exchanges;
`fixtures/contract/operations/list-*.json`, the captured `GET /v1/operations`
list exchanges; `fixtures/contract/display-outputs/available.json`,
`bridge-unavailable.json` and `with-active-preview.json`, the three captured
`GET /v1/display/outputs` exchanges that vector reads; and
`fixtures/contract/power/forbidden-after-owner-removed-scope.json`, the one
captured `POST /v1/power` exchange the tests use, the refusal after the owner
removed a scope. Not vendored: `fixtures/pairing-payload.json`, the other
exchanges under `fixtures/contract/operations/`, `fixtures/contract/display-outputs/`
and `fixtures/contract/power/`, and the rest of `fixtures/contract/` with its
`index.json`. `fixtures/display-order-*.json` are Omarchy-local.

The contract deliberately exposes only enumerated operations. It never carries
raw Steam payloads, arbitrary methods, PIDs, service names, paths, commands,
credentials, or Sunshine Web UI administration.

Mutations use a client-generated `request_id`. A `202 Accepted` response means
the host persisted the operation and may still be processing it. Clients query
`GET /v1/operations/{id}` with the same credential after disconnect instead of
creating a second request. `GET /v1/operations` returns the bounded operation
list, newest first, for the caller's credential plus host-owned display records
(`client_id: "host"`); it never includes another remote credential's records.
Query strings and request bodies are rejected on this route.

Pairing has two bootstrap forms. The normal form is a short authentication
string (SAS) that is **derived on both sides and never transmitted**. After
discovery and certificate pinning, the client generates a random 16-byte
`verification_nonce`, sends it base64url-encoded (unpadded, 22 characters) to
`POST /v1/pair/request`, and derives the 8-digit comparison code locally from
that nonce and the certificate fingerprint it pinned. The host derives the code
from the same nonce and its own certificate fingerprint. Both sides use:

```
code = scrypt(nonce,
              salt = b"steamos-companion:v1:pairing-sas:" + fingerprint,
              n = 2**14, r = 8, p = 1, dklen = 8)
       interpreted big-endian, modulo 10**8, zero-padded to 8 digits
```

`fingerprint` is the ASCII `sha256:<64 lowercase hex>` form of the DER
certificate digest. Because the fingerprint is part of the salt, a
TLS-terminating LAN relay — which necessarily presents its own certificate —
derives a different code than the real host, so the two screens disagree and
the owner sees the mismatch. The relay cannot repair this by choosing a nonce:
finding one that makes the real host display the relay's digits is an offline
search of the 10^8 code space, and scrypt at these parameters (~50 ms per
candidate, 16 MiB working set) puts that far outside the 120-second request
window. This is what the previous client-chosen `verification_code` form failed
to do, and that field is now rejected with `400 pairing_method_unsupported`.

The request also includes `X-SteamOS-Companion-TLS-Binding`, derived from the
exact TLS 1.2 channel used by the client. The host derives the same binding
from its accepted socket and rejects a mismatch before creating a pending
request. Regular authenticated traffic may still negotiate TLS 1.3. Decky
displays the derived short-lived code beside the pending client for visual
comparison. The code is a verification string, not a bearer credential; the
owner must still explicitly approve the matching request before the host mints
a token. The host stores only a hash of the nonce and the derived display
string, and runs the derivation once per request — never per stored record.

The first successful request returns a high-entropy `pairing_session` handle.
The client sends that handle on subsequent approval polls, so a new TLS
connection does not require re-deriving the code. The handle is stored only as
a hash on the host and expires with the pairing request.
The requesting client may cancel its pending request with
`POST /v1/pair/cancel`, authenticated by its private verification nonce and
optional session handle. The host returns the terminal state idempotently, and
owner rejection is reported to the client as `pairing_rejected` instead of an
ambiguous missing request.
The advanced form sends `pairing_id` and a short-lived secret from the full
payload. The legacy host-issued `pairing_code` form remains accepted for
compatibility. Verification nonces expire after 120 seconds and are consumed
with the approved request. The approved response also carries a read-only
`wake_target` object with the host NIC MAC; the client saves it automatically
and chooses its own active LAN interface for sending the magic packet.

The authenticated status response exposes the same wake target and capability
values for `suspend`, `restart`, and `shutdown`. Power commands remain fixed
verbs; they do not accept arbitrary system methods or shell commands.

For display recovery, `POST /v1/display/save-current` accepts exactly
`{request_id, output_id, generation, visible:true}`. The host requires the
owner confirmation, refuses the request while a preview is active, reads the
current mode from the live Decky bridge, verifies the output identity and
generation, and only then persists the mode as the recovery profile. A client
must never infer a recovery profile from an inventory read alone.

## Remote Gaming Mode display order

New hosts may expose `GET /v1/display/order` as an additive resource under
the existing v1 protocol. It is authenticated with `status.read` and returns
`display_order` with a fresh `generation`, `observed_at`, ordered opaque
`output_keys` for the current inventory, bounded output records, the saved
priority in `saved_output_keys`, independent restart flags, an adapter
identifier, and explicit unsupported/stale/ambiguous or previous-reading
reasons. The output record exposes only a friendly display name, connector
label, connected state, and active readback; it is not a Gamescope command
interface.

The client submits only host-issued opaque keys and the observed generation to
`POST /v1/display/order`, with `restart:false` for **Save for next session** or
`restart:true` for **Save and restart Gaming Mode**. The latter is shown only
after confirmation and targets the fixed user `gamescope-session.target` route
on the host. `POST /v1/display/order/automatic` is the explicit reset that
removes only plugin-owned order configuration. The host re-enumerates and
resolves every key, rejects duplicate or stale input, writes atomically, and
returns an operation ID. The client never replays an unresolved restart; it
reconciles that ID after reconnect.

## Contract fixtures

`fixtures/contract/<area>/<case>.json` are captured wire exchanges for client
test suites (Android, Omarchy). Each file is:

```text
{"case": "<area>/<case>", "description": "...",
 "request":  {"method": "...", "path": "...", "headers": {...}, "body": {...} | null},
 "response": {"status": 200, "body": {...}}}
```

`request.headers` lists only protocol headers (`Authorization`,
`X-SteamOS-Companion-TLS-Binding`); transport headers are omitted.
`request.raw_body` appears only when the case sends bytes that are not JSON.
`response.headers` appears only when it carries protocol meaning (currently
`Retry-After` on 429). Every error body is `{"error": <code>, "message": ...}`.
`index.json` lists every case with its method, path, status, and error code.

The files are generated, never hand-written: `tests/contract_fixtures.py`
feeds raw HTTP/1.1 requests through the production `server._Handler` (error
envelope, query-string rejection, `Retry-After`) into `HostService.handle_http`,
using the fakes and fixed clocks from `tests/test_host.py`. Every request has a
LAN peer address, so the first pairing request needs a matching TLS binding
and later polls authenticate with `pairing_session` over a new (TLS 1.3)
connection. Preconditions a client cannot create over the wire (owner approval
or rejection, host reload, a seeded queue of 32 pending requests) are applied
through the host's own Decky-side methods or state and described per case.

Deterministic placeholders replace values that are random or host-specific in
production. They keep the production shape, so they parse the same way:

| Field(s) | Fixture value |
| --- | --- |
| `host_id`, `pairing_id`, operation `id`/`preview_id`, profile `id` | `<prefix>fixtureNNNN` padded with `x` to 24 characters after the prefix |
| `credential.token`, `pairing_session`, payload secret, `Authorization` bearer | `fixture-token-NNNN-` padded with `x` to 43 characters |
| all ISO timestamps (`created_at`, `updated_at`, `verified_at`, `observed_at`, `checked_at`) | fixed clock `2026-09-13T12:00:00.250000Z`, advanced only where a case needs expiry |
| `expires_at`, preview `deadline` (epoch seconds) | the same fixed clock plus the host's window |
| `certificate_fingerprint` | `sha256:` + 64 × `a` (so the SAS for nonce `AAECAwQFBgcICQoLDA0ODw` is `3969-3759`, as pinned in `tests/test_protocol.py`) |
| `X-SteamOS-Companion-TLS-Binding` | base64url of the fixed 12-byte `tls-unique` value `fixture-tls1` |
| legacy `pairing_code` | `4821-3579` |
| `boot_id`, `uptime_seconds`, `cpu_temperature`, `wake_target` | fixed values |
| `self_check.versions.plugin`, `self_check.certificate.not_before` / `not_after` | `0.0.0-fixture` (never the release number, so a version bump cannot drift the fixtures), `2026-09-13T11:59:00Z`, `2036-09-10T11:59:00Z` |
| `steam_age_ms`, `sunshine.age_ms` | `0` (fixed monotonic clock) |
| `gamescope_output_update_failed` / restart-failure message detail after `user systemd command failed:` | text from the fake `systemctl` runner |

Regenerate after any intended host or protocol change, and review the diff:

```sh
python3 -m tests.contract_fixtures --write
python3 -m tests.contract_fixtures --check   # exit 1 on drift
```

`tests/test_contract_fixtures.py` regenerates in memory and requires byte
equality with the checked-in files, so these fixtures cannot drift from the
host implementation.

## Host self-check

The authenticated `GET /v1/status` response carries an additive `self_check`
object (`status.read`, no new route or scope). It is a read-only account of what
the host already knows about itself, for a health card and a version nudge; it
never carries credentials, MACs, pins, host or boot identifiers, or pairing
material, and a contract test asserts that.

| Key | Facts |
| --- | --- |
| `certificate` | `ready`; `fingerprint_short` (last 8 hex digits, the form pairing shows as "…xxxxxxxx"); `not_before` / `not_after` as UTC `YYYY-MM-DDTHH:MM:SSZ`, each `null` when unreadable |
| `listener` | `bound`, `address`, `port` |
| `bridge` | Steam frontend bridge `ready` and the fixed `methods` it currently offers |
| `temperature` | `source`: `steam`, `hwmon`, or `unavailable` |
| `provider` | Sunshine `monitor_enabled`, `state`, `stale`, and the owner's `contract_version` (`null` while monitoring is off) |
| `versions` | `protocol` and the Decky `plugin` version; `plugin` is the literal `"unknown"` when the host cannot read it, never a guess |

Clients must treat the object, and every field in it, as optional: a host that
predates the block omits it. A client compares `versions.protocol` with the protocol it
speaks and renders the mismatch itself; the host keeps no peer registry and
probes nothing. The block is bounded by `schema.json` (`$defs/self_check`,
`additionalProperties: false`), so an extension is a schema change.

## DRM link facts

`GET /v1/drm` (`status.read`; no body, no query string) returns the connector
facts the kernel publishes for each physical output, so a client can warn before
a preview that picks a resolution the display link never listed. It is advisory:
it never blocks a selection, and an older host answers `404 not_found`, which a
client treats as "no facts", not an error.

The body is `{protocol_version, drm: {available, observed_at, reason, connectors}}`.
`available` is `false` with a `reason` and an empty list when the host cannot
read any DRM connector. Each connector carries `connector`, `gpu_id`
(`drm:cardN`), `display_name`, `monitor_vendor` and `monitor_product_id` (parsed
EDID identity, `null` when unreadable), `is_internal`, `connected`,
`resolutions` and `steam_output_id`. There are at most 16 connectors and 256
resolutions each. No raw EDID bytes, serial numbers or MACs are ever included,
and a contract test asserts the key allowlist.

- **Resolutions only.** The DRM `modes` attribute has no refresh rates, so
  `resolutions` is a deduplicated list of `"<width>x<height>"` strings and a
  client must not draw any conclusion about refresh rate from it.
- **The host does the join.** `steam_output_id` is the id of the display-bridge
  output (`GET /v1/display/outputs`) that this connector corresponds to, matched
  by connector name. It is `null` when the connector is disconnected, when the
  same connector name is connected on two GPUs, when Steam reports the name
  twice, or when the bridge has not reported. A client never matches by name
  itself.
- **When a client may warn.** Only for the connector whose `steam_output_id`
  equals the selected output's `id`, when it is `connected`, not `is_internal`,
  and its `resolutions` list is non-empty, and the selected mode's
  `"<width>x<height>"` is not in that list. Anything else (unjoined,
  unavailable, empty list, internal panel, older host) shows nothing. The text
  says the resolution is not in the display link's mode list and that it may
  still work; it must not call the mode invalid. Internal panels are skipped
  because Steam exposes scaled modes the connector list does not.
- **Fetch cadence.** Once per display interaction (and when the output topology
  changes), never with each status poll.

## Operation journal

`GET /v1/operations` (any valid credential; no body, no query string) returns
`{protocol_version, operations}`, newest first by `updated_at` (ties by `id`,
descending), at most 256 records. A credential sees its own operations plus the
Deck's own display records: kinds `display.selection.*` and `display.preference`
written by Decky itself (owner `decky-local`; `host` is accepted for records an
older build wrote). Another credential's records, and host-internal records of
any other kind (`display.session-restart`, `sunshine.restart`), never appear.
`GET /v1/operations/{id}` stays narrower: it serves only the caller's own
records, so a journal row for a Deck-owned record has no by-id read.

Records are the `operation` definition in `schema.json`. `target` is the host's
account of what was requested (output and mode ids, connector names, a profile
id, a power action) and `outcome`, `restore_state`, `restored` and `resolved_by`
are what the host recorded. None of it is proof of a visible picture: a
`succeeded` record means the host finished what it set out to do, and only the
outcomes whose code contains `visible` carry the owner's confirmation.

`operation-vocabulary.json` is the shared wording for every client: a label for
each `state`, `kind`, `outcome` and `restore_state` the host can record. A host
test fails when the host starts recording a code the vocabulary lacks, and a
client test fails when its embedded wording drifts from the file. A client shows
an unknown code as sanitised text rather than hiding or guessing at it.

Clients present the journal as diagnostics, not as status:

- Read on entry to the diagnostics view and on an explicit refresh (spaced at
  least five seconds apart). Never with the status poll, never on a timer.
- Optional and advisory: a host that predates the route, a rejected request or
  a malformed reply shows "not available", never an error, and never changes
  connection state.
- Rebuild each record from an allowlist and bound what is shown. Never persist
  or export the records.
- Show the list with a standing note that a completed entry does not by itself
  show the picture is visible.

The `operations/list-*` fixtures are generated like the rest: `list-mixed`
holds the caller's records, two Deck-owned display records and one record each
that must not appear (another client's power operation and a host
session-restart).

## Diagnostic report

Each client's explicit "export diagnostics" action can include a **report** of
the paired host for pasting into an issue. It is built only from response bodies
the client already read (`GET /v1/status`, `/v1/display/outputs`, `/v1/drm`,
`/v1/operations`) and adds no route. The export is a user action: the client
shows the text before it is copied or shared, and nothing is sent anywhere.

`fixtures/diagnostic-report.json` is the cross-client vector. Each case gives
the four response bodies (a contract fixture path or an inline body, or `null`
for "not read") and the exact `report` every client must rebuild from them.
`tests/diagnostic_report_vectors.py` generates it from the reference
implementation, `host/backend/diagnostics.py`; never hand-edit it.

```text
{"format": "steamos-companion-report-v1",
 "host":    null | {plugin_version, protocol_version, certificate, listener, bridge,
                    temperature_source, sunshine},
 "outputs": null | {available, count, outputs: [...], saved_profiles, preview_active},
 "link":    null | {available, reason, connectors: [...]},
 "journal": null | {count, truncated, records: [...]}}
```

A section is `null` when it was not read or the host does not offer it, and
present-but-empty when the host answered with nothing, so an older host is
distinguishable from an empty one.

Redaction (applied identically by every client):

- **Only named facts are copied, never a record.** No operation, request or
  preview id, no profile id, no host or boot identifier, no endpoint or listener
  address, no certificate pin or its short form, no wake
  MAC, no credential or pairing material, no EDID bytes, serial or display name
  beyond the Steam output's own name and description.
- **Free text is scrubbed, then bounded.** Control characters become a space,
  everything outside printable ASCII is dropped, runs of spaces collapse, then in
  order: URLs `[url]`, `sha256:` pins `[pin]`, MACs `[mac]`, IPv4 (with optional
  port) and IPv6 `[ip]`, absolute paths of two or more segments `[path]`, and
  opaque tokens (24 or more of `[A-Za-z0-9_-]` containing a digit) `[token]`;
  only then is the text cut to its limit, so a token is never cut into a
  fragment that escapes the rule. Free text is journal `reason` (160), output
  `name` (32) and `description` (48), link `reason` (96) and journal target
  strings (48, list entries 32).
- **Identifiers are matched whole.** Every token and code pattern must match the
  entire value (a trailing newline does not pass), and a journal record needs a
  valid `id`, `kind` and `state` even though its `id` is never copied.
- **Journal `target`** keeps only these keys, in this order: scalars
  `output_id`, `mode_id`, `generation` (integers under 2^31), `action`,
  `restart`, `automatic`, `connector`, `output_key`, `target_output_key`,
  `baseline_output_key`, `target`; string lists `connectors`, `output_keys` (at
  most 4 entries); and `mode` as `"<w>x<h>[@<hz>]"`.
- **Modes** are `"<w>x<h>"` with `@<hz>` when a finite refresh rate between 0 and
  1000 is known, formatted to two decimals with trailing zeros removed (`59.94`,
  `60`, `59.9`).
- **Bounds:** 8 methods, 8 outputs with 24 modes each, 8 connectors with 32
  resolutions each, 20 journal records (`count` and `truncated` report what was
  cut). If the compact JSON still exceeds 12 KiB, journal records are dropped
  from the oldest end until it fits and `truncated` is set.
- **Never fail:** a malformed body yields a null section, not an error.

Vector cases marked `"strict": true` use only bodies the wire schema allows
(a Decky test enforces it) and journal targets of at most eight keys, so every
compliant client's own decoder reaches them, and they must match exactly. `hostile-shape` is
`"strict": false`: the body is malformed, so a client whose own decoder refuses
it may report that section as `null`, but it must not fail and must not repeat
any identifier, address, pin or token from the input.

## Credential scopes and the remote-access lock

A paired credential holds some of three scopes. `status.read` is always held and
guards every read (`GET /v1/status`, `/v1/display/outputs`, `/v1/display/order`,
`/v1/drm`). `power.control` guards `POST /v1/power` and `POST /v1/sunshine/restart`.
`display.control` guards every `POST /v1/display/*`. `sunshine.control` exists in the
host but is never grantable. `GET /v1/operations`, `GET /v1/operations/{id}` and
`POST /v1/pair/revoke-self` need only a valid credential, so a view-only client can
still read its own journal and remove its own pairing.

**View only.** The owner may approve a pairing with fewer scopes than it asked for;
`status.read` alone is the view-only role. The delivered `credential.scopes` is the
approved list, not the requested one.

**Editing after pairing.** The owner changes a paired client's scopes on the host
at any time. The very next request is judged by the new list and a missing scope is
`403 forbidden`. When a scope is removed the host cancels that client's queued
operations of that kind (`accepted`, not yet sent: `power.` and `sunshine.` for
`power.control`, `display.` for `display.control`). Work already sent is not
recalled, and a display preview the client already started is reverted by the
host's own deadline because the client can no longer confirm it. A cancelled
operation is never sent to Steam, even when its worker was already waiting for
its turn behind another operation.

**What the credential holds.** `GET /v1/status` carries an optional
`credential` block, present only for a client credential:

```text
"credential": {"scopes": ["status.read", "power.control"]}
```

It lists the grantable scopes the credential holds now, in the order above. An older
host omits the block; a client then keeps what it stored at pairing and treats
nothing as changed. A client adopts the reported list, drops any value it does not
know, and may use it to say what the pairing can do before the user hits a `403`.
It never widens what a client believes from any other field.
Fixtures: `status/view-only-credential`, `status/scopes-changed-by-owner`,
`power/forbidden-after-owner-removed-scope`.

**Remote-access lock.** The owner's panic switch on the host closes the listener
and keeps it closed across restarts, so no device can discover, connect, read
status or pair until the owner unlocks it. Locking also expires pending pairing
requests and cancels every paired client's queued power, Sunshine and display
operations. Operations already sent and the host's own local work are untouched.
Pairings and credentials are kept: after the owner unlocks, every client works
again with the access it had. On the host, creating or approving a pairing while
locked is refused (`409 host_locked`), and a request that still reaches the
handler is answered `503 host_locked`; that is a safety net, not something a client
should rely on. A client sees a refused or timed-out connection, which looks the
same as a host that is off, so it must not say its pairing was removed.
