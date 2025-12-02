# core/models.py
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import time


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class ProductType(str, Enum):
    SPOT = "spot"
    PERP = "perp"


@dataclass
class ProductModel:
    product_type: ProductType
    supports_leverage: bool
    has_funding: bool
    can_short: bool


@dataclass
class MarketState:
    symbol: str
    best_bid: float
    best_ask: float
    last_price: float
    ts_exchange: float  # timestamp exchange
    ts_local: float     # timestamp locale


@dataclass
class InventoryState:
    symbol: str
    base_position: float   # per spot: coin, per perp: contracts * contract_size
    quote_position: float  # cash balance (USDT etc.)
    notional_usd: float
    ts: float


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    price: float
    amount: float
    filled: float
    status: str          # "open", "filled", "partially_filled", "canceled"
    ts: float


@dataclass
class QuoteLevel:
    price: float
    size: float
    level: int           # 0 = più vicino al mid


@dataclass
class SideQuotes:
    symbol: str
    bids: list[QuoteLevel]
    asks: list[QuoteLevel]
    ts: float = time.time()
