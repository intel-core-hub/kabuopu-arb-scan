# Phase 2: delayed quote-board ingestion + executable-side arbitrage screening

This patch is intentionally additive so it can be applied after the existing JPX theoretical-price fixes without overwriting them.

## Why this is the next step

JPX currently links a public **15-minute delayed** “かぶオプ価格情報” board that shows quote status and trading volume. JPX also announced that the market-making universe expanded from 32 to 50 underlyings effective 2026-08-03.

The free end-of-day theoretical-price CSV is still useful for IV/theory diagnostics, but it does not contain executable bid/ask quotes. Therefore the quote board is the better source for testing whether static-arbitrage violations survive the bid/ask spread.

Official references:

- https://www.jpx.co.jp/news/2041/20260605-02.html
- https://www.jpx.co.jp/ose-toshijuku/column/09.html
- https://www.jpx.co.jp/derivatives/products/individual/securities-options/02.html

The JPX site links to the external quote service under `https://svc.qri.jp/jpx/kbopm/`. Example per-underlying paths used by JPX educational materials have the form `/7203/0`, `/7203/1`, etc.

## Added files

- `scripts/fetch_kabuopu_quotes.py`
  - browser-like request headers and JPX referer
  - dependency-free HTML table parser
  - normalizes the side-by-side CALL/strike/PUT board into one option per CSV row
  - preserves bid, ask, displayed size, bid/ask IV, last, volume, OI, settlement, spot reference and lot size
  - `--mm50` scans the official 50-name MM universe
  - supports `--html-file` for reproducible offline parsing/tests
- `scripts/quote_arbitrage_scan.py`
  - crossed spread
  - vertical monotonicity lower bound
  - vertical upper bound versus PV(strike width)
  - equal-spaced butterfly convexity using executable sides
  - long/reverse box spread using executable sides
  - fee allowance in JPY per option contract leg
  - reports gross and net edge per contract
- `tests/test_quote_phase2.py`
  - synthetic 17-column quote-board fixture
  - parser validation
  - long-box, vertical and butterfly tests

## Usage

One underlying, two nearest month tabs:

```bash
python scripts/fetch_kabuopu_quotes.py --underlying 7203 --output data/quotes_7203.csv
python scripts/quote_arbitrage_scan.py data/quotes_7203.csv --output data/findings_7203.csv
```

The official market-making 50-name universe:

```bash
python scripts/fetch_kabuopu_quotes.py --mm50 --output data/quotes_mm50.csv
python scripts/quote_arbitrage_scan.py data/quotes_mm50.csv \
  --fee-per-contract-leg 100 \
  --output data/findings_mm50.csv
```

If QRI blocks programmatic HTTP from the current network, save the quote-board HTML in a normal browser and parse it offline:

```bash
python scripts/fetch_kabuopu_quotes.py \
  --underlying 7203 --month-index 0 \
  --html-file data/7203_0.html \
  --output data/quotes_7203.csv
```

## Interpretation

The quote source is delayed by about 15 minutes. A positive scanner result is therefore **not** a trade signal. It is a candidate for a second-stage live check (for example in IBKR) where all legs must be simultaneously executable in sufficient size after commissions/slippage.

Box spreads are especially useful in this phase because the expiry payoff is fixed and the stock/dividend leg cancels. They provide a cleaner sanity check than put-call parity when only option quotes are available.

## Branch cleanup

Do not delete `codex` while PR #1 is still open unless PR #1 is intentionally being abandoned. After the corrected work is merged into `main`, the safe cleanup is:

```bash
gh pr merge 1 --squash --delete-branch
git switch main
git pull --ff-only
git fetch --prune
git branch -D codex 2>/dev/null || true
```

If the PR was already merged but the remote branch remains:

```bash
git push origin --delete codex
git fetch --prune
```
