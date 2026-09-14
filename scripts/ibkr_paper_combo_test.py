#!/usr/bin/env python3
"""Phase 7 paper-account-only combo mechanics experiment.

Default mode is offline and only prepares one promoted Phase 6 candidate for a
paper-account test.  ``--run-paper`` is intentionally guarded: the selected
account must be returned by IBKR's managedAccounts callback, must start with
``DU``, and an exact acknowledgement phrase is required.  The script submits at
most one 1-package LMT BAG order, records order/execution callbacks, optionally
modifies the limit once, and then cancels any remaining quantity.

This is a simulator experiment, not a live execution engine.  Paper fills do not
establish live OSE atomicity or production fill quality.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:  # script execution: scripts/ is on sys.path
    from ibkr_execution_study import (
        _canon,
        _finite,
        build_bag_contract,
        parse_legs_json,
        resolve_option_contract,
    )
except ImportError:  # test/module execution from repository root
    from scripts.ibkr_execution_study import (  # type: ignore
        _canon,
        _finite,
        build_bag_contract,
        parse_legs_json,
        resolve_option_contract,
    )


PHASE6_READY = "PAPER_COMBO_TEST_REQUIRED"
PAPER_ACK = "I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER"
PLAN_ONLY = "PLAN_ONLY"
RUN_COMPLETE = "PAPER_TEST_COMPLETE"

TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
ACTIVE_STATUSES = {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}


@dataclass
class StatusEvent:
    ts_utc: str
    status: str
    filled: Optional[float]
    remaining: Optional[float]
    avg_fill_price: Optional[float]
    last_fill_price: Optional[float]
    perm_id: Optional[int]
    why_held: str


@dataclass
class ExecutionEvent:
    ts_utc: str
    exec_id: str
    order_id: Optional[int]
    perm_id: Optional[int]
    sec_type: str
    con_id: Optional[int]
    exchange: str
    side: str
    shares: Optional[float]
    price: Optional[float]
    cum_qty: Optional[float]
    avg_price: Optional[float]
    order_ref: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def is_paper_account_code(account: str) -> bool:
    """Conservative local guard used in addition to IBKR managedAccounts.

    IBKR documentation/examples commonly use DU-prefixed paper account IDs.  This
    is a client-side safety heuristic, not a broker-side attestation that a TWS
    session is paper.
    """

    return str(account).strip().upper().startswith("DU")


def assert_paper_account(account: Optional[str], managed_accounts: list[str]) -> str:
    if not account:
        raise ValueError("--account is required for --run-paper")
    selected = str(account).strip().upper()
    managed = {str(x).strip().upper() for x in managed_accounts if str(x).strip()}
    if selected not in managed:
        raise RuntimeError(
            f"refusing paper submit: account {selected!r} was not returned by managedAccounts"
        )
    if not is_paper_account_code(selected):
        raise RuntimeError(
            f"refusing paper submit: account {selected!r} does not satisfy DU paper-account guard"
        )
    return selected


def _jsonable_error(req_id: int, code: int, message: str) -> dict[str, Any]:
    return {"req_id": int(req_id), "code": int(code), "message": str(message)}


def select_paper_limit(plan: dict[str, Any], override: Optional[float]) -> tuple[float, str]:
    if override is not None:
        value = _finite(override)
        if value is None:
            raise ValueError("--paper-limit-price must be finite")
        return float(value), "cli_override"
    value = _finite(plan.get("phase7_limit_price"))
    if value is None:
        raise ValueError("Phase 6 reference combo limit is unavailable")
    return float(value), "phase6_reference"


def plan_candidate(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    base = dict(row)
    if str(row.get("phase6_decision", "")) != PHASE6_READY:
        base.update(
            phase7_status="SKIPPED_NOT_PHASE6_READY",
            phase7_decision="NO_PAPER_COMBO_TEST",
            phase7_reason=f"Phase 6 decision is not {PHASE6_READY}",
        )
        return base

    limit_price = _finite(row.get("phase6_combo_limit_per_share"))
    if limit_price is None:
        base.update(
            phase7_status="MISSING_LIMIT_PRICE",
            phase7_decision="REVIEW_PHASE6_INPUT",
            phase7_reason="phase6_combo_limit_per_share is missing/non-finite",
        )
        return base

    legs = parse_legs_json(str(row.get("legs_json", "")))
    base.update(
        phase7_status=PLAN_ONLY,
        phase7_decision="PAPER_ORDER_EXPERIMENT_REQUIRED",
        phase7_reason=(
            "eligible for a one-package paper-account BAG mechanics experiment; "
            "paper behavior cannot establish live OSE atomicity"
        ),
        phase7_order_action="BUY",
        phase7_order_type="LMT",
        phase7_order_quantity=1,
        phase7_limit_price=float(limit_price),
        phase7_leg_count=len(legs),
        phase7_contract_legs=sum(leg.qty for leg in legs),
        phase7_live_money_allowed=False,
        phase7_atomicity_established=False,
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


def make_paper_app():
    EClient, EWrapper, Contract, ComboLeg, Order = _load_ibapi()

    class App(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready = threading.Event()
            self.accounts_ready = threading.Event()
            self._lock = threading.RLock()
            self._next_req_id = 7000
            self._next_order_id: Optional[int] = None
            self.managed_accounts: list[str] = []
            self.contract_rows: dict[int, list[Any]] = {}
            self.contract_done: dict[int, threading.Event] = {}
            self.status_events: dict[int, list[StatusEvent]] = {}
            self.execution_events: dict[int, list[ExecutionEvent]] = {}
            self.order_contract_sec_type: dict[int, str] = {}
            self.order_open_seen: set[int] = set()
            self.errors: list[tuple[int, int, str]] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802
            with self._lock:
                self._next_order_id = int(orderId)
            self.ready.set()

        def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
            accounts = [x.strip() for x in str(accountsList).split(",") if x.strip()]
            with self._lock:
                self.managed_accounts = accounts
            self.accounts_ready.set()

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

        def error(self, reqId, *args):  # noqa: N802, ANN001
            # Old API: error(reqId, code, msg, advancedJson="")
            # Newer API variants may add errorTime before code.
            code: int
            msg: str
            if len(args) >= 3 and isinstance(args[0], (int, float)) and isinstance(args[1], int):
                code = int(args[1])
                msg = str(args[2])
            elif len(args) >= 2:
                code = int(args[0])
                msg = str(args[1])
            else:
                code = -1
                msg = " | ".join(map(str, args))
            with self._lock:
                self.errors.append((int(reqId), code, msg))
            event = self.contract_done.get(int(reqId))
            if event is not None:
                event.set()

        def contractDetails(self, reqId, contractDetails):  # noqa: N802, ANN001
            with self._lock:
                self.contract_rows.setdefault(int(reqId), []).append(contractDetails)

        def contractDetailsEnd(self, reqId):  # noqa: N802, ANN001
            self.contract_done.setdefault(int(reqId), threading.Event()).set()

        def openOrder(self, orderId, contract, order, orderState):  # noqa: N802, ANN001
            with self._lock:
                self.order_open_seen.add(int(orderId))
                self.order_contract_sec_type[int(orderId)] = str(getattr(contract, "secType", ""))

        def orderStatus(  # noqa: N802, ANN001
            self,
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice=0.0,
        ):
            event = StatusEvent(
                ts_utc=utc_now(),
                status=str(status),
                filled=_finite(filled),
                remaining=_finite(remaining),
                avg_fill_price=_finite(avgFillPrice),
                last_fill_price=_finite(lastFillPrice),
                perm_id=int(permId) if permId is not None else None,
                why_held=str(whyHeld or ""),
            )
            with self._lock:
                self.status_events.setdefault(int(orderId), []).append(event)

        def execDetails(self, reqId, contract, execution):  # noqa: N802, ANN001
            oid_raw = getattr(execution, "orderId", None)
            order_id = int(oid_raw) if oid_raw is not None else None
            event = ExecutionEvent(
                ts_utc=utc_now(),
                exec_id=str(getattr(execution, "execId", "")),
                order_id=order_id,
                perm_id=(int(getattr(execution, "permId")) if getattr(execution, "permId", None) is not None else None),
                sec_type=str(getattr(contract, "secType", "")),
                con_id=(int(getattr(contract, "conId")) if getattr(contract, "conId", None) is not None else None),
                exchange=str(getattr(execution, "exchange", "") or getattr(contract, "exchange", "")),
                side=str(getattr(execution, "side", "")),
                shares=_finite(getattr(execution, "shares", None)),
                price=_finite(getattr(execution, "price", None)),
                cum_qty=_finite(getattr(execution, "cumQty", None)),
                avg_price=_finite(getattr(execution, "avgPrice", None)),
                order_ref=str(getattr(execution, "orderRef", "")),
            )
            if order_id is not None:
                with self._lock:
                    self.execution_events.setdefault(order_id, []).append(event)

    return App(), Contract, ComboLeg, Order


def build_paper_order(
    Order: Any,
    *,
    account: str,
    limit_price: float,
    order_ref: str,
):
    selected = str(account).strip().upper()
    if not is_paper_account_code(selected):
        raise RuntimeError("refusing to build transmissible order for non-DU account")
    price = _finite(limit_price)
    if price is None:
        raise ValueError("paper test requires a finite combo limit price")
    order = Order()
    order.action = "BUY"
    order.orderType = "LMT"
    order.totalQuantity = 1
    order.lmtPrice = float(price)
    order.tif = "DAY"
    order.whatIf = False
    order.transmit = True
    order.account = selected
    order.orderRef = str(order_ref)
    return order


def _assert_submit_invariants(order: Any, account: str, managed_accounts: list[str]) -> None:
    selected = assert_paper_account(account, managed_accounts)
    if str(getattr(order, "account", "")).strip().upper() != selected:
        raise RuntimeError("refusing submit: order.account does not match guarded paper account")
    if getattr(order, "whatIf", None) is not False:
        raise RuntimeError("refusing Phase 7 submit: expected whatIf=False paper order")
    if getattr(order, "transmit", None) is not True:
        raise RuntimeError("refusing Phase 7 submit: expected transmit=True")
    if str(getattr(order, "action", "")).upper() != "BUY":
        raise RuntimeError("refusing Phase 7 submit: parent action must be BUY")
    if str(getattr(order, "orderType", "")).upper() != "LMT":
        raise RuntimeError("refusing Phase 7 submit: order type must be LMT")
    qty = _finite(getattr(order, "totalQuantity", None))
    if qty != 1.0:
        raise RuntimeError("refusing Phase 7 submit: quantity must be exactly one package")
    if not str(getattr(order, "orderRef", "")).startswith("kabuopu-phase7-paper-"):
        raise RuntimeError("refusing Phase 7 submit: missing Phase 7 paper orderRef guard")


def submit_paper_order(app: Any, *, order_id: int, bag: Any, order: Any, account: str) -> None:
    """The only real placeOrder submission path in Phase 7.

    It is deliberately guarded again immediately before the API call.
    """

    _assert_submit_invariants(order, account, list(getattr(app, "managed_accounts", [])))
    app.placeOrder(int(order_id), bag, order)


def modify_paper_order(
    app: Any,
    *,
    order_id: int,
    bag: Any,
    order: Any,
    account: str,
    new_limit_price: float,
) -> None:
    _assert_submit_invariants(order, account, list(getattr(app, "managed_accounts", [])))
    price = _finite(new_limit_price)
    if price is None:
        raise ValueError("replacement limit must be finite")
    order.lmtPrice = float(price)
    _assert_submit_invariants(order, account, list(getattr(app, "managed_accounts", [])))
    app.placeOrder(int(order_id), bag, order)


def cancel_own_order(app: Any, order_id: int) -> None:
    # Never use reqGlobalCancel here: it could affect unrelated paper orders.
    app.cancelOrder(int(order_id), "")


def _snapshot_events(app: Any, order_id: int) -> tuple[list[StatusEvent], list[ExecutionEvent], list[tuple[int, int, str]]]:
    lock = getattr(app, "_lock", None)
    if lock is None:
        statuses = list(getattr(app, "status_events", {}).get(order_id, []))
        executions = list(getattr(app, "execution_events", {}).get(order_id, []))
        errors = [x for x in getattr(app, "errors", []) if int(x[0]) == order_id]
        return statuses, executions, errors
    with lock:
        statuses = list(getattr(app, "status_events", {}).get(order_id, []))
        executions = list(getattr(app, "execution_events", {}).get(order_id, []))
        errors = [x for x in getattr(app, "errors", []) if int(x[0]) == order_id]
    return statuses, executions, errors


def _latest_status(statuses: list[StatusEvent]) -> Optional[StatusEvent]:
    return statuses[-1] if statuses else None


def _wait_for_any_status(app: Any, order_id: int, timeout: float) -> list[StatusEvent]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses, _, _ = _snapshot_events(app, order_id)
        if statuses:
            return statuses
        time.sleep(0.05)
    return _snapshot_events(app, order_id)[0]


def _wait_for_terminal_or_timeout(app: Any, order_id: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses, _, _ = _snapshot_events(app, order_id)
        latest = _latest_status(statuses)
        if latest is not None and latest.status in TERMINAL_STATUSES:
            return
        time.sleep(0.05)


def classify_trace(statuses: list[StatusEvent], executions: list[ExecutionEvent], errors: list[tuple[int, int, str]]) -> tuple[str, str]:
    latest = _latest_status(statuses)
    max_filled = max((x.filled or 0.0) for x in statuses) if statuses else 0.0
    min_remaining = min((x.remaining for x in statuses if x.remaining is not None), default=None)

    if latest is not None and latest.status == "Inactive" and max_filled <= 0:
        return "PAPER_ORDER_REJECTED_OR_INACTIVE", "paper order ended Inactive before any reported fill"
    if errors and not statuses and not executions:
        return "PAPER_ORDER_REJECTED_OR_NO_ACK", "IBKR returned order errors without order-status/execution acknowledgement"
    if max_filled >= 1.0 or (min_remaining is not None and min_remaining <= 0):
        return "PAPER_FULL_FILL_OBSERVED", "paper simulator reported a full package fill"
    if max_filled > 0 or executions:
        return "PAPER_PARTIAL_OR_LEG_EXECUTION_OBSERVED", "paper simulator reported execution activity without a confirmed full package fill"
    if latest is not None and latest.status in {"Cancelled", "ApiCancelled"}:
        return "PAPER_ACCEPTED_THEN_CANCELLED", "paper order was acknowledged and remaining quantity was cancelled"
    if statuses:
        return "PAPER_ACKNOWLEDGED_NO_TERMINAL", f"latest paper order status={latest.status if latest else 'unknown'}"
    return "PAPER_NO_ACKNOWLEDGEMENT", "no orderStatus or execution callback was observed"


def summarize_trace(
    base: dict[str, Any],
    *,
    order_id: int,
    account: str,
    initial_limit: float,
    limit_source: str,
    replacement_limit: Optional[float],
    statuses: list[StatusEvent],
    executions: list[ExecutionEvent],
    errors: list[tuple[int, int, str]],
    started_at: str,
    finished_at: str,
    modify_attempted: bool,
    cancel_attempted: bool,
    open_order_seen: bool,
) -> dict[str, Any]:
    out = dict(base)
    status, reason = classify_trace(statuses, executions, errors)
    latest = _latest_status(statuses)
    sec_types = sorted({x.sec_type for x in executions if x.sec_type})
    exchanges = sorted({x.exchange for x in executions if x.exchange})
    out.update(
        phase7_status=RUN_COMPLETE,
        phase7_decision="MANUAL_REVIEW_PAPER_TRACE",
        phase7_reason=(
            reason
            + "; paper simulation has limited combo behavior and cannot establish live OSE atomicity"
        ),
        phase7_trace_class=status,
        phase7_account=account,
        phase7_order_id=int(order_id),
        phase7_initial_limit_price=float(initial_limit),
        phase7_limit_source=str(limit_source),
        phase7_replacement_limit_price=replacement_limit,
        phase7_started_at_utc=started_at,
        phase7_finished_at_utc=finished_at,
        phase7_latest_order_status=(latest.status if latest else None),
        phase7_max_filled=max((x.filled or 0.0) for x in statuses) if statuses else 0.0,
        phase7_min_remaining=min((x.remaining for x in statuses if x.remaining is not None), default=None),
        phase7_status_event_count=len(statuses),
        phase7_execution_event_count=len(executions),
        phase7_modify_attempted=bool(modify_attempted),
        phase7_cancel_attempted=bool(cancel_attempted),
        phase7_open_order_seen=bool(open_order_seen),
        phase7_execution_sec_types_json=json.dumps(sec_types, ensure_ascii=False, separators=(",", ":")),
        phase7_execution_exchanges_json=json.dumps(exchanges, ensure_ascii=False, separators=(",", ":")),
        phase7_status_events_json=json.dumps([asdict(x) for x in statuses], ensure_ascii=False, separators=(",", ":")),
        phase7_execution_events_json=json.dumps([asdict(x) for x in executions], ensure_ascii=False, separators=(",", ":")),
        phase7_errors_json=json.dumps([_jsonable_error(*x) for x in errors], ensure_ascii=False, separators=(",", ":")),
        phase7_live_money_allowed=False,
        phase7_atomicity_established=False,
    )
    return out


def run_candidate(
    row: pd.Series,
    *,
    app: Any,
    Contract: Any,
    ComboLeg: Any,
    Order: Any,
    account: str,
    exchange: str,
    currency: str,
    timeout: float,
    observe_seconds: float,
    replace_offset: Optional[float],
    limit_price_override: Optional[float],
) -> dict[str, Any]:
    plan = plan_candidate(row)
    if plan.get("phase7_status") != PLAN_ONLY:
        return plan

    selected = assert_paper_account(account, list(getattr(app, "managed_accounts", [])))
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

    initial_limit, limit_source = select_paper_limit(plan, limit_price_override)
    cid = _canon(row.get("candidate_id")) or "candidate"
    order = build_paper_order(
        Order,
        account=selected,
        limit_price=initial_limit,
        order_ref=f"kabuopu-phase7-paper-{cid}",
    )
    order_id = app.next_order_id()
    started_at = utc_now()
    submit_paper_order(app, order_id=order_id, bag=bag, order=order, account=selected)
    _wait_for_any_status(app, order_id, timeout)

    replacement_limit: Optional[float] = None
    modify_attempted = False
    if replace_offset is not None:
        statuses, executions, _ = _snapshot_events(app, order_id)
        latest = _latest_status(statuses)
        # Do not modify after any execution activity: at that point the useful
        # experiment is to observe/cancel the residual, not chase the simulator.
        if (
            latest is not None
            and latest.status not in TERMINAL_STATUSES
            and (latest.remaining or 0.0) > 0
            and not executions
        ):
            replacement_limit = initial_limit + float(replace_offset)
            modify_attempted = True
            modify_paper_order(
                app,
                order_id=order_id,
                bag=bag,
                order=order,
                account=selected,
                new_limit_price=replacement_limit,
            )

    _wait_for_terminal_or_timeout(app, order_id, observe_seconds)
    statuses, executions, errors = _snapshot_events(app, order_id)
    latest = _latest_status(statuses)
    cancel_attempted = False
    if latest is None or latest.status not in TERMINAL_STATUSES:
        cancel_attempted = True
        cancel_own_order(app, order_id)
        _wait_for_terminal_or_timeout(app, order_id, min(timeout, 5.0))
        statuses, executions, errors = _snapshot_events(app, order_id)

    return summarize_trace(
        plan,
        order_id=order_id,
        account=selected,
        initial_limit=initial_limit,
        limit_source=limit_source,
        replacement_limit=replacement_limit,
        statuses=statuses,
        executions=executions,
        errors=errors,
        started_at=started_at,
        finished_at=utc_now(),
        modify_attempted=modify_attempted,
        cancel_attempted=cancel_attempted,
        open_order_seen=(order_id in getattr(app, "order_open_seen", set())),
    )


def _validate_input(df: pd.DataFrame) -> None:
    required = {
        "underlying",
        "expiry",
        "legs_json",
        "phase6_decision",
        "phase6_combo_limit_per_share",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Phase 6 study missing required columns: {', '.join(sorted(missing))}")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Phase 6 execution_study_whatif.csv")
    p.add_argument("--output", default="data/paper_combo_trace.csv")
    p.add_argument("--candidate-id", default=None, help="optional exact Phase 6 candidate_id")
    p.add_argument("--run-paper", action="store_true", help="submit one simulated paper BAG order")
    p.add_argument("--paper-ack", default=None, help=f"required with --run-paper: {PAPER_ACK}")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="paper IB Gateway default; configure TWS/Gateway as needed")
    p.add_argument("--client-id", type=int, default=71)
    p.add_argument("--account", default=None, help="required with --run-paper; must be managed and DU-prefixed")
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--observe-seconds", type=float, default=5.0)
    p.add_argument(
        "--paper-limit-price",
        type=float,
        default=None,
        help="optional paper-only limit override from a current observed combo quote; otherwise use Phase 6 reference",
    )
    p.add_argument(
        "--replace-offset",
        type=float,
        default=None,
        help="optional one-time LMT price offset on the same paper order ID; omit to skip replace test",
    )
    args = p.parse_args()
    if args.timeout <= 0:
        p.error("--timeout must be positive")
    if args.observe_seconds < 0:
        p.error("--observe-seconds must be non-negative")
    if args.run_paper:
        if args.paper_ack != PAPER_ACK:
            p.error(f"--run-paper requires --paper-ack {PAPER_ACK!r}")
        if not args.account:
            p.error("--run-paper requires --account")
        if not is_paper_account_code(args.account):
            p.error("--run-paper account must satisfy the hard DU paper-account guard")
    return args


def main() -> int:
    args = _args()
    df = pd.read_csv(args.input)
    _validate_input(df)
    work = df[df["phase6_decision"].astype(str) == PHASE6_READY].copy()
    if args.candidate_id is not None:
        if "candidate_id" not in work.columns:
            raise SystemExit("--candidate-id requested but input has no candidate_id column")
        work = work[work["candidate_id"].astype(str) == str(args.candidate_id)]
    work = work.head(1)  # hard safety limit: at most one simulated order per invocation

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if work.empty:
        pd.DataFrame(columns=list(df.columns) + ["phase7_status", "phase7_decision", "phase7_reason"]).to_csv(
            out_path, index=False, encoding="utf-8-sig"
        )
        print(f"no {PHASE6_READY} candidate matched; wrote empty Phase 7 file to {out_path}")
        return 0

    if not args.run_paper:
        out = pd.DataFrame([plan_candidate(work.iloc[0])])
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"wrote one Phase 7 plan row to {out_path}")
        print("plan-only mode: no IBKR connection and no order submission occurred")
        return 0

    app, Contract, ComboLeg, Order = make_paper_app()
    app.connect(args.host, args.port, args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    try:
        if not app.ready.wait(args.timeout):
            raise SystemExit("IBKR connected but nextValidId was not received")
        app.reqManagedAccts()
        if not app.accounts_ready.wait(args.timeout):
            raise SystemExit("IBKR managedAccounts was not received; refusing paper submit")
        # Guard once before contract lookup, then again immediately inside submit_paper_order.
        selected = assert_paper_account(args.account, list(app.managed_accounts))
        row = run_candidate(
            work.iloc[0],
            app=app,
            Contract=Contract,
            ComboLeg=ComboLeg,
            Order=Order,
            account=selected,
            exchange=args.exchange,
            currency=args.currency,
            timeout=args.timeout,
            observe_seconds=args.observe_seconds,
            replace_offset=args.replace_offset,
            limit_price_override=args.paper_limit_price,
        )
        out = pd.DataFrame([row])
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"wrote one Phase 7 paper trace to {out_path}")
        print(f"trace_class={row.get('phase7_trace_class')} latest={row.get('phase7_latest_order_status')}")
        print("paper simulation only: no Phase 7 result establishes live OSE atomicity or authorizes live trading")
        return 0
    finally:
        app.disconnect()
        thread.join(timeout=1.0)


if __name__ == "__main__":
    raise SystemExit(main())
