# core/exchange/whitebit_spot.py
import asyncio
from typing import Dict
from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType
from .base import ExchangeClient


class WhitebitSpotClient:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_spot"
        self._orderbooks: Dict[str, MarketState] = {}
        # qui in futuro: session HTTP, connessione WS, ecc.

    async def connect_ws(self) -> None:
        # TODO: connettere al WS WhiteBIT, avviare task di lettura e aggiornamento orderbook
        self.logger.info("Connecting Whitebit Spot WS...")
        await asyncio.sleep(0.01)

    async def close(self) -> None:
        # TODO: chiudere connessione WS/HTTP
        self.logger.info("Closing Whitebit Spot client...")

    async def subscribe_orderbook(self, symbol: str) -> None:
        # TODO: mandare messaggio di subscribe. Per ora placeholder.
        self.logger.info(f"Subscribing orderbook for {symbol}")
        await asyncio.sleep(0.01)

    async def get_market_state(self, symbol: str) -> MarketState:
        # TODO: leggere dallo stato cache aggiornato dal WS
        state = self._orderbooks.get(symbol)
        if state is None:
            # placeholder: in prod deve essere impossibile
            raise RuntimeError(f"No market state cached for {symbol}")
        return state

    async def get_inventory(self, symbol: str) -> InventoryState:
        # TODO: chiamare REST balance / account
        # placeholder
        return InventoryState(
            symbol=symbol,
            base_position=0.0,
            quote_position=0.0,
            notional_usd=0.0,
            ts=0.0,
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        # TODO: chiamare REST "open orders"
        return []

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        price: float,
        amount: float,
        post_only: bool = True,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        # TODO: chiamata POST /order
        self.logger.debug(f"Placing order {side} {amount} {symbol} @ {price}")
        return Order(
            id="TEMP_ID",
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            filled=0.0,
            status="open",
            ts=0.0,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        # TODO: chiamata DELETE /order
        self.logger.debug(f"Cancel order {order_id} on {symbol}")

    async def cancel_all_symbol(self, symbol: str) -> None:
        # TODO: loop cancella tutti
        self.logger.info(f"Cancel all orders on {symbol}")

    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.SPOT,
            supports_leverage=False,
            has_funding=False,
            can_short=False,
        )

    def get_tick_size(self, symbol: str) -> float:
        # in prod potresti leggere da cfg["symbols"][symbol] o chiamare /markets
        return 0.000001

    def get_min_order_size(self, symbol: str) -> float:
        return 1.0
