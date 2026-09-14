#!/usr/bin/env python3
"""Reconstruct locally observed continuous package-valid intervals from Phase 18 events.

The analysis is fully offline. It replays timestamped IBKR quote callbacks and splits
intervals not only at callbacks but also when the last executable price/size update
would age past max_state_age_sec. A valid interval requires every leg to be live,
executable at displayed size, and the package edge to remain positive after fees.

This is not exchange-time continuity, fill probability, or evidence of atomic execution.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

PHASE17_READY = "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"
PHASE18_READY = "EVENT_DRIVEN_PACKAGE_INTERVALS_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"


@dataclass
class LegState:
    action: str
    qty: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    market_data_type: Optional[int] = None
    bid_time: Optional[float] = None
    ask_time: Optional[float] = None
    bid_size_time: Optional[float] = None
    ask_size_time: Optional[float] = None


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


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_csv(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def expand_inputs(patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if not matches and Path(pattern).exists():
            matches = [pattern]
        for match in matches:
            resolved = str(Path(match).resolve())
            if resolved not in seen:
                seen.add(resolved)
                out.append(match)
    return out


def parse_phase17_gate(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = _text(row.get("candidate_id"))
        if cid and cid not in out:
            out[cid] = dict(row)
    return out


def initial_states(events: Sequence[dict[str, Any]]) -> dict[int, LegState]:
    states: dict[int, LegState] = {}
    for row in events:
        idx = _integer(row.get("leg_index"), -1)
        if idx < 0:
            continue
        action = _text(row.get("action")).upper()
        qty = _integer(row.get("qty"), 0)
        if action not in {"BUY", "SELL"} or qty <= 0:
            continue
        states.setdefault(idx, LegState(action=action, qty=qty))
    return states


def apply_event(states: dict[int, LegState], row: dict[str, Any]) -> None:
    idx = _integer(row.get("leg_index"), -1)
    if idx < 0 or idx not in states:
        return
    kind = _text(row.get("event_kind"))
    value = _finite(row.get("value"))
    t = _finite(row.get("elapsed_sec"))
    if t is None:
        return
    s = states[idx]
    if kind == "MARKET_DATA_TYPE" and value is not None:
        s.market_data_type = int(value)
    elif kind == "BID_PRICE":
        s.bid = value
        s.bid_time = t
    elif kind == "ASK_PRICE":
        s.ask = value
        s.ask_time = t
    elif kind == "BID_SIZE":
        s.bid_size = value
        s.bid_size_time = t
    elif kind == "ASK_SIZE":
        s.ask_size = value
        s.ask_size_time = t


def executable_view(state: LegState) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if state.action == "BUY":
        return state.ask, state.ask_size, state.ask_time, state.ask_size_time
    return state.bid, state.bid_size, state.bid_time, state.bid_size_time


def evaluate_package(
    states: dict[int, LegState], *, now: float, floor_pv_per_share: float,
    lot_size: float, fee_per_contract_leg: float, max_state_age_sec: float,
) -> tuple[bool, str, Optional[float]]:
    if not states:
        return False, "NO_LEGS", None
    debit = 0.0
    fee_legs = 0
    for idx in sorted(states):
        s = states[idx]
        if s.market_data_type != 1:
            return False, f"LEG_{idx}_NONLIVE_OR_UNVERIFIED", None
        px, size, px_t, size_t = executable_view(s)
        if px is None or px_t is None:
            return False, f"LEG_{idx}_MISSING_PRICE", None
        if size is None or size_t is None:
            return False, f"LEG_{idx}_MISSING_SIZE", None
        if s.action == "BUY" and px <= 0:
            return False, f"LEG_{idx}_INVALID_BUY_PRICE", None
        if s.action == "SELL" and px < 0:
            return False, f"LEG_{idx}_INVALID_SELL_PRICE", None
        if size < s.qty:
            return False, f"LEG_{idx}_INSUFFICIENT_SIZE", None
        if now - px_t >= max_state_age_sec - 1e-12:
            return False, f"LEG_{idx}_STALE_PRICE", None
        if now - size_t >= max_state_age_sec - 1e-12:
            return False, f"LEG_{idx}_STALE_SIZE", None
        if s.action == "BUY":
            debit += s.qty * px
        else:
            debit -= s.qty * px
        fee_legs += s.qty
    gross = (float(floor_pv_per_share) - debit) * float(lot_size)
    net = gross - fee_legs * float(fee_per_contract_leg)
    if net <= 0:
        return False, "NONPOSITIVE_EDGE", net
    return True, "LIVE_POSITIVE_EXECUTABLE_PACKAGE", net


def next_expiry_time(states: dict[int, LegState], now: float, max_state_age_sec: float) -> Optional[float]:
    expiries: list[float] = []
    for s in states.values():
        _, _, px_t, size_t = executable_view(s)
        for t in (px_t, size_t):
            if t is not None and t + max_state_age_sec > now + 1e-12:
                expiries.append(t + max_state_age_sec)
    return min(expiries) if expiries else None


def _session_bounds(events: Sequence[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    starts = [_finite(r.get("elapsed_sec")) for r in events if _text(r.get("event_kind")) == "ANALYSIS_START"]
    ends = [_finite(r.get("elapsed_sec")) for r in events if _text(r.get("event_kind")) == "ANALYSIS_END"]
    starts = [x for x in starts if x is not None]
    ends = [x for x in ends if x is not None]
    if not starts or not ends:
        return None, None
    start = min(starts)
    end = max(ends)
    if end <= start:
        return None, None
    return start, end


def analyze_session(events: Sequence[dict[str, Any]], *, max_state_age_sec: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not events:
        return {"phase18_session_status": "NO_EVENTS"}, []
    ordered = sorted(events, key=lambda r: (_finite(r.get("elapsed_sec")) or -1.0))
    first = ordered[0]
    cid = _text(first.get("candidate_id"))
    session_id = _text(first.get("session_id"))
    base = {
        "candidate_id": cid,
        "session_id": session_id,
        "monitor_started_at_utc": _text(first.get("monitor_started_at_utc")),
        "phase18_is_event_driven_local_observation": True,
        "phase18_is_exchange_time": False,
        "phase18_is_fill_probability": False,
        "phase18_is_continuous_exchange_survival": False,
        "phase18_live_money_allowed": False,
    }
    start, end = _session_bounds(ordered)
    if start is None or end is None:
        return {**base, "phase18_session_status": "MISSING_ANALYSIS_MARKERS"}, []
    floor = _finite(first.get("floor_pv_per_share"))
    lot = _finite(first.get("lot_size"))
    fee = _finite(first.get("fee_per_contract_leg"))
    if floor is None or lot is None or fee is None:
        return {**base, "phase18_session_status": "MISSING_PRICING_METADATA"}, []
    states = initial_states(ordered)
    if not states:
        return {**base, "phase18_session_status": "NO_LEG_METADATA"}, []

    # Replay warmup events before the scored interval to seed current state.
    pos = 0
    while pos < len(ordered):
        t = _finite(ordered[pos].get("elapsed_sec"))
        if t is None or t > start + 1e-12:
            break
        apply_event(states, ordered[pos])
        pos += 1

    boundaries = {float(start), float(end)}
    for row in ordered:
        t = _finite(row.get("elapsed_sec"))
        if t is not None and start < t < end:
            boundaries.add(float(t))
    # Expiry boundaries depend on state, so build dynamically while replaying.
    timeline = sorted(boundaries)
    cursor = float(start)
    current_valid, current_reason, current_edge = evaluate_package(
        states, now=cursor, floor_pv_per_share=floor, lot_size=lot,
        fee_per_contract_leg=fee, max_state_age_sec=max_state_age_sec,
    )
    interval_start: Optional[float] = cursor if current_valid else None
    interval_min_edge: Optional[float] = current_edge if current_valid else None
    intervals: list[dict[str, Any]] = []

    # Group scored events by exact local receipt time.
    by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in ordered:
        t = _finite(row.get("elapsed_sec"))
        if t is not None and start < t <= end + 1e-12:
            by_time[float(t)].append(row)
    event_times = sorted(by_time)
    evt_idx = 0

    while cursor < end - 1e-12:
        next_event = event_times[evt_idx] if evt_idx < len(event_times) else None
        expiry = next_expiry_time(states, cursor, max_state_age_sec)
        candidates = [end]
        if next_event is not None and next_event > cursor + 1e-12:
            candidates.append(next_event)
        if expiry is not None and expiry > cursor + 1e-12:
            candidates.append(expiry)
        nxt = min(candidates)
        # State is constant and fresh over [cursor, nxt), so valid time accrues.
        cursor = nxt
        # Apply callbacks at this boundary before evaluating the state after it.
        while evt_idx < len(event_times) and abs(event_times[evt_idx] - cursor) <= 1e-12:
            for row in by_time[event_times[evt_idx]]:
                apply_event(states, row)
            evt_idx += 1
        new_valid, new_reason, new_edge = evaluate_package(
            states, now=cursor, floor_pv_per_share=floor, lot_size=lot,
            fee_per_contract_leg=fee, max_state_age_sec=max_state_age_sec,
        )
        if current_valid and not new_valid:
            assert interval_start is not None
            intervals.append({
                **base,
                "interval_start_sec": interval_start,
                "interval_end_sec": cursor,
                "interval_duration_sec": max(0.0, cursor - interval_start),
                "min_net_edge_per_contract": interval_min_edge,
                "end_reason": new_reason,
            })
            interval_start = None
            interval_min_edge = None
        elif not current_valid and new_valid:
            interval_start = cursor
            interval_min_edge = new_edge
        elif current_valid and new_valid and new_edge is not None:
            interval_min_edge = new_edge if interval_min_edge is None else min(interval_min_edge, new_edge)
        current_valid, current_reason, current_edge = new_valid, new_reason, new_edge

    if current_valid and interval_start is not None and end > interval_start + 1e-12:
        intervals.append({
            **base,
            "interval_start_sec": interval_start,
            "interval_end_sec": end,
            "interval_duration_sec": end - interval_start,
            "min_net_edge_per_contract": interval_min_edge,
            "end_reason": "ANALYSIS_END",
        })

    observed = end - start
    valid_time = sum(float(x["interval_duration_sec"]) for x in intervals)
    longest = max((float(x["interval_duration_sec"]) for x in intervals), default=0.0)
    min_edge = min((float(x["min_net_edge_per_contract"]) for x in intervals if x.get("min_net_edge_per_contract") is not None), default=None)
    return {
        **base,
        "analysis_start_sec": start,
        "analysis_end_sec": end,
        "observed_duration_sec": observed,
        "valid_package_time_sec": valid_time,
        "valid_package_time_ratio": valid_time / observed if observed > 0 else None,
        "valid_interval_count": len(intervals),
        "longest_valid_interval_sec": longest,
        "min_interval_net_edge_per_contract": min_edge,
        "event_rows": len(ordered),
        "phase18_session_status": "ANALYZED",
    }, intervals


def bootstrap_mean_interval(values: Sequence[float], *, reps: int, confidence: float, seed: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return None, None, None
    if reps <= 0 or len(vals) == 1:
        m = sum(vals) / len(vals)
        return m, m, m
    rng = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(reps):
        draw = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(draw) / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    def pick(q: float) -> float:
        idx = min(len(means) - 1, max(0, int(round(q * (len(means) - 1)))))
        return means[idx]
    return pick(alpha), pick(0.5), pick(1.0 - alpha)


def aggregate_candidate(
    candidate_id: str, phase17_row: Optional[dict[str, Any]], sessions: Sequence[dict[str, Any]], *,
    min_sessions: int, min_events_per_session: int, min_valid_time_ratio: float,
    target_interval_sec: float, min_target_session_ratio: float,
    bootstrap_reps: int, bootstrap_confidence: float, seed: int,
) -> dict[str, Any]:
    base = {
        "candidate_id": candidate_id,
        "phase17_candidate_decision": _text((phase17_row or {}).get("phase17_candidate_decision")),
        "phase18_is_event_driven_local_observation": True,
        "phase18_is_exchange_time": False,
        "phase18_is_fill_probability": False,
        "phase18_is_continuous_exchange_survival": False,
        "phase18_live_money_allowed": False,
    }
    if not phase17_row or _text(phase17_row.get("phase17_candidate_decision")) != PHASE17_READY:
        return {**base, "phase18_candidate_decision": "PHASE17_GATE_NOT_MET", "phase18_reason": "candidate did not clear Phase 17"}
    usable = [
        s for s in sessions
        if _text(s.get("phase18_session_status")) == "ANALYZED" and _integer(s.get("event_rows"), 0) >= min_events_per_session
    ]
    if len(usable) < min_sessions:
        return {
            **base, "sessions_total": len(sessions), "sessions_usable": len(usable),
            "phase18_candidate_decision": "COLLECT_MORE_EVENT_DRIVEN_SESSIONS",
            "phase18_reason": f"usable event-driven sessions={len(usable)} < required {min_sessions}",
        }
    ratios = [float(s["valid_package_time_ratio"]) for s in usable if _finite(s.get("valid_package_time_ratio")) is not None]
    if len(ratios) < min_sessions:
        return {**base, "sessions_usable": len(ratios), "phase18_candidate_decision": "INPUT_INCOMPLETE", "phase18_reason": "usable sessions lack valid_package_time_ratio"}
    target_hits = [1.0 if float(s.get("longest_valid_interval_sec") or 0.0) >= target_interval_sec else 0.0 for s in usable]
    target_ratio = sum(target_hits) / len(target_hits)
    lower, med, upper = bootstrap_mean_interval(ratios, reps=bootstrap_reps, confidence=bootstrap_confidence, seed=seed)
    if lower is not None and lower >= min_valid_time_ratio and target_ratio >= min_target_session_ratio:
        decision = PHASE18_READY
        reason = (
            f"session-bootstrap lower valid-time ratio={lower:.3f} >= {min_valid_time_ratio:.3f}; "
            f"sessions with >= {target_interval_sec:g}s valid interval={target_ratio:.3f} >= {min_target_session_ratio:.3f}"
        )
    else:
        decision = "EVENT_DRIVEN_PACKAGE_INTERVALS_NOT_ROBUST"
        reason = (
            f"lower valid-time ratio={lower if lower is not None else float('nan'):.3f}; "
            f"target-duration session ratio={target_ratio:.3f}"
        )
    return {
        **base,
        "sessions_total": len(sessions),
        "sessions_usable": len(usable),
        "mean_valid_package_time_ratio": sum(ratios) / len(ratios),
        "min_valid_package_time_ratio": min(ratios),
        "bootstrap_lower_valid_package_time_ratio": lower,
        "bootstrap_median_valid_package_time_ratio": med,
        "bootstrap_upper_valid_package_time_ratio": upper,
        "target_interval_sec": target_interval_sec,
        "sessions_with_target_interval": int(sum(target_hits)),
        "target_interval_session_ratio": target_ratio,
        "phase18_candidate_decision": decision,
        "phase18_reason": reason,
    }


def group_event_sessions(paths: Iterable[str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_files: set[str] = set()
    for path in expand_inputs(paths):
        resolved = str(Path(path).resolve())
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        for row in read_csv(path):
            cid = _text(row.get("candidate_id"))
            sid = _text(row.get("session_id")) or _text(row.get("monitor_started_at_utc"))
            if cid and sid:
                groups[(cid, sid)].append(row)
    return groups


def write_csv(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
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


def analyze(
    phase17_rows: Sequence[dict[str, Any]], event_groups: dict[tuple[str, str], list[dict[str, Any]]], *,
    max_state_age_sec: float, min_sessions: int, min_events_per_session: int,
    min_valid_time_ratio: float, target_interval_sec: float, min_target_session_ratio: float,
    bootstrap_reps: int, bootstrap_confidence: float, seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    gates = parse_phase17_gate(phase17_rows)
    sessions_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    session_rows: list[dict[str, Any]] = []
    interval_rows: list[dict[str, Any]] = []
    for (cid, _sid), events in sorted(event_groups.items()):
        session, intervals = analyze_session(events, max_state_age_sec=max_state_age_sec)
        sessions_by_candidate[cid].append(session)
        session_rows.append(session)
        interval_rows.extend(intervals)
    candidate_ids = sorted(set(gates) | set(sessions_by_candidate))
    candidate_rows = []
    for idx, cid in enumerate(candidate_ids):
        candidate_rows.append(aggregate_candidate(
            cid, gates.get(cid), sessions_by_candidate.get(cid, []),
            min_sessions=min_sessions, min_events_per_session=min_events_per_session,
            min_valid_time_ratio=min_valid_time_ratio, target_interval_sec=target_interval_sec,
            min_target_session_ratio=min_target_session_ratio, bootstrap_reps=bootstrap_reps,
            bootstrap_confidence=bootstrap_confidence, seed=seed + idx,
        ))
    counts: dict[str, int] = defaultdict(int)
    for row in candidate_rows:
        counts[_text(row.get("phase18_candidate_decision"))] += 1
    ready = any(_text(r.get("phase18_candidate_decision")) == PHASE18_READY for r in candidate_rows)
    summary = {
        "phase18_overall_decision": "EVENT_DRIVEN_PACKAGE_RESEARCH_CANDIDATE_EXISTS" if ready else "NO_EVENT_DRIVEN_PACKAGE_RESEARCH_CANDIDATE_YET",
        "candidate_decision_counts": dict(counts),
        "max_state_age_sec": max_state_age_sec,
        "phase18_is_event_driven_local_observation": True,
        "phase18_is_exchange_time": False,
        "phase18_is_fill_probability": False,
        "phase18_is_continuous_exchange_survival": False,
        "phase18_live_money_allowed": False,
    }
    return candidate_rows, session_rows, interval_rows, summary


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase17_candidates")
    p.add_argument("--event-inputs", nargs="+", required=True, help="Phase 18 event CSV files/globs")
    p.add_argument("--max-state-age-sec", type=float, default=3.0)
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-events-per-session", type=int, default=20)
    p.add_argument("--min-valid-time-ratio", type=float, default=0.80)
    p.add_argument("--target-interval-sec", type=float, default=1.0)
    p.add_argument("--min-target-session-ratio", type=float, default=2/3)
    p.add_argument("--bootstrap-reps", type=int, default=2000)
    p.add_argument("--bootstrap-confidence", type=float, default=0.90)
    p.add_argument("--seed", type=int, default=1818)
    p.add_argument("--sessions-output", required=True)
    p.add_argument("--intervals-output", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--summary-json")
    args = p.parse_args()
    if args.max_state_age_sec <= 0 or args.min_sessions <= 0 or args.min_events_per_session <= 0 or args.target_interval_sec <= 0:
        p.error("age/session/event/target parameters must be positive")
    if not 0 <= args.min_valid_time_ratio <= 1 or not 0 <= args.min_target_session_ratio <= 1:
        p.error("ratio thresholds must be in [0,1]")
    if not 0 < args.bootstrap_confidence < 1 or args.bootstrap_reps < 0:
        p.error("bootstrap confidence must be in (0,1) and reps non-negative")
    return args


def main() -> int:
    args = _args()
    candidates, sessions, intervals, summary = analyze(
        read_csv(args.phase17_candidates), group_event_sessions(args.event_inputs),
        max_state_age_sec=args.max_state_age_sec, min_sessions=args.min_sessions,
        min_events_per_session=args.min_events_per_session, min_valid_time_ratio=args.min_valid_time_ratio,
        target_interval_sec=args.target_interval_sec, min_target_session_ratio=args.min_target_session_ratio,
        bootstrap_reps=args.bootstrap_reps, bootstrap_confidence=args.bootstrap_confidence, seed=args.seed,
    )
    write_csv(args.output, candidates)
    write_csv(args.sessions_output, sessions)
    write_csv(args.intervals_output, intervals)
    if args.summary_json:
        p = Path(args.summary_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(candidates)} candidate row(s), {len(sessions)} session row(s), {len(intervals)} interval row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
