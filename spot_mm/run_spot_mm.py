# spot_mm/run_spot_mm.py
import asyncio
import logging
import argparse
from pathlib import Path

from core.config_loader import load_config
from core.infra.logging_setup import setup_logging
from core.infra.metrics import MetricsCollector, start_metrics_server
from core.exchange.factory import create_exchange_client
from core.engine.risk_spot import SpotRiskEngine
from core.engine.quoting_spot import SpotQuoter
from core.engine.order_manager import OrderManager
from core.engine.strategy_spot import SymbolStrategySpot


async def run_spot_mm(config_path: str) -> None:
    # ------------------------------------------------------------------
    # Carica config e logging
    # ------------------------------------------------------------------
    cfg = load_config(config_path)
    setup_logging(cfg.get("logging", {}))
    logger = logging.getLogger("spot_mm")

    logger.info("Starting Spot MM", extra={"config_path": config_path})

    # ------------------------------------------------------------------
    # Metrics (Prometheus)
    # ------------------------------------------------------------------
    metrics = MetricsCollector()
    prom_cfg = cfg.get("metrics", {}).get("prometheus", {})
    if prom_cfg.get("enabled", False):
        host = prom_cfg.get("listen_host", "0.0.0.0")
        port = prom_cfg.get("listen_port", 9100)
        logger.info("Starting Prometheus metrics server", extra={"host": host, "port": port})
        asyncio.create_task(start_metrics_server(host, port))

    # ------------------------------------------------------------------
    # Crea ExchangeClient + OrderManager per ciascun venue
    # ------------------------------------------------------------------
    venues_cfg = cfg.get("venues", {})
    if not venues_cfg:
        raise RuntimeError("No 'venues' configured in config")

    clients_by_venue = {}
    order_managers_by_venue = {}

    for venue_name, venue_cfg in venues_cfg.items():
        v_logger = logging.getLogger(f"Venue_{venue_name}")
        client = create_exchange_client(venue_name, venue_cfg, v_logger)
        clients_by_venue[venue_name] = client

        om_logger = logging.getLogger(f"OrderManager_{venue_name}")
        order_manager = OrderManager(
            client=client,
            logger=om_logger,
            reprice_threshold_ticks=venue_cfg.get("reprice_threshold_ticks", 1),
            size_change_threshold=venue_cfg.get("size_change_threshold", 0.10),
            min_reprice_interval_ms=venue_cfg.get("min_reprice_interval_ms", 200),
        )
        order_managers_by_venue[venue_name] = order_manager

    # ------------------------------------------------------------------
    # Connetti WS pubblici per tutti i venue
    # ------------------------------------------------------------------
    logger.info("Connecting WS for all venues...")
    for venue_name, client in clients_by_venue.items():
        v_logger = logging.getLogger(f"Venue_{venue_name}")
        v_logger.info("Connecting WS")
        await client.connect_ws()

    # ------------------------------------------------------------------
    # RiskEngine spot condiviso
    # ------------------------------------------------------------------
    risk_cfg = cfg.get("risk", {})
    risk_engine_logger = logging.getLogger("SpotRiskEngine")
    risk_engine = SpotRiskEngine(risk_cfg, risk_engine_logger)

    # ------------------------------------------------------------------
    # Crea strategie spot per ciascun simbolo
    # ------------------------------------------------------------------
    symbols_cfg = cfg.get("symbols", {})
    if not symbols_cfg:
        raise RuntimeError("No 'symbols' configured in config")

    tasks = []

    for symbol, sym_cfg in symbols_cfg.items():
        enabled = sym_cfg.get("enabled", True)
        product_type = sym_cfg.get("product_type", "spot")

        if not enabled:
            logger.info("Skipping disabled symbol", extra={"symbol": symbol})
            continue
        if product_type != "spot":
            # questo file gestisce solo SPOT
            continue

        venue_name = sym_cfg.get("venue")
        if not venue_name or venue_name not in clients_by_venue:
            logger.error(
                "Symbol has unknown or missing venue",
                extra={"symbol": symbol, "venue": venue_name},
            )
            continue

        client = clients_by_venue[venue_name]
        order_manager = order_managers_by_venue[venue_name]

        # Subscribe orderbook per il simbolo
        depth_limit = sym_cfg.get("depth_limit", 50)
        await client.subscribe_orderbook(symbol, limit=depth_limit)

        quoter_logger = logging.getLogger(f"Quoter_{symbol}")
        quoter = SpotQuoter(symbol, sym_cfg, quoter_logger)

        strat_logger = logging.getLogger(f"Strategy_{symbol}")
        strategy = SymbolStrategySpot(
            symbol=symbol,
            sym_cfg=sym_cfg,
            client=client,
            risk_engine=risk_engine,
            quoter=quoter,
            order_manager=order_manager,
            metrics=metrics,
            logger=strat_logger,
        )

        task = asyncio.create_task(strategy.run(), name=f"spot_{symbol}")
        tasks.append(task)
        logger.info(
            "Started spot strategy",
            extra={"symbol": symbol, "venue": venue_name, "depth_limit": depth_limit},
        )

    if not tasks:
        logger.error("No spot strategies started (check symbols.product_type/enabled)")
        return

    logger.info("All spot strategies started", extra={"count": len(tasks)})

    # ------------------------------------------------------------------
    # Main loop: attendi che tutte le strategie terminino
    # ------------------------------------------------------------------
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Cancellation requested, shutting down strategies...")
    finally:
        # chiudi i client in modo pulito
        for venue_name, client in clients_by_venue.items():
            v_logger = logging.getLogger(f"Venue_{venue_name}")
            v_logger.info("Closing venue client")
            try:
                await client.close()
            except Exception as e:
                v_logger.warning("Error closing client", extra={"error": str(e)})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spot Market Making Bot")
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to configuration file (default: config/config.yaml)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    asyncio.run(run_spot_mm(str(config_path)))


if __name__ == "__main__":
    main()

