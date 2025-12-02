# core/engine/order_manager.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..models import Side, Order, SideQuotes, QuoteLevel
from ..exchange.base import ExchangeClient


@dataclass
class ManagedOrder:
    """
    Ordine gestito dalla strategia per un dato (symbol, side, level).

    Non è l'order ufficiale dell'exchange, ma un wrapper che include:
    - l'Order restituito dall'exchange (id, price, amount, status, ...)
    - metadati di strategia (level, last_update, ecc.)
    """
    order: Order
    side: Side
    level: int
    last_update_ts: float = field(default_factory=time.time)


class OrderManager:
    """
    Order manager per MM:

    - Riceve le quote target per simbolo (SideQuotes)
    - Riconcilia contro gli ordini attivi per quel simbolo (in memoria)
    - Decide cosa creare / cancellare / modificare
    - Usa ExchangeClient per parlare con l'exchange (limit order + cancel)

    NOTA: al momento gestiamo solo ordini LIMIT, post-only per MM.
    """

    def __init__(
        self,
        client: ExchangeClient,
        logger,
        reprice_threshold_ticks: int = 1,
        size_change_threshold: float = 0.10,  # 10%
        min_reprice_interval_ms: int = 200,   # per evitare flip troppo frequenti
    ):
        self.client = client
        self.logger = logger
        self.reprice_threshold_ticks = reprice_threshold_ticks
        self.size_change_threshold = size_change_threshold
        self.min_reprice_interval_ms = min_reprice_interval_ms

        # symbol -> (order_id -> ManagedOrder)
        self._active_orders: Dict[str, Dict[str, ManagedOrder]] = {}

        # lock per simbolo per evitare race tra cicli async
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    def _get_symbol_orders(self, symbol: str) -> Dict[str, ManagedOrder]:
        if symbol not in self._active_orders:
            self._active_orders[symbol] = {}
        return self._active_orders[symbol]

    # ------------------------------------------------------------------ #
    # API principale usata dalla strategia per applicare le quote
    # ------------------------------------------------------------------ #

    async def sync_symbol_orders(self, symbol: str, quotes: SideQuotes) -> None:
        """
        Sincronizza gli ordini per 'symbol' con le quote target.

        - Per ogni quote (bid/ask, per level), assicura che ci sia un ordine consistente.
        - Cancella ordini che non hanno più una quote target associata.
        """
        async with self._get_lock(symbol):
            symbol_orders = self._get_symbol_orders(symbol)

            # costruisci mappa target: (side, level) -> QuoteLevel
            target_by_key: Dict[tuple[Side, int], QuoteLevel] = {}
            for q in quotes.bids:
                target_by_key[(Side.BUY, q.level)] = q
            for q in quotes.asks:
                target_by_key[(Side.SELL, q.level)] = q

            # costruisci mappa corrente: (side, level) -> ManagedOrder
            current_by_key: Dict[tuple[Side, int], ManagedOrder] = {}
            for mo in symbol_orders.values():
                current_by_key[(mo.side, mo.level)] = mo

            # 1) Per ogni target: crea o aggiorna
            for (side, level), q in target_by_key.items():
                existing = current_by_key.get((side, level))
                try:
                    if existing is None:
                        await self._create_new_order(symbol, side, level, q)
                    else:
                        await self._maybe_reprice_or_resize(symbol, existing, q)
                except Exception as e:
                    self.logger.exception(
                        "Error managing quote",
                        extra={
                            "symbol": symbol,
                            "side": side.value,
                            "level": level,
                            "price": q.price,
                            "size": q.size,
                            "error": str(e),
                        },
                    )

            # 2) Cancella ordini che non hanno più una quote target
            for (side, level), mo in current_by_key.items():
                if (side, level) not in target_by_key:
                    await self._cancel_managed_order(symbol, mo)

    # ------------------------------------------------------------------ #
    # Helpers: create / cancel / reprice
    # ------------------------------------------------------------------ #

    async def _create_new_order(
        self,
        symbol: str,
        side: Side,
        level: int,
        q: QuoteLevel,
    ) -> None:
        """
        Crea un nuovo limit order per una quote target.
        """
        # WhiteBIT spot: vogliamo POST /api/v4/order/new limit, side buy/sell, postOnly true :contentReference[oaicite:2]{index=2}
        order = await self.client.place_limit_order(
            symbol=symbol,
            side=side,
            price=q.price,
            amount=q.size,
            post_only=True,
            reduce_only=False,
        )
        mo = ManagedOrder(order=order, side=side, level=level)
        symbol_orders = self._get_symbol_orders(symbol)
        symbol_orders[order.id] = mo

        self.logger.debug(
            "Created new MM order",
            extra={
                "symbol": symbol,
                "side": side.value,
                "level": level,
                "price": q.price,
                "size": q.size,
                "order_id": order.id,
            },
        )

    async def _cancel_managed_order(self, symbol: str, mo: ManagedOrder) -> None:
        """
        Cancella un ordine MM e rimuove dai tracking interni.
        """
        symbol_orders = self._get_symbol_orders(symbol)
        if mo.order.id not in symbol_orders:
            return

        try:
            await self.client.cancel_order(symbol, mo.order.id)
        except Exception as e:
            self.logger.warning(
                "Failed to cancel order",
                extra={"symbol": symbol, "order_id": mo.order.id, "error": str(e)},
            )
            return

        symbol_orders.pop(mo.order.id, None)
        self.logger.debug(
            "Cancelled MM order",
            extra={"symbol": symbol, "order_id": mo.order.id, "side": mo.side.value, "level": mo.level},
        )

    async def _maybe_reprice_or_resize(
        self,
        symbol: str,
        mo: ManagedOrder,
        q: QuoteLevel,
    ) -> None:
        """
        Controlla se l'ordine esistente è abbastanza vicino alla quote target.
        Se differenza di prezzo/size troppo grande → cancella + ricrea.
        Altrimenti NON fare nulla (evita churn).
        """
        old = mo.order
        price_diff = abs(old.price - q.price)
        # tick size dalla exchange
        tick = self.client.get_tick_size(symbol)
        price_diff_ticks = price_diff / tick if tick > 0 else 0.0

        size_diff = abs(old.amount - q.size)
        size_diff_rel = size_diff / old.amount if old.amount > 0 else 1.0

        now = time.time()
        ms_since_update = (now - mo.last_update_ts) * 1000

        need_reprice = price_diff_ticks >= self.reprice_threshold_ticks
        need_resize = size_diff_rel >= self.size_change_threshold

        # throttle: non toccare troppo spesso lo stesso ordine
        if not (need_reprice or need_resize):
            return
        if ms_since_update < self.min_reprice_interval_ms:
            return

        # cancella + ricrea
        await self._cancel_managed_order(symbol, mo)
        await self._create_new_order(symbol, mo.side, mo.level, q)
