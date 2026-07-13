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

The passphrase is validated on the host and resolves immediately:
- `APPROVED` (exit 0) — a fresh key was minted and injected; GCP calls work again.
- `DECLINED` (exit 3) — the passphrase was wrong, expired, or already used; ask the
  operator for a new one. You cannot refresh without a valid passphrase.

The passphrase is single-use: it authorises exactly one refresh of a fixed,
operator-defined credential. You cannot choose scope or renew on your own.
