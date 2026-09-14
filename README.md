# kabuopu-arb-scan

個別株オプション(通称「かぶオプ」、大阪取引所の有価証券オプション)を対象に、
数学的優位性(確率解析・無裁定条件)が実際に残っているかを調べるプロジェクト。

## 背景・経緯

これまでの実弾トラック(commodity-trend-following → keiba_ai → defi-yield-optimizer →
neutral-carry-engine → topix-index-event-strategy)が4連続NO-GO/KILLとなったのを受け、
「代数幾何(確率解析)という自分の専門性を直接使える場所」を探す新トラックとして開始。

最初のアイデアはバリア・アジアン等のエキゾチックオプションの理論価格と市場価格の
乖離だったが、調査の結果:

- Deribit等の海外暗号資産オプション取引所は、2020年5月の資金決済法改正対応で
  日本居住者向けサービスを停止しており、実質アクセス不可
- 東証カバードワラント(バリア型含む)は、商品性の詳細・現在の上場構成が未確認
- 日経225/TOPIXオプション(指数)は流動性が高すぎてプロが徹底監視しており、
  無裁定条件の明白な違反はほぼ残っていないと考えられる

一方で、個別株オプション(かぶオプ)は次の理由で最有力候補と判断した:

- 取扱証券会社は3社のみ(インタラクティブ・ブローカーズ証券・光世証券・moomoo証券)。
  このうちインタラクティブ・ブローカーズ証券はintelさんが既に口座を持っている
- 200銘柄超の原資産に対しシリーズ数は1万超あるが、JPXの資料によれば
  「多くの銘柄に気配がないのが現実」で、2024年8月までは取引の9割以上が
  J-NET(相対)経由。2024年9月のマーケットメイク再開で改善傾向はあるが依然薄い
  → 機関投資家が本気で見ていない領域が実在する可能性がある
- 弱点: 気配が薄い分、歪みを見つけても実際に約定できるとは限らない(執行リスク)

## 現在のステータス(2026-09-13時点)

- [x] 取扱証券会社の特定
- [ ] JPX「オプション理論価格等情報」の現在(2026年)の提供形態・料金・URLの確認
      ※ 2018年時点のブログ記事では無料CSV配信を確認できたが、現在は
      JPXデータカタログ/クライアントポータル経由に変わっている可能性があり未確認
- [ ] かぶオプ銘柄が実際にデータに含まれるかの確認
- [ ] 1〜2銘柄でのIV(インプライドボラティリティ)サーフェス作成・無裁定条件
      (バタフライ・カレンダー・put-callパリティ)の机上チェック
- [ ] 実際に気配値・板がどこまで見えるか(IBKR等での確認)

## 次の一歩

1. `fetch_jpx_option_data.py` を使って、JPXの公開データから個別株オプションの
   気配値・理論価格・IVが取得できるか確認する(URLが未確定のため要調査・要修正)
2. 取得できたら、put-callパリティとバタフライ・カレンダー・スプレッドの
   無裁定条件を1〜2銘柄でチェックするノートブック/スクリプトを書く
3. 明らかな違反が見つかれば、執行可能性(気配の厚み)を確認するフェーズに進む

## ディレクトリ構成

```
kabuopu-arb-scan/
├── README.md
├── PHASE2_NOTES.md                # Phase 2(気配値ベース検査)の詳細ドキュメント
├── PHASE3_NOTES.md                # Phase 3(IBKRライブ再検証)の詳細ドキュメント
├── PHASE4_NOTES.md                # Phase 4(同時ストリーミング持続性検査)の詳細ドキュメント
├── PHASE5_NOTES.md                # Phase 5(複数セッション再現性ゲート)の詳細ドキュメント
├── requirements.txt
├── requirements-ibkr.txt          # Phase 3/4専用の追加依存(ibapi)
├── .gitignore
├── scripts/
│   ├── fetch_jpx_option_data.py   # JPXオプション理論価格データ取得(URL要確認)
│   ├── option_arbitrage_scan.py   # 理論価格ベースの静的無裁定性スキャナ
│   ├── fetch_kabuopu_quotes.py    # かぶオプ気配値ボード(15分遅延)取得
│   ├── quote_arbitrage_scan.py    # 気配値(bid/ask)ベースの静的無裁定性スキャナ
│   ├── ibkr_validate_findings.py  # IBKRライブ気配での単発再検証(発注は一切行わない)
│   ├── ibkr_monitor_findings.py   # IBKR同時ストリーミングでの持続性検査(発注は一切行わない)
│   └── evaluate_persistence.py    # 複数セッションを集計する再現性ゲート(IBKR接続なし)
├── tests/                         # 上記スクリプトの単体テスト
└── data/                          # 取得したデータの置き場(gitignore対象)
```

