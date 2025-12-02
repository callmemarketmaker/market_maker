# core/analytics/pnl.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional
import time

from ..models import Side


@dataclass
class Fill:
    """
    Rappresenta un fill (esecuzione parziale o totale) a livello di trade.

    NOTA: questo è separato da Order: un ordine può generare N fill.
    """
    symbol: str
    side: Side
    price: float         # prezzo per 1 unità di base
    amount: float        # quantità in base (es. MEME)
    fee: float = 0.0
    fee_currency: str = ""   # es. "USDT", "MEME"
    ts: float = field(default_factory=time.time)


@dataclass
class SymbolPnlState:
    """
    Stato PnL per un singolo simbolo (es. MEME/USDT).

    - base_position: posizione netta in base (>=0 long, <0 short)
    - quote_position: cash in quote (per spot è il saldo in USDT accoppiato a questo symbol)
    - avg_entry_price: costo medio della posizione netta in base
    - realized_pnl_quote: PnL realizzato in quote (USDT)
    - realized_fees_quote: fee totali (convertite in quote quando possibile)
    - volume_base: volume totale tradato in base (somma di |amount|)
    - volume_quote: volume totale tradato in quote (somma di |amount * price|)
    """
    symbol: str
    base_position: float = 0.0
    quote_position: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl_quote: float = 0.0
    realized_fees_quote: float = 0.0
    volume_base: float = 0.0
    volume_quote: float = 0.0
    last_update_ts: float = field(default_factory=time.time)

    def apply_fill(self, fill: Fill, mark_price_for_fee: Optional[float] = None) -> None:
        """
        Aggiorna lo stato con un nuovo fill.

        Per spot:

        - BUY:
          * base_position += amount
          * quote_position -= amount * price
          * avg_entry_price ricalcolato solo sulla parte netta long (se rimani long)
        - SELL:
          * base_position -= amount
          * quote_position += amount * price
          * se eri long, parte della vendita genera realized_pnl
        """
        self.last_update_ts = fill.ts

        signed_amount = fill.amount if fill.side == Side.BUY else -fill.amount
        trade_notional = fill.amount * fill.price

        # Aggiorna volume
        self.volume_base += abs(fill.amount)
        self.volume_quote += abs(trade_notional)

        # Gestione fee (semplificata):
        # - se fee in quote -> sottrai direttamente dall'equity quote
        # - se fee in base -> aggiusta la base_position (riduci leggermente)
        fee_in_quote = 0.0
        if fill.fee != 0.0:
            if fill.fee_currency == self._quote_ccy():
                fee_in_quote = fill.fee
                self.realized_fees_quote += fee_in_quote
                self.quote_position -= fee_in_quote
            elif fill.fee_currency == self._base_ccy():
                # riduci base_position; per PnL preciso dovresti anche modificare avg_entry
                self.base_position -= fill.fee
            else:
                # fee in terza valuta: se hai un mark_price_for_fee, converti,
                # altrimenti ignora (o logga)
                if mark_price_for_fee is not None:
                    # ipotesi: fee_currency "≈ base" per semplicità
                    fee_in_quote = fill.fee * mark_price_for_fee
                    self.realized_fees_quote += fee_in_quote
                    self.quote_position -= fee_in_quote

        # Se non hai ancora posizione netta (o sei flat), l'operazione definisce il nuovo lato
        if self.base_position == 0.0:
            # apri nuova posizione
            self.base_position = signed_amount
            # cash flow in quote
            self.quote_position -= trade_notional if fill.side == Side.BUY else -trade_notional
            self.avg_entry_price = fill.price
            return

        # Se la nuova operazione va nella stessa direzione della posizione
        if (self.base_position > 0 and signed_amount > 0) or (self.base_position < 0 and signed_amount < 0):
            # stai aggiungendo alla posizione esistente
            old_pos = self.base_position
            new_pos = self.base_position + signed_amount

            # aggiorna avg_entry_price solo se non attraversi lo zero
            if (old_pos > 0 and new_pos > 0) or (old_pos < 0 and new_pos < 0):
                # media ponderata dei costi
                # NB: per short puro dovresti usare logica leggermente diversa,
                # ma per spot MM normalmente sei net long / vicino allo zero
                total_cost_old = self.avg_entry_price * abs(old_pos)
                total_cost_new = total_cost_old + fill.price * abs(signed_amount)
                self.avg_entry_price = total_cost_new / abs(new_pos)

            self.base_position = new_pos
            self.quote_position -= trade_notional if fill.side == Side.BUY else -trade_notional
            return

        # Se la nuova operazione va nella direzione opposta -> stai chiudendo parte o tutta la posizione
        old_pos = self.base_position
        new_pos = self.base_position + signed_amount

        # quantità che chiude la posizione esistente (in base)
        closed_amount = min(abs(fill.amount), abs(old_pos))
        if closed_amount > 0:
            # PnL realizzato = (prezzo_fill - avg_entry_price) * quantità (con segno corretto)
            if old_pos > 0:
                # eri long, vendi
                pnl = (fill.price - self.avg_entry_price) * closed_amount
            else:
                # eri short, compri per chiudere
                pnl = (self.avg_entry_price - fill.price) * closed_amount

            self.realized_pnl_quote += pnl

        # aggiorna posizioni cash e base
        self.base_position = new_pos
        self.quote_position -= trade_notional if fill.side == Side.BUY else -trade_notional

        # se hai attraversato lo zero (da long a short o viceversa),
        # la parte residua apre una nuova posizione a prezzo fill
        if old_pos > 0 and new_pos < 0:
            # eri long, hai venduto più di quanto detenuto => ora short
            residual = abs(new_pos)
            self.avg_entry_price = fill.price
        elif old_pos < 0 and new_pos > 0:
            # eri short, hai comprato più di quanto short => ora long
            residual = abs(new_pos)
            self.avg_entry_price = fill.price
        elif new_pos == 0.0:
            # flat
            self.avg_entry_price = 0.0

    def snapshot(self, mark_price: float) -> dict:
        """
        Crea uno snapshot PnL per questo simbolo al prezzo mark indicato.
        """
        unrealized = (mark_price - self.avg_entry_price) * self.base_position
        equity_quote = self.quote_position + self.base_position * mark_price

        return {
            "symbol": self.symbol,
            "base_position": self.base_position,
            "quote_position": self.quote_position,
            "avg_entry_price": self.avg_entry_price,
            "realized_pnl_quote": self.realized_pnl_quote,
            "realized_fees_quote": self.realized_fees_quote,
            "unrealized_pnl_quote": unrealized,
            "equity_quote": equity_quote,
            "volume_base": self.volume_base,
            "volume_quote": self.volume_quote,
            "last_update_ts": self.last_update_ts,
        }

    # Helpers per base/quote, assumendo formato "BASE/QUOTE"
    def _base_ccy(self) -> str:
        return self.symbol.split("/")[0]

    def _quote_ccy(self) -> str:
        return self.symbol.split("/")[1]


