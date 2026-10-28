# OG Connect

OG Connect is a Linux desktop companion for an Android phone. It connects over **USB ADB** — no app to install on the phone, no Wi-Fi required, no account.

## Features
- **Samsung DeX Mode:** Launch a virtual 1080p DeX desktop environment inside a window on your Linux machine (replaces the discontinued DeX for PC app).
- **Files:** Two-pane file manager with large file support (tested up to 7+ GB).
- **Transfers:** Multi-file queue with progress, cancellation, and safe resume.
- **Screen Mirroring:** Launches `scrcpy`. Full mouse/keyboard control or view-only mode.
- **SMS:** Read, compose, and send messages over ADB.
- **Media:** Play/pause/skip controls and current track info (with audio routing fixes).
- **Notifications:** Read active notifications.
- **Home & Settings:** Live device info, battery status, and preferences.

## Supported Devices
Tested on **Samsung Galaxy S-series** (S25 FE running Android 17).

## Requirements
- **Linux** (X11 recommended)
- Python 3.10+, `adb`, `scrcpy`, PySide6

## Setup
1. Clone the repo:
   `git clone https://github.com/OneGuyOS/og-connect.git`
2. Enable USB debugging on your phone.
3. Connect via USB and tap "Allow".
4. Run:
   `python -m phonebridge`

## License
This project is licensed under the GPL-3.0 License.

## Author
Created by [OneGuy](https://github.com/OneGuyOS).
