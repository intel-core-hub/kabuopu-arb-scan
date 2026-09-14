#!/usr/bin/env python3
"""Phase 15b: aggregate paper-order callback lifecycle timings.

The analysis compares repeated paper-simulator callback timings with the conservative
Phase 14 latency budget.  It does NOT reinterpret either measurement as exchange-arrival
latency, live latency, queue position, or fill probability.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _finite(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def empirical_quantile(values: list[float], q: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if _finite(v) is not None)
    if not vals:
        return None
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0,1]")
    if q == 0:
        return vals[0]
    rank = max(1, math.ceil(q * len(vals)))
    return vals[min(rank - 1, len(vals) - 1)]


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def expand_inputs(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        matches = sorted(glob.glob(item))
        out.extend(matches if matches else [item])
    # preserve order, de-duplicate exact file names
    return list(dict.fromkeys(out))


def analyze_candidate(rows: list[dict[str, Any]], *, min_sessions: int, quantile: float) -> dict[str, Any]:
    candidate_id = _text(rows[0].get("candidate_id")) if rows else ""
    measured = [r for r in rows if _text(r.get("phase15_status")) == "PAPER_LIFECYCLE_MEASURED"]
    acked = [r for r in measured if _finite(r.get("phase15_first_callback_ms")) is not None]
    rejected = [r for r in measured if _text(r.get("phase15_trace_class")) in {"PAPER_REJECTED_OR_INACTIVE", "PAPER_ERROR_WITHOUT_ORDER_ACK", "PAPER_NO_ORDER_ACK"}]
    budgets = [_finite(r.get("phase15_phase14_budget_ms")) for r in measured]
    budgets = [x for x in budgets if x is not None]
    firsts = [float(r["phase15_first_callback_ms"]) for r in acked]
    submitted = [_finite(r.get("phase15_submitted_ms")) for r in acked]
    submitted = [x for x in submitted if x is not None]
    first_exec = [_finite(r.get("phase15_first_exec_details_ms")) for r in measured]
    first_exec = [x for x in first_exec if x is not None]
    q_ack = empirical_quantile(firsts, quantile)
    budget = max(budgets) if budgets else None  # conservative if configs differ across runs

    if not measured:
        decision = "NO_PAPER_LIFECYCLE_DATA"
        reason = "no measured Phase 15 paper lifecycle rows"
    elif rejected:
        decision = "PAPER_LIFECYCLE_REJECTED_OR_UNACKNOWLEDGED"
        reason = f"{len(rejected)} measured session(s) rejected or lacked an order acknowledgement"
    elif len(acked) < min_sessions:
        decision = "COLLECT_MORE_PAPER_LIFECYCLE_DATA"
        reason = f"acknowledged sessions={len(acked)} < required {min_sessions}"
    elif budget is None or q_ack is None:
        decision = "INPUT_INCOMPLETE"
        reason = "Phase 14 budget or paper callback timing is unavailable"
    elif q_ack > budget:
        decision = "PAPER_ACK_P95_EXCEEDS_PHASE14_BUDGET" if abs(quantile - 0.95) < 1e-12 else "PAPER_ACK_QUANTILE_EXCEEDS_PHASE14_BUDGET"
        reason = f"paper first-callback q={quantile:.3f} is {q_ack:.3f}ms > Phase 14 budget {budget:.3f}ms"
    else:
        decision = "PAPER_ACK_P95_WITHIN_PHASE14_BUDGET" if abs(quantile - 0.95) < 1e-12 else "PAPER_ACK_QUANTILE_WITHIN_PHASE14_BUDGET"
        reason = f"paper first-callback q={quantile:.3f} is {q_ack:.3f}ms <= Phase 14 budget {budget:.3f}ms"

    return {
        "candidate_id": candidate_id,
        "phase15_decision": decision,
        "phase15_reason": reason,
        "measured_sessions": len(measured),
        "acknowledged_sessions": len(acked),
        "rejected_or_unacknowledged_sessions": len(rejected),
        "timing_quantile": quantile,
        "paper_first_callback_quantile_ms": q_ack,
        "paper_first_callback_max_ms": max(firsts) if firsts else None,
        "paper_submitted_quantile_ms": empirical_quantile([float(x) for x in submitted], quantile),
        "paper_first_exec_quantile_ms": empirical_quantile([float(x) for x in first_exec], quantile),
        "phase14_budget_ms_conservative": budget,
        "phase15_is_exchange_arrival_latency": False,
        "phase15_is_live_latency": False,
        "phase15_is_fill_probability": False,
        "phase15_live_money_allowed": False,
    }


def analyze(rows: list[dict[str, Any]], *, min_sessions: int = 3, quantile: float = 0.95) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cid = _text(row.get("candidate_id"))
        if cid:
            by_candidate[cid].append(row)
    candidates = [analyze_candidate(group, min_sessions=min_sessions, quantile=quantile) for _, group in sorted(by_candidate.items())]
    counts: dict[str, int] = defaultdict(int)
    for row in candidates:
        counts[_text(row.get("phase15_decision"))] += 1
    if any(_text(r.get("phase15_decision")).endswith("WITHIN_PHASE14_BUDGET") for r in candidates):
        overall = "PAPER_CONTROL_PATH_WITHIN_BUDGET_FOR_NEXT_RESEARCH"
        reason = "at least one candidate has repeated paper callback acknowledgement within the conservative Phase 14 budget"
    elif candidates:
        overall = "NO_PAPER_CONTROL_PATH_CANDIDATE_YET"
        reason = "no candidate cleared the repeated paper callback timing gate"
    else:
        overall = "NO_ANALYZABLE_CANDIDATES"
        reason = "no candidate rows were found"
    summary = {
        "phase15_overall_decision": overall,
        "phase15_reason": reason,
        "candidate_decision_counts": dict(counts),
        "min_sessions": min_sessions,
        "timing_quantile": quantile,
        "phase15_is_exchange_arrival_latency": False,
        "phase15_is_live_latency": False,
        "phase15_is_fill_probability": False,
        "phase15_live_money_allowed": False,
        "interpretation": (
            "Paper callback timing is only a TWS/paper-simulator control-path observation. "
            "Even a favorable result does not establish OSE arrival latency, production routing, atomicity, queue priority, or fill probability."
        ),
    }
    return candidates, summary


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        p.write_text("", encoding="utf-8-sig")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--timing-quantile", type=float, default=0.95)
    p.add_argument("--output", default="data/phase15_paper_lifecycle_summary.csv")
    p.add_argument("--summary-json", default="data/phase15_summary.json")
    args = p.parse_args()
    if args.min_sessions <= 0:
        p.error("--min-sessions must be positive")
    if not 0 <= args.timing_quantile <= 1:
        p.error("--timing-quantile must be in [0,1]")
    return args


def main() -> int:
    args = _args()
    rows: list[dict[str, Any]] = []
    for path in expand_inputs(args.inputs):
        rows.extend(read_csv(path))
    candidates, summary = analyze(rows, min_sessions=args.min_sessions, quantile=args.timing_quantile)
    write_csv(args.output, candidates)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
