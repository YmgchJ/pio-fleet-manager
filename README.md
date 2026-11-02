# PIO Fleet Manager

研究室のロボット群をブラウザから管理する、FastAPI製のローカルダッシュボードです。
機体の検出、状態監視、PlatformIOによるUSB／OTA書き込み、実験シーケンスの実行、ログ回収をまとめて操作できます。

## できること

| 画面・機能 | 内容 |
| --- | --- |
| Fleet | UDPでの機体検出、電圧・動作状態の確認、台帳管理 |
| 書き込み | USB初期書き込み、一括OTA、ビルド出力の表示 |
| Monitor | パラメータ確認、START／STOP、テレメトリ表示 |
| 実験 | 条件の切り替え、自動シーケンス、進行状況の確認 |
| 校正 | 事前校正ログの選択、補正値と出典の記録 |
| ログ | 機体別ログの回収・結合、セッション情報の保存 |
| Records | 機体ごとのメンテナンスメモ |
| 表示 | ライト／ダーク／端末設定に合わせたテーマ |

実機で主に使用しているのはRaspberry Pi Pico Wと対応するModular RobotsのFWです。
任意のESP32／Picoプロジェクトを無変更で操作できるわけではありません。

## 最初に：単体利用と研究用機能

**このリポジトリだけでもサーバーと画面を起動できます。**
台帳を用意すればFleet機能を使えます。書き込みには別途PlatformIOプロジェクトが必要です。

**曲げ・電流・2センサの実験パネルは、Modular Robots側の設定・FW・同定結果に依存します。**
このリポジトリには、それらの実験データやモデルを同梱していません。
画面が表示されることと、実験の実行準備が整っていることは別です。

## セットアップ

Python 3.10以降を使用してください。実機への書き込みを行う場合はPlatformIO CLIも必要です。

```bash
git clone https://github.com/YmgchJ/pio-fleet-manager.git
cd pio-fleet-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windowsでは仮想環境を `.venv\Scripts\activate` で有効化します。

### 画面の起動

```bash
uvicorn server:app --host 127.0.0.1 --port 8000
```

ブラウザで **http://127.0.0.1:8000** を開きます。
Chart.jsはCDNから読み込むため、グラフ表示にはインターネット接続が必要です。

HTTPサーバーに認証機能はありません。研究室の信頼できる端末で使用してください。
`FM_PREVIEW_ONLY=1` はHTTP更新の抑止用であり、UDP送受信やWebSocket操作を隔離するモードではありません。
実機接続中のデモ用途には使わないでください。

### 自分の台帳を使う

```bash
mkdir -p local
cp examples/agents.example.yaml local/agents.yaml
export PIO_AGENTS_PATH="$PWD/local/agents.yaml"
```

`local/agents.yaml` を自分のID・UID・IPアドレスに変更してから、サーバーを再起動します。
起動時点で台帳がなくても画面は開けますが、機体一覧は空です。
UIDは各機体の値を設定してください。台帳のIPは検出時に更新されます。

### 実機へのUSB／OTA書き込み

```bash
python -m pip install platformio
export PIO_FIRMWARE_ROOT_DIR="/absolute/path/to/firmware"
```

`firmware/` の下に `platformio.ini` を持つプロジェクトを配置します。
対応するPlatformIO環境名・OTA方式・FWの通信形式が必要です。
Wi-Fi情報は画面上部の設定から入力し、各端末に保存します。

## Modular Robotsで研究用実験を行う

親リポジトリから、submoduleを含めて取得する構成を推奨します。
親リポジトリの閲覧権限が別途必要です。

```bash
git clone --recurse-submodules https://github.com/YmgchJ/Modular-Robots.git
cd Modular-Robots/fleet_manager
```

前述の仮想環境・依存関係を設定して起動します。親リポジトリの既定パスは次の通りです。

```text
Modular-Robots/
  fleet/              # 台帳、実験プリセット、同期パラメータ、Zプロファイル
  firmware/pico_robot/ # 対応FW
  analysis/python/    # 校正・解析処理
  reports/            # 同定係数・設計結果
  fleet_manager/      # このリポジトリ
