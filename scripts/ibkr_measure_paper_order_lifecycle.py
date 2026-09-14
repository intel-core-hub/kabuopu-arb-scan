#!/usr/bin/env python3
"""Phase 15a: paper-only order lifecycle timing probe.

This script reuses the hard paper-account guards from Phase 7, submits at most one
1-package BAG order per invocation, and measures local monotonic elapsed time from
just before ``placeOrder`` to IBKR callbacks such as ``openOrder``, ``orderStatus``,
``execDetails``, and order-specific errors.

The timings are paper-simulator / TWS callback timings only.  They are NOT OSE order
arrival times, exchange latency, live execution latency, or fill probabilities.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PHASE14_READY = "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH"
PHASE6_READY = "PAPER_COMBO_TEST_REQUIRED"
PAPER_ACK = "I_UNDERSTAND_THIS_SUBMITS_ONE_SIMULATED_PAPER_ORDER_FOR_TIMING_RESEARCH"
TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class CallbackEvent:
    kind: str
    elapsed_ms: float
    ts_utc: str
    detail: str = ""


class LifecycleRecorder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.start_ns: Optional[int] = None
        self.events: list[CallbackEvent] = []

    def start(self) -> None:
        with self._lock:
            self.start_ns = time.monotonic_ns()
            self.events = []

    def mark(self, kind: str, detail: str = "") -> None:
        now = time.monotonic_ns()
        with self._lock:
            if self.start_ns is None:
                return
            elapsed = (now - self.start_ns) / 1_000_000.0
            self.events.append(CallbackEvent(kind=str(kind), elapsed_ms=elapsed, ts_utc=utc_now(), detail=str(detail)))

    def snapshot(self) -> list[CallbackEvent]:
        with self._lock:
            return list(self.events)


def _first_ms(events: list[CallbackEvent], kind: str) -> Optional[float]:
    vals = [e.elapsed_ms for e in events if e.kind == kind]
    return min(vals) if vals else None


def _first_prefix_ms(events: list[CallbackEvent], prefix: str) -> Optional[float]:
    vals = [e.elapsed_ms for e in events if e.kind.startswith(prefix)]
    return min(vals) if vals else None


def classify_lifecycle(events: list[CallbackEvent]) -> tuple[str, str]:
    kinds = [e.kind for e in events]
    if any(k == "orderStatus:Inactive" for k in kinds):
        return "PAPER_REJECTED_OR_INACTIVE", "paper simulator reported Inactive"
    if not any(k in {"openOrder", "execDetails"} or k.startswith("orderStatus:") for k in kinds):
        if any(k == "error" for k in kinds):
            return "PAPER_ERROR_WITHOUT_ORDER_ACK", "order-specific error arrived without openOrder/orderStatus/execDetails"
        return "PAPER_NO_ORDER_ACK", "no openOrder/orderStatus/execDetails callback observed"
    if any(k == "orderStatus:Filled" for k in kinds) or any(k == "execDetails" for k in kinds):
        return "PAPER_EXECUTION_ACTIVITY_OBSERVED", "paper simulator reported execution activity"
    if any(k in {"orderStatus:Cancelled", "orderStatus:ApiCancelled"} for k in kinds):
        return "PAPER_ACK_THEN_CANCELLED", "paper order was acknowledged and then cancelled"
    return "PAPER_ACK_OBSERVED", "paper order acknowledgement was observed"


def summarize_lifecycle(
    *,
    base: dict[str, Any],
    events: list[CallbackEvent],
    account: str,
    order_id: int,
    submit_return_ms: Optional[float],
    cancel_sent_ms: Optional[float],
    phase14_budget_ms: Optional[float],
) -> dict[str, Any]:
    status, reason = classify_lifecycle(events)
    callback_ms = [e.elapsed_ms for e in events if e.kind != "placeOrder_return"]
    first_any = min(callback_ms) if callback_ms else None
    terminal_candidates = [
        e.elapsed_ms for e in events
        if e.kind in {"orderStatus:Filled", "orderStatus:Cancelled", "orderStatus:ApiCancelled", "orderStatus:Inactive"}
    ]
    terminal_ms = min(terminal_candidates) if terminal_candidates else None
    out = dict(base)
    out.update(
        phase15_status="PAPER_LIFECYCLE_MEASURED",
        phase15_trace_class=status,
        phase15_reason=reason,
        phase15_account=account,
        phase15_order_id=int(order_id),
        phase15_submit_return_ms=submit_return_ms,
        phase15_first_callback_ms=first_any,
        phase15_open_order_ms=_first_ms(events, "openOrder"),
        phase15_first_order_status_ms=_first_prefix_ms(events, "orderStatus:"),
        phase15_pre_submitted_ms=_first_ms(events, "orderStatus:PreSubmitted"),
        phase15_submitted_ms=_first_ms(events, "orderStatus:Submitted"),
        phase15_first_exec_details_ms=_first_ms(events, "execDetails"),
        phase15_first_error_ms=_first_ms(events, "error"),
        phase15_terminal_ms=terminal_ms,
        phase15_cancel_sent_ms=cancel_sent_ms,
        phase15_cancel_to_terminal_ms=(terminal_ms - cancel_sent_ms if terminal_ms is not None and cancel_sent_ms is not None and terminal_ms >= cancel_sent_ms else None),
        phase15_phase14_budget_ms=phase14_budget_ms,
        phase15_events_json=json.dumps([asdict(e) for e in events], ensure_ascii=False, separators=(",", ":")),
        phase15_is_exchange_arrival_latency=False,
        phase15_is_live_latency=False,
        phase15_is_fill_probability=False,
        phase15_live_money_allowed=False,
    )
    return out


def phase14_gate(rows: pd.DataFrame, candidate_id: str) -> tuple[bool, Optional[float], str]:
    if "candidate_id" not in rows.columns or "phase14_candidate_decision" not in rows.columns:
        return False, None, "Phase 14 candidate file lacks required columns"
    hit = rows[rows["candidate_id"].astype(str) == str(candidate_id)]
    if hit.empty:
        return False, None, "candidate_id not found in Phase 14 candidate file"
    row = hit.iloc[0]
    decision = _text(row.get("phase14_candidate_decision"))
    budget = _finite(row.get("latency_budget_ms"))
    if decision != PHASE14_READY:
        return False, budget, f"Phase 14 decision is {decision or 'missing'}, not {PHASE14_READY}"
    return True, budget, "Phase 14 gate passed"


def instrument_app(app: Any, recorder: LifecycleRecorder, order_id: int) -> None:
    """Wrap only this order's callbacks with monotonic timestamps."""
    original_open = app.openOrder
    original_status = app.orderStatus
    original_exec = app.execDetails
    original_error = app.error

    def open_order(oid, contract, order, order_state):  # noqa: ANN001
        if int(oid) == int(order_id):
            recorder.mark("openOrder")
        return original_open(oid, contract, order, order_state)

    def order_status(oid, status, filled, remaining, avg_fill_price, perm_id, parent_id, last_fill_price, client_id, why_held, mkt_cap_price=0.0):  # noqa: ANN001,E501
        if int(oid) == int(order_id):
            recorder.mark(f"orderStatus:{status}", f"filled={filled};remaining={remaining}")
        return original_status(oid, status, filled, remaining, avg_fill_price, perm_id, parent_id, last_fill_price, client_id, why_held, mkt_cap_price)

    def exec_details(req_id, contract, execution):  # noqa: ANN001
        oid = getattr(execution, "orderId", None)
        if oid is not None and int(oid) == int(order_id):
            recorder.mark("execDetails", f"execId={getattr(execution, 'execId', '')};secType={getattr(contract, 'secType', '')}")
        return original_exec(req_id, contract, execution)

    def error(req_id, *args):  # noqa: ANN001
        try:
            matches = int(req_id) == int(order_id)
        except Exception:
            matches = False
        if matches:
            recorder.mark("error", " | ".join(map(str, args)))
        return original_error(req_id, *args)

    app.openOrder = open_order
    app.orderStatus = order_status
    app.execDetails = exec_details
    app.error = error


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase6_input", help="Phase 6 execution_study_whatif.csv")
    p.add_argument("phase14_candidates", help="Phase 14 candidate CSV")
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--output", default="data/phase15_paper_lifecycle.csv")
    p.add_argument("--run-paper", action="store_true")
    p.add_argument("--paper-ack", default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=75)
    p.add_argument("--exchange", default="OSE.JPN")
    p.add_argument("--currency", default="JPY")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--observe-seconds", type=float, default=3.0)
    p.add_argument("--paper-limit-price", type=float, default=None)
    args = p.parse_args()
    if args.timeout <= 0 or args.observe_seconds < 0:
        p.error("timeout must be positive and observe-seconds non-negative")
    if args.run_paper:
        if args.paper_ack != PAPER_ACK:
            p.error(f"--run-paper requires --paper-ack {PAPER_ACK!r}")
        if not args.account:
            p.error("--run-paper requires --account")
        if not str(args.account).strip().upper().startswith("DU"):
            p.error("--account must satisfy the DU paper-account guard")
    return args


