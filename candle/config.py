from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "localhost"
    port: int = 18080
    universe_size: int = 45
    exchange_division: str = "T"
    symbols: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    data_dir: Path = Path("data")
    record_raw: bool = True
    bar_minutes: int = 5

    @property
    def rest_url(self) -> str:
        return f"http://{self.host}:{self.port}/kabusapi"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/kabusapi/websocket"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "candle.sqlite"

    @property
    def api_password(self) -> str:
        pw = os.environ.get("KABU_API_PASSWORD", "")
        if not pw:
            raise RuntimeError("環境変数 KABU_API_PASSWORD が設定されていません")
        return pw


def load_config(path: str | Path | None = None) -> Config:
    """config.toml を読む。ファイルが無ければ既定値を使う。"""
    p = Path(path) if path else Path("config.toml")
    if not p.exists():
        if path:
            raise FileNotFoundError(p)
        return Config()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    api, uni = raw.get("api", {}), raw.get("universe", {})
    data, bars = raw.get("data", {}), raw.get("bars", {})
    cfg = Config(
        host=api.get("host", "localhost"),
        port=int(api.get("port", 18080)),
        universe_size=int(uni.get("size", 45)),
        exchange_division=uni.get("exchange_division", "T"),
        symbols=[str(s) for s in uni.get("symbols", [])],
        exclude=[str(s) for s in uni.get("exclude", [])],
        data_dir=Path(data.get("dir", "data")),
        record_raw=bool(data.get("record_raw", True)),
        bar_minutes=int(bars.get("minutes", 5)),
    )
    if not 1 <= cfg.universe_size <= 50:
        raise ValueError("universe.size は 1〜50 で指定してください（API登録上限は50銘柄）")
    return cfg
