#!/usr/bin/env python3
"""Monitor kabu-opu arbitrage candidates with simultaneous IBKR streaming quotes.

Read-only Phase 4 tool. It resolves each candidate's option contracts, subscribes to
all legs concurrently with reqMktData(snapshot=False), samples executable sides at a
fixed cadence, and reports whether a positive post-fee edge persists. It never sends,
modifies, or cancels orders (cancelMktData only stops quote subscriptions).
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


MARKET_DATA_TYPE_NAMES = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed-frozen",
}


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
class StreamQuote:
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    market_data_type: Optional[int] = None
    bid_update_mono: Optional[float] = None
    ask_update_mono: Optional[float] = None
    bid_size_update_mono: Optional[float] = None
    ask_size_update_mono: Optional[float] = None


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def market_data_type_name(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    return MARKET_DATA_TYPE_NAMES.get(int(value), f"unknown-{value}")


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


def apply_tick_price(quote: StreamQuote, tick_type: int, price: Any, now_mono: float) -> bool:
    """Apply live or delayed bid/ask tickPrice and timestamp that exact side."""
    if tick_type in {1, 66}:  # BID / DELAYED_BID
        quote.bid = _finite(price)
        quote.bid_update_mono = now_mono
        return True
    if tick_type in {2, 67}:  # ASK / DELAYED_ASK
        quote.ask = _finite(price)
        quote.ask_update_mono = now_mono
        return True
    return False


def apply_tick_size(quote: StreamQuote, tick_type: int, size: Any, now_mono: float) -> bool:
    """Apply live or delayed bid/ask size and timestamp that exact side."""
    if tick_type in {0, 69}:  # BID_SIZE / DELAYED_BID_SIZE
        quote.bid_size = _finite(size)
        quote.bid_size_update_mono = now_mono
        return True
    if tick_type in {3, 70}:  # ASK_SIZE / DELAYED_ASK_SIZE
        quote.ask_size = _finite(size)
        quote.ask_size_update_mono = now_mono
        return True
    return False


def executable_side(
    leg: Leg, quote: StreamQuote
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return executable price/size and their exact-side update timestamps."""
    if leg.action == "BUY":
        return quote.ask, quote.ask_size, quote.ask_update_mono, quote.ask_size_update_mono
    return quote.bid, quote.bid_size, quote.bid_update_mono, quote.bid_size_update_mono


def sample_candidate(
    legs: list[Leg],
    quotes: dict[tuple[str, float], StreamQuote],
    *,
    floor_pv_per_share: float,
    lot_size: float,
    fee_per_contract_leg: float,
    now_mono: float,
    max_quote_age_sec: float,
) -> dict[str, Any]:
    """Reprice one package and classify quote freshness / actual market-data type."""
    debit = 0.0
    executable_sizes: list[float] = []
    side_times: list[float] = []
    size_times: list[float] = []
    fee_legs = 0
    data_types: list[int] = []

    for leg in legs:
        quote = quotes.get(leg.key)
        if quote is None:
            return {"sample_status": "MISSING_QUOTE"}
        px, size, side_time, size_time = executable_side(leg, quote)
        if px is None or (px <= 0 if leg.action == "BUY" else px < 0):
            return {"sample_status": "MISSING_EXECUTABLE_SIDE"}
        if side_time is None:
            return {"sample_status": "MISSING_SIDE_TIMESTAMP"}
        age = max(0.0, now_mono - side_time)
        if age > max_quote_age_sec:
            return {
                "sample_status": "STALE_QUOTE",
                "max_quote_age_ms": age * 1000.0,
            }
        if size is None or size < leg.qty:
            return {"sample_status": "NO_EXECUTABLE_SIZE"}
        if size_time is None:
            return {"sample_status": "MISSING_SIZE_TIMESTAMP"}
        size_age = max(0.0, now_mono - size_time)
        if size_age > max_quote_age_sec:
            return {
                "sample_status": "STALE_SIZE",
                "max_size_age_ms": size_age * 1000.0,
            }
        if quote.market_data_type is None:
            return {"sample_status": "UNVERIFIED_DATA_TYPE"}
        data_types.append(int(quote.market_data_type))

        if leg.action == "BUY":
            debit += leg.qty * float(px)
        else:
            debit -= leg.qty * float(px)
        fee_legs += leg.qty
        side_times.append(side_time)
        size_times.append(size_time)
        executable_sizes.append(float(size) / leg.qty)

    gross_edge_per_share = float(floor_pv_per_share) - debit
    gross_edge_per_contract = gross_edge_per_share * float(lot_size)
    fees_per_contract = fee_legs * float(fee_per_contract_leg)
    net_edge_per_contract = gross_edge_per_contract - fees_per_contract
    min_quote_size = min(executable_sizes) if len(executable_sizes) == len(legs) else None
    max_age_ms = max((now_mono - x) for x in side_times) * 1000.0
    max_size_age_ms = max((now_mono - x) for x in size_times) * 1000.0
    side_skew_ms = (max(side_times) - min(side_times)) * 1000.0 if side_times else None
    unique_types = sorted(set(data_types))

    if unique_types == [1]:
        status = "LIVE_POSITIVE" if net_edge_per_contract > 0 else "LIVE_NONPOSITIVE"
    else:
        status = "NONLIVE_POSITIVE" if net_edge_per_contract > 0 else "NONLIVE_NONPOSITIVE"

    return {
        "sample_status": status,
        "net_debit_per_share": debit,
        "gross_edge_per_contract": gross_edge_per_contract,
        "fees_per_contract": fees_per_contract,
        "net_edge_per_contract": net_edge_per_contract,
        "min_quote_size": min_quote_size,
        "max_quote_age_ms": max_age_ms,
        "max_size_age_ms": max_size_age_ms,
        "side_skew_ms": side_skew_ms,
        "actual_market_data_types": json.dumps(unique_types, separators=(",", ":")),
        "actual_market_data_type_names": ",".join(market_data_type_name(x) for x in unique_types),
    }


