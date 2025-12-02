# core/exchange/whitebit_spot.py
from __future__ import annotations

import asyncio
import json
import time
import base64
import hmac
import hashlib
from collections import defaultdict
from typing import Any, Dict, Optional

import websockets
import aiohttp

from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType
from .base import ExchangeClient
from ..engine.orderbook import LocalOrderBook


class WhitebitSpotClient(ExchangeClient):
    """
    Client WhiteBIT Spot v4:

    - WS pubblico per orderbook (depth_subscribe / depth_update)
    - REST privato per:
      - balance: /api/v4/trade-account/balance
      - open orders: /api/v4/orders
      - new order: /api/v4/order/new
      - cancel: /api/v4/order/cancel
      - cancel all: /api/v4/order/cancel/all
    """

    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_spot"

        # URL di base
        self.ws_url = cfg["ws_base_url"].rstrip("/")   # es: wss://api.whitebit.com/ws
        self.rest_base_url = cfg.get("rest_base_url", "https://api.whitebit.com")

        # Keys
        self.api_key = cfg.get("api_key", "")
        self.api_secret = cfg.get("api_secret", "")

        # WS
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_reader_task: Optional[asyncio.Task] = None
        self._running = False

        # HTTP
        self._http: Optional[aiohttp.ClientSession] = None
        self._http_timeout = aiohttp.ClientTimeout(total=5.0)
        self._nonce_lock = asyncio.Lock()
        self._last_nonce = 0

        # Orderbook locale per simbolo
        self._books: Dict[str, LocalOrderBook] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = defaultdict(asyncio.Event)

        # Mapping simboli
        # "MEME/USDT" -> "MEME_USDT"
        # (per WS e REST è uguale)
        # se ti serve qualcosa di più complesso lo centralizziamo poi
        self._symbol_to_market: Dict[str, str] = {}
        self._market_to_symbol: Dict[str, str] = {}

        # JSON-RPC id + subscriptions
        self._req_id = 0
        self._req_lock = asyncio.Lock()
        self._subscriptions: set[str] = set()

        if not self.api_key or not self.api_secret:
            self.logger.warning("WhiteBIT Spot client initialized WITHOUT api_key/api_secret")

    # ------------------------------------------------------------------ #
    # LIFECYCLE
    # ------------------------------------------------------------------ #

    async def connect_ws(self) -> None:
        if self._ws is not None:
            return

        self.logger.info("Connecting WhiteBIT Spot WS...", extra={"ws_url": self.ws_url})
        self._running = True
        self._ws = await websockets.connect(
            self.ws_url,
            ping_interval=None,   # usiamo ping JSON-RPC, non ping TCP automatico
            close_timeout=5,
        )
        self._ws_reader_task = asyncio.create_task(self._ws_reader_loop(), name="wb_spot_ws_reader")
        # ping periodico come da doc (ping JSON-RPC ogni ~50s)
        asyncio.create_task(self._ws_ping_loop(), name="wb_spot_ws_ping")

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=self._http_timeout)
        return self._http

    async def close(self) -> None:
        self._running = False

        if self._ws_reader_task is not None:
            self._ws_reader_task.cancel()
            try:
                await self._ws_reader_task
            except asyncio.CancelledError:
                pass

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        if self._http is not None:
            await self._http.close()
            self._http = None

        self.logger.info("Closed Whitebit Spot client")

    # ------------------------------------------------------------------ #
    # HELPERS: symbol/market, req id, nonce
    # ------------------------------------------------------------------ #

    def _to_market(self, symbol: str) -> str:
        if symbol in self._symbol_to_market:
            return self._symbol_to_market[symbol]
        base, quote = symbol.split("/")
        market = f"{base}_{quote}"
        self._symbol_to_market[symbol] = market
        self._market_to_symbol[market] = symbol
        return market

    def _from_market(self, market: str) -> str:
        if market in self._market_to_symbol:
            return self._market_to_symbol[market]
        base, quote = market.split("_")
        symbol = f"{base}/{quote}"
        self._market_to_symbol[market] = symbol
        self._symbol_to_market[symbol] = market
        return symbol

    async def _next_id(self) -> int:
        async with self._req_lock:
            self._req_id += 1
            return self._req_id

    async def _next_nonce(self) -> int:
        ts_ms = int(time.time() * 1000)
        async with self._nonce_lock:
            if ts_ms <= self._last_nonce:
                ts_ms = self._last_nonce + 1
            self._last_nonce = ts_ms
            return ts_ms

    # ------------------------------------------------------------------ #
    # SIGNED HTTP REQUEST (PRIVATE API)
    # ------------------------------------------------------------------ #

    async def _signed_request(self, endpoint: str, body: dict) -> Any:
        """
        endpoint: es "/api/v4/trade-account/balance"
        body: dict con i parametri specifici dell'endpoint (senza request/nonce).
        """
        if not self.api_key or not self.api_secret:
            raise RuntimeError("WhiteBIT private API called without api_key/api_secret configured")

        session = await self._ensure_http()

        nonce = await self._next_nonce()
        payload_obj = dict(body)
        payload_obj.setdefault("request", endpoint)
        payload_obj.setdefault("nonce", nonce)

        raw = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
        payload_b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha512,
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-TXC-APIKEY": self.api_key,
            "X-TXC-PAYLOAD": payload_b64,
            "X-TXC-SIGNATURE": signature,
        }

        url = f"{self.rest_base_url}{endpoint}"

        try:
            async with session.post(url, data=raw, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    self.logger.error(
                        "WhiteBIT HTTP error",
                        extra={"url": url, "status": resp.status, "body": text},
                    )
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    self.logger.error(
                        "WhiteBIT HTTP invalid JSON",
                        extra={"url": url, "body": text},
                    )
                    raise
        except asyncio.TimeoutError:
            self.logger.error("WhiteBIT HTTP timeout", extra={"url": url})
            raise
        except Exception as e:
            self.logger.exception("WhiteBIT HTTP request failed", extra={"url": url})
            raise

    # ------------------------------------------------------------------ #
    # WS: subscribe + reader + ping
    # ------------------------------------------------------------------ #

    async def subscribe_orderbook(self, symbol: str, limit: int = 50) -> None:
        market = self._to_market(symbol)
        if market in self._subscriptions:
            return
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected; call connect_ws() first")

        # inizializza orderbook locale
        if symbol not in self._books:
            self._books[symbol] = LocalOrderBook(symbol=symbol, limit=limit)

        req = {
            "id": await self._next_id(),
            "method": "depth_subscribe",
            # params: [market, limit, interval, multiple_subscription_flag]
            "params": [market, limit, "0", True],
        }
        await self._ws.send(json.dumps(req))
        self.logger.info("Subscribed orderbook", extra={"symbol": symbol, "market": market, "limit": limit})
        self._subscriptions.add(market)

    async def _ws_reader_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self.logger.warning("Failed to decode WS message", extra={"payload": raw})
                    continue
                await self._handle_ws_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.exception(f"Error in WhiteBIT WS reader: {e}")
        finally:
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

    async def _ws_ping_loop(self) -> None:
        # ping JSON-RPC ogni ~50s (server chiude dopo ~60s di inattività)
        try:
            while self._running:
                await asyncio.sleep(50)
                if self._ws is None:
                    continue
                msg = {
                    "id": await self._next_id(),
                    "method": "ping",
                    "params": [],
                }
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception as e:
                    self.logger.warning("WS ping failed", extra={"error": str(e)})
        except asyncio.CancelledError:
            return

    async def _handle_ws_message(self, msg: dict[str, Any]) -> None:
        if "error" in msg and msg["error"]:
            self.logger.error("WS error", extra={"error": msg["error"]})
            return

        method = msg.get("method")
        if method == "depth_update":
            params = msg.get("params", [])
            await self._handle_depth_update(params)

    async def _handle_depth_update(self, params: list[Any]) -> None:
        """
        WhiteBIT: depth_update.params = [payload, market]

        payload = {
          "timestamp": ...,
          "update_id": ...,
          "past_update_id": ... (solo incrementali),
          "asks": [...],
          "bids": [...]
        }
        """
        if len(params) != 2:
            self.logger.warning("Malformed depth_update", extra={"params": params})
            return

        payload = params[0]
        market = params[1]

        symbol = self._from_market(market)
        book = self._books.get(symbol)
        if book is None:
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
            # snapshot completo
            book.apply_snapshot(asks=asks, bids=bids, update_id=update_id)
            self._orderbook_events[symbol].set()
        else:
            ok = book.apply_increment(
                asks=asks,
                bids=bids,
                update_id=update_id,
                past_update_id=past_update_id,
            )
            if not ok:
                self.logger.warning(
                    "Orderbook out of sync",
                    extra={"symbol": symbol, "book_last": book.last_update_id, "past_update_id": past_update_id},
                )
                # TODO: resync (depth_request o nuova subscribe)

    # ------------------------------------------------------------------ #
    # PUBLIC API USATA DALLA STRATEGIA
    # ------------------------------------------------------------------ #

    async def get_market_state(self, symbol: str) -> MarketState:
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
            last_price=mid,
            ts_exchange=0.0,          # se ti serve, puoi salvare payload["timestamp"]
            ts_local=time.time(),
        )

    async def get_inventory(self, symbol: str) -> InventoryState:
        """
        /api/v4/trade-account/balance
        Ritorna balances per asset; qui ricaviamo base/quote del simbolo.
        """
        base, quote = symbol.split("/")

        resp = await self._signed_request(
            "/api/v4/trade-account/balance",
            body={},  # tutte le balances
        )

        base_info = resp.get(base, {"available": "0", "freeze": "0"})
        quote_info = resp.get(quote, {"available": "0", "freeze": "0"})

        base_free = float(base_info.get("available", "0"))
        base_lock = float(base_info.get("freeze", "0"))
        quote_free = float(quote_info.get("available", "0"))
        quote_lock = float(quote_info.get("freeze", "0"))

        base_total = base_free + base_lock
        quote_total = quote_free + quote_lock

        # stima molto semplice notional_usd
        notional_usd = 0.0
        try:
            mkt = await self.get_market_state(symbol)
            mid = (mkt.best_bid + mkt.best_ask) / 2
            notional_usd += base_total * mid
            if quote in ("USDT", "USDC", "USD", "EURT"):
                notional_usd += quote_total
        except Exception:
            pass

        return InventoryState(
            symbol=symbol,
            base_position=base_total,
            quote_position=quote_total,
            notional_usd=notional_usd,
            ts=time.time(),
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """
        /api/v4/orders — unexecuted (active) orders.
        """
        market = self._to_market(symbol)
        body = {
            "market": market,
            "limit": 100,
            "offset": 0,
        }

        self.logger.debug("Query open orders", extra={"symbol": symbol, "market": market})
        resp = await self._signed_request("/api/v4/orders", body=body)

        orders: list[Order] = []
        for item in resp:
            try:
                if item.get("market") != market:
                    continue

                order_id = str(item["orderId"])
                side_str = item["side"]
                side = Side.BUY if side_str.lower() == "buy" else Side.SELL

                price = float(item["price"])
                amount_total = float(item["amount"])
                filled = float(item.get("dealStock", "0"))
                status = item.get("status", "NEW").lower()
                ts = float(item.get("timestamp", time.time()))

                orders.append(
                    Order(
                        id=order_id,
                        symbol=symbol,
                        side=side,
                        price=price,
                        amount=amount_total,
                        filled=filled,
                        status=status,
                        ts=ts,
                    )
                )
            except Exception as e:
                self.logger.warning(
                    "Failed to parse open order",
                    extra={"raw": item, "error": str(e)},
                )
                continue

        return orders

    async def get_order_history(
        self,
        symbol: str,
        status: str | None = "FILLED",
        limit: int = 100,
        offset: int = 0,
        client_order_id: str | None = None,
    ) -> list[Order]:
        """
        Storico ordini eseguiti / chiusi per un simbolo via:
        /api/v4/trade-account/order/history

        - status: se valorizzato (es. "FILLED", "CANCELED"), filtra lato client.
        - limit/offset: paginazione base.
        - client_order_id: se passato, filtra lato server per quello specifico ID.
        """
        market = self._to_market(symbol)

        body: dict[str, Any] = {
            "market": market,
            "limit": limit,
            "offset": offset,
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id

        self.logger.debug(
            "Query order history",
            extra={
                "symbol": symbol,
                "market": market,
                "status_filter": status,
                "limit": limit,
                "offset": offset,
                "clientOrderId": client_order_id,
            },
        )

        resp = await self._signed_request("/api/v4/trade-account/order/history", body=body)

        records = resp.get("records", [])
        orders: list[Order] = []

        for item in records:
            try:
                if item.get("market") != market:
                    continue

                item_status = item.get("status", "").upper()
                if status is not None and item_status != status.upper():
                    # filtriamo lato client per status
                    continue

                order_id = str(item["orderId"])
                side_str = item["side"]
                side = Side.BUY if side_str.lower() == "buy" else Side.SELL

                price = float(item["price"])
                amount_total = float(item["amount"])
                filled = float(item.get("dealStock", "0"))
                ts = float(item.get("timestamp", time.time()))

                orders.append(
                    Order(
                        id=order_id,
                        symbol=symbol,
                        side=side,
                        price=price,
                        amount=amount_total,
                        filled=filled,
                        status=item_status.lower(),
                        ts=ts,
                    )
                )
            except Exception as e:
                self.logger.warning(
                    "Failed to parse order history record",
                    extra={"raw": item, "error": str(e)},
                )
                continue

        return orders

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        price: float,
        amount: float,
        post_only: bool = True,
        reduce_only: bool = False,  # non rilevante per spot
        client_order_id: str | None = None,
    ) -> Order:
        """
        /api/v4/order/new — crea ordine limit spot.
        """
        market = self._to_market(symbol)
        side_str = "buy" if side == Side.BUY else "sell"

        body: dict[str, Any] = {
            "market": market,
            "side": side_str,
            "amount": str(amount),
            "price": str(price),
            "postOnly": bool(post_only),
            "ioc": False,
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id

        self.logger.debug(
            "Placing WhiteBIT limit order",
            extra={"market": market, "side": side_str, "price": price, "amount": amount},
        )

        resp = await self._signed_request("/api/v4/order/new", body=body)

        order_id = str(resp["orderId"])
        deal_stock = float(resp.get("dealStock", "0"))
        amount_total = float(resp.get("amount", "0"))
        status = resp.get("status", "NEW").lower()
        ts = float(resp.get("timestamp", time.time()))

        return Order(
            id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            amount=amount_total,
            filled=deal_stock,
            status=status,
            ts=ts,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        """
        /api/v4/order/cancel — cancella un ordine.
        """
        market = self._to_market(symbol)
        body = {
            "market": market,
            "orderId": int(order_id),
        }
        self.logger.debug(
            "Cancel order",
            extra={"symbol": symbol, "market": market, "order_id": order_id},
        )
        await self._signed_request("/api/v4/order/cancel", body=body)

    async def cancel_all_symbol(self, symbol: str) -> None:
        """
        /api/v4/order/cancel/all — cancella tutti gli ordini Spot per quel market.
        """
        market = self._to_market(symbol)
        body = {
            "market": market,
            "type": ["Spot"],
        }
        self.logger.info(
            "Cancel all spot orders on symbol",
            extra={"symbol": symbol, "market": market},
        )
        await self._signed_request("/api/v4/order/cancel/all", body=body)

    # ------------------------------------------------------------------ #
    # PRODUCT MODEL
    # ------------------------------------------------------------------ #

    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.SPOT,
            supports_leverage=False,
            has_funding=False,
            can_short=False,
        )

    def get_tick_size(self, symbol: str) -> float:
        # TODO: in futuro, leggere da public markets
        return 0.000001

    def get_min_order_size(self, symbol: str) -> float:
        # TODO: in futuro, leggere da public markets
        return 1.0
