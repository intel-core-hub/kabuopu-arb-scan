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

## 現在のステータス(2026-09-14時点)

- [x] 取扱証券会社の特定
- [x] JPX「オプション理論価格等情報」が2026年も公開CSVで日々配信され、
      オプション全銘柄を対象とすることを公式ページで確認
- [x] 有価証券オプション(かぶオプ)の対象証券一覧と、2026年8月以降の
      マーケットメイク対象50銘柄を公式ページで確認
- [x] Phase 2〜4: 気配値ベース無裁定スキャン、IBKRデータ種別ガード、
      同時ストリーミング持続性検査を実装
- [x] Phase 5〜7: 再現性ゲート、What-If、DUペーパー口座限定のBAG実験を実装
- [ ] 実市場でPhase 2〜5を複数セッション実測し、再現候補の有無を確認
- [ ] Phase 6 What-If通過候補が出た場合のみ、Phase 7ペーパートレースを複数回収集

## 次の一歩

1. 市場時間中にPhase 2〜5を実データで複数回まわし、再現候補を作る
2. `PROMOTE_TO_EXECUTION_STUDY` 候補だけPhase 6 What-If、必要ならPhase 7 paperへ進める
3. Phase 7トレースをPhase 8で集計し、BAG/OPT報告形態・callback順序・combo拒否を評価する
4. Phase 8が良好でもlive原子性は未証明のままなので、broker/exchange仕様確認を次ゲートにする

## ディレクトリ構成

```
kabuopu-arb-scan/
├── README.md
├── PHASE2_NOTES.md                # Phase 2(気配値ベース検査)の詳細ドキュメント
├── PHASE3_NOTES.md                # Phase 3(IBKRライブ再検証)の詳細ドキュメント
├── PHASE4_NOTES.md                # Phase 4(同時ストリーミング持続性検査)の詳細ドキュメント
├── PHASE5_NOTES.md                # Phase 5(複数セッション再現性ゲート)の詳細ドキュメント
├── PHASE6_NOTES.md                # Phase 6(IBKR What-If執行可能性調査)の詳細ドキュメント
├── PHASE7_NOTES.md                # Phase 7(ペーパー口座コンボ約定メカニズム実験)の詳細ドキュメント
├── PHASE8_NOTES.md                # Phase 8(複数ペーパートレースの約定メカニズム集計)の詳細ドキュメント
├── PHASE9_NOTES.md                # Phase 9(ベニュー/ブローカー確認ゲート)の詳細ドキュメント
├── PHASE9_BROKER_QUESTIONS.md     # IBKRサポートへ確認すべき質問リスト
├── PHASE10_NOTES.md               # Phase 10(非原子執行の気配リプレイ)の詳細ドキュメント
├── PHASE11_NOTES.md               # Phase 11(OSEリアルタイム/L2 API能力ゲート)の詳細ドキュメント
├── PHASE12_NOTES.md               # Phase 12(表示気配タッチの持続性実測)の詳細ドキュメント
├── requirements.txt
├── requirements-ibkr.txt          # Phase 3/4/6/7/11/12専用の追加依存(ibapi)
├── .gitignore
├── config/
│   └── phase9_venue_broker_evidence.example.json # Phase 9証拠JSONのテンプレート
├── scripts/
│   ├── fetch_jpx_option_data.py   # JPXオプション理論価格データ取得(URL要確認)
│   ├── option_arbitrage_scan.py   # 理論価格ベースの静的無裁定性スキャナ
│   ├── fetch_kabuopu_quotes.py    # かぶオプ気配値ボード(15分遅延)取得
│   ├── quote_arbitrage_scan.py    # 気配値(bid/ask)ベースの静的無裁定性スキャナ
│   ├── ibkr_validate_findings.py  # IBKRライブ気配での単発再検証(発注は一切行わない)
│   ├── ibkr_monitor_findings.py   # IBKR同時ストリーミングでの持続性検査(発注は一切行わない)
│   ├── evaluate_persistence.py    # 複数セッションを集計する再現性ゲート(IBKR接続なし)
│   ├── ibkr_execution_study.py    # IBKR What-Ifプレビューのみ(実発注は一切行わない)
│   ├── ibkr_paper_combo_test.py   # DU口座限定のペーパー約定実験(実弾口座には送信できない)
│   ├── analyze_paper_combo_traces.py # Phase 7トレースのオフライン集計(IBKR接続なし)
│   ├── evaluate_broker_confirmation.py # ベニュー/ブローカー証拠に基づく最終ゲート(IBKR接続なし)
│   ├── simulate_nonatomic_execution.py # レッグ別逐次約定の気配リプレイ(IBKR接続なし)
│   ├── ibkr_probe_marketdata_capability.py # OSEリアルタイムL1/L2データ能力の読み取り専用プローブ(発注系API一切なし)
│   ├── ibkr_record_depth_survival.py # OSE L2板情報の読み取り専用記録(発注系API一切なし)
│   └── analyze_touch_survival.py  # 表示気配タッチの持続性をオフライン分析(IBKR接続なし)
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

### 7. (Phase 6) IBKR What-Ifで執行可能性を調査する

Phase 5で `PROMOTE_TO_EXECUTION_STUDY` になった候補について、`ibkr_execution_study.py`
はさらに狭い3つの問いだけを調べます:IBKRが全レッグを`BAG`として解決し
**What-Ifプレビュー**を受理するか、想定される手数料・証拠金インパクトはどの程度か、
そしてPhase 4/5が仮定した手数料をWhat-Ifの実見積りに置き換えてもエッジが正のまま
残るか。**この段階でも一切発注しません**。唯一の`placeOrder`呼び出しは
`order.whatIf is True` というハード不変条件で保護されており、CLIのどのオプションを
使ってもこの値を変えることはできません。IBKRはWhat-If注文を「発注先には送信され
ないプレビュー/与信チェック」と定義しています。

```bash
# デフォルトはオフラインのプラン作成のみ(IBKR接続なし)
python scripts/ibkr_execution_study.py \
  data/reproducibility_ranking.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --output data/execution_study_plan.csv