def summarize_samples(samples: list[dict[str, Any]], sample_interval_sec: float) -> dict[str, Any]:
    """Summarize persistence conservatively from regularly-spaced observations."""
    total = len(samples)
    valid = [s for s in samples if "net_edge_per_contract" in s]
    live = [s for s in valid if str(s.get("sample_status", "")).startswith("LIVE_")]
    live_positive = [s for s in live if s.get("sample_status") == "LIVE_POSITIVE"]

    longest_run = 0
    current_run = 0
    for s in samples:
        if s.get("sample_status") == "LIVE_POSITIVE":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0

    edges = [float(s["net_edge_per_contract"]) for s in live_positive]
    sizes = [float(s["min_quote_size"]) for s in live_positive if s.get("min_quote_size") is not None]
    skews = [float(s["side_skew_ms"]) for s in valid if s.get("side_skew_ms") is not None]
    ages = [float(s["max_quote_age_ms"]) for s in valid if s.get("max_quote_age_ms") is not None]
    size_ages = [float(s["max_size_age_ms"]) for s in valid if s.get("max_size_age_ms") is not None]

    return {
        "samples_total": total,
        "samples_valid": len(valid),
        "samples_live": len(live),
        "samples_live_positive": len(live_positive),
        "live_positive_ratio": (len(live_positive) / len(live)) if live else 0.0,
        # Observed-duration lower bound: N positive samples at cadence dt imply at least
        # max(0, N-1)*dt between first and last positive observation in a run.
        "longest_live_positive_run_sec": max(0, longest_run - 1) * float(sample_interval_sec),
        "min_live_positive_edge_per_contract": min(edges) if edges else None,
        "max_live_positive_edge_per_contract": max(edges) if edges else None,
        "min_live_positive_quote_size": min(sizes) if sizes else None,
        "max_observed_side_skew_ms": max(skews) if skews else None,
        "max_observed_quote_age_ms": max(ages) if ages else None,
        "max_observed_size_age_ms": max(size_ages) if size_ages else None,
    }


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError(
            "IBKR Python API is not installed. Install the official TWS API Python package (ibapi)."
        ) from exc
    return EClient, EWrapper, Contract


