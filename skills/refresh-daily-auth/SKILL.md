---
name: refresh-daily-auth
description: Refresh the sandbox's GCP credentials using a one-time passphrase the operator gives you. Use when GCP calls fail with expired/invalid credentials and the operator has provided a passphrase (e.g. remotely, so they need not be at the host).
allowed-tools: [Bash]
---

# refresh-daily-auth

The sandbox's GCP credentials are short-lived and rotate on demand. If they have
expired and the operator has handed you a **one-time passphrase**, trigger a
refresh:

```bash
refresh-daily-auth <passphrase>
```

The passphrase is validated on the host; if valid, a fresh key is minted and
injected, and the response is `"status":"ok"`. Otherwise you get
`"status":"denied"` with a reason (wrong / expired / already used) — ask the
operator for a new passphrase; you cannot refresh without one.

The passphrase is single-use: it authorises exactly one refresh of a fixed,
operator-defined credential. You cannot choose scope or renew on your own.
