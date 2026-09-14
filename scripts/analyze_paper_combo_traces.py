#!/usr/bin/env python3
"""Phase 8: offline analysis of Phase 7 IBKR paper-combo traces.

This script never connects to IBKR and never submits, modifies, or cancels orders.
It reads one or more Phase 7 CSV traces, annotates each paper session, aggregates
repeated observations by candidate, and emits a conservative research decision.

No Phase 8 result establishes live OSE atomicity or permits live-money trading.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

PAPER_COMPLETE = "PAPER_TEST_COMPLETE"
STRUCTURAL_COMBO_ERROR_CODES = {312, 313, 314}
REJECT_TRACE_CLASSES = {
    "PAPER_ORDER_REJECTED_OR_INACTIVE",
    "PAPER_ORDER_REJECTED_OR_NO_ACK",
}
NO_ACK_TRACE_CLASSES = {"PAPER_NO_ACKNOWLEDGEMENT"}
FULL_TRACE_CLASSES = {"PAPER_FULL_FILL_OBSERVED"}


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "t", "yes", "y"}


def _json_list(value: Any, *, field: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    text = _text(value)
    if not text:
        return [], None
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - malformed research input must be surfaced
        return [], f"{field}: invalid JSON ({exc})"
    if not isinstance(parsed, list):
        return [], f"{field}: expected JSON list"
    out: list[dict[str, Any]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            return [], f"{field}[{i}]: expected object"
        out.append(item)
    return out, None


def _parse_ts(value: Any) -> Optional[pd.Timestamp]:
    text = _text(value)
    if not text:
        return None
    try:
        ts = pd.Timestamp(text)
    except Exception:  # noqa: BLE001
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _dedupe_executions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact duplicate execDetails callbacks, not execution corrections.

    IBKR can emit duplicate callbacks. Execution corrections use a different execId,
    so only exact non-empty exec_id duplicates are collapsed here.
    """
    seen_exec_ids: set[str] = set()
    seen_fallback: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for event in events:
        exec_id = _text(event.get("exec_id"))
        if exec_id:
            if exec_id in seen_exec_ids:
                continue
            seen_exec_ids.add(exec_id)
        else:
            key = (
                _text(event.get("ts_utc")),
                _text(event.get("sec_type")).upper(),
                event.get("con_id"),
                _text(event.get("side")).upper(),
                _finite(event.get("shares")),
                _finite(event.get("price")),
            )
            if key in seen_fallback:
                continue
            seen_fallback.add(key)
        out.append(event)
    return out


def _candidate_id(row: dict[str, Any]) -> str:
    existing = _text(row.get("candidate_id"))
    if existing:
        return existing
    material = "|".join(
        [
            _text(row.get("underlying")),
            _text(row.get("expiry")),
            _text(row.get("legs_json")),
        ]
    )
    return "fallback-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _expected_leg_count(row: dict[str, Any]) -> Optional[int]:
    raw = _finite(row.get("phase7_leg_count"))
    if raw is not None and raw >= 0:
        return int(raw)
    legs, err = _json_list(row.get("legs_json"), field="legs_json")
    if err is None and legs:
        return len(legs)
    return None


