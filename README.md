# utone-ndi-utils
NDIを活用することでDJイベント "The Utopia Tone" の映像伝送をIPネットワーク上に移行します。  
PythonとSDL2を使用してNDIソースを受信し全画面表示を行ったり、OpenCVを使用してNDIソースの送信を行います。また、FastAPIによるWeb APIおよびブラウザUIからの遠隔操作に対応しています。

## 使い方
動作にはPython3及びavahi-daemon、libgl1-mesa-dev、portaudio19-devが必要です。
以下のコマンドで必要なパッケージをインストールしてください。
```bash
# Debian/Ubuntu
$ sudo apt install git curl avahi-daemon libgl1-mesa-dev portaudio19-dev
# Fedora
$ sudo dnf install git curl avahi mesa-libGL-devel portaudio-devel
$ curl -LsSf https://astral.sh/uv/install.sh | sh
```

次に、リポジトリをクローンし、仮想環境を作成して依存関係をインストールします。
```bash
$ git clone https://github.com/TechnoTUT/utone-ndi-utils.git
$ cd utone-ndi-utils
$ uv venv
$ uv pip install -r requirements.txt
```

---

## 起動コマンド

統合CLI `main.py` から `rx` / `tx` / `web` の各機能を統一的に実行できます。

### 1. Web API / ブラウザUI
フロントエンドをビルドすることで、ブラウザからNDIソース探索、受信（RX）切り替え、送信（TX）開始・停止を直感的に操作できます。

#### UIのビルド
```bash
$ cd frontend
$ npm install
$ npm run build
$ cd ..
```

#### サーバー起動
```bash
$ uv run main.py web --host 0.0.0.0 --port 8000
```
- ブラウザ操作画面: `http://localhost:8000/`
- APIドキュメント（Swagger UI）: `http://localhost:8000/docs`

> [!TIP]
> **SSH やリモート接続から起動する場合の環境変数**  
> SSH 経由で起動したサーバーから実機のディスプレイに RX ウィンドウを表示する場合、ディスプレイサーバー（Wayland / X11）へのアクセス環境変数が必要です。
>
> 1. **セッション種別の確認方法**:
>    ```bash
>    $ loginctl show-session $(loginctl | grep $(whoami) | awk '{print $1}') -p Type
>    ```
> 2. **Wayland 環境の場合** (`Type=wayland`):
>    ```bash
>    $ WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) uv run main.py web --host 0.0.0.0 --port 8000
>    ```
> 3. **X11 環境の場合** (`Type=x11`):
>    ```bash
>    $ DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority uv run main.py web --host 0.0.0.0 --port 8000
>    ```

### 2. NDIソースの受信・全画面表示 (RX)
```bash
$ uv run main.py rx -s "<NDI Source Name>" --fullscreen
```
`<NDI Source Name>`を省略した場合は、ネットワーク上のNDIソースを自動検索し、対話式メニューから選択して起動できます。
*(※SSH 経由で直接 `rx` を起動する場合も、上記と同様に `WAYLAND_DISPLAY` または `DISPLAY` 環境変数が必要です)*

### 3. NDIソースの送信 (TX)
```bash
$ uv run main.py tx
```
接続されているカメラ・マイクデバイスの確認:
```bash
$ uv run main.py tx --list-devices
```

---

## ディレクトリ構成
```
utone-ndi-utils/
├── core/                   # 低レベル共通コアロジック
│   ├── rx.py               # SDL2初期化、OpenGL描画、フレーム同期、NDI受信定義
│   └── tx.py               # カメラ取得スレッド、映像/音声NDI送信スレッド
├── cli/                    # コマンドラインUI定義
│   ├── menu.py             # 対話型NDIソース選択メニュー
│   ├── rx_cmd.py           # rx コマンド定義
│   └── tx_cmd.py           # tx コマンド定義
├── backend/                # FastAPI Web API & プロセス制御
│   ├── models.py           # Pydantic スキーマ
│   ├── ndi_scanner.py      # NDIソース自動探索サービス
│   ├── devices.py          # カメラ・オーディオデバイス検出
│   ├── rx_runner.py        # RXプロセスコントローラー
│   ├── tx_runner.py        # TXプロセスコントローラー
│   └── main.py             # REST APIエンドポイント
├── main.py                 # 統合CLIエントリーポイント (rx / tx / web)
├── requirements.txt
└── systemd-example/        # systemd用ユニット設定例
```

---

## 自動起動設定 (systemd)
Systemdを使用して自動起動する場合は、以下の手順を実行します。GUIなしの環境でも動作します。  
`ExecStart` と `WorkingDirectory` のパスを環境に合わせて設定してください。
```bash
$ mkdir -p ~/.config/systemd/user
$ cp systemd-example/ndi-rx.service ~/.config/systemd/user/
$ vim ~/.config/systemd/user/ndi-rx.service
```

自動起動を有効にします。
```bash
$ systemctl --user daemon-reload
$ systemctl --user enable --now ndi-rx.service
```

システム起動時にログインなしで自動起動する場合は、lingerを有効にします。
```bash
$ sudo loginctl enable-linger username
```