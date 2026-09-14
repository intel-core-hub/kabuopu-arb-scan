#!/usr/bin/env python3
"""Diagnose what first breaks Phase 18 event-driven package-valid intervals.

Fully offline Phase 19 analysis.  It replays Phase 18 callback logs using the exact
Phase 18 package-validity rules, then attributes every valid->invalid transition to
an observed failure mode.  ANALYSIS_END is treated as right censoring, not failure.

This produces local callback-time diagnostics only.  It is not an exchange hazard
rate, fill probability, cancellation inference, or evidence of atomic execution.
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.util
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


def _load_phase18_module():
    try:
        import analyze_event_driven_package_intervals as mod  # type: ignore
        return mod
    except ModuleNotFoundError:
        path = Path(__file__).with_name("analyze_event_driven_package_intervals.py")
        spec = importlib.util.spec_from_file_location("analyze_event_driven_package_intervals", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load Phase 18 analyzer from {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, mod)
        spec.loader.exec_module(mod)
        return mod


P18 = _load_phase18_module()
PHASE18_READY = "EVENT_DRIVEN_PACKAGE_INTERVALS_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"

CAUSE_PRICE = "PRICE_EDGE_COLLAPSE"
CAUSE_SIZE = "DISPLAYED_SIZE_LOSS"
CAUSE_MDT = "MARKET_DATA_TYPE_LOSS"
CAUSE_STALE_PRICE = "STALE_PRICE"
CAUSE_STALE_SIZE = "STALE_SIZE"
CAUSE_QUOTE = "QUOTE_INVALID_OR_MISSING"
CAUSE_OTHER = "OTHER"
KNOWN_CAUSES = (
    CAUSE_PRICE,
    CAUSE_SIZE,
    CAUSE_MDT,
    CAUSE_STALE_PRICE,
    CAUSE_STALE_SIZE,
    CAUSE_QUOTE,
    CAUSE_OTHER,
)


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


def group_event_sessions(paths: Iterable[str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for path in expand_inputs(paths):
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        for row in read_csv(path):
            cid = _text(row.get("candidate_id"))
            sid = _text(row.get("session_id")) or _text(row.get("monitor_started_at_utc"))
            if cid and sid:
                groups[(cid, sid)].append(row)
    return groups


def parse_phase18_gate(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = _text(row.get("candidate_id"))
        if cid and cid not in out:
            out[cid] = dict(row)
    return out


def clone_states(states: dict[int, Any]) -> dict[int, Any]:
    return {idx: replace(state) for idx, state in states.items()}


def classify_reason(reason: str) -> tuple[str, Optional[int]]:
    reason = _text(reason)
    match = re.match(r"LEG_(\d+)_(.+)$", reason)
    leg_idx = int(match.group(1)) if match else None
    suffix = match.group(2) if match else reason
    if reason == "NONPOSITIVE_EDGE":
        return CAUSE_PRICE, None
    if suffix == "INSUFFICIENT_SIZE":
        return CAUSE_SIZE, leg_idx
    if suffix == "NONLIVE_OR_UNVERIFIED":
        return CAUSE_MDT, leg_idx
    if suffix == "STALE_PRICE":
        return CAUSE_STALE_PRICE, leg_idx
    if suffix == "STALE_SIZE":
        return CAUSE_STALE_SIZE, leg_idx
    if suffix in {"MISSING_PRICE", "MISSING_SIZE", "INVALID_BUY_PRICE", "INVALID_SELL_PRICE"}:
        return CAUSE_QUOTE, leg_idx
    return CAUSE_OTHER, leg_idx


def _event_leg_indices(rows: Sequence[dict[str, Any]]) -> list[int]:
    return sorted({idx for idx in (_integer(r.get("leg_index"), -1) for r in rows) if idx >= 0})


def _event_kinds(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted({_text(r.get("event_kind")) for r in rows if _text(r.get("event_kind"))})


def _price_breaker_legs(rows: Sequence[dict[str, Any]], states: dict[int, Any]) -> list[int]:
    out: set[int] = set()
    for row in rows:
        idx = _integer(row.get("leg_index"), -1)
        if idx < 0 or idx not in states:
            continue
        kind = _text(row.get("event_kind"))
        action = _text(states[idx].action).upper()
        if (action == "BUY" and kind == "ASK_PRICE") or (action == "SELL" and kind == "BID_PRICE"):
            out.add(idx)
    return sorted(out)


def make_failure_row(
    *, base: dict[str, Any], boundary_sec: float, interval_start_sec: float,
    reason: str, edge_before: Optional[float], edge_after: Optional[float],
    boundary_events: Sequence[dict[str, Any]], states_after: dict[int, Any],
    expiry_hit: bool,
) -> dict[str, Any]:
    cause, reason_leg = classify_reason(reason)
    event_legs = _event_leg_indices(boundary_events)
    event_kinds = _event_kinds(boundary_events)
    if cause == CAUSE_PRICE:
        breaker_legs = _price_breaker_legs(boundary_events, states_after)
    elif reason_leg is not None:
        breaker_legs = [reason_leg]
    else:
        breaker_legs = event_legs

    if cause in {CAUSE_STALE_PRICE, CAUSE_STALE_SIZE}:
        attribution = "STALENESS_BOUNDARY_EXACT" if reason_leg is not None else "STALENESS_BOUNDARY_AMBIGUOUS"
    elif len(breaker_legs) == 1:
        attribution = "SINGLE_BREAKER_LEG"
    elif len(breaker_legs) > 1:
        attribution = "MULTIPLE_POSSIBLE_BREAKER_LEGS"
    else:
        attribution = "CAUSE_ONLY"

    breaker_idx = breaker_legs[0] if len(breaker_legs) == 1 else None
    breaker = states_after.get(breaker_idx) if breaker_idx is not None else None
    return {
        **base,
        "failure_time_sec": boundary_sec,
        "valid_interval_start_sec": interval_start_sec,
        "valid_interval_duration_sec": max(0.0, boundary_sec - interval_start_sec),
        "failure_reason": reason,
        "failure_mode": cause,
        "breaker_leg_index": "" if breaker_idx is None else breaker_idx,
        "breaker_action": "" if breaker is None else breaker.action,
        "breaker_option_type": "" if breaker is None else getattr(breaker, "option_type", ""),
        "breaker_strike": "" if breaker is None else getattr(breaker, "strike", ""),
        "possible_breaker_leg_indices_json": json.dumps(breaker_legs),
        "boundary_event_leg_indices_json": json.dumps(event_legs),
        "boundary_event_kinds_json": json.dumps(event_kinds),
        "boundary_source": (
            "CALLBACK_AND_STALENESS" if boundary_events and expiry_hit
            else "CALLBACK" if boundary_events
            else "STALENESS" if expiry_hit
            else "SYNTHETIC_OR_OTHER"
        ),
        "attribution_quality": attribution,
        "net_edge_before_failure": edge_before,
        "net_edge_after_failure": edge_after,
        "phase19_is_exchange_hazard": False,
        "phase19_is_fill_probability": False,
        "phase19_cancellation_inference_allowed": False,
        "phase19_live_money_allowed": False,
    }


def analyze_failure_session(
    events: Sequence[dict[str, Any]], *, max_state_age_sec: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not events:
        return {"phase19_session_status": "NO_EVENTS"}, []
    ordered = sorted(events, key=lambda r: (_finite(r.get("elapsed_sec")) or -1.0))
    first = ordered[0]
    cid = _text(first.get("candidate_id"))
    sid = _text(first.get("session_id"))
    base = {
        "candidate_id": cid,
        "session_id": sid,
        "monitor_started_at_utc": _text(first.get("monitor_started_at_utc")),
        "phase19_is_exchange_hazard": False,
        "phase19_is_fill_probability": False,
        "phase19_cancellation_inference_allowed": False,
        "phase19_live_money_allowed": False,
    }
    start, end = P18._session_bounds(ordered)
    if start is None or end is None:
        return {**base, "phase19_session_status": "MISSING_ANALYSIS_MARKERS"}, []
    floor = _finite(first.get("floor_pv_per_share"))
    lot = _finite(first.get("lot_size"))
    fee = _finite(first.get("fee_per_contract_leg"))
    if floor is None or lot is None or fee is None:
        return {**base, "phase19_session_status": "MISSING_PRICING_METADATA"}, []
    states = P18.initial_states(ordered)
    if not states:
        return {**base, "phase19_session_status": "NO_LEG_METADATA"}, []

    # Enrich state objects with stable display metadata if the Phase 18 dataclass does not carry it.
    leg_meta: dict[int, tuple[str, float]] = {}
    for row in ordered:
        idx = _integer(row.get("leg_index"), -1)
        if idx >= 0 and idx not in leg_meta:
            leg_meta[idx] = (_text(row.get("option_type")), _finite(row.get("strike")) or 0.0)
    for idx, state in states.items():
        # Dynamic attributes are allowed on Phase 18's non-slotted dataclass.
        setattr(state, "option_type", leg_meta.get(idx, ("", 0.0))[0])
        setattr(state, "strike", leg_meta.get(idx, ("", 0.0))[1])

    pos = 0
    while pos < len(ordered):
        t = _finite(ordered[pos].get("elapsed_sec"))
        if t is None or t > start + 1e-12:
            break
        P18.apply_event(states, ordered[pos])
        pos += 1

    by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in ordered:
        t = _finite(row.get("elapsed_sec"))
        if t is not None and start < t <= end + 1e-12:
            by_time[float(t)].append(row)
    event_times = sorted(by_time)
    evt_idx = 0
    cursor = float(start)
    current_valid, current_reason, current_edge = P18.evaluate_package(
        states, now=cursor, floor_pv_per_share=floor, lot_size=lot,
        fee_per_contract_leg=fee, max_state_age_sec=max_state_age_sec,
    )
    interval_start: Optional[float] = cursor if current_valid else None
    valid_time = 0.0
    valid_interval_count = 0
    right_censored = 0
    failures: list[dict[str, Any]] = []

    while cursor < end - 1e-12:
        next_event = event_times[evt_idx] if evt_idx < len(event_times) else None
        expiry = P18.next_expiry_time(states, cursor, max_state_age_sec)
        candidates = [float(end)]
        if next_event is not None and next_event > cursor + 1e-12:
            candidates.append(float(next_event))
        if expiry is not None and expiry > cursor + 1e-12:
            candidates.append(float(expiry))
        nxt = min(candidates)
        if current_valid:
            valid_time += max(0.0, nxt - cursor)
        cursor = nxt

        boundary_events: list[dict[str, Any]] = []
        while evt_idx < len(event_times) and abs(event_times[evt_idx] - cursor) <= 1e-12:
            boundary_events.extend(by_time[event_times[evt_idx]])
            for row in by_time[event_times[evt_idx]]:
                P18.apply_event(states, row)
            evt_idx += 1
        expiry_hit = expiry is not None and abs(float(expiry) - cursor) <= 1e-9
        new_valid, new_reason, new_edge = P18.evaluate_package(
            states, now=cursor, floor_pv_per_share=floor, lot_size=lot,
            fee_per_contract_leg=fee, max_state_age_sec=max_state_age_sec,
        )
        if current_valid and not new_valid:
            if interval_start is None:
                interval_start = cursor
            valid_interval_count += 1
            failures.append(make_failure_row(
                base=base, boundary_sec=cursor, interval_start_sec=interval_start,
                reason=new_reason, edge_before=current_edge, edge_after=new_edge,
                boundary_events=boundary_events, states_after=states, expiry_hit=expiry_hit,
            ))
            interval_start = None
        elif not current_valid and new_valid:
            interval_start = cursor
        current_valid, current_reason, current_edge = new_valid, new_reason, new_edge

    if current_valid and interval_start is not None:
        valid_interval_count += 1
        right_censored = 1

    cause_counts = Counter(_text(r.get("failure_mode")) for r in failures)
    breaker_counts = Counter(
        str(r.get("breaker_leg_index")) for r in failures if _text(r.get("breaker_leg_index"))
    )
    failure_count = len(failures)
    return {
        **base,
        "observed_duration_sec": float(end) - float(start),
        "valid_package_time_sec": valid_time,
        "valid_interval_count": valid_interval_count,
        "failure_count": failure_count,
        "right_censored_valid_interval_count": right_censored,
        "local_failure_incidence_per_valid_minute": (
            failure_count / valid_time * 60.0 if valid_time > 0 else None
        ),
        "failure_mode_counts_json": json.dumps(dict(cause_counts), sort_keys=True),
        "breaker_leg_counts_json": json.dumps(dict(breaker_counts), sort_keys=True),
        "phase19_session_status": "ANALYZED",
    }, failures


def aggregate_candidate(
    candidate_id: str,
    phase18_row: Optional[dict[str, Any]],
    sessions: Sequence[dict[str, Any]],
    failures: Sequence[dict[str, Any]],
    *,
    min_sessions: int,
    min_failures: int,
    artifact_dominance_threshold: float,
) -> dict[str, Any]:
    base = {
        "candidate_id": candidate_id,
        "phase18_candidate_decision": _text((phase18_row or {}).get("phase18_candidate_decision")),
        "phase19_is_exchange_hazard": False,
        "phase19_is_fill_probability": False,
        "phase19_cancellation_inference_allowed": False,
        "phase19_live_money_allowed": False,
    }
    if not phase18_row or _text(phase18_row.get("phase18_candidate_decision")) != PHASE18_READY:
        return {
            **base,
            "phase19_candidate_decision": "PHASE18_GATE_NOT_MET",
            "phase19_reason": "candidate did not clear Phase 18",
        }
    usable = [s for s in sessions if _text(s.get("phase19_session_status")) == "ANALYZED"]
    if len(usable) < min_sessions:
        return {
            **base,
            "sessions_total": len(sessions),
            "sessions_usable": len(usable),
            "phase19_candidate_decision": "COLLECT_MORE_FAILURE_SESSIONS",
            "phase19_reason": f"usable sessions={len(usable)} < required {min_sessions}",
        }

    valid_time = sum(_finite(s.get("valid_package_time_sec")) or 0.0 for s in usable)
    cause_counts = Counter(_text(f.get("failure_mode")) or CAUSE_OTHER for f in failures)
    breaker_counts = Counter(
        _text(f.get("breaker_leg_index")) for f in failures if _text(f.get("breaker_leg_index"))
    )
    total_failures = sum(cause_counts.values())
    dominant_mode = cause_counts.most_common(1)[0][0] if cause_counts else "NONE"
    dominant_count = cause_counts.get(dominant_mode, 0)
    dominant_share = dominant_count / total_failures if total_failures else 0.0
    stale_count = cause_counts.get(CAUSE_STALE_PRICE, 0) + cause_counts.get(CAUSE_STALE_SIZE, 0)
    stale_share = stale_count / total_failures if total_failures else 0.0
    mdt_share = cause_counts.get(CAUSE_MDT, 0) / total_failures if total_failures else 0.0

    if total_failures == 0:
        decision = "NO_FAILURES_OBSERVED"
        reason = "no valid->invalid transitions observed; collect longer sessions before failure-mode research"
    elif total_failures < min_failures:
        decision = "COLLECT_MORE_FAILURE_EVENTS"
        reason = f"failure events={total_failures} < required {min_failures}"
    elif stale_share >= artifact_dominance_threshold:
        decision = "OBSERVATION_STALENESS_DOMINATES"
        reason = f"staleness share={stale_share:.3f} >= {artifact_dominance_threshold:.3f}"
    elif mdt_share >= artifact_dominance_threshold:
        decision = "MARKET_DATA_INSTABILITY_DOMINATES"
        reason = f"market-data-type loss share={mdt_share:.3f} >= {artifact_dominance_threshold:.3f}"
    else:
        decision = "FAILURE_MODES_CHARACTERIZED_FOR_NEXT_RESEARCH"
        reason = (
            f"failures={total_failures}; dominant mode={dominant_mode} share={dominant_share:.3f}; "
            f"staleness share={stale_share:.3f}"
        )

    out = {
        **base,
        "sessions_total": len(sessions),
        "sessions_usable": len(usable),
        "total_valid_package_time_sec": valid_time,
        "failure_events": total_failures,
        "right_censored_valid_intervals": sum(_integer(s.get("right_censored_valid_interval_count"), 0) for s in usable),
        "local_failure_incidence_per_valid_minute": (
            total_failures / valid_time * 60.0 if valid_time > 0 else None
        ),
        "dominant_failure_mode": dominant_mode,
        "dominant_failure_mode_count": dominant_count,
        "dominant_failure_mode_share": dominant_share,
        "staleness_failure_share": stale_share,
        "market_data_type_failure_share": mdt_share,
        "failure_mode_counts_json": json.dumps(dict(cause_counts), sort_keys=True),
        "breaker_leg_counts_json": json.dumps(dict(breaker_counts), sort_keys=True),
        "phase19_candidate_decision": decision,
        "phase19_reason": reason,
    }
    for cause in KNOWN_CAUSES:
        key = "failure_count_" + cause.lower()
        out[key] = cause_counts.get(cause, 0)
    return out


def analyze(
    phase18_rows: Sequence[dict[str, Any]],
    event_groups: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    max_state_age_sec: float,
    min_sessions: int,
    min_failures: int,
    artifact_dominance_threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    gates = parse_phase18_gate(phase18_rows)
    sessions_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    session_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    for (cid, _sid), events in sorted(event_groups.items()):
        session, failures = analyze_failure_session(events, max_state_age_sec=max_state_age_sec)
        sessions_by_candidate[cid].append(session)
        failures_by_candidate[cid].extend(failures)
        session_rows.append(session)
        failure_rows.extend(failures)

    candidate_ids = sorted(set(gates) | set(sessions_by_candidate))
    candidate_rows = [
        aggregate_candidate(
            cid, gates.get(cid), sessions_by_candidate.get(cid, []), failures_by_candidate.get(cid, []),
            min_sessions=min_sessions, min_failures=min_failures,
            artifact_dominance_threshold=artifact_dominance_threshold,
        )
        for cid in candidate_ids
    ]
    counts = Counter(_text(r.get("phase19_candidate_decision")) for r in candidate_rows)
    ready = any(_text(r.get("phase19_candidate_decision")) == "FAILURE_MODES_CHARACTERIZED_FOR_NEXT_RESEARCH" for r in candidate_rows)
    summary = {
        "phase19_overall_decision": (
            "FAILURE_MODE_RESEARCH_CANDIDATE_EXISTS" if ready
            else "NO_FAILURE_MODE_RESEARCH_CANDIDATE_YET"
        ),
        "candidate_decision_counts": dict(counts),
        "failure_rows": len(failure_rows),
        "max_state_age_sec": max_state_age_sec,
        "phase19_is_exchange_hazard": False,
        "phase19_is_fill_probability": False,
        "phase19_cancellation_inference_allowed": False,
        "phase19_live_money_allowed": False,
    }
    return candidate_rows, session_rows, failure_rows, summary


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase18_candidates")
    p.add_argument("--event-inputs", nargs="+", required=True, help="Phase 18 event CSV files/globs")
    p.add_argument("--max-state-age-sec", type=float, default=3.0)
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-failures", type=int, default=5)
    p.add_argument("--artifact-dominance-threshold", type=float, default=0.50)
    p.add_argument("--sessions-output", required=True)
    p.add_argument("--failures-output", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--summary-json")
    args = p.parse_args()
    if args.max_state_age_sec <= 0 or args.min_sessions <= 0 or args.min_failures <= 0:
        p.error("age/session/failure parameters must be positive")
    if not 0 <= args.artifact_dominance_threshold <= 1:
        p.error("artifact dominance threshold must be in [0,1]")
    return args


def main() -> int:
    args = _args()
    candidates, sessions, failures, summary = analyze(
        read_csv(args.phase18_candidates), group_event_sessions(args.event_inputs),
        max_state_age_sec=args.max_state_age_sec,
        min_sessions=args.min_sessions,
        min_failures=args.min_failures,
        artifact_dominance_threshold=args.artifact_dominance_threshold,
    )
    write_csv(args.output, candidates)
    write_csv(args.sessions_output, sessions)
    write_csv(args.failures_output, failures)
    if args.summary_json:
        p = Path(args.summary_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(candidates)} candidate row(s), {len(sessions)} session row(s), {len(failures)} failure row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
