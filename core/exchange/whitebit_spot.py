# core/exchange/whitebit_spot.py
import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional

import websockets

from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType
from .base import ExchangeClient
from ..engine.orderbook import LocalOrderBook


class WhitebitSpotClient:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_spot"

        # WS
        self.ws_url = self.cfg["ws_base_url"].rstrip("/")  # es: "wss://api.whitebit.com/ws"
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reader_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._running = False

        # orderbook per simbolo interno ("MEME/USDT")
        self._books: Dict[str, LocalOrderBook] = {}
        # eventi per segnalare che abbiamo ricevuto il primo snapshot
        self._orderbook_events: Dict[str, asyncio.Event] = defaultdict(asyncio.Event)

        # mapping simbolo interno <-> market WS ("MEME_USDT")
        self._symbol_to_ws: Dict[str, str] = {}
        self._ws_to_symbol: Dict[str, str] = {}

        # JSON-RPC id
        self._req_id = 0
        self._req_lock = asyncio.Lock()
        self._subscriptions: set[str] = set()

    # ------------------------------------------------------------------ #
    # Public API richiesto dall'interfaccia ExchangeClient
    # ------------------------------------------------------------------ #

    async def connect_ws(self) -> None:
        if self._ws is not None:
            return
        self.logger.info("Connecting Whitebit Spot WS...", extra={"ws_url": self.ws_url})
        self._running = True
        self._ws = await websockets.connect(
            self.ws_url,
            ping_interval=None,   # ping app-layer (JSON-RPC), non WS-level
            close_timeout=5,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="whitebit_spot_ws_reader")
        self._ping_task = asyncio.create_task(self._ping_loop(), name="whitebit_spot_ws_ping")

    async def close(self) -> None:
        self._running = False
        if self._ping_task is not None:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

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

    async def subscribe_orderbook(self, symbol: str, limit: int = 50) -> None:
        ws_symbol = self._to_ws_symbol(symbol)
        if ws_symbol in self._subscriptions:
            return
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected; call connect_ws() first")

        # inizializza orderbook locale
        if symbol not in self._books:
            self._books[symbol] = LocalOrderBook(symbol=symbol, limit=limit)

        req_id = await self._next_id()
        # WhiteBIT: params = [market, limit, interval, multiple_subscription_flag]
        msg = {
            "id": req_id,
            "method": "depth_subscribe",
            "params": [ws_symbol, limit, "0", True],  # "0" = no price interval, True = multiple subscriptions
        }
        await self._ws.send(json.dumps(msg))
        self.logger.info("Subscribed orderbook", extra={"symbol": symbol, "ws_symbol": ws_symbol, "limit": limit})
        self._subscriptions.add(ws_symbol)

    async def get_market_state(self, symbol: str) -> MarketState:
        # aspetta primo snapshot se non presente
        if symbol not in self._books or self._books[symbol].best_bid() is None:
            await asyncio.wait_for(self._orderbook_events[symbol].wait(), timeout=10)

        book = self._books.get(symbol)
        if book is None:
            raise RuntimeError(f"No orderbook for {symbol}")

        bb = book.best_bid()
        ba = book.best_ask()
        if not bb or not ba:
            raise RuntimeError(f"Orderbook for {symbol} not initialized")

        best_bid, _ = bb
        best_ask, _ = ba
        mid = (best_bid + best_ask) / 2

        return MarketState(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            last_price=mid,      # in futuro possiamo usare last trade reale
            ts_exchange=0.0,     # possiamo riempirlo se passiamo il timestamp del payload
            ts_local=time.time(),
        )

    async def get_inventory(self, symbol: str) -> InventoryState:
        # TODO: chiamare REST /balance
        return InventoryState(
            symbol=symbol,
            base_position=0.0,
            quote_position=0.0,
            notional_usd=0.0,
            ts=time.time(),
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        # TODO: REST private per open orders
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
        # TODO: REST private per creare ordine
        self.logger.debug(
            "Placing order",
            extra={"side": side, "amount": amount, "symbol": symbol, "price": price},
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
        # TODO: REST private per cancellare ordine
        self.logger.debug("Cancel order", extra={"order_id": order_id, "symbol": symbol})

    async def cancel_all_symbol(self, symbol: str) -> None:
        # TODO: REST private per cancellare tutti gli ordini del simbolo
        self.logger.info("Cancel all orders on symbol", extra={"symbol": symbol})

    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.SPOT,
            supports_leverage=False,
            has_funding=False,
            can_short=False,
        )

    def get_tick_size(self, symbol: str) -> float:
        return 0.000001  # in prod: da config o /markets

    def get_min_order_size(self, symbol: str) -> float:
        return 1.0

    # ------------------------------------------------------------------ #
    # WS internals
    # ------------------------------------------------------------------ #

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

    async def _ping_loop(self) -> None:
        """
        WhiteBIT chiude dopo ~60s di inattività: mandiamo ping JSON-RPC ogni ~50s.
        """
        try:
            while self._running:
                await asyncio.sleep(50)
                if self._ws is None:
                    continue
                try:
                    req_id = await self._next_id()
                    payload = {"id": req_id, "method": "ping", "params": []}
                    await self._ws.send(json.dumps(payload))
                except Exception as e:
                    self.logger.warning("Ping failed", extra={"error": str(e)})
        except asyncio.CancelledError:
            return

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if "error" in msg and msg["error"]:
            self.logger.error("WS error", extra={"error": msg["error"]})
            return

        method = msg.get("method")
        if method == "depth_update":
            params = msg.get("params", [])
            await self._handle_depth_update(params)

    async def _handle_depth_update(self, params: list[Any]) -> None:
        """
        WhiteBIT: params = [payload, market]
        payload = {
          "timestamp": ...,
          "update_id": ...,
          "past_update_id": ... (solo negli incrementali),
          "asks": [...],
          "bids": [...]
        }
        """
        if len(params) != 2:
            self.logger.warning("Malformed depth_update", extra={"params": params})
            return

        payload = params[0]
        ws_symbol = params[1]

        symbol = self._from_ws_symbol(ws_symbol)
        book = self._books.get(symbol)
        if book is None:
            # se non c'è, creiamo con un limite default
            book = LocalOrderBook(symbol=symbol, limit=50)
            self._books[symbol] = book

        update_id = payload.get("update_id")
        past_update_id = payload.get("past_update_id")
        asks = payload.get("asks", [])
        bids = payload.get("bids", [])

        if update_id is None:
            self.logger.warning("depth_update missing update_id", extra={"payload": payload})
            return

        if past_update_id is None:
            # primo messaggio: snapshot completo
            book.apply_snapshot(asks=asks, bids=bids, update_id=update_id)
            self._orderbook_events[symbol].set()
        else:
            # incrementale
            ok = book.apply_increment(
                asks=asks,
                bids=bids,
                update_id=update_id,
                past_update_id=past_update_id,
            )
            if not ok:
                self.logger.warning(
                    "Depth out of sync",
                    extra={
                        "symbol": symbol,
                        "book_last": book.last_update_id,
                        "past_update_id": past_update_id,
                    },
                )
                # TODO: resync: depth_request o resubscribe

    # ------------------------------------------------------------------ #
    # Helpers vari
    # ------------------------------------------------------------------ #

    async def _next_id(self) -> int:
        async with self._req_lock:
            self._req_id += 1
            return self._req_id

    def _to_ws_symbol(self, symbol: str) -> str:
        # "MEME/USDT" -> "MEME_USDT"
        if symbol in self._symbol_to_ws:
            return self._symbol_to_ws[symbol]
        ws_symbol = symbol.replace("/", "_")
        self._symbol_to_ws[symbol] = ws_symbol
        self._ws_to_symbol[ws_symbol] = symbol
        return ws_symbol

    def _from_ws_symbol(self, ws_symbol: str) -> str:
        if ws_symbol in self._ws_to_symbol:
            return self._ws_to_symbol[ws_symbol]
        symbol = ws_symbol.replace("_", "/")
        self._ws_to_symbol[ws_symbol] = symbol
        self._symbol_to_ws[symbol] = ws_symbol
        return symbol
