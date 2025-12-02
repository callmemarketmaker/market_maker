# core/exchange/whitebit_spot.py
import asyncio
import json
import time
import base64
import hmac
import hashlib
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional

import websockets
import aiohttp

from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType
from .base import ExchangeClient


class WhitebitSpotClient(ExchangeClient):
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_spot"

        # WS
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False

        # HTTP
        self._http: Optional[aiohttp.ClientSession] = None
        self._http_timeout = aiohttp.ClientTimeout(total=5.0)
        self._nonce_lock = asyncio.Lock()
        self._last_nonce = 0

        # Market state cache
        self._orderbooks: Dict[str, MarketState] = {}
        self._orderbook_events: Dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._req_id = 0
        self._subscriptions: set[str] = set()

        # URLs
        # es: ws_base_url = "wss://whitebit.com/ws"
        self.ws_url = f"{self.cfg['ws_base_url'].rstrip('/')}"
        # es: rest_base_url = "https://whitebit.com"
        self.rest_base_url = self.cfg.get("rest_base_url", "https://whitebit.com")

        # API keys
        self.api_key = self.cfg.get("api_key", "")
        self.api_secret = self.cfg.get("api_secret", "")

        if not self.api_key or not self.api_secret:
            self.logger.warning("WhiteBIT Spot client initialized WITHOUT api_key/api_secret")

    # ----------------------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------------------
    async def connect_ws(self) -> None:
        if self._ws is not None:
            return

        self.logger.info("Connecting WhiteBIT Spot WS...", extra={"ws_url": self.ws_url})
        self._running = True
        self._ws = await websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="whitebit_spot_ws")

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=self._http_timeout)
        return self._http

    async def close(self) -> None:
        self._running = False

        # stop WS reader
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        # close HTTP
        if self._http is not None:
            await self._http.close()
            self._http = None

        self.logger.info("Closing Whitebit Spot client...")

    # ----------------------------------------------------------------------
    # PRIVATE HTTP SIGNED REQUEST
    # ----------------------------------------------------------------------
    async def _next_nonce(self) -> int:
        # nonce monotono, consigliato: timestamp in ms
        # + guard rail per concorrenza
        ts_ms = int(time.time() * 1000)
        async with self._nonce_lock:
            if ts_ms <= self._last_nonce:
                ts_ms = self._last_nonce + 1
            self._last_nonce = ts_ms
            return ts_ms

    async def _signed_request(self, endpoint: str, body: dict) -> Any:
        """
        endpoint: path completo, es. "/api/v4/trade-account/balance"
        body: params specifici dell'endpoint (senza request/nonce)
        """
        if not self.api_key or not self.api_secret:
            raise RuntimeError("WhiteBIT private API called without api_key/api_secret configured")

        session = await self._ensure_http()

        nonce = await self._next_nonce()
        payload_obj = dict(body)
        payload_obj.setdefault("request", endpoint)
        payload_obj.setdefault("nonce", nonce)
        # se vuoi usare la finestra temporale:
        # payload_obj.setdefault("nonceWindow", True)

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

    # ----------------------------------------------------------------------
    # PUBLIC DATA / ORDERBOOK
    # ----------------------------------------------------------------------
    async def subscribe_orderbook(self, symbol: str, limit: int = 50) -> None:
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
            "params": [ws_symbol, limit, "0.00000001"],
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

    # ----------------------------------------------------------------------
    # PRIVATE METHODS MANDATI DAL BOT (INVENTORY / ORDERS)
    # ----------------------------------------------------------------------
    async def get_inventory(self, symbol: str) -> InventoryState:
        """
        Usa /api/v4/trade-account/balance per ottenere i bilanci spot,
        poi costruisce l'inventory per base/quote del simbolo.
        """
        base, quote = symbol.split("/")

        resp = await self._signed_request(
            "/api/v4/trade-account/balance",
            body={},  # nessun ticker -> tutte le balances
        )

        base_info = resp.get(base, {"available": "0", "freeze": "0"})
        quote_info = resp.get(quote, {"available": "0", "freeze": "0"})

        base_available = float(base_info.get("available", "0"))
        base_freeze = float(base_info.get("freeze", "0"))
        quote_available = float(quote_info.get("available", "0"))
        quote_freeze = float(quote_info.get("freeze", "0"))

        base_total = base_available + base_freeze
        quote_total = quote_available + quote_freeze

        # stima notional_usd molto semplice: se quote è USDT/USDC ecc, la prendiamo 1:1
        notional_usd = 0.0
        try:
            ms = self._orderbooks.get(symbol)
            if ms is not None:
                mid = (ms.best_bid + ms.best_ask) / 2
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
        # TODO: mappare /api/v4/trade-account/order/history or unexecuted orders
        return []

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        price: float,
        amount: float,
        post_only: bool = True,
        reduce_only: bool = False,  # non usato per spot
        client_order_id: str | None = None,
    ) -> Order:
        """
        /api/v4/order/new — crea un ordine limit spot
        """
        market = self._to_rest_market(symbol)
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
        /api/v4/order/cancel — cancella un singolo ordine spot
        """
        market = self._to_rest_market(symbol)
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
        /api/v4/order/cancel/all — cancella tutti gli ordini per un market.
        Qui limitiamo type=["Spot"] per non toccare margin/futures.
        """
        market = self._to_rest_market(symbol)
        body = {
            "market": market,
            "type": ["Spot"],
        }
        self.logger.info(
            "Cancel all spot orders on symbol",
            extra={"symbol": symbol, "market": market},
        )
        await self._signed_request("/api/v4/order/cancel/all", body=body)

    # ----------------------------------------------------------------------
    # STATIC PRODUCT INFO
    # ----------------------------------------------------------------------
    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.SPOT,
            supports_leverage=False,
            has_funding=False,
            can_short=False,
        )

    def get_tick_size(self, symbol: str) -> float:
        # TODO: prendere dai public /api/v4/public/orderbook/{market} o assets
        return 0.000001

    def get_min_order_size(self, symbol: str) -> float:
        # TODO: prendere dai public assets/markets
        return 1.0

    # ----------------------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------------------
    def _to_ws_symbol(self, symbol: str) -> str:
        # es: BTC/USDT -> BTC_USDT
        return symbol.replace("/", "_")

    def _from_ws_symbol(self, symbol: str) -> str:
        # es: BTC_USDT -> BTC/USDT
        return symbol.replace("_", "/")

    def _to_rest_market(self, symbol: str) -> str:
        # stesse regole del WS per il market
        return symbol.replace("/", "_")
