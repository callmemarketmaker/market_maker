# core/exchange/whitebit_perp.py
from ..models import MarketState, InventoryState, Order, Side, ProductModel, ProductType


class WhitebitPerpClient:
    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.logger = logger
        self.name = "whitebit_perp"

    async def connect_ws(self) -> None:
        self.logger.info("Perp WS not implemented yet")

    async def close(self) -> None:
        self.logger.info("Closing Whitebit Perp client")

    async def subscribe_orderbook(self, symbol: str) -> None:
        raise NotImplementedError("Perpetuals orderbook not implemented")

    async def get_market_state(self, symbol: str) -> MarketState:
        raise NotImplementedError("Perpetuals market state not implemented")

    async def get_inventory(self, symbol: str) -> InventoryState:
        raise NotImplementedError("Perpetuals inventory not implemented")

    async def get_open_orders(self, symbol: str) -> list[Order]:
        raise NotImplementedError("Perpetuals orders not implemented")

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
        raise NotImplementedError("Perpetuals trading not implemented")

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        raise NotImplementedError("Perpetuals cancel not implemented")

    async def cancel_all_symbol(self, symbol: str) -> None:
        raise NotImplementedError("Perpetuals mass cancel not implemented")

    def get_product_model(self, symbol: str) -> ProductModel:
        return ProductModel(
            product_type=ProductType.PERP,
            supports_leverage=True,
            has_funding=True,
            can_short=True,
        )

    def get_tick_size(self, symbol: str) -> float:
        return 0.0001

    def get_min_order_size(self, symbol: str) -> float:
        return 1.0
