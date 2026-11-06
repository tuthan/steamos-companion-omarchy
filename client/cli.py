#!/usr/bin/env python3
"""Command-line access to the paired SteamOS Companion host."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client_core import ClientCore, ClientError, _new_request_id, access_view, credential_scopes, validate_endpoint


_SECRET_FIELDS = {
    "token",
    "secret",
    "pairing_session",
    "verification_nonce",
    "certificate_fingerprint",
}
# One wording across clients: Decky asks "Is the picture on {device}'s display ...", and
# this client has no device name from the protocol, so the device is the paired host.
_CONFIRM_TEXT = (
    "Is the picture on the paired SteamOS host's display correct right now? "
    "This is what Restore saved display will return to."
)


class _ArgumentError(Exception):
    pass


def _request_id_argument(value: str) -> str:
    try:
        return _new_request_id(value)
    except ClientError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_request_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-id",
        type=_request_id_argument,
        help="name this request, so repeating the command with the same id and arguments returns the original operation instead of running it again",
    )


def _with_request_id(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Name the request, when the caller asked to, so the host can recognise a retry of it."""
    request_id = getattr(args, "request_id", None)
    if request_id is not None:
        request["request_id"] = request_id
    return request


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _plain(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        lines: list[str] = []
        for key in sorted(value):
            label = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_plain(value[key], label))
        return lines or [f"{prefix}: {{}}".strip()]
    if isinstance(value, list):
        if not value:
            return [f"{prefix}: []".strip()]
        lines = []
        for index, item in enumerate(value):
            label = f"{prefix}[{index}]" if prefix else f"[{index}]"
            lines.extend(_plain(item, label))
        return lines
    return [f"{prefix}: {value}".strip()]


def _writable(text: str) -> str:
    """Text the terminal can take: a locale that cannot show a separator gets an escape, not a failed command."""
    encoding = getattr(sys.stdout, "encoding", None)
    return text.encode(encoding, "backslashreplace").decode(encoding) if encoding else text


def _emit(value: dict[str, Any], output_format: str) -> None:
    safe = _redact(value)
    if output_format == "plain":
        access = safe.pop("access", None)
        lines = _plain(safe)
        if access:
            lines = [f"{access['label']}: {access['role']} \u00b7 {access['summary']}", *([access["note"]] if access["note"] else []), *lines]
        sys.stdout.write(_writable("\n".join(lines) + "\n"))
    else:
        sys.stdout.write(json.dumps(safe, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _confirmed(args: argparse.Namespace, prompt: str) -> None:
    sys.stderr.write(prompt + "\n")
    if getattr(args, "yes", False):
        if not getattr(args, "acknowledged", False):
            raise ClientError("--yes requires --acknowledged after checking the physical display")
        return
    if not sys.stdin.isatty():
        raise ClientError("interactive confirmation is unavailable; inspect the display, then use --yes --acknowledged")
    answer = input("Type YES to continue: ")
    if answer != "YES":
        raise ClientError("cancelled")


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="steamos-companion-ctl",
        description="Use the saved pinned pairing; --host is available only on pair start/payload.",
    )
    parser.add_argument("--json", dest="output_format", action="store_const", const="json")
    parser.add_argument("--plain", dest="output_format", action="store_const", const="plain")
    parser.set_defaults(output_format="json")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="read host status once")
    commands.add_parser("outputs", help="read the display inventory and saved profiles")
    commands.add_parser("inspect", help="show local pairing state without credentials")
    commands.add_parser("diagnostics", help="print a redacted report that is safe to paste into an issue")

    display = commands.add_parser("display", help="display recovery and output-order commands")
    display_commands = display.add_subparsers(dest="display_command", required=True)
    save = display_commands.add_parser("save-current", help="save the currently visible mode as recovery mode")
    save.add_argument("--output-id", required=True)
    save.add_argument("--generation", required=True, type=int)
    save.add_argument("--yes", action="store_true", help="skip the prompt after checking the picture")
    save.add_argument("--acknowledged", action="store_true", help="confirm the visible-picture assertion")
    _add_request_id(save)
    preview = display_commands.add_parser("preview", help="preview an advertised mode for the host deadline")
    preview.add_argument("--output-id", required=True)
    preview.add_argument("--mode-id", required=True)
    preview.add_argument("--generation", required=True, type=int)
    _add_request_id(preview)
    keep = display_commands.add_parser("keep", help="keep the active preview after checking the picture")
    keep.add_argument("--preview-id")
    keep.add_argument("--yes", action="store_true")
    keep.add_argument("--acknowledged", action="store_true")
    _add_request_id(keep)
    revert = display_commands.add_parser("revert", help="restore the active preview baseline")
    _add_request_id(revert)
    restore = display_commands.add_parser("restore", help="restore the saved recovery mode")
    restore.add_argument("--source", choices=("verified", "preview"), default="verified")
    _add_request_id(restore)
    display_commands.add_parser("order", help="read the Gaming Mode output order")
    display_commands.add_parser("link-facts", help="read the advisory DRM link facts (resolutions only; older hosts report none)")
    order_save = display_commands.add_parser("order-save", help="save host-issued output keys")
    order_save.add_argument("--output-keys", required=True, help="comma-separated host-issued keys")
    order_save.add_argument("--generation", required=True, type=int)
    order_save.add_argument("--restart", action="store_true")
    order_save.add_argument("--yes", action="store_true")
    order_save.add_argument("--acknowledged", action="store_true")
    _add_request_id(order_save)
    order_reset = display_commands.add_parser("order-reset", help="restore automatic Gaming Mode ordering")
    _add_request_id(order_reset)

    power = commands.add_parser("power", help="request a fixed Steam power action")
    power.add_argument("action", choices=("suspend", "restart", "shutdown"))
    power.add_argument("--yes", action="store_true")
    power.add_argument("--acknowledged", action="store_true")
    _add_request_id(power)

    wake = commands.add_parser("wake", help="send Wake-on-LAN to the saved host target")
    wake.add_argument("--mac")
    wake.add_argument("--interface")

    sunshine = commands.add_parser("sunshine", help="inspect or recover Sunshine")
    sunshine_commands = sunshine.add_subparsers(dest="sunshine_command", required=True)
    sunshine_commands.add_parser("status")
    _add_request_id(sunshine_commands.add_parser("recover"))

    journal = commands.add_parser("journal", help="read operation records")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    journal_commands.add_parser("list")
    journal_commands.add_parser("view", help="read the bounded journal the panel shows (older hosts report it unavailable)")
    get_operation = journal_commands.add_parser("get")
    get_operation.add_argument("operation_id")

    pair = commands.add_parser("pair", help="create, inspect, or cancel a pairing")
    pair_commands = pair.add_subparsers(dest="pair_command", required=True)
    pair_commands.add_parser("discover")
    start_pair = pair_commands.add_parser("start", help="request pairing from a discovered pinned host")
    start_pair.add_argument("--host", required=True, help="discovered HTTPS endpoint")
    start_pair.add_argument("--name", default="Omarchy CLI client")
    start_pair.add_argument("--scopes", default="status.read,power.control,display.control")
    pair_commands.add_parser("poll")
    pair_commands.add_parser("cancel")
    payload_pair = pair_commands.add_parser("payload", help="pair from a Decky payload read from stdin")
    payload_pair.add_argument("--host", help="optional endpoint override during pairing")
    payload_pair.add_argument("--name", default="Omarchy CLI client")
    payload_pair.add_argument("--scopes", default="status.read,power.control,display.control")
    return parser