# TWS/IB GatewayでAPIが有効な場合のみ、What-Ifプレビューを実行
python scripts/ibkr_execution_study.py \
  data/reproducibility_ranking.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --run-whatif \
  --exchange OSE.JPN \
  --currency JPY \
  --limit 5 \
  --output data/execution_study_whatif.csv
```

- `PAPER_COMBO_TEST_REQUIRED`: BAGプレビューが受理され、手数料通貨も一致し、
  実見積りに置き換えても正のエッジが残る場合(原子性は依然`False`のまま)
- `STOP_COMBO_UNSUPPORTED_OR_REJECTED`: IBKRがBAGプレビューを受理しなかった場合
- `STOP_EDGE_AFTER_COMMISSION_NONPOSITIVE`: 実手数料見積りでエッジが消失する場合
- `REVIEW_WHATIF_WARNINGS` / `REVIEW_WHATIF_INCOMPLETE` / `REVIEW_COMMISSION_CURRENCY` /
  `REVIEW_WHATIF_TIMEOUT`: 個別に要確認な状態
- `WHATIF_PREVIEW_REQUIRED`: プラン作成のみでブローカーへの問い合わせ未実施

JPXの有価証券オプションは現時点で「ストラテジー取引: 利用不可」とされており、
IBKRのBAGプレビューが受理されたこと自体はOSEでの約定が原子的であることを何ら
証明しません。`PAPER_COMBO_TEST_REQUIRED` はあくまで次にペーパー口座/TWSでの
手動コンボ約定メカニズム検証に進む価値がある、という研究上の結論に過ぎません。
詳細は [`PHASE6_NOTES.md`](PHASE6_NOTES.md) を参照してください。

### 8. (Phase 7) ペーパー口座でコンボ約定メカニズムを実験する

Phase 6はWhat-If(プレビュー)止まりでした。`ibkr_paper_combo_test.py` は
Phase 7で初めて実際に注文を送信しうる段階ですが、**IBKRのペーパー口座のみ**を
対象に約定メカニズム(受理されるか、`orderStatus`の遷移、`execDetails`が
`BAG`単位か個別`OPT`単位か、部分約定の有無、同一注文IDでの指値変更、残数量の
キャンセル)を観察するための実験です。実弾口座に発注するものでは**ありません**。

**安全機構(実運用への流出を防ぐ多重ガード):**
- デフォルトはオフラインの `PLAN_ONLY`。`--run-paper` を明示しない限りIBKRには接続しません
- `--run-paper` には `--account` と、厳密一致が必要な確認フレーズ
  `--paper-ack I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER` が必須です
- 指定した口座がIBKR自身の`managedAccounts`コールバックに実際に含まれているか、
  かつ口座IDが`DU`で始まるか(IBKRのペーパー口座命名規則)を**発注直前にも
  再チェック**します
- 親注文は `BUY` / `LMT` / 数量ちょうど1パッケージ / `transmit=True` に固定され、
  `orderRef`は`kabuopu-phase7-paper-`で始まる必要があります
- 1回の実行につき候補は最大1件のみ処理します
- キャンセルは自分が発注した注文IDに対する`cancelOrder`のみ(`reqGlobalCancel`は
  一切使用しません)
- 出力には常に `phase7_live_money_allowed=False` と
  `phase7_atomicity_established=False` が記録されます

```bash
# プランのみ(IBKR接続なし)
python scripts/ibkr_paper_combo_test.py \
  data/execution_study_whatif.csv \
  --output data/paper_combo_plan.csv

