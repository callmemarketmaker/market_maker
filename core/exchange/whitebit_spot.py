# core/exchange/whitebit_spot.py
import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Dict, Iterable

import websockets

from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType
from .base import ExchangeClient


class WhitebitSpotClient:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_spot"
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._running = False
        self._orderbooks: Dict[str, MarketState] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._req_id = 0
        self._subscriptions: set[str] = set()
        self.ws_url = f"{self.cfg['ws_base_url'].rstrip('/')}/ws"

    async def connect_ws(self) -> None:
        if self._ws is not None:
            return
        self.logger.info("Connecting Whitebit Spot WS...", extra={"ws_url": self.ws_url})
        self._running = True
        self._ws = await websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="whitebit_spot_ws")

    async def close(self) -> None:
        self._running = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self.logger.info("Closing Whitebit Spot client...")

    async def subscribe_orderbook(self, symbol: str) -> None:
        ws_symbol = self._to_ws_symbol(symbol)
        if ws_symbol in self._subscriptions:
            return
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected; call connect_ws() first")

        self._req_id += 1
        msg = {
            "id": self._req_id,
            "method": "depth_subscribe",
            # params: [market, limit, interval]
            "params": [ws_symbol, 50, "0.00000001"],
        }
        await self._ws.send(json.dumps(msg))
        self.logger.info("Subscribed orderbook", extra={"symbol": symbol, "ws_symbol": ws_symbol})
        self._subscriptions.add(ws_symbol)

    async def get_market_state(self, symbol: str) -> MarketState:
        if symbol not in self._orderbooks:
            await asyncio.wait_for(self._orderbook_events[symbol].wait(), timeout=10)
        state = self._orderbooks.get(symbol)
        if state is None:
            raise RuntimeError(f"No market state cached for {symbol}")
        return state

    async def get_inventory(self, symbol: str) -> InventoryState:
        return InventoryState(
            symbol=symbol,
            base_position=0.0,
            quote_position=0.0,
            notional_usd=0.0,
            ts=time.time(),
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
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
        self.logger.debug(
            "Placing order", extra={"side": side, "amount": amount, "symbol": symbol, "price": price}
        )
        return Order(
            id="TEMP_ID",
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            filled=0.0,
            status="open",
            ts=time.time(),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.logger.debug("Cancel order", extra={"order_id": order_id, "symbol": symbol})

    async def cancel_all_symbol(self, symbol: str) -> None:
        self.logger.info("Cancel all orders on symbol", extra={"symbol": symbol})

    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.SPOT,
            supports_leverage=False,
            has_funding=False,
            can_short=False,
        )

    def get_tick_size(self, symbol: str) -> float:
        return 0.000001

    def get_min_order_size(self, symbol: str) -> float:
        return 1.0

    async def _reader_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self.logger.warning("Failed to decode WS message", extra={"payload": raw})
                    continue
                await self._handle_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.exception(f"Error in WhiteBIT WS reader: {e}")
        finally:
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if "error" in msg and msg["error"]:
            self.logger.error("WS error", extra={"error": msg["error"]})
            return

        method = msg.get("method")
        if method == "depth_update":
            await self._handle_depth_update(msg.get("params", []))

    async def _handle_depth_update(self, params: list[Any]) -> None:
        if len(params) < 3:
            self.logger.warning("Malformed depth_update", extra={"params": params})
            return

        book_data = params[1]
        ws_symbol = params[2]
        ts_exchange = float(params[3]) if len(params) > 3 else time.time()

        bids = self._parse_side(book_data.get("bids", []))
        asks = self._parse_side(book_data.get("asks", []))
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        if best_bid is None or best_ask is None:
            self.logger.debug("Missing bid/ask in update", extra={"ws_symbol": ws_symbol})
            return

        symbol = self._from_ws_symbol(ws_symbol)
        previous = self._orderbooks.get(symbol)
        mid = (best_bid + best_ask) / 2
        last_price = previous.last_price if previous else mid

        self._orderbooks[symbol] = MarketState(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            last_price=last_price,
            ts_exchange=ts_exchange,
            ts_local=time.time(),
        )
        self._orderbook_events[symbol].set()

    def _parse_side(self, levels: Iterable[list[str]]) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        for level in levels:
            if len(level) < 2:
                continue
            try:
                price = float(level[0])
                size = float(level[1])
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            parsed.append((price, size))
        return parsed

    def _to_ws_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "_")

    def _from_ws_symbol(self, symbol: str) -> str:
        return symbol.replace("_", "/")
