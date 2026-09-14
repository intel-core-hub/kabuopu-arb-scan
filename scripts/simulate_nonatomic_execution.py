#!/usr/bin/env python3
"""Phase 10: offline quote-replay stress test for non-atomic leg execution.

This tool never connects to IBKR and never submits, modifies, or cancels orders.
It combines Phase 9 candidate decisions with enriched Phase 4 monitoring CSVs.
For every LIVE_POSITIVE trigger sample it replays all leg-order permutations,
spacing successive leg fills by a configurable delay, and reprices each leg from
that later sample's executable top-of-book side.

The result is a *quote-touch replay*, not an execution forecast. It does not model
queue position, fill probability, cancellations between samples, hidden liquidity,
market impact, or adverse selection. No Phase 10 outcome authorizes live trading.
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

ELIGIBLE_PHASE9_DECISIONS = {
    "NON_ATOMIC_EXECUTION_RESEARCH_ONLY",
    "NO_GO_EXCHANGE_ATOMIC_COMBO",
    "STOP_BROKER_COMBO_UNSUPPORTED",
}


@dataclass(frozen=True)
class Leg:
    action: str
    option_type: str
    strike: float
    qty: int


@dataclass(frozen=True)
class ReplayConfig:
    leg_delay_sec: float = 0.5
    max_leg_quote_age_ms: float = 3000.0
    extra_slippage_per_contract_leg: float = 0.0
    max_permutations: int = 120


@dataclass(frozen=True)
class DecisionThresholds:
    min_sessions: int = 3
    min_completion_ratio: float = 0.95
    min_positive_path_ratio: float = 0.90
    min_robust_trigger_ratio: float = 0.50
    min_median_worst_edge_jpy: float = 1000.0
    min_p10_edge_jpy: float = 0.0
    stop_positive_path_ratio: float = 0.50


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "t", "yes", "y"}


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if not 0 <= q <= 1:
        raise ValueError("q must be between 0 and 1")
    xs = sorted(float(x) for x in values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def parse_legs_json(value: Any) -> list[Leg]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid legs_json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("legs_json must be a non-empty JSON array")
    legs: list[Leg] = []
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
        if strike is None or strike <= 0:
            raise ValueError(f"leg {i} has invalid strike")
        if qty <= 0:
            raise ValueError(f"leg {i} has non-positive qty")
        legs.append(Leg(action, option_type, strike, qty))
    return legs


def parse_samples_json(value: Any) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid monitor_samples_json: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("monitor_samples_json must be a JSON array")
    return [x for x in raw if isinstance(x, dict)]


def _snapshot_map(sample: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw = sample.get("leg_snapshots")
    if not isinstance(raw, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("leg_index"))
        except (TypeError, ValueError):
            continue
        out[idx] = item
    return out


def _sample_elapsed(sample: dict[str, Any]) -> float | None:
    return _finite(sample.get("elapsed_sec"))


def _sample_at_or_after(samples: Sequence[dict[str, Any]], start_index: int, target_elapsed: float) -> tuple[int, dict[str, Any]] | None:
    for idx in range(start_index, len(samples)):
        elapsed = _sample_elapsed(samples[idx])
        if elapsed is not None and elapsed + 1e-12 >= target_elapsed:
            return idx, samples[idx]
    return None


def _validate_snapshot(snapshot: dict[str, Any], leg: Leg, *, max_age_ms: float) -> tuple[bool, str, float | None]:
    if _text(snapshot.get("action")).upper() != leg.action:
        return False, "LEG_ACTION_MISMATCH", None
    if _text(snapshot.get("option_type")).upper()[:1] != leg.option_type:
        return False, "LEG_TYPE_MISMATCH", None
    strike = _finite(snapshot.get("strike"))
    if strike is None or abs(strike - leg.strike) > 1e-9:
        return False, "LEG_STRIKE_MISMATCH", None
    try:
        qty = int(snapshot.get("qty"))
    except (TypeError, ValueError):
        return False, "LEG_QTY_INVALID", None
    if qty != leg.qty:
        return False, "LEG_QTY_MISMATCH", None
    try:
        market_data_type = int(snapshot.get("market_data_type"))
    except (TypeError, ValueError):
        return False, "UNVERIFIED_DATA_TYPE", None
    if market_data_type != 1:
        return False, "NONLIVE_DATA", None
    px = _finite(snapshot.get("executable_price"))
    if px is None or (px <= 0 if leg.action == "BUY" else px < 0):
        return False, "MISSING_EXECUTABLE_PRICE", None
    size = _finite(snapshot.get("executable_size"))
    if size is None or size < leg.qty:
        return False, "NO_EXECUTABLE_SIZE", None
    price_age = _finite(snapshot.get("price_age_ms"))
    size_age = _finite(snapshot.get("size_age_ms"))
    if price_age is None or price_age > max_age_ms:
        return False, "STALE_PRICE", None
    if size_age is None or size_age > max_age_ms:
        return False, "STALE_SIZE", None
    return True, "OK", px


def enumerate_orders(leg_count: int, max_permutations: int) -> list[tuple[int, ...]]:
    if leg_count <= 0:
        raise ValueError("leg_count must be positive")
    total = math.factorial(leg_count)
    if total > max_permutations:
        raise ValueError(
            f"{leg_count} legs require {total} permutations, exceeding max_permutations={max_permutations}"
        )
    return list(itertools.permutations(range(leg_count)))


def replay_path(
    samples: Sequence[dict[str, Any]],
    legs: Sequence[Leg],
    *,
    trigger_index: int,
    order: Sequence[int],
    floor_pv_per_share: float,
    lot_size: float,
    fee_per_contract_leg: float,
    config: ReplayConfig,
) -> dict[str, Any]:
    if trigger_index < 0 or trigger_index >= len(samples):
        raise IndexError("trigger_index out of range")
    trigger_elapsed = _sample_elapsed(samples[trigger_index])
    if trigger_elapsed is None:
        return {"path_status": "INCOMPLETE", "incomplete_reason": "TRIGGER_TIME_MISSING"}

    debit = 0.0
    fills: list[dict[str, Any]] = []
    fill_times: list[float] = []
    for position, leg_index in enumerate(order):
        leg = legs[leg_index]
        target_elapsed = trigger_elapsed + position * config.leg_delay_sec
        found = _sample_at_or_after(samples, trigger_index, target_elapsed)
        if found is None:
            return {
                "path_status": "INCOMPLETE",
                "incomplete_reason": "MONITOR_WINDOW_ENDED",
                "failed_leg_index": int(leg_index),
                "fills_json": json.dumps(fills, ensure_ascii=False, separators=(",", ":")),
            }
        sample_index, sample = found
        snap = _snapshot_map(sample).get(int(leg_index))
        if snap is None:
            return {
                "path_status": "INCOMPLETE",
                "incomplete_reason": "LEG_SNAPSHOT_MISSING",
                "failed_leg_index": int(leg_index),
                "failed_sample_index": int(sample_index),
                "fills_json": json.dumps(fills, ensure_ascii=False, separators=(",", ":")),
            }
        ok, reason, px = _validate_snapshot(snap, leg, max_age_ms=config.max_leg_quote_age_ms)
        if not ok or px is None:
            return {
                "path_status": "INCOMPLETE",
                "incomplete_reason": reason,
                "failed_leg_index": int(leg_index),
                "failed_sample_index": int(sample_index),
                "fills_json": json.dumps(fills, ensure_ascii=False, separators=(",", ":")),
            }
        if leg.action == "BUY":
            debit += leg.qty * px
        else:
            debit -= leg.qty * px
        elapsed = _sample_elapsed(sample)
        assert elapsed is not None
        fill_times.append(elapsed)
        fills.append(
            {
                "sequence_position": position,
                "leg_index": int(leg_index),
                "action": leg.action,
                "option_type": leg.option_type,
                "strike": leg.strike,
                "qty": leg.qty,
                "sample_index": sample_index,
                "elapsed_sec": elapsed,
                "price": px,
            }
        )

    total_leg_qty = sum(leg.qty for leg in legs)
    gross_edge = (float(floor_pv_per_share) - debit) * float(lot_size)
    fees = total_leg_qty * float(fee_per_contract_leg)
    extra_slippage = total_leg_qty * float(config.extra_slippage_per_contract_leg)
    net_edge = gross_edge - fees - extra_slippage
    fill_window = max(fill_times) - min(fill_times) if fill_times else 0.0
    return {
        "path_status": "COMPLETE_POSITIVE" if net_edge > 0 else "COMPLETE_NONPOSITIVE",
        "net_debit_per_share": debit,
        "gross_edge_per_contract": gross_edge,
        "fees_per_contract": fees,
        "extra_slippage_per_contract": extra_slippage,
        "net_edge_per_contract": net_edge,
        "fill_window_sec": fill_window,
        "fills_json": json.dumps(fills, ensure_ascii=False, separators=(",", ":")),
    }


def replay_session(row: dict[str, Any], *, config: ReplayConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_id = _text(row.get("candidate_id"))
    session_base = {
        "candidate_id": candidate_id,
        "source_file": _text(row.get("source_file")),
        "monitor_started_at_utc": _text(row.get("monitor_started_at_utc")),
        "monitor_finished_at_utc": _text(row.get("monitor_finished_at_utc")),
    }
    legs = parse_legs_json(row.get("legs_json"))
    try:
        orders = enumerate_orders(len(legs), config.max_permutations)
    except ValueError as exc:
        return {**session_base, "session_status": "TOO_MANY_LEGS", "session_reason": str(exc)}, []

    samples = parse_samples_json(row.get("monitor_samples_json"))
    if not samples:
        return {**session_base, "session_status": "NO_MONITOR_SAMPLES", "session_reason": "monitor_samples_json is empty"}, []
    live_positive_indices = [i for i, s in enumerate(samples) if _text(s.get("sample_status")) == "LIVE_POSITIVE"]
    if not live_positive_indices:
        return {**session_base, "session_status": "NO_LIVE_TRIGGER_SAMPLES", "session_reason": "no LIVE_POSITIVE sample"}, []
    enriched_indices = [i for i in live_positive_indices if _snapshot_map(samples[i])]
    if not enriched_indices:
        return {
            **session_base,
            "session_status": "NEEDS_ENRICHED_PHASE4_CAPTURE",
            "session_reason": "LIVE_POSITIVE samples do not contain leg_snapshots; recollect with Phase 10-enriched Phase 4",
        }, []

    elapsed_values = [_sample_elapsed(s) for s in samples]
    elapsed_values = [x for x in elapsed_values if x is not None]
    last_elapsed = max(elapsed_values) if elapsed_values else None
    required_horizon = max(0, len(legs) - 1) * config.leg_delay_sec
    replayable_indices = [
        i for i in enriched_indices
        if last_elapsed is not None
        and _sample_elapsed(samples[i]) is not None
        and float(_sample_elapsed(samples[i])) + required_horizon <= last_elapsed + 1e-12
    ]
    if not replayable_indices:
        return {
            **session_base,
            "session_status": "INSUFFICIENT_POST_TRIGGER_WINDOW",
            "session_reason": "LIVE_POSITIVE samples exist, but none leave enough monitoring horizon for all sequential legs",
            "trigger_samples_skipped_end_of_window": len(enriched_indices),
        }, []

    floor_pv = _finite(row.get("floor_pv_per_share"))
    lot_size = _finite(row.get("lot_size"))
    fee = _finite(row.get("effective_fee_per_contract_leg"))
    if fee is None:
        fee = _finite(row.get("fee_per_contract_leg"))
    if floor_pv is None or lot_size is None or fee is None:
        return {
            **session_base,
            "session_status": "MISSING_PRICING_METADATA",
            "session_reason": "floor_pv_per_share, lot_size and effective fee are required",
        }, []

    path_rows: list[dict[str, Any]] = []
    trigger_rows: list[dict[str, Any]] = []
    for trigger_index in replayable_indices:
        trigger_elapsed = _sample_elapsed(samples[trigger_index])
        completed_edges: list[float] = []
        completed_count = 0
        positive_count = 0
        for order in orders:
            result = replay_path(
                samples,
                legs,
                trigger_index=trigger_index,
                order=order,
                floor_pv_per_share=floor_pv,
                lot_size=lot_size,
                fee_per_contract_leg=fee,
                config=config,
            )
            path = {
                **session_base,
                "trigger_index": trigger_index,
                "trigger_elapsed_sec": trigger_elapsed,
                "order_json": json.dumps(list(order), separators=(",", ":")),
                **result,
            }
            path_rows.append(path)
            if result.get("path_status", "").startswith("COMPLETE_"):
                completed_count += 1
                edge = float(result["net_edge_per_contract"])
                completed_edges.append(edge)
                if edge > 0:
                    positive_count += 1
        trigger_rows.append(
            {
                "complete": completed_count,
                "positive": positive_count,
                "total": len(orders),
                "worst_edge": min(completed_edges) if completed_edges else None,
                "best_edge": max(completed_edges) if completed_edges else None,
                "median_edge": statistics.median(completed_edges) if completed_edges else None,
                "all_orders_complete_positive": completed_count == len(orders) and positive_count == len(orders),
            }
        )

    completed_edges = [
        float(p["net_edge_per_contract"])
        for p in path_rows
        if p.get("path_status", "").startswith("COMPLETE_") and p.get("net_edge_per_contract") is not None
    ]
    complete_paths = sum(int(t["complete"]) for t in trigger_rows)
    positive_paths = sum(int(t["positive"]) for t in trigger_rows)
    total_paths = sum(int(t["total"]) for t in trigger_rows)
    robust_triggers = sum(1 for t in trigger_rows if t["all_orders_complete_positive"])
    trigger_worst_edges = [float(t["worst_edge"]) for t in trigger_rows if t["worst_edge"] is not None]
    session = {
        **session_base,
        "session_status": "REPLAYED",
        "session_reason": "quote-touch replay completed",
        "legs_count": len(legs),
        "permutations_per_trigger": len(orders),
        "trigger_samples": len(trigger_rows),
        "trigger_samples_skipped_end_of_window": len(enriched_indices) - len(replayable_indices),
        "paths_total": total_paths,
        "paths_complete": complete_paths,
        "paths_positive": positive_paths,
        "path_completion_ratio": complete_paths / total_paths if total_paths else 0.0,
        "positive_complete_path_ratio": positive_paths / complete_paths if complete_paths else 0.0,
        "robust_trigger_count": robust_triggers,
        "robust_trigger_ratio": robust_triggers / len(trigger_rows) if trigger_rows else 0.0,
        "worst_completed_path_edge_jpy": min(completed_edges) if completed_edges else None,
        "p10_completed_path_edge_jpy": _quantile(completed_edges, 0.10),
        "median_completed_path_edge_jpy": statistics.median(completed_edges) if completed_edges else None,
        "best_completed_path_edge_jpy": max(completed_edges) if completed_edges else None,
        "median_trigger_worst_edge_jpy": statistics.median(trigger_worst_edges) if trigger_worst_edges else None,
        "leg_delay_sec": config.leg_delay_sec,
        "max_leg_quote_age_ms": config.max_leg_quote_age_ms,
        "extra_slippage_per_contract_leg": config.extra_slippage_per_contract_leg,
    }
    return session, path_rows


def _expand_inputs(items: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for item in items:
        matches = sorted(glob.glob(item))
        if not matches and Path(item).exists():
            matches = [item]
        for match in matches:
            key = str(Path(match).resolve())
            if key not in seen:
                seen.add(key)
                out.append(Path(match))
    return out


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_phase9(path: str | Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv(Path(path))
    required = {"candidate_id", "phase9_candidate_decision", "phase9_live_money_allowed", "phase9_atomicity_established"}
    if rows:
        missing = required - set(rows[0])
        if missing:
            raise ValueError(f"Phase 9 input missing required columns: {', '.join(sorted(missing))}")
    return {_text(r.get("candidate_id")): r for r in rows if _text(r.get("candidate_id"))}


def load_phase4(inputs: Iterable[str]) -> list[dict[str, Any]]:
    paths = _expand_inputs(inputs)
    rows: list[dict[str, Any]] = []
    seen_sessions: set[tuple[str, str, str]] = set()
    for path in paths:
        for row in _read_csv(path):
            row = dict(row)
            row["source_file"] = str(path)
            key = (
                str(path.resolve()),
                _text(row.get("candidate_id")),
                _text(row.get("monitor_started_at_utc")),
            )
            if key in seen_sessions:
                continue
            seen_sessions.add(key)
            rows.append(row)
    return rows


def _eligible_phase9(row: dict[str, Any] | None) -> tuple[bool, str]:
    if row is None:
        return False, "candidate missing from Phase 9"
    if _bool(row.get("phase9_live_money_allowed")) or _bool(row.get("phase9_atomicity_established")):
        return False, "Phase 9 safety flags are inconsistent with research-only mode"
    decision = _text(row.get("phase9_candidate_decision"))
    if decision not in ELIGIBLE_PHASE9_DECISIONS:
        return False, f"Phase 9 decision {decision or 'missing'} is not eligible for non-atomic replay"
    return True, decision


def aggregate_candidate(
    candidate_id: str,
    phase9_row: dict[str, Any] | None,
    sessions: Sequence[dict[str, Any]],
    *,
    thresholds: DecisionThresholds,
) -> dict[str, Any]:
    eligible, eligibility_reason = _eligible_phase9(phase9_row)
    base = {
        "candidate_id": candidate_id,
        "phase9_candidate_decision": _text((phase9_row or {}).get("phase9_candidate_decision")),
        "phase10_live_money_allowed": False,
        "phase10_atomicity_established": False,
        "phase10_model": "TOP_OF_BOOK_QUOTE_TOUCH_REPLAY",
    }
    if not eligible:
        return {**base, "phase10_candidate_decision": "NOT_ELIGIBLE_FROM_PHASE9", "phase10_candidate_reason": eligibility_reason}
    if not sessions:
        return {**base, "phase10_candidate_decision": "NO_PHASE4_SESSIONS", "phase10_candidate_reason": "no Phase 4 sessions found for candidate"}

    status_counts: dict[str, int] = {}
    for s in sessions:
        status = _text(s.get("session_status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    replayed = [s for s in sessions if s.get("session_status") == "REPLAYED"]
    needs_recollect = any(s.get("session_status") == "NEEDS_ENRICHED_PHASE4_CAPTURE" for s in sessions)
    if not replayed:
        if needs_recollect:
            decision = "NEEDS_PHASE4_RECOLLECTION"
            reason = "existing Phase 4 files lack per-leg snapshots required for legging replay"
        else:
            decision = "NO_REPLAYABLE_SESSIONS"
            reason = "no Phase 4 session could be replayed"
        return {
            **base,
            "sessions_total": len(sessions),
            "sessions_replayed": 0,
            "session_status_counts_json": json.dumps(status_counts, separators=(",", ":")),
            "phase10_candidate_decision": decision,
            "phase10_candidate_reason": reason,
        }

    total_paths = sum(int(s.get("paths_total", 0)) for s in replayed)
    complete_paths = sum(int(s.get("paths_complete", 0)) for s in replayed)
    positive_paths = sum(int(s.get("paths_positive", 0)) for s in replayed)
    triggers = sum(int(s.get("trigger_samples", 0)) for s in replayed)
    robust_triggers = sum(int(s.get("robust_trigger_count", 0)) for s in replayed)
    completion_ratio = complete_paths / total_paths if total_paths else 0.0
    positive_ratio = positive_paths / complete_paths if complete_paths else 0.0
    robust_ratio = robust_triggers / triggers if triggers else 0.0
    worst_edges = [float(s["worst_completed_path_edge_jpy"]) for s in replayed if _finite(s.get("worst_completed_path_edge_jpy")) is not None]
    p10_edges = [float(s["p10_completed_path_edge_jpy"]) for s in replayed if _finite(s.get("p10_completed_path_edge_jpy")) is not None]
    trigger_worst = [float(s["median_trigger_worst_edge_jpy"]) for s in replayed if _finite(s.get("median_trigger_worst_edge_jpy")) is not None]
    worst_edge = min(worst_edges) if worst_edges else None
    p10_edge = min(p10_edges) if p10_edges else None
    median_trigger_worst = statistics.median(trigger_worst) if trigger_worst else None

    failures: list[str] = []
    if len(replayed) < thresholds.min_sessions:
        failures.append(f"replayed_sessions={len(replayed)}<{thresholds.min_sessions}")
    if completion_ratio < thresholds.min_completion_ratio:
        failures.append(f"completion_ratio={completion_ratio:.3f}<{thresholds.min_completion_ratio:.3f}")
    if positive_ratio < thresholds.min_positive_path_ratio:
        failures.append(f"positive_path_ratio={positive_ratio:.3f}<{thresholds.min_positive_path_ratio:.3f}")
    if robust_ratio < thresholds.min_robust_trigger_ratio:
        failures.append(f"robust_trigger_ratio={robust_ratio:.3f}<{thresholds.min_robust_trigger_ratio:.3f}")
    if median_trigger_worst is None or median_trigger_worst < thresholds.min_median_worst_edge_jpy:
        failures.append(f"median_trigger_worst_edge={median_trigger_worst!r}<{thresholds.min_median_worst_edge_jpy:g}")
    if p10_edge is None or p10_edge < thresholds.min_p10_edge_jpy:
        failures.append(f"session_p10_edge_floor={p10_edge!r}<{thresholds.min_p10_edge_jpy:g}")

    if needs_recollect:
        decision = "MIXED_SCHEMA_RECOLLECT_MORE"
        reason = "some sessions are replayable but older Phase 4 files lack leg_snapshots"
    elif positive_ratio < thresholds.stop_positive_path_ratio or (p10_edge is not None and p10_edge < 0):
        decision = "STOP_NONATOMIC_EDGE_NOT_ROBUST"
        reason = "sequential quote replay materially destroys the simultaneous-quote edge"
    elif not failures:
        decision = "NONATOMIC_SIGNAL_SURVIVES_REPLAY"
        reason = "research thresholds passed under exhaustive leg-order quote replay; still not execution evidence"
    else:
        decision = "KEEP_NONATOMIC_RESEARCH"
        reason = "; ".join(failures)

    return {
        **base,
        "sessions_total": len(sessions),
        "sessions_replayed": len(replayed),
        "session_status_counts_json": json.dumps(status_counts, separators=(",", ":")),
        "trigger_samples": triggers,
        "paths_total": total_paths,
        "paths_complete": complete_paths,
        "paths_positive": positive_paths,
        "path_completion_ratio": completion_ratio,
        "positive_complete_path_ratio": positive_ratio,
        "robust_trigger_ratio": robust_ratio,
        "worst_completed_path_edge_jpy": worst_edge,
        "session_p10_edge_floor_jpy": p10_edge,
        "median_trigger_worst_edge_jpy": median_trigger_worst,
        "phase10_candidate_decision": decision,
        "phase10_candidate_reason": reason,
    }


def evaluate(
    phase9_csv: str | Path,
    phase4_inputs: Iterable[str],
    *,
    config: ReplayConfig | None = None,
    thresholds: DecisionThresholds | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    config = config or ReplayConfig()
    thresholds = thresholds or DecisionThresholds()
    phase4_inputs = list(phase4_inputs)
    phase9 = load_phase9(phase9_csv)
    phase4_rows = load_phase4(phase4_inputs)

    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in phase4_rows:
        cid = _text(row.get("candidate_id"))
        if cid:
            by_candidate.setdefault(cid, []).append(row)

    session_rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    candidate_ids = sorted(set(phase9) | set(by_candidate))
    candidate_rows: list[dict[str, Any]] = []
    for cid in candidate_ids:
        p9 = phase9.get(cid)
        eligible, _ = _eligible_phase9(p9)
        replayed_for_candidate: list[dict[str, Any]] = []
        if eligible:
            for row in by_candidate.get(cid, []):
                try:
                    session, paths = replay_session(row, config=config)
                except Exception as exc:
                    session = {
                        "candidate_id": cid,
                        "source_file": _text(row.get("source_file")),
                        "monitor_started_at_utc": _text(row.get("monitor_started_at_utc")),
                        "session_status": "ERROR",
                        "session_reason": str(exc),
                    }
                    paths = []
                session_rows.append(session)
                path_rows.extend(paths)
                replayed_for_candidate.append(session)
        candidate_rows.append(
            aggregate_candidate(cid, p9, replayed_for_candidate, thresholds=thresholds)
        )

    decisions = [_text(r.get("phase10_candidate_decision")) for r in candidate_rows]
    counts = {name: decisions.count(name) for name in sorted(set(decisions))}
    if counts.get("NONATOMIC_SIGNAL_SURVIVES_REPLAY", 0):
        overall = "NONATOMIC_RESEARCH_SURVIVES_QUOTE_REPLAY"
    elif counts.get("NEEDS_PHASE4_RECOLLECTION", 0) or counts.get("MIXED_SCHEMA_RECOLLECT_MORE", 0):
        overall = "RECOLLECT_ENRICHED_PHASE4_DATA"
    elif counts.get("KEEP_NONATOMIC_RESEARCH", 0):
        overall = "NONATOMIC_RESEARCH_INCONCLUSIVE"
    elif counts.get("STOP_NONATOMIC_EDGE_NOT_ROBUST", 0):
        overall = "NONATOMIC_EDGE_COLLAPSES_IN_QUOTE_REPLAY"
    else:
        overall = "NO_ELIGIBLE_NONATOMIC_CANDIDATE"

    summary = {
        "phase10_overall_decision": overall,
        "phase10_live_money_allowed": False,
        "phase10_atomicity_established": False,
        "phase10_model": "TOP_OF_BOOK_QUOTE_TOUCH_REPLAY",
        "phase10_limitations": [
            "no queue-position or fill-probability model",
            "no market-impact model",
            "no guarantee displayed size remains available",
            "sampled quotes can miss adverse moves between observations",
        ],
        "phase4_input_files": len(_expand_inputs(phase4_inputs)),
        "candidate_count": len(candidate_rows),
        "candidate_decision_counts": counts,
        "replay_config": {
            "leg_delay_sec": config.leg_delay_sec,
            "max_leg_quote_age_ms": config.max_leg_quote_age_ms,
            "extra_slippage_per_contract_leg": config.extra_slippage_per_contract_leg,
            "max_permutations": config.max_permutations,
        },
    }
    return candidate_rows, session_rows, path_rows, summary


def _write_csv(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
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
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase9_csv", help="Phase 9 broker-gate CSV")
    p.add_argument("--phase4-inputs", nargs="+", required=True, help="Phase 4 CSV paths or glob patterns")
    p.add_argument("--output", default="data/phase10_nonatomic_candidates.csv")
    p.add_argument("--sessions-output", default="data/phase10_nonatomic_sessions.csv")
    p.add_argument("--paths-output", default="data/phase10_nonatomic_paths.csv")
    p.add_argument("--summary-json", default="data/phase10_summary.json")
    p.add_argument("--leg-delay-sec", type=float, default=0.5)
    p.add_argument("--max-leg-quote-age-ms", type=float, default=3000.0)
    p.add_argument("--extra-slippage-per-contract-leg", type=float, default=0.0)
    p.add_argument("--max-permutations", type=int, default=120)
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-completion-ratio", type=float, default=0.95)
    p.add_argument("--min-positive-path-ratio", type=float, default=0.90)
    p.add_argument("--min-robust-trigger-ratio", type=float, default=0.50)
    p.add_argument("--min-median-worst-edge-jpy", type=float, default=1000.0)
    p.add_argument("--min-p10-edge-jpy", type=float, default=0.0)
    p.add_argument("--stop-positive-path-ratio", type=float, default=0.50)
    args = p.parse_args()
    if args.leg_delay_sec < 0:
        p.error("--leg-delay-sec must be non-negative")
    if args.max_leg_quote_age_ms <= 0:
        p.error("--max-leg-quote-age-ms must be positive")
    if args.extra_slippage_per_contract_leg < 0:
        p.error("--extra-slippage-per-contract-leg must be non-negative")
    if args.max_permutations <= 0 or args.min_sessions <= 0:
        p.error("--max-permutations and --min-sessions must be positive")
    for name in ("min_completion_ratio", "min_positive_path_ratio", "min_robust_trigger_ratio", "stop_positive_path_ratio"):
        value = getattr(args, name)
        if not 0 <= value <= 1:
            p.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    return args


def main() -> int:
    args = _args()
    config = ReplayConfig(
        leg_delay_sec=args.leg_delay_sec,
        max_leg_quote_age_ms=args.max_leg_quote_age_ms,
        extra_slippage_per_contract_leg=args.extra_slippage_per_contract_leg,
        max_permutations=args.max_permutations,
    )
    thresholds = DecisionThresholds(
        min_sessions=args.min_sessions,
        min_completion_ratio=args.min_completion_ratio,
        min_positive_path_ratio=args.min_positive_path_ratio,
        min_robust_trigger_ratio=args.min_robust_trigger_ratio,
        min_median_worst_edge_jpy=args.min_median_worst_edge_jpy,
        min_p10_edge_jpy=args.min_p10_edge_jpy,
        stop_positive_path_ratio=args.stop_positive_path_ratio,
    )
    candidates, sessions, paths, summary = evaluate(
        args.phase9_csv,
        args.phase4_inputs,
        config=config,
        thresholds=thresholds,
    )
    _write_csv(args.output, candidates)
    _write_csv(args.sessions_output, sessions)
    _write_csv(args.paths_output, paths)
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
