"""管理画面から変更できる売買設定。data/settings.json に保存する。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

STOP_MODES = {"pattern_low": "パターンの安値", "percent": "買値から一定率（%）下", "yen": "買値から一定額（円）下"}
TRAIL_MODES = {"r_step": "リスク幅（R）ごとに引き上げ", "percent": "高値から一定率（%）下に追従",
               "yen": "高値から一定額（円）下に追従", "none": "トレーリングなし"}

OLD_NOTIFY = "tweezer_bottom,squeeze,fvg_hold,fvg_break,wall_break,liq_sweep,open_dip"


@dataclass
class TradeSettings:
    stop_mode: str = "pattern_low"
    stop_value: float = 0.5        # percent なら %、yen なら円
    trail_mode: str = "r_step"
    trail_value: float = 0.5       # percent なら %、yen なら円
    per_trade_cap: float = 500_000   # 1回の買いの上限（円）
    total_cap: float = 1_000_000     # 同時に持つ合計の上限（円）
    size_mode: str = "yen"           # 上限の決め方: yen=金額で指定, percent=信用の買付余力に対する割合で指定
    per_trade_pct: float = 10        # percent のとき: 1回の買いに使う割合（%）
    total_pct: float = 30            # percent のとき: 同時に持つ合計の割合（%）。100で余力いっぱい
    daily_max_loss: float = 10_000   # 1日の最大損失（円）
    max_hold_min: float = 0          # 最大保有時間（分）。0なら時間では決済しない
    quiet_open_min: float = 15       # 寄り付き（9:00）からこの分数は、通知も売買もしない
    min_price: float = 500           # 株価の下限（円）。これより安い銘柄は通知も売買もしない。0なら除かない
    # 通知するサイン（戦略IDをカンマ区切り）。管理画面のチェックで選ぶ
    notify_on: str = "tweezer_bottom,squeeze,fvg_hold,fvg_break,wall_break,liq_sweep,open_dip"
    # 画面に出したことのあるサイン。後から増えたサインを、保存済みの設定にも初期状態（オン）で足すために使う
    notify_seen: str = "tweezer_bottom,squeeze,fvg_hold,fvg_break,wall_break,liq_sweep,open_dip"
    min_range_pct: float = 0.15      # 足の平均値幅率の下限（%）。これ未満は値動きが小さい（レンジ）とみなして見送る。0なら除かない
    vwap_rule: str = "above"        # above=株価がVWAPより下のときは買わない / off=VWAPを見ない
    min_drop_pct: float = 0.3        # 直前の下落率の下限（%）。横ばいの中のサインを除く。0なら除かない
    mkt_fut_drop: float = 0.3        # 地合い: 先物が5分でこの%以上下げたら「警戒」
    mkt_topix_drop: float = 0.15     # 地合い: 加えてTOPIXが5分でこの%以上下げたら「警戒強化」
    mkt_breadth: float = 70          # 地合い: 監視銘柄のこの%以上が5分前より下なら「新規買い停止」。0なら止めない

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
        if not 0 <= self.quiet_open_min <= 120:
            raise ValueError("寄り付き後に待つ時間は0〜120分にしてください")
        if not 0 <= self.min_drop_pct <= 10:
            raise ValueError("直前の下落率の下限は0〜10%にしてください")
        if min(self.mkt_fut_drop, self.mkt_topix_drop) <= 0 or not 0 <= self.mkt_breadth <= 100:
            raise ValueError("地合いの基準は、下落率は0より大きく、割合は0〜100%にしてください")
        if self.size_mode not in ("yen", "percent"):
            raise ValueError("上限の決め方が不正です")
        if not (0 < self.per_trade_pct <= 100 and 0 < self.total_pct <= 100):
            raise ValueError("余力に対する割合は、0より大きく100%以下にしてください")
        if self.per_trade_pct > self.total_pct:
            raise ValueError("1回の割合は、合計の割合以下にしてください")
        if not 0 <= self.min_range_pct <= 5:
            raise ValueError("平均値幅率の下限は0〜5%にしてください")
        if self.vwap_rule not in ("above", "off"):
            raise ValueError("VWAPの条件が不正です")
        if self.min_price < 0:
            raise ValueError("株価の下限は0以上にしてください")
        if self.max_hold_min < 0:
            raise ValueError("最大保有時間は0以上にしてください")
        if min(self.per_trade_cap, self.total_cap, self.daily_max_loss) <= 0:
            raise ValueError("金額は0より大きくしてください")
        if self.per_trade_cap > self.total_cap:
            raise ValueError("1回の上限は、合計の上限以下にしてください")

    def notifies(self, strategy_id: str) -> bool:
        return strategy_id in self.notify_on.split(",")

    def initial_stop(self, entry: float, pattern_low: float) -> float:
        """買値とパターン安値から初期の損切りラインを決める。"""
        if self.stop_mode == "percent":
            return entry * (1 - self.stop_value / 100)
        if self.stop_mode == "yen":
            return entry - self.stop_value
        return pattern_low

    def apply_to(self, limits, capacity: float | None = None) -> bool:
        """上限をリスク管理に反映する。capacity は信用で建てられる総額（余力＋すでに建てている分）。

        割合で指定していて capacity が分かっていれば、その割合から金額を決める。
        分からない場合は金額の指定にとどめる（戻り値 False）。知らない金額で大きく買わないため。
        """
        limits.daily_max_loss = self.daily_max_loss
        if self.size_mode == "percent" and capacity is not None and capacity > 0:
            limits.per_symbol_cap = capacity * self.per_trade_pct / 100
            limits.total_cap = capacity * self.total_pct / 100
            return True
        limits.per_symbol_cap = self.per_trade_cap
        limits.total_cap = self.total_cap
        return self.size_mode != "percent"

    @classmethod
    def from_dict(cls, data: dict) -> "TradeSettings":
        base = asdict(cls())
        for k, v in data.items():
            if k in base:
                base[k] = type(base[k])(v)
        if "notify_on" in data:
            # 保存した後に増えたサインは、オンの状態で加える（旧版の設定には notify_seen が無い）
            seen = str(data.get("notify_seen", OLD_NOTIFY)).split(",")
            new = [x for x in cls().notify_seen.split(",") if x not in seen]
            if new:
                base["notify_on"] = ",".join([x for x in base["notify_on"].split(",") if x] + new)
            base["notify_seen"] = cls().notify_seen
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
