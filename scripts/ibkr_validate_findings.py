#!/usr/bin/env python3
"""Re-check delayed kabu-opu arbitrage candidates against IBKR live/frozen quotes.

This tool is deliberately read-only: it resolves contracts and requests market-data
snapshots, but it never submits, modifies, or cancels orders.

Input must be a findings CSV produced by the Phase-3-aware quote_arbitrage_scan.py,
which includes machine-readable ``legs_json`` and ``floor_pv_per_share`` columns.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class Leg:
    action: str
    option_type: str
    strike: float
    qty: int

    @property
    def key(self) -> tuple[str, float]:
        return self.option_type, self.strike


@dataclass
class LiveQuote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    first_update_utc: Optional[str] = None
    last_update_utc: Optional[str] = None


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_legs_json(value: str) -> list[Leg]:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid legs_json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs_json must be a non-empty JSON array")

    legs: list[Leg] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"leg {i} is not an object")
        action = str(item.get("action", "")).upper()
        option_type = str(item.get("option_type", "")).upper()[:1]
        strike = _finite(item.get("strike"))
        try:
            qty = int(item.get("qty", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"leg {i} has invalid qty") from exc
        if action not in {"BUY", "SELL"}:
            raise ValueError(f"leg {i} has invalid action={action!r}")
        if option_type not in {"C", "P"}:
            raise ValueError(f"leg {i} has invalid option_type={option_type!r}")
        if strike is None or strike <= 0:
            raise ValueError(f"leg {i} has invalid strike")
        if qty <= 0:
            raise ValueError(f"leg {i} has non-positive qty")
        legs.append(Leg(action, option_type, strike, qty))
    return legs


def reprice_candidate(
    legs: list[Leg],
    quotes: dict[tuple[str, float], LiveQuote],
    *,
    floor_pv_per_share: float,
    lot_size: float,
    fee_per_contract_leg: float,
) -> dict[str, float | None]:
    """Price the exact candidate legs using live executable sides.

    A positive ``live_net_edge_per_contract`` means the static lower bound still
    clears the supplied fee/slippage allowance at the received quotes.  It is not
    an execution guarantee because separate snapshots are not atomic fills.
    """
    debit = 0.0
    executable_sizes: list[float] = []
    fee_legs = 0

    for leg in legs:
        quote = quotes.get(leg.key)
        if quote is None:
            raise ValueError(f"missing live quote for {leg.option_type} {leg.strike:g}")
        if leg.action == "BUY":
            px = _finite(quote.ask)
            size = _finite(quote.ask_size)
            if px is None or px <= 0:
                raise ValueError(f"missing live ask for {leg.option_type} {leg.strike:g}")
            debit += leg.qty * px
        else:
            px = _finite(quote.bid)
            size = _finite(quote.bid_size)
            if px is None or px < 0:
                raise ValueError(f"missing live bid for {leg.option_type} {leg.strike:g}")
            debit -= leg.qty * px
        fee_legs += leg.qty
        if size is not None and size >= 0:
            executable_sizes.append(size / leg.qty)

    gross_edge_per_share = float(floor_pv_per_share) - debit
    gross_edge_per_contract = gross_edge_per_share * float(lot_size)
    fees_per_contract = fee_legs * float(fee_per_contract_leg)
    net_edge_per_contract = gross_edge_per_contract - fees_per_contract
    min_quote_size = min(executable_sizes) if len(executable_sizes) == len(legs) else None

    return {
        "live_net_debit_per_share": debit,
        "live_gross_edge_per_share": gross_edge_per_share,
        "live_gross_edge_per_contract": gross_edge_per_contract,
        "live_fee_legs": float(fee_legs),
        "live_fees_per_contract": fees_per_contract,
        "live_net_edge_per_contract": net_edge_per_contract,
        "live_min_quote_size": min_quote_size,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _quote_json(legs: list[Leg], quotes: dict[tuple[str, float], LiveQuote]) -> str:
    out = []
    seen: set[tuple[str, float]] = set()
    for leg in legs:
        if leg.key in seen:
            continue
        seen.add(leg.key)
        q = quotes[leg.key]
        out.append({
            "option_type": leg.option_type,
            "strike": leg.strike,
            "bid": q.bid,
            "ask": q.ask,
            "bid_size": q.bid_size,
            "ask_size": q.ask_size,
            "first_update_utc": q.first_update_utc,
            "last_update_utc": q.last_update_utc,
        })
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _snapshot_skew_ms(quotes: dict[tuple[str, float], LiveQuote]) -> Optional[float]:
    stamps = []
    for q in quotes.values():
        if q.last_update_utc:
            try:
                stamps.append(datetime.fromisoformat(q.last_update_utc))
            except ValueError:
                pass
    if len(stamps) < 2:
        return 0.0 if stamps else None
    return (max(stamps) - min(stamps)).total_seconds() * 1000.0


class IBKRUnavailable(RuntimeError):
    pass


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise IBKRUnavailable(
            "IBKR Python API is not installed. Install the official TWS API Python package "
            "(ibapi), then retry."
        ) from exc
    return EClient, EWrapper, Contract


def make_ibkr_app():
    EClient, EWrapper, Contract = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self._lock = threading.Lock()
            self._next_req_id = 1000
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.market_rows: dict[int, LiveQuote] = {}
            self.market_done: dict[int, threading.Event] = {}
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802, ARG002
            self.ready.set()

        def next_req_id(self) -> int:
            with self._lock:
                self._next_req_id += 1
                return self._next_req_id

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802, ANN001
            self.errors.append((int(reqId), int(errorCode), str(errorString)))
            # Errors such as "no security definition" otherwise make callers wait
            # the full timeout. Signal any pending request with this reqId.
            if reqId in self.contract_done:
                self.contract_done[reqId].set()
            if reqId in self.market_done:
                self.market_done[reqId].set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(reqId, []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(reqId, threading.Event()).set()

        def tickPrice(self, reqId, tickType, price, attrib=None):  # noqa: N802, ANN001, ARG002
            q = self.market_rows.setdefault(reqId, LiveQuote())
            now = _utc_now_iso()
            q.first_update_utc = q.first_update_utc or now
            q.last_update_utc = now
            if tickType == 1:  # BID
                q.bid = _finite(price)
            elif tickType == 2:  # ASK
                q.ask = _finite(price)

        def tickSize(self, reqId, tickType, size):  # noqa: N802, ANN001
            q = self.market_rows.setdefault(reqId, LiveQuote())
            now = _utc_now_iso()
            q.first_update_utc = q.first_update_utc or now
            q.last_update_utc = now
            if tickType == 0:  # BID_SIZE
                q.bid_size = _finite(size)
            elif tickType == 3:  # ASK_SIZE
                q.ask_size = _finite(size)

        def tickSnapshotEnd(self, reqId):  # noqa: N802, ANN001
            self.market_done.setdefault(reqId, threading.Event()).set()

    return App(), Contract


def _contract_error(app, req_id: int) -> str:
    msgs = [f"{code}: {msg}" for rid, code, msg in app.errors if rid == req_id]
    return " | ".join(msgs)


def resolve_option_contract(app, Contract, *, underlying: str, expiry: str, leg: Leg,
                            exchange: str, currency: str, timeout: float):  # noqa: N803, ANN001
    req_id = app.next_req_id()
    event = threading.Event()
    app.contract_done[req_id] = event
    app.contract_rows[req_id] = []

    c = Contract()
    c.symbol = str(underlying)
    c.secType = "OPT"
    c.exchange = exchange
    c.currency = currency
    c.lastTradeDateOrContractMonth = str(expiry).replace("-", "")
    c.strike = float(leg.strike)
    c.right = leg.option_type
    app.reqContractDetails(req_id, c)
    event.wait(timeout)

    rows = app.contract_rows.get(req_id, [])
    if len(rows) != 1:
        err = _contract_error(app, req_id)
        extra = f"; IBKR error: {err}" if err else ""
        raise ValueError(
            f"contract resolution returned {len(rows)} matches for {underlying} {expiry} "
            f"{leg.option_type}{leg.strike:g} on {exchange}{extra}"
        )
    return rows[0].contract


def snapshot_quotes(app, contracts: dict[tuple[str, float], Any], *, timeout: float,
                    market_data_type: int) -> dict[tuple[str, float], LiveQuote]:
    app.reqMarketDataType(market_data_type)
    req_for_key: dict[tuple[str, float], int] = {}
    for key, contract in contracts.items():
        req_id = app.next_req_id()
        req_for_key[key] = req_id
        app.market_rows[req_id] = LiveQuote()
        app.market_done[req_id] = threading.Event()
        app.reqMktData(req_id, contract, "", True, False, [])

    deadline = time.monotonic() + timeout
    for req_id in req_for_key.values():
        remaining = max(0.0, deadline - time.monotonic())
        app.market_done[req_id].wait(remaining)

    out: dict[tuple[str, float], LiveQuote] = {}
    for key, req_id in req_for_key.items():
        q = app.market_rows.get(req_id, LiveQuote())
        out[key] = q
    return out


def validate_findings(
    df: pd.DataFrame,
    *,
    app,
    Contract,
    exchange: str,
    currency: str,
    timeout: float,
    market_data_type: int,
    fee_per_contract_leg: Optional[float],
    limit: Optional[int],
) -> pd.DataFrame:  # noqa: N803, ANN001
    required = {"underlying", "expiry", "legs_json", "floor_pv_per_share", "lot_size"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            "findings CSV is from an older scanner; rerun quote_arbitrage_scan.py. "
            f"Missing columns: {', '.join(sorted(missing))}"
        )

    work = df.head(limit) if limit is not None else df
    rows: list[dict[str, Any]] = []

    for _, finding in work.iterrows():
        base = finding.to_dict()
        try:
            legs = parse_legs_json(str(finding["legs_json"]))
            contracts: dict[tuple[str, float], Any] = {}
            for leg in legs:
                if leg.key not in contracts:
                    contracts[leg.key] = resolve_option_contract(
                        app, Contract,
                        underlying=str(finding["underlying"]),
                        expiry=str(finding["expiry"]),
                        leg=leg,
                        exchange=exchange,
                        currency=currency,
                        timeout=timeout,
                    )
            live = snapshot_quotes(
                app, contracts, timeout=timeout, market_data_type=market_data_type
            )

            candidate_fee = fee_per_contract_leg
            if candidate_fee is None:
                candidate_fee = _finite(finding.get("fee_per_contract_leg"))
            if candidate_fee is None:
                candidate_fee = 0.0

            metrics = reprice_candidate(
                legs,
                live,
                floor_pv_per_share=float(finding["floor_pv_per_share"]),
                lot_size=float(finding["lot_size"]),
                fee_per_contract_leg=float(candidate_fee),
            )
            base.update(metrics)
            base["live_quotes_json"] = _quote_json(legs, live)
            base["snapshot_skew_ms"] = _snapshot_skew_ms(live)
            base["ibkr_exchange"] = exchange
            base["ibkr_market_data_type"] = market_data_type
            base["validation_status"] = (
                "CONFIRMED_CANDIDATE" if metrics["live_net_edge_per_contract"] > 0
                else "NO_LONGER_POSITIVE"
            )
            base["validation_error"] = ""
        except Exception as exc:  # keep batch validation moving
            base["validation_status"] = "ERROR"
            base["validation_error"] = str(exc)
        rows.append(base)

    out = pd.DataFrame(rows)
    if "live_net_edge_per_contract" in out.columns:
        out = out.sort_values(
            ["validation_status", "live_net_edge_per_contract"],
            ascending=[True, False], na_position="last"
        ).reset_index(drop=True)
    return out


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="findings CSV from quote_arbitrage_scan.py")
    p.add_argument("--output", help="validation CSV path; default stdout")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="TWS/Gateway API port")
    p.add_argument("--client-id", type=int, default=41)
    p.add_argument("--exchange", default="OSE.JPN", help="IBKR option exchange code")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0, help="seconds per contract/snapshot stage")
    p.add_argument("--limit", type=int, default=20, help="validate top N delayed candidates")
    p.add_argument(
        "--market-data-type", choices=["live", "frozen", "delayed", "delayed-frozen"],
        default="live",
    )
    p.add_argument(
        "--fee-per-contract-leg", type=float, default=None,
        help="override scanner fee/slippage allowance; otherwise reuse scanner value",
    )
    return p.parse_args()


def main() -> int:
    args = _args()
    md_types = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
    app, Contract = make_ibkr_app()
    app.connect(args.host, args.port, args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(args.timeout):
        app.disconnect()
        raise SystemExit("IBKR API connection did not become ready; check TWS/Gateway API settings")

    try:
        df = pd.read_csv(args.input)
        out = validate_findings(
            df,
            app=app,
            Contract=Contract,
            exchange=args.exchange,
            currency=args.currency,
            timeout=args.timeout,
            market_data_type=md_types[args.market_data_type],
            fee_per_contract_leg=args.fee_per_contract_leg,
            limit=args.limit,
        )
    finally:
        app.disconnect()

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"wrote {len(out)} validation row(s) to {args.output}")
    elif out.empty:
        print("no findings to validate")
    else:
        print(out.to_csv(index=False))

    confirmed = 0 if out.empty else int((out["validation_status"] == "CONFIRMED_CANDIDATE").sum())
    print(f"confirmed live candidates: {confirmed}/{len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
