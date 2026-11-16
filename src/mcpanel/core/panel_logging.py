"""Panel log rotation and quiet filtering for routine dashboard polling."""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SafePanelRotatingHandler(RotatingFileHandler):
    def __init__(self, filename: Path, data_dir: Path, max_bytes: int, backup_count: int):
        self.data_dir = data_dir.resolve()
        resolved = filename.resolve()
        resolved.relative_to(self.data_dir)
        super().__init__(resolved, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        base = Path(self.baseFilename)
        for index in range(self.backupCount - 1, 0, -1):
            source = base.with_name(base.name + f".{index}")
            destination = base.with_name(base.name + f".{index + 1}")
            if source.exists():
                source.resolve().relative_to(self.data_dir)
                if destination.exists():
                    destination.resolve().relative_to(self.data_dir)
                    destination.unlink()
                os.replace(source, destination)
        first = base.with_name(base.name + ".1")
        if base.exists():
            base.resolve().relative_to(self.data_dir)
            if first.exists():
                first.resolve().relative_to(self.data_dir)
                first.unlink()
            os.replace(base, first)
        if not self.delay:
            self.stream = self._open()


class PollingAccessFilter(logging.Filter):
    _paths = {
        "/api/profiles/stats",
        "/api/profiles/partials/list",
        "/api/lifecycle/startup-events",
        "/api/monitoring/stats",
        "/api/monitoring/history",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) < 5:
            return True
        path = str(record.args[2]).split("?", 1)[0]
        try:
            status = int(record.args[4])
        except (TypeError, ValueError):
            return True
        return status >= 400 or path not in self._paths
