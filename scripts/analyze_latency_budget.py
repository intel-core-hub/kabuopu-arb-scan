#!/usr/bin/env python3
"""Phase 14b: combine control-plane RTT proxy with Phase 12/13 evidence.

The analysis uses the FULL selected RTT quantile plus a configurable safety margin as a
conservative latency budget.  It never divides RTT by two and never labels the result as
order-arrival latency or fill probability.  For each leg it chooses the smallest recorded
Phase 12 survival horizon that is >= the latency budget, which is conservative because
survival is non-increasing with horizon.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

SURVIVAL_RE = re.compile(r"^survival_(\d+)ms$")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def empirical_quantile(values: list[float], q: float) -> Optional[float]:
    clean = sorted(float(v) for v in values if _finite(v) is not None)
    if not clean:
        return None
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0,1]")
    if q == 0:
        return clean[0]
    rank = max(1, math.ceil(q * len(clean)))
    return clean[min(rank - 1, len(clean) - 1)]


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_rtt(paths: Iterable[str | Path]) -> list[float]:
    out: list[float] = []
    for path in paths:
        for row in read_csv(path):
            if _text(row.get("status")).upper() != "OK":
                continue
            if _text(row.get("is_warmup")).lower() in {"true", "1", "yes"}:
                continue
            value = _finite(row.get("rtt_ms"))
            if value is not None and value >= 0:
                out.append(value)
    return out


def survival_horizons(row: dict[str, Any]) -> list[int]:
    out = []
    for key in row:
        m = SURVIVAL_RE.match(key)
        if m:
            out.append(int(m.group(1)))
    return sorted(set(out))


def choose_conservative_horizon(horizons: list[int], budget_ms: float) -> Optional[int]:
    for h in sorted(horizons):
        if h >= budget_ms - 1e-12:
            return h
    return None


def index_phase13(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (_text(row.get("candidate_id")), _text(row.get("leg_key")))
        if key in out:
            raise ValueError(f"duplicate Phase 13 leg row: {key}")
        out[key] = row
    return out


def analyze_leg(
    phase12_row: dict[str, Any],
    phase13_row: Optional[dict[str, Any]],
    *,
    latency_budget_ms: float,
    min_survival: float,
    min_complete_triggers: int,
    min_depletions: int,
    min_corroboration: float,
) -> dict[str, Any]:
    horizons = survival_horizons(phase12_row)
    chosen = choose_conservative_horizon(horizons, latency_budget_ms)
    complete_triggers = _integer(phase12_row.get("complete_triggers"), 0)
    survival = _finite(phase12_row.get(f"survival_{chosen}ms")) if chosen is not None else None
    phase13_depletions = _integer(phase13_row.get("depletion_events"), 0) if phase13_row else 0
    phase13_corr = _finite(phase13_row.get("corroboration_rate")) if phase13_row else None

    if phase13_row is None:
        decision = "INPUT_INCOMPLETE"
        reason = "matching Phase 13 leg summary is missing"
    elif phase13_depletions < min_depletions or phase13_corr is None or phase13_corr < min_corroboration:
        decision = "PHASE13_GATE_NOT_MET"
        reason = (
            f"Phase 13 evidence insufficient: depletions={phase13_depletions}, "
            f"corroboration={phase13_corr}"
        )
    elif complete_triggers < min_complete_triggers:
        decision = "COLLECT_MORE_TOUCH_SURVIVAL_DATA"
        reason = f"complete_triggers={complete_triggers} < {min_complete_triggers}"
    elif chosen is None:
        decision = "RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON"
        reason = f"latency budget {latency_budget_ms:.3f}ms exceeds max measured horizon"
    elif survival is None:
        decision = "INPUT_INCOMPLETE"
        reason = f"survival_{chosen}ms is missing"
    elif survival < min_survival:
        decision = "LATENCY_BUDGET_NOT_SURVIVED"
        reason = f"survival_{chosen}ms={survival:.3f} < {min_survival:.3f}"
    else:
        decision = "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH"
        reason = (
            f"full RTT proxy budget {latency_budget_ms:.3f}ms mapped conservatively to "
            f"{chosen}ms survival={survival:.3f}"
        )

    return {
        "candidate_id": _text(phase12_row.get("candidate_id")),
        "underlying": _text(phase12_row.get("underlying")),
        "expiry": _text(phase12_row.get("expiry")),
        "leg_key": _text(phase12_row.get("leg_key")),
        "option_type": _text(phase12_row.get("option_type")),
        "strike": _finite(phase12_row.get("strike")),
        "action": _text(phase12_row.get("action")).upper(),
        "qty": max(1, _integer(phase12_row.get("qty"), 1)),
        "latency_budget_ms": latency_budget_ms,
        "conservative_survival_horizon_ms": chosen,
        "survival_at_conservative_horizon": survival,
        "phase12_complete_triggers": complete_triggers,
        "phase13_depletion_events": phase13_depletions,
        "phase13_corroboration_rate": phase13_corr,
        "phase14_leg_decision": decision,
        "phase14_leg_reason": reason,
        "phase14_is_order_arrival_latency": False,
        "phase14_is_fill_probability": False,
        "phase14_one_way_inference_used": False,
        "phase14_live_money_allowed": False,
    }


def classify_candidate(rows: list[dict[str, Any]]) -> tuple[str, str]:
    if not rows:
        return "NO_ANALYZABLE_LEGS", "no Phase 12 leg rows"
    priorities = [
        "INPUT_INCOMPLETE",
        "PHASE13_GATE_NOT_MET",
        "COLLECT_MORE_TOUCH_SURVIVAL_DATA",
        "RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON",
        "LATENCY_BUDGET_NOT_SURVIVED",
    ]
    for code in priorities:
        bad = [r for r in rows if r.get("phase14_leg_decision") == code]
        if bad:
            return code, f"{len(bad)} leg(s) returned {code}"
    if all(r.get("phase14_leg_decision") == "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH" for r in rows):
        return (
            "LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH",
            "every leg survived the conservative full-RTT-proxy budget at the configured threshold",
        )
    return "INPUT_INCOMPLETE", "unexpected per-leg decision mix"


def analyze(
    phase12_rows: list[dict[str, Any]],
    phase13_rows: list[dict[str, Any]],
    rtt_samples: list[float],
    *,
    latency_quantile: float,
    extra_budget_ms: float,
    min_survival: float,
    min_complete_triggers: int,
    min_depletions: int,
    min_corroboration: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not rtt_samples:
        return [], [], {
            "phase14_overall_decision": "COLLECT_API_RTT_DATA",
            "phase14_reason": "no successful non-warmup RTT proxy samples",
            "phase14_live_money_allowed": False,
            "phase14_is_order_arrival_latency": False,
            "phase14_is_fill_probability": False,
            "phase14_one_way_inference_used": False,
        }
    q = empirical_quantile(rtt_samples, latency_quantile)
    assert q is not None
    budget = q + extra_budget_ms
    p13 = index_phase13(phase13_rows)
    leg_rows: list[dict[str, Any]] = []
    for row in phase12_rows:
        key = (_text(row.get("candidate_id")), _text(row.get("leg_key")))
        leg_rows.append(
            analyze_leg(
                row,
                p13.get(key),
                latency_budget_ms=budget,
                min_survival=min_survival,
                min_complete_triggers=min_complete_triggers,
                min_depletions=min_depletions,
                min_corroboration=min_corroboration,
            )
        )

    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in leg_rows:
        by_candidate[_text(row.get("candidate_id"))].append(row)
    candidate_rows: list[dict[str, Any]] = []
    for candidate_id, group in sorted(by_candidate.items()):
        decision, reason = classify_candidate(group)
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "underlying": _text(group[0].get("underlying")),
                "expiry": _text(group[0].get("expiry")),
                "leg_count": len(group),
                "phase14_candidate_decision": decision,
                "phase14_candidate_reason": reason,
                "latency_quantile": latency_quantile,
                "selected_rtt_quantile_ms": q,
                "extra_budget_ms": extra_budget_ms,
                "latency_budget_ms": budget,
                "min_leg_survival_at_budget_horizon": min(
                    (float(r["survival_at_conservative_horizon"]) for r in group if r.get("survival_at_conservative_horizon") is not None),
                    default=None,
                ),
                "phase14_live_money_allowed": False,
                "phase14_is_order_arrival_latency": False,
                "phase14_is_fill_probability": False,
                "phase14_one_way_inference_used": False,
            }
        )

    counts: dict[str, int] = defaultdict(int)
    for row in candidate_rows:
        counts[_text(row.get("phase14_candidate_decision"))] += 1
    if counts.get("LATENCY_BUDGET_SURVIVES_FOR_NEXT_RESEARCH", 0) > 0:
        overall = "LATENCY_BUDGET_CANDIDATE_EXISTS"
        reason = "at least one candidate cleared the conservative RTT-proxy/touch-survival gate"
    elif candidate_rows:
        overall = "NO_LATENCY_BUDGET_CANDIDATE"
        reason = "no candidate cleared the conservative RTT-proxy/touch-survival gate"
    else:
        overall = "NO_ANALYZABLE_CANDIDATES"
        reason = "no candidate summaries were produced"
    summary = {
        "phase14_overall_decision": overall,
        "phase14_reason": reason,
        "phase14_live_money_allowed": False,
        "phase14_is_order_arrival_latency": False,
        "phase14_is_exchange_latency": False,
        "phase14_is_fill_probability": False,
        "phase14_one_way_inference_used": False,
        "rtt_sample_count": len(rtt_samples),
        "latency_quantile": latency_quantile,
        "selected_rtt_quantile_ms": q,
        "extra_budget_ms": extra_budget_ms,
        "latency_budget_ms": budget,
        "min_survival": min_survival,
        "candidate_decision_counts": dict(counts),
        "interpretation": (
            "The full reqCurrentTime round-trip proxy plus a safety margin is used as a conservative budget. "
            "The result only asks whether previously observed displayed touch commonly survived at least that long. "
            "It does not infer one-way order latency, exchange arrival, queue priority, or fill probability."
        ),
    }
    return leg_rows, candidate_rows, summary


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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase12_csv")
    p.add_argument("phase13_csv")
    p.add_argument("rtt_inputs", nargs="+")
    p.add_argument("--latency-quantile", type=float, default=0.95)
    p.add_argument("--extra-budget-ms", type=float, default=100.0)
    p.add_argument("--min-survival", type=float, default=0.80)
    p.add_argument("--min-complete-triggers", type=int, default=20)
    p.add_argument("--min-depletions", type=int, default=5)
    p.add_argument("--min-corroboration", type=float, default=0.50)
    p.add_argument("--legs-output", default="data/phase14_latency_legs.csv")
    p.add_argument("--output", default="data/phase14_latency_candidates.csv")
    p.add_argument("--summary-json", default="data/phase14_summary.json")
    args = p.parse_args()
    if not 0 <= args.latency_quantile <= 1:
        p.error("--latency-quantile must be in [0,1]")
    if args.extra_budget_ms < 0:
        p.error("--extra-budget-ms must be non-negative")
    if not 0 <= args.min_survival <= 1 or not 0 <= args.min_corroboration <= 1:
        p.error("survival/corroboration thresholds must be in [0,1]")
    if args.min_complete_triggers <= 0 or args.min_depletions <= 0:
        p.error("minimum evidence counts must be positive")
    return args


def main() -> int:
    args = _args()
    p12 = read_csv(args.phase12_csv)
    p13 = read_csv(args.phase13_csv)
    rtt = load_rtt(args.rtt_inputs)
    legs, candidates, summary = analyze(
        p12,
        p13,
        rtt,
        latency_quantile=args.latency_quantile,
        extra_budget_ms=args.extra_budget_ms,
        min_survival=args.min_survival,
        min_complete_triggers=args.min_complete_triggers,
        min_depletions=args.min_depletions,
        min_corroboration=args.min_corroboration,
    )
    write_csv(args.legs_output, legs)
    write_csv(args.output, candidates)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