# ペーパー口座での実験(paper用のTWS/IB Gatewayに接続していること)
python scripts/ibkr_paper_combo_test.py \
  data/execution_study_whatif.csv \
  --run-paper \
  --account DU1234567 \
  --paper-ack I_UNDERSTAND_THIS_SUBMITS_A_SIMULATED_PAPER_ORDER \
  --port 4002 \
  --exchange OSE.JPN \
  --currency JPY \
  --observe-seconds 5 \
  --output data/paper_combo_trace.csv
```

`phase7_trace_class` は `PAPER_FULL_FILL_OBSERVED` / `PAPER_PARTIAL_OR_LEG_EXECUTION_OBSERVED` /
`PAPER_ACCEPTED_THEN_CANCELLED` / `PAPER_ORDER_REJECTED_OR_INACTIVE` /
`PAPER_ORDER_REJECTED_OR_NO_ACK` / `PAPER_ACKNOWLEDGED_NO_TERMINAL` /
`PAPER_NO_ACKNOWLEDGEMENT` のいずれかで、送信後の唯一の判定は
`MANUAL_REVIEW_PAPER_TRACE` です。ペーパーでの完全約定観測(`PAPER_FULL_FILL_OBSERVED`)
であっても、それ自体は自動的な「実弾GOシグナル」では**ありません**。IBKRのペーパー
シミュレーションはコンボ取引の挙動が制限されていることが公式に案内されています。
詳細は [`PHASE7_NOTES.md`](PHASE7_NOTES.md) を参照してください。

### 9. (Phase 8) 複数のペーパートレースをオフライン集計する

Phase 7の1回の結果だけでは、BAG/OPTの報告形態やcallback順序が再現するか判断できません。
`analyze_paper_combo_traces.py` は複数のPhase 7 CSVを読み、同一候補について
`orderStatus` と `execDetails` の整合性、BAG/OPT報告、combo固有エラー、重複callbackを
集計します。**IBKRには接続せず、発注・変更・取消も一切行いません**。

```bash
python scripts/analyze_paper_combo_traces.py \
  'data/paper_combo_trace_*.csv' \
  --min-sessions 3 \
  --min-bag-full-fills 2 \
  --sequence-gap-ms 250 \
  --sessions-output data/phase8_paper_sessions.csv \
  --output data/phase8_paper_candidate_summary.csv \
  --summary-json data/phase8_summary.json
