#!/usr/bin/env python3
"""One bounded helper invocation for the Omarchy QML plugin."""

from __future__ import annotations

import json
import sys

from client_core import ClientCore, ClientError


def main() -> int:
    line = sys.stdin.readline(256 * 1024 + 1)
    if not line:
        result = {"ok": False, "error": "helper request is empty", "unknown": False}
    else:
        try:
            request = json.loads(line)
            result = {"ok": True, "data": ClientCore().run(request)}
        except ClientError as exc:
            result = {"ok": False, "error": str(exc)[:256], "unknown": exc.unknown}
            if exc.status is not None:
                result["status"] = exc.status
            if exc.retry_after is not None:
                result["retry_after"] = exc.retry_after
        except Exception:
            result = {"ok": False, "error": "client helper failed", "unknown": False}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