def analyze_session(row: dict[str, Any], *, sequence_gap_ms: float = 250.0) -> dict[str, Any]:
    out = dict(row)
    out["candidate_id"] = _candidate_id(row)
    out["phase8_live_money_allowed"] = False
    out["phase8_atomicity_established"] = False

    if _text(row.get("phase7_status")) != PAPER_COMPLETE:
        out.update(
            phase8_session_class="NOT_EXECUTED",
            phase8_session_reason="Phase 7 row is not a completed paper test",
            phase8_usable_session=False,
        )
        return out

    statuses, err_s = _json_list(row.get("phase7_status_events_json"), field="phase7_status_events_json")
    executions_raw, err_e = _json_list(row.get("phase7_execution_events_json"), field="phase7_execution_events_json")
    errors, err_r = _json_list(row.get("phase7_errors_json"), field="phase7_errors_json")
    parse_errors = [x for x in (err_s, err_e, err_r) if x]
    if parse_errors:
        out.update(
            phase8_session_class="REVIEW_MALFORMED_TRACE",
            phase8_session_reason="; ".join(parse_errors),
            phase8_usable_session=False,
            phase8_parse_errors_json=json.dumps(parse_errors, ensure_ascii=False, separators=(",", ":")),
        )
        return out

    executions = _dedupe_executions(executions_raw)
    error_codes: list[int] = []
    for event in errors:
        try:
            error_codes.append(int(event.get("code")))
        except (TypeError, ValueError):
            continue
    structural_codes = sorted(set(error_codes) & STRUCTURAL_COMBO_ERROR_CODES)

    sec_types = {_text(event.get("sec_type")).upper() for event in executions if _text(event.get("sec_type"))}
    has_bag_exec = "BAG" in sec_types
    opt_events = [event for event in executions if _text(event.get("sec_type")).upper() == "OPT"]
    has_opt_exec = bool(opt_events)
    if has_bag_exec and has_opt_exec:
        reporting_mode = "MIXED_BAG_AND_OPT"
    elif has_bag_exec:
        reporting_mode = "BAG_ONLY"
    elif has_opt_exec:
        reporting_mode = "OPT_ONLY"
    elif executions:
        reporting_mode = "OTHER_EXECUTION_TYPE"
    else:
        reporting_mode = "NO_EXECUTIONS"

    full_status_times: list[pd.Timestamp] = []
    full_status_seen = False
    accepted_status_seen = False
    max_filled = 0.0
    min_remaining: Optional[float] = None
    for event in statuses:
        status = _text(event.get("status"))
        filled = _finite(event.get("filled"))
        remaining = _finite(event.get("remaining"))
        if status not in {"", "Inactive"}:
            accepted_status_seen = True
        if filled is not None:
            max_filled = max(max_filled, filled)
        if remaining is not None:
            min_remaining = remaining if min_remaining is None else min(min_remaining, remaining)
        is_full = status == "Filled" or (filled is not None and filled >= 1.0) or (remaining is not None and remaining <= 0.0)
        if is_full:
            full_status_seen = True
            ts = _parse_ts(event.get("ts_utc"))
            if ts is not None:
                full_status_times.append(ts)
    first_parent_full_ts = min(full_status_times) if full_status_times else None

    opt_times = [ts for ts in (_parse_ts(e.get("ts_utc")) for e in opt_events) if ts is not None]
    sequence_gap = pd.Timedelta(milliseconds=max(0.0, float(sequence_gap_ms)))
    opt_before_parent_full = 0
    if opt_times:
        if first_parent_full_ts is None:
            opt_before_parent_full = len(opt_times)
        else:
            opt_before_parent_full = sum(ts + sequence_gap < first_parent_full_ts for ts in opt_times)
    opt_span_ms: Optional[float] = None
    if len(opt_times) >= 2:
        opt_span_ms = (max(opt_times) - min(opt_times)).total_seconds() * 1000.0

    con_ids = {str(e.get("con_id")) for e in opt_events if e.get("con_id") not in (None, "")}
    expected_legs = _expected_leg_count(row)
    trace_class = _text(row.get("phase7_trace_class"))
    open_seen = _bool(row.get("phase7_open_order_seen"))

    # A Filled status without execDetails is not impossible to observe if capture was
    # incomplete, but it is not strong enough for a mechanics conclusion.
    inconsistent = bool(full_status_seen and not executions)

    if structural_codes:
        session_class = "STRUCTURAL_COMBO_REJECT"
        reason = f"IBKR combo-structure error code(s) observed: {structural_codes}"
    elif inconsistent:
        session_class = "REVIEW_TRACE_INCONSISTENT"
        reason = "parent full-fill status was recorded but no execDetails survived in the trace"
    elif has_opt_exec and opt_before_parent_full > 0:
        session_class = "LEG_CALLBACK_SEQUENCE_RISK"
        reason = (
            f"{opt_before_parent_full} OPT execDetails callback(s) arrived materially before "
            "a parent full-fill callback (or no parent full-fill callback was recorded)"
        )
    elif has_opt_exec:
        session_class = "LEG_LEVEL_REPORTING_OBSERVED"
        reason = "OPT-level execDetails callbacks were observed; this is reporting evidence, not proof of legging"
    elif full_status_seen and has_bag_exec:
        session_class = "BAG_ONLY_FULL_FILL_OBSERVED"
        reason = "paper trace shows a parent full fill with BAG-only execution reporting"
    elif trace_class in REJECT_TRACE_CLASSES:
        session_class = "PAPER_ORDER_REJECTED"
        reason = f"Phase 7 trace class={trace_class}"
    elif trace_class in NO_ACK_TRACE_CLASSES or (not statuses and not executions and not open_seen):
        session_class = "NO_ACKNOWLEDGEMENT"
        reason = "no reliable order acknowledgement/execution evidence was recorded"
    elif accepted_status_seen or open_seen:
        session_class = "ACCEPTED_NO_FILL_EVIDENCE"
        reason = "paper order was acknowledged but no usable execution evidence was recorded"
    else:
        session_class = "REVIEW_TRACE"
        reason = f"unclassified completed paper trace (Phase 7 class={trace_class or 'missing'})"

    out.update(
        phase8_session_class=session_class,
        phase8_session_reason=reason,
        phase8_usable_session=session_class not in {"REVIEW_MALFORMED_TRACE", "NOT_EXECUTED"},
        phase8_reporting_mode=reporting_mode,
        phase8_status_event_count=len(statuses),
        phase8_execution_event_count_raw=len(executions_raw),
        phase8_execution_event_count_dedup=len(executions),
        phase8_duplicate_execution_callbacks=max(0, len(executions_raw) - len(executions)),
        phase8_error_codes_json=json.dumps(sorted(set(error_codes)), separators=(",", ":")),
        phase8_structural_combo_error_codes_json=json.dumps(structural_codes, separators=(",", ":")),
        phase8_parent_full_status_seen=bool(full_status_seen),
        phase8_accepted_status_seen=bool(accepted_status_seen or open_seen),
        phase8_max_parent_filled=max_filled,
        phase8_min_parent_remaining=min_remaining,
        phase8_opt_execution_callbacks=len(opt_events),
        phase8_opt_conids_seen=len(con_ids),
        phase8_expected_leg_count=expected_legs,
        phase8_opt_before_parent_full_count=opt_before_parent_full,
        phase8_opt_callback_span_ms=opt_span_ms,
    )
    return out


