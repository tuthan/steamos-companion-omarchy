"""Pinned HTTPS client and private state for the Omarchy plugin."""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 256 * 1024
PAIRING_PREFIX = "steamos-companion:v1:"
DISCOVERY_DEFAULT_PORT = 18443
DISCOVERY_MAX_NETWORK_ADDRESSES = 1024
DISCOVERY_MAX_ADDRESSES = 1024
DISCOVERY_MAX_RESPONSE_BYTES = 16 * 1024
DISCOVERY_CONNECT_TIMEOUT = 0.35
DISCOVERY_SERVER_PREFIX = "SteamOSCompanion/1"
PAIRING_REQUEST_TIMEOUT_SECONDS = 120
PAIRING_EXCHANGE_TIMEOUT = 6.0
DEFAULT_TIMEOUT = 5.0
POLL_TIMEOUT = 2.5
DEFAULT_SCOPES = ["status.read", "power.control", "display.control"]
ALLOWED_SCOPES = frozenset({"status.read", "power.control", "display.control", "sunshine.control"})

# Short authentication string. The comparison code is derived from a client
# nonce and the host certificate fingerprint; it is never transmitted. A relay
# holding a different certificate therefore cannot make both screens agree, and
# scrypt makes searching the 10^8 code space infeasible inside the 120-second
# request window.
SAS_SALT_PREFIX = b"steamos-companion:v1:pairing-sas:"
SAS_NONCE_BYTES = 16
SAS_SCRYPT_N = 2 ** 14
SAS_SCRYPT_R = 8
SAS_SCRYPT_P = 1
SAS_MAXMEM = 64 * 1024 * 1024
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,32}$")
INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PATH_RE = re.compile(r"^/v1/(?:pair/request|status|display/outputs|display/order(?:/automatic)?|display/preview|display/confirm|display/restore|power|sunshine/restart|pair/revoke-self|operations/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DISPLAY_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}$")
PIN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAC_RE = re.compile(r"^[0-9a-f]{12}$")
DISPLAY_ORDER_MAX_OUTPUTS = 16
OPERATION_STATES = frozenset({"accepted", "dispatched", "observed_return", "succeeded", "failed", "unknown"})


