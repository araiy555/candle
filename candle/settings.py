"""管理画面から変更できる売買設定。data/settings.json に保存する。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

STOP_MODES = {"pattern_low": "パターンの安値", "percent": "買値から一定率（%）下", "yen": "買値から一定額（円）下"}
TRAIL_MODES = {"r_step": "リスク幅（R）ごとに引き上げ", "percent": "高値から一定率（%）下に追従",
               "yen": "高値から一定額（円）下に追従", "none": "トレーリングなし"}


@dataclass
class TradeSettings:
    stop_mode: str = "pattern_low"
    stop_value: float = 0.5        # percent なら %、yen なら円
    trail_mode: str = "r_step"
    trail_value: float = 0.5       # percent なら %、yen なら円
    per_trade_cap: float = 500_000   # 1回の買いの上限（円）
    total_cap: float = 1_000_000     # 同時に持つ合計の上限（円）
    daily_max_loss: float = 10_000   # 1日の最大損失（円）

    def validate(self) -> None:
        if self.stop_mode not in STOP_MODES:
            raise ValueError("損切りラインの種類が不正です")
        if self.trail_mode not in TRAIL_MODES:
            raise ValueError("トレーリングの種類が不正です")
        if self.stop_mode != "pattern_low" and not self.stop_value > 0:
            raise ValueError("損切りの数値は0より大きくしてください")
        if self.stop_mode == "percent" and self.stop_value >= 20:
            raise ValueError("損切りの率は20%未満にしてください")
        if self.trail_mode in ("percent", "yen") and not self.trail_value > 0:
            raise ValueError("トレーリングの数値は0より大きくしてください")
        if self.trail_mode == "percent" and self.trail_value >= 20:
            raise ValueError("トレーリングの率は20%未満にしてください")
        if min(self.per_trade_cap, self.total_cap, self.daily_max_loss) <= 0:
            raise ValueError("金額は0より大きくしてください")
        if self.per_trade_cap > self.total_cap:
            raise ValueError("1回の上限は、合計の上限以下にしてください")

    def initial_stop(self, entry: float, pattern_low: float) -> float:
        """買値とパターン安値から初期の損切りラインを決める。"""
        if self.stop_mode == "percent":
            return entry * (1 - self.stop_value / 100)
        if self.stop_mode == "yen":
            return entry - self.stop_value
        return pattern_low

    def apply_to(self, limits) -> None:
        limits.per_symbol_cap = self.per_trade_cap
        limits.total_cap = self.total_cap
        limits.daily_max_loss = self.daily_max_loss

    @classmethod
    def from_dict(cls, data: dict) -> "TradeSettings":
        base = asdict(cls())
        for k, v in data.items():
            if k in base:
                base[k] = type(base[k])(v)
        s = cls(**base)
        s.validate()
        return s

    @classmethod
    def load(cls, path: Path | None) -> "TradeSettings":
        if path and path.exists():
            try:
                return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, OSError):
                pass
        return cls()

    def save(self, path: Path | None) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
