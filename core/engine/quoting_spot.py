# core/engine/quoting_spot.py
import time
from ..models import MarketState, InventoryState, SideQuotes, QuoteLevel
from .risk_spot import RiskView


class SpotQuoter:
    def __init__(self, symbol: str, sym_cfg: dict, logger):
        self.symbol = symbol
        self.cfg = sym_cfg
        self.logger = logger
        self.tick_size = sym_cfg["tick_size"]
        self.levels_per_side = sym_cfg.get("levels_per_side", 1)

    def compute_quotes(
        self,
        mkt: MarketState,
        inv: InventoryState,
        risk: RiskView,
    ) -> SideQuotes:
        """
        Versione ultra-semplificata: 1 livello per lato attaccato al mid.
        Poi ci mettiamo multi-level, skew, anti-sniping, ecc.
        """
        mid = (mkt.best_bid + mkt.best_ask) / 2
        spread_ticks = self.cfg.get("target_spread_ticks", 1)
        hidden_spread_factor = self.cfg.get("aggressiveness", {}).get("hidden_spread_factor", 0.0)

        # base spread in price
        base_spread = spread_ticks * self.tick_size * max(1.0, risk.widen_factor * (1 + hidden_spread_factor))

        bid_price = mid - base_spread / 2
        ask_price = mid + base_spread / 2

        # arrotonda ai tick
        bid_price = bid_price - (bid_price % self.tick_size)
        ask_price = ask_price - (ask_price % self.tick_size)
        if ask_price <= bid_price:
            ask_price = bid_price + self.tick_size

        base_size = self.cfg["base_order_size"]

        bids = []
        asks = []

        if risk.allow_bid:
            bids.append(QuoteLevel(price=bid_price, size=base_size, level=0))
        if risk.allow_ask:
            asks.append(QuoteLevel(price=ask_price, size=base_size, level=0))

        return SideQuotes(symbol=self.symbol, bids=bids, asks=asks, ts=time.time())
