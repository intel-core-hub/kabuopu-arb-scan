#!/usr/bin/env python3
"""Phase 11: read-only OSE option market-data capability probe for IBKR TWS API.

This tool exists because current IBKR pricing pages advertise Osaka Exchange L1/L2
real-time subscriptions, while older TWS API documentation historically stated that
OSE API data was delayed-only. Rather than assume either source describes the user's
actual account/API path, this probe measures what TWS/IB Gateway actually returns.

It never sends, modifies, or cancels orders. It only resolves option contracts,
subscribes to L1 top-of-book data, requests direct market depth, records callbacks,
and then cancels market-data subscriptions.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


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
class L1State:
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    market_data_type: Optional[int] = None
    bid_updates: int = 0
    ask_updates: int = 0
    size_updates: int = 0


@dataclass
class DepthStats:
    events: int = 0
    bid_events: int = 0
    ask_events: int = 0
    positive_size_events: int = 0
    max_position_seen: int = -1
    l2_callbacks: int = 0
    direct_callbacks: int = 0


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


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


def parse_legs_json(value: Any) -> list[Leg]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid legs_json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs_json must be a non-empty JSON array")

    legs: list[Leg] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"leg {i} is not an object")
        action = _text(item.get("action")).upper()
        option_type = _text(item.get("option_type")).upper()[:1]
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
        legs.append(Leg(action, option_type, float(strike), qty))
    return legs


def load_candidate(path: str | Path, candidate_id: Optional[str] = None) -> dict[str, Any]:
    p = Path(path)
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"input CSV is empty: {p}")
    required = {"underlying", "expiry", "legs_json"}
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"input missing required columns: {', '.join(sorted(missing))}")
    if candidate_id is None:
        return rows[0]
    matches = [r for r in rows if _text(r.get("candidate_id")) == candidate_id]
    if not matches:
        raise ValueError(f"candidate_id not found: {candidate_id}")
    return matches[0]


def apply_l1_price(state: L1State, tick_type: int, price: Any) -> bool:
    px = _finite(price)
    if tick_type in {1, 66}:  # bid / delayed bid
        state.bid = px
        state.bid_updates += 1
        return True
    if tick_type in {2, 67}:  # ask / delayed ask
        state.ask = px
        state.ask_updates += 1
        return True
    return False


def apply_l1_size(state: L1State, tick_type: int, size: Any) -> bool:
    sz = _finite(size)
    if tick_type in {0, 69}:  # bid size / delayed bid size
        state.bid_size = sz
        state.size_updates += 1
        return True
    if tick_type in {3, 70}:  # ask size / delayed ask size
        state.ask_size = sz
        state.size_updates += 1
        return True
    return False


def apply_depth_event(stats: DepthStats, *, position: int, side: int, size: Any, is_l2: bool) -> None:
    stats.events += 1
    stats.max_position_seen = max(stats.max_position_seen, int(position))
    if int(side) == 1:  # IBKR: 1=bid, 0=ask
        stats.bid_events += 1
    elif int(side) == 0:
        stats.ask_events += 1
    if (_finite(size) or 0.0) > 0:
        stats.positive_size_events += 1
    if is_l2:
        stats.l2_callbacks += 1
    else:
        stats.direct_callbacks += 1


def classify_leg(*, l1: L1State, depth: DepthStats, errors: Iterable[tuple[int, str]]) -> tuple[str, str]:
    err_text = " | ".join(f"{code}: {msg}" for code, msg in errors)
    has_bid_ask = (
        l1.bid is not None and l1.ask is not None and
        l1.bid >= 0 and l1.ask > 0
    )
    if l1.market_data_type != 1:
        if l1.market_data_type in {2, 3, 4}:
            return (
                "NONLIVE_L1",
                f"IBKR reported {market_data_type_name(l1.market_data_type)} market data; "
                "real-time execution research is not established",
            )
        if err_text:
            return "L1_UNAVAILABLE_OR_UNVERIFIED", err_text
        return "L1_UNAVAILABLE_OR_UNVERIFIED", "no authoritative live marketDataType callback observed"
    if not has_bid_ask:
        return "LIVE_L1_INCOMPLETE", "marketDataType=live but both executable top-of-book sides were not observed"
    if depth.events <= 0:
        reason = "live L1 observed, but no market-depth callback was received"
        if err_text:
            reason += f"; {err_text}"
        return "LIVE_L1_ONLY", reason
    if depth.bid_events <= 0 or depth.ask_events <= 0:
        return "L2_ONE_SIDED", "depth callbacks were observed, but not on both bid and ask sides"
    if depth.positive_size_events <= 0:
        return "L2_NO_POSITIVE_SIZE", "depth callbacks contained no positive displayed size"
    return "REALTIME_L2_OBSERVED", "live L1 plus two-sided direct market-depth callbacks observed"


def summarize_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [_text(r.get("probe_status")) for r in rows]
    counts = {name: statuses.count(name) for name in sorted(set(statuses))}
    if rows and all(s == "REALTIME_L2_OBSERVED" for s in statuses):
        overall = "READY_FOR_L2_FILL_MODEL"
    elif any(s in {"LIVE_L1_ONLY", "L2_ONE_SIDED", "L2_NO_POSITIVE_SIZE"} for s in statuses):
        overall = "REALTIME_L1_ESTABLISHED_L2_NOT_ESTABLISHED"
    elif any(s.startswith("LIVE_L1") for s in statuses):
        overall = "REALTIME_L1_PARTIAL"
    elif any(s == "NONLIVE_L1" for s in statuses):
        overall = "OSE_API_REALTIME_NOT_ESTABLISHED"
    else:
        overall = "PROBE_INCOMPLETE"
    return {
        "phase11_overall_decision": overall,
        "phase11_live_money_allowed": False,
        "phase11_atomicity_established": False,
        "phase11_probe_is_execution_evidence": False,
        "leg_count": len(rows),
        "status_counts": counts,
        "interpretation": (
            "READY_FOR_L2_FILL_MODEL only establishes that this API session delivered live L1 and "
            "market-depth callbacks for every probed leg. It does not establish fill probability, "
            "queue position, atomic execution, or live profitability."
        ),
    }


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError(
            "IBKR Python API is not installed. Install the official TWS API Python package before running the live probe."
        ) from exc
    return EClient, EWrapper, Contract


def make_probe_app():
    EClient, EWrapper, Contract = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self._id_lock = threading.Lock()
            self._next_req_id = 30000
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.l1: dict[int, L1State] = {}
            self.depth: dict[int, DepthStats] = {}
            self.errors: list[tuple[int, int, str]] = []
            self.data_lock = threading.Lock()

        def nextValidId(self, orderId: int) -> None:  # noqa: N802, ARG002
            self.ready.set()

        def next_req_id(self) -> int:
            with self._id_lock:
                self._next_req_id += 1
                return self._next_req_id

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802, ANN001, ARG002
            try:
                rid = int(reqId)
            except (TypeError, ValueError):
                rid = -1
            self.errors.append((rid, int(errorCode), str(errorString)))
            if rid in self.contract_done:
                self.contract_done[rid].set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(int(reqId), []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(int(reqId), threading.Event()).set()

        def marketDataType(self, reqId, marketDataType):  # noqa: N802, ANN001
            with self.data_lock:
                self.l1.setdefault(int(reqId), L1State()).market_data_type = int(marketDataType)

        def tickPrice(self, reqId, tickType, price, attrib=None):  # noqa: N802, ANN001, ARG002
            with self.data_lock:
                apply_l1_price(self.l1.setdefault(int(reqId), L1State()), int(tickType), price)

        def tickSize(self, reqId, tickType, size):  # noqa: N802, ANN001
            with self.data_lock:
                apply_l1_size(self.l1.setdefault(int(reqId), L1State()), int(tickType), size)

        def updateMktDepth(self, reqId, position, operation, side, price, size):  # noqa: N802, ANN001, ARG002
            with self.data_lock:
                apply_depth_event(
                    self.depth.setdefault(int(reqId), DepthStats()),
                    position=int(position), side=int(side), size=size, is_l2=False,
                )

        def updateMktDepthL2(self, reqId, position, marketMaker, operation, side, price, size, isSmartDepth):  # noqa: N802, ANN001, ARG002
            with self.data_lock:
                apply_depth_event(
                    self.depth.setdefault(int(reqId), DepthStats()),
                    position=int(position), side=int(side), size=size, is_l2=True,
                )

    return App(), Contract


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
        errors = " | ".join(f"{code}: {msg}" for rid, code, msg in app.errors if rid == req_id)
        suffix = f"; {errors}" if errors else ""
        raise ValueError(
            f"contract resolution returned {len(rows)} matches for {underlying} {expiry} "
            f"{leg.option_type}{leg.strike:g} on {exchange}{suffix}"
        )
    return rows[0].contract


def _errors_for(app, *req_ids: int) -> list[tuple[int, str]]:
    wanted = set(req_ids)
    return [(code, msg) for rid, code, msg in app.errors if rid in wanted]


def run_probe(
    candidate: dict[str, Any],
    *,
    app,
    Contract,
    exchange: str,
    currency: str,
    timeout: float,
    duration_sec: float,
    depth_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:  # noqa: N803, ANN001
    legs = parse_legs_json(candidate.get("legs_json"))
    underlying = _text(candidate.get("underlying"))
    expiry = _text(candidate.get("expiry"))
    candidate_id = _text(candidate.get("candidate_id"))

    unique_legs: dict[tuple[str, float], Leg] = {}
    for leg in legs:
        unique_legs.setdefault(leg.key, leg)

    contracts: dict[tuple[str, float], Any] = {}
    for key, leg in unique_legs.items():
        contracts[key] = resolve_option_contract(
            app, Contract,
            underlying=underlying,
            expiry=expiry,
            leg=leg,
            exchange=exchange,
            currency=currency,
            timeout=timeout,
        )

    l1_req: dict[tuple[str, float], int] = {}
    depth_req: dict[tuple[str, float], int] = {}
    app.reqMarketDataType(1)
    try:
        for key, contract in contracts.items():
            rid = app.next_req_id()
            l1_req[key] = rid
            app.l1[rid] = L1State()
            app.reqMktData(rid, contract, "", False, False, [])

            did = app.next_req_id()
            depth_req[key] = did
            app.depth[did] = DepthStats()
            # isSmartDepth=False asks for direct-routed depth for the contract exchange.
            app.reqMktDepth(did, contract, int(depth_rows), False, [])

        time.sleep(duration_sec)
    finally:
        for rid in l1_req.values():
            try:
                app.cancelMktData(rid)
            except Exception:
                pass
        for rid in depth_req.values():
            try:
                app.cancelMktDepth(rid, False)
            except Exception:
                pass

    rows: list[dict[str, Any]] = []
    with app.data_lock:
        for key, leg in unique_legs.items():
            lrid = l1_req[key]
            drid = depth_req[key]
            l1 = app.l1.get(lrid, L1State())
            depth = app.depth.get(drid, DepthStats())
            errors = _errors_for(app, lrid, drid)
            status, reason = classify_leg(l1=l1, depth=depth, errors=errors)
            rows.append({
                "candidate_id": candidate_id,
                "underlying": underlying,
                "expiry": expiry,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "action": leg.action,
                "qty": leg.qty,
                "exchange": exchange,
                "currency": currency,
                "l1_req_id": lrid,
                "depth_req_id": drid,
                "actual_market_data_type": l1.market_data_type,
                "actual_market_data_type_name": market_data_type_name(l1.market_data_type),
                "bid": l1.bid,
                "ask": l1.ask,
                "bid_size": l1.bid_size,
                "ask_size": l1.ask_size,
                "bid_updates": l1.bid_updates,
                "ask_updates": l1.ask_updates,
                "size_updates": l1.size_updates,
                "depth_events": depth.events,
                "depth_bid_events": depth.bid_events,
                "depth_ask_events": depth.ask_events,
                "depth_positive_size_events": depth.positive_size_events,
                "depth_max_position_seen": depth.max_position_seen,
                "depth_l2_callbacks": depth.l2_callbacks,
                "depth_direct_callbacks": depth.direct_callbacks,
                "errors_json": json.dumps(
                    [{"code": code, "message": msg} for code, msg in errors],
                    ensure_ascii=False, separators=(",", ":"),
                ),
                "probe_status": status,
                "probe_reason": reason,
                "phase11_live_money_allowed": False,
                "phase11_atomicity_established": False,
            })

    return rows, summarize_probe(rows)


def _write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        p.write_text("", encoding="utf-8-sig")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase4_csv", help="Phase 4 enriched monitoring CSV containing underlying/expiry/legs_json")
    p.add_argument("--candidate-id", help="candidate_id to probe; defaults to first CSV row")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001, help="TWS/IB Gateway API port")
    p.add_argument("--client-id", type=int, default=71)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--duration", type=float, default=8.0)
    p.add_argument("--depth-rows", type=int, default=5)
    p.add_argument("--output", default="data/phase11_marketdata_probe.csv")
    p.add_argument("--summary-json", default="data/phase11_marketdata_probe_summary.json")
    args = p.parse_args()
    if args.port <= 0 or args.client_id < 0:
        p.error("--port must be positive and --client-id non-negative")
    if args.timeout <= 0 or args.duration <= 0:
        p.error("--timeout and --duration must be positive")
    if args.depth_rows <= 0:
        p.error("--depth-rows must be positive")
    return args


def main() -> int:
    args = _args()
    candidate = load_candidate(args.phase4_csv, args.candidate_id)
    app, Contract = make_probe_app()
    app.connect(args.host, args.port, clientId=args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(args.timeout):
        app.disconnect()
        raise SystemExit("IBKR API connection did not become ready before timeout")
    try:
        rows, summary = run_probe(
            candidate,
            app=app,
            Contract=Contract,
            exchange=args.exchange,
            currency=args.currency,
            timeout=args.timeout,
            duration_sec=args.duration,
            depth_rows=args.depth_rows,
        )
    finally:
        app.disconnect()
        thread.join(timeout=2.0)

    _write_csv(args.output, rows)
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
