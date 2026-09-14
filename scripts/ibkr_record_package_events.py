#!/usr/bin/env python3
"""Record event-driven IBKR quote callbacks for one Phase 17 candidate.

Read-only Phase 18 capture. It subscribes to every option leg concurrently and records
bid/ask, displayed size, and market-data-type callbacks with local monotonic receipt
timestamps. It never submits, modifies, or cancels orders; cancelMktData only stops
market-data subscriptions.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PHASE17_READY = "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"


@dataclass(frozen=True)
class Leg:
    action: str
    option_type: str
    strike: float
    qty: int

    @property
    def key(self) -> tuple[str, float]:
        return self.option_type, self.strike


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


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
        legs.append(Leg(action, option_type, strike, qty))
    return legs


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def find_candidate_row(rows: list[dict[str, Any]], candidate_id: str) -> dict[str, Any]:
    matches = [r for r in rows if _text(r.get("candidate_id")) == candidate_id]
    if not matches:
        raise ValueError(f"candidate_id {candidate_id!r} not found")
    return matches[0]


def ensure_phase17_ready(row: dict[str, Any]) -> None:
    decision = _text(row.get("phase17_candidate_decision"))
    if decision != PHASE17_READY:
        raise ValueError(
            f"candidate did not clear Phase 17: {decision or 'missing phase17_candidate_decision'}"
        )


def select_metadata_row(rows: list[dict[str, Any]], candidate_id: str) -> dict[str, Any]:
    required = {"underlying", "expiry", "legs_json", "floor_pv_per_share", "lot_size"}
    matches = [r for r in rows if _text(r.get("candidate_id")) == candidate_id]
    for row in matches:
        if required.issubset(row.keys()) and all(_text(row.get(k)) for k in required):
            return row
    raise ValueError(
        f"no Phase 4 row for {candidate_id!r} contains underlying/expiry/legs_json/floor_pv_per_share/lot_size"
    )


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


def make_app(session_origin_mono: float):
    EClient, EWrapper, Contract = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self._id_lock = threading.Lock()
            self._next_req_id = 50000
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.req_leg: dict[int, tuple[int, Leg]] = {}
            self.events: list[dict[str, Any]] = []
            self.event_lock = threading.Lock()
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802, ARG002
            self.ready.set()

        def next_req_id(self) -> int:
            with self._id_lock:
                self._next_req_id += 1
                return self._next_req_id

        def _append_event(self, req_id: int, kind: str, value: Any, tick_type: Optional[int] = None) -> None:
            now = time.monotonic()
            leg_info = self.req_leg.get(int(req_id))
            if leg_info is None:
                return
            leg_index, leg = leg_info
            row = {
                "elapsed_sec": now - session_origin_mono,
                "local_time_utc": datetime.now(timezone.utc).isoformat(),
                "req_id": int(req_id),
                "leg_index": leg_index,
                "action": leg.action,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "qty": leg.qty,
                "event_kind": kind,
                "tick_type": tick_type if tick_type is not None else "",
                "value": value,
            }
            with self.event_lock:
                self.events.append(row)

        def add_marker(self, kind: str) -> None:
            now = time.monotonic()
            with self.event_lock:
                self.events.append({
                    "elapsed_sec": now - session_origin_mono,
                    "local_time_utc": datetime.now(timezone.utc).isoformat(),
                    "req_id": "",
                    "leg_index": "",
                    "action": "",
                    "option_type": "",
                    "strike": "",
                    "qty": "",
                    "event_kind": kind,
                    "tick_type": "",
                    "value": "",
                })

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802, ANN001
            self.errors.append((int(reqId), int(errorCode), str(errorString)))
            if reqId in self.contract_done:
                self.contract_done[reqId].set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(int(reqId), []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(int(reqId), threading.Event()).set()

        def marketDataType(self, reqId, marketDataType):  # noqa: N802, ANN001
            self._append_event(int(reqId), "MARKET_DATA_TYPE", int(marketDataType))

        def tickPrice(self, reqId, tickType, price, attrib=None):  # noqa: N802, ANN001, ARG002
            kind = {1: "BID_PRICE", 2: "ASK_PRICE", 66: "BID_PRICE", 67: "ASK_PRICE"}.get(int(tickType))
            if kind is not None:
                self._append_event(int(reqId), kind, float(price), int(tickType))

        def tickSize(self, reqId, tickType, size):  # noqa: N802, ANN001
            kind = {0: "BID_SIZE", 3: "ASK_SIZE", 69: "BID_SIZE", 70: "ASK_SIZE"}.get(int(tickType))
            if kind is not None:
                self._append_event(int(reqId), kind, float(size), int(tickType))

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
        extra = f"; IBKR error: {errors}" if errors else ""
        raise ValueError(
            f"contract resolution returned {len(rows)} matches for {underlying} {expiry} "
            f"{leg.option_type}{leg.strike:g} on {exchange}{extra}"
        )
    return rows[0].contract


def write_events(path: str | Path, rows: list[dict[str, Any]], common: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "candidate_id", "session_id", "monitor_started_at_utc", "underlying", "expiry",
        "floor_pv_per_share", "lot_size", "fee_per_contract_leg", "requested_market_data_type",
        "elapsed_sec", "local_time_utc", "req_id", "leg_index", "action", "option_type",
        "strike", "qty", "event_kind", "tick_type", "value",
    ]
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**common, **row})


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase17_candidates", help="Phase 17 candidate summary CSV")
    p.add_argument("--phase4-input", required=True, help="enriched Phase 4 CSV containing candidate metadata")
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--summary-json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=48)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--market-data-type", choices=["live", "frozen", "delayed", "delayed-frozen"], default="live")
    args = p.parse_args()
    if args.timeout <= 0 or args.duration <= 0 or args.warmup < 0:
        p.error("--timeout and --duration must be positive; --warmup must be non-negative")
    return args


def main() -> int:
    args = _args()
    p17 = find_candidate_row(read_csv(args.phase17_candidates), args.candidate_id)
    ensure_phase17_ready(p17)
    meta = select_metadata_row(read_csv(args.phase4_input), args.candidate_id)
    legs = parse_legs_json(_text(meta["legs_json"]))
    fee = _finite(meta.get("effective_fee_per_contract_leg"))
    if fee is None:
        fee = _finite(meta.get("fee_per_contract_leg")) or 0.0
    md_types = {"live": 1, "frozen": 2, "delayed": 3, "delayed-frozen": 4}
    session_origin = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    session_id = f"{args.candidate_id}:{started_utc}"
    app, Contract = make_app(session_origin)
    app.connect(args.host, args.port, args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(args.timeout):
        app.disconnect()
        raise SystemExit("IBKR API connection did not become ready; check TWS/Gateway API settings")
    req_ids: list[int] = []
    try:
        contracts: list[Any] = []
        for leg in legs:
            contracts.append(resolve_option_contract(
                app, Contract,
                underlying=_text(meta["underlying"]), expiry=_text(meta["expiry"]), leg=leg,
                exchange=args.exchange, currency=args.currency, timeout=args.timeout,
            ))
        app.reqMarketDataType(md_types[args.market_data_type])
        for i, (leg, contract) in enumerate(zip(legs, contracts)):
            req_id = app.next_req_id()
            app.req_leg[req_id] = (i, leg)
            req_ids.append(req_id)
            app.reqMktData(req_id, contract, "", False, False, [])
        if args.warmup:
            time.sleep(args.warmup)
        app.add_marker("ANALYSIS_START")
        time.sleep(args.duration)
        app.add_marker("ANALYSIS_END")
    finally:
        for req_id in req_ids:
            try:
                app.cancelMktData(req_id)
            except Exception:
                pass
        app.disconnect()
    with app.event_lock:
        rows = sorted(app.events, key=lambda r: float(r["elapsed_sec"]))
    common = {
        "candidate_id": args.candidate_id,
        "session_id": session_id,
        "monitor_started_at_utc": started_utc,
        "underlying": _text(meta["underlying"]),
        "expiry": _text(meta["expiry"]),
        "floor_pv_per_share": float(meta["floor_pv_per_share"]),
        "lot_size": float(meta["lot_size"]),
        "fee_per_contract_leg": float(fee),
        "requested_market_data_type": md_types[args.market_data_type],
    }
    write_events(args.output, rows, common)
    summary = {
        **common,
        "event_rows": len(rows),
        "analysis_duration_sec": args.duration,
        "warmup_sec": args.warmup,
        "phase18_is_event_driven_local_observation": True,
        "phase18_is_exchange_time": False,
        "phase18_is_fill_probability": False,
        "phase18_live_money_allowed": False,
        "ibkr_errors": [{"req_id": rid, "code": code, "message": msg} for rid, code, msg in app.errors],
    }
    if args.summary_json:
        sp = Path(args.summary_json)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(rows)} event row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
