#!/usr/bin/env python3
"""Phase 9: offline venue/broker confirmation gate after Phase 8.

This script never connects to IBKR and never submits, modifies, or cancels orders.
It combines Phase 8 candidate decisions with explicit, reviewable evidence about
OSE securities-option strategy support and IBKR routing/guarantee semantics.

No Phase 9 outcome authorizes live-money trading.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

PHASE8_PROMOTE = "BROKER_CONFIRMATION_REQUIRED"

VENUE_STATES = {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}
BAG_STATES = {"YES", "NO", "UNKNOWN"}
ROUTE_STATES = {"DIRECT_EXCHANGE", "SMART", "BROKER_INTERNAL", "UNKNOWN"}
GUARANTEE_STATES = {"ATOMIC", "NON_ATOMIC_OR_LEGGING_POSSIBLE", "UNKNOWN"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "t", "yes", "y"}


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalized(value: Any, allowed: set[str], *, field: str) -> str:
    out = _text(value).upper()
    if out not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")
    return out


def load_evidence(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("evidence JSON must contain an object")
    venue = data.get("venue")
    broker = data.get("broker")
    if not isinstance(venue, dict) or not isinstance(broker, dict):
        raise ValueError("evidence JSON requires object fields: venue and broker")

    # Fail closed on typos instead of silently interpreting them as confirmations.
    _normalized(venue.get("strategy_trades"), VENUE_STATES, field="venue.strategy_trades")
    _normalized(broker.get("ose_sso_bag_supported"), BAG_STATES, field="broker.ose_sso_bag_supported")
    _normalized(broker.get("ose_sso_route_mode"), ROUTE_STATES, field="broker.ose_sso_route_mode")
    _normalized(
        broker.get("ose_sso_execution_guarantee"),
        GUARANTEE_STATES,
        field="broker.ose_sso_execution_guarantee",
    )
    return data


def evidence_freshness(
    evidence: dict[str, Any], *, as_of: date, max_age_days: int
) -> tuple[bool, list[str]]:
    stale: list[str] = []
    for section_name in ("venue", "broker"):
        section = evidence.get(section_name, {})
        verified = _parse_date(section.get("verified_date"))
        if verified is None:
            stale.append(f"{section_name}.verified_date missing/invalid")
            continue
        age = (as_of - verified).days
        if age < 0:
            stale.append(f"{section_name}.verified_date is in the future")
        elif age > max_age_days:
            stale.append(f"{section_name} evidence is {age} days old (> {max_age_days})")
    return not stale, stale


def evaluate_candidate(
    row: dict[str, Any],
    evidence: dict[str, Any],
    *,
    as_of: date,
    max_age_days: int = 90,
) -> dict[str, Any]:
    out = dict(row)
    out["phase9_live_money_allowed"] = False
    out["phase9_atomicity_established"] = False

    venue = evidence["venue"]
    broker = evidence["broker"]
    venue_strategy = _normalized(venue.get("strategy_trades"), VENUE_STATES, field="venue.strategy_trades")
    bag = _normalized(broker.get("ose_sso_bag_supported"), BAG_STATES, field="broker.ose_sso_bag_supported")
    route = _normalized(broker.get("ose_sso_route_mode"), ROUTE_STATES, field="broker.ose_sso_route_mode")
    guarantee = _normalized(
        broker.get("ose_sso_execution_guarantee"),
        GUARANTEE_STATES,
        field="broker.ose_sso_execution_guarantee",
    )
    fresh, stale_reasons = evidence_freshness(evidence, as_of=as_of, max_age_days=max_age_days)

    out.update(
        phase9_venue_strategy_trades=venue_strategy,
        phase9_broker_bag_supported=bag,
        phase9_broker_route_mode=route,
        phase9_broker_execution_guarantee=guarantee,
        phase9_evidence_fresh=fresh,
        phase9_evidence_stale_reasons_json=json.dumps(stale_reasons, ensure_ascii=False, separators=(",", ":")),
        phase9_venue_source=_text(venue.get("source")),
        phase9_broker_source=_text(broker.get("source")),
        phase9_broker_case_id=_text(broker.get("support_case_id")),
    )

    phase8_decision = _text(row.get("phase8_candidate_decision"))
    if phase8_decision != PHASE8_PROMOTE:
        decision = "NOT_ELIGIBLE_FROM_PHASE8"
        reason = f"Phase 8 decision is {phase8_decision or 'missing'}, not {PHASE8_PROMOTE}"
    elif _bool(row.get("phase8_live_money_allowed")) or _bool(row.get("phase8_atomicity_established")):
        decision = "STOP_INCONSISTENT_PHASE8_SAFETY_FLAGS"
        reason = "Phase 8 unexpectedly claims live-money permission or established atomicity"
    elif venue_strategy == "UNAVAILABLE":
        # Current JPX securities-option specification lands here. A broker may still
        # accept a BAG as a synthetic/non-guaranteed convenience, but that is not an
        # exchange-native strategy trade and must not be promoted as atomic arbitrage.
        if guarantee == "ATOMIC" or route == "DIRECT_EXCHANGE":
            decision = "EVIDENCE_CONFLICT_MANUAL_ESCALATION"
            reason = (
                "venue evidence says strategy trades are unavailable, but broker evidence "
                "claims direct/atomic OSE SSO combo semantics; resolve the contradiction in writing"
            )
        elif bag == "YES":
            decision = "NON_ATOMIC_EXECUTION_RESEARCH_ONLY"
            reason = (
                "broker may accept a BAG, but venue strategy trades are unavailable; treat it as "
                "synthetic/non-atomic unless authoritative written evidence proves otherwise"
            )
        else:
            decision = "NO_GO_EXCHANGE_ATOMIC_COMBO"
            reason = "OSE securities-option strategy trades are unavailable in the supplied venue evidence"
    elif venue_strategy == "UNKNOWN":
        decision = "WAIT_VENUE_CONFIRMATION"
        reason = "venue strategy-trade availability is not confirmed"
    elif not fresh:
        decision = "STALE_EVIDENCE_RECHECK_REQUIRED"
        reason = "required venue/broker evidence is stale or missing a verification date"
    elif bag == "UNKNOWN":
        decision = "WAIT_BROKER_CONFIRMATION"
        reason = "IBKR support for OSE single-stock-option BAG orders is not confirmed"
    elif bag == "NO":
        decision = "STOP_BROKER_COMBO_UNSUPPORTED"
        reason = "broker evidence says OSE single-stock-option BAG orders are unsupported"
    elif route == "UNKNOWN" or guarantee == "UNKNOWN":
        decision = "WAIT_BROKER_ROUTING_GUARANTEE_CONFIRMATION"
        reason = "BAG acceptance alone is insufficient; route mode and execution guarantee remain unknown"
    elif route in {"SMART", "BROKER_INTERNAL"} or guarantee == "NON_ATOMIC_OR_LEGGING_POSSIBLE":
        decision = "NON_ATOMIC_EXECUTION_RESEARCH_ONLY"
        reason = "broker routing/guarantee evidence permits separate leg execution"
    elif route == "DIRECT_EXCHANGE" and guarantee == "ATOMIC":
        decision = "MANUAL_LIVE_DESIGN_REVIEW_REQUIRED"
        reason = (
            "venue and broker evidence are internally consistent with an atomic direct route, but Phase 9 "
            "still does not authorize live-money trading"
        )
    else:
        decision = "WAIT_BROKER_CONFIRMATION"
        reason = "evidence does not meet a known promotion or stop condition"

    out["phase9_candidate_decision"] = decision
    out["phase9_candidate_reason"] = reason
    return out


def evaluate(
    phase8_csv: str | Path,
    evidence_json: str | Path,
    *,
    as_of: date | None = None,
    max_age_days: int = 90,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    as_of = as_of or datetime.now().date()
    evidence = load_evidence(evidence_json)
    with Path(phase8_csv).open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    required = {"candidate_id", "phase8_candidate_decision", "phase8_live_money_allowed", "phase8_atomicity_established"}
    if rows:
        missing = required - set(rows[0])
        if missing:
            raise ValueError(f"Phase 8 input missing required columns: {', '.join(sorted(missing))}")

    evaluated = [
        evaluate_candidate(row, evidence, as_of=as_of, max_age_days=max_age_days)
        for row in rows
    ]
    decisions = [r["phase9_candidate_decision"] for r in evaluated]
    counts = {name: decisions.count(name) for name in sorted(set(decisions))}

    if any(d == "EVIDENCE_CONFLICT_MANUAL_ESCALATION" for d in decisions):
        overall = "STOP_AND_RESOLVE_EVIDENCE_CONFLICT"
    elif any(d == "MANUAL_LIVE_DESIGN_REVIEW_REQUIRED" for d in decisions):
        overall = "NO_LIVE_AUTOMATION_MANUAL_DESIGN_REVIEW"
    elif any(d == "NON_ATOMIC_EXECUTION_RESEARCH_ONLY" for d in decisions):
        overall = "ATOMIC_ARBITRAGE_NO_GO_NONATOMIC_RESEARCH_ONLY"
    elif any(d.startswith("WAIT_") or d == "STALE_EVIDENCE_RECHECK_REQUIRED" for d in decisions):
        overall = "NO_LIVE_AUTOMATION_CONFIRMATION_INCOMPLETE"
    elif decisions and all(d in {"NO_GO_EXCHANGE_ATOMIC_COMBO", "STOP_BROKER_COMBO_UNSUPPORTED", "NOT_ELIGIBLE_FROM_PHASE8"} for d in decisions):
        overall = "ATOMIC_COMBO_PATH_BLOCKED"
    else:
        overall = "NO_LIVE_AUTOMATION_KEEP_RESEARCHING"

    summary = {
        "phase9_overall_decision": overall,
        "phase9_live_money_allowed": False,
        "phase9_atomicity_established": False,
        "as_of_date": as_of.isoformat(),
        "candidate_count": len(evaluated),
        "candidate_decision_counts": counts,
        "venue_strategy_trades": _text(evidence["venue"].get("strategy_trades")).upper(),
        "broker_ose_sso_bag_supported": _text(evidence["broker"].get("ose_sso_bag_supported")).upper(),
        "broker_ose_sso_route_mode": _text(evidence["broker"].get("ose_sso_route_mode")).upper(),
        "broker_ose_sso_execution_guarantee": _text(evidence["broker"].get("ose_sso_execution_guarantee")).upper(),
    }
    return evaluated, summary


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase8_csv", help="Phase 8 candidate summary CSV")
    p.add_argument("--evidence", required=True, help="reviewed venue/broker evidence JSON")
    p.add_argument("--output", default="data/phase9_broker_gate.csv")
    p.add_argument("--summary-json", default="data/phase9_summary.json")
    p.add_argument("--as-of-date", help="YYYY-MM-DD; defaults to local current date")
    p.add_argument("--max-evidence-age-days", type=int, default=90)
    args = p.parse_args()
    if args.max_evidence_age_days < 0:
        p.error("--max-evidence-age-days must be non-negative")
    if args.as_of_date:
        try:
            date.fromisoformat(args.as_of_date)
        except ValueError:
            p.error("--as-of-date must be YYYY-MM-DD")
    return args


def main() -> int:
    args = _args()
    as_of = date.fromisoformat(args.as_of_date) if args.as_of_date else None
    rows, summary = evaluate(
        args.phase8_csv,
        args.evidence,
        as_of=as_of,
        max_age_days=args.max_evidence_age_days,
    )
    out = Path(args.output)
    js = Path(args.summary_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    js.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    else:
        out.write_text("", encoding="utf-8-sig")
    js.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} candidate row(s) to {out}")
    print(f"overall={summary['phase9_overall_decision']}")
    print("Phase 9 never authorizes live-money trading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