## セットアップ

```bash
python -m venv venv
source venv/bin/activate  # Windowsは venv\Scripts\activate
pip install -r requirements.txt
```

## 実装済みのデータ取得・スキャン手順

> **データ提供形態の確認状況:** 実行環境からJPX公式サイトへのHTTPS接続はプロキシで
> 拒否されたため、2026年9月13日現在の配信URL・料金・個別株オプション収録有無を
> このリポジトリ内では未検証です。取得スクリプトは旧来の公開URLを既定値として持ち
> ますが、実運用では契約済みのJPXデータ配信URLを指定してください。

### 1. データを取得する

```bash
python scripts/fetch_jpx_option_data.py \
  --date 2026-09-11 \
  --url-template 'https://<JPX提供URL>/{date}.zip' \
  --kabuopu-only
```

URLテンプレートは `JPX_OPTION_DATA_URL_TEMPLATE` 環境変数でも指定できます。ZIP内の
CSV/TXT（CP932、UTF-8、CSV/TSV）を読み取り、UTF-8 BOM付きCSVとして `data/` に保存
します。`--kabuopu-only` は商品種別に「有価証券」「個別株」「stock」を含む行を優先し、
商品種別がない場合は日経225/TOPIX名称を除外する**候補抽出**です。保存結果は必ず
銘柄コード・商品種別で確認してください。

### 2. 静的無裁定性をスキャンする

```bash
python scripts/option_arbitrage_scan.py data/option_theoretical_price_20260911.csv \
  --as-of 2026-09-11 \
  --output data/arbitrage_findings_20260911.csv
```

スキャナは標準列 `underlying`, `expiry`, `option_type`, `strike`, `bid`, `ask`,
`settlement`, `iv`, `spot`、または対応する日本語JPX列名を受け付けます。出力CSVには、
以下の一次スクリーニング結果を記録します。

- `crossed_spread`: 買気配が売気配を上回る。
- `butterfly_convexity`: 同一満期・同一種別の権利行使価格に対して価格凸性に反する。
- `calendar_total_variance`: 同一原資産・権利行使価格・種別で満期が先の総分散が小さい。
- `put_call_parity`: `C - P - (S - K)` が許容値を超える。これは金利・配当・取引単位を
  未調整の候補抽出であり、そのまま裁定機会を意味しません。

検出結果は、金利・予想配当・契約乗数・権利落ち・気配の時点差を調整して再計算し、
IBKR等の板で数量、両建て可否、手数料、レッグ約定リスクを確認してから評価します。

### 3. (Phase 2) 気配値(bid/ask)ベースで執行可能性を検査する

理論価格は気配のスプレッドを含まないため、実際に約定できる無裁定違反かどうかは
気配値ベースで別途検査する必要があります。`fetch_kabuopu_quotes.py` が15分遅延の
かぶオプ気配値ボードを取得し、`quote_arbitrage_scan.py` がクロススプレッド・
バーティカル・バタフライ・ボックススプレッドを手数料込みで検査します。

```bash
python scripts/fetch_kabuopu_quotes.py --underlying 7203 --output data/quotes_7203.csv
python scripts/quote_arbitrage_scan.py data/quotes_7203.csv \
  --fee-per-contract-leg 100 \
  --output data/findings_7203.csv
```

詳細な設計・解釈上の注意点(気配は15分遅延であり検出結果はそのまま裁定機会を
意味しないこと等)は [`PHASE2_NOTES.md`](PHASE2_NOTES.md) を参照してください。

### 4. (Phase 3) IBKRのライブ気配で再検証する

かぶオプ気配値ボードは15分遅延のため、有力な候補だけをIBKRのライブ気配で
再評価します。`ibkr_validate_findings.py` は**発注・変更・取消を一切行わない
読み取り専用**のスクリプトです。TWS/IB Gatewayが起動し、APIクライアント接続
が有効になっている必要があります。

```bash
pip install -r requirements-ibkr.txt

python scripts/ibkr_validate_findings.py \
  data/findings_7203.csv \
  --market-data-type live \
  --exchange OSE.JPN \
  --limit 20 \
  --output data/live_validation_7203.csv
```

- `CONFIRMED_CANDIDATE`: 手数料考慮後も正のエッジが残り、かつ**全レッグがIBKRから
  明示的にliveと報告された**場合のみ
