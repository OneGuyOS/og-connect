OG Connect
OG Connect is a Linux desktop companion for an Android phone. It connects over USB ADB — no app to install on the phone, no Wi-Fi required, no account.

It allows you to browse and transfer files, view images, read and send SMS, control media playback, view notifications, and mirror your phone's screen to your desktop.

Features
Files: Two-pane file manager with large file support (tested up to 7+ GB).

Transfers: Multi-file queue with progress, cancellation, and safe resume.

Screen Mirroring: Launches scrcpy. Full mouse/keyboard control or view-only mode.

SMS: Read, compose, and send messages over ADB.

Media: Play/pause/skip controls and current track info.

Notifications: Read active notifications.

Home & Settings: Live device info, battery status, and preferences.

Supported Devices
Tested on Samsung Galaxy S-series (SM-S731U1, Android 17).

Requirements
Linux (X11 recommended)

Python 3.10+

adb (Android platform-tools)

scrcpy

PySide6

Setup
Clone the repo:
git clone https://github.com/OneGuyOS/og-connect.git
cd og-connect
pip install PySide6

Enable USB debugging on your phone.

Connect via USB and tap "Allow".

Run:
python -m phonebridge

License
This project is licensed under the GPL-3.0 License.

Author
Created by OneGuy.
