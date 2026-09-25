"""Local transcript history: JSONL, text only (never audio), rotates at a size cap."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class History:
    def __init__(self, path: Path, enabled: bool = True, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.path = path
        self.enabled = enabled
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _rotate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                backup.unlink(missing_ok=True)
                self.path.rename(backup)
        except OSError as e:
            log.warning("history rotation failed: %s", e)

    def append(self, **entry) -> None:
        if not self.enabled:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                log.warning("history write failed: %s", e)

    def recent(self, n: int = 20) -> list[dict]:
        files = [self.path.with_suffix(self.path.suffix + ".1"), self.path]
        lines: list[str] = []
        for p in files:
            if p.exists():
                lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
        out = []
        for line in lines[-n:] if n > 0 else []:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
