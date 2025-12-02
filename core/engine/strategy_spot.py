# core/engine/strategy_spot.py
import asyncio
import time
from ..models import MarketState, InventoryState
from ..exchange.base import ExchangeClient
from .risk_spot import SpotRiskEngine
from .quoting_spot import SpotQuoter
from ..infra.metrics import MetricsCollector


class SymbolStrategySpot:
    def __init__(
        self,
        symbol: str,
        sym_cfg: dict,
        client: ExchangeClient,
        risk_engine: SpotRiskEngine,
        quoter: SpotQuoter,
        metrics: MetricsCollector,
        logger,
    ):
        self.symbol = symbol
        self.sym_cfg = sym_cfg
        self.client = client
        self.risk_engine = risk_engine
        self.quoter = quoter
        self.metrics = metrics
        self.logger = logger
        self.refresh_ms = sym_cfg.get("refresh_interval_ms", 200)
        self._running = True

    async def run(self) -> None:
        self.logger.info("Starting SymbolStrategySpot")
        while self._running:
            start = time.time()
            try:
                mkt: MarketState = await self.client.get_market_state(self.symbol)
                inv: InventoryState = await self.client.get_inventory(self.symbol)

                risk_view = self.risk_engine.assess(self.symbol, mkt, inv, self.sym_cfg)
                quotes = self.quoter.compute_quotes(mkt, inv, risk_view)

                # TODO: OrderManager per riconciliare gli ordini
                # per ora, stub: niente ordini reali

                self.metrics.record_inventory(self.symbol, inv)
                self.metrics.record_quote(self.symbol, mkt, quotes)

            except Exception as e:
                self.logger.exception(f"Error in spot strategy loop for {self.symbol}: {e}")

            elapsed_ms = (time.time() - start) * 1000
            sleep_ms = max(0, self.refresh_ms - elapsed_ms)
            await asyncio.sleep(sleep_ms / 1000.0)
