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

The command **blocks until the operator approves or declines**, then returns:
- `APPROVED` (exit 0) — the domain is now allow-listed; retry your original request.
- `DECLINED` (exit 3) — refused; don't retry, choose another approach.
- `STILL PENDING` (exit 2) — no decision within ~2 min; keep waiting by re-running
  `await-request <id>` (the id is printed above) — it returns the instant they decide.

Run it with a generous Bash timeout (e.g. 10 minutes) so it can wait for the decision.
Don't try to bypass the proxy — there is no other route out.
