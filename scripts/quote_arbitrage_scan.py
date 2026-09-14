#!/usr/bin/env python3
"""Bid/ask no-arbitrage screening for normalized kabu-opu quote snapshots.

This scans delayed quotes.  A positive edge is not a live executable arbitrage;
it is a candidate that must be verified against simultaneous live quotes, fees,
and available size before any order is sent.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

REQUIRED = {"underlying", "expiry", "option_type", "strike", "bid", "ask"}


@dataclass(frozen=True)
class Quote:
    strike: float
    bid: float
    ask: float
    bid_size: Optional[float]
    ask_size: Optional[float]


def _num(v) -> Optional[float]:  # noqa: ANN001
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _size_min(*values: Optional[float]) -> Optional[float]:
    xs = [v for v in values if v is not None and math.isfinite(v)]
    return min(xs) if len(xs) == len(values) else None


def _quote(row: pd.Series) -> Optional[Quote]:
    bid, ask, strike = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("strike"))
    if bid is None or ask is None or strike is None:
        return None
    return Quote(strike, bid, ask, _num(row.get("bid_size")), _num(row.get("ask_size")))


def _snapshot_date(group: pd.DataFrame, fallback: date) -> date:
    if "snapshot_time" in group.columns:
        vals = pd.to_datetime(group["snapshot_time"], errors="coerce")
        vals = vals.dropna()
        if not vals.empty:
            return vals.iloc[0].date()
    return fallback


def _pv_width(width: float, rate: float, as_of: date, expiry: str) -> float:
    dt = pd.to_datetime(expiry, errors="coerce")
    if pd.isna(dt):
        return width
    t = max((dt.date() - as_of).days / 365.0, 0.0)
    return width * math.exp(-rate * t)


def _emit(findings: list[dict], *, check: str, underlying: str, expiry: str, strikes: str,
          legs: str, gross_edge_per_share: float, lot_size: float, fee_legs: int,
          fee_per_contract_leg: float, min_quote_size: Optional[float], detail: str) -> None:
    gross_contract = gross_edge_per_share * lot_size
    fees = fee_legs * fee_per_contract_leg
    net = gross_contract - fees
    if net <= 0:
        return
    findings.append({
        "check": check,
        "underlying": underlying,
        "expiry": expiry,
        "strikes": strikes,
        "legs": legs,
        "gross_edge_per_share": gross_edge_per_share,
        "gross_edge_per_contract": gross_contract,
        "fees_per_contract": fees,
        "net_edge_per_contract": net,
        "min_quote_size": min_quote_size,
        "detail": detail,
    })


def scan(df: pd.DataFrame, *, rate: float = 0.0, fee_per_contract_leg: float = 0.0,
         default_lot_size: float = 100.0, as_of: Optional[date] = None) -> pd.DataFrame:
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")

    d = df.copy()
    d["option_type"] = d["option_type"].astype(str).str.upper().str[0]
    for col in ["strike", "bid", "ask", "bid_size", "ask_size", "lot_size"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[d["option_type"].isin(["C", "P"])]
    d = d.dropna(subset=["strike"])

    findings: list[dict] = []
    fallback_date = as_of or date.today()

    for (underlying, expiry), grp in d.groupby(["underlying", "expiry"], dropna=False):
        underlying_s, expiry_s = str(underlying), str(expiry)
        lot_vals = grp.get("lot_size")
        lot = default_lot_size
        if lot_vals is not None:
            valid_lot = pd.to_numeric(lot_vals, errors="coerce").dropna()
            if not valid_lot.empty and valid_lot.iloc[0] > 0:
                lot = float(valid_lot.iloc[0])
        snap_date = _snapshot_date(grp, fallback_date)

        # Same-instrument crossed market.
        for _, row in grp.iterrows():
            q = _quote(row)
            if q and q.bid > q.ask:
                _emit(
                    findings, check="crossed_spread", underlying=underlying_s, expiry=expiry_s,
                    strikes=f"{q.strike:g}", legs=f"BUY ask {q.ask:g}; SELL bid {q.bid:g}",
                    gross_edge_per_share=q.bid - q.ask, lot_size=lot, fee_legs=2,
                    fee_per_contract_leg=fee_per_contract_leg,
                    min_quote_size=_size_min(q.ask_size, q.bid_size),
                    detail="same option bid exceeds ask in delayed snapshot",
                )

        by_type: dict[str, list[Quote]] = {"C": [], "P": []}
        for typ in ("C", "P"):
            for _, row in grp[grp["option_type"] == typ].sort_values("strike").iterrows():
                q = _quote(row)
                if q:
                    by_type[typ].append(q)

        # Vertical bounds.  Adjacent strikes are enough for a fast primary screen;
        # box spreads below scan all strike pairs with both C and P available.
        for typ, qs in by_type.items():
            for q1, q2 in zip(qs, qs[1:]):
                if q2.strike <= q1.strike:
                    continue
                width = q2.strike - q1.strike
                pv = _pv_width(width, rate, snap_date, expiry_s)
                if typ == "C":
                    # C(K1) >= C(K2): buy low-strike call at ask, sell high-strike call at bid.
                    edge_lower = q2.bid - q1.ask
                    lower_legs = f"BUY C {q1.strike:g}@{q1.ask:g}; SELL C {q2.strike:g}@{q2.bid:g}"
                    lower_size = _size_min(q1.ask_size, q2.bid_size)
                    # C(K1)-C(K2) <= PV(K2-K1): receive reverse side of long spread.
                    credit = q1.bid - q2.ask
                    edge_upper = credit - pv
                    upper_legs = f"SELL C {q1.strike:g}@{q1.bid:g}; BUY C {q2.strike:g}@{q2.ask:g}"
                    upper_size = _size_min(q1.bid_size, q2.ask_size)
                else:
                    # P(K2) >= P(K1): buy high-strike put, sell low-strike put.
                    edge_lower = q1.bid - q2.ask
                    lower_legs = f"SELL P {q1.strike:g}@{q1.bid:g}; BUY P {q2.strike:g}@{q2.ask:g}"
                    lower_size = _size_min(q1.bid_size, q2.ask_size)
                    # P(K2)-P(K1) <= PV(width).
                    credit = q2.bid - q1.ask
                    edge_upper = credit - pv
                    upper_legs = f"BUY P {q1.strike:g}@{q1.ask:g}; SELL P {q2.strike:g}@{q2.bid:g}"
                    upper_size = _size_min(q1.ask_size, q2.bid_size)

                if edge_lower > 0:
                    _emit(findings, check="vertical_monotonicity", underlying=underlying_s, expiry=expiry_s,
                          strikes=f"{q1.strike:g},{q2.strike:g}", legs=lower_legs,
                          gross_edge_per_share=edge_lower, lot_size=lot, fee_legs=2,
                          fee_per_contract_leg=fee_per_contract_leg, min_quote_size=lower_size,
                          detail=f"{typ} vertical has negative debit")
                if edge_upper > 0:
                    _emit(findings, check="vertical_upper_bound", underlying=underlying_s, expiry=expiry_s,
                          strikes=f"{q1.strike:g},{q2.strike:g}", legs=upper_legs,
                          gross_edge_per_share=edge_upper, lot_size=lot, fee_legs=2,
                          fee_per_contract_leg=fee_per_contract_leg, min_quote_size=upper_size,
                          detail=f"vertical credit exceeds PV(strike width)={pv:.6g} per share")

            # Equal-spaced long butterfly: buy wings at ask, sell 2x body at bid.
            for left, mid, right in zip(qs, qs[1:], qs[2:]):
                if not math.isclose(mid.strike - left.strike, right.strike - mid.strike, rel_tol=1e-9, abs_tol=1e-9):
                    continue
                edge = 2.0 * mid.bid - left.ask - right.ask
                if edge > 0:
                    body_size = (mid.bid_size / 2.0) if mid.bid_size is not None else None
                    _emit(findings, check="butterfly_convexity", underlying=underlying_s, expiry=expiry_s,
                          strikes=f"{left.strike:g},{mid.strike:g},{right.strike:g}",
                          legs=(f"BUY {typ} {left.strike:g}@{left.ask:g}; SELL 2 {typ} {mid.strike:g}@{mid.bid:g}; "
                                f"BUY {typ} {right.strike:g}@{right.ask:g}"),
                          gross_edge_per_share=edge, lot_size=lot, fee_legs=4,
                          fee_per_contract_leg=fee_per_contract_leg,
                          min_quote_size=_size_min(left.ask_size, body_size, right.ask_size),
                          detail="equal-spaced long butterfly has negative debit")

        # Boxes: pair strikes for which all four executable sides are present.
        calls = {q.strike: q for q in by_type["C"]}
        puts = {q.strike: q for q in by_type["P"]}
        strikes = sorted(set(calls) & set(puts))
        for i, k1 in enumerate(strikes):
            for k2 in strikes[i + 1 :]:
                c1, c2, p1, p2 = calls[k1], calls[k2], puts[k1], puts[k2]
                width = k2 - k1
                pv = _pv_width(width, rate, snap_date, expiry_s)

                # Long box = +C(K1)-C(K2)+P(K2)-P(K1), pays K2-K1 at expiry.
                cost = c1.ask - c2.bid + p2.ask - p1.bid
                edge = pv - cost
                if edge > 0:
                    _emit(findings, check="long_box", underlying=underlying_s, expiry=expiry_s,
                          strikes=f"{k1:g},{k2:g}",
                          legs=(f"BUY C {k1:g}@{c1.ask:g}; SELL C {k2:g}@{c2.bid:g}; "
                                f"BUY P {k2:g}@{p2.ask:g}; SELL P {k1:g}@{p1.bid:g}"),
                          gross_edge_per_share=edge, lot_size=lot, fee_legs=4,
                          fee_per_contract_leg=fee_per_contract_leg,
                          min_quote_size=_size_min(c1.ask_size, c2.bid_size, p2.ask_size, p1.bid_size),
                          detail=f"long box cost={cost:.6g} < PV(width)={pv:.6g}")

                # Reverse box receives credit now and owes width at expiry.
                credit = c1.bid - c2.ask + p2.bid - p1.ask
                edge_rev = credit - pv
                if edge_rev > 0:
                    _emit(findings, check="reverse_box", underlying=underlying_s, expiry=expiry_s,
                          strikes=f"{k1:g},{k2:g}",
                          legs=(f"SELL C {k1:g}@{c1.bid:g}; BUY C {k2:g}@{c2.ask:g}; "
                                f"SELL P {k2:g}@{p2.bid:g}; BUY P {k1:g}@{p1.ask:g}"),
                          gross_edge_per_share=edge_rev, lot_size=lot, fee_legs=4,
                          fee_per_contract_leg=fee_per_contract_leg,
                          min_quote_size=_size_min(c1.bid_size, c2.ask_size, p2.bid_size, p1.ask_size),
                          detail=f"reverse box credit={credit:.6g} > PV(width)={pv:.6g}")

    out = pd.DataFrame(findings)
    if not out.empty:
        out = out.sort_values(["net_edge_per_contract", "gross_edge_per_share"], ascending=False).reset_index(drop=True)
    return out


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="normalized quote CSV from fetch_kabuopu_quotes.py")
    p.add_argument("--output", help="findings CSV path; default stdout")
    p.add_argument("--rate", type=float, default=0.0, help="annual continuously compounded risk-free rate, decimal")
    p.add_argument("--fee-per-contract-leg", type=float, default=0.0, help="JPY fee/slippage allowance per option contract leg")
    p.add_argument("--lot-size", type=float, default=100.0, help="fallback shares per option contract")
    p.add_argument("--as-of", help="YYYY-MM-DD fallback if snapshot_time is absent")
    return p.parse_args()


def main() -> int:
    args = _args()
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else None
    df = pd.read_csv(args.input)
    findings = scan(df, rate=args.rate, fee_per_contract_leg=args.fee_per_contract_leg,
                    default_lot_size=args.lot_size, as_of=as_of)
    if args.output:
        findings.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"wrote {len(findings)} finding(s) to {args.output}")
    else:
        if findings.empty:
            print("no quote-level violations after fee allowance")
        else:
            print(findings.to_csv(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
