# SteamOS Companion protocol v1

This directory is the pinned client copy of the v1 contract. The Decky project
under `/home/hvo/Projects/steamos-companion-decky/protocol/` remains the source of
truth; this copy is reviewed and bundled with the Omarchy client so runtime
behavior never depends on a sibling checkout.

The contract accepts only enumerated routes and operations. It does not carry
raw Steam payloads, arbitrary methods, PIDs, service names, paths, commands,
credentials, or Sunshine Web UI administration.

## Pairing bootstrap

The normal bootstrap is a short authentication string that is derived on both
sides and never transmitted. After bounded LAN discovery and certificate
pinning, the client generates a random 16-byte `verification_nonce`, sends it
base64url-encoded (unpadded, 22 characters) to `POST /v1/pair/request`, and
derives the 8-digit comparison code locally from that nonce and the
certificate fingerprint it pinned. The host derives the code from the same
nonce and its own certificate fingerprint. Both sides compute:

```
code = scrypt(nonce,
              salt = b"steamos-companion:v1:pairing-sas:" + fingerprint,
              n = 2**14, r = 8, p = 1, dklen = 8)
       interpreted big-endian, modulo 10**8, zero-padded to 8 digits
```

`fingerprint` is the ASCII `sha256:<64 lowercase hex>` form of the DER
certificate digest. Because the fingerprint is part of the salt, a
TLS-terminating LAN relay presents its own certificate and therefore derives a
different code than the real host. The two screens disagree and the owner sees
the mismatch. The relay cannot repair this by choosing a nonce: finding one
that makes the real host display the relay's digits is an offline search of the
10^8 code space, and scrypt at these parameters costs roughly 40 ms and a
16 MiB working set per candidate, which puts that search far outside the
120-second lifetime of a pending request.

The earlier form, in which the client chose the 8-digit code and sent it, did
not have this property: a rogue listener could forward the received code to the
real host over its own connection and both screens would agree. That field is
now rejected by the host with `400 pairing_method_unsupported`.

The client also sends a TLS 1.2 `X-SteamOS-Companion-TLS-Binding` header. Decky
derives the same value from the accepted socket and rejects a mismatch before
creating the pending request. The host creates a pending request and the owner
must approve it in Decky. The first pending response also contains a
high-entropy `pairing_session` handle, which the client reuses for approval
polling. The full payload form remains supported as an advanced fallback, and
the host-issued `pairing_code` form remains accepted for compatibility.

Approval polling is a sequence of short client calls rather than one long call.
The in-flight request, including the nonce or payload secret, lives in the
client's private state directory and never travels back out to the panel
process, so the panel can be closed and reopened while approval is pending.

The approved response carries a read-only `wake_target` object with the host
NIC MAC; the client saves it automatically and chooses its own active LAN
interface for the magic packet. Authenticated status refreshes the target for
older paired clients.

## Operations

Power uses only the fixed `suspend`, `restart`, and `shutdown` actions. Display
generation identifies the output/mode inventory, not the current mode, so the
normal mode switch and Steam's mode-ID re-enumeration do not invalidate a live
preview before semantic readback confirmation.

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
