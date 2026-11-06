# Decision: clean rename to SteamOS Companion, no compatibility

- Status: accepted
- Date: 2026-09-16
- Scope: this repository (the Omarchy client). The shared decision, with the full
  identifier table, is `docs/decisions/clean-rename.md` in
  `tuthan/steamos-companion-decky`.

## Decision

The project began as SteamOS Remote and was renamed SteamOS Companion before any
public release. Every identifier moved at once and nothing is carried over:

- No mixed-version support between a renamed host and a pre-rename client.
- No migration of pairings, credentials or settings between the namespaces.
- No transitional plugin ID. Protocol version stays `1`; it is a new pre-public
  namespace.

Upgrading means removing the old plugin, installing this one and pairing again.

## What changed here

| Surface | Former identifier | Current identifier |
| --- | --- | --- |
| Omarchy plugin ID and QML module | `io.github.tuthan.steamosremote` | `io.github.tuthan.steamoscompanion` |
| Repository | `tuthan/omarchy-steam-remote` | `tuthan/steamos-companion-omarchy` |
| Private client state | `${XDG_STATE_HOME:-~/.local/state}/steamos-remote` | `${XDG_STATE_HOME:-~/.local/state}/steamos-companion` |
| Service marker the client looks for | `SteamOSRemote/1` | `SteamOSCompanion/1` |
| Pairing payload prefix | `steamos-remote:v1:` | `steamos-companion:v1:` |

Old Git tags and releases are historical records and were not rewritten. This
repository has one release tag, `0.5.14`, with no `v` prefix; use `v0.5.x` for
future tags to match the Decky repository.

## Consequences

- Remove the old plugin and its local state before enabling this one, or the two
  coexist: `omarchy plugin disable` and `omarchy plugin remove` the former plugin
  ID, then delete its state directory. A hidden backup the Omarchy installer made
  under `~/.config/omarchy/plugins/` is yours to keep or delete.
- Pair again from **Settings -> Find hosts**; credentials are bound to the new
  namespace and cannot be copied.
- There is no rollback path except reinstalling a matching old Decky and Omarchy
  pair together with their own state. Never mix an old peer with a new one.

## Enforcement

`tests/test_naming.py` fails when any git-tracked file, or file name, contains a
former identifier. It exempts itself and this record. The human-readable
two-word form of the old name is not gated, so the README upgrade note can use
it.
