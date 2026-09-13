"""個別株オプションの静的無裁定条件を検査するCLI。"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = {"underlying", "expiry", "option_type", "strike", "bid", "ask", "settlement", "iv", "spot"}
ALIASES = {
    "underlying": ("underlying", "原資産", "原資産コード", "対象証券コード"),
    "expiry": ("expiry", "満期日", "限月", "SQ日"),
    "option_type": ("option_type", "プット・コール", "コール・プット", "CP"),
    "strike": ("strike", "権利行使価格", "行使価格"),
    "bid": ("bid", "買気配", "最良買気配", "Bid"),
    "ask": ("ask", "売気配", "最良売気配", "Ask"),
    "settlement": ("settlement", "清算値", "終値", "プレミアム終値", "価格"),
    "iv": ("iv", "IV", "インプライドボラティリティ", "ボラティリティ"),
    "spot": ("spot", "対象証券終値", "原資産価格", "原資産終値"),
}


def normalize_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """JPX等の列名をスキャンで使う標準列名に寄せる。"""
    rename = {}
    for canonical, aliases in ALIASES.items():
        match = next((column for column in aliases if column in raw.columns), None)
        if match:
            rename[match] = canonical
    frame = raw.rename(columns=rename).copy()
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError("必要な列を特定できません: " + ", ".join(sorted(missing)) + f"。入力列: {list(raw.columns)}")
    frame["option_type"] = frame["option_type"].astype(str).str.upper().replace({"コール": "C", "プット": "P", "CALL": "C", "PUT": "P"})
    frame["expiry"] = pd.to_datetime(frame["expiry"], errors="coerce")
    for column in ("strike", "bid", "ask", "settlement", "iv", "spot"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _violation(kind: str, rows: pd.DataFrame, detail: str) -> dict[str, object]:
    first = rows.iloc[0]
    return {"check": kind, "underlying": first.underlying, "expiry": first.expiry, "option_type": first.option_type, "strikes": ",".join(map(str, rows.strike)), "detail": detail}


def scan(frame: pd.DataFrame, tolerance: float = 1e-8, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """クロススプレッド、バタフライ凸性、カレンダー総分散、put-call parityを検査する。"""
    findings: list[dict[str, object]] = []
    as_of = pd.Timestamp(as_of or dt.date.today()).normalize()
    valid = frame.copy()
    valid["expiry"] = pd.to_datetime(valid["expiry"], errors="coerce")
    valid = valid.dropna(subset=["underlying", "expiry", "option_type", "strike"]).copy()
    for _, row in valid.dropna(subset=["bid", "ask"]).iterrows():
        if row.bid > row.ask + tolerance:
            findings.append(_violation("crossed_spread", pd.DataFrame([row]), f"bid {row.bid} > ask {row.ask}"))

    for _, group in valid.dropna(subset=["settlement"]).groupby(["underlying", "expiry", "option_type"]):
        ordered = group.sort_values("strike")
        for index in range(1, len(ordered) - 1):
            left, middle, right = ordered.iloc[index - 1], ordered.iloc[index], ordered.iloc[index + 1]
            interpolated = left.settlement + (right.settlement - left.settlement) * (middle.strike - left.strike) / (right.strike - left.strike)
            if middle.settlement > interpolated + tolerance:
                findings.append(_violation("butterfly_convexity", ordered.iloc[index - 1:index + 2], f"middle {middle.settlement:.6g} > linear bound {interpolated:.6g}"))

    for (underlying, strike, option_type), group in valid.dropna(subset=["iv"]).groupby(["underlying", "strike", "option_type"]):
        ordered = group.sort_values("expiry")
        years = (ordered.expiry - as_of).dt.days / 365.0
        if (years <= 0).any():
            continue
        total_variance = (ordered.iv / 100.0) ** 2 * years
        for index in range(1, len(ordered)):
            if total_variance.iloc[index] + tolerance < total_variance.iloc[index - 1]:
                findings.append(_violation("calendar_total_variance", ordered.iloc[index - 1:index + 1], f"total variance {total_variance.iloc[index]:.6g} < {total_variance.iloc[index - 1]:.6g}"))

    parity = valid.dropna(subset=["settlement", "spot"]).pivot_table(
        index=["underlying", "expiry", "strike", "spot"], columns="option_type", values="settlement", aggfunc="first"
    )
    if {"C", "P"}.issubset(parity.columns):
        parity = parity.dropna(subset=["C", "P"])
    else:
        parity = parity.iloc[0:0]
    for index, row in parity.iterrows():
        underlying, expiry, strike, spot = index
        residual = row.C - row.P - (spot - strike)  # 金利・配当を無視した一次スクリーニング
        if abs(residual) > tolerance:
            rows = valid[(valid.underlying == underlying) & (valid.expiry == expiry) & (valid.strike == strike)]
            findings.append(_violation("put_call_parity", rows, f"C-P-(S-K) = {residual:.6g}; 金利・配当調整前"))
    return pd.DataFrame(findings, columns=["check", "underlying", "expiry", "option_type", "strikes", "detail"])


def main() -> None:
    parser = argparse.ArgumentParser(description="オプション静的無裁定性を検査する")
    parser.add_argument("input", type=Path, help="標準列または対応するJPX列名を持つCSV")
    parser.add_argument("--output", type=Path, default=Path("data/arbitrage_findings.csv"))
    parser.add_argument("--tolerance", type=float, default=1e-8)
    parser.add_argument("--as-of", type=pd.Timestamp, help="評価日 (YYYY-MM-DD)。未指定時は当日")
    args = parser.parse_args()
    findings = scan(normalize_columns(pd.read_csv(args.input)), args.tolerance, args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    findings.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"検出件数: {len(findings):,}; 出力: {args.output}")


if __name__ == "__main__":
    main()
