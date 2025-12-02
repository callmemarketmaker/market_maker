# core/exchange/factory.py
from .base import ExchangeClient
from .whitebit_spot import WhitebitSpotClient
from .whitebit_perp import WhitebitPerpClient


def create_exchange_client(venue_name: str, cfg: dict, logger) -> ExchangeClient:
    exchange = cfg["exchange"]
    vtype = cfg.get("type", "spot")

    if exchange == "whitebit" and vtype == "spot":
        return WhitebitSpotClient(cfg, logger)
    if exchange == "whitebit" and vtype == "perp":
        return WhitebitPerpClient(cfg, logger)

    raise ValueError(f"Unsupported venue: {venue_name} ({exchange}, {vtype})")
