"""ZCode 数据目录定位与结构校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Paths:
    zcode_dir: Path
    cli_dir: Path
    db_path: Path
    tasks_db: Path

    @classmethod
    def from_dir(cls, d: Path) -> "Paths":
        return cls(zcode_dir=d, cli_dir=d / "cli", db_path=d / "cli" / "db" / "db.sqlite",
                   tasks_db=d / "v2" / "tasks-index.sqlite")

    @classmethod
    def default(cls) -> "Paths | None":
        cand = Path.home() / ".zcode"
        return cls.from_dir(cand) if looks_valid(cand) else None

    def backups_base(self, custom: str | None) -> Path:
        if custom and custom.strip():
            return Path(custom.strip())
        return self.zcode_dir / "zsm-backups"


def looks_valid(d: Path) -> bool:
    return (d / "cli" / "db" / "db.sqlite").is_file() or (d / "v2" / "tasks-index.sqlite").is_file()