def _scope_list(value: str) -> list[str]:
    scopes = [item.strip() for item in value.split(",") if item.strip()]
    if not scopes:
        raise ClientError("at least one pairing scope is required")
    return scopes


def _discover_pair_host(core: ClientCore, endpoint: str) -> dict[str, Any]:
    wanted = validate_endpoint(endpoint)
    result = core.run({"action": "discover"})
    hosts = result.get("hosts", []) if isinstance(result, dict) else []
    matches = []
    for item in hosts:
        if not isinstance(item, dict):
            continue
        try:
            item_endpoint = validate_endpoint(item.get("endpoint"))
        except ClientError:
            continue
        if item_endpoint == wanted:
            matches.append(item)
    if len(matches) != 1:
        raise ClientError("--host must match exactly one listener returned by LAN discovery")
    return matches[0]


def _preview_id(core: ClientCore, requested: str | None) -> str:
    if requested:
        return requested
    outputs = core.run({"action": "outputs"})
    preview = outputs.get("preview") if isinstance(outputs, dict) else None
    preview_id = preview.get("preview_id") if isinstance(preview, dict) else None
    if not isinstance(preview_id, str) or not preview_id:
        raise ClientError("no active display preview is available")
    return preview_id


def _dispatch(args: argparse.Namespace, core: ClientCore) -> dict[str, Any]:
    command = args.command
    if command == "status":
        return core.run({"action": "status"})
    if command == "outputs":
        return core.run({"action": "outputs"})
    if command == "inspect":
        return core.run({"action": "inspect"})
    if command == "diagnostics":
        return core.run({"action": "diagnostics"})
    if command == "display":
        action = args.display_command
        if action == "save-current":
            core.require_scope("save-current")
            _confirmed(args, _CONFIRM_TEXT)
            return core.run(_with_request_id({
                "action": "save-current",
                "output_id": args.output_id,
                "generation": args.generation,
                "visible": True,
            }, args))
        if action == "preview":
            return core.run(_with_request_id({
                "action": "preview",
                "output_id": args.output_id,
                "mode_id": args.mode_id,
                "generation": args.generation,
            }, args))
        if action == "keep":
            core.require_scope("confirm")
            _confirmed(args, "Can you see the picture on the paired SteamOS host's display? Keeping it saves this mode.")
            return core.run(_with_request_id({"action": "confirm", "preview_id": _preview_id(core, args.preview_id), "visible": True}, args))
        if action == "revert":
            return core.run(_with_request_id({"action": "restore", "source": "preview"}, args))
        if action == "restore":
            return core.run(_with_request_id({"action": "restore", "source": args.source}, args))
        if action == "order":
            return core.run({"action": "display-order"})
        if action == "link-facts":
            return core.run({"action": "link-facts"})
        if action == "order-save":
            keys = [item.strip() for item in args.output_keys.split(",") if item.strip()]
            if args.restart:
                core.require_scope("display-order-restart")
                _confirmed(args, "Save this display order and restart Gaming Mode on the paired host?")
            return core.run(_with_request_id({
                "action": "display-order-restart" if args.restart else "display-order-save",
                "output_keys": keys,
                "generation": args.generation,
            }, args))
        if action == "order-reset":
            return core.run(_with_request_id({"action": "display-order-reset"}, args))
    if command == "power":
        core.require_scope("power")
        _confirmed(args, f"{args.action.title()} the exact paired SteamOS host?")
        return core.run(_with_request_id({"action": args.action}, args))
    if command == "wake":
        mac = args.mac
        interface = args.interface
        if not mac:
            local = core.run({"action": "inspect"})
            mac = local.get("wake_mac") if isinstance(local, dict) else None
            interface = interface or (local.get("wake_interface") if isinstance(local, dict) else None)
        if not mac:
            raise ClientError("no saved Wake-on-LAN target is available")
        return core.run({"action": "wake", "mac": mac, "interface": interface})
    if command == "sunshine":
        if args.sunshine_command == "status":
            status = core.run({"action": "status"})
            return {"protocol_version": status.get("protocol_version"), "sunshine": status.get("sunshine")}
        return core.run(_with_request_id({"action": "sunshine-restart"}, args))
    if command == "journal":
        if args.journal_command == "list":
            return core.run({"action": "operations"})
        if args.journal_command == "view":
            return core.run({"action": "journal"})
        return core.run({"action": "operation", "operation_id": args.operation_id})
    if command == "pair":
        if args.pair_command == "discover":
            return core.run({"action": "discover"})
        if args.pair_command == "start":
            host = _discover_pair_host(core, args.host)
            return core.run({
                "action": "pair-start",
                "host": host,
                "client_name": args.name,
                "scopes": _scope_list(args.scopes),
            })
        if args.pair_command == "poll":
            return core.run({"action": "pair-poll"})
        if args.pair_command == "cancel":
            return core.run({"action": "pair-cancel"})
        if args.pair_command == "payload":
            payload = sys.stdin.readline(8193).strip()
            if not payload:
                raise ClientError("pairing payload is required on stdin")
            return core.run({
                "action": "pair",
                "payload": payload,
                "endpoint": args.host,
                "client_name": args.name,
                "scopes": _scope_list(args.scopes),
            })
    raise ClientError("command is unsupported")