class PnlEngine:
    """
    Gestore PnL multi-simbolo.

    - Tiene SymbolPnlState per ogni symbol.
    - Espone on_fill() per aggiornare PnL in tempo reale.
    - Espone snapshot()/aggregate() per consultare lo stato.
    """

    def __init__(self, logger):
        self.logger = logger
        self._states: Dict[str, SymbolPnlState] = {}

    def _get_state(self, symbol: str) -> SymbolPnlState:
        if symbol not in self._states:
            self._states[symbol] = SymbolPnlState(symbol=symbol)
        return self._states[symbol]

    def on_fill(self, fill: Fill, mark_price_for_fee: Optional[float] = None) -> None:
        """
        Deve essere chiamata ogni volta che ricevi un fill (da WS privato o da history).
        """
        st = self._get_state(fill.symbol)
        st.apply_fill(fill, mark_price_for_fee=mark_price_for_fee)

    def snapshot_symbol(self, symbol: str, mark_price: float) -> dict:
        st = self._get_state(symbol)
        return st.snapshot(mark_price)

    def snapshot_all(self, mark_prices: Dict[str, float]) -> Dict[str, dict]:
        """
        mark_prices: mapping symbol -> mark price
        """
        res: Dict[str, dict] = {}
        for symbol, st in self._states.items():
            mp = mark_prices.get(symbol)
            if mp is None:
                continue
            res[symbol] = st.snapshot(mp)
        return res

    def aggregate_equity(self, mark_prices: Dict[str, float]) -> float:
        """
        Ritorna l'equity totale in quote (somma su tutti i simboli).
        """
        eq = 0.0
        for symbol, st in self._states.items():
            mp = mark_prices.get(symbol)
            if mp is None:
                continue
            snap = st.snapshot(mp)
            eq += snap["equity_quote"]
        return eq