```

親リポジトリの取得だけで全実験の再現が保証されるわけではありません。
電流補正には参照係数、2センサ実験には設計成果物・対応build ID・開始位相の参照記録が必要です。
必要な成果物を担当者から受け取り、各実験のREADMEと開始前検証に従ってください。

### 現在の実験機能

- 曲げセンサの位相ロック比較、7点＋追加6点のスイープ。
- 電流の `Z_opt,1`／`Z_opt,10` 比較、事前中心補正、2周期平均、収束比較。
- 曲げ・電流の2センサ併用、事前校正、位相外乱からの復帰比較。
- 選択条件と実機パラメータの照合、校正出典と実験条件のスナップショット保存。

プリセット選択は親の `fleet/current_phase_sequence.yaml` と
`fleet/sync_experiment_params.yaml` を変更します。バックアップは `data/` 内に保存されます。
実行前にパドル・対象ID・FW・処理経路・校正値を確認してください。

電流の現行比較では、因果MA5と事前に決めた固定中心・固定幅を使います。
移動P10/P90正規化は使用しません。曲げと電流の正規化方式を混同しないでください。
位相差と周波数差は `Δφ = φ₂ − φ₁`、`Δω = ω₂ − ω₁` です（通常ID12／ID13）。

## 保存先

| 保存先 | 内容 |
| --- | --- |
| `data/experiments/` | 実験CSV、条件・イベント・回収情報 |
| `data/saved_chunks/`, `data/merged_chunks/` | 回収したチャンクと結合結果 |
| `data/diagnostics/` | 診断データ |
| `data/*backups/` | 条件選択前の設定バックアップ |
| `data/fm_events.log` | 実行イベント |
| `data/records.csv` | 機体メモ |
| `data/wifi_config.json` | 端末のWi-Fi設定 |

`data/`、`local/`、キャッシュ、仮想環境はGit管理対象外です。
既存履歴に含まれていた機体メモとPythonキャッシュは、今回の共有版から追跡を外しています。

## 通信と設定

PCと機体を同じネットワークに接続し、ファイアウォールでUDP受信を許可します。

- PCのUDP受信：`5000`。同じポートを使う別サーバーを同時起動しないでください。
- 機体へのコマンド：UDP `5000 + agent_id`。
- HTTP／WebSocket：起動時に指定したポート（例：`8000`）。
- STATUS最小例：`STATUS,id:4,volt:4.12`。実験には追加の状態・イベント・ログ通信が必要です。

| 環境変数 | 用途 |
| --- | --- |
| `PIO_AGENTS_PATH` | 台帳YAMLのパス |
| `PIO_FIRMWARE_ROOT_DIR` | PlatformIOプロジェクト群の親ディレクトリ |
| `PIO_FIRMWARE_DIR` | 既定FWディレクトリ |
| `PIO_POST_REG_SCRIPT` | 登録後に実行するスクリプト（任意） |
| `SEQUENCE_CONFIG_PATH` | シーケンスYAMLのパス |

研究用の全パスが環境変数で差し替えられるわけではありません。
`SEQUENCE_CONFIG_PATH` の指定だけでは、プリセットや校正モデルの参照先は変わりません。

## 検証

実機へコマンドを送らない、単体リポジトリ用の検証：

```bash
python -m unittest -v test_feedback_diagnostic test_log_receiver_isolation test_precalibrated_two_sensor_telemetry
```

研究用の `test_*plan.py` などは、親のプリセット・同定結果が必要です。
`test_ws.py`／`test_ws_client.py` は実際に書き込みを行う手動スクリプト、
`test_udp.py` はUDP受信待機用です。**無条件に `unittest discover` を実行しないでください。**

機体が表示されない場合は、台帳パス・同一ネットワーク・UDP 5000番の競合を確認します。
校正候補が出ない場合は、対象機体すべての回収完了と校正条件の一致を確認します。
片方だけの受信やレコード数の不一致を、両機の回収完了とは扱いません。