```

主な候補判定は `BROKER_CONFIRMATION_REQUIRED` / `INVESTIGATE_LEG_CALLBACK_SEQUENCE` /
`INVESTIGATE_LEG_REPORTING` / `STOP_COMBO_PATH_PENDING_FIX` / `KEEP_PAPER_OBSERVING` です。
`OPT`のexecDetailsが見えただけでは実際のleggingとは断定せず、親fillより一定時間早い
callbackのみをsequence riskとして分離します。API callbackの受信順序は取引所の約定順序
そのものではないためです。

Phase 8には意図的に`GO_LIVE`判定がありません。出力は常に
`phase8_live_money_allowed=False`、`phase8_atomicity_established=False`です。
繰り返しBAG-only fillが観測されても最大で`BROKER_CONFIRMATION_REQUIRED`に留め、
live実験を設計する前にOSE/IBKRのrouting・guarantee semanticsを別途確認します。
詳細は [`PHASE8_NOTES.md`](PHASE8_NOTES.md) を参照してください。

### 10. (Phase 9) ベニュー/ブローカーの確認をゲートする

Phase 8が繰り返し一貫したペーパーBAG約定を示しても、それだけではOSEでの
ライブ原子的約定は証明されません。`evaluate_broker_confirmation.py` は
Phase 8の候補サマリーと、レビュー済みのベニュー/ブローカー事実を記録した
JSON(`config/phase9_venue_broker_evidence.json`)を突き合わせる、**完全
オフライン**のゲートです。IBKRには一切接続しません。

JPXの有価証券オプション契約仕様は現時点で「ストラテジー取引: 利用不可」と
なっているため、`config/phase9_venue_broker_evidence.example.json` は
venue側を`UNAVAILABLE`、broker側のOSE固有フィールドをすべて`UNKNOWN`で
初期化しています。IBKRサポートへ確認すべき質問は
[`PHASE9_BROKER_QUESTIONS.md`](PHASE9_BROKER_QUESTIONS.md) にまとめてあり、
ケースID・回答は必ずこの証拠JSONに記録した上で更新してください(一般的な
コンボ注文の説明だけではOSE個別株オプション固有の保証にはなりません)。

```bash
cp config/phase9_venue_broker_evidence.example.json \
   config/phase9_venue_broker_evidence.json
# 証拠JSONは権威ある一次情報でのみ更新してください

python scripts/evaluate_broker_confirmation.py \
  data/phase8_paper_candidate_summary.csv \
  --evidence config/phase9_venue_broker_evidence.json \
  --as-of-date 2026-09-14 \
  --output data/phase9_broker_gate.csv \
  --summary-json data/phase9_summary.json