def summarize_candidate(
    group: pd.DataFrame,
    *,
    min_sessions: int = 3,
    min_bag_full_fills: int = 2,
) -> dict[str, Any]:
    first = group.iloc[0].to_dict()
    classes = group["phase8_session_class"].astype(str)
    usable = group[group["phase8_usable_session"].map(_bool)]
    n = len(usable)

    def count(name: str) -> int:
        return int((usable["phase8_session_class"].astype(str) == name).sum()) if n else 0

    structural = count("STRUCTURAL_COMBO_REJECT")
    sequence_risk = count("LEG_CALLBACK_SEQUENCE_RISK")
    leg_reporting = sequence_risk + count("LEG_LEVEL_REPORTING_OBSERVED")
    bag_full = count("BAG_ONLY_FULL_FILL_OBSERVED")
    rejected = structural + count("PAPER_ORDER_REJECTED")
    accepted_no_fill = count("ACCEPTED_NO_FILL_EVIDENCE")
    no_ack = count("NO_ACKNOWLEDGEMENT")
    inconsistent = count("REVIEW_TRACE_INCONSISTENT")

    if structural > 0:
        decision = "STOP_COMBO_PATH_PENDING_FIX"
        reason = "at least one paper session returned a structural IBKR combo-definition error"
    elif sequence_risk > 0:
        decision = "INVESTIGATE_LEG_CALLBACK_SEQUENCE"
        reason = "OPT execution callbacks preceded parent full-fill evidence in at least one paper session"
    elif inconsistent > 0:
        decision = "REVIEW_TRACE_CAPTURE_INCONSISTENCY"
        reason = "at least one paper trace contains parent fill evidence without execution callbacks"
    elif n < int(min_sessions):
        decision = "KEEP_PAPER_OBSERVING"
        reason = f"only {n} usable paper session(s); require at least {int(min_sessions)}"
    elif leg_reporting > 0:
        decision = "INVESTIGATE_LEG_REPORTING"
        reason = "OPT-level execution reporting was observed; distinguish reporting semantics from actual legging before any further promotion"
    elif bag_full >= int(min_bag_full_fills) and rejected == 0 and no_ack == 0:
        decision = "BROKER_CONFIRMATION_REQUIRED"
        reason = (
            "repeated BAG-only paper fills were observed, but paper simulation cannot establish live OSE atomicity; "
            "obtain broker/exchange confirmation before designing any live experiment"
        )
    elif rejected == n and n > 0:
        decision = "STOP_COMBO_PATH_IN_PAPER"
        reason = "all usable paper sessions were rejected"
    elif accepted_no_fill > 0:
        decision = "PAPER_ACCEPTS_BUT_NO_FILL_EVIDENCE"
        reason = "paper simulator accepted orders, but repeated execution mechanics were not demonstrated"
    else:
        decision = "KEEP_PAPER_OBSERVING"
        reason = "paper evidence is mixed or insufficient for a mechanics conclusion"

    days: set[str] = set()
    for value in usable.get("phase7_started_at_utc", pd.Series(dtype=object)):
        ts = _parse_ts(value)
        if ts is not None:
            days.add(ts.date().isoformat())

    return {
        "candidate_id": _text(first.get("candidate_id")),
        "underlying": _text(first.get("underlying")),
        "expiry": _text(first.get("expiry")),
        "legs_json": _text(first.get("legs_json")),
        "phase8_total_rows": int(len(group)),
        "phase8_usable_sessions": int(n),
        "phase8_distinct_days": int(len(days)),
        "phase8_bag_only_full_fill_sessions": int(bag_full),
        "phase8_leg_reporting_sessions": int(leg_reporting),
        "phase8_leg_sequence_risk_sessions": int(sequence_risk),
        "phase8_structural_reject_sessions": int(structural),
        "phase8_other_reject_sessions": int(max(0, rejected - structural)),
        "phase8_no_ack_sessions": int(no_ack),
        "phase8_accepted_no_fill_sessions": int(accepted_no_fill),
        "phase8_inconsistent_trace_sessions": int(inconsistent),
        "phase8_candidate_decision": decision,
        "phase8_candidate_reason": reason,
        "phase8_live_money_allowed": False,
        "phase8_atomicity_established": False,
        "phase8_session_classes_json": json.dumps(classes.value_counts().to_dict(), separators=(",", ":")),
    }


