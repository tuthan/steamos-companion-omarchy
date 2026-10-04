"""Redacted diagnostic report for pasting into an issue.

A pure port of the reference in Decky's ``host/backend/diagnostics.py`` (pinned
under ``protocol/``, section "Diagnostic report"). It is built from raw response
bodies a client already read (status, display outputs, DRM link facts, the
operation journal), names facts and never copies a record: no MAC, pin, host or
boot identifier, address, credential, pairing material or operation id can reach
it, and free text is scrubbed before it is bounded. It never raises: a malformed
body gives a null section. ``tests/test_diagnostics.py`` proves it against the
vendored cross-client vector ``protocol/fixtures/diagnostic-report.json``.

Deliberately separate from the transport's allowlist decoders (link facts, the
journal view), which feed the panel: the report is rebuilt from the raw bodies
so every client states the same facts.

Two notes against the reference:

- ``\\d`` in a validated field (a card number, a resolution, a timestamp) is
  ASCII-only here (``re.ASCII``). The reference's ``\\d`` also matches other
  scripts' digits, which would put non-ASCII text into a report that is
  otherwise printable ASCII. No vector case differs.
- Patterns keep the reference's ``^...$`` and are applied with ``fullmatch``.
  ``$`` accepts a trailing newline, so ``fullmatch`` is what rejects it.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable

REPORT_FORMAT = "steamos-companion-report-v1"
MAX_REPORT_BYTES = 12 * 1024
MAX_METHODS = 8
MAX_OUTPUTS = 8
MAX_MODES = 24
MAX_CONNECTORS = 8
MAX_RESOLUTIONS = 32
MAX_JOURNAL = 20

_TEXT_PRECAP = 1024
_SCRUBBERS = (
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"sha256:[0-9A-Fa-f]{8,}"), "[pin]"),
    (re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"), "[mac]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "[ip]"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4}\b"), "[ip]"),
    (re.compile(r"\b[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6}::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"), "[ip]"),
    (re.compile(r"(?:/[A-Za-z0-9_.\-]+){2,}"), "[path]"),
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}\b"), "[token]"),
)
_LINE_BREAKS = re.compile(r"[\t\r\n\f\v]+")
_NON_PRINTABLE = re.compile(r"[^\x20-\x7e]")
_SPACES = re.compile(r" {2,}")

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_METHOD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,23}$")
_CONTRACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
_CONNECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")
_CARD = re.compile(r"^drm:(card\d{1,3})$", re.ASCII)
_VENDOR = re.compile(r"^[A-Za-z0-9]{1,8}$")
_RESOLUTION = re.compile(r"^\d{3,5}x\d{3,5}$", re.ASCII)
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})T", re.ASCII)
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?Z$", re.ASCII)
_RESTORE_STATE = re.compile(r"[a-z]{1,16}")

_TEMPERATURE_SOURCES = frozenset({"steam", "hwmon", "unavailable"})
_SUNSHINE_STATES = frozenset({"disabled", "unavailable", "unknown", "running", "stopped", "restarting"})
_TARGET_SCALARS = ("output_id", "mode_id", "generation", "action", "restart", "automatic", "connector", "output_key", "target_output_key", "baseline_output_key", "target")
_TARGET_LISTS = ("connectors", "output_keys")


def scrub_text(value: Any, limit: int) -> str | None:
    """Printable ASCII with addresses, pins, tokens and paths replaced, then bounded."""
    if not isinstance(value, str):
        return None
    text = _LINE_BREAKS.sub(" ", value[:_TEXT_PRECAP])
    text = _NON_PRINTABLE.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    for pattern, marker in _SCRUBBERS:
        text = pattern.sub(marker, text)
    text = text[:limit].strip()
    return text or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any, low: int, high: int) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None


def _pattern(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _hertz(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1000:
        return None
    text = f"{round(float(value), 2):.2f}".rstrip("0").rstrip(".")
    return text or None


def _mode_text(mode: Any) -> str | None:
    mode = _dict(mode)
    width, height = _int(mode.get("width"), 1, 16384), _int(mode.get("height"), 1, 16384)
    if width is None or height is None:
        return None
    hertz = _hertz(mode.get("refresh_hz"))
    return f"{width}x{height}" + (f"@{hertz}" if hertz else "")


def _day(value: Any) -> str | None:
    found = _DAY.match(value) if isinstance(value, str) else None
    return found.group(1) if found else None


def _host(status: Any) -> dict[str, Any] | None:
    check = _dict(status).get("self_check")
    if not isinstance(check, dict):
        return None
    certificate, listener, bridge = _dict(check.get("certificate")), _dict(check.get("listener")), _dict(check.get("bridge"))
    provider, versions = _dict(check.get("provider")), _dict(check.get("versions"))
    methods = bridge.get("methods") if isinstance(bridge.get("methods"), list) else []
    source = _dict(check.get("temperature")).get("source")
    state = provider.get("state")
    return {
        "plugin_version": _pattern(versions.get("plugin"), _VERSION),
        "protocol_version": _int(versions.get("protocol"), 1, 99),
        "certificate": {"ready": certificate.get("ready") is True, "valid_from": _day(certificate.get("not_before")), "valid_until": _day(certificate.get("not_after"))},
        "listener": {"bound": listener.get("bound") is True, "port": _int(listener.get("port"), 1, 65535)},
        "bridge": {"ready": bridge.get("ready") is True, "methods": [item for item in methods if isinstance(item, str) and _METHOD.fullmatch(item)][:MAX_METHODS]},
        "temperature_source": source if source in _TEMPERATURE_SOURCES else None,
        "sunshine": {
            "monitor_enabled": provider.get("monitor_enabled") is True,
            "state": state if state in _SUNSHINE_STATES else None,
            "stale": provider.get("stale") is True,
            "contract_version": _pattern(provider.get("contract_version"), _CONTRACT),
        },
    }


def _outputs(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    items = body.get("outputs") if isinstance(body.get("outputs"), list) else []
    records: list[dict[str, Any]] = []
    for item in items:
        item = _dict(item)
        identifier = _pattern(item.get("id"), _TOKEN)
        if identifier is None or len(identifier) > 16:
            continue
        modes = [mode for mode in item.get("modes", []) if isinstance(mode, dict)] if isinstance(item.get("modes"), list) else []
        texts = [text for text in (_mode_text(mode) for mode in modes) if text]
        current = next((_mode_text(mode) for mode in modes if mode.get("id") == item.get("current_mode_id") and isinstance(mode.get("id"), str)), None)
        records.append({
            "id": identifier,
            "name": scrub_text(item.get("name"), 32),
            "description": scrub_text(item.get("description"), 48),
            "internal": item.get("is_internal") is True,
            "current_mode": current,
            "mode_count": len(texts),
            "modes": texts[:MAX_MODES],
        })
    profiles = body.get("profiles") if isinstance(body.get("profiles"), list) else []
    return {
        "available": body.get("available") is True,
        "count": len(records),
        "outputs": records[:MAX_OUTPUTS],
        "saved_profiles": min(len(profiles), 256),
        "preview_active": isinstance(body.get("preview"), dict),
    }


def _link(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    drm = _dict(body.get("drm"))
    if drm.get("available") is not True or not isinstance(drm.get("connectors"), list):
        return {"available": False, "reason": scrub_text(drm.get("reason"), 96), "connectors": []}
    connectors: list[dict[str, Any]] = []
    for item in drm["connectors"]:
        item = _dict(item)
        name = _pattern(item.get("connector"), _CONNECTOR)
        if name is None:
            continue
        card = _CARD.fullmatch(item["gpu_id"]) if isinstance(item.get("gpu_id"), str) else None
        resolutions: list[str] = []
        for entry in item.get("resolutions", []) if isinstance(item.get("resolutions"), list) else []:
            if isinstance(entry, str) and _RESOLUTION.fullmatch(entry) and entry not in resolutions:
                resolutions.append(entry)
        joined = _pattern(item.get("steam_output_id"), _TOKEN)
        connectors.append({
            "connector": name,
            "card": card.group(1) if card else None,
            "connected": item.get("connected") is True,
            "internal": item.get("is_internal") is True,
            "vendor": _pattern(item.get("monitor_vendor"), _VENDOR),
            "model": _int(item.get("monitor_product_id"), 0, 65535),
            "output_id": joined if joined and len(joined) <= 16 else None,
            "resolution_count": len(resolutions),
            "resolutions": resolutions[:MAX_RESOLUTIONS],
        })
    return {"available": True, "reason": scrub_text(drm.get("reason"), 96), "connectors": connectors[:MAX_CONNECTORS]}


def _target(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    target: dict[str, Any] = {}
    for key in _TARGET_SCALARS:
        item = value.get(key)
        if isinstance(item, bool):
            target[key] = item
        elif isinstance(item, int):
            if abs(item) < 2**31:
                target[key] = item
        elif isinstance(item, str) and scrub_text(item, 48):
            target[key] = scrub_text(item, 48)
    for key in _TARGET_LISTS:
        item = value.get(key)
        if isinstance(item, list):
            texts = [scrub_text(entry, 32) for entry in item[:4] if isinstance(entry, str)]
            if texts and all(texts):
                target[key] = texts
    text = _mode_text(value.get("mode"))
    if text:
        target["mode"] = text
    return target or None


def _journal(body: Any) -> dict[str, Any] | None:
    items = _dict(body).get("operations") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    records: list[dict[str, Any]] = []
    for item in items:
        item = _dict(item)
        identifier, kind, state = _pattern(item.get("id"), _TOKEN), _pattern(item.get("kind"), _TOKEN), _pattern(item.get("state"), _TOKEN)
        if identifier is None or kind is None or state is None or len(kind) > 64 or len(state) > 32:
            continue
        stamp = _STAMP.fullmatch(item["updated_at"]) if isinstance(item.get("updated_at"), str) else None
        records.append({
            "updated_at": f"{stamp.group(1)}Z" if stamp else None,
            "kind": kind,
            "state": state,
            "outcome": _pattern(item.get("outcome"), _CODE),
            "restore_state": _pattern(item.get("restore_state"), _RESTORE_STATE),
            "restored": item["restored"] if isinstance(item.get("restored"), bool) else None,
            "reason": scrub_text(item.get("reason"), 160),
            "target": _target(item.get("target")),
        })
    return {"count": len(records), "truncated": len(records) > MAX_JOURNAL, "records": records[:MAX_JOURNAL]}


def _guarded(section: Callable[[Any], Any], value: Any) -> Any:
    try:
        return section(value)
    except Exception:  # a diagnostic must never be the thing that fails
        return None


def build_report(status: Any = None, outputs: Any = None, drm: Any = None, operations: Any = None) -> dict[str, Any]:
    """Build the shared report from raw response bodies; any input may be absent or hostile."""
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "host": _guarded(_host, status),
        "outputs": _guarded(_outputs, outputs),
        "link": _guarded(_link, drm),
        "journal": _guarded(_journal, operations),
    }
    journal = report["journal"]
    while journal and journal["records"] and len(json.dumps(report, separators=(",", ":"))) > MAX_REPORT_BYTES:
        journal["records"].pop()
        journal["truncated"] = True
    return report
