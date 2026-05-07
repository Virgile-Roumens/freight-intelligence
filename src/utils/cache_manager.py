import os
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


class CacheManager:
    """Disk-based CSV cache that persists across Streamlit server restarts."""

    SUBDIRS = ["freight", "macro", "news", "supply"]

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        for sub in self.SUBDIRS:
            (self.base_dir / sub).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, subdir: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.base_dir / subdir / f"{safe_key}.csv"

    def save(self, key: str, df: pd.DataFrame, subdir: str = "freight") -> None:
        path = self._path(key, subdir)
        tmp = path.with_suffix(".tmp")
        try:
            df.to_csv(tmp, index=True)
            os.replace(tmp, path)  # atomic on Windows
        except Exception as e:
            logger.warning(f"Cache save failed for {key}: {e}")
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def load(self, key: str, subdir: str = "freight", max_age_hours: float = 24) -> pd.DataFrame | None:
        path = self._path(key, subdir)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df if not df.empty else None
        except Exception as e:
            logger.warning(f"Cache load failed for {key}: {e}")
            return None

    def invalidate(self, key: str, subdir: str = "freight") -> None:
        path = self._path(key, subdir)
        path.unlink(missing_ok=True)

    def invalidate_all(self) -> None:
        for sub in self.SUBDIRS:
            subpath = self.base_dir / sub
            for f in subpath.glob("*.csv"):
                f.unlink(missing_ok=True)

    def get_status(self) -> list[dict]:
        entries = []
        for sub in self.SUBDIRS:
            subpath = self.base_dir / sub
            for f in subpath.glob("*.csv"):
                stat = f.stat()
                age_h = (time.time() - stat.st_mtime) / 3600
                entries.append({
                    "key":        f.stem,
                    "subdir":     sub,
                    "age_hours":  round(age_h, 1),
                    "size_kb":    round(stat.st_size / 1024, 1),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        return entries