def make_stream_app():
    EClient, EWrapper, Contract = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self._lock = threading.Lock()
            self._next_req_id = 20000
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.market_rows: dict[int, StreamQuote] = {}
            self.market_lock = threading.Lock()
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802, ARG002
            self.ready.set()

        def next_req_id(self) -> int:
            with self._lock:
                self._next_req_id += 1
                return self._next_req_id

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802, ANN001
            self.errors.append((int(reqId), int(errorCode), str(errorString)))
            if reqId in self.contract_done:
                self.contract_done[reqId].set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(reqId, []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(reqId, threading.Event()).set()

        def marketDataType(self, reqId, marketDataType):  # noqa: N802, ANN001
            with self.market_lock:
                self.market_rows.setdefault(reqId, StreamQuote()).market_data_type = int(marketDataType)

        def tickPrice(self, reqId, tickType, price, attrib=None):  # noqa: N802, ANN001, ARG002
            with self.market_lock:
                q = self.market_rows.setdefault(reqId, StreamQuote())
                apply_tick_price(q, int(tickType), price, time.monotonic())

        def tickSize(self, reqId, tickType, size):  # noqa: N802, ANN001
            with self.market_lock:
                q = self.market_rows.setdefault(reqId, StreamQuote())
                apply_tick_size(q, int(tickType), size, time.monotonic())

    return App(), Contract


def _contract_error(app, req_id: int) -> str:
    return " | ".join(f"{code}: {msg}" for rid, code, msg in app.errors if rid == req_id)


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


def subscribe_quotes(app, contracts: dict[tuple[str, float], Any], *, market_data_type: int):
    app.reqMarketDataType(market_data_type)
    req_for_key: dict[tuple[str, float], int] = {}
    for key, contract in contracts.items():
        req_id = app.next_req_id()
        req_for_key[key] = req_id
        app.market_rows[req_id] = StreamQuote()
        # snapshot=False => continuous subscription until cancelMktData.
        app.reqMktData(req_id, contract, "", False, False, [])
    return req_for_key


def cancel_quotes(app, req_for_key: dict[tuple[str, float], int]) -> None:
    for req_id in req_for_key.values():
        try:
            app.cancelMktData(req_id)
        except Exception:
            pass


def current_quotes(app, req_for_key: dict[tuple[str, float], int]) -> dict[tuple[str, float], StreamQuote]:
    with app.market_lock:
        out: dict[tuple[str, float], StreamQuote] = {}
        for key, req_id in req_for_key.items():
            q = app.market_rows.get(req_id, StreamQuote())
            out[key] = StreamQuote(**vars(q))
        return out


def monitor_one(
    finding: pd.Series,
    *,
    app,
    Contract,
    exchange: str,
    currency: str,
    timeout: float,
    market_data_type: int,
    fee_per_contract_leg: Optional[float],
    duration_sec: float,
    sample_interval_sec: float,
    warmup_sec: float,
    max_quote_age_sec: float,
) -> dict[str, Any]:  # noqa: N803, ANN001
    base = finding.to_dict()
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

    candidate_fee = fee_per_contract_leg
    if candidate_fee is None:
        candidate_fee = _finite(finding.get("fee_per_contract_leg"))
    if candidate_fee is None:
        candidate_fee = 0.0

    req_for_key = subscribe_quotes(app, contracts, market_data_type=market_data_type)
    samples: list[dict[str, Any]] = []
    monitor_started_at_utc = datetime.now(timezone.utc).isoformat()
    try:
        if warmup_sec > 0:
            time.sleep(warmup_sec)
        start = time.monotonic()
        next_sample = start
        end = start + duration_sec
        while True:
            now = time.monotonic()
            if now > end + 1e-9:
                break
            if now < next_sample:
                time.sleep(next_sample - now)
                now = time.monotonic()
            quotes = current_quotes(app, req_for_key)
            sample = sample_candidate(
                legs,
                quotes,
                floor_pv_per_share=float(finding["floor_pv_per_share"]),
                lot_size=float(finding["lot_size"]),
                fee_per_contract_leg=float(candidate_fee),
                now_mono=now,
                max_quote_age_sec=max_quote_age_sec,
            )
            sample["elapsed_sec"] = now - start
            samples.append(sample)
            next_sample += sample_interval_sec
    finally:
        cancel_quotes(app, req_for_key)

    monitor_finished_at_utc = datetime.now(timezone.utc).isoformat()
    summary = summarize_samples(samples, sample_interval_sec)
    base.update(summary)
    base["monitor_started_at_utc"] = monitor_started_at_utc
    base["monitor_finished_at_utc"] = monitor_finished_at_utc
    base["monitor_duration_sec"] = duration_sec
    base["sample_interval_sec"] = sample_interval_sec
    base["warmup_sec"] = warmup_sec
    base["max_quote_age_sec"] = max_quote_age_sec
    base["ibkr_exchange"] = exchange
    base["ibkr_market_data_type_requested"] = market_data_type
    base["monitor_samples_json"] = json.dumps(samples, ensure_ascii=False, separators=(",", ":"))

    if summary["samples_live_positive"] <= 0:
        status = "NO_PERSISTENT_LIVE_EDGE"
    elif summary["longest_live_positive_run_sec"] >= duration_sec * 0.8:
        status = "PERSISTENT_LIVE_CANDIDATE"
    else:
        status = "INTERMITTENT_LIVE_CANDIDATE"
    base["monitor_status"] = status
    base["monitor_error"] = ""
    return base


def monitor_findings(
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
    duration_sec: float,
    sample_interval_sec: float,
    warmup_sec: float,
    max_quote_age_sec: float,
) -> pd.DataFrame:  # noqa: N803, ANN001
    required = {"underlying", "expiry", "legs_json", "floor_pv_per_share", "lot_size"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing required finding columns: {', '.join(sorted(missing))}")
    work = df.head(limit) if limit is not None else df
    rows: list[dict[str, Any]] = []
    for _, finding in work.iterrows():
        try:
            rows.append(monitor_one(
                finding,
                app=app,
                Contract=Contract,
                exchange=exchange,
                currency=currency,
                timeout=timeout,
                market_data_type=market_data_type,
                fee_per_contract_leg=fee_per_contract_leg,
                duration_sec=duration_sec,
                sample_interval_sec=sample_interval_sec,
                warmup_sec=warmup_sec,
                max_quote_age_sec=max_quote_age_sec,
            ))
        except Exception as exc:
            base = finding.to_dict()
            base["monitor_status"] = "ERROR"
            base["monitor_error"] = str(exc)
            rows.append(base)
    out = pd.DataFrame(rows)
    if "min_live_positive_edge_per_contract" in out.columns:
        out = out.sort_values(
            ["monitor_status", "min_live_positive_edge_per_contract"],
            ascending=[True, False], na_position="last"
        ).reset_index(drop=True)
    return out


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="findings CSV from quote_arbitrage_scan.py")
    p.add_argument("--output", help="summary CSV path; default stdout")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="TWS/Gateway API port")
    p.add_argument("--client-id", type=int, default=42)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0, help="seconds per contract-resolution stage")
    p.add_argument("--limit", type=int, default=5, help="monitor top N candidates sequentially")
    p.add_argument("--duration", type=float, default=10.0, help="monitoring seconds per candidate")
    p.add_argument("--sample-interval", type=float, default=0.5, help="seconds between observations")
    p.add_argument("--warmup", type=float, default=2.0, help="seconds after subscription before sampling")
    p.add_argument(
        "--max-quote-age", type=float, default=3.0,
        help="reject an executable side older than this many seconds",
    )
    p.add_argument(
        "--market-data-type", choices=["live", "frozen", "delayed", "delayed-frozen"],
        default="live",
    )
    p.add_argument("--fee-per-contract-leg", type=float, default=None)
    args = p.parse_args()
    if args.duration <= 0 or args.sample_interval <= 0 or args.max_quote_age <= 0:
        p.error("--duration, --sample-interval and --max-quote-age must be positive")
    if args.warmup < 0:
        p.error("--warmup must be non-negative")
    return args


def main() -> int:
    args = _args()
    md_types = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
    app, Contract = make_stream_app()
    app.connect(args.host, args.port, args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(args.timeout):
        app.disconnect()
        raise SystemExit("IBKR API connection did not become ready; check TWS/Gateway API settings")
    try:
        df = pd.read_csv(args.input)
        out = monitor_findings(
            df,
            app=app,
            Contract=Contract,
            exchange=args.exchange,
            currency=args.currency,
            timeout=args.timeout,
            market_data_type=md_types[args.market_data_type],
            fee_per_contract_leg=args.fee_per_contract_leg,
            limit=args.limit,
            duration_sec=args.duration,
            sample_interval_sec=args.sample_interval,
            warmup_sec=args.warmup,
            max_quote_age_sec=args.max_quote_age,
        )
    finally:
        app.disconnect()

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"wrote {len(out)} monitor row(s) to {args.output}")
    elif out.empty:
        print("no findings to monitor")
    else:
        print(out.to_csv(index=False))
    persistent = 0 if out.empty else int((out["monitor_status"] == "PERSISTENT_LIVE_CANDIDATE").sum())
    print(f"persistent live candidates: {persistent}/{len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
