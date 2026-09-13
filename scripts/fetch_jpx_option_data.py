"""JPXのオプション理論価格等情報を取得してCSV保存するCLI。

JPXの配信URLは契約・ログイン状態によって変わることがあるため、URLテンプレートは
コマンドラインまたは環境変数 ``JPX_OPTION_DATA_URL_TEMPLATE`` で指定できる。
テンプレートでは ``{date}`` (YYYYMMDD) を使用する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
from pathlib import Path
import zipfile

import pandas as pd
import requests

DEFAULT_URL_TEMPLATE = "https://www.jpx.co.jp/markets/derivatives/option-price/data/{date}.zip"


def _read_csv(payload: bytes, name: str) -> pd.DataFrame:
    """JPX配信で使われうる文字コードと区切り文字を順に試す。"""
    errors: list[str] = []
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        for sep in (None, ",", "\t"):
            try:
                return pd.read_csv(io.BytesIO(payload), encoding=encoding, sep=sep, engine="python")
            except (UnicodeDecodeError, pd.errors.ParserError) as exc:
                errors.append(f"{encoding}/{sep}: {exc}")
    raise ValueError(f"CSV {name!r} を読み込めませんでした。試行結果: {'; '.join(errors)}")


def _select_data_file(zf: zipfile.ZipFile) -> str:
    files = [name for name in zf.namelist() if not name.endswith("/")]
    csv_files = [name for name in files if name.lower().endswith((".csv", ".txt"))]
    if not csv_files:
        raise ValueError(f"ZIPにCSV/TXTファイルがありません: {files}")
    return csv_files[0]


def _reject_non_data_response(response: requests.Response) -> bytes:
    """HTTP 200でも、ログイン/エラーページ等のHTML応答を非データとして拒否する。

    未認証アクセスがログインページ等にリダイレクトされてHTTP 200を返すと、
    raise_for_status() では検出できず、pandasがそのHTMLを空・1行のCSVとして
    受理してしまうことがある。Content-Typeまたは本文先頭のHTML兆候で弾く。
    """
    payload = response.content
    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        raise ValueError(
            f"データではなくHTMLが返されました(Content-Type: {content_type!r})。"
            "認証切れ・アクセス拒否等でログイン/エラーページにリダイレクトされた可能性があります。"
        )
    if payload.lstrip()[:256].lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("データではなくHTMLが返されました。認証切れ・アクセス拒否の可能性があります。")
    return payload


def fetch_option_theoretical_price(
    target_date: dt.date, *, url_template: str = DEFAULT_URL_TEMPLATE, timeout: int = 30
) -> pd.DataFrame:
    """指定取引日のデータを取得し、元の列名を保ったDataFrameを返す。"""
    url = url_template.format(date=target_date.strftime("%Y%m%d"))
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = _reject_non_data_response(response)

    if zipfile.is_zipfile(io.BytesIO(payload)):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            name = _select_data_file(zf)
            return _read_csv(zf.read(name), name)
    return _read_csv(payload, url)


def filter_kabuopu(df: pd.DataFrame) -> pd.DataFrame:
    """商品種別・銘柄名から有価証券（個別株）オプションの候補を抽出する。

    配信元ごとに列名は異なるため、明示的な商品種別列があればそれを優先する。該当列が
    ない場合は、日経225・TOPIXを除外した上で全行を返す。結果は必ず目視確認すること。
    """
    product_columns = ("商品種別", "商品", "取引種別", "Product Type", "product_type")
    name_columns = ("銘柄名", "銘柄名称", "限月取引名称", "Security Name", "name")
    for column in product_columns:
        if column in df.columns:
            values = df[column].astype(str)
            return df.loc[values.str.contains(r"有価証券|個別株|stock", case=False, regex=True, na=False)].copy()
    for column in name_columns:
        if column in df.columns:
            values = df[column].astype(str)
            excluded = values.str.contains(r"日経\s*225|TOPIX", case=False, regex=True, na=False)
            return df.loc[~excluded].copy()
    return df.copy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JPXオプション理論価格等情報を取得する")
    parser.add_argument("--date", required=True, type=dt.date.fromisoformat, help="取引日 (YYYY-MM-DD)")
    parser.add_argument("--output", type=Path, help="出力CSV。未指定時は data/ 配下")
    parser.add_argument("--url-template", default=os.getenv("JPX_OPTION_DATA_URL_TEMPLATE", DEFAULT_URL_TEMPLATE))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--kabuopu-only", action="store_true", help="個別株オプション候補だけを保存する")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    df = fetch_option_theoretical_price(args.date, url_template=args.url_template, timeout=args.timeout)
    if args.kabuopu_only:
        df = filter_kabuopu(df)
    output = args.output or Path("data") / f"option_theoretical_price_{args.date:%Y%m%d}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"{len(df):,} 行を保存しました: {output}")
    print("列:", ", ".join(map(str, df.columns)))


if __name__ == "__main__":
    main()
