---
name: install-flutter
description: Install the Flutter SDK into this container at runtime. Use when a task needs Flutter/Dart and it is not already on PATH — the base image is intentionally lean and does not bake Flutter in.
allowed-tools: [Bash]
---

# install-flutter

This image does not ship Flutter by default (it stays lean so one container can
serve Python one day and Flutter the next). Install it on demand into your home:

```bash
install-flutter            # stable channel
install-flutter beta       # or a specific channel
```

Then, in your current shell:

```bash
export PATH="$HOME/flutter/bin:$PATH"
flutter doctor
```

Requires these domains on the egress allow-list: `github.com`, `dl.google.com`,
`storage.googleapis.com`, `pub.dev` (the defaults include them). If a download is
blocked, use `request-domain` to ask for the missing one.