def main() -> int:
    args = _args()
    phase6 = pd.read_csv(args.phase6_input)
    p14 = pd.read_csv(args.phase14_candidates)
    if "candidate_id" not in phase6.columns:
        raise SystemExit("Phase 6 input lacks candidate_id")
    work = phase6[phase6["candidate_id"].astype(str) == str(args.candidate_id)].copy()
    if "phase6_decision" in work.columns:
        work = work[work["phase6_decision"].astype(str) == PHASE6_READY]
    work = work.head(1)
    if work.empty:
        raise SystemExit("candidate not found / not Phase 6 paper-ready")
    gate_ok, budget_ms, gate_reason = phase14_gate(p14, args.candidate_id)
    base = dict(work.iloc[0])
    base["phase15_phase14_gate_reason"] = gate_reason
    base["phase15_phase14_budget_ms"] = budget_ms
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not gate_ok:
        base.update(
            phase15_status="SKIPPED_PHASE14_GATE",
            phase15_trace_class="NOT_RUN",
            phase15_reason=gate_reason,
            phase15_live_money_allowed=False,
            phase15_is_exchange_arrival_latency=False,
            phase15_is_live_latency=False,
            phase15_is_fill_probability=False,
        )
        pd.DataFrame([base]).to_csv(out_path, index=False, encoding="utf-8-sig")
        return 0
    if not args.run_paper:
        base.update(
            phase15_status="PLAN_ONLY",
            phase15_trace_class="NOT_RUN",
            phase15_reason="Phase 14 gate passed; exact paper ACK + --run-paper required to submit one simulated order",
            phase15_live_money_allowed=False,
            phase15_is_exchange_arrival_latency=False,
            phase15_is_live_latency=False,
            phase15_is_fill_probability=False,
        )
        pd.DataFrame([base]).to_csv(out_path, index=False, encoding="utf-8-sig")
        print("plan-only mode: no IBKR connection and no order submission occurred")
        return 0

    try:
        from ibkr_paper_combo_test import (
            _latest_status,
            _snapshot_events,
            _wait_for_terminal_or_timeout,
            assert_paper_account,
            build_bag_contract,
            build_paper_order,
            cancel_own_order,
            make_paper_app,
            parse_legs_json,
            resolve_option_contract,
            select_paper_limit,
            submit_paper_order,
        )
    except ImportError:
        from scripts.ibkr_paper_combo_test import (  # type: ignore
            _latest_status,
            _snapshot_events,
            _wait_for_terminal_or_timeout,
            assert_paper_account,
            build_bag_contract,
            build_paper_order,
            cancel_own_order,
            make_paper_app,
            parse_legs_json,
            resolve_option_contract,
            select_paper_limit,
            submit_paper_order,
        )

    app, Contract, ComboLeg, Order = make_paper_app()
    app.connect(args.host, args.port, args.client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    try:
        if not app.ready.wait(args.timeout):
            raise SystemExit("IBKR connected but nextValidId was not received")
        app.reqManagedAccts()
        if not app.accounts_ready.wait(args.timeout):
            raise SystemExit("managedAccounts not received; refusing paper submit")
        selected = assert_paper_account(args.account, list(app.managed_accounts))
        row = work.iloc[0]
        legs = parse_legs_json(str(row["legs_json"]))
        contracts: dict[tuple[str, float], Any] = {}
        for leg in legs:
            contracts[leg.key] = resolve_option_contract(
                app, Contract,
                underlying=str(row["underlying"]), expiry=str(row["expiry"]), leg=leg,
                exchange=args.exchange, currency=args.currency, timeout=args.timeout,
            )
        bag = build_bag_contract(
            Contract, ComboLeg,
            underlying=str(row["underlying"]), legs=legs, contracts=contracts,
            exchange=args.exchange, currency=args.currency,
        )
        plan = dict(row)
        plan["phase7_limit_price"] = row.get("phase6_combo_limit_per_share")
        limit_price, _ = select_paper_limit(plan, args.paper_limit_price)
        order = build_paper_order(
            Order,
            account=selected,
            limit_price=limit_price,
            order_ref=f"kabuopu-phase7-paper-{args.candidate_id}-phase15",
        )
        order_id = app.next_order_id()
        recorder = LifecycleRecorder()
        instrument_app(app, recorder, order_id)
        recorder.start()
        submit_paper_order(app, order_id=order_id, bag=bag, order=order, account=selected)
        recorder.mark("placeOrder_return")
        submit_return_ms = _first_ms(recorder.snapshot(), "placeOrder_return")
        _wait_for_terminal_or_timeout(app, order_id, args.observe_seconds)
        statuses, executions, errors = _snapshot_events(app, order_id)
        latest = _latest_status(statuses)
        cancel_sent_ms: Optional[float] = None
        if latest is None or latest.status not in TERMINAL_STATUSES:
            recorder.mark("cancelOrder_send")
            cancel_sent_ms = _first_ms(recorder.snapshot(), "cancelOrder_send")
            cancel_own_order(app, order_id)
            _wait_for_terminal_or_timeout(app, order_id, min(args.timeout, 5.0))
        # allow in-flight callbacks to settle very briefly without extending the experiment materially
        time.sleep(0.10)
        statuses, executions, errors = _snapshot_events(app, order_id)
        out = summarize_lifecycle(
            base=base,
            events=recorder.snapshot(),
            account=selected,
            order_id=order_id,
            submit_return_ms=submit_return_ms,
            cancel_sent_ms=cancel_sent_ms,
            phase14_budget_ms=budget_ms,
        )
        out["phase15_observed_status_count"] = len(statuses)
        out["phase15_observed_execution_count"] = len(executions)
        out["phase15_observed_error_count"] = len(errors)
        pd.DataFrame([out]).to_csv(out_path, index=False, encoding="utf-8-sig")
        print(json.dumps({k: out.get(k) for k in ["candidate_id", "phase15_trace_class", "phase15_first_callback_ms", "phase15_phase14_budget_ms"]}, ensure_ascii=False, indent=2))
        print("paper simulator only: this does not measure OSE arrival or authorize live trading")
        return 0
    finally:
        app.disconnect()
        thread.join(timeout=1.0)


if __name__ == "__main__":
    raise SystemExit(main())