```

venueが`UNAVAILABLE`のままbrokerが直接的な原子約定を主張した場合は
`EVIDENCE_CONFLICT_MANUAL_ESCALATION`として自動判定せず矛盾を手動解決に
回します。venueとbrokerの証拠がすべて`AVAILABLE`/`DIRECT_EXCHANGE`/`ATOMIC`
で整合していても、到達するのは`MANUAL_LIVE_DESIGN_REVIEW_REQUIRED`までで、
`GO_LIVE`は意図的に存在しません。出力は常に
`phase9_live_money_allowed=False`、`phase9_atomicity_established=False`です。
詳細は [`PHASE9_NOTES.md`](PHASE9_NOTES.md) を参照してください。

### 11. (Phase 10) 非原子執行を気配リプレイで検証する

Phase 9は現行のベニュー証拠(「ストラテジー取引: 利用不可」)の下で取引所
ネイティブな原子コンボという仮説をブロックします。Phase 10はそのゲートを
回避しようとするものでは**ありません**。個別レッグ執行を別の非原子的な
研究トラックとして扱い、より狭い問いだけを検証します:

> 同時サンプルで利益が出て見えたパッケージは、レッグを1本ずつ遅延を
> 挟んで約定させた場合でもエッジが残るか?

`ibkr_monitor_findings.py`(Phase 4)が `monitor_samples_json` の各サンプルに
`leg_snapshots`(レッグ別の実行可能価格・サイズ・鮮度・実際のIBKRデータ種別)を
記録するようになりました。`simulate_nonatomic_execution.py` はこれと
Phase 9の候補CSVを読み、レッグ順序の総当たり(`--max-permutations`まで)で
逐次約定パスをオフライン再計算します。**IBKRには一切接続しません**。
過去のPhase 4 CSVにはこのフィールドがないため、再収集していないデータは
`NEEDS_PHASE4_RECOLLECTION`として扱われます。

```bash
# このパッチ適用後にPhase 4データを再収集してから実行
python scripts/simulate_nonatomic_execution.py \
  data/phase9_broker_gate.csv \
  --phase4-inputs 'data/persistence_*.csv' \
  --leg-delay-sec 0.5 \
  --extra-slippage-per-contract-leg 100 \
  --output data/phase10_nonatomic_candidates.csv \
  --sessions-output data/phase10_nonatomic_sessions.csv \
  --paths-output data/phase10_nonatomic_paths.csv \
  --summary-json data/phase10_summary.json
```

これは**気配タッチのリプレイであり、約定シミュレータではありません**。
キュー位置・約定確率・サンプル間の気配取消・隠れ流動性・マーケットインパクト・
1レッグの約定が他レッグの価格に与える影響は一切モデル化していません。正の
結果は「表示されたトップオブブックのエッジが単純な逐次価格ストレスに耐えた」
という証拠に過ぎず、実際に執行可能・利益が出ることの証明ではありません。
`--leg-delay-sec` や `--extra-slippage-per-contract-leg` を変えた感度分析を
必ず行い、遅延ゼロ近辺でしか残らない結果は頑健な非原子エッジとして扱わない
でください。出力は常に `phase10_live_money_allowed=False`、
`phase10_atomicity_established=False`です。詳細は
[`PHASE10_NOTES.md`](PHASE10_NOTES.md) を参照してください。

### 12. (Phase 11) OSEリアルタイム/L2 APIの能力をゲートする

Phase 10はトップオブブックの気配タッチ止まりで、キュー位置・約定確率は
モデル化していません。より深い執行モデルを作る前に、実際のIBKR APIセッションが
そのモデルに必要な市場データを提供できるかを確認する必要があります。IBKRの
現行の料金ページは「Osaka Exchange (L1)」「Osaka Exchange (L2)」のリアルタイム
購読を掲載する一方、過去のTWS APIドキュメントはOSEのAPIデータが遅延限定と
述べていました。この食い違いは推測せず実測すべきものです。

`ibkr_probe_marketdata_capability.py` は自分のTWS/IB Gatewayセッションに対して
**読み取り専用**の能力プローブを実行します。`reqMktData`でL1を購読して権威ある
`marketDataType`コールバックを記録し、`reqMktDepth(..., isSmartDepth=False)`で
直接の板情報を要求し、両サイドかつ正のサイズを伴うcallbackが届くかを記録した後、
市場データの購読のみをキャンセルします。**発注・変更・取消・What-If・口座取引の
ロジックは一切含みません**(ソースに`placeOrder`/`cancelOrder`/`reqGlobalCancel`
が存在しないことをテストでも保証しています)。

```bash
python scripts/ibkr_probe_marketdata_capability.py \
  data/persistence_7203_new.csv \
  --candidate-id '<candidate-id>' \
  --exchange OSE.JPN \
  --duration 8 \
  --depth-rows 5 \
  --output data/phase11_marketdata_probe.csv \
  --summary-json data/phase11_marketdata_probe_summary.json