- `NONLIVE_CANDIDATE`: 正のエッジは残るが、いずれかのレッグがfrozen/delayed/
  delayed-frozenだった場合(研究用の参考値であり、ライブ確認扱いにしない)
- `UNVERIFIED_DATA_TYPE`: 正のエッジは残るが、IBKRからmarketDataTypeの
  コールバックが得られず、live/非liveを判定できなかった場合
- `NO_LONGER_POSITIVE`: 受信した実行可能気配でエッジが消失した場合
- `ERROR`: 銘柄特定失敗・気配欠落・API/権限エラー等

`CONFIRMED_CANDIDATE` はあくまで研究上の候補であり、発注前に数量・乗数・
手数料・空売り可否・再取得での再現性・複数レッグ約定リスクを必ず確認して
ください。詳細は [`PHASE3_NOTES.md`](PHASE3_NOTES.md) を参照してください。

### 5. (Phase 4) 複数レッグを同時ストリーミングして持続性を検査する

Phase 3の単発スナップショットでは、たまたま瞬間的・内部的に不整合な組み合わせを
捉えてしまう可能性があります。`ibkr_monitor_findings.py` は候補の全レッグを
同時に購読し続け(`reqMktData(..., snapshot=False)`)、一定間隔でサンプリングして
エッジが持続するかを検査します。こちらも**発注・変更・取消は一切行わない
読み取り専用**で、購読終了時は `cancelMktData` のみを呼びます。

```bash
python scripts/ibkr_monitor_findings.py data/findings_7203.csv \
  --market-data-type live \
  --exchange OSE.JPN \
  --limit 5 \
  --duration 10 \
  --sample-interval 0.5 \
  --warmup 2 \
  --max-quote-age 3 \
  --output data/persistence_7203.csv
```

- `PERSISTENT_LIVE_CANDIDATE`: live判定かつ正のエッジの観測が監視時間の80%以上
  継続した場合(保守的な下限推定による)
- `INTERMITTENT_LIVE_CANDIDATE`: live判定かつ正のエッジの観測はあったが、
  持続時間が短かった場合
- `NO_PERSISTENT_LIVE_EDGE`: live判定かつ正のエッジの観測が一度もなかった場合
- `ERROR`: 銘柄特定失敗・API・入力エラー等

frozen/delayedな気配がliveとして扱われることはなく、古くなった実行可能価格・
サイズは持ち越さずに除外されます。表示サイズが不足・欠落している観測もlive陽性
としてカウントしません。これも研究シグナルであり執行保証ではないため、詳細は
[`PHASE4_NOTES.md`](PHASE4_NOTES.md) を参照してください。

### 6. (Phase 5) 複数セッションにまたがる再現性を評価する

Phase 4は「今回の短い監視時間だけ」の持続性しか答えません。`evaluate_persistence.py`
は複数回実行したPhase 4のCSV(またはglobパターン)を集計し、同一候補が独立した
セッションを跨いで再現するかを判定します。**IBKRには一切接続しない**、完全に
オフラインの集計・分析ツールです。

```bash
# Phase 4を複数回実行し、実行の都度別ファイルに保存
python scripts/ibkr_monitor_findings.py data/findings_mm50.csv \
  --market-data-type live --exchange OSE.JPN --limit 10 --duration 10 \
  --output data/persistence_20260914_1200.csv

# 何回か蓄積したら集計
python scripts/evaluate_persistence.py 'data/persistence_*.csv' \
  --min-sessions 3 \
  --min-persistent-sessions 2 \
  --min-edge-jpy 1000 \
  --output data/reproducibility_ranking.csv \
  --summary-json data/reproducibility_summary.json
```

- `PROMOTE_TO_EXECUTION_STUDY`: セッション数・持続性・エッジ・サイズ・気配同期の
  各ゲートを全て満たした場合(「執行メカニズムを検討する価値がある」であり
  「発注してよい」ではない)
- `KEEP_OBSERVING`: 陽性の証拠はあるが、いずれかのゲートが未達
- `INSUFFICIENT_EVIDENCE`: 有効なセッション数がまだ足りない
- `NO_REPRODUCIBLE_LIVE_EDGE`: 十分なセッションがあるがlive陽性が一度もない

複数日にまたがる証拠を求める場合は `--min-distinct-days` を(例えば3に)上げて
ください。`PROMOTE_TO_EXECUTION_STUDY` になった候補も研究上のハンドオフに
過ぎません。約定の非原子性・表示サイズの消失・手数料・証拠金・乗数・権利行使/
割当メカニズム等は別途必ず検証してください。詳細は
[`PHASE5_NOTES.md`](PHASE5_NOTES.md) を参照してください。