def _expand_inputs(items: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for item in items:
        matches = glob.glob(item)
        if not matches and Path(item).is_file():
            matches = [item]
        for match in sorted(matches):
            p = Path(match)
            if not p.is_file():
                continue
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                found.append(p)
    return found


def _validate(df: pd.DataFrame) -> None:
    required = {
        "phase7_status",
        "phase7_status_events_json",
        "phase7_execution_events_json",
        "phase7_errors_json",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Phase 7 trace input missing required columns: {', '.join(sorted(missing))}")


def evaluate(
    inputs: Iterable[str],
    *,
    sequence_gap_ms: float = 250.0,
    min_sessions: int = 3,
    min_bag_full_fills: int = 2,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    paths = _expand_inputs(inputs)
    if not paths:
        raise ValueError("no Phase 7 CSV inputs matched")
    frames: list[pd.DataFrame] = []
    for p in paths:
        frame = pd.read_csv(p)
        _validate(frame)
        frame["phase8_source_file"] = str(p)
        frames.append(frame)
    raw = pd.concat(frames, ignore_index=True, sort=False)
    annotated_rows = [analyze_session(row.to_dict(), sequence_gap_ms=sequence_gap_ms) for _, row in raw.iterrows()]
    annotated = pd.DataFrame(annotated_rows)

    # Duplicate source selection/globs must not double-count an identical paper run.
    if not annotated.empty:
        keys = []
        for _, row in annotated.iterrows():
            order_id = _text(row.get("phase7_order_id"))
            started = _text(row.get("phase7_started_at_utc"))
            if order_id or started:
                keys.append(f"{row.get('candidate_id','')}|{order_id}|{started}")
            else:
                material = "|".join(
                    [
                        _text(row.get("candidate_id")),
                        _text(row.get("phase7_status_events_json")),
                        _text(row.get("phase7_execution_events_json")),
                        _text(row.get("phase7_errors_json")),
                    ]
                )
                keys.append("content-" + hashlib.sha256(material.encode("utf-8")).hexdigest())
        annotated["phase8_session_key"] = keys
        annotated = annotated.drop_duplicates(subset=["phase8_session_key"], keep="first").reset_index(drop=True)

    summaries = [
        summarize_candidate(group, min_sessions=min_sessions, min_bag_full_fills=min_bag_full_fills)
        for _, group in annotated.groupby("candidate_id", dropna=False, sort=False)
    ]
    ranking = pd.DataFrame(summaries)
    if not ranking.empty:
        priority = {
            "STOP_COMBO_PATH_PENDING_FIX": 0,
            "INVESTIGATE_LEG_CALLBACK_SEQUENCE": 1,
            "REVIEW_TRACE_CAPTURE_INCONSISTENCY": 2,
            "INVESTIGATE_LEG_REPORTING": 3,
            "BROKER_CONFIRMATION_REQUIRED": 4,
            "PAPER_ACCEPTS_BUT_NO_FILL_EVIDENCE": 5,
            "KEEP_PAPER_OBSERVING": 6,
            "STOP_COMBO_PATH_IN_PAPER": 7,
        }
        ranking["_priority"] = ranking["phase8_candidate_decision"].map(priority).fillna(99)
        ranking = ranking.sort_values(["_priority", "phase8_usable_sessions"], ascending=[True, False]).drop(columns="_priority")

    decisions = ranking["phase8_candidate_decision"].astype(str).tolist() if not ranking.empty else []
    if any(x in {"INVESTIGATE_LEG_CALLBACK_SEQUENCE", "INVESTIGATE_LEG_REPORTING", "REVIEW_TRACE_CAPTURE_INCONSISTENCY"} for x in decisions):
        overall = "NO_LIVE_AUTOMATION_INVESTIGATE_PAPER_TRACES"
    elif any(x == "BROKER_CONFIRMATION_REQUIRED" for x in decisions):
        overall = "NO_LIVE_AUTOMATION_BROKER_CONFIRMATION_REQUIRED"
    elif decisions and all(x.startswith("STOP_") for x in decisions):
        overall = "NO_LIVE_AUTOMATION_COMBO_PATH_BLOCKED"
    else:
        overall = "NO_LIVE_AUTOMATION_KEEP_OBSERVING"
    summary = {
        "phase8_overall_decision": overall,
        "phase8_live_money_allowed": False,
        "phase8_atomicity_established": False,
        "input_files": [str(p) for p in paths],
        "unique_sessions": int(len(annotated)),
        "candidate_count": int(len(ranking)),
        "candidate_decision_counts": ranking["phase8_candidate_decision"].value_counts().to_dict() if not ranking.empty else {},
    }
    return annotated, ranking, summary


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", help="Phase 7 CSV path(s) or glob pattern(s)")
    p.add_argument("--sessions-output", default="data/phase8_paper_sessions.csv")
    p.add_argument("--output", default="data/phase8_paper_candidate_summary.csv")
    p.add_argument("--summary-json", default="data/phase8_summary.json")
    p.add_argument("--sequence-gap-ms", type=float, default=250.0, help="minimum callback lead to flag OPT-before-parent sequence risk")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-bag-full-fills", type=int, default=2)
    args = p.parse_args()
    if args.sequence_gap_ms < 0:
        p.error("--sequence-gap-ms must be non-negative")
    if args.min_sessions < 1:
        p.error("--min-sessions must be at least 1")
    if args.min_bag_full_fills < 1:
        p.error("--min-bag-full-fills must be at least 1")
    return args


def main() -> int:
    args = _args()
    sessions, ranking, summary = evaluate(
        args.inputs,
        sequence_gap_ms=args.sequence_gap_ms,
        min_sessions=args.min_sessions,
        min_bag_full_fills=args.min_bag_full_fills,
    )
    sessions_path = Path(args.sessions_output)
    ranking_path = Path(args.output)
    json_path = Path(args.summary_json)
    for p in (sessions_path, ranking_path, json_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    sessions.to_csv(sessions_path, index=False, encoding="utf-8-sig")
    ranking.to_csv(ranking_path, index=False, encoding="utf-8-sig")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(sessions)} unique paper session(s) to {sessions_path}")
    print(f"wrote {len(ranking)} candidate summary row(s) to {ranking_path}")
    print(f"overall={summary['phase8_overall_decision']}")
    print("Phase 8 is offline analysis only; no result authorizes live-money trading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
