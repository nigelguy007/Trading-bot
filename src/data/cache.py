"""
TTL cache backed by JSON files on disk.

Rate-limit bans come from hammering an API in a tight loop. Every outbound
data call goes through here, so a restart doesn't re-fetch what we already
have and a 15-minute scan loop doesn't make 15-minute-old requests again.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self, directory: Path, namespace: str = "default") -> None:
        self.dir = directory / "cache" / namespace
        self.dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, tuple[float, Any]] = {}

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return self.dir / f"{digest}.json"

    def get(self, key: str, ttl: int) -> Optional[Any]:
        hit = self._memory.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]

        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if (time.time() - payload.get("stored_at", 0)) >= ttl:
            return None
        self._memory[key] = (payload["stored_at"], payload["value"])
        return payload["value"]

    def set(self, key: str, value: Any) -> None:
        stored_at = time.time()
        self._memory[key] = (stored_at, value)
        try:
            self._path(key).write_text(
                json.dumps({"key": key, "stored_at": stored_at, "value": value}),
                encoding="utf-8",
            )
        except (OSError, TypeError):
            # A cache write failing must never take down a scan.
            pass

    def get_or_fetch(self, key: str, ttl: int, fetch: Callable[[], Any]) -> Any:
        cached = self.get(key, ttl)
        if cached is not None:
            return cached
        value = fetch()
        if value is not None:
            self.set(key, value)
        return value

    def clear(self) -> int:
        self._memory.clear()
        removed = 0
        for f in self.dir.glob("*.json"):
            f.unlink(missing_ok=True)
            removed += 1
        return removed
