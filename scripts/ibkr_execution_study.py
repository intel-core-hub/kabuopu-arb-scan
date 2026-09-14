#!/usr/bin/env python3
"""Phase 6 execution-feasibility study for promoted kabu-opu candidates.

This tool never submits a live order.  By default it is fully offline and only
builds an execution-study plan from the Phase 5 ranking.  With ``--run-whatif``
it may connect to TWS / IB Gateway and send IBKR *What-If* orders.  IBKR documents
What-If as a preview/credit-check request: the order is not routed to a market.

The goal is deliberately narrower than execution: determine whether IBKR accepts
an option BAG preview for the candidate and, when it does, record estimated
commission and margin impact.  Acceptance does *not* establish atomic execution,
especially for OSE securities options where JPX currently lists Strategy Trades
as unavailable.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


PROMOTED = "PROMOTE_TO_EXECUTION_STUDY"
PLAN_ONLY = "PLAN_ONLY"
BAG_PREVIEW_ACCEPTED = "BAG_PREVIEW_ACCEPTED"
BAG_PREVIEW_REJECTED = "BAG_PREVIEW_REJECTED"
BAG_PREVIEW_TIMEOUT = "BAG_PREVIEW_TIMEOUT"


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
class WhatIfResult:
    status: str
    order_state: Optional[Any]
    errors: list[tuple[int, int, str]]


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or abs(x) >= 1e100:
        return None
    return x


def _float_text(value: Any) -> Optional[float]:
    """Parse TWS OrderState numeric strings conservatively."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        # Margin strings returned by TWS are normally plain numerics.  Avoid
        # guessing when a currency/unit suffix is present.
        value = text
    return _finite(value)


def _canon(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, (int, float)) and _finite(value) is not None:
        x = float(value)
        return str(int(x)) if x.is_integer() else format(x, ".15g")
    return str(value).strip()




def candidate_signature(row: pd.Series | dict[str, Any]) -> str:
    parts = [
        _canon(row.get("underlying", "")),
        _canon(row.get("expiry", "")),
        _canon(row.get("check", "")),
        _canon(row.get("strikes", "")),
        _canon(row.get("legs_json", "")),
    ]
    return "|".join(parts)


def candidate_id(row: pd.Series | dict[str, Any]) -> str:
    return hashlib.sha256(candidate_signature(row).encode("utf-8")).hexdigest()[:16]


