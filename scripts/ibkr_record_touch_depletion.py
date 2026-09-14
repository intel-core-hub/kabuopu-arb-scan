#!/usr/bin/env python3
"""Phase 13a: read-only recorder for OSE touch depletion vs Last/Last Size callbacks.

This intentionally does NOT estimate fills.  It records direct L2 top-of-book changes
alongside ordinary streaming L1 Last/Last Size callbacks from reqMktData, one option leg
at a time.  The trace can later be used to ask whether displayed touch depletion is
corroborated by nearby trade callbacks or is merely an unclassified book change.

No order API is used.  This recorder also deliberately avoids reqTickByTickData because
real-time option tick-by-tick availability is not assumed by this research pipeline.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Leg:
    action: str
    option_type: str
    strike: float
    qty: int

    @property
    def key(self) -> tuple[str, float]:
        return self.option_type, self.strike

    @property
    def key_text(self) -> str:
        return f"{self.option_type}:{self.strike:g}"


@dataclass
class BookLevel:
    price: float
    size: float
    market_maker: str = ""


@dataclass
class DepthBook:
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)

    def apply(
        self,
        *,
        position: int,
        operation: int,
        side: int,
        price: Any,
        size: Any,
        market_maker: str = "",
    ) -> bool:
        rows = self.bids if int(side) == 1 else self.asks if int(side) == 0 else None
        if rows is None or int(position) < 0:
            return False
        pos = int(position)
        op = int(operation)
        if op == 2:
            if pos >= len(rows):
                return False
            del rows[pos]
            return True
        px = _finite(price)
        sz = _finite(size)
        if px is None or sz is None or px < 0 or sz < 0:
            return False
        level = BookLevel(px, sz, str(market_maker or ""))
        if op == 0:
            if pos > len(rows):
                return False
            rows.insert(pos, level)
            return True
        if op == 1:
            if pos >= len(rows):
                return False
            rows[pos] = level
            return True
        return False

    def top(self) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        bid = self.bids[0] if self.bids else None
        ask = self.asks[0] if self.asks else None
        return (
            bid.price if bid else None,
            bid.size if bid else None,
            ask.price if ask else None,
            ask.size if ask else None,
        )


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_legs_json(value: Any) -> list[Leg]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid legs_json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs_json must be a non-empty JSON array")
    out: list[Leg] = []
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
        if strike is None or strike <= 0 or qty <= 0:
            raise ValueError(f"leg {i} has invalid strike/qty")
        out.append(Leg(action, option_type, float(strike), qty))
    return out


def load_candidate(path: str | Path, candidate_id: Optional[str]) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("Phase 4 CSV is empty")
    required = {"underlying", "expiry", "legs_json"}
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"input missing required columns: {', '.join(sorted(missing))}")
    if candidate_id is None:
        return rows[0]
    for row in rows:
        if _text(row.get("candidate_id")) == candidate_id:
            return row
    raise ValueError(f"candidate_id not found: {candidate_id}")


def require_phase12_ready(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    decision = _text(data.get("phase12_overall_decision"))
    expected = "TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH"
    if decision != expected:
        raise ValueError(
            f"Phase 12 is not ready for depletion research: {decision or 'missing decision'}"
        )
    return data


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError("IBKR Python API is not installed") from exc
    return EClient, EWrapper, Contract


def make_recorder_app():
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
            self.market_data_type: dict[int, int] = {}
            self.books: dict[int, DepthBook] = {}
            self.depth_meta: dict[int, dict[str, Any]] = {}
            self.l1_to_depth: dict[int, int] = {}
            self.last_price: dict[int, Optional[float]] = {}
            self.last_size: dict[int, Optional[float]] = {}
            self.events: list[dict[str, Any]] = []
            self.errors: list[tuple[int, int, str]] = []
            self.data_lock = threading.Lock()
            self.recording_start_mono: Optional[float] = None
            self.recording_enabled: set[int] = set()

        def nextValidId(self, orderId):  # noqa: N802, ANN001, ARG002
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
            event = self.contract_done.get(rid)
            if event is not None:
                event.set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(int(reqId), []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(int(reqId), threading.Event()).set()

        def marketDataType(self, reqId, marketDataType):  # noqa: N802, ANN001
            with self.data_lock:
                self.market_data_type[int(reqId)] = int(marketDataType)

        def _top_state(self, depth_id: int) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
            return self.books.setdefault(depth_id, DepthBook()).top()

        def _append_event(
            self,
            *,
            depth_id: int,
            event_kind: str,
            callback_tick_type: int = -1,
            position: int = -1,
            operation: int = -1,
            side: int = -1,
            event_price: Optional[float] = None,
            event_size: Optional[float] = None,
            market_maker: str = "",
        ) -> None:
            if depth_id not in self.recording_enabled:
                return
            now_mono = time.monotonic()
            meta = self.depth_meta.get(depth_id, {})
            l1_id = int(meta.get("l1_req_id", -1))
            bid, bid_size, ask, ask_size = self._top_state(depth_id)
            start = self.recording_start_mono or now_mono
            self.events.append(
                {
                    **meta,
                    "elapsed_ms": round((now_mono - start) * 1000.0, 3),
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "event_kind": event_kind,
                    "callback_tick_type": callback_tick_type,
                    "position": position,
                    "operation": operation,
                    "side": side,
                    "event_price": event_price,
                    "event_size": event_size,
                    "market_maker": market_maker,
                    "actual_market_data_type": self.market_data_type.get(l1_id),
                    "top_bid": bid,
                    "top_bid_size": bid_size,
                    "top_ask": ask,
                    "top_ask_size": ask_size,
                    "last_price": self.last_price.get(l1_id),
                    "last_size": self.last_size.get(l1_id),
                }
            )

        def _record_depth(
            self,
            req_id: int,
            position: int,
            operation: int,
            side: int,
            price: Any,
            size: Any,
            market_maker: str,
        ) -> None:
            with self.data_lock:
                book = self.books.setdefault(req_id, DepthBook())
                applied = book.apply(
                    position=int(position),
                    operation=int(operation),
                    side=int(side),
                    price=price,
                    size=size,
                    market_maker=market_maker,
                )
                if not applied:
                    return
                self._append_event(
                    depth_id=req_id,
                    event_kind="DEPTH",
                    position=int(position),
                    operation=int(operation),
                    side=int(side),
                    event_price=_finite(price),
                    event_size=_finite(size),
                    market_maker=str(market_maker or ""),
                )

        def updateMktDepth(self, reqId, position, operation, side, price, size):  # noqa: N802, ANN001
            self._record_depth(int(reqId), position, operation, side, price, size, "")

        def updateMktDepthL2(self, reqId, position, marketMaker, operation, side, price, size, isSmartDepth):  # noqa: N802, ANN001, ARG002
            self._record_depth(int(reqId), position, operation, side, price, size, str(marketMaker))

        def tickPrice(self, reqId, tickType, price, attrib):  # noqa: N802, ANN001, ARG002
            # 4=LAST; 68=DELAYED_LAST. Delayed callbacks are recorded for diagnostics,
            # but Phase 13 analysis only accepts actual_market_data_type == 1.
            if int(tickType) not in {4, 68}:
                return
            px = _finite(price)
            if px is None or px <= 0:
                return
            rid = int(reqId)
            with self.data_lock:
                self.last_price[rid] = px
                depth_id = self.l1_to_depth.get(rid)
                if depth_id is not None:
                    self._append_event(
                        depth_id=depth_id,
                        event_kind="LAST_PRICE",
                        callback_tick_type=int(tickType),
                        event_price=px,
                    )

        def tickSize(self, reqId, tickType, size):  # noqa: N802, ANN001
            # 5=LAST_SIZE; 71=DELAYED_LAST_SIZE.
            if int(tickType) not in {5, 71}:
                return
            sz = _finite(size)
            if sz is None or sz < 0:
                return
            rid = int(reqId)
            with self.data_lock:
                self.last_size[rid] = sz
                depth_id = self.l1_to_depth.get(rid)
                if depth_id is not None:
                    self._append_event(
                        depth_id=depth_id,
                        event_kind="LAST_SIZE",
                        callback_tick_type=int(tickType),
                        event_size=sz,
                    )

    return App(), Contract


def resolve_option_contract(
    app,
    Contract,
    *,
    underlying: str,
    expiry: str,
    leg: Leg,
    exchange: str,
    currency: str,
    timeout: float,
):  # noqa: N803, ANN001
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
        raise ValueError(
            f"contract resolution returned {len(rows)} matches for "
            f"{underlying} {expiry} {leg.option_type}{leg.strike:g}"
        )
    return rows[0].contract


def _baseline_event(app, depth_id: int, kind: str) -> None:
    with app.data_lock:
        meta = app.depth_meta.get(depth_id, {})
        l1_id = int(meta.get("l1_req_id", -1))
        bid, bid_size, ask, ask_size = app._top_state(depth_id)
        now_mono = time.monotonic()
        start = app.recording_start_mono or now_mono
        app.events.append(
            {
                **meta,
                "elapsed_ms": round((now_mono - start) * 1000.0, 3),
                "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "event_kind": kind,
                "callback_tick_type": -1,
                "position": -1,
                "operation": -1,
                "side": -1,
                "event_price": None,
                "event_size": None,
                "market_maker": "",
                "actual_market_data_type": app.market_data_type.get(l1_id),
                "top_bid": bid,
                "top_bid_size": bid_size,
                "top_ask": ask,
                "top_ask_size": ask_size,
                "last_price": app.last_price.get(l1_id),
                "last_size": app.last_size.get(l1_id),
            }
        )


def record_candidate(
    candidate: dict[str, Any],
    *,
    app,
    Contract,
    exchange: str,
    currency: str,
    timeout: float,
    warmup_sec: float,
    duration_sec: float,
    depth_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:  # noqa: N803, ANN001
    legs = parse_legs_json(candidate.get("legs_json"))
    unique: dict[tuple[str, float], Leg] = {}
    for leg in legs:
        unique.setdefault(leg.key, leg)
    underlying = _text(candidate.get("underlying"))
    expiry = _text(candidate.get("expiry"))
    candidate_id = _text(candidate.get("candidate_id"))
    app.reqMarketDataType(1)
    per_leg: list[dict[str, Any]] = []

    # Sequential subscriptions respect the smallest commonly documented L2 line cap.
    for leg in unique.values():
        contract = resolve_option_contract(
            app,
            Contract,
            underlying=underlying,
            expiry=expiry,
            leg=leg,
            exchange=exchange,
            currency=currency,
            timeout=timeout,
        )
        l1_id = app.next_req_id()
        depth_id = app.next_req_id()
        meta = {
            "candidate_id": candidate_id,
            "underlying": underlying,
            "expiry": expiry,
            "option_type": leg.option_type,
            "strike": leg.strike,
            "action": leg.action,
            "qty": leg.qty,
            "leg_key": leg.key_text,
            "exchange": exchange,
            "currency": currency,
            "l1_req_id": l1_id,
            "depth_req_id": depth_id,
        }
        with app.data_lock:
            app.books[depth_id] = DepthBook()
            app.depth_meta[depth_id] = meta
            app.l1_to_depth[l1_id] = depth_id
        app.reqMktData(l1_id, contract, "", False, False, [])
        app.reqMktDepth(depth_id, contract, int(depth_rows), False, [])
        time.sleep(warmup_sec)
        with app.data_lock:
            if app.recording_start_mono is None:
                app.recording_start_mono = time.monotonic()
            app.recording_enabled.add(depth_id)
        _baseline_event(app, depth_id, "BASELINE")
        time.sleep(duration_sec)
        with app.data_lock:
            app.recording_enabled.discard(depth_id)
        _baseline_event(app, depth_id, "TERMINAL")
        with app.data_lock:
            md_type = app.market_data_type.get(l1_id)
            leg_events = [e for e in app.events if e.get("depth_req_id") == depth_id]
            last_size_events = sum(1 for e in leg_events if e.get("event_kind") == "LAST_SIZE")
            depth_events = sum(1 for e in leg_events if e.get("event_kind") == "DEPTH")
        try:
            app.cancelMktDepth(depth_id, False)
        finally:
            app.cancelMktData(l1_id)
        per_leg.append(
            {
                "leg_key": leg.key_text,
                "actual_market_data_type": md_type,
                "recorded_events": len(leg_events),
                "depth_events": depth_events,
                "last_size_events": last_size_events,
                "live_l1_confirmed": md_type == 1,
            }
        )

    rows = list(app.events)
    all_live = bool(per_leg) and all(r["live_l1_confirmed"] for r in per_leg)
    all_have_depth = bool(per_leg) and all(int(r["depth_events"]) > 0 for r in per_leg)
    any_trade_callbacks = any(int(r["last_size_events"]) > 0 for r in per_leg)
    decision = (
        "TOUCH_DEPLETION_TRACE_READY"
        if all_live and all_have_depth
        else "TOUCH_DEPLETION_TRACE_INCOMPLETE"
    )
    summary = {
        "phase13_recording_decision": decision,
        "phase13_live_money_allowed": False,
        "phase13_is_fill_probability": False,
        "candidate_id": candidate_id,
        "leg_count": len(per_leg),
        "event_count": len(rows),
        "any_last_size_callbacks_observed": any_trade_callbacks,
        "per_leg": per_leg,
        "interpretation": (
            "Last/Last Size callback arrival near a depth reduction can corroborate turnover, but cannot prove "
            "that the reduction was entirely executed volume, reveal hidden liquidity, or establish fill probability."
        ),
    }
    return rows, summary


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
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
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase4_csv")
    p.add_argument("--phase12-summary", required=True)
    p.add_argument("--candidate-id")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4001)
    p.add_argument("--client-id", type=int, default=73)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--duration-per-leg", type=float, default=60.0)
    p.add_argument("--depth-rows", type=int, default=5)
    p.add_argument("--output", default="data/phase13_touch_depletion_events.csv")
    p.add_argument("--summary-json", default="data/phase13_recording_summary.json")
    args = p.parse_args()
    if args.port <= 0 or args.client_id < 0:
        p.error("invalid port/client-id")
    if args.timeout <= 0 or args.warmup < 0 or args.duration_per_leg <= 0 or args.depth_rows <= 0:
        p.error("timeout/duration/depth rows must be positive (warmup may be zero)")
    return args


def main() -> int:
    args = _args()
    require_phase12_ready(args.phase12_summary)
    candidate = load_candidate(args.phase4_csv, args.candidate_id)
    app, Contract = make_recorder_app()
    app.connect(args.host, args.port, clientId=args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.ready.wait(args.timeout):
        app.disconnect()
        raise SystemExit("IBKR API connection did not become ready before timeout")
    try:
        rows, summary = record_candidate(
            candidate,
            app=app,
            Contract=Contract,
            exchange=args.exchange,
            currency=args.currency,
            timeout=args.timeout,
            warmup_sec=args.warmup,
            duration_sec=args.duration_per_leg,
            depth_rows=args.depth_rows,
        )
    finally:
        app.disconnect()
        thread.join(timeout=2.0)
    write_csv(args.output, rows)
    out = Path(args.summary_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
