OG Connect — User Guide
Version: 0.1
Tested on: Samsung Galaxy S-series (SM-S731U1), Android 17
Tested by: OneGuy
License: GPL-3.0

1. What this is
OG Connect is a Linux desktop companion for an Android phone. It connects over USB ADB — no app to install on the phone, no Wi-Fi, no account.

It lets you browse and transfer files, view images, read SMS, control media playback, read notifications, and check battery and connection state.

Supported: Samsung Galaxy S-series (verified on SM-S731U1, Android 17). Other phones may work but are untested.

2. Setup
On Linux, you need:

Python 3.10+

PySide6

adb (Android platform-tools)

scrcpy (optional, for future screen mirroring)

On the phone:

Enable Developer Options → USB debugging

Connect with a USB cable

Tap "Allow" on the "Allow USB debugging?" prompt

When connected, the header shows Connected over USB: SM S731U1 — battery NN%.

3. Features
Files
Two panes: the left is your Linux home directory, the right is the phone's /storage/emulated/0.

Double-click to enter a folder, use the button to go up, refresh, sort by name/size/date.

The left pane updates automatically when files change on disk.

Right-click to "Copy to Phone" or "Copy to Computer". Actions that can't run are disabled with a reason.

Transfers
Multi-file transfers appear in a visible queue showing the current file, per-file progress, overall progress, and completed/remaining counts.

Cancel the current file, or the whole queue.

Switching pages does not interrupt transfers.

If you unplug mid-transfer, the current file fails with a clear error, the .phonebridge-part file is kept, and no corrupt final file is left behind. Reconnect and the queue still works.

Image Viewer
Right-click "Open image" from either pane. Fit-to-window, zoom, rotate, previous/next. Clear states for loading, unsupported format, corrupt file, and phone disconnected.

Home
Shows connection state, phone model, Android version, ADB serial, battery percentage, and charging state. Battery refreshes every 30 seconds (adjustable in Settings).

Settings
Shows ADB version, whether scrcpy is installed, phone info, and the support line.
Adjustable: show Home on startup, what to do when a destination file already exists (Ask / Skip / Overwrite / Rename), battery refresh interval.
"Copy diagnostics" copies device info to the clipboard.

Media
Shows the current track, artist, album, and playback state, with Play, Pause, Previous, and Next buttons.

Buttons are enabled or disabled based on what the phone's current media session says it supports. Unsupported actions are greyed out with a reason.

Verified with Amazon Music: Play, Pause, and Next work. Previous may restart the current track instead of going back, depending on how far into the song you are — that's the player's behavior, not a bug.

SMS
Reads SMS from the phone (verified on SM-S731U1, returning 220 real messages). Conversation view, compose, reply.

Sending uses service call isms. A test message to a Google Voice number was sent and confirmed delivered.

Known limitation: messages sent through OG Connect do not appear in the phone's own Messages app sent history, because the ADB telephony path doesn't write to content://sms/sent. If you need phone-side history, send from the phone's messaging app instead.

Notifications
Reads active notifications: app name, title, text snippet, timestamp.

Known limitation: on your Android 17 device, cmd notification exposes listing only. Dismiss and inline reply are not supported from OG Connect. Both buttons are disabled with an explanation.

4. Known limitations
Samsung Galaxy S-series only (verified on SM-S731U1 / Android 17). Other brands may work; they're untested.

USB ADB only. No Wi-Fi, Bluetooth, or MTP.

"Previous" depends on the player. Amazon Music restarts the current track when you're deep in a song; Spotify Free has hourly skip limits. OG Connect sends the command and reflects what the player advertises.

SMS sends don't appear in the phone's sent history.

Notifications are read-only on your device — no dismiss, no reply.

Screen mirroring / control is not implemented. scrcpy detection is in place; launching scrcpy is left for a later phase.

5. Troubleshooting
"No phone connected"
Check the USB cable. Confirm you tapped "Allow" on the USB debugging prompt. Unplug and replug, then wait 3 seconds.

"Phone unauthorized"
There should be an "Allow USB debugging?" prompt on the phone — tap Allow. If it doesn't appear, go to Developer Options → Revoke USB debugging authorizations, then replug.

A transfer failed mid-way
Check the error in the Transfers tab. If the phone disconnected, the queue is still usable after reconnecting. The .phonebridge-part file is kept; delete it manually if you don't need it.

An image won't open
"Unsupported format" or "corrupt file" means the image's encoding can't be decoded in the current Qt environment. "Phone disconnected" means the USB connection dropped.

6. Source and license
OG Connect is released under GPL-3.0. You may use, study, modify, and redistribute it. If you distribute a modified version, you must publish the source under the same license.

---

## 7. Screen Mirroring (Update)

OG Connect now supports screen mirroring via `scrcpy`.
- Go to the **Screen** tab and click **Launch Screen**.
- The phone screen will open in a new window next to OG Connect.
- **Input control:** By default, you can use your mouse and keyboard to control the phone. Uncheck "Enable input control" to make it view-only.
- **Cleanup:** The mirror will close automatically if you close the window, click "Close Screen", disconnect the phone, or close OG Connect.

### Samsung DeX (Virtual Display)
If you have a Samsung phone, you can check the **Launch in DeX Mode** box in the Screen tab. This launches a virtual 1080p desktop environment instead of mirroring the phone screen. It includes full mouse and keyboard support, and is configured to pass media audio correctly.
