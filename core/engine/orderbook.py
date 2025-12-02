# core/engine/orderbook.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


@dataclass
class LocalOrderBook:
    symbol: str
    limit: int
    last_update_id: Optional[int] = None

    # dict: price -> size
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)

    def _sorted_bids(self) -> List[Tuple[float, float]]:
        # descending by price
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[: self.limit]

    def _sorted_asks(self) -> List[Tuple[float, float]]:
        # ascending by price
        return sorted(self.asks.items(), key=lambda x: x[0])[: self.limit]

    def best_bid(self) -> Optional[Tuple[float, float]]:
        sb = self._sorted_bids()
        return sb[0] if sb else None

    def best_ask(self) -> Optional[Tuple[float, float]]:
        sa = self._sorted_asks()
        return sa[0] if sa else None

    # --- snapshot completo ---
    def apply_snapshot(self, asks: list[list[str]], bids: list[list[str]], update_id: int) -> None:
        self.asks.clear()
        self.bids.clear()

        for price_str, size_str in asks:
            price = float(price_str)
            size = float(size_str)
            if size > 0:
                self.asks[price] = size

        for price_str, size_str in bids:
            price = float(price_str)
            size = float(size_str)
            if size > 0:
                self.bids[price] = size

        self._truncate()
        self.last_update_id = update_id

    # --- update incrementale ---
    def apply_increment(
        self,
        asks: list[list[str]],
        bids: list[list[str]],
        update_id: int,
        past_update_id: int,
    ) -> bool:
        """
        Ritorna True se update applicato, False se c'è mismatch (serve resync).
        """
        if self.last_update_id is not None and past_update_id != self.last_update_id:
            # out-of-sync, chi ci usa deve fare resync
            return False

        # asks e bids contengono solo delta: size "0" = cancella livello
        for price_str, size_str in asks:
            price = float(price_str)
            size = float(size_str)
            if size <= 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size

        for price_str, size_str in bids:
            price = float(price_str)
            size = float(size_str)
            if size <= 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

        self._truncate()
        self.last_update_id = update_id
        return True

    def _truncate(self) -> None:
        # mantieni solo top-N livelli
        if len(self.bids) > self.limit:
            for price, _ in list(self._sorted_bids())[self.limit :]:
                self.bids.pop(price, None)
        if len(self.asks) > self.limit:
            for price, _ in list(self._sorted_asks())[self.limit :]:
                self.asks.pop(price, None)
