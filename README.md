# PIO Fleet Manager

PlatformIO を利用した組み込みプロジェクト（ESP32, Pico W など）向けの汎用的な一括管理・OTA書き込みダッシュボードです。

## 特徴
- ネットワーク上のデバイスをUDPブロードキャストで自動検出
- 未登録デバイスへのUSB初期書き込みとID割り当て
- ブラウザ上からワンクリックで全機一括OTAアップデート（順番処理）
- リアルタイムでの状態モニタリング
- 一斉制御（START / STOP）コマンドのUDPブロードキャスト機能
- ロボットごとのメンテナンス記録（Records）の保存・編集・削除機能
- 最新のメモ一覧表示機能
- 大パドル・電流フィードバックの6条件自動位相差スイープ

## セットアップ手順

1. **依存関係のインストール**
   ```bash
   pip install -r requirements.txt
   ```

2. **環境変数（任意）**
   通常は不要です。台帳はリポジトリの `fleet/agents.yaml` を自動参照します。
   別パスを使う場合のみ:

   ```bash
   export PIO_AGENTS_PATH="/path/to/agents.yaml"
   ```

3. **サーバーの起動**
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```
   起動後、ブラウザで `http://localhost:8000` にアクセスしてください。

## ロボット側（ファームウェア）の要件

ダッシュボードからデバイスを自動検出させるには、マイコン側で以下のフォーマットのUDPメッセージをブロードキャスト（またはサーバーのIP宛に送信）し続ける必要があります。

**送信先ポート:** `5000`
**メッセージフォーマット:**
```text
STATUS,id:<ID>,volt:<Voltage>
```
（例: `STATUS,id:4,volt:4.12`）

その他の任意のキーも `key:value` 形式でカンマ区切りで追加することで、ダッシュボードの監視画面にリアルタイムで表示させることができます。

## 中間発表用の自動実験

`fleet/current_phase_sequence.yaml` が、ID 8 と ID 11 の大パドル実験を定義します。
開始前に `pico_robot` の current-only ファームウェアを両機へUSBまたはOTAで書き込み、
STATUS に `mode:current_only`, `flex_used:0`, `src:1`, `params:1` が表示されることを確認してください。

モニタ画面の「自動シーケンス開始」を押すと、両機を中央姿勢にしてから20 s後の開始を
ロボット側へ予約します。予約中はサーボを動かさず、記録もしません。その後、次の順で各60 sを記録します。

```text
trial 1: Δφ = 0
trial 2: Δφ = π/3
trial 3: Δφ = 2π/3
trial 4: Δφ = π
trial 5: Δφ = 4π/3
trial 6: Δφ = 5π/3
```

各trialは 0–10 sを `κ=0`、10–60 sを `current feedback, κ=5` とします。6条件すべてのデータはロボットRAMに保持され、
**水上で「STOP ALL」を押したときに一括アップロード**されます（従来の手動実験と同じ方式）。
trial間ではアップロードせず、次の条件へ自動的に進みます。安全停止を押した場合は、その時点で
シーケンスを終了し、次条件へは進みません。

条件、実際に送られたパラメータ、ARM/START/FEEDBACK_ON/STOP/UPLOAD/UPLOAD_DONE/LOG_END、
INA226の読み取り成功・失敗数は、最終アップロード後の `data/experiments/<session_id>/session_meta.json`
に保存されます（6条件分のイベントが1セッションにまとまります）。
シーケンス全体の計画と状態は `data/experiments/sequence_<sequence_id>/` に保存されます。