def enrich_from_phase4(ranking: pd.DataFrame, inputs: list[str]) -> pd.DataFrame:
    """Fill Phase-6 pricing inputs from the Phase-4 files used by Phase 5.

    Phase 5 intentionally emits a compact aggregate and older rankings do not carry
    floor_pv_per_share / lot_size / fee_per_contract_leg.  The Phase-4 rows retain
    those original scanner fields, so Phase 6 can join them by the same stable
    candidate signature without changing historical Phase-5 output.
    """
    if not inputs or ranking.empty:
        return ranking.copy()
    paths: list[str] = []
    seen: set[str] = set()
    for value in inputs:
        matches = glob.glob(value)
        if not matches and Path(value).is_file():
            matches = [value]
        for match in matches:
            key = str(Path(match).resolve())
            if key not in seen:
                seen.add(key)
                paths.append(key)
    if not paths:
        raise ValueError("--phase4-inputs matched no files")

    fields = ["floor_pv_per_share", "lot_size", "fee_per_contract_leg"]
    lookup: dict[str, dict[str, Any]] = {}
    for path in paths:
        frame = pd.read_csv(path)
        required = {"underlying", "expiry", "check", "strikes", "legs_json"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{path}: missing Phase 4 identity columns: {', '.join(sorted(missing))}")
        for _, src in frame.iterrows():
            cid = candidate_id(src)
            values = lookup.setdefault(cid, {})
            for field in fields:
                if field not in frame.columns:
                    continue
                value = src.get(field)
                if field not in values and not (pd.isna(value) if not isinstance(value, (list, dict)) else False):
                    values[field] = value

    out = ranking.copy()
    if "candidate_id" not in out.columns:
        out["candidate_id"] = out.apply(candidate_id, axis=1)
    for field in fields:
        if field not in out.columns:
            out[field] = pd.NA
        for idx, row in out.iterrows():
            existing = row.get(field)
            if pd.notna(existing):
                continue
            value = lookup.get(str(row["candidate_id"]), {}).get(field)
            if value is not None:
                out.at[idx, field] = value
    return out


def parse_legs_json(value: str) -> list[Leg]:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid legs_json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs_json must be a non-empty JSON array")

    signed: dict[tuple[str, float], int] = {}
    order: list[tuple[str, float]] = []
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
        key = (option_type, float(strike))
        if key not in signed:
            order.append(key)
            signed[key] = 0
        signed[key] += qty if action == "BUY" else -qty

    legs: list[Leg] = []
    for key in order:
        qty_signed = signed[key]
        if qty_signed == 0:
            continue
        option_type, strike = key
        legs.append(
            Leg(
                action="BUY" if qty_signed > 0 else "SELL",
                option_type=option_type,
                strike=strike,
                qty=abs(qty_signed),
            )
        )
    if not legs:
        raise ValueError("legs_json nets to an empty package")
    return legs


def assumed_fee_per_package(legs: list[Leg], fee_per_contract_leg: Any) -> Optional[float]:
    fee = _finite(fee_per_contract_leg)
    if fee is None or fee < 0:
        return None
    return float(sum(leg.qty for leg in legs)) * fee


def infer_combo_limit_per_share(row: pd.Series | dict[str, Any], legs: list[Leg]) -> Optional[float]:
    """Infer the observed package debit from Phase 5 summary fields.

    Phase 4 edge definition is:
        net_edge = (floor_pv_per_share - debit_per_share) * lot - assumed_fees

    Therefore:
        debit_per_share = floor_pv_per_share - (net_edge + assumed_fees) / lot

    We use the median of per-session minimum live edge, i.e. a conservative
    reproducibility summary, not a fresh market price.
    """
    floor_pv = _finite(row.get("floor_pv_per_share"))
    lot = _finite(row.get("lot_size"))
    edge = _finite(row.get("median_min_live_edge_per_contract"))
    fees = assumed_fee_per_package(legs, row.get("fee_per_contract_leg"))
    if floor_pv is None or lot is None or lot <= 0 or edge is None or fees is None:
        return None
    return floor_pv - (edge + fees) / lot


def round_combo_limit(value: Optional[float], tick: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if tick is None:
        return float(value)
    t = _finite(tick)
    if t is None or t <= 0:
        raise ValueError("combo price tick must be positive")
    # Nearest tick for a *preview*.  This is not used to place an order.
    return round(float(value) / t) * t


def plan_candidate(
    row: pd.Series | dict[str, Any], *, combo_price_tick: Optional[float] = None
) -> dict[str, Any]:
    base = dict(row)
    if str(row.get("research_decision", "")) != PROMOTED:
        base.update(
            phase6_status="SKIPPED_NOT_PROMOTED",
            phase6_decision="NO_EXECUTION_STUDY",
            phase6_reason="candidate was not promoted by Phase 5",
        )
        return base

    legs = parse_legs_json(str(row.get("legs_json", "")))
    assumed_fees = assumed_fee_per_package(legs, row.get("fee_per_contract_leg"))
    inferred = infer_combo_limit_per_share(row, legs)
    rounded = round_combo_limit(inferred, combo_price_tick)
    base.update(
        phase6_status=PLAN_ONLY,
        phase6_decision="WHATIF_PREVIEW_REQUIRED",
        phase6_reason=(
            "Phase 5 promoted candidate; IBKR BAG What-If can test broker acceptance, "
            "commission and margin, but cannot prove atomic OSE execution"
        ),
        phase6_leg_count=len(legs),
        phase6_contract_legs=sum(leg.qty for leg in legs),
        phase6_assumed_fees_per_package=assumed_fees,
        phase6_inferred_combo_limit_per_share=inferred,
        phase6_combo_limit_per_share=rounded,
        phase6_parent_action="BUY",
        phase6_order_type="LMT",
        phase6_order_quantity=1,
        phase6_jpx_strategy_trades="unavailable",
        phase6_atomicity_established=False,
    )
    return base


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.contract import ComboLeg, Contract
        from ibapi.order import Order
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError(
            "IBKR Python API is not installed. Install requirements-ibkr.txt / official ibapi."
        ) from exc
    return EClient, EWrapper, Contract, ComboLeg, Order


def make_whatif_app():
    EClient, EWrapper, Contract, ComboLeg, Order = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self._lock = threading.Lock()
            self._next_req_id = 2000
            self._next_order_id: Optional[int] = None
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.whatif_done: dict[int, threading.Event] = {}
            self.whatif_state: dict[int, Any] = {}
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802
            with self._lock:
                self._next_order_id = int(orderId)
            self.ready.set()

        def next_req_id(self) -> int:
            with self._lock:
                self._next_req_id += 1
                return self._next_req_id

        def next_order_id(self) -> int:
            with self._lock:
                if self._next_order_id is None:
                    raise RuntimeError("IBKR nextValidId not received")
                oid = self._next_order_id
                self._next_order_id += 1
                return oid

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802, ANN001
            self.errors.append((int(reqId), int(errorCode), str(errorString)))
            if reqId in self.contract_done:
                self.contract_done[reqId].set()
            # For What-If we intentionally do not mark completion on error: a
            # warning can precede openOrder/orderState.  The caller waits for the
            # state until timeout and then includes all order-specific errors.

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            self.contract_rows.setdefault(reqId, []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(reqId, threading.Event()).set()

        def openOrder(self, orderId, contract, order, orderState):  # noqa: N802, ANN001
            if orderId in self.whatif_done:
                self.whatif_state[orderId] = orderState
                self.whatif_done[orderId].set()

    return App(), Contract, ComboLeg, Order


def _errors_for(app: Any, req_id: int) -> list[tuple[int, int, str]]:
    return [(rid, code, msg) for rid, code, msg in getattr(app, "errors", []) if rid == req_id]


def resolve_option_contract(
    app: Any,
    Contract: Any,
    *,
    underlying: str,
    expiry: str,
    leg: Leg,
    exchange: str,
    currency: str,
    timeout: float,
):
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
        errs = _errors_for(app, req_id)
        suffix = " | ".join(f"{code}: {msg}" for _, code, msg in errs)
        if suffix:
            suffix = f"; IBKR error: {suffix}"
        raise ValueError(
            f"contract resolution returned {len(rows)} matches for {underlying} {expiry} "
            f"{leg.option_type}{leg.strike:g} on {exchange}{suffix}"
        )
    return rows[0].contract


def build_bag_contract(
    Contract: Any,
    ComboLeg: Any,
    *,
    underlying: str,
    legs: list[Leg],
    contracts: dict[tuple[str, float], Any],
    exchange: str,
    currency: str,
):
    bag = Contract()
    bag.symbol = str(underlying)
    bag.secType = "BAG"
    bag.exchange = exchange
    bag.currency = currency
    bag.comboLegs = []
    for leg in legs:
        resolved = contracts[leg.key]
        combo_leg = ComboLeg()
        combo_leg.conId = int(resolved.conId)
        combo_leg.ratio = int(leg.qty)
        combo_leg.action = leg.action
        combo_leg.exchange = exchange
        combo_leg.openClose = 0
        bag.comboLegs.append(combo_leg)
    return bag


def build_whatif_order(
    Order: Any,
    *,
    limit_price: float,
    account: Optional[str],
    order_ref: str,
):
    if _finite(limit_price) is None:
        raise ValueError("What-If requires a finite combo limit price")
    order = Order()
    order.action = "BUY"
    order.orderType = "LMT"
    order.totalQuantity = 1
    order.lmtPrice = float(limit_price)
    order.tif = "DAY"
    order.whatIf = True
    order.orderRef = order_ref
    if account:
        order.account = account
    # Critical invariant: this script has no live-order mode.
    if getattr(order, "whatIf", None) is not True:
        raise RuntimeError("refusing to call placeOrder without whatIf=True")
    return order


def run_whatif_preview(
    app: Any,
    *,
    bag: Any,
    order: Any,
    timeout: float,
) -> WhatIfResult:
    if getattr(order, "whatIf", None) is not True:
        raise RuntimeError("refusing to call placeOrder without whatIf=True")
    order_id = app.next_order_id()
    event = threading.Event()
    app.whatif_done[order_id] = event
    app.placeOrder(order_id, bag, order)
    completed = event.wait(timeout)
    state = app.whatif_state.get(order_id)
    errors = _errors_for(app, order_id)
    if state is not None:
        status = BAG_PREVIEW_ACCEPTED
    elif completed:
        status = BAG_PREVIEW_REJECTED
    elif errors:
        status = BAG_PREVIEW_REJECTED
    else:
        status = BAG_PREVIEW_TIMEOUT
    return WhatIfResult(status=status, order_state=state, errors=errors)


def _state_dict(state: Optional[Any]) -> dict[str, Any]:
    if state is None:
        return {
            "whatif_state_status": None,
            "whatif_init_margin_before": None,
            "whatif_init_margin_change": None,
            "whatif_init_margin_after": None,
            "whatif_maint_margin_before": None,
            "whatif_maint_margin_change": None,
            "whatif_maint_margin_after": None,
            "whatif_equity_with_loan_before": None,
            "whatif_equity_with_loan_change": None,
            "whatif_equity_with_loan_after": None,
            "whatif_commission": None,
            "whatif_min_commission": None,
            "whatif_max_commission": None,
            "whatif_commission_currency": None,
            "whatif_warning_text": None,
        }
    return {
        "whatif_state_status": getattr(state, "status", None),
        "whatif_init_margin_before": _float_text(getattr(state, "initMarginBefore", None)),
        "whatif_init_margin_change": _float_text(getattr(state, "initMarginChange", None)),
        "whatif_init_margin_after": _float_text(getattr(state, "initMarginAfter", None)),
        "whatif_maint_margin_before": _float_text(getattr(state, "maintMarginBefore", None)),
        "whatif_maint_margin_change": _float_text(getattr(state, "maintMarginChange", None)),
        "whatif_maint_margin_after": _float_text(getattr(state, "maintMarginAfter", None)),
        "whatif_equity_with_loan_before": _float_text(getattr(state, "equityWithLoanBefore", None)),
        "whatif_equity_with_loan_change": _float_text(getattr(state, "equityWithLoanChange", None)),
        "whatif_equity_with_loan_after": _float_text(getattr(state, "equityWithLoanAfter", None)),
        "whatif_commission": _finite(getattr(state, "commission", None)),
        "whatif_min_commission": _finite(getattr(state, "minCommission", None)),
        "whatif_max_commission": _finite(getattr(state, "maxCommission", None)),
        "whatif_commission_currency": getattr(state, "commissionCurrency", None),
        "whatif_warning_text": getattr(state, "warningText", None),
    }


def assess_preview(
    plan: dict[str, Any], result: WhatIfResult, *, currency: str
) -> dict[str, Any]:
    out = dict(plan)
    out["phase6_status"] = result.status
    out["whatif_errors_json"] = json.dumps(
        [{"req_id": rid, "code": code, "message": msg} for rid, code, msg in result.errors],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    out.update(_state_dict(result.order_state))

    if result.status == BAG_PREVIEW_REJECTED:
        out["phase6_decision"] = "STOP_COMBO_UNSUPPORTED_OR_REJECTED"
        out["phase6_reason"] = "IBKR did not return an accepted BAG What-If preview"
        return out
    if result.status == BAG_PREVIEW_TIMEOUT:
        out["phase6_decision"] = "REVIEW_WHATIF_TIMEOUT"
        out["phase6_reason"] = "no What-If orderState was received before timeout"
        return out

    commission = _finite(out.get("whatif_commission"))
    commission_ccy = _canon(out.get("whatif_commission_currency")).upper()
    observed_net = _finite(out.get("median_min_live_edge_per_contract"))
    assumed = _finite(out.get("phase6_assumed_fees_per_package"))
    adjusted: Optional[float] = None
    if (
        commission is not None
        and observed_net is not None
        and assumed is not None
        and commission_ccy == str(currency).upper()
    ):
        adjusted = observed_net + assumed - commission
    out["phase6_edge_after_whatif_commission"] = adjusted

    init_change = _finite(out.get("whatif_init_margin_change"))
    if adjusted is not None and init_change is not None and init_change > 0:
        out["phase6_edge_to_initial_margin"] = adjusted / init_change
    else:
        out["phase6_edge_to_initial_margin"] = None

    warning = _canon(out.get("whatif_warning_text"))
    if commission is None:
        decision = "REVIEW_WHATIF_INCOMPLETE"
        reason = "BAG preview returned but commission estimate is unavailable"
    elif commission_ccy != str(currency).upper():
        decision = "REVIEW_COMMISSION_CURRENCY"
        reason = f"commission currency={commission_ccy or 'unknown'}; expected {currency}"
    elif adjusted is None:
        decision = "REVIEW_WHATIF_INCOMPLETE"
        reason = "could not reconcile Phase 5 fee assumption with What-If commission"
    elif adjusted <= 0:
        decision = "STOP_EDGE_AFTER_COMMISSION_NONPOSITIVE"
        reason = f"edge after What-If commission={adjusted:.6g}"
    elif warning:
        decision = "REVIEW_WHATIF_WARNINGS"
        reason = f"positive edge remains but IBKR warning requires review: {warning}"
    else:
        decision = "PAPER_COMBO_TEST_REQUIRED"
        reason = (
            "BAG What-If accepted and estimated commission does not remove the observed edge; "
            "atomic OSE execution is still not established"
        )
    out["phase6_decision"] = decision
    out["phase6_reason"] = reason
    out["phase6_atomicity_established"] = False
    return out


def study_candidate(
    row: pd.Series,
    *,
    app: Any,
    Contract: Any,
    ComboLeg: Any,
    Order: Any,
    exchange: str,
    currency: str,
    timeout: float,
    account: Optional[str],
    combo_price_tick: Optional[float],
) -> dict[str, Any]:
    plan = plan_candidate(row, combo_price_tick=combo_price_tick)
    if plan.get("phase6_status") != PLAN_ONLY:
        return plan
    limit_price = _finite(plan.get("phase6_combo_limit_per_share"))
    if limit_price is None:
        plan["phase6_status"] = "WHATIF_NOT_RUN_MISSING_LIMIT_INPUTS"
        plan["phase6_decision"] = "REVIEW_INPUTS"
        plan["phase6_reason"] = (
            "need floor_pv_per_share, lot_size, fee_per_contract_leg and "
            "median_min_live_edge_per_contract to infer a BAG preview price"
        )
        return plan

    legs = parse_legs_json(str(row["legs_json"]))
    contracts: dict[tuple[str, float], Any] = {}
    for leg in legs:
        contracts[leg.key] = resolve_option_contract(
            app,
            Contract,
            underlying=str(row["underlying"]),
            expiry=str(row["expiry"]),
            leg=leg,
            exchange=exchange,
            currency=currency,
            timeout=timeout,
        )
    bag = build_bag_contract(
        Contract,
        ComboLeg,
        underlying=str(row["underlying"]),
        legs=legs,
        contracts=contracts,
        exchange=exchange,
        currency=currency,
    )
    order = build_whatif_order(
        Order,
        limit_price=limit_price,
        account=account,
        order_ref=f"kabuopu-phase6-{_canon(row.get('candidate_id')) or 'candidate'}",
    )
    result = run_whatif_preview(app, bag=bag, order=order, timeout=timeout)
    return assess_preview(plan, result, currency=currency)


def _validate_input(df: pd.DataFrame) -> None:
    required = {
        "underlying",
        "expiry",
        "legs_json",
        "research_decision",
        "median_min_live_edge_per_contract",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Phase 5 ranking missing required columns: {', '.join(sorted(missing))}")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Phase 5 reproducibility_ranking.csv")
    p.add_argument("--output", default="data/execution_study.csv")
    p.add_argument(
        "--phase4-inputs",
        nargs="*",
        default=[],
        help="Phase 4 persistence CSV paths/globs used to enrich pricing fields missing from Phase 5",
    )
    p.add_argument("--limit", type=int, default=10, help="maximum promoted rows to study")
    p.add_argument(
        "--run-whatif",
        action="store_true",
        help="connect to IBKR and run non-routing What-If previews; default is offline plan-only",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=61)
    p.add_argument("--account", default=None)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument(
        "--combo-price-tick",
        type=float,
        default=None,
        help="optional preview-price rounding tick; omit to preserve inferred price",
    )
    args = p.parse_args()
    if args.limit < 1:
        p.error("--limit must be positive")
    if args.timeout <= 0:
        p.error("--timeout must be positive")
    if args.combo_price_tick is not None and args.combo_price_tick <= 0:
        p.error("--combo-price-tick must be positive")
    return args


def main() -> int:
    args = _args()
    df = pd.read_csv(args.input)
    _validate_input(df)
    df = enrich_from_phase4(df, args.phase4_inputs)

    # Work only on promoted rows.  Keep non-promoted rows out of the result so the
    # Phase 6 file remains a compact execution-study handoff.
    work = df[df["research_decision"].astype(str) == PROMOTED].head(args.limit).copy()
    if work.empty:
        out = pd.DataFrame(columns=list(df.columns) + ["phase6_status", "phase6_decision", "phase6_reason"])
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"no {PROMOTED} candidates; wrote empty study to {out_path}")
        return 0

    rows: list[dict[str, Any]] = []
    app = Contract = ComboLeg = Order = None
    thread = None
    if args.run_whatif:
        app, Contract, ComboLeg, Order = make_whatif_app()
        app.connect(args.host, args.port, args.client_id)
        thread = threading.Thread(target=app.run, daemon=True)
        thread.start()
        if not app.ready.wait(args.timeout):
            app.disconnect()
            raise SystemExit("IBKR connection established but nextValidId was not received")

    try:
        for _, row in work.iterrows():
            try:
                if not args.run_whatif:
                    rows.append(plan_candidate(row, combo_price_tick=args.combo_price_tick))
                else:
                    rows.append(
                        study_candidate(
                            row,
                            app=app,
                            Contract=Contract,
                            ComboLeg=ComboLeg,
                            Order=Order,
                            exchange=args.exchange,
                            currency=args.currency,
                            timeout=args.timeout,
                            account=args.account,
                            combo_price_tick=args.combo_price_tick,
                        )
                    )
            except Exception as exc:  # keep other promoted candidates inspectable
                base = row.to_dict()
                base.update(
                    phase6_status="ERROR",
                    phase6_decision="REVIEW_ERROR",
                    phase6_reason=str(exc),
                    phase6_atomicity_established=False,
                )
                rows.append(base)
    finally:
        if app is not None:
            app.disconnect()
        if thread is not None:
            thread.join(timeout=1.0)

    out = pd.DataFrame(rows)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"wrote {len(out)} Phase 6 row(s) to {out_path}")
    if "phase6_decision" in out.columns:
        print(out["phase6_decision"].value_counts(dropna=False).to_string())
    if args.run_whatif:
        print("What-If only: this script has no live-order mode and does not establish atomic OSE execution.")
    else:
        print("plan-only mode: no IBKR connection and no placeOrder call was made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
