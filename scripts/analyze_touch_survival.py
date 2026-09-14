#!/usr/bin/env python3
"""Phase 12b: offline displayed-touch survival analysis from Phase 12 depth traces.

This is deliberately not a fill-probability estimator. For BUY legs it measures how
long the displayed best ask remains at-or-better than the trigger ask with enough
visible size; for SELL legs it does the analogous test on the bid.
"""
from __future__ import annotations

import argparse
import csv
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


def _integer(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_horizons(value: str) -> list[int]:
    out: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        x = int(token)
        if x <= 0:
            raise ValueError("horizons must be positive milliseconds")
        out.append(x)
    if not out:
        raise ValueError("at least one horizon is required")
    return sorted(set(out))


def load_events(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
            rows.extend(csv.DictReader(f))
    if not rows:
        raise ValueError("no depth events found")
    return rows


def executable_state(row: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    action = _text(row.get("action")).upper()
    if action == "BUY":
        return _finite(row.get("top_ask")), _finite(row.get("top_ask_size"))
    if action == "SELL":
        return _finite(row.get("top_bid")), _finite(row.get("top_bid_size"))
    raise ValueError(f"invalid action: {action!r}")


def no_worse(action: str, current: Optional[float], baseline: float) -> bool:
    if current is None:
        return False
    if action == "BUY":
        return current <= baseline
    if action == "SELL":
        return current >= baseline
    raise ValueError(f"invalid action: {action!r}")


def build_grid(rows: list[dict[str, Any]], *, step_ms: int) -> list[dict[str, Any]]:
    """Forward-fill event states onto a regular grid over the observed interval."""
    if step_ms <= 0:
        raise ValueError("step_ms must be positive")
    ordered = sorted(rows, key=lambda r: float(r.get("elapsed_ms") or 0.0))
    if not ordered:
        return []
    start = float(ordered[0].get("elapsed_ms") or 0.0)
    end = float(ordered[-1].get("elapsed_ms") or 0.0)
    out: list[dict[str, Any]] = []
    idx = 0
    current = ordered[0]
    t = start
    while t <= end + 1e-9:
        while idx + 1 < len(ordered) and float(ordered[idx + 1].get("elapsed_ms") or 0.0) <= t:
            idx += 1
            current = ordered[idx]
        sample = dict(current)
        sample["grid_elapsed_ms"] = t
        out.append(sample)
        t += step_ms
    return out


def analyze_leg(rows: list[dict[str, Any]], *, horizons_ms: list[int], step_ms: int) -> dict[str, Any]:
    if not rows:
        raise ValueError("leg has no rows")
    first = rows[0]
    action = _text(first.get("action")).upper()
    qty = max(1, _integer(first.get("qty"), 1))
    grid = build_grid(rows, step_ms=step_ms)
    max_h = max(horizons_ms)
    triggers = 0
    complete_triggers = 0
    survival_counts = {h: 0 for h in horizons_ms}
    lifetimes: list[float] = []

    for i, row in enumerate(grid):
        md_type = _integer(row.get("actual_market_data_type"), 0)
        px, size = executable_state(row)
        if md_type != 1 or px is None or px <= 0 or size is None or size < qty:
            continue
        triggers += 1
        t0 = float(row["grid_elapsed_ms"])
        if float(grid[-1]["grid_elapsed_ms"]) - t0 < max_h:
            continue
        complete_triggers += 1
        baseline = px
        failure_ms = max_h
        failed = False
        for later in grid[i + 1:]:
            dt = float(later["grid_elapsed_ms"]) - t0
            if dt > max_h:
                break
            md = _integer(later.get("actual_market_data_type"), 0)
            cur_px, cur_size = executable_state(later)
            if md != 1 or not no_worse(action, cur_px, baseline) or cur_size is None or cur_size < qty:
                failure_ms = max(0.0, dt)
                failed = True
                break
        if not failed:
            failure_ms = float(max_h)
        lifetimes.append(failure_ms)
        for h in horizons_ms:
            if failure_ms >= h:
                survival_counts[h] += 1

    probs = {
        f"survival_{h}ms": (survival_counts[h] / complete_triggers if complete_triggers else None)
        for h in horizons_ms
    }
    sorted_life = sorted(lifetimes)
    median = None
    p10 = None
    if sorted_life:
        median = sorted_life[(len(sorted_life) - 1) // 2]
        p10 = sorted_life[max(0, math.ceil(0.10 * len(sorted_life)) - 1)]
    return {
        "candidate_id": _text(first.get("candidate_id")),
        "underlying": _text(first.get("underlying")),
        "expiry": _text(first.get("expiry")),
        "leg_key": _text(first.get("leg_key")),
        "option_type": _text(first.get("option_type")),
        "strike": _finite(first.get("strike")),
        "action": action,
        "qty": qty,
        "event_rows": len(rows),
        "grid_rows": len(grid),
        "eligible_triggers": triggers,
        "complete_triggers": complete_triggers,
        "median_continuous_touch_ms_capped": median,
        "p10_continuous_touch_ms_capped": p10,
        **probs,
        "phase12_is_fill_probability": False,
        "phase12_live_money_allowed": False,
    }


def classify_candidate(leg_rows: list[dict[str, Any]], *, target_horizon_ms: int,
                       min_survival: float, min_complete_triggers: int) -> tuple[str, str]:
    if not leg_rows:
        return "NO_ANALYZABLE_LEGS", "no leg summaries"
    field = f"survival_{target_horizon_ms}ms"
    for row in leg_rows:
        if _integer(row.get("complete_triggers"), 0) < min_complete_triggers:
            return "COLLECT_MORE_DEPTH_DATA", f"{row.get('leg_key')} has too few complete triggers"
        value = row.get(field)
        if value is None:
            return "COLLECT_MORE_DEPTH_DATA", f"{row.get('leg_key')} lacks target-horizon survival"
        if float(value) < min_survival:
            return "DISPLAYED_TOUCH_NOT_ROBUST", f"{row.get('leg_key')} {field}={float(value):.3f}"
    return (
        "TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH",
        "every leg met the displayed-touch survival threshold; queue position and fills remain unmodelled",
    )


def analyze(rows: list[dict[str, Any]], *, horizons_ms: list[int], step_ms: int,
            target_horizon_ms: int, min_survival: float,
            min_complete_triggers: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if target_horizon_ms not in horizons_ms:
        raise ValueError("target horizon must be included in horizons")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(_text(row.get("candidate_id")), _text(row.get("leg_key")))].append(row)
    leg_summaries = [
        analyze_leg(g, horizons_ms=horizons_ms, step_ms=step_ms)
        for _, g in sorted(groups.items())
    ]
    decision, reason = classify_candidate(
        leg_summaries, target_horizon_ms=target_horizon_ms,
        min_survival=min_survival, min_complete_triggers=min_complete_triggers,
    )
    summary = {
        "phase12_overall_decision": decision,
        "phase12_reason": reason,
        "phase12_live_money_allowed": False,
        "phase12_is_fill_probability": False,
        "target_horizon_ms": target_horizon_ms,
        "min_survival": min_survival,
        "leg_count": len(leg_summaries),
        "interpretation": (
            "Survival is the empirical fraction of eligible displayed-touch states that remained "
            "continuously executable at the same-or-better price with enough visible size through the horizon. "
            "It is not fill probability and contains no queue-priority or hidden-liquidity inference."
        ),
    }
    return leg_summaries, summary


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        p.write_text("", encoding="utf-8-sig")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+")
    p.add_argument("--horizons-ms", default="100,250,500,1000,2000")
    p.add_argument("--grid-step-ms", type=int, default=50)
    p.add_argument("--target-horizon-ms", type=int, default=500)
    p.add_argument("--min-survival", type=float, default=0.80)
    p.add_argument("--min-complete-triggers", type=int, default=20)
    p.add_argument("--output", default="data/phase12_touch_survival.csv")
    p.add_argument("--summary-json", default="data/phase12_touch_survival_summary.json")
    args = p.parse_args()
    if args.grid_step_ms <= 0 or args.target_horizon_ms <= 0 or args.min_complete_triggers <= 0:
        p.error("grid/target/min triggers must be positive")
    if not (0 <= args.min_survival <= 1):
        p.error("--min-survival must be in [0,1]")
    return args


def main() -> int:
    args = _args()
    horizons = parse_horizons(args.horizons_ms)
    rows = load_events(args.inputs)
    leg_rows, summary = analyze(
        rows, horizons_ms=horizons, step_ms=args.grid_step_ms,
        target_horizon_ms=args.target_horizon_ms, min_survival=args.min_survival,
        min_complete_triggers=args.min_complete_triggers,
    )
    write_csv(args.output, leg_rows)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
