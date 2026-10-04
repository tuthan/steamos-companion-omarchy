# SteamOS Companion protocol v1

This directory is the pinned client copy of the v1 contract. The Decky project
under `/home/hvo/Projects/steamos-companion-decky/protocol/` remains the source of
truth; this copy is reviewed and bundled with the Omarchy client so runtime
behavior never depends on a sibling checkout.

Pinned to Decky `main` at `51d92b3` (one commit after `v0.5.18`). Vendored:
`schema.json`; this file, where everything after this note is Decky's text
unchanged; the flat `fixtures/*.json` that the tests load; and
`fixtures/contract/status/`, the captured `GET /v1/status` exchanges. Not
vendored: `fixtures/pairing-payload.json` and the rest of `fixtures/contract/`
with its `index.json`. `fixtures/display-order-*.json` are Omarchy-local.

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
