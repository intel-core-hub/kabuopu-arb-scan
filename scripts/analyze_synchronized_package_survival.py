#!/usr/bin/env python3
"""Phase 17: synchronized multi-leg displayed-touch retention from Phase 4 samples.

This offline research tool uses the enriched ``monitor_samples_json`` written by
Phase 4/10.  A trigger is a LIVE_POSITIVE sample with complete per-leg snapshots.
For each configured horizon it checks every sampled checkpoint from the trigger to
(the first sample at/after) that horizon and asks whether *all legs at the same
checkpoint* remain live, sized for the requested quantity, and no worse than their
trigger executable prices.

The result is a directly observed synchronized package-level displayed-touch
retention metric at the Phase 4 sampling cadence.  It is NOT continuous-time
survival, exchange-arrival probability, fill probability, or atomic-execution
probability.  No leg-independence assumption is used.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional, Sequence

PHASE16_READY = "TOUCH_RETAINS_THROUGH_PAPER_CONTROL_PATH_FOR_NEXT_RESEARCH"


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


def parse_horizons(value: str | Sequence[float]) -> list[float]:
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
    else:
        raw = list(value)
    horizons: list[float] = []
    for item in raw:
        x = _finite(item)
        if x is None or x <= 0:
            raise ValueError("all horizons must be finite and positive")
        if not any(abs(x - old) <= 1e-12 for old in horizons):
            horizons.append(float(x))
    if not horizons:
        raise ValueError("at least one horizon is required")
    return sorted(horizons)


def parse_samples_json(value: Any) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_text(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid monitor_samples_json: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("monitor_samples_json must be a JSON array")
    rows = [dict(x) for x in raw if isinstance(x, dict)]
    rows.sort(key=lambda x: (_finite(x.get("elapsed_sec")) is None, _finite(x.get("elapsed_sec")) or 0.0))
    return rows


def snapshot_map(sample: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw = sample.get("leg_snapshots")
    if not isinstance(raw, list):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = _integer(item.get("leg_index"), -1)
        if idx >= 0:
            out[idx] = item
    return out


def _valid_live_leg(snapshot: dict[str, Any]) -> tuple[bool, str]:
    if _integer(snapshot.get("market_data_type"), -1) != 1:
        return False, "NONLIVE_LEG"
    action = _text(snapshot.get("action")).upper()
    if action not in {"BUY", "SELL"}:
        return False, "INVALID_ACTION"
    price = _finite(snapshot.get("executable_price"))
    qty = _integer(snapshot.get("qty"), 0)
    size = _finite(snapshot.get("executable_size"))
    if price is None or (price <= 0 if action == "BUY" else price < 0):
        return False, "MISSING_EXECUTABLE_PRICE"
    if qty <= 0:
        return False, "INVALID_QTY"
    if size is None or size < qty:
        return False, "NO_EXECUTABLE_SIZE"
    return True, ""


def trigger_snapshots(sample: dict[str, Any]) -> tuple[Optional[dict[int, dict[str, Any]]], str]:
    if _text(sample.get("sample_status")) != "LIVE_POSITIVE":
        return None, "NOT_LIVE_POSITIVE"
    snaps = snapshot_map(sample)
    if not snaps:
        return None, "MISSING_LEG_SNAPSHOTS"
    for idx in sorted(snaps):
        ok, reason = _valid_live_leg(snaps[idx])
        if not ok:
            return None, f"LEG_{idx}_{reason}"
    return snaps, ""


def package_checkpoint_retained(
    trigger: dict[int, dict[str, Any]],
    sample: dict[str, Any],
    *,
    price_epsilon: float = 1e-12,
) -> tuple[bool, str]:
    """Whether all trigger legs remain synchronously executable, at no worse prices."""
    if _text(sample.get("sample_status")) != "LIVE_POSITIVE":
        return False, f"PACKAGE_STATUS_{_text(sample.get('sample_status')) or 'MISSING'}"
    current = snapshot_map(sample)
    if not current:
        return False, "MISSING_LEG_SNAPSHOTS"
    if set(current) != set(trigger):
        return False, "LEG_SET_CHANGED_OR_INCOMPLETE"
    for idx in sorted(trigger):
        base = trigger[idx]
        now = current[idx]
        ok, reason = _valid_live_leg(now)
        if not ok:
            return False, f"LEG_{idx}_{reason}"
        if _text(now.get("action")).upper() != _text(base.get("action")).upper():
            return False, f"LEG_{idx}_ACTION_CHANGED"
        if _integer(now.get("qty"), 0) != _integer(base.get("qty"), 0):
            return False, f"LEG_{idx}_QTY_CHANGED"
        base_px = _finite(base.get("executable_price"))
        now_px = _finite(now.get("executable_price"))
        if base_px is None or now_px is None:
            return False, f"LEG_{idx}_MISSING_EXECUTABLE_PRICE"
        action = _text(base.get("action")).upper()
        if action == "BUY" and now_px > base_px + price_epsilon:
            return False, f"LEG_{idx}_BUY_PRICE_WORSE"
        if action == "SELL" and now_px < base_px - price_epsilon:
            return False, f"LEG_{idx}_SELL_PRICE_WORSE"
    return True, ""


def infer_sample_interval(samples: Sequence[dict[str, Any]]) -> Optional[float]:
    times = [_finite(s.get("elapsed_sec")) for s in samples]
    clean = [x for x in times if x is not None]
    diffs = [b - a for a, b in zip(clean, clean[1:]) if b > a + 1e-12]
    return float(median(diffs)) if diffs else None


def analyze_session(
    row: dict[str, Any],
    *,
    horizons_sec: Sequence[float],
    max_target_overshoot_factor: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_id = _text(row.get("candidate_id"))
    base = {
        "candidate_id": candidate_id,
        "source_file": _text(row.get("_source_file") or row.get("source_file")),
        "monitor_started_at_utc": _text(row.get("monitor_started_at_utc")),
        "monitor_finished_at_utc": _text(row.get("monitor_finished_at_utc")),
        "phase17_is_fill_probability": False,
        "phase17_is_continuous_time_survival": False,
        "phase17_independence_assumption_used": False,
        "phase17_live_money_allowed": False,
    }
    try:
        samples = parse_samples_json(row.get("monitor_samples_json"))
    except ValueError as exc:
        return {**base, "phase17_session_status": "INVALID_MONITOR_SAMPLES", "phase17_session_reason": str(exc)}, []
    if not samples:
        return {**base, "phase17_session_status": "NO_MONITOR_SAMPLES", "phase17_session_reason": "monitor_samples_json is empty"}, []
    enriched = any(snapshot_map(s) for s in samples)
    if not enriched:
        return {
            **base,
            "phase17_session_status": "NEEDS_ENRICHED_PHASE4_CAPTURE",
            "phase17_session_reason": "Phase 4 samples lack per-leg snapshots required for synchronized package analysis",
        }, []
    interval = infer_sample_interval(samples)
    if interval is None or interval <= 0:
        return {**base, "phase17_session_status": "INSUFFICIENT_SAMPLE_TIMING", "phase17_session_reason": "cannot infer a positive sample interval"}, []
    max_overshoot = interval * max_target_overshoot_factor
    times = [_finite(s.get("elapsed_sec")) for s in samples]
    trigger_indices: list[int] = []
    triggers: dict[int, dict[int, dict[str, Any]]] = {}
    for i, sample in enumerate(samples):
        trig, _ = trigger_snapshots(sample)
        if trig is not None and times[i] is not None:
            trigger_indices.append(i)
            triggers[i] = trig
    if not trigger_indices:
        return {
            **base,
            "sample_interval_sec_observed": interval,
            "phase17_session_status": "NO_LIVE_PACKAGE_TRIGGERS",
            "phase17_session_reason": "no LIVE_POSITIVE sample with complete live per-leg snapshots",
        }, []
    details: list[dict[str, Any]] = []
    counts: dict[float, list[int]] = {float(h): [0, 0] for h in horizons_sec}  # complete, retained
    last_elapsed = max(x for x in times if x is not None)
    for trigger_index in trigger_indices:
        trigger_time = float(times[trigger_index])
        trigger = triggers[trigger_index]
        for horizon in horizons_sec:
            h = float(horizon)
            target_time = trigger_time + h
            if target_time > last_elapsed + 1e-12:
                details.append({
                    "candidate_id": candidate_id,
                    "source_file": base["source_file"],
                    "monitor_started_at_utc": base["monitor_started_at_utc"],
                    "trigger_sample_index": trigger_index,
                    "trigger_elapsed_sec": trigger_time,
                    "horizon_sec": h,
                    "complete_window": False,
                    "package_retained": None,
                    "failure_reason": "END_OF_MONITOR_WINDOW",
                })
                continue
            target_index: Optional[int] = None
            for j in range(trigger_index, len(samples)):
                t = times[j]
                if t is not None and t + 1e-12 >= target_time:
                    target_index = j
                    break
            if target_index is None or times[target_index] is None:
                complete = False
                reason = "NO_TARGET_SAMPLE"
            else:
                overshoot = float(times[target_index]) - target_time
                complete = overshoot <= max_overshoot + 1e-12
                reason = "" if complete else "TARGET_SAMPLE_TOO_FAR"
            if not complete:
                details.append({
                    "candidate_id": candidate_id,
                    "source_file": base["source_file"],
                    "monitor_started_at_utc": base["monitor_started_at_utc"],
                    "trigger_sample_index": trigger_index,
                    "trigger_elapsed_sec": trigger_time,
                    "horizon_sec": h,
                    "complete_window": False,
                    "package_retained": None,
                    "failure_reason": reason,
                })
                continue
            counts[h][0] += 1
            retained = True
            failure_reason = ""
            failure_index: Optional[int] = None
            # Check every sampled checkpoint through the horizon target.  This is still
            # discrete-time evidence only; nothing is inferred between callbacks.
            for j in range(trigger_index, int(target_index) + 1):
                ok, why = package_checkpoint_retained(trigger, samples[j])
                if not ok:
                    retained = False
                    failure_reason = why
                    failure_index = j
                    break
            if retained:
                counts[h][1] += 1
            details.append({
                "candidate_id": candidate_id,
                "source_file": base["source_file"],
                "monitor_started_at_utc": base["monitor_started_at_utc"],
                "trigger_sample_index": trigger_index,
                "trigger_elapsed_sec": trigger_time,
                "horizon_sec": h,
                "complete_window": True,
                "package_retained": retained,
                "target_sample_index": target_index,
                "target_elapsed_sec": float(times[target_index]),
                "target_overshoot_ms": (float(times[target_index]) - target_time) * 1000.0,
                "failure_sample_index": failure_index,
                "failure_reason": failure_reason,
            })
    out = dict(base)
    out["sample_interval_sec_observed"] = interval
    out["live_package_trigger_samples"] = len(trigger_indices)
    complete_any = 0
    for h in horizons_sec:
        complete, retained = counts[float(h)]
        complete_any += complete
        label = _horizon_label(float(h))
        out[f"complete_triggers_{label}"] = complete
        out[f"retained_triggers_{label}"] = retained
        out[f"package_retention_{label}"] = (retained / complete) if complete else None
    if complete_any <= 0:
        out["phase17_session_status"] = "NO_COMPLETE_HORIZON_WINDOWS"
        out["phase17_session_reason"] = "triggers exist but no configured horizon has a complete sampled future window"
    else:
        out["phase17_session_status"] = "ANALYZED"
        out["phase17_session_reason"] = "synchronized package touch evaluated at sampled checkpoints"
    return out, details


def _horizon_label(horizon_sec: float) -> str:
    ms = horizon_sec * 1000.0
    if abs(ms - round(ms)) <= 1e-9:
        return f"{int(round(ms))}ms"
    return f"{horizon_sec:g}s".replace(".", "p")


def bootstrap_mean_interval(
    values: Sequence[float], *, reps: int, confidence: float, seed: int
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return None, None, None
    if reps <= 0 or len(vals) == 1:
        mean = sum(vals) / len(vals)
        return mean, mean, mean
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


def _phase16_gate_ok(row: Optional[dict[str, Any]]) -> bool:
    return bool(row) and _text(row.get("phase16_candidate_decision")) == PHASE16_READY


def aggregate_candidate(
    candidate_id: str,
    phase16_row: Optional[dict[str, Any]],
    sessions: Sequence[dict[str, Any]],
    *,
    target_horizon_sec: float,
    min_sessions: int,
    min_complete_triggers_per_session: int,
    min_retention: float,
    bootstrap_reps: int,
    bootstrap_confidence: float,
    seed: int,
) -> dict[str, Any]:
    base = {
        "candidate_id": candidate_id,
        "phase16_candidate_decision": _text((phase16_row or {}).get("phase16_candidate_decision")),
        "phase17_is_joint_displayed_touch_metric": True,
        "phase17_is_fill_probability": False,
        "phase17_is_continuous_time_survival": False,
        "phase17_is_exchange_arrival_probability": False,
        "phase17_independence_assumption_used": False,
        "phase17_live_money_allowed": False,
    }
    if not _phase16_gate_ok(phase16_row):
        return {
            **base,
            "phase17_candidate_decision": "PHASE16_GATE_NOT_MET",
            "phase17_reason": "candidate did not clear the conservative Phase 16 control-path overlay gate",
        }
    if not sessions:
        return {**base, "phase17_candidate_decision": "NO_PHASE4_SESSIONS", "phase17_reason": "no Phase 4 sessions found"}
    needs_recollect = any(_text(s.get("phase17_session_status")) == "NEEDS_ENRICHED_PHASE4_CAPTURE" for s in sessions)
    label = _horizon_label(target_horizon_sec)
    usable: list[dict[str, Any]] = []
    ratios: list[float] = []
    for session in sessions:
        if _text(session.get("phase17_session_status")) != "ANALYZED":
            continue
        complete = _integer(session.get(f"complete_triggers_{label}"), 0)
        ratio = _finite(session.get(f"package_retention_{label}"))
        if complete < min_complete_triggers_per_session or ratio is None:
            continue
        usable.append(session)
        ratios.append(float(ratio))
    if not usable:
        if needs_recollect:
            decision = "NEEDS_PHASE4_RECOLLECTION"
            reason = "available Phase 4 sessions lack enriched per-leg snapshots"
        else:
            decision = "COLLECT_MORE_SYNCHRONIZED_PACKAGE_DATA"
            reason = f"no session has at least {min_complete_triggers_per_session} complete triggers at {target_horizon_sec:g}s"
        return {
            **base,
            "target_horizon_sec": target_horizon_sec,
            "sessions_total": len(sessions),
            "sessions_usable": 0,
            "phase17_candidate_decision": decision,
            "phase17_reason": reason,
        }
    if len(usable) < min_sessions:
        return {
            **base,
            "target_horizon_sec": target_horizon_sec,
            "sessions_total": len(sessions),
            "sessions_usable": len(usable),
            "mean_session_package_retention": sum(ratios) / len(ratios),
            "min_session_package_retention": min(ratios),
            "phase17_candidate_decision": "COLLECT_MORE_SYNCHRONIZED_SESSIONS",
            "phase17_reason": f"usable synchronized sessions={len(usable)} < required {min_sessions}",
        }
    lower, med, upper = bootstrap_mean_interval(
        ratios, reps=bootstrap_reps, confidence=bootstrap_confidence, seed=seed
    )
    mean_ratio = sum(ratios) / len(ratios)
    if lower is not None and lower >= min_retention:
        decision = "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"
        reason = (
            f"session-bootstrap lower bound of synchronized package retention={lower:.3f} "
            f">= threshold {min_retention:.3f} at {target_horizon_sec:g}s"
        )
    else:
        decision = "SYNCHRONIZED_PACKAGE_TOUCH_NOT_ROBUST"
        reason = (
            f"session-bootstrap lower bound of synchronized package retention="
            f"{lower if lower is not None else float('nan'):.3f} < threshold {min_retention:.3f}"
        )
    return {
        **base,
        "target_horizon_sec": target_horizon_sec,
        "sessions_total": len(sessions),
        "sessions_usable": len(usable),
        "mean_session_package_retention": mean_ratio,
        "min_session_package_retention": min(ratios),
        "max_session_package_retention": max(ratios),
        "bootstrap_confidence": bootstrap_confidence,
        "bootstrap_lower_session_package_retention": lower,
        "bootstrap_median_session_package_retention": med,
        "bootstrap_upper_session_package_retention": upper,
        "min_required_retention": min_retention,
        "phase17_candidate_decision": decision,
        "phase17_reason": reason,
        "interpretation": (
            "Direct joint displayed-touch retention across synchronized Phase 4 sampled checkpoints. "
            "This does not establish continuous survival between samples, fill probability, order-arrival probability, or atomic execution."
        ),
    }


def analyze(
    phase16_rows: Sequence[dict[str, Any]],
    phase4_rows: Sequence[dict[str, Any]],
    *,
    horizons_sec: Sequence[float],
    target_horizon_sec: float,
    max_target_overshoot_factor: float,
    min_sessions: int,
    min_complete_triggers_per_session: int,
    min_retention: float,
    bootstrap_reps: int,
    bootstrap_confidence: float,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    horizons = parse_horizons(horizons_sec)
    if not any(abs(target_horizon_sec - h) <= 1e-12 for h in horizons):
        raise ValueError("target_horizon_sec must be one of horizons_sec")
    p16: dict[str, dict[str, Any]] = {}
    for row in phase16_rows:
        cid = _text(row.get("candidate_id"))
        if cid and cid not in p16:
            p16[cid] = dict(row)
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in phase4_rows:
        cid = _text(row.get("candidate_id"))
        if cid:
            by_candidate[cid].append(dict(row))
    candidate_ids = sorted(set(p16) | set(by_candidate))
    sessions_out: list[dict[str, Any]] = []
    triggers_out: list[dict[str, Any]] = []
    candidates_out: list[dict[str, Any]] = []
    for cidx, cid in enumerate(candidate_ids):
        analyzed_sessions: list[dict[str, Any]] = []
        for row in by_candidate.get(cid, []):
            session, details = analyze_session(
                row,
                horizons_sec=horizons,
                max_target_overshoot_factor=max_target_overshoot_factor,
            )
            analyzed_sessions.append(session)
            sessions_out.append(session)
            triggers_out.extend(details)
        candidates_out.append(aggregate_candidate(
            cid,
            p16.get(cid),
            analyzed_sessions,
            target_horizon_sec=target_horizon_sec,
            min_sessions=min_sessions,
            min_complete_triggers_per_session=min_complete_triggers_per_session,
            min_retention=min_retention,
            bootstrap_reps=bootstrap_reps,
            bootstrap_confidence=bootstrap_confidence,
            seed=seed + cidx,
        ))
    counts: dict[str, int] = defaultdict(int)
    for row in candidates_out:
        counts[_text(row.get("phase17_candidate_decision"))] += 1
    ready = any(
        _text(r.get("phase17_candidate_decision")) == "SYNCHRONIZED_PACKAGE_TOUCH_ROBUST_ENOUGH_FOR_NEXT_RESEARCH"
        for r in candidates_out
    )
    if ready:
        overall = "SYNCHRONIZED_PACKAGE_RESEARCH_CANDIDATE_EXISTS"
        reason = "at least one candidate retained all displayed executable legs jointly at synchronized sampled checkpoints"
    elif candidates_out:
        overall = "NO_SYNCHRONIZED_PACKAGE_RESEARCH_CANDIDATE_YET"
        reason = "no candidate cleared the Phase 17 synchronized package-retention gate"
    else:
        overall = "NO_ANALYZABLE_CANDIDATES"
        reason = "no candidate IDs were found"
    summary = {
        "phase17_overall_decision": overall,
        "phase17_reason": reason,
        "target_horizon_sec": target_horizon_sec,
        "horizons_sec": horizons,
        "candidate_decision_counts": dict(counts),
        "phase17_is_joint_displayed_touch_metric": True,
        "phase17_is_fill_probability": False,
        "phase17_is_continuous_time_survival": False,
        "phase17_is_exchange_arrival_probability": False,
        "phase17_independence_assumption_used": False,
        "phase17_live_money_allowed": False,
    }
    return candidates_out, sessions_out, triggers_out, summary


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


def load_phase4(patterns: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_sessions: set[tuple[str, str, str]] = set()
    for path in expand_inputs(patterns):
        resolved = str(Path(path).resolve())
        for row in read_csv(path):
            item = dict(row)
            item["_source_file"] = path
            key = (resolved, _text(item.get("candidate_id")), _text(item.get("monitor_started_at_utc")))
            if key in seen_sessions:
                continue
            seen_sessions.add(key)
            rows.append(item)
    return rows


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


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase16_candidates", help="Phase 16 candidate summary CSV")
    p.add_argument("--phase4-inputs", nargs="+", required=True, help="Phase 4 enriched CSV files/globs")
    p.add_argument("--horizons-sec", default="0.5,1,2", help="comma-separated sampled retention horizons")
    p.add_argument("--target-horizon-sec", type=float, default=1.0)
    p.add_argument("--max-target-overshoot-factor", type=float, default=1.5,
                   help="max target-sample overshoot as a multiple of observed median sample interval")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-complete-triggers-per-session", type=int, default=5)
    p.add_argument("--min-retention", type=float, default=0.80)
    p.add_argument("--bootstrap-reps", type=int, default=2000)
    p.add_argument("--bootstrap-confidence", type=float, default=0.90)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--sessions-output", default="data/phase17_package_sessions.csv")
    p.add_argument("--triggers-output", default="data/phase17_package_triggers.csv")
    p.add_argument("--output", default="data/phase17_package_candidates.csv")
    p.add_argument("--summary-json", default="data/phase17_summary.json")
    args = p.parse_args()
    horizons = parse_horizons(args.horizons_sec)
    if args.target_horizon_sec <= 0 or not any(abs(args.target_horizon_sec - h) <= 1e-12 for h in horizons):
        p.error("--target-horizon-sec must be positive and included in --horizons-sec")
    if args.max_target_overshoot_factor < 0:
        p.error("--max-target-overshoot-factor must be non-negative")
    if args.min_sessions <= 0 or args.min_complete_triggers_per_session <= 0 or args.bootstrap_reps < 0:
        p.error("session/trigger counts must be positive and bootstrap reps non-negative")
    if not 0 <= args.min_retention <= 1:
        p.error("--min-retention must be in [0,1]")
    if not 0 < args.bootstrap_confidence < 1:
        p.error("--bootstrap-confidence must be in (0,1)")
    args._parsed_horizons = horizons
    return args


def main() -> int:
    args = _args()
    phase16_rows = read_csv(args.phase16_candidates)
    phase4_rows = load_phase4(args.phase4_inputs)
    candidates, sessions, triggers, summary = analyze(
        phase16_rows,
        phase4_rows,
        horizons_sec=args._parsed_horizons,
        target_horizon_sec=args.target_horizon_sec,
        max_target_overshoot_factor=args.max_target_overshoot_factor,
        min_sessions=args.min_sessions,
        min_complete_triggers_per_session=args.min_complete_triggers_per_session,
        min_retention=args.min_retention,
        bootstrap_reps=args.bootstrap_reps,
        bootstrap_confidence=args.bootstrap_confidence,
        seed=args.seed,
    )
    write_csv(args.output, candidates)
    write_csv(args.sessions_output, sessions)
    write_csv(args.triggers_output, triggers)
    p = Path(args.summary_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
