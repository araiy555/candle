# candle — kabuステーションAPI ローソク足自動売買システム

ローソク足パターンごとに独立した戦略で、底打ち検出・買い・損切り・トレーリングを行うシステムです。
段階的に開発しており、**現在は第1段階（データ取得と5分足生成）のみ**です。発注機能はまだありません。

| 段階 | 内容 | 状態 |
|---|---|---|
| 1 | データ取得とローソク足生成 | 実装済み（実機での接続確認は未実施） |
| 2 | パターン検出 | 未着手 |
| 3 | 戦略別バックテスト | 未着手 |
| 4 | リアルタイム仮想売買・管理画面 | 未着手 |
| 5 | 注文・決済・トレーリング | 未着手 |
| 6 | 少額運用検証 | 未着手 |

## 必要なもの

- kabuステーションを起動・ログインしたWindows PC（APIはそのPCの `localhost` でのみ動きます）
- kabuステーションの設定でAPIを有効にし、APIパスワードを設定済みであること
- Python 3.11 以上

## セットアップ（Windows / PowerShell）

```powershell
git clone https://github.com/araiy555/candle.git
cd candle
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.toml config.toml
$env:KABU_API_PASSWORD = "APIパスワード"
```

APIパスワードは `config.toml` やリポジトリには書かず、環境変数で渡します。

## 使い方

```powershell
python -m candle check            # 接続確認（トークン発行とランキング取得のみ）
python -m candle collect          # PUSH受信 → 5分足を data/candle.sqlite に保存（Ctrl+Cで終了）
python -m candle bars 7203        # 保存済みの足を表示
python -m candle replay data/raw/20261124.jsonl   # 記録データから足を作り直す
```

検証用環境で試す場合は `config.toml` の `port` を `18081` にします。

## 仕様メモ

- **監視銘柄**：売買代金ランキング上位（既定45銘柄）。API登録上限は50銘柄で、残りは保有銘柄用の予備枠です。
  ランキングは平日7:53頃〜9:00過ぎは空になるため、その間に起動した場合は前回保存した監視銘柄を使います。
- **足の時刻**：東証の立会時間（9:00–11:30、12:30–15:30）のみ。引けちょうどの約定は最後の足に含めます。
- **出来高**：累積出来高の差分で計算します。
- **不完全な足**：足の途中から受信した場合や切断を挟んだ場合は `complete=0` で保存します。
  パターン検出には `complete=1` の足だけを使います。
- **約定のない5分間**は足を作りません。
- **記録**：受信データは必要項目だけ `data/raw/YYYYMMDD.jsonl` に保存します（板全体は保存しません）。

## テスト

```powershell
pip install pytest
python -m pytest
```

kabuステーションを模した擬似サーバーを使うので、実機なしで動きます。
