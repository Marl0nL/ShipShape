---
name: request-domain
description: Request that the operator allow-list an egress domain the sandbox is blocked from reaching. Use when a network call fails because the domain is not on the egress allow-list — submit the domain (and why) for one-click operator approval.
allowed-tools: [Bash]
---

# request-domain

This sandbox blocks all outbound network access except an operator-managed
allow-list (via the egress proxy). If a request fails because a domain is not
allowed, ask for it:

```bash
request-domain <domain> [reason]
# e.g.
request-domain files.pythonhosted.org "pip install needs the PyPI CDN"
```

The command returns JSON:
- `"status":"ok"` — already allowed; retry your request now.
- `"status":"pending"` — queued for the operator to approve out-of-band. After
  they approve it, retry your original request. You can re-check with the `id`:
  `curl -fsS "$SHIPSHAPE_BROKER/status/<id>"`.

Do not try to bypass the proxy; there is no other route out.
