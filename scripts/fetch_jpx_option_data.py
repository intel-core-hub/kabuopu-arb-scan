"""
JPXの「オプション理論価格等情報」を取得するスクリプト(たたき台)。

【要確認・未完成】
2018年時点ではブログ記事で以下のURLパターンでの無料ダウンロードが確認できたが、
2026年9月時点でJPXの提供方法(URL・料金体系)が同じかどうかは未確認。
    http://www.jpx.co.jp/markets/derivatives/option-price/data/[ファイル名].zip

現在のJPX公式ページ(下記)はJavaScript必須のため、機械的な取得には
実際のダウンロードリンクをブラウザの開発者ツール等で確認し、
DOWNLOAD_URL_TEMPLATE を更新する必要がある。
    https://www.jpx.co.jp/markets/derivatives/option-price/index.html

かぶオプ(個別株オプション)が本当にこのデータセットに含まれるかも
未確認(公式ページの説明文では「オプション取引全銘柄」と記載されているため
含まれる可能性が高いと推測しているだけ)。
"""

import datetime
import io
import zipfile

import pandas as pd
import requests

# TODO: 実際のURLパターンに要修正(上記コメント参照)
DOWNLOAD_URL_TEMPLATE = (
    "https://www.jpx.co.jp/markets/derivatives/option-price/data/{date}.zip"
)


def fetch_option_theoretical_price(target_date: datetime.date) -> pd.DataFrame:
    """指定日のオプション理論価格等情報を取得してDataFrameで返す。

    Parameters
    ----------
    target_date : datetime.date
        取得したい取引日

    Returns
    -------
    pd.DataFrame
        銘柄コード・限月・プレミアム終値・理論価格・IV・対象証券終値等を含む想定。
        実際のカラム構成はデータを取得してから確認・修正すること。
    """
    date_str = target_date.strftime("%Y%m%d")
    url = DOWNLOAD_URL_TEMPLATE.format(date=date_str)

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # ZIP内のファイル名は要確認
        names = zf.namelist()
        print(f"ZIP内ファイル: {names}")
        with zf.open(names[0]) as f:
            df = pd.read_csv(f, encoding="shift_jis")

    return df


def filter_kabuopu(df: pd.DataFrame) -> pd.DataFrame:
    """個別株オプション(かぶオプ)の行だけを抽出する(たたき台)。

    実際のデータを見てから、どの列で日経225/TOPIXオプションと
    個別株オプションを区別できるかを確認して実装すること。
    """
    raise NotImplementedError("実データ取得後にフィルタ条件を実装してください")


if __name__ == "__main__":
    today = datetime.date.today()
    df = fetch_option_theoretical_price(today)
    print(df.head())
    df.to_csv(f"data/option_theoretical_price_{today.strftime('%Y%m%d')}.csv", index=False)
