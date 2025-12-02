# core/engine/risk_spot.py
from dataclasses import dataclass
from ..models import MarketState, InventoryState


@dataclass
class RiskView:
    allow_bid: bool
    allow_ask: bool
    inventory_skew: float   # [-1,1]
    widen_factor: float     # >=1, per hidden spread
    panic_mode: bool


class SpotRiskEngine:
    def __init__(self, global_cfg: dict, logger):
        self.global_cfg = global_cfg
        self.logger = logger

    def assess(self, symbol: str, mkt: MarketState, inv: InventoryState, sym_cfg: dict) -> RiskView:
        # TODO: logica vera (inventory limit, vol, PnL, ecc.)
        # placeholder: sempre ok
        return RiskView(
            allow_bid=True,
            allow_ask=True,
            inventory_skew=0.0,
            widen_factor=1.0,
            panic_mode=False,
        )
