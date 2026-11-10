# moip-toolkit

**English** | [日本語](README.ja.md)

By using NDI, this toolkit moves video transport for the DJ event "The Utopia Tone" onto an IP network.  
It receives NDI sources and displays them fullscreen using Python and SDL2, and sends NDI sources using OpenCV. It also supports remote control through a Web API built with FastAPI and a browser UI.

## Getting Started
Python 3, avahi-daemon, libgl1-mesa-dev and portaudio19-dev are required.
Install the required packages with the following commands.
```bash
# Debian/Ubuntu
$ sudo apt install git curl avahi-daemon libgl1-mesa-dev portaudio19-dev
# Fedora
$ sudo dnf install git curl avahi mesa-libGL-devel portaudio-devel
$ curl -LsSf https://astral.sh/uv/install.sh | sh
```

Next, clone the repository, create a virtual environment and install the dependencies.
```bash
$ git clone https://github.com/TechnoTUT/moip-toolkit.git
$ cd moip-toolkit
$ uv venv
$ uv pip install -r requirements.txt
```

---

## Commands

The unified CLI `main.py` runs the `rx` / `tx` / `multiview` / `signage` / `web` features from a single entry point.

### 1. Web API / Browser UI
Once the frontend is built, you can discover NDI sources, switch the receiver (RX), and start/stop the transmitter (TX) from your browser.

#### Building the UI
```bash
$ cd frontend
$ npm install
$ npm run build
$ cd ..
```

#### Starting the server
```bash
$ uv run main.py web --host 0.0.0.0 --port 8000
```
- Browser UI: `http://localhost:8000/`
- API docs (Swagger UI): `http://localhost:8000/docs`

> [!TIP]
> **Environment variables when starting from SSH or a remote connection**  
> To show the RX window on the physical display from a server started over SSH, you need environment variables that give access to the display server (Wayland / X11).
>
> 1. **Check the session type**:
>    ```bash
>    $ loginctl show-session $(loginctl | grep $(whoami) | awk '{print $1}') -p Type
>    ```
> 2. **On Wayland** (`Type=wayland`):
>    ```bash
>    $ WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) uv run main.py web --host 0.0.0.0 --port 8000
>    ```
> 3. **On X11** (`Type=x11`):
>    ```bash
>    $ DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority uv run main.py web --host 0.0.0.0 --port 8000
>    ```

### 2. Receive and display an NDI source fullscreen (RX)
```bash
$ uv run main.py rx -s "<NDI Source Name>" --fullscreen
```
If `<NDI Source Name>` is omitted, the NDI sources on the network are discovered automatically and you can pick one from an interactive menu.

> [!TIP]
> **Low-latency display (direct KMS/DRM output)**  
> On a dedicated RX machine, drawing directly to the display without going through the desktop compositor (Wayland / X11) cuts 1–2 frames of latency.
> ```bash
> $ uv run main.py rx -s "<NDI Source Name>" --video-driver kmsdrm
> $ uv run main.py web --video-driver kmsdrm   # Also applies to RX / multiview launched from the Web UI
> ```
> - No desktop session may be running on that display (console / TTY state). `sudo systemctl set-default multi-user.target` makes the machine boot without a GUI.
> - Add the user running it to the `video` (display) and `input` (ESC key) groups: `sudo usermod -aG video,input $USER`
> - Output is always fullscreen. You do not need to set `WAYLAND_DISPLAY` / `DISPLAY`.

*(Note: when launching `rx` directly over SSH, the `WAYLAND_DISPLAY` or `DISPLAY` environment variable is also required, as above.)*

### 3. Send an NDI source (TX)
```bash
$ uv run main.py tx
```
List the connected camera and microphone devices:
```bash
$ uv run main.py tx --list-devices
```

### 4. Multi-viewer (Multiview)
Monitor multiple NDI sources at once in an auto-arranged grid.
```bash
$ uv run main.py multiview --fullscreen
$ uv run main.py multiview -s CAM1 -s CAM2   # Only the specified sources
```
If `-s` is omitted, all discovered sources are shown.

### 5. Digital Signage
Upload an image from the Web UI and show it fullscreen directly on a connected display.  
You can also specify an image file from the CLI and show it fullscreen:
```bash
$ uv run main.py signage -i /path/to/image.png --fullscreen
```
*(Note: if `-i` is omitted, the most recent image uploaded via the Web UI is shown.)*

#### Display on a CLI environment (TTY)
The `--framebuffer` option draws directly to the Linux framebuffer (`/dev/fb0`) instead of using SDL2/OpenGL. This works without a GUI, such as over SSH or on a console (TTY).
```bash
$ uv run main.py signage -i /path/to/image.png --framebuffer
```
- Use `--fb-device` to change the framebuffer device path (default: `/dev/fb0`).
- Writing to the framebuffer may require root privileges.

---

## Directory Structure
```
moip-toolkit/
├── core/                   # Low-level shared core logic
│   ├── rx.py               # SDL2 init, OpenGL rendering, frame sync, NDI receive definitions
│   ├── reconnecting_receiver.py # NDI receiver with reconnect / stall-recovery handling
│   ├── tx.py               # Camera capture thread, video/audio NDI send threads
│   ├── multiview.py        # Multi-NDI grid display logic
│   └── signage.py          # Image signage rendering via SDL2/OpenGL or the Linux framebuffer
├── cli/                    # Command-line UI definitions
│   ├── menu.py             # Interactive NDI source selection menu
│   ├── rx_cmd.py           # rx command definition
│   ├── tx_cmd.py           # tx command definition
│   ├── multiview_cmd.py    # multiview command definition
│   └── signage_cmd.py      # signage command definition
├── backend/                # FastAPI Web API & process control
│   ├── models.py           # Pydantic schemas
│   ├── ndi_scanner.py      # Automatic NDI source discovery service
│   ├── devices.py          # Camera / audio device detection
│   ├── rx_runner.py        # RX process controller
│   ├── tx_runner.py        # TX process controller
│   ├── multiview_runner.py # Multiview process controller
│   ├── signage_runner.py   # Signage process controller
│   ├── webrtc_manager.py   # Low-latency WebRTC preview manager
│   ├── preview_manager.py  # On-demand MJPEG preview streamer
│   ├── settings.py         # Persistent server settings (JSON)
│   ├── system_monitor.py   # Lightweight system resource monitor
│   └── main.py             # REST API endpoints
├── data/
│   └── signage/            # Storage for uploaded signage images
├── frontend/               # Nuxt 3 / Vue 3 admin web dashboard
├── main.py                 # Unified CLI entry point (rx / tx / multiview / signage / web)
├── requirements.txt
└── systemd-example/        # Example systemd unit files
```

---

## Autostart (systemd)
To start automatically with systemd, follow the steps below. This also works on environments without a GUI.  
Set the `ExecStart` and `WorkingDirectory` paths to match your environment.  
Unit files for TX (`ndi-tx.service`) and the Web UI (`ndi-web.service`) are also provided in `systemd-example/`.
```bash
$ mkdir -p ~/.config/systemd/user
$ cp systemd-example/ndi-rx.service ~/.config/systemd/user/
$ vim ~/.config/systemd/user/ndi-rx.service
```

Enable autostart.
```bash
$ systemctl --user daemon-reload
$ systemctl --user enable --now ndi-rx.service
```

To start automatically at boot without logging in, enable linger.
```bash
$ sudo loginctl enable-linger username
```
