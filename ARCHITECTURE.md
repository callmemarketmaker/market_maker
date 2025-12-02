mm_bot/
  config/
    config.yaml              # configurazione unica, multi-venue e multi-simbolo

  core/
    __init__.py
    models.py                # dataclass generali (MarketState, Order, Inventory, ecc.)
    config_loader.py         # caricamento + validazione config

    infra/
      __init__.py
      logging_setup.py       # logging JSON, livelli, file, ecc.
      metrics.py             # Prometheus metrics server + collector
      process_pool.py        # ProcessPoolExecutor per CPU-bound/ML
      errors.py              # eccezioni custom, circuit breaker, retry helper

    exchange/
      __init__.py
      base.py                # interfaccia ExchangeClient + ProductModel
      factory.py             # crea il client giusto per ogni venue
      whitebit_spot.py       # implementazione concreta per WhiteBIT Spot
      whitebit_perp.py       # (stub) per WhiteBIT Perp
      # in futuro: binance_spot.py, bybit_perp.py, ecc.

    engine/
      __init__.py
      state_store.py         # stato per simbolo (inventario, pnl, history)
      risk_spot.py           # SpotRiskEngine
      risk_perp.py           # PerpRiskEngine
      quoting_spot.py        # SpotQuoter (MM Spot)
      quoting_perp.py        # PerpQuoter (MM Perp)
      strategy_spot.py       # SymbolStrategySpot (loop async per simbolo spot)
      strategy_perp.py       # SymbolStrategyPerp

    ml/
      __init__.py
      features.py            # feature extraction da book/trades
      models.py              # wrapper per modelli ML (in futuro)

  spot_mm/
    run_spot_mm.py           # entrypoint: MM spot multiplo

  perp_mm/
    run_perp_mm.py           # entrypoint: MM perp multiplo
