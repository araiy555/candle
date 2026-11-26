from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .broker import LiveConfig
from .risk import RiskLimits


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
    risk: RiskLimits = field(default_factory=RiskLimits)
    slippage: float = 0.0005
    exit_after: str = "15:20"
    disabled_strategies: list[str] = field(default_factory=list)
    web_port: int = 8765
    watch_strategies: list[str] = field(default_factory=lambda: ["tweezer_bottom", "squeeze", "fvg_hold", "fvg_break", "wall_break", "liq_sweep", "open_dip"])
    notify_desktop: bool = True
    notify_sound: bool = True
    webhook_url: str = ""
    notify_copy_code: bool = True
    live: LiveConfig = field(default_factory=LiveConfig)
    market_future: str = "NK225"     # 地合いの判定に使う先物（空なら使わない）
    market_future_exchange: int = 2  # 2=日通し, 23=日中
    market_topix: str = "151"        # TOPIXの銘柄コード（空なら使わない）
    trade_strategies: list[str] = field(default_factory=lambda: ["tweezer_bottom"])

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
    raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))  # メモ帳のBOM付き保存にも対応
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
        risk=RiskLimits(**raw.get("risk", {})),
        slippage=float(raw.get("trading", {}).get("slippage", 0.0005)),
        exit_after=str(raw.get("trading", {}).get("exit_after", "15:20")),
        disabled_strategies=list(raw.get("trading", {}).get("disabled_strategies", [])),
        web_port=int(raw.get("web", {}).get("port", 8765)),
        watch_strategies=list(raw.get("notify", {}).get("strategies",
                                                         ["tweezer_bottom", "squeeze", "fvg_hold", "fvg_break", "wall_break", "liq_sweep", "open_dip"])),
        notify_desktop=bool(raw.get("notify", {}).get("desktop", True)),
        notify_sound=bool(raw.get("notify", {}).get("sound", True)),
        webhook_url=str(raw.get("notify", {}).get("webhook_url", "")),
        notify_copy_code=bool(raw.get("notify", {}).get("copy_code", True)),
        live=LiveConfig(**raw.get("live", {})),
        market_future=str(raw.get("market", {}).get("future", "NK225")),
        market_future_exchange=int(raw.get("market", {}).get("future_exchange", 2)),
        market_topix=str(raw.get("market", {}).get("topix", "151")),
        trade_strategies=list(raw.get("trading", {}).get("strategies", ["tweezer_bottom"])),
    )
    if not 1 <= cfg.universe_size <= 50:
        raise ValueError("universe.size は 1〜50 で指定してください（API登録上限は50銘柄）")
    return cfg
