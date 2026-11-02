# PIO Fleet Manager

FastAPIとPlatformIOを使った、ロボットの状態監視・USB／OTA書き込み・実験ログ回収用のWeb UIです。

## 起動

```bash
git clone https://github.com/YmgchJ/pio-fleet-manager.git
cd pio-fleet-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8000
```

ブラウザで http://127.0.0.1:8000 を開きます。
書き込みには、PlatformIO CLIと対応するファームウェアが別途必要です。

## 設定

- `PIO_AGENTS_PATH`：機体台帳のYAML。例は [`examples/agents.example.yaml`](examples/agents.example.yaml)。
- `PIO_FIRMWARE_ROOT_DIR`：PlatformIOプロジェクトを置くディレクトリ。
- `data/`：ログ・機体メモ・Wi-Fi設定の保存先。Git管理対象外。

研究用の実験機能は、親のModular Robotsリポジトリにある設定・FW・同定結果を参照します。
これらは、このリポジトリには含まれていません。