```

**全レッグ**が`REALTIME_L2_OBSERVED`の場合のみ`READY_FOR_L2_FILL_MODEL`となり、
次に板情報ベースの約定モデルを検討する価値があることを意味します。ただし
これもあくまでデータ能力の確認であり、キュー位置・約定確率・原子的執行・
実際の収益性を証明するものではありません。遅延/frozenなL1や使える板情報が
得られない場合は、IBKR経由の約定モデル化を止め、別のリアルタイムデータ源を
検討してください。出力は常に `phase11_live_money_allowed=False`、
`phase11_atomicity_established=False`です。詳細は
[`PHASE11_NOTES.md`](PHASE11_NOTES.md) を参照してください。

### 13. (Phase 12) 表示気配タッチの持続性を実測する

Phase 11はAPIセッションがL1/L2を受信できるかを確認するだけで、キュー位置・
隠れ流動性・約定確率は一切わかりません。Phase 12はこれらを「約定確率」とは
呼ばず、より狭い観測量である**表示気配タッチの持続性**だけを測定します。
BUYレッグではライブの最良売気配が必要数量以上で表示され続けた時間を、SELL
レッグでは対称的に最良買気配について測定します。Phase 10のレッグ間遅延の
ストレス・キャリブレーション統計として有用ですが、約定確率の推定ではあり
ません。

`ibkr_record_depth_survival.py`(読み取り専用の記録)はPhase 11の
`READY_FOR_L2_FILL_MODEL`判定を要求し、候補の各レッグに**順番に**(同時ではなく)
板情報を購読して直接の板イベントとトップオブブック状態を記録します。IBKRは
レベルIIの同時購読数を通常の気配データより厳しく制限しているため、複数レッグ
候補で同時購読数を仮定しないよう逐次記録としています。発注・変更・取消は
一切行わず、`cancelMktDepth`/`cancelMktData`で購読を止めるのみです。

`analyze_touch_survival.py`(完全オフライン)は記録されたイベントを規則的な
グリッドにフォワードフィルし、実行可能なトリガーを特定して、設定可能な
複数の時間軸(既定100/250/500/1000/2000ms)での持続割合を報告します。

```bash
python scripts/ibkr_record_depth_survival.py \
  data/persistence_7203_new.csv \
  --phase11-summary data/phase11_marketdata_probe_summary.json \
  --candidate-id '<candidate-id>' \
  --duration-per-leg 60 \
  --depth-rows 5 \
  --output data/phase12_depth_events.csv \
  --summary-json data/phase12_recording_summary.json

python scripts/analyze_touch_survival.py \
  data/phase12_depth_events.csv \
  --horizons-ms 100,250,500,1000,2000 \
  --grid-step-ms 50 \
  --target-horizon-ms 500 \
  --min-survival 0.80 \
  --min-complete-triggers 20 \
  --output data/phase12_touch_survival.csv \
  --summary-json data/phase12_touch_survival_summary.json
```

最も強い判定`TOUCH_SURVIVAL_ROBUST_ENOUGH_FOR_QUEUE_RESEARCH`が意味するのは
「表示気配が経験的に安定しておりキュー/約定確率の研究に進む価値がある」だけ
で、実弾取引を許可するものでは**ありません**。500msの持続率が低ければ、
Phase 10で仮定した500msの逐次レッグ遅延は既に楽観的だったことになり、非原子
執行パスは弱い証拠として扱うべきです。1〜2秒でも持続率が高い場合でも、それは
あくまで表示気配の持続性についての証拠であり、新規注文が約定することの証明
ではありません。詳細は [`PHASE12_NOTES.md`](PHASE12_NOTES.md) を参照して
ください。
