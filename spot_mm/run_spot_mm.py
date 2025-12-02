# spot_mm/run_spot_mm.py
import asyncio
import logging
from pathlib import Path

from core.config_loader import load_config
from core.infra.logging_setup import setup_logging
from core.infra.metrics import MetricsCollector, start_metrics_server
from core.exchange.factory import create_exchange_client
from core.engine.risk_spot import SpotRiskEngine
from core.engine.quoting_spot import SpotQuoter
from core.engine.strategy_spot import SymbolStrategySpot


async def main(config_path: str) -> None:
    cfg = load_config(config_path)
    setup_logging(cfg["logging"])
    logger = logging.getLogger("spot_mm")

    metrics = MetricsCollector()
    if cfg["metrics"]["prometheus"]["enabled"]:
        asyncio.create_task(
            start_metrics_server(
                cfg["metrics"]["prometheus"]["listen_host"],
                cfg["metrics"]["prometheus"]["listen_port"],
            )
        )

    # crea client per ciascun venue
    clients_by_venue = {}
    for venue_name, venue_cfg in cfg["venues"].items():
        clients_by_venue[venue_name] = create_exchange_client(venue_name, venue_cfg, logger)

    # connetti WS di tutti i venue
    for client in clients_by_venue.values():
        await client.connect_ws()

    risk_engine = SpotRiskEngine(cfg["risk"], logger=logging.getLogger("SpotRisk"))
    tasks = []

    for symbol, sym_cfg in cfg["symbols"].items():
        if not sym_cfg.get("enabled", True):
            continue
        if sym_cfg["product_type"] != "spot":
            continue

        venue_name = sym_cfg["venue"]
        client = clients_by_venue[venue_name]

        await client.subscribe_orderbook(symbol)

        quoter = SpotQuoter(symbol, sym_cfg, logger=logging.getLogger(f"Quoter_{symbol}"))
        strategy = SymbolStrategySpot(
            symbol=symbol,
            sym_cfg=sym_cfg,
            client=client,
            risk_engine=risk_engine,
            quoter=quoter,
            metrics=metrics,
            logger=logging.getLogger(f"Strategy_{symbol}"),
        )
        tasks.append(asyncio.create_task(strategy.run(), name=f"spot_{symbol}"))

    logger.info(f"Started {len(tasks)} spot strategies")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown requested, cancelling tasks...")
    finally:
        for client in clients_by_venue.values():
            await client.close()


if __name__ == "__main__":
    asyncio.run(main("config/config.yaml"))
