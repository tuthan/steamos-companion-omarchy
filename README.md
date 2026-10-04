# SteamOS Companion — Omarchy client

SteamOS Companion is a two-client system that shares one v1 protocol:

- **Decky plugin** — runs on SteamOS as a **Client**, **Server**, or **Both**.
- **Omarchy client** — this repository's Linux desktop bar widget and client.

The Omarchy client discovers and pairs with a SteamOS Companion Decky host,
shows its status, wakes it, sends power commands, and provides display recovery.
Sunshine status and restart are shown only when monitoring is enabled on the
host. It pairs with a Decky host in **Server** or **Both** mode.

The Decky host is maintained separately in
[tuthan/steamos-companion-decky](https://github.com/tuthan/steamos-companion-decky).

**Website:** <https://steamos-companion.atas.tech/>

SteamOS Companion is an independent open-source project and is not affiliated with or endorsed by Valve Corporation.

- Plugin ID: `io.github.tuthan.steamoscompanion`
- License: [MIT](LICENSE)

## Screenshots

![SteamOS Companion Omarchy host view](preview.png)

![SteamOS Companion Omarchy display view](assets/res.png)

![SteamOS Companion Omarchy display order view](assets/screen-order.png)

![SteamOS Companion Omarchy settings view](assets/settings.png)

## Install

Install this client on a Linux desktop running Omarchy from a reviewed checkout
or repository:

```sh
omarchy plugin add https://github.com/tuthan/steamos-companion-omarchy.git --enable
```

The Decky plugin must be installed and running as **Server** or **Both** on the
SteamOS device you want to control before pairing. The Omarchy client is
directory-installed and does not use the Decky ZIP or checksum artifact.

## Pair and use

1. Open the widget’s **Settings** and choose **Find hosts**.
2. Select the intended host, choose **Request pairing**, compare the code with
   the one shown by the Decky plugin, and approve it on the Deck.
3. Use **Host** for status, wake, suspend, restart, and shutdown.
4. Use **Display** to preview, save, or revert a display mode. When the host
   exposes display-order control, you can also reorder connected outputs, save
   that order for the next session, or save it and restart Gaming Mode after
   confirmation.
5. Open **Settings → Connection details → Host health** to see what the host
   reports about itself: certificate validity, listener, Steam bridge,
   temperature source, Sunshine, and plugin and protocol versions. It reads the
   status the open panel already polls. A host on an older Decky plugin does not
   report it, and the panel says to update that plugin.
6. On **Display**, selecting a resolution the display link never listed shows an
   advisory above Preview. It only warns: the list comes from the connector
   facts the host reads from the kernel, has no refresh rates, and a resolution
   it omits may still work. **Settings → Connection details → Link facts** names
   the connector behind the display. An older host gets no advisory, and the
   group says the facts are not available.

The host listens on all IPv4 interfaces by default. Set its host/IP override
only when it has multiple network interfaces. Sunshine monitoring is disabled
by default and is controlled from Decky settings. When it is disabled, the
client does not show Sunshine controls or request a separate Sunshine poll;
the normal status read remains the source of truth for all remote capabilities.

## CLI evidence tool

The checkout includes an executable `steamos-companion-ctl` wrapper. It uses the
same `ClientCore` and private pairing state as the Omarchy panel, so run it from
a reviewed checkout or the installed plugin directory; it does not create a
second credential store. Global output flags go before the command.

```sh
./steamos-companion-ctl --json pair discover
./steamos-companion-ctl --json pair start --host https://HOST-IP:PORT
./steamos-companion-ctl --json pair poll
./steamos-companion-ctl --json inspect
./steamos-companion-ctl --json status
./steamos-companion-ctl --json outputs
./steamos-companion-ctl --json display link-facts
./steamos-companion-ctl --json display save-current --output-id OUTPUT_ID --generation GENERATION
./steamos-companion-ctl --json journal get OPERATION_ID
```

`display save-current` prompts for a typed confirmation after asking the
operator to check the physical picture. In a non-interactive script, first
check that picture and pass both `--yes --acknowledged`; the assertion is still
sent only by this explicit command. A successful response includes the host
operation and read-back profile. Record the operation ID, then use `journal get` to reconcile it. Use `--plain` instead of `--json` for line-oriented human
output. Errors are emitted as a JSON object by default.

`journal list` uses the additive `GET /v1/operations` route. Hosts that only
implement the pinned `v0.5.16` protocol can still serve `journal get`, but need
the matching host update before the list command is available. A 404 means the
host build does not expose the list route; it is not acceptance evidence.

`display link-facts` reads the advisory DRM connector facts (resolutions only).
A host without the route, or one that answers badly, is reported as
`"drm": null` with an `unavailable` reason and exit 0, because the facts only
ever feed a warning.

The wrapper is included in the plugin checkout but is not installed as a
system-wide command by the plugin manager.

## Security

The client requires the system `python3` interpreter and uses only its standard
library. The bundled helper sends credentials only over HTTPS after checking
the paired host's certificate fingerprint. Compare the pairing codes on both
devices before approving a new host.

Credentials and pending requests are stored under
`${XDG_STATE_HOME:-~/.local/state}/steamos-companion` in private files. Symlinked
state directories/ancestors and unsafe shared paths are refused. Network data
and helper output have explicit size limits; oversized responses are rejected.

See the [marketplace feedback review and release checklist](docs/security-review.md)
for the checked patterns, regression tests, and remaining validation limits.

## Local development

Validate the plugin from this directory:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
omarchy plugin validate .
qmllint Panel.qml
```

To install the current checkout for testing, sync it into Omarchy’s real
plugin directory and rescan the shell:

```sh
plugin_dir="$HOME/.config/omarchy/plugins/io.github.tuthan.steamoscompanion"
mkdir -p "$(dirname "$plugin_dir")"
if [ -L "$plugin_dir" ]; then unlink "$plugin_dir"; fi
mkdir -p "$plugin_dir"
rsync -a --delete --exclude='.git/' ./ "$plugin_dir"/
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.tuthan.steamoscompanion --section right
```

Repeat the `rsync` and rescan commands after changes.

## Remove

```sh
omarchy plugin disable io.github.tuthan.steamoscompanion
omarchy plugin remove io.github.tuthan.steamoscompanion
```

Removing the plugin does not remove its pairing credentials. Use **Forget
local pairing** in the widget settings if those credentials should also be
deleted.
