#!/usr/bin/env python3
"""Aggregate repeated Phase 4 monitor runs into a reproducibility research gate.

This is a read-only Phase 5 analysis tool. It does not connect to IBKR and it never
submits orders. Feed it multiple CSV files emitted by ibkr_monitor_findings.py.
The same candidate is matched across sessions by its underlying/expiry/check/strikes/
legs_json signature, then evaluated for repeatability, edge, displayed size, and quote
synchrony.
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

REQUIRED = {"underlying", "expiry", "check", "strikes", "legs_json", "monitor_status"}
NUMERIC_COLUMNS = [
    "samples_total",
    "samples_valid",
    "samples_live",
    "samples_live_positive",
    "live_positive_ratio",
    "longest_live_positive_run_sec",
    "min_live_positive_edge_per_contract",
    "max_live_positive_edge_per_contract",
    "min_live_positive_quote_size",
    "max_observed_side_skew_ms",
]
POSITIVE_STATUSES = {"PERSISTENT_LIVE_CANDIDATE", "INTERMITTENT_LIVE_CANDIDATE"}


def _finite(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _canon(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        x = float(value)
        return str(int(x)) if x.is_integer() else format(x, ".15g")
    return str(value).strip()


def candidate_signature(row: pd.Series | dict[str, Any]) -> str:
    get = row.get
    parts = [
        _canon(get("underlying", "")),
        _canon(get("expiry", "")),
        _canon(get("check", "")),
        _canon(get("strikes", "")),
        _canon(get("legs_json", "")),
    ]
    return "|".join(parts)


def candidate_id(row: pd.Series | dict[str, Any]) -> str:
    return hashlib.sha256(candidate_signature(row).encode("utf-8")).hexdigest()[:16]


def expand_inputs(values: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        matches = glob.glob(value)
        if not matches and Path(value).exists():
            matches = [value]
        for match in matches:
            path = Path(match).resolve()
            key = str(path)
            if path.is_file() and key not in seen:
                seen.add(key)
                paths.append(path)
    return sorted(paths)


def load_sessions(paths: Iterable[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        df = pd.read_csv(path)
        missing = REQUIRED - set(df.columns)
        if missing:
            raise ValueError(f"{path}: missing required columns: {', '.join(sorted(missing))}")
        d = df.copy()
        d["source_file"] = str(path)
        d["source_session"] = path.name
        d["candidate_id"] = d.apply(candidate_id, axis=1)
        for col in NUMERIC_COLUMNS:
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
        if "monitor_started_at_utc" in d.columns:
            d["monitor_started_at_utc"] = pd.to_datetime(
                d["monitor_started_at_utc"], errors="coerce", utc=True
            )
        else:
            d["monitor_started_at_utc"] = pd.NaT
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    all_rows = pd.concat(frames, ignore_index=True, sort=False)
    # A file represents one monitoring session. Duplicate candidate rows in the same
    # file must not inflate reproducibility counts. Prefer the strongest observation.
    rank = {
        "PERSISTENT_LIVE_CANDIDATE": 4,
        "INTERMITTENT_LIVE_CANDIDATE": 3,
        "NO_PERSISTENT_LIVE_EDGE": 2,
        "ERROR": 1,
    }
    all_rows["_status_rank"] = all_rows["monitor_status"].map(rank).fillna(0)
    if "min_live_positive_edge_per_contract" not in all_rows.columns:
        all_rows["min_live_positive_edge_per_contract"] = pd.NA
    all_rows["_edge_rank"] = pd.to_numeric(
        all_rows["min_live_positive_edge_per_contract"], errors="coerce"
    ).fillna(float("-inf"))
    all_rows = all_rows.sort_values(
        ["source_file", "candidate_id", "_status_rank", "_edge_rank"],
        ascending=[True, True, False, False],
    ).drop_duplicates(["source_file", "candidate_id"], keep="first")
    return all_rows.drop(columns=["_status_rank", "_edge_rank"]).reset_index(drop=True)


def _median(series: pd.Series) -> Optional[float]:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.median()) if not vals.empty else None


def _min(series: pd.Series) -> Optional[float]:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.min()) if not vals.empty else None


def _max(series: pd.Series) -> Optional[float]:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.max()) if not vals.empty else None


def _sum(series: pd.Series) -> float:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.sum()) if not vals.empty else 0.0


def aggregate_candidates(sessions: pd.DataFrame) -> pd.DataFrame:
    if sessions.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for cid, grp in sessions.groupby("candidate_id", sort=False):
        first = grp.iloc[0]
        statuses = grp["monitor_status"].astype(str)
        sessions_total = len(grp)
        sessions_error = int((statuses == "ERROR").sum())
        sessions_usable = sessions_total - sessions_error
        sessions_persistent = int((statuses == "PERSISTENT_LIVE_CANDIDATE").sum())
        sessions_intermittent = int((statuses == "INTERMITTENT_LIVE_CANDIDATE").sum())
        sessions_no_edge = int((statuses == "NO_PERSISTENT_LIVE_EDGE").sum())
        positive_sessions = grp[statuses.isin(POSITIVE_STATUSES)]

        samples_live = _sum(grp.get("samples_live", pd.Series(dtype=float)))
        samples_positive = _sum(grp.get("samples_live_positive", pd.Series(dtype=float)))
        obs_ratio = samples_positive / samples_live if samples_live > 0 else 0.0
        persistent_ratio = sessions_persistent / sessions_usable if sessions_usable > 0 else 0.0
        positive_session_ratio = (
            (sessions_persistent + sessions_intermittent) / sessions_usable
            if sessions_usable > 0 else 0.0
        )

        timestamps = pd.to_datetime(grp["monitor_started_at_utc"], errors="coerce", utc=True).dropna()
        distinct_days = int(timestamps.dt.date.nunique()) if not timestamps.empty else 0
        first_seen = timestamps.min().isoformat() if not timestamps.empty else ""
        last_seen = timestamps.max().isoformat() if not timestamps.empty else ""

        row = {
            "candidate_id": cid,
            "underlying": first.get("underlying"),
            "expiry": first.get("expiry"),
            "check": first.get("check"),
            "strikes": first.get("strikes"),
            "legs_json": first.get("legs_json"),
            "sessions_total": sessions_total,
            "sessions_usable": sessions_usable,
            "sessions_error": sessions_error,
            "sessions_persistent": sessions_persistent,
            "sessions_intermittent": sessions_intermittent,
            "sessions_no_edge": sessions_no_edge,
            "persistent_session_ratio": persistent_ratio,
            "positive_session_ratio": positive_session_ratio,
            "samples_live": int(samples_live),
            "samples_live_positive": int(samples_positive),
            "live_positive_observation_ratio": obs_ratio,
            "total_longest_live_positive_run_sec": _sum(
                positive_sessions.get("longest_live_positive_run_sec", pd.Series(dtype=float))
            ),
            "median_min_live_edge_per_contract": _median(
                positive_sessions.get("min_live_positive_edge_per_contract", pd.Series(dtype=float))
            ),
            "worst_min_live_edge_per_contract": _min(
                positive_sessions.get("min_live_positive_edge_per_contract", pd.Series(dtype=float))
            ),
            "best_min_live_edge_per_contract": _max(
                positive_sessions.get("min_live_positive_edge_per_contract", pd.Series(dtype=float))
            ),
            "worst_min_live_quote_size": _min(
                positive_sessions.get("min_live_positive_quote_size", pd.Series(dtype=float))
            ),
            "max_observed_side_skew_ms": _max(
                positive_sessions.get("max_observed_side_skew_ms", pd.Series(dtype=float))
            ),
            "distinct_monitor_days": distinct_days,
            "first_seen_utc": first_seen,
            "last_seen_utc": last_seen,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def classify_candidates(
    agg: pd.DataFrame,
    *,
    min_sessions: int = 3,
    min_persistent_sessions: int = 2,
    min_persistent_ratio: float = 0.60,
    min_live_positive_ratio: float = 0.80,
    min_edge_jpy: float = 1000.0,
    min_quote_size: float = 1.0,
    max_side_skew_ms: float = 1000.0,
    min_distinct_days: int = 1,
) -> pd.DataFrame:
    if agg.empty:
        return agg.copy()
    out = agg.copy()
    decisions: list[str] = []
    reasons: list[str] = []
    for _, row in out.iterrows():
        usable = int(row.get("sessions_usable", 0))
        persistent = int(row.get("sessions_persistent", 0))
        intermittent = int(row.get("sessions_intermittent", 0))
        days = int(row.get("distinct_monitor_days", 0))
        edge = _finite(row.get("median_min_live_edge_per_contract"))
        size = _finite(row.get("worst_min_live_quote_size"))
        skew = _finite(row.get("max_observed_side_skew_ms"))
        persistent_ratio = _finite(row.get("persistent_session_ratio")) or 0.0
        obs_ratio = _finite(row.get("live_positive_observation_ratio")) or 0.0

        if usable < min_sessions:
            decision = "INSUFFICIENT_EVIDENCE"
            reason = f"usable_sessions={usable} < {min_sessions}"
        elif persistent == 0 and intermittent == 0:
            decision = "NO_REPRODUCIBLE_LIVE_EDGE"
            reason = "no live-positive session observed"
        else:
            failures: list[str] = []
            if persistent < min_persistent_sessions:
                failures.append(f"persistent_sessions={persistent}<{min_persistent_sessions}")
            if persistent_ratio < min_persistent_ratio:
                failures.append(f"persistent_ratio={persistent_ratio:.3f}<{min_persistent_ratio:.3f}")
            if obs_ratio < min_live_positive_ratio:
                failures.append(f"live_positive_ratio={obs_ratio:.3f}<{min_live_positive_ratio:.3f}")
            if edge is None or edge < min_edge_jpy:
                failures.append(f"median_min_edge={edge!r}<{min_edge_jpy:g}")
            if size is None or size < min_quote_size:
                failures.append(f"worst_size={size!r}<{min_quote_size:g}")
            if skew is None or skew > max_side_skew_ms:
                failures.append(f"max_side_skew_ms={skew!r}>{max_side_skew_ms:g}")
            if min_distinct_days > 0 and days > 0 and days < min_distinct_days:
                failures.append(f"distinct_days={days}<{min_distinct_days}")
            if min_distinct_days > 0 and days == 0:
                failures.append("monitor timestamps unavailable")

            if not failures:
                decision = "PROMOTE_TO_EXECUTION_STUDY"
                reason = "all reproducibility and data-quality gates passed"
            else:
                decision = "KEEP_OBSERVING"
                reason = "; ".join(failures)
        decisions.append(decision)
        reasons.append(reason)

    out["research_decision"] = decisions
    out["decision_reason"] = reasons
    tier = {
        "PROMOTE_TO_EXECUTION_STUDY": 0,
        "KEEP_OBSERVING": 1,
        "INSUFFICIENT_EVIDENCE": 2,
        "NO_REPRODUCIBLE_LIVE_EDGE": 3,
    }
    out["_decision_rank"] = out["research_decision"].map(tier).fillna(9)
    out = out.sort_values(
        [
            "_decision_rank",
            "persistent_session_ratio",
            "median_min_live_edge_per_contract",
            "live_positive_observation_ratio",
            "sessions_usable",
        ],
        ascending=[True, False, False, False, False],
        na_position="last",
    ).drop(columns="_decision_rank").reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def build_summary(ranked: pd.DataFrame, sessions: pd.DataFrame) -> dict[str, Any]:
    counts = ranked["research_decision"].value_counts().to_dict() if not ranked.empty else {}
    if counts.get("PROMOTE_TO_EXECUTION_STUDY", 0) > 0:
        track = "PROMOTE_TOP_CANDIDATES_TO_EXECUTION_STUDY"
    elif counts.get("KEEP_OBSERVING", 0) > 0 or counts.get("INSUFFICIENT_EVIDENCE", 0) > 0:
        track = "KEEP_COLLECTING_DATA"
    else:
        track = "NO_REPRODUCIBLE_EDGE_YET"
    top = []
    if not ranked.empty:
        cols = [
            "rank", "candidate_id", "underlying", "expiry", "check", "strikes",
            "research_decision", "sessions_persistent", "persistent_session_ratio",
            "median_min_live_edge_per_contract", "live_positive_observation_ratio",
        ]
        top = ranked[cols].head(10).where(pd.notna(ranked[cols].head(10)), None).to_dict("records")
    return {
        "track_decision": track,
        "input_session_files": int(sessions["source_file"].nunique()) if not sessions.empty else 0,
        "candidate_session_rows": int(len(sessions)),
        "unique_candidates": int(len(ranked)),
        "decision_counts": {str(k): int(v) for k, v in counts.items()},
        "top_candidates": top,
    }


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", help="Phase 4 persistence CSV paths or glob patterns")
    p.add_argument("--output", default="data/reproducibility_ranking.csv")
    p.add_argument("--summary-json", default="data/reproducibility_summary.json")
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--min-persistent-sessions", type=int, default=2)
    p.add_argument("--min-persistent-ratio", type=float, default=0.60)
    p.add_argument("--min-live-positive-ratio", type=float, default=0.80)
    p.add_argument("--min-edge-jpy", type=float, default=1000.0)
    p.add_argument("--min-quote-size", type=float, default=1.0)
    p.add_argument("--max-side-skew-ms", type=float, default=1000.0)
    p.add_argument("--min-distinct-days", type=int, default=1)
    args = p.parse_args()
    if args.min_sessions < 1 or args.min_persistent_sessions < 1:
        p.error("session thresholds must be positive")
    if not (0 <= args.min_persistent_ratio <= 1 and 0 <= args.min_live_positive_ratio <= 1):
        p.error("ratio thresholds must be between 0 and 1")
    if args.min_edge_jpy < 0 or args.min_quote_size <= 0 or args.max_side_skew_ms <= 0:
        p.error("edge must be non-negative; size and skew thresholds must be positive")
    if args.min_distinct_days < 0:
        p.error("--min-distinct-days must be non-negative")
    return args


def main() -> int:
    args = _args()
    paths = expand_inputs(args.inputs)
    if not paths:
        raise SystemExit("no input CSV files matched")
    sessions = load_sessions(paths)
    agg = aggregate_candidates(sessions)
    ranked = classify_candidates(
        agg,
        min_sessions=args.min_sessions,
        min_persistent_sessions=args.min_persistent_sessions,
        min_persistent_ratio=args.min_persistent_ratio,
        min_live_positive_ratio=args.min_live_positive_ratio,
        min_edge_jpy=args.min_edge_jpy,
        min_quote_size=args.min_quote_size,
        max_side_skew_ms=args.max_side_skew_ms,
        min_distinct_days=args.min_distinct_days,
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ranked.to_csv(out_path, index=False, encoding="utf-8-sig")

    summary = build_summary(ranked, sessions)
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {len(ranked)} candidate ranking row(s) to {out_path}")
    print(f"track decision: {summary['track_decision']}")
    if not ranked.empty:
        print(ranked[["rank", "underlying", "expiry", "check", "research_decision"]].head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