class ClientError(RuntimeError):
    def __init__(self, message: str, *, unknown: bool = False, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.unknown = unknown
        self.status = status
        self.retry_after = retry_after


def _id(prefix: str = "") -> str:
    return prefix + base64.urlsafe_b64encode(secrets.token_bytes(18)).decode().rstrip("=")


def _bounded(value: Any, limit: int = 256) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def normalize_pin(value: Any) -> str:
    if not isinstance(value, str):
        raise ClientError("certificate fingerprint is missing")
    value = value.strip().lower().replace(" ", "").replace(":", "")
    if value.startswith("sha256"):
        value = "sha256:" + value[6:].lstrip("-")
    if value.startswith("sha256-"):
        value = "sha256:" + value[7:]
    if not PIN_RE.fullmatch(value):
        raise ClientError("certificate fingerprint is invalid")
    return value


def new_verification_nonce() -> str:
    """Create the per-request nonce the comparison code is derived from."""
    return base64.urlsafe_b64encode(secrets.token_bytes(SAS_NONCE_BYTES)).decode().rstrip("=")


def decode_verification_nonce(value: Any) -> bytes:
    if not isinstance(value, str) or not NONCE_RE.fullmatch(value):
        raise ClientError("verification nonce is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ClientError("verification nonce is invalid") from exc
    if len(raw) != SAS_NONCE_BYTES:
        raise ClientError("verification nonce is invalid")
    return raw


def derive_pairing_code(nonce: Any, certificate_fingerprint: Any) -> str:
    """Derive the comparison code from the nonce and the pinned certificate.

    The code is never sent. Both sides derive it, the host from its own
    certificate and this client from the certificate it pinned, so a relay
    presenting a different certificate cannot make the two screens agree.
    scrypt bounds an offline search of the eight-digit space well beyond the
    120-second lifetime of a pending request.
    """
    raw = decode_verification_nonce(nonce)
    fingerprint = normalize_pin(certificate_fingerprint)
    derived = hashlib.scrypt(
        raw,
        salt=SAS_SALT_PREFIX + fingerprint.encode("ascii"),
        n=SAS_SCRYPT_N,
        r=SAS_SCRYPT_R,
        p=SAS_SCRYPT_P,
        maxmem=SAS_MAXMEM,
        dklen=8,
    )
    value = int.from_bytes(derived, "big") % 100_000_000
    return "%08d" % value


def _display_pairing_code(value: str) -> str:
    return f"{value[:4]}-{value[4:]}"


def _pairing_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) and value.strip() else "Omarchy client"
    if len(name) > 96:
        raise ClientError("client name is too long")
    return name


def _pairing_scopes(value: Any) -> list[str]:
    selected = DEFAULT_SCOPES if value is None else value
    if not isinstance(selected, list) or not all(isinstance(scope, str) and scope in ALLOWED_SCOPES for scope in selected):
        raise ClientError("pairing scopes are invalid")
    selected = list(dict.fromkeys(selected))
    if "status.read" not in selected:
        selected.insert(0, "status.read")
    return selected


def fingerprint_der(cert_der: bytes) -> str:
    return "sha256:" + hashlib.sha256(cert_der).hexdigest()


def parse_pairing_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(PAIRING_PREFIX) or len(value) > 4096:
        raise ClientError("pairing payload format is unsupported")
    encoded = value[len(PAIRING_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("pairing payload is malformed") from exc
    required = {"protocol_version", "endpoint", "host_id", "certificate_fingerprint", "pairing_id", "secret", "expires_at"}
    if not isinstance(payload, dict) or payload.get("protocol_version") != PROTOCOL_VERSION or not required.issubset(payload):
        raise ClientError("pairing payload is incomplete or unsupported")
    if not isinstance(payload["endpoint"], str) or not isinstance(payload["host_id"], str) or not ID_RE.fullmatch(payload["host_id"]):
        raise ClientError("pairing payload host identity is invalid")
    if not isinstance(payload["pairing_id"], str) or not ID_RE.fullmatch(payload["pairing_id"]):
        raise ClientError("pairing payload pairing identity is invalid")
    if not isinstance(payload["secret"], str) or not 16 <= len(payload["secret"]) <= 256:
        raise ClientError("pairing payload secret is invalid")
    try:
        expires_at = float(payload["expires_at"])
    except (TypeError, ValueError) as exc:
        raise ClientError("pairing expiry is invalid") from exc
    if not math.isfinite(expires_at):
        raise ClientError("pairing expiry is invalid")
    if expires_at <= time.time():
        raise ClientError("pairing payload has expired")
    endpoint = validate_endpoint(payload["endpoint"])
    return {**payload, "endpoint": endpoint, "certificate_fingerprint": normalize_pin(payload["certificate_fingerprint"]), "expires_at": expires_at}


def validate_endpoint(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ClientError("endpoint is invalid")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ClientError("endpoint must be an HTTPS origin")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ClientError("endpoint port is invalid") from exc
    if not 1 <= port <= 65535:
        raise ClientError("endpoint port is invalid")
    host = parsed.hostname
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host):
        raise ClientError("endpoint host is invalid")
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{rendered_host}:{port}"


def validate_discovery_port(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
        raise ClientError("discovery port must be between 1024 and 65535")
    return value


def _local_ipv4_networks() -> list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]]:
    """Return usable IPv4 LAN networks without invoking a shell command."""
    try:
        import fcntl
        import struct
    except ImportError:
        return []

    try:
        interfaces = socket.if_nameindex()
    except (AttributeError, OSError):
        return []

    networks: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, name in interfaces:
                if name == "lo":
                    continue
                try:
                    request = struct.pack("256s", name.encode("ascii")[:15])
                    address_raw = fcntl.ioctl(probe.fileno(), 0x8915, request)
                    netmask_raw = fcntl.ioctl(probe.fileno(), 0x891b, request)
                    address = ipaddress.IPv4Address(socket.inet_ntoa(address_raw[20:24]))
                    netmask = ipaddress.IPv4Address(socket.inet_ntoa(netmask_raw[20:24]))
                    network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
                except (OSError, ValueError, UnicodeEncodeError, struct.error):
                    continue
                if address.is_loopback or address.is_unspecified or address.is_multicast:
                    continue
                networks.append((network, address))
    except OSError:
        return []
    return networks


def _discovery_targets(
    networks: list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]],
) -> tuple[list[str], list[str], bool]:
    """Bound discovery to local subnets and a predictable amount of work."""
    targets: list[str] = []
    seen: set[str] = set()
    skipped: list[str] = []
    truncated = False
    for network, local_address in networks:
        if network.num_addresses > DISCOVERY_MAX_NETWORK_ADDRESSES:
            skipped.append(str(network))
            continue
        for address in network.hosts():
            rendered = str(address)
            if address == local_address or rendered in seen:
                continue
            seen.add(rendered)
            targets.append(rendered)
            if len(targets) >= DISCOVERY_MAX_ADDRESSES:
                truncated = True
                return targets, skipped, truncated
    return targets, skipped, truncated


def _probe_discovery_candidate(host: str, port: int) -> dict[str, Any] | None:
    """Identify the current host listener without sending credentials."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=DISCOVERY_CONNECT_TIMEOUT) as raw:
            raw.settimeout(DISCOVERY_CONNECT_TIMEOUT)
            with context.wrap_socket(raw, server_hostname=None) as connection:
                certificate = connection.getpeercert(binary_form=True)
                if not certificate:
                    return None
                request = (
                    f"GET /v1/status HTTP/1.0\r\nHost: {host}\r\n"
                    "Accept: application/json\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                connection.sendall(request)
                response = http.client.HTTPResponse(connection)
                response.begin()
                server = response.getheader("Server", "") or ""
                body = response.read(DISCOVERY_MAX_RESPONSE_BYTES + 1)
                if len(body) > DISCOVERY_MAX_RESPONSE_BYTES or not server.startswith(DISCOVERY_SERVER_PREFIX):
                    return None
                try:
                    value = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
                if response.status != 401 or not isinstance(value, dict) or value.get("error") != "unauthorized":
                    return None
                return {
                    "host": host,
                    "port": port,
                    "endpoint": validate_endpoint(f"https://{host}:{port}"),
                    "certificate_fingerprint": fingerprint_der(certificate),
                    "server": server[:128],
                }
    except (OSError, TimeoutError, ValueError, ssl.SSLError, http.client.HTTPException):
        return None


def discover_hosts(port: Any = DISCOVERY_DEFAULT_PORT) -> dict[str, Any]:
    """Find SteamOS Companion HTTPS listeners on the client's active IPv4 LANs."""
    port = validate_discovery_port(port)
    networks = _local_ipv4_networks()
    targets, skipped, truncated = _discovery_targets(networks)
    found: list[dict[str, Any]] = []
    if targets:
        workers = min(64, len(targets))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_probe_discovery_candidate, host, port) for host in targets]
            for future in concurrent.futures.as_completed(futures):
                try:
                    candidate = future.result()
                except Exception:
                    candidate = None
                if candidate is not None:
                    found.append(candidate)
    found.sort(key=lambda item: item["host"])
    result: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "port": port,
        "hosts": found[:32],
        "network_count": len(networks),
        "addresses_scanned": len(targets),
        "truncated": truncated or len(found) > 32,
    }
    if skipped:
        result["skipped_networks"] = skipped[:8]
    if not found:
        if not networks:
            result["reason"] = "No usable IPv4 LAN interface was found"
        elif not targets:
            result["reason"] = "The local network is larger than the bounded discovery scan"
        else:
            result["reason"] = f"No SteamOS Companion listener responded on port {port}"
    return result


