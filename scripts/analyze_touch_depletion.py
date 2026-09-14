#!/usr/bin/env python3
"""Phase 13b: offline quote-trade concordance analysis for displayed touch depletion.

The analysis does not estimate fill probability.  It identifies adverse displayed-touch
changes (size loss at the same price, or a move to a worse executable price) and asks
whether a nearby Last/Last Size callback at the old touch price corroborates turnover.
An unmatched depletion is deliberately labelled "uncorroborated", not "cancelled",
because callback timing, aggregation and feed semantics do not permit that inference.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


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


def load_events(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
            rows.extend(csv.DictReader(f))
    if not rows:
        raise ValueError("no Phase 13 events found")
    return rows


def executable_state(row: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    action = _text(row.get("action")).upper()
    if action == "BUY":
        return _finite(row.get("top_ask")), _finite(row.get("top_ask_size"))
    if action == "SELL":
        return _finite(row.get("top_bid")), _finite(row.get("top_bid_size"))
    raise ValueError(f"invalid action: {action!r}")


def worsened(action: str, old_price: float, new_price: Optional[float], *, eps: float = 1e-12) -> bool:
    if new_price is None:
        return True
    if action == "BUY":
        return new_price > old_price + eps
    if action == "SELL":
        return new_price < old_price - eps
    raise ValueError(f"invalid action: {action!r}")


def same_price(a: Optional[float], b: Optional[float], tolerance: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def extract_trade_prints(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if _text(row.get("event_kind")) != "LAST_SIZE":
            continue
        if _integer(row.get("actual_market_data_type"), 0) != 1:
            continue
        price = _finite(row.get("last_price"))
        size = _finite(row.get("last_size"))
        elapsed = _finite(row.get("elapsed_ms"))
        if price is None or price <= 0 or size is None or size <= 0 or elapsed is None:
            continue
        out.append(
            {
                "print_id": i,
                "elapsed_ms": elapsed,
                "price": price,
                "size": size,
                "used": False,
            }
        )
    return out


def detect_depletions(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted(rows, key=lambda r: float(r.get("elapsed_ms") or 0.0))
    previous: Optional[dict[str, Any]] = None
    replenishments = 0
    out: list[dict[str, Any]] = []
    for row in ordered:
        if _integer(row.get("actual_market_data_type"), 0) != 1:
            continue
        if _text(row.get("event_kind")) not in {"BASELINE", "DEPTH", "TERMINAL"}:
            continue
        price, size = executable_state(row)
        if previous is None:
            if price is not None and price > 0 and size is not None and size >= 0:
                previous = {"row": row, "price": price, "size": size}
            continue
        old_price = float(previous["price"])
        old_size = float(previous["size"])
        action = _text(row.get("action")).upper()
        elapsed = _finite(row.get("elapsed_ms"))
        if elapsed is None:
            continue
        if price is not None and same_price(price, old_price, 1e-12) and size is not None:
            if size < old_size - 1e-12:
                out.append(
                    {
                        "elapsed_ms": elapsed,
                        "kind": "SIZE_DECREASE",
                        "old_touch_price": old_price,
                        "new_touch_price": price,
                        "old_touch_size": old_size,
                        "new_touch_size": size,
                        "depleted_size": old_size - size,
                    }
                )
            elif size > old_size + 1e-12:
                replenishments += 1
        elif worsened(action, old_price, price):
            out.append(
                {
                    "elapsed_ms": elapsed,
                    "kind": "PRICE_WORSEN",
                    "old_touch_price": old_price,
                    "new_touch_price": price,
                    "old_touch_size": old_size,
                    "new_touch_size": size,
                    "depleted_size": old_size,
                }
            )
        if price is not None and price > 0 and size is not None and size >= 0:
            previous = {"row": row, "price": price, "size": size}
        elif price is None:
            previous = None
    return out, replenishments


def match_prints(
    depletions: list[dict[str, Any]],
    prints: list[dict[str, Any]],
    *,
    window_ms: float,
    price_tolerance: float,
) -> list[dict[str, Any]]:
    """Greedily match each trade callback to at most one nearest depletion event."""
    out: list[dict[str, Any]] = []
    for dep_index, dep in enumerate(depletions):
        t = float(dep["elapsed_ms"])
        px = float(dep["old_touch_price"])
        candidates = [
            p
            for p in prints
            if not p["used"]
            and abs(float(p["elapsed_ms"]) - t) <= window_ms
            and same_price(float(p["price"]), px, price_tolerance)
        ]
        candidates.sort(key=lambda p: abs(float(p["elapsed_ms"]) - t))
        matched_volume = 0.0
        matched_ids: list[int] = []
        target = max(0.0, float(dep.get("depleted_size") or 0.0))
        for p in candidates:
            p["used"] = True
            matched_ids.append(int(p["print_id"]))
            matched_volume += float(p["size"])
            if target > 0 and matched_volume >= target:
                break
        row = dict(dep)
        row["depletion_index"] = dep_index
        row["matched_print_count"] = len(matched_ids)
        row["matched_print_volume"] = matched_volume
        row["matched_print_ids_json"] = json.dumps(matched_ids)
        row["print_corroborated"] = bool(matched_ids)
        row["matched_volume_to_depletion_ratio_capped"] = (
            min(1.0, matched_volume / target) if target > 0 else None
        )
        out.append(row)
    return out


def analyze_leg(
    rows: list[dict[str, Any]], *, window_ms: float, price_tolerance: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not rows:
        raise ValueError("leg has no rows")
    ordered = sorted(rows, key=lambda r: float(r.get("elapsed_ms") or 0.0))
    first = ordered[0]
    depletions, replenishments = detect_depletions(ordered)
    prints = extract_trade_prints(ordered)
    matched = match_prints(
        depletions,
        prints,
        window_ms=window_ms,
        price_tolerance=price_tolerance,
    )
    corroborated = sum(1 for d in matched if d["print_corroborated"])
    total_depleted = sum(float(d.get("depleted_size") or 0.0) for d in matched)
    total_matched = sum(float(d.get("matched_print_volume") or 0.0) for d in matched)
    summary = {
        "candidate_id": _text(first.get("candidate_id")),
        "underlying": _text(first.get("underlying")),
        "expiry": _text(first.get("expiry")),
        "leg_key": _text(first.get("leg_key")),
        "option_type": _text(first.get("option_type")),
        "strike": _finite(first.get("strike")),
        "action": _text(first.get("action")).upper(),
        "qty": max(1, _integer(first.get("qty"), 1)),
        "event_rows": len(ordered),
        "trade_print_callbacks": len(prints),
        "depletion_events": len(matched),
        "size_decrease_events": sum(1 for d in matched if d["kind"] == "SIZE_DECREASE"),
        "price_worsen_events": sum(1 for d in matched if d["kind"] == "PRICE_WORSEN"),
        "replenishment_events": replenishments,
        "corroborated_depletion_events": corroborated,
        "corroboration_rate": (corroborated / len(matched) if matched else None),
        "observed_depleted_size": total_depleted,
        "matched_last_size_volume": total_matched,
        "matched_volume_to_depleted_size_ratio_capped": (
            min(1.0, total_matched / total_depleted) if total_depleted > 0 else None
        ),
        "match_window_ms": window_ms,
        "price_tolerance": price_tolerance,
        "phase13_is_fill_probability": False,
        "phase13_live_money_allowed": False,
    }
    detail = []
    for d in matched:
        detail.append(
            {
                "candidate_id": summary["candidate_id"],
                "leg_key": summary["leg_key"],
                "action": summary["action"],
                **d,
            }
        )
    return summary, detail


def classify_candidate(
    leg_rows: list[dict[str, Any]], *, min_depletions: int, min_corroboration: float
) -> tuple[str, str]:
    if not leg_rows:
        return "NO_ANALYZABLE_LEGS", "no leg summaries"
    for row in leg_rows:
        n = _integer(row.get("depletion_events"), 0)
        if n < min_depletions:
            return (
                "COLLECT_MORE_TURNOVER_DATA",
                f"{row.get('leg_key')} has only {n} depletion events",
            )
        rate = row.get("corroboration_rate")
        if rate is None:
            return "COLLECT_MORE_TURNOVER_DATA", f"{row.get('leg_key')} has no corroboration rate"
        if float(rate) < min_corroboration:
            return (
                "DISPLAYED_DEPLETION_MOSTLY_UNCORROBORATED",
                f"{row.get('leg_key')} corroboration_rate={float(rate):.3f}",
            )
    return (
        "TOUCH_DEPLETION_CORROBORATED_ENOUGH_FOR_LATENCY_STUDY",
        "every leg met the nearby Last/Last Size corroboration threshold; this remains a callback-time heuristic, not fill probability",
    )


def analyze(
    rows: list[dict[str, Any]], *, window_ms: float, price_tolerance: float,
    min_depletions: int, min_corroboration: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(_text(row.get("candidate_id")), _text(row.get("leg_key")))].append(row)
    leg_summaries: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for _, group in sorted(groups.items()):
        summary, detail = analyze_leg(group, window_ms=window_ms, price_tolerance=price_tolerance)
        leg_summaries.append(summary)
        details.extend(detail)
    decision, reason = classify_candidate(
        leg_summaries,
        min_depletions=min_depletions,
        min_corroboration=min_corroboration,
    )
    summary = {
        "phase13_overall_decision": decision,
        "phase13_reason": reason,
        "phase13_live_money_allowed": False,
        "phase13_is_fill_probability": False,
        "phase13_cancellation_inference_allowed": False,
        "match_window_ms": window_ms,
        "price_tolerance": price_tolerance,
        "min_depletions": min_depletions,
        "min_corroboration": min_corroboration,
        "leg_count": len(leg_summaries),
        "interpretation": (
            "A nearby Last/Last Size callback can corroborate that trading occurred at the depleted touch, "
            "but unmatched depth changes are not labelled cancellations and matched callbacks do not prove "
            "that a hypothetical order would have filled."
        ),
    }
    return leg_summaries, details, summary


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
    p.add_argument("inputs", nargs="+")
    p.add_argument("--match-window-ms", type=float, default=250.0)
    p.add_argument("--price-tolerance", type=float, default=1e-9)
    p.add_argument("--min-depletions", type=int, default=5)
    p.add_argument("--min-corroboration", type=float, default=0.50)
    p.add_argument("--output", default="data/phase13_touch_depletion_summary.csv")
    p.add_argument("--details-output", default="data/phase13_touch_depletion_events_matched.csv")
    p.add_argument("--summary-json", default="data/phase13_summary.json")
    args = p.parse_args()
    if args.match_window_ms < 0 or args.price_tolerance < 0 or args.min_depletions <= 0:
        p.error("match window/tolerance must be non-negative and min depletions positive")
    if not (0 <= args.min_corroboration <= 1):
        p.error("--min-corroboration must be in [0,1]")
    return args


def main() -> int:
    args = _args()
    rows = load_events(args.inputs)
    leg_rows, details, summary = analyze(
        rows,
        window_ms=args.match_window_ms,
        price_tolerance=args.price_tolerance,
        min_depletions=args.min_depletions,
        min_corroboration=args.min_corroboration,
    )
    write_csv(args.output, leg_rows)
    write_csv(args.details_output, details)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