def _access(command: str, core: ClientCore, result: dict[str, Any]) -> dict[str, str] | None:
    """What this pairing may do on the host, for the two commands that read it.

    It sits beside the data, not in it, so the host's own reply and the diagnostics document
    are printed as they are. A host that did not say leaves the saved scopes standing.
    """
    if command == "status":
        scopes = credential_scopes(result)
        if scopes is None:
            saved = core.run({"action": "inspect"})
            scopes = saved.get("scopes") if isinstance(saved, dict) else None
    elif command == "diagnostics":
        local = result.get("local") if isinstance(result, dict) else None
        scopes = local.get("scopes") if isinstance(local, dict) and local.get("paired") is True else None
    else:
        return None
    return None if scopes is None else access_view(scopes)


def _exit_code(value: dict[str, Any]) -> int:
    operation = value.get("operation")
    if not isinstance(operation, dict):
        return 0
    state = operation.get("state")
    if state == "failed":
        return 1
    if state in {"accepted", "dispatched", "observed_return", "unknown"}:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    output_format = "plain" if "--plain" in raw_args else "json"
    try:
        args = _parser().parse_args(raw_args)
        output_format = args.output_format
        core = ClientCore()
        result = _dispatch(args, core)
        access = _access(args.command, core, result)
        _emit({"ok": True, "data": result, **({"access": access} if access else {})}, output_format)
        return _exit_code(result)
    except _ArgumentError as exc:
        _emit({"ok": False, "error": str(exc)[:256], "unknown": False}, output_format)
        return 1
    except ClientError as exc:
        result: dict[str, Any] = {"ok": False, "error": str(exc)[:256], "unknown": exc.unknown}
        if exc.status is not None:
            result["status"] = exc.status
        if exc.retry_after is not None:
            result["retry_after"] = exc.retry_after
        _emit(result, output_format)
        return 2 if exc.unknown else 1
    except (KeyboardInterrupt, EOFError):
        _emit({"ok": False, "error": "cancelled", "unknown": False}, output_format)
        return 1
    except Exception:
        _emit({"ok": False, "error": "client command failed", "unknown": False}, output_format)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