MAX_STATE_BYTES = 64 * 1024


@contextmanager
def _private_dir(path: Path):
    """Walk without following links; retain the directory fd for all I/O.

    Writable ancestors are refused except trusted sticky directories such as
    /tmp. Root-owned ancestors are allowed; the final directory must be ours.
    Rename/symlink swaps cannot redirect operations through a retained fd.
    """
    path = path.absolute()
    if ".." in path.parts or path == Path("/"):
        raise ClientError("private client state directory is unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for index, part in enumerate(path.parts[1:]):
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            final = index == len(path.parts) - 2
            trusted_sticky_ancestor = (
                not final
                and bool(info.st_mode & stat.S_ISVTX)
                and bool(info.st_mode & stat.S_IWOTH)
            )
            if (
                (info.st_uid not in {0, os.geteuid()} and not trusted_sticky_ancestor)
                or (final and info.st_uid != os.geteuid())
            ):
                raise ClientError("private client state directory is unsafe")
            if final:
                os.fchmod(fd, 0o700)
            elif info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise ClientError("private client state ancestor is writable by others")
        yield fd
    except OSError as exc:
        raise ClientError("private client state directory is unavailable or unsafe") from exc
    finally:
        os.close(fd)


def _safe_dir(path: Path) -> None:
    with _private_dir(path):
        pass


def _default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "steamos-companion"


def _check_private(info, label: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ClientError(f"private {label} file is unsafe")


def _read_private(path: Path, label: str) -> dict[str, Any] | None:
    with _private_dir(path.parent) as directory:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            _check_private(os.fstat(handle.fileno()), label)
            os.fchmod(handle.fileno(), 0o600)
            raw = handle.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ClientError(f"private {label} is too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError(f"private {label} is unavailable") from exc


def _write_private(root: Path, path: Path, value: dict[str, Any], label: str) -> None:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2).encode() + b"\n"
    if len(encoded) > MAX_STATE_BYTES:
        raise ClientError(f"private {label} is too large")
    with _private_dir(root) as directory:
        try:
            _check_private(os.stat(path.name, dir_fd=directory, follow_symlinks=False), label)
        except FileNotFoundError:
            pass
        name = ".client-" + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


def _remove_private(path: Path, label: str) -> None:
    with _private_dir(path.parent) as directory:
        try:
            _check_private(os.stat(path.name, dir_fd=directory, follow_symlinks=False), label)
        except FileNotFoundError:
            return
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)


class ClientStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = _default_state_root() if root is None else Path(root)
        self.path = self.root / "client.json"
        _safe_dir(self.root)

    def load(self, *, required: bool = True) -> dict[str, Any] | None:
        value = _read_private(self.path, "client state")
        if value is None:
            if required:
                raise ClientError("client is not paired")
            return None
        self._validate_state(value)
        return value

    def save(self, value: dict[str, Any]) -> None:
        self._validate_state(value)
        _write_private(self.root, self.path, value, "client state")

    def forget(self) -> None:
        _remove_private(self.path, "client state")

    @staticmethod
    def _validate_state(value: Any) -> None:
        if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
            raise ClientError("client state protocol is unsupported")
        for key in ("endpoint", "host_id", "certificate_fingerprint", "client_id", "token"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise ClientError("client state is incomplete")
        validate_endpoint(value["endpoint"])
        normalize_pin(value["certificate_fingerprint"])
        if not ID_RE.fullmatch(value["host_id"]) or not ID_RE.fullmatch(value["client_id"]):
            raise ClientError("client state identity is invalid")
        scopes = value.get("scopes")
        if not isinstance(scopes, list) or not scopes or not all(scope in ALLOWED_SCOPES for scope in scopes):
            raise ClientError("client state scopes are invalid")
        wake_mac = value.get("wake_mac", "")
        wake_interface = value.get("wake_interface", "")
        wake_host_interface = value.get("wake_host_interface", "")
        if not all(isinstance(item, str) for item in (wake_mac, wake_interface, wake_host_interface)):
            raise ClientError("client wake state is invalid")
        if wake_mac:
            validate_wake_values(wake_mac, wake_interface or None)
            if wake_host_interface:
                validate_wake_values(wake_mac, wake_host_interface)
        elif wake_interface or wake_host_interface:
            raise ClientError("client wake state is incomplete")
        display_preference = value.get("show_nonstandard_refresh_rates", False)
        if not isinstance(display_preference, bool):
            raise ClientError("client display preferences are invalid")
        pending_operation = value.get("pending_operation")
        if pending_operation is not None:
            if not isinstance(pending_operation, dict):
                raise ClientError("client pending operation is invalid")
            operation_id = pending_operation.get("id")
            request_id = pending_operation.get("request_id")
            operation_state = pending_operation.get("state")
            operation_kind = pending_operation.get("kind")
            pending_keys = pending_operation.get("output_keys")
            pending_generation = pending_operation.get("generation")
            if (
                not isinstance(operation_id, str)
                or not ID_RE.fullmatch(operation_id)
                or not isinstance(request_id, str)
                or not ID_RE.fullmatch(request_id)
                or not isinstance(operation_state, str)
                or operation_state not in OPERATION_STATES
                or not isinstance(operation_kind, str)
                or not 1 <= len(operation_kind) <= 64
                or (pending_keys is None) != (pending_generation is None)
            ):
                raise ClientError("client pending operation is invalid")
            if pending_keys is not None:
                _display_order_keys(pending_keys)
                _display_order_generation(pending_generation)


class PendingStore:
    """One in-flight pairing request, owned by the client rather than the panel.

    Keeping it on disk means approval polling is a sequence of short helper
    calls instead of one blocking call, the panel can be closed and reopened
    while the owner walks to the Deck, and the pairing secret never has to
    travel back out through the panel process.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = _default_state_root() if root is None else Path(root)
        self.path = self.root / "pending.json"
        _safe_dir(self.root)

    def load(self, *, required: bool = False) -> dict[str, Any] | None:
        try:
            value = _read_private(self.path, "pairing request")
            if value is None:
                if required:
                    raise ClientError("no pairing request is in progress")
                return None
            self._validate(value)
        except ClientError:
            self.clear()
            if required:
                raise
            return None
        return value

    def save(self, value: dict[str, Any]) -> None:
        self._validate(value)
        _write_private(self.root, self.path, value, "pairing request")

    def clear(self) -> None:
        _remove_private(self.path, "pairing request")

    @staticmethod
    def _validate(value: Any) -> None:
        if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
            raise ClientError("pairing request protocol is unsupported")
        if value.get("method") not in {"verification", "payload"}:
            raise ClientError("pairing request method is unsupported")
        validate_endpoint(value.get("endpoint"))
        normalize_pin(value.get("certificate_fingerprint"))
        if not isinstance(value.get("client_id"), str) or not ID_RE.fullmatch(value["client_id"]):
            raise ClientError("pairing request client identity is invalid")
        _pairing_name(value.get("client_name"))
        _pairing_scopes(value.get("scopes"))
        try:
            expires_at = float(value.get("expires_at"))
        except (TypeError, ValueError) as exc:
            raise ClientError("pairing request expiry is invalid") from exc
        if not math.isfinite(expires_at):
            raise ClientError("pairing request expiry is invalid")
        host_id = value.get("host_id", "")
        if host_id and (not isinstance(host_id, str) or not ID_RE.fullmatch(host_id)):
            raise ClientError("pairing request host identity is invalid")
        session = value.get("pairing_session", "")
        if session and (not isinstance(session, str) or not 16 <= len(session) <= 256):
            raise ClientError("pairing request session is invalid")
        if value["method"] == "verification":
            decode_verification_nonce(value.get("verification_nonce"))
            code = value.get("pairing_code", "")
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{4}", code):
                raise ClientError("pairing request code is invalid")
        else:
            if not isinstance(value.get("pairing_id"), str) or not ID_RE.fullmatch(value["pairing_id"]):
                raise ClientError("pairing request identity is invalid")
            if not isinstance(value.get("secret"), str) or not 16 <= len(value["secret"]) <= 256:
                raise ClientError("pairing request secret is invalid")


def validate_response_limits(value: Any) -> None:
    """Reject oversized UI data, never truncate identifiers used for actions."""
    remaining = 4096

    def visit(item: Any, depth: int = 0, field: str = "") -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ClientError("host response is too complex")
        if isinstance(item, str):
            if len(item) > 1024:
                raise ClientError("host response text is too long")
        elif isinstance(item, list):
            limit = DISPLAY_ORDER_MAX_OUTPUTS if field in {"outputs", "output_keys", "saved_output_keys"} else 256
            if len(item) > limit:
                raise ClientError("host response list is too large")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            if len(item) > 128:
                raise ClientError("host response object is too large")
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1, key)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ClientError("host response number is invalid")

    visit(value)


class PinnedTransport:
    def __init__(self, endpoint: str, pin: str, token: str | None = None, timeout: float = 5.0):
        self.endpoint = validate_endpoint(endpoint)
        self.pin = normalize_pin(pin)
        self.token = token
        self.timeout = max(1.0, min(float(timeout), 10.0))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        method = method.upper()
        if method not in {"GET", "POST"} or not PATH_RE.fullmatch(path):
            raise ClientError("route is not allowed")
        if method == "GET" and body:
            raise ClientError("GET does not accept a body")
        encoded = b"" if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > MAX_BODY_BYTES:
            raise ClientError("request body is too large")
        parsed = urlsplit(self.endpoint)
        port = parsed.port or 443
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if path == "/v1/pair/request":
            # CPython exposes tls-unique but not the RFC 9266 TLS 1.3
            # exporter. Keep the short pairing exchange on TLS 1.2, where
            # tls-unique is defined; normal authenticated traffic can still
            # negotiate TLS 1.3.
            context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=self.timeout, context=context)
        try:
            # Connect and pin before writing the request line/body. This is
            # especially important for the unauthenticated pairing secret.
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True) if connection.sock else b""
            if fingerprint_der(certificate) != self.pin:
                raise ClientError(
                    "host certificate does not match the pinned identity. Either the host "
                    "regenerated its certificate, or another device is answering on this "
                    "address. Pair again from Settings only if you replaced the certificate."
                )
            host_header = parsed.hostname
            if ":" in host_header and not host_header.startswith("["):
                host_header = f"[{host_header}]"
            headers = {"Host": host_header, "Accept": "application/json", "Connection": "close"}
            if path == "/v1/pair/request":
                getter = getattr(connection.sock, "get_channel_binding", None) if connection.sock else None
                try:
                    channel_binding = getter("tls-unique") if callable(getter) else None
                except (ValueError, OSError, ssl.SSLError) as exc:
                    raise ClientError("host TLS channel binding is unavailable; update Decky and Omarchy before pairing") from exc
                if not isinstance(channel_binding, bytes) or len(channel_binding) < 12:
                    raise ClientError("host TLS channel binding is unavailable; select the host again and retry")
                headers["X-SteamOS-Companion-TLS-Binding"] = base64.urlsafe_b64encode(channel_binding).decode("ascii")
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(encoded))
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            connection.putrequest(method, path, skip_accept_encoding=True)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders(encoded if body is not None else None)
            response = connection.getresponse()
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ClientError("host response is too large")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClientError("host response is malformed") from exc
            if not isinstance(value, dict):
                raise ClientError("host response is invalid")
            if response.status >= 400:
                retry_after = None
                try:
                    retry_after = float(response.getheader("Retry-After", ""))
                except (TypeError, ValueError):
                    pass
                raise ClientError(
                    _bounded(value.get("message") or value.get("error") or "host rejected the request"),
                    unknown=False,
                    status=response.status,
                    retry_after=retry_after,
                )
            if value.get("protocol_version") != PROTOCOL_VERSION:
                raise ClientError("host protocol version is unsupported")
            validate_response_limits(value)
            return value
        except ClientError as exc:
            if method == "POST" and exc.status is None:
                exc.unknown = True
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise ClientError(f"host connection failed: {_bounded(exc)}", unknown=method == "POST") from exc
        finally:
            connection.close()


def _new_request_id(value: Any = None) -> str:
    if value is None:
        return _id("req-")
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ClientError("request_id is invalid")
    return value


def _display_order_keys(value: Any) -> list[str]:
    """Validate the opaque output keys accepted by the order endpoint.

    The client may forward keys it received from the host, but it never turns
    them into connector arguments or shell text. The host remains responsible
    for resolving each key against a fresh inventory.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= DISPLAY_ORDER_MAX_OUTPUTS:
        raise ClientError("display order must contain 1 to 16 output keys")
    keys: list[str] = []
    for item in value:
        if not isinstance(item, str) or not DISPLAY_OUTPUT_KEY_RE.fullmatch(item):
            raise ClientError("display order contains an invalid output key")
        keys.append(item)
    if len(set(keys)) != len(keys):
        raise ClientError("display order contains duplicate output keys")
    return keys


def _display_order_generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2_147_483_647:
        raise ClientError("display order generation is invalid")
    return value


def send_wol(mac: Any, interface: Any = None, port: Any = 9) -> dict[str, Any]:
    if not isinstance(mac, str):
        raise ClientError("wake MAC is invalid")
    normalized = mac.replace(":", "").replace("-", "").lower()
    if not MAC_RE.fullmatch(normalized):
        raise ClientError("wake MAC is invalid")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ClientError("wake port is invalid")
    iface = None
    if interface is not None and interface != "":
        if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", interface):
            raise ClientError("wake interface is invalid")
        iface = interface
    packet = bytes.fromhex("ff" * 6 + normalized * 16)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if iface:
            bind_option = getattr(socket, "SO_BINDTODEVICE", None)
            if bind_option is None:
                raise ClientError("selected interface binding is unsupported")
            try:
                sock.setsockopt(socket.SOL_SOCKET, bind_option, (iface + "\0").encode())
            except OSError as exc:
                raise ClientError(f"selected interface could not be used: {_bounded(exc)}") from exc
        sent = sock.sendto(packet, ("255.255.255.255", port))
    except ClientError:
        raise
    except OSError as exc:
        raise ClientError(f"Wake-on-LAN packet was not sent: {_bounded(exc)}") from exc
    finally:
        sock.close()
    return {"packet_sent": sent == len(packet), "bytes": sent, "interface": iface, "mac": normalized, "port": port}


def validate_wake_values(mac: Any, interface: Any = None) -> tuple[str, str | None]:
    if not isinstance(mac, str):
        raise ClientError("wake MAC is invalid")
    normalized = mac.replace(":", "").replace("-", "").lower()
    if not MAC_RE.fullmatch(normalized):
        raise ClientError("wake MAC is invalid")
    if interface in (None, ""):
        return normalized, None
    if not isinstance(interface, str) or not INTERFACE_RE.fullmatch(interface):
        raise ClientError("wake interface is invalid")
    return normalized, interface


def _default_route_interface() -> str | None:
    """Return the local interface carrying the default IPv4 route."""
    try:
        lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000" or not INTERFACE_RE.fullmatch(fields[0]):
            continue
        try:
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x1:
            return fields[0]
    return None


def _active_wake_interface() -> str:
    """Select the Omarchy sender interface; do not reuse the host NIC name."""
    route_interface = _default_route_interface()
    if route_interface and route_interface != "lo":
        return route_interface
    try:
        interfaces = socket.if_nameindex()
    except (AttributeError, OSError):
        try:
            interfaces = [(0, path.name) for path in Path("/sys/class/net").iterdir()]
        except OSError:
            return ""
    for _, name in interfaces:
        if name == "lo" or not INTERFACE_RE.fullmatch(name):
            continue
        try:
            state = (Path("/sys/class/net") / name / "operstate").read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError):
            state = ""
        if state in {"up", "unknown"}:
            return name
    return ""


def _wake_target_details(value: Any) -> tuple[str, str, str]:
    """Validate host-advertised wake data and add the local sender interface."""
    if not isinstance(value, dict) or not value.get("mac"):
        return "", "", ""
    try:
        mac, host_interface = validate_wake_values(value.get("mac"), value.get("interface") or None)
    except ClientError:
        return "", "", ""
    return mac, host_interface or "", _active_wake_interface()


class ClientCore:
    def __init__(self, state_root: str | os.PathLike[str] | None = None):
        self.store = ClientStore(state_root)
        self.pending = PendingStore(state_root)

    # ---- pairing -------------------------------------------------------
    #
    # Pairing is a sequence of short calls rather than one blocking call. The
    # in-flight request lives in the private pending record, so the panel keeps
    # its helper lane free for status and wake while the owner walks over to
    # the Deck, and the pairing secret never travels back out to the panel.

    PAIR_TERMINAL_STATUS = frozenset({401, 403, 404, 409, 410})

    def _pair_body(self, pending: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "client_name": pending["client_name"],
            "client_id": pending["client_id"],
            "scopes": list(pending["scopes"]),
        }
        if pending["method"] == "verification":
            body["verification_nonce"] = pending["verification_nonce"]
        else:
            body["pairing_id"] = pending["pairing_id"]
            body["secret"] = pending["secret"]
        if pending.get("pairing_session"):
            body["pairing_session"] = pending["pairing_session"]
        return body

    def _pair_view(self, pending: dict[str, Any], **extra: Any) -> dict[str, Any]:
        view = {
            "state": "pending",
            "method": pending["method"],
            # Stable across polls of the same request, so a client can tell an
            # answer about this request from an answer about the next one.
            "client_id": pending["client_id"],
            "endpoint": pending["endpoint"],
            "certificate_fingerprint": pending["certificate_fingerprint"],
            "pairing_code": pending.get("pairing_code", ""),
            "expires_at": float(pending["expires_at"]),
            "seconds_remaining": max(0, int(math.ceil(float(pending["expires_at"]) - time.time()))),
        }
        view.update(extra)
        return view

    def _pair_store_credential(self, pending: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        credential = response.get("credential")
        if not isinstance(credential, dict) or not isinstance(credential.get("token"), str) or not credential.get("token"):
            raise ClientError("host returned an incomplete credential")
        resolved_host_id = response.get("host_id") or credential.get("host_id") or pending.get("host_id")
        if not isinstance(resolved_host_id, str) or not ID_RE.fullmatch(resolved_host_id):
            raise ClientError("host returned an incomplete identity")
        resolved_client_id = credential.get("client_id") or pending["client_id"]
        if not isinstance(resolved_client_id, str) or not ID_RE.fullmatch(resolved_client_id):
            raise ClientError("host returned an invalid client identity")
        granted_scopes = _pairing_scopes(credential.get("scopes", pending["scopes"]))
        wake_mac, wake_host_interface, wake_interface = _wake_target_details(response.get("wake_target"))
        state = {
            "protocol_version": PROTOCOL_VERSION,
            "endpoint": pending["endpoint"],
            "host_id": resolved_host_id,
            "certificate_fingerprint": pending["certificate_fingerprint"],
            "client_id": resolved_client_id,
            "token": credential["token"],
            "scopes": granted_scopes,
            "client_name": pending["client_name"],
            "paired_at": time.time(),
        }
        if wake_mac:
            state.update({
                "wake_mac": wake_mac,
                "wake_interface": wake_interface,
                "wake_host_interface": wake_host_interface,
            })
        self.store.save(state)
        self.pending.clear()
        return {
            "state": "approved",
            "client_id": state["client_id"],
            "scopes": state["scopes"],
            "endpoint": state["endpoint"],
            "wake_mac": wake_mac,
        }

    def _pair_exchange(self, pending: dict[str, Any]) -> dict[str, Any]:
        if time.time() >= float(pending["expires_at"]):
            self.pending.clear()
            raise ClientError("pairing request expired; start a new request")
        transport = PinnedTransport(
            pending["endpoint"], pending["certificate_fingerprint"], timeout=PAIRING_EXCHANGE_TIMEOUT
        )
        try:
            response = transport.request("POST", "/v1/pair/request", self._pair_body(pending))
        except ClientError as exc:
            if exc.status == 429:
                retry_after = 2.0 if exc.retry_after is None else max(0.5, min(float(exc.retry_after), 10.0))
                return self._pair_view(pending, retry_after=retry_after, notice="host is rate limiting the request")
            if exc.status in self.PAIR_TERMINAL_STATUS:
                self.pending.clear()
            raise
        state = response.get("state")
        if state == "approved":
            return self._pair_store_credential(pending, response)
        if state != "pending":
            self.pending.clear()
            raise ClientError("host returned an unsupported pairing state")
        session = response.get("pairing_session")
        if session is not None:
            if not isinstance(session, str) or not 16 <= len(session) <= 256:
                self.pending.clear()
                raise ClientError("host returned an invalid pairing session")
            pending["pairing_session"] = session
        self.pending.save(pending)
        return self._pair_view(pending)

    def pair_start(self, host: Any, client_name: Any = "Omarchy client", scopes: Any = None) -> dict[str, Any]:
        """Open a pairing request against a selected, pinned listener."""
        if not isinstance(host, dict):
            raise ClientError("select a discovered SteamOS Companion host first")
        endpoint = validate_endpoint(host.get("endpoint"))
        fingerprint = normalize_pin(host.get("certificate_fingerprint"))
        host_id = host.get("host_id")
        if host_id not in (None, "") and (not isinstance(host_id, str) or not ID_RE.fullmatch(host_id)):
            raise ClientError("discovered host identity is invalid")
        nonce = new_verification_nonce()
        pending = {
            "protocol_version": PROTOCOL_VERSION,
            "method": "verification",
            "endpoint": endpoint,
            "certificate_fingerprint": fingerprint,
            "host_id": host_id or "",
            "client_id": _id("client-"),
            "client_name": _pairing_name(client_name),
            "scopes": _pairing_scopes(scopes),
            "verification_nonce": nonce,
            # Derived from the pinned certificate, so this is what the real
            # host will show. A relay with another certificate shows different
            # digits and the owner sees the mismatch before approving.
            "pairing_code": _display_pairing_code(derive_pairing_code(nonce, fingerprint)),
            "pairing_session": "",
            "created_at": time.time(),
            "expires_at": time.time() + PAIRING_REQUEST_TIMEOUT_SECONDS,
        }
        return self._pair_open(pending)

    def pair_payload_start(
        self,
        payload_value: Any,
        client_name: Any = "Omarchy client",
        scopes: Any = None,
        endpoint: Any = None,
    ) -> dict[str, Any]:
        """Open a pairing request from the advanced full payload fallback."""
        payload = parse_pairing_payload(payload_value)
        target_endpoint = payload["endpoint"] if endpoint in (None, "") else validate_endpoint(endpoint)
        pending = {
            "protocol_version": PROTOCOL_VERSION,
            "method": "payload",
            "endpoint": target_endpoint,
            "certificate_fingerprint": payload["certificate_fingerprint"],
            "host_id": payload["host_id"],
            "client_id": _id("client-"),
            "client_name": _pairing_name(client_name),
            "scopes": _pairing_scopes(scopes),
            "pairing_id": payload["pairing_id"],
            "secret": payload["secret"],
            "pairing_session": "",
            "created_at": time.time(),
            "expires_at": min(float(payload["expires_at"]), time.time() + PAIRING_REQUEST_TIMEOUT_SECONDS),
        }
        return self._pair_open(pending)

    def _pair_open(self, pending: dict[str, Any]) -> dict[str, Any]:
        """Run the first exchange, which is what creates the host-side request.

        If it does not complete there is nothing to poll for, so the record is
        dropped rather than left to resurface as a pending request the host
        never saw.
        """
        self.pending.save(pending)
        try:
            return self._pair_exchange(pending)
        except ClientError:
            self.pending.clear()
            raise

    def pair_poll(self) -> dict[str, Any]:
        pending = self.pending.load(required=True)
        return self._pair_exchange(pending)

    def pair_cancel(self) -> dict[str, Any]:
        self.pending.clear()
        return {"state": "cancelled"}

    def pending_pairing(self) -> dict[str, Any] | None:
        pending = self.pending.load()
        if pending is None:
            return None
        if time.time() >= float(pending["expires_at"]):
            self.pending.clear()
            return None
        return self._pair_view(pending)

    @staticmethod
    def _timeout(value: Any) -> float:
        if value is None:
            return DEFAULT_TIMEOUT
        try:
            return max(1.0, min(float(value), 10.0))
        except (TypeError, ValueError) as exc:
            raise ClientError("timeout is invalid") from exc

    def _remember_operation(
        self,
        state: dict[str, Any],
        response: dict[str, Any],
        *,
        kind: str,
        request_id: str,
        output_keys: list[str] | None = None,
        generation: int | None = None,
    ) -> None:
        """Keep only a bounded operation handle for reconnect reconciliation.

        A restart response can be followed by a deliberate host disconnect. A
        full response is not needed in private state; retaining the validated
        operation ID lets the next panel instance query the original journal
        entry without replaying the mutation.
        """
        operation = response.get("operation")
        if not isinstance(operation, dict):
            return
        operation_id = operation.get("id")
        if not isinstance(operation_id, str) or not ID_RE.fullmatch(operation_id):
            return
        operation_state = operation.get("state", "accepted")
        if not isinstance(operation_state, str) or operation_state not in OPERATION_STATES:
            operation_state = "accepted"
        if operation_state in {"succeeded", "failed"}:
            pending = state.get("pending_operation")
            if not isinstance(pending, dict) or pending.get("id") == operation_id:
                state.pop("pending_operation", None)
        else:
            pending_operation = {
                "id": operation_id,
                "request_id": request_id,
                "state": operation_state,
                "kind": kind[:64],
            }
            if output_keys is not None:
                pending_operation["output_keys"] = list(output_keys)
                pending_operation["generation"] = generation
            state["pending_operation"] = pending_operation
        self.store.save(state)

    def _reconcile_operation(self, state: dict[str, Any], response: dict[str, Any]) -> None:
        """Clear the matching private handle only after a terminal readback."""
        pending = state.get("pending_operation")
        operation = response.get("operation")
        if not isinstance(pending, dict) or not isinstance(operation, dict):
            return
        operation_state = operation.get("state")
        if (
            operation.get("id") != pending.get("id")
            or not isinstance(operation_state, str)
            or operation_state not in {"succeeded", "failed"}
        ):
            return
        state.pop("pending_operation", None)
        self.store.save(state)

    def request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        state = self.store.load()
        timeout = self._timeout(arguments.get("timeout"))

        def transport() -> PinnedTransport:
            return PinnedTransport(state["endpoint"], state["certificate_fingerprint"], state["token"], timeout=timeout)

        if action == "status":
            response = transport().request("GET", "/v1/status")
            self._remember_wake_target(response)
            return response
        if action == "outputs":
            return transport().request("GET", "/v1/display/outputs")
        if action == "display-order":
            return transport().request("GET", "/v1/display/order")
        if action == "operation":
            operation_id = arguments.get("operation_id")
            if not isinstance(operation_id, str) or not ID_RE.fullmatch(operation_id):
                raise ClientError("operation_id is invalid")
            response = transport().request("GET", "/v1/operations/" + operation_id)
            self._reconcile_operation(state, response)
            return response
        if action in {"display-order-save", "display-order-restart"}:
            keys = _display_order_keys(arguments.get("output_keys"))
            generation = _display_order_generation(arguments.get("generation"))
            request_id = _new_request_id(arguments.get("request_id"))
            body = {
                "request_id": request_id,
                "output_keys": keys,
                "generation": generation,
                "restart": action == "display-order-restart",
            }
            response = transport().request("POST", "/v1/display/order", body)
            self._remember_operation(
                state,
                response,
                kind=action,
                request_id=request_id,
                output_keys=keys,
                generation=generation,
            )
            return response
        if action == "display-order-reset":
            request_id = _new_request_id(arguments.get("request_id"))
            response = transport().request("POST", "/v1/display/order/automatic", {"request_id": request_id})
            self._remember_operation(state, response, kind=action, request_id=request_id)
            return response
        routes = {
            "power": ("POST", "/v1/power"),
            "preview": ("POST", "/v1/display/preview"),
            "confirm": ("POST", "/v1/display/confirm"),
            "restore": ("POST", "/v1/display/restore"),
            "sunshine-restart": ("POST", "/v1/sunshine/restart"),
            "revoke": ("POST", "/v1/pair/revoke-self"),
        }
        if action not in routes:
            raise ClientError("client action is unsupported")
        method, path = routes[action]
        body = dict(arguments)
        # Helper-only controls never reach the host body.
        body.pop("timeout", None)
        if action != "power":
            body.pop("action", None)
        body["request_id"] = _new_request_id(body.get("request_id"))
        response = transport().request(method, path, body)
        self._remember_operation(state, response, kind=action, request_id=body["request_id"])
        return response

    def configure_endpoint(self, endpoint: Any) -> dict[str, Any]:
        state = self.store.load()
        endpoint = validate_endpoint(endpoint)
        response = PinnedTransport(endpoint, state["certificate_fingerprint"], state["token"]).request("GET", "/v1/status")
        if response.get("host_id") != state["host_id"]:
            raise ClientError("endpoint responded as a different pinned host")
        state["endpoint"] = endpoint
        self.store.save(state)
        return {"saved": True, "endpoint": endpoint}

    def inspect(self) -> dict[str, Any]:
        state = self.store.load(required=False)
        pending = self.pending_pairing()
        if state is None:
            return {"paired": False, "pending_pairing": pending}
        return {
            "pending_pairing": pending,
            "paired": True,
            "endpoint": state["endpoint"],
            "host_id": state["host_id"],
            "certificate_fingerprint": state["certificate_fingerprint"],
            "client_id": state["client_id"],
            "client_name": state.get("client_name", "Omarchy client"),
            "scopes": state.get("scopes", []),
            "wake_mac": state.get("wake_mac", ""),
            "wake_interface": state.get("wake_interface", ""),
            "wake_host_interface": state.get("wake_host_interface", ""),
            "show_nonstandard_refresh_rates": state.get("show_nonstandard_refresh_rates", False),
            "pending_operation": state.get("pending_operation"),
        }

    def _remember_wake_target(self, response: dict[str, Any]) -> None:
        mac, host_interface, local_interface = _wake_target_details(response.get("wake_target"))
        if not mac:
            return
        state = self.store.load()
        changed = False
        for key, value in (
            ("wake_mac", mac),
            ("wake_host_interface", host_interface),
        ):
            if state.get(key, "") != value:
                state[key] = value
                changed = True
        if local_interface and state.get("wake_interface", "") != local_interface:
            state["wake_interface"] = local_interface
            changed = True
        if changed:
            self.store.save(state)

    def configure_wake(self, mac: Any, interface: Any = None) -> dict[str, Any]:
        state = self.store.load()
        normalized, iface = validate_wake_values(mac, interface)
        state["wake_mac"] = normalized
        state["wake_interface"] = iface or ""
        self.store.save(state)
        return {"saved": True, "wake_mac": normalized, "wake_interface": iface or ""}

    def configure_display(self, show_nonstandard_refresh_rates: Any = False) -> dict[str, Any]:
        state = self.store.load()
        if not isinstance(show_nonstandard_refresh_rates, bool):
            raise ClientError("show_nonstandard_refresh_rates must be boolean")
        state["show_nonstandard_refresh_rates"] = show_nonstandard_refresh_rates
        self.store.save(state)
        return {"saved": True, "show_nonstandard_refresh_rates": show_nonstandard_refresh_rates}

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ClientError("helper request must be an object")
        action = request.get("action")
        if action == "discover":
            return discover_hosts(request.get("port", DISCOVERY_DEFAULT_PORT))
        if action == "pair-start":
            return self.pair_start(
                request.get("host"),
                request.get("client_name"),
                request.get("scopes"),
            )
        if action == "pair":
            return self.pair_payload_start(
                request.get("payload"),
                request.get("client_name"),
                request.get("scopes"),
                request.get("endpoint"),
            )
        if action == "pair-poll":
            return self.pair_poll()
        if action == "pair-cancel":
            return self.pair_cancel()
        if action == "forget":
            self.store.forget()
            self.pending.clear()
            return {"forgotten": True}
        if action == "configure":
            return self.configure_endpoint(request.get("endpoint"))
        if action == "inspect":
            return self.inspect()
        if action == "configure-wake":
            return self.configure_wake(request.get("mac"), request.get("interface"))
        if action == "configure-display":
            return self.configure_display(request.get("show_nonstandard_refresh_rates", False))
        if action == "wake":
            return send_wol(request.get("mac"), request.get("interface"), request.get("port", 9))
        # Keep direct helper actions compatible with older panel builds. The
        # transport route is still the fixed /v1/power endpoint; no caller can
        # select an arbitrary host path or Steam method.
        if action in {"suspend", "restart", "shutdown", "reboot"}:
            forwarded = dict(request)
            forwarded["action"] = "restart" if action == "reboot" else action
            return self.request("power", forwarded)
        return self.request(str(action), request)
