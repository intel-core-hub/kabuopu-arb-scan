#!/usr/bin/env python3
"""Phase 16: overlay Phase 15 paper callback timing on Phase 12 touch survival.

This is a conservative research diagnostic, NOT a fill-probability model.
For each observed Phase 15 first-callback time, the script adds an explicit safety
margin, then maps the resulting delay to the next-longer observed Phase 12 survival
horizon. It never interpolates between horizons and never assumes leg independence.
The package score is the weakest marginal displayed-touch survival across all legs.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

SURVIVAL_RE = re.compile(r"^survival_(\d+)ms$")
PHASE15_READY_SUFFIX = "WITHIN_PHASE14_BUDGET"
REJECTED_TRACE_CLASSES = {
    "PAPER_REJECTED_OR_INACTIVE",
    "PAPER_ERROR_WITHOUT_ORDER_ACK",
    "PAPER_NO_ORDER_ACK",
}


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


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def expand_inputs(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        matches = sorted(glob.glob(item))
        out.extend(matches if matches else [item])
    return list(dict.fromkeys(out))


def survival_curve(row: dict[str, Any]) -> dict[int, float]:
    curve: dict[int, float] = {}
    for key, value in row.items():
        m = SURVIVAL_RE.match(str(key))
        if not m:
            continue
        x = _finite(value)
        if x is None:
            continue
        if not 0.0 <= x <= 1.0:
            raise ValueError(f"invalid survival probability {key}={value!r}")
        curve[int(m.group(1))] = float(x)
    return dict(sorted(curve.items()))


def ceiling_survival(curve: dict[int, float], delay_ms: float) -> tuple[Optional[int], Optional[float]]:
    """Return the first observed horizon >= delay. Never interpolate/extrapolate."""
    if delay_ms < 0:
        raise ValueError("delay_ms must be non-negative")
    for horizon, value in sorted(curve.items()):
        if horizon + 1e-12 >= delay_ms:
            return horizon, value
    return None, None


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


def bootstrap_mean_interval(
    values: list[float], *, reps: int, confidence: float, seed: int
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not values:
        return None, None, None
    if reps <= 0:
        mean = sum(values) / len(values)
        return mean, mean, mean
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0,1)")
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(reps):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    alpha = (1.0 - confidence) / 2.0
    return (
        empirical_quantile(means, alpha),
        empirical_quantile(means, 0.5),
        empirical_quantile(means, 1.0 - alpha),
    )


def _phase15_gate_ok(row: Optional[dict[str, Any]]) -> bool:
    if not row:
        return False
    return _text(row.get("phase15_decision")).endswith(PHASE15_READY_SUFFIX)


def _usable_phase15_timings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if _text(row.get("phase15_status")) != "PAPER_LIFECYCLE_MEASURED":
            continue
        if _text(row.get("phase15_trace_class")) in REJECTED_TRACE_CLASSES:
            continue
        timing = _finite(row.get("phase15_first_callback_ms"))
        if timing is None or timing < 0:
            continue
        item = dict(row)
        item["_timing_ms"] = float(timing)
        out.append(item)
    return out


def analyze_candidate(
    candidate_id: str,
    leg_rows: list[dict[str, Any]],
    phase15_summary: Optional[dict[str, Any]],
    trace_rows: list[dict[str, Any]],
    *,
    extra_latency_ms: float,
    min_sessions: int,
    min_complete_triggers: int,
    min_retention: float,
    max_out_of_range_fraction: float,
    bootstrap_reps: int,
    bootstrap_confidence: float,
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if extra_latency_ms < 0:
        raise ValueError("extra_latency_ms must be non-negative")

    base = {
        "candidate_id": candidate_id,
        "phase16_is_fill_probability": False,
        "phase16_is_order_arrival_probability": False,
        "phase16_is_joint_leg_probability": False,
        "phase16_independence_assumption_used": False,
        "phase16_live_money_allowed": False,
    }

    if not _phase15_gate_ok(phase15_summary):
        return ({
            **base,
            "phase16_candidate_decision": "PHASE15_GATE_NOT_MET",
            "phase16_reason": "Phase 15 did not establish repeated paper callback acknowledgement within the Phase 14 budget",
        }, [], [])

    usable = _usable_phase15_timings(trace_rows)
    if len(usable) < min_sessions:
        return ({
            **base,
            "phase16_candidate_decision": "COLLECT_MORE_PAPER_TIMING_DATA",
            "phase16_reason": f"usable Phase 15 timing sessions={len(usable)} < required {min_sessions}",
            "usable_timing_sessions": len(usable),
        }, [], [])

    if not leg_rows:
        return ({
            **base,
            "phase16_candidate_decision": "INPUT_INCOMPLETE",
            "phase16_reason": "no Phase 12 leg rows for candidate",
        }, [], [])

    curves: dict[str, dict[int, float]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(leg_rows):
        leg_key = _text(row.get("leg_key")) or f"leg_{idx + 1}"
        if _integer(row.get("complete_triggers"), 0) < min_complete_triggers:
            return ({
                **base,
                "phase16_candidate_decision": "COLLECT_MORE_TOUCH_SURVIVAL_DATA",
                "phase16_reason": f"{leg_key} has fewer than {min_complete_triggers} complete Phase 12 triggers",
            }, [], [])
        curve = survival_curve(row)
        if not curve:
            return ({
                **base,
                "phase16_candidate_decision": "INPUT_INCOMPLETE",
                "phase16_reason": f"{leg_key} has no survival_*ms fields",
            }, [], [])
        curves[leg_key] = curve
        metadata[leg_key] = row

    per_leg_values: dict[str, list[float]] = defaultdict(list)
    per_leg_horizons: dict[str, list[int]] = defaultdict(list)
    sample_rows: list[dict[str, Any]] = []
    weakest_values: list[float] = []
    out_of_range = 0

    for sample_idx, trace in enumerate(usable, start=1):
        timing = float(trace["_timing_ms"])
        effective = timing + extra_latency_ms
        matched: dict[str, dict[str, Any]] = {}
        values: list[float] = []
        complete = True
        for leg_key, curve in curves.items():
            horizon, value = ceiling_survival(curve, effective)
            if horizon is None or value is None:
                complete = False
                matched[leg_key] = {"matched_horizon_ms": None, "survival": None}
                continue
            matched[leg_key] = {"matched_horizon_ms": horizon, "survival": value}
            values.append(value)
            per_leg_values[leg_key].append(value)
            per_leg_horizons[leg_key].append(horizon)
        if not complete or len(values) != len(curves):
            out_of_range += 1
            weakest = None
        else:
            weakest = min(values)
            weakest_values.append(weakest)
        sample_rows.append({
            "candidate_id": candidate_id,
            "sample_index": sample_idx,
            "source_file": _text(trace.get("_source_file")),
            "paper_first_callback_ms": timing,
            "extra_latency_ms": extra_latency_ms,
            "effective_control_path_ms": effective,
            "all_legs_within_observed_horizons": complete,
            "weakest_leg_survival_at_ceiling_horizon": weakest,
            "matched_legs_json": json.dumps(matched, ensure_ascii=False, separators=(",", ":")),
            "phase16_is_fill_probability": False,
        })

    total_samples = len(usable)
    valid_samples = len(weakest_values)
    out_fraction = (out_of_range / total_samples) if total_samples else 1.0
    if not valid_samples or out_fraction > max_out_of_range_fraction:
        return ({
            **base,
            "phase16_candidate_decision": "RECOLLECT_LONGER_TOUCH_SURVIVAL_HORIZON",
            "phase16_reason": (
                f"{out_of_range}/{total_samples} timing samples exceed at least one observed Phase 12 horizon; "
                f"allowed fraction={max_out_of_range_fraction:.3f}"
            ),
            "usable_timing_sessions": total_samples,
            "valid_overlay_samples": valid_samples,
            "out_of_range_samples": out_of_range,
            "out_of_range_fraction": out_fraction,
        }, [], sample_rows)

    lower, median, upper = bootstrap_mean_interval(
        weakest_values,
        reps=bootstrap_reps,
        confidence=bootstrap_confidence,
        seed=seed,
    )
    mean_score = sum(weakest_values) / len(weakest_values)

    leg_out: list[dict[str, Any]] = []
    for leg_key in sorted(curves):
        vals = per_leg_values[leg_key]
        horizons = per_leg_horizons[leg_key]
        meta = metadata[leg_key]
        leg_out.append({
            "candidate_id": candidate_id,
            "leg_key": leg_key,
            "option_type": _text(meta.get("option_type")),
            "strike": _finite(meta.get("strike")),
            "action": _text(meta.get("action")),
            "qty": _integer(meta.get("qty"), 1),
            "overlay_samples": len(vals),
            "mean_marginal_touch_retention": (sum(vals) / len(vals)) if vals else None,
            "min_marginal_touch_retention": min(vals) if vals else None,
            "max_matched_horizon_ms": max(horizons) if horizons else None,
            "phase16_is_fill_probability": False,
            "phase16_is_joint_leg_probability": False,
        })

    if lower is not None and lower >= min_retention:
        decision = "TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH"
        reason = (
            f"bootstrap lower bound of weakest-leg marginal retention={lower:.3f} "
            f">= threshold {min_retention:.3f}"
        )
    else:
        decision = "TOUCH_RETENTION_NOT_ROBUST_TO_CONTROL_PATH"
        reason = (
            f"bootstrap lower bound of weakest-leg marginal retention={lower if lower is not None else float('nan'):.3f} "
            f"< threshold {min_retention:.3f}"
        )

    candidate = {
        **base,
        "phase16_candidate_decision": decision,
        "phase16_reason": reason,
        "usable_timing_sessions": total_samples,
        "valid_overlay_samples": valid_samples,
        "out_of_range_samples": out_of_range,
        "out_of_range_fraction": out_fraction,
        "extra_latency_ms": extra_latency_ms,
        "mean_weakest_leg_marginal_retention": mean_score,
        "bootstrap_confidence": bootstrap_confidence,
        "bootstrap_lower_weakest_leg_retention": lower,
        "bootstrap_median_weakest_leg_retention": median,
        "bootstrap_upper_weakest_leg_retention": upper,
        "min_required_retention": min_retention,
        "leg_count": len(curves),
        "interpretation": (
            "This overlays the empirical paper first-callback timing distribution on marginal displayed-touch survival. "
            "It is not a joint probability that all legs remain executable, not exchange-arrival latency, and not fill probability."
        ),
    }
    return candidate, leg_out, sample_rows


def analyze(
    phase12_rows: list[dict[str, Any]],
    phase15_summary_rows: list[dict[str, Any]],
    trace_rows: list[dict[str, Any]],
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    p12: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in phase12_rows:
        cid = _text(row.get("candidate_id"))
        if cid:
            p12[cid].append(row)
    p15s: dict[str, dict[str, Any]] = {}
    for row in phase15_summary_rows:
        cid = _text(row.get("candidate_id"))
        if cid and cid not in p15s:
            p15s[cid] = row
    traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trace_rows:
        cid = _text(row.get("candidate_id"))
        if cid:
            traces[cid].append(row)

    candidate_ids = sorted(set(p12) | set(p15s) | set(traces))
    candidates: list[dict[str, Any]] = []
    legs: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for idx, cid in enumerate(candidate_ids):
        candidate, leg_rows, sample_rows = analyze_candidate(
            cid,
            p12.get(cid, []),
            p15s.get(cid),
            traces.get(cid, []),
            seed=int(kwargs.get("seed", 16)) + idx,
            **{k: v for k, v in kwargs.items() if k != "seed"},
        )
        candidates.append(candidate)
        legs.extend(leg_rows)
        samples.extend(sample_rows)

    counts: dict[str, int] = defaultdict(int)
    for row in candidates:
        counts[_text(row.get("phase16_candidate_decision"))] += 1
    if any(_text(r.get("phase16_candidate_decision")) == "TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH" for r in candidates):
        overall = "END_TO_END_RESEARCH_CANDIDATE_EXISTS"
        reason = "at least one candidate retained a conservative weakest-leg displayed-touch score through the paper control-path timing overlay"
    elif candidates:
        overall = "NO_END_TO_END_RESEARCH_CANDIDATE_YET"
        reason = "no candidate cleared the conservative Phase 16 touch-retention overlay"
    else:
        overall = "NO_ANALYZABLE_CANDIDATES"
        reason = "no candidate IDs were found"
    summary = {
        "phase16_overall_decision": overall,
        "phase16_reason": reason,
        "candidate_decision_counts": dict(counts),
        "phase16_is_fill_probability": False,
        "phase16_is_order_arrival_probability": False,
        "phase16_is_joint_leg_probability": False,
        "phase16_independence_assumption_used": False,
        "phase16_live_money_allowed": False,
        "interpretation": (
            "Phase 16 combines two empirical but limited observations: displayed-touch survival and paper/TWS first-callback timing. "
            "It deliberately avoids interpolation, leg-independence assumptions, exchange-arrival claims, and fill-probability claims."
        ),
    }
    return candidates, legs, samples, summary


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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase12_touch_survival")
    p.add_argument("phase15_summary")
    p.add_argument("--phase15-traces", nargs="+", required=True)
    p.add_argument("--extra-latency-ms", type=float, default=100.0)
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-complete-triggers", type=int, default=20)
    p.add_argument("--min-retention", type=float, default=0.80)
    p.add_argument("--max-out-of-range-fraction", type=float, default=0.05)
    p.add_argument("--bootstrap-reps", type=int, default=2000)
    p.add_argument("--bootstrap-confidence", type=float, default=0.90)
    p.add_argument("--seed", type=int, default=16)
    p.add_argument("--legs-output", default="data/phase16_touch_latency_legs.csv")
    p.add_argument("--samples-output", default="data/phase16_touch_latency_samples.csv")
    p.add_argument("--output", default="data/phase16_candidates.csv")
    p.add_argument("--summary-json", default="data/phase16_summary.json")
    args = p.parse_args()
    if args.extra_latency_ms < 0 or args.min_sessions <= 0 or args.min_complete_triggers <= 0 or args.bootstrap_reps < 0:
        p.error("latency must be non-negative; session/trigger counts positive; bootstrap reps non-negative")
    if not 0 <= args.min_retention <= 1:
        p.error("--min-retention must be in [0,1]")
    if not 0 <= args.max_out_of_range_fraction <= 1:
        p.error("--max-out-of-range-fraction must be in [0,1]")
    if not 0 < args.bootstrap_confidence < 1:
        p.error("--bootstrap-confidence must be in (0,1)")
    return args


def main() -> int:
    args = _args()
    phase12_rows = read_csv(args.phase12_touch_survival)
    phase15_summary_rows = read_csv(args.phase15_summary)
    trace_rows: list[dict[str, Any]] = []
    for path in expand_inputs(args.phase15_traces):
        for row in read_csv(path):
            row["_source_file"] = path
            trace_rows.append(row)
    candidates, legs, samples, summary = analyze(
        phase12_rows,
        phase15_summary_rows,
        trace_rows,
        extra_latency_ms=args.extra_latency_ms,
        min_sessions=args.min_sessions,
        min_complete_triggers=args.min_complete_triggers,
        min_retention=args.min_retention,
        max_out_of_range_fraction=args.max_out_of_range_fraction,
        bootstrap_reps=args.bootstrap_reps,
        bootstrap_confidence=args.bootstrap_confidence,
        seed=args.seed,
    )
    write_csv(args.output, candidates)
    write_csv(args.legs_output, legs)
    write_csv(args.samples_output, samples)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
