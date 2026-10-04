# Decky Sunshine provider compatibility

Status: guarded Decky Loader owner bridge implemented; installed host/provider
runtime validation remains open.

The host accepts one narrow adapter with two calls:

```python
get_status() -> {"running": bool, "reason": str?}
ensure_running() -> {"accepted": bool, "outcome": str?}
```

The remote adapter issues no recovery from `get_status`; it must not stop a
running process, accept a PID/path/service/command from the LAN, or replace
Decky Sunshine's ownership of launch configuration and crash recovery. The
inspected owner implementation may nudge its own existing watchdog as part of
`is_sunshine_running`; verify that coordination on the target. `ensure_running`
is called only by the authorized, serialized host operation after a fresh
`stopped` observation.

## Actual M0 checks

| Check | Result |
|---|---|
| Omarchy/Quickshell versions | Omarchy `4.0.3-1`; Quickshell `0.3.1` |
| Local Decky executable | `decky` not present |
| Local Decky Sunshine executable | `decky-sunshine` not present |
| Supported cross-plugin method | Source uses a guarded Decky Loader `loader/call_plugin_method` bridge; target-host validation is still required |
| Remote fallback | None; the implementation intentionally does not guess a process, service, path, or shell command |

## Host-side contract behavior

`Monitor Sunshine` is off by default. When disabled, the monitor performs no
provider calls and reports `state: disabled`. When enabled, the host samples
one provider with a 2-second call bound, every 5 seconds, and marks a cached
sample unknown after 15 seconds. A provider exception or timeout is never
translated into `stopped`.

The client-facing restart route requires all of the following at dispatch:

- monitoring is enabled;
- the provider is connected and compatible;
- the caller is a normally paired client with `power.control`;
- a fresh provider sample says `stopped`;
- no display preview or conflicting mutation owns the lane.

Sunshine is not a separate client pairing scope. Decky owns the monitoring
setting and rechecks it before dispatch; the Omarchy client only renders the
status and restart action that Decky exposes. The Decky frontend first probes
the owner plugin named `Decky Sunshine` through the guarded loader bridge and
then delegates `is_sunshine_running` and `restart_sunshine`; it never launches
or kills a local process. The legacy `sunshine.control` label is accepted in
old stored credentials for migration, but is not requested or required by new
pairings.

The operation is bounded to 30 seconds of post-dispatch observation. It
returns success only after a fresh `running` sample, or `already_running` if
the owner recovered the process before the serialized ensure call. Disable or
revocation cancels queued recovery; a dispatched call is not replayed.

The same status response carries `self_check.provider` (`monitor_enabled`,
`state`, `stale`, and the owner's `contract_version`, `null` while monitoring is
off). Omarchy shows it under Settings, Connection details, Host health, which is
a convenient place to read the contract version during the validation below. A
`running` state reported with `stale: true` is shown as stale, not as confirmed
running.

## Host validation still required

On the SteamOS reference host, verify that the guarded loader call reaches the
installed Decky Sunshine methods and record the tested owner version and
contract (`loader-call-v1`). Then exercise disabled, missing, timeout,
intentional stop, crash, watchdog race, owner unload/update, recovery failure,
duplicate click, and revocation cases. Until that evidence exists, the source
integration is implemented but the release is not a claim that every
installed Decky Sunshine build is compatible.
