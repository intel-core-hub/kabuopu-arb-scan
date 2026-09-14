#!/usr/bin/env python3
"""Fetch and normalize JPX/OSE kabu-opu 15-minute delayed quote boards.

The public JPX page links to Quant Research's quote board, which presents CALL and
PUT quotes side-by-side.  This script converts the board into one-option-per-row
CSV suitable for quote-level no-arbitrage screening.

Important: the board is delayed data.  Findings are candidates only and must be
re-checked against live executable quotes before trading.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

import requests

BASE_URL = "https://svc.qri.jp/jpx/kbopm/{underlying}/{month_index}"
JPX_REFERRER = "https://www.jpx.co.jp/derivatives/products/individual/securities-options/"

# OSE market-making names effective 2026-08-03 (official JPX announcement).
MM50_CODES = (
    "1306", "1321", "1489", "1655", "2914", "4063", "4502", "4503", "5401", "5802",
    "5803", "6098", "6146", "6301", "6501", "6503", "6702", "6723", "6752", "6758",
    "6857", "6861", "6902", "6920", "6954", "6981", "7011", "7203", "7267", "7270",
    "7741", "7974", "8001", "8031", "8035", "8058", "8306", "8308", "8316", "8411",
    "8604", "8750", "8766", "8801", "9101", "9107", "9432", "9433", "9983", "9984",
)

OUTPUT_COLUMNS = [
    "snapshot_time", "underlying", "expiry", "month_index", "option_type", "strike",
    "bid", "ask", "bid_size", "ask_size", "bid_iv", "ask_iv", "last", "change",
    "iv", "volume", "open_interest", "settlement", "spot_ref", "lot_size", "source_url",
]


class TableParser(HTMLParser):
    """Small dependency-free HTML table/text extractor.

    It deliberately does not try to model the whole DOM.  QRI's board is tabular,
    and preserving <br> boundaries inside quote/IV cells is the key requirement.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._all_parts: list[str] = []

    @property
    def text(self) -> str:
        return " ".join(x for x in self._all_parts if x).strip()

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in {"td", "th"}:
            self._cell_parts = []
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._table_depth == 1 and tag in {"td", "th"} and self._cell_parts is not None:
            cell = _clean_cell(self._cell_parts)
            if self._row is not None:
                self._row.append(cell)
            self._cell_parts = None
        elif self._table_depth == 1 and tag == "tr":
            if self._rows is not None and self._row is not None and any(self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._table_depth == 1:
                if self._rows:
                    self.tables.append(self._rows)
                self._rows = None
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self._all_parts.append(text)
            if self._cell_parts is not None:
                self._cell_parts.append(text)


def _clean_cell(parts: list[str]) -> str:
    out: list[str] = []
    for p in parts:
        if p == "\n":
            if out and out[-1] != "\n":
                out.append(p)
        else:
            out.append(p)
    text = " ".join(out)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _number(value: str | None) -> Optional[float]:
    if value is None:
        return None
    s = value.strip().replace(",", "").replace("％", "%")
    if not s or s in {"-", "--", "―", "－", "(-)", "—"}:
        return None
    s = s.replace("%", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _integer(value: str | None) -> Optional[int]:
    n = _number(value)
    return int(n) if n is not None else None


def _split_two_lines(cell: str) -> tuple[str, str]:
    parts = [p.strip() for p in cell.splitlines() if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    # Some HTML renderers flatten <br>.  Common quote forms still permit a split
    # when each side contains one parenthesized size.
    hits = re.findall(r"(?:[-+]?\d[\d,.]*|-|―|－)\s*\([^)]*\)", cell)
    if len(hits) >= 2:
        return hits[0], hits[1]
    return (parts[0] if parts else cell.strip()), ""


def _parse_quote_cell(cell: str) -> tuple[Optional[float], Optional[int], Optional[float], Optional[int]]:
    """Return ask, ask_size, bid, bid_size from a two-line QRI quote cell."""
    ask_s, bid_s = _split_two_lines(cell)

    def one(s: str) -> tuple[Optional[float], Optional[int]]:
        price = _number(s.split("(", 1)[0])
        m = re.search(r"\(([^)]*)\)", s)
        size = _integer(m.group(1)) if m else None
        return price, size

    ask, ask_size = one(ask_s)
    bid, bid_size = one(bid_s)
    return ask, ask_size, bid, bid_size


def _parse_iv_cell(cell: str) -> tuple[Optional[float], Optional[float]]:
    ask_s, bid_s = _split_two_lines(cell)
    return _number(ask_s), _number(bid_s)


def _date_from_text(text: str, label: str) -> str:
    # Accept Japanese separators and slash form; normalize to YYYY-MM-DD.
    pat = rf"{re.escape(label)}\s*[:：]?\s*(\d{{4}})[/年](\d{{1,2}})[/月](\d{{1,2}})日?"
    m = re.search(pat, text)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _snapshot_from_text(text: str) -> str:
    m = re.search(
        r"(?:最終更新(?:時刻)?|更新時刻)\s*[:：]?\s*(\d{4})[/年](\d{1,2})[/月](\d{1,2})日?\s+(\d{1,2}):(\d{2})",
        text,
    )
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{m.group(5)}"


def _metadata_from_tables(tables: list[list[list[str]]]) -> tuple[Optional[float], Optional[int]]:
    """Best-effort extraction of underlying reference price and trading unit."""
    spot: Optional[float] = None
    lot: Optional[int] = None
    for table in tables:
        flat = [c for row in table for c in row]
        joined = " ".join(flat)
        # Trading unit is typically displayed as e.g. "100株".
        if "売買単位" in joined or "取引単位" in joined:
            for c in flat:
                m = re.search(r"([\d,]+)\s*株", c)
                if m:
                    lot = int(m.group(1).replace(",", ""))
                    break
        # Prefer a value immediately under/after a 現在値 header.
        for r_idx, row in enumerate(table):
            for c_idx, cell in enumerate(row):
                if "現在値" in cell or "参考価格" in cell:
                    candidates: list[str] = []
                    if r_idx + 1 < len(table) and c_idx < len(table[r_idx + 1]):
                        candidates.append(table[r_idx + 1][c_idx])
                    candidates.extend(row[c_idx + 1 : c_idx + 2])
                    for candidate in candidates:
                        n = _number(candidate)
                        if n is not None and n > 0:
                            spot = n
                            break
            if spot is not None:
                break
    return spot, lot


def _looks_like_option_table(table: list[list[str]]) -> bool:
    joined = " ".join(c for row in table for c in row)
    return ("CALL" in joined.upper() or "コール" in joined) and ("PUT" in joined.upper() or "プット" in joined) and ("権利行使価格" in joined or "行使価格" in joined)


def _is_strike(value: str) -> bool:
    n = _number(value)
    return n is not None and n > 0


def parse_quote_board(html: str, underlying: str, month_index: int, source_url: str = "") -> list[dict[str, object]]:
    parser = TableParser()
    parser.feed(html)
    text = parser.text

    if "<html" not in html.lower() and not parser.tables:
        raise ValueError("response does not look like HTML")

    option_tables = [t for t in parser.tables if _looks_like_option_table(t)]
    if not option_tables:
        raise ValueError("kabu-opu CALL/PUT quote table was not found in the page")

    snapshot = _snapshot_from_text(text)
    expiry = _date_from_text(text, "取引最終日") or _date_from_text(text, "満期日")
    spot, lot = _metadata_from_tables(parser.tables)

    rows: list[dict[str, object]] = []
    for table in option_tables:
        for cells in table:
            if len(cells) < 17:
                continue
            # QRI layout is 8 CALL cols + strike + 8 PUT cols.
            # Ignore rows where the central cell is a heading.
            strike = _number(cells[8])
            if strike is None or not _is_strike(cells[8]):
                continue
            call = cells[:8]
            put = cells[9:17]

            # CALL columns (left -> right): settlement, OI, volume, ask/bid IV,
            # ask/bid quote, IV, change, last.
            c_ask_iv, c_bid_iv = _parse_iv_cell(call[3])
            c_ask, c_ask_size, c_bid, c_bid_size = _parse_quote_cell(call[4])
            rows.append({
                "snapshot_time": snapshot, "underlying": underlying, "expiry": expiry,
                "month_index": month_index, "option_type": "C", "strike": strike,
                "bid": c_bid, "ask": c_ask, "bid_size": c_bid_size, "ask_size": c_ask_size,
                "bid_iv": c_bid_iv, "ask_iv": c_ask_iv, "last": _number(call[7]),
                "change": _number(call[6]), "iv": _number(call[5]), "volume": _integer(call[2]),
                "open_interest": _integer(call[1]), "settlement": _number(call[0]),
                "spot_ref": spot, "lot_size": lot, "source_url": source_url,
            })

            # PUT columns mirror the board: last, change, IV, ask/bid quote,
            # ask/bid IV, volume, OI, settlement.
            p_ask, p_ask_size, p_bid, p_bid_size = _parse_quote_cell(put[3])
            p_ask_iv, p_bid_iv = _parse_iv_cell(put[4])
            rows.append({
                "snapshot_time": snapshot, "underlying": underlying, "expiry": expiry,
                "month_index": month_index, "option_type": "P", "strike": strike,
                "bid": p_bid, "ask": p_ask, "bid_size": p_bid_size, "ask_size": p_ask_size,
                "bid_iv": p_bid_iv, "ask_iv": p_ask_iv, "last": _number(put[0]),
                "change": _number(put[1]), "iv": _number(put[2]), "volume": _integer(put[5]),
                "open_interest": _integer(put[6]), "settlement": _number(put[7]),
                "spot_ref": spot, "lot_size": lot, "source_url": source_url,
            })

    if not rows:
        raise ValueError("quote table was found but no 17-column option rows could be parsed")
    return rows


def _decode_html(content: bytes, declared_encoding: str | None = None) -> str:
    encodings = [declared_encoding, "utf-8", "cp932", "shift_jis", "euc_jp"]
    seen: set[str] = set()
    for enc in encodings:
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return content.decode("utf-8", errors="replace")


def fetch_page(session: requests.Session, underlying: str, month_index: int, timeout: float = 15.0) -> tuple[str, str]:
    url = BASE_URL.format(underlying=underlying, month_index=month_index)
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Referer": JPX_REFERRER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    html = _decode_html(response.content, response.encoding)
    if "CALL" not in html.upper() and "コール" not in html:
        raise RuntimeError(f"{url}: HTTP success but quote-board markers were absent")
    return html, url


def write_csv(rows: Iterable[dict[str, object]], output: str | Path | None) -> None:
    rows = list(rows)
    if output:
        fp = open(output, "w", encoding="utf-8-sig", newline="")
        close = True
    else:
        fp = sys.stdout
        close = False
    try:
        writer = csv.DictWriter(fp, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if close:
            fp.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--underlying", action="append", default=[], help="4-digit code; repeatable")
    p.add_argument("--mm50", action="store_true", help="scan the OSE MM50 universe effective 2026-08-03")
    p.add_argument("--month-index", type=int, action="append", default=None, help="QRI month tab index; repeatable (default: 0,1)")
    p.add_argument("--output", help="output CSV path (default: stdout)")
    p.add_argument("--sleep", type=float, default=0.4, help="seconds between requests")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--html-file", help="parse a saved HTML file instead of network; requires one --underlying")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    codes = list(dict.fromkeys(([str(c) for c in MM50_CODES] if args.mm50 else []) + args.underlying))
    if not codes:
        raise SystemExit("specify at least one --underlying or --mm50")
    months = args.month_index if args.month_index is not None else [0, 1]

    all_rows: list[dict[str, object]] = []
    errors: list[str] = []

    if args.html_file:
        if len(codes) != 1 or len(months) != 1:
            raise SystemExit("--html-file requires exactly one underlying and one month index")
        html = Path(args.html_file).read_text(encoding="utf-8")
        all_rows.extend(parse_quote_board(html, codes[0], months[0], source_url=str(args.html_file)))
    else:
        session = requests.Session()
        # Best effort cookie/referrer bootstrap. Failure here is non-fatal because
        # the external board may still accept a direct request with Referer.
        try:
            session.get(JPX_REFERRER, timeout=args.timeout)
        except requests.RequestException:
            pass
        for i, code in enumerate(codes):
            for month in months:
                try:
                    html, url = fetch_page(session, code, month, timeout=args.timeout)
                    all_rows.extend(parse_quote_board(html, code, month, source_url=url))
                except Exception as exc:  # continue a universe scan, but report failures
                    errors.append(f"{code}/{month}: {exc}")
                if args.sleep > 0 and (i != len(codes) - 1 or month != months[-1]):
                    time.sleep(args.sleep)

    if not all_rows:
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        return 2

    write_csv(all_rows, args.output)
    for e in errors:
        print(f"WARN {e}", file=sys.stderr)
    print(f"parsed {len(all_rows)} option rows from {len(codes)} underlying(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
