---
name: install-adb
description: Install Android platform-tools (adb) into this container at runtime, for on-device Android debugging via the host adb relay. Use when a task needs adb and it is not already on PATH.
allowed-tools: [Bash]
---

# install-adb

adb is not baked into the image. Install platform-tools into your home on demand:

```bash
install-adb
export PATH="$HOME/android/platform-tools:$PATH"
```

adb here does **not** talk to USB/Wi-Fi devices directly — the sandbox has no LAN
access. It connects to an adb *server running on the host* through a toggle-able
relay. Once installed:

```bash
export ADB_SERVER_SOCKET=tcp:adb-relay:5037
adb devices          # shows devices the host has connected (USB or `adb connect`)
```

The operator must start the adb relay on the host (`docker compose --profile adb
up -d adb-relay`) and connect the device there. Requires `dl.google.com` on the
allow-list. Hot reload / the Dart VM Service needs extra port bridging — see
DESIGN.md.
