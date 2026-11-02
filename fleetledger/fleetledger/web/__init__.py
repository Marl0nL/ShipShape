"""FleetLedger's local web UI (`ship-fleet ui`): the fleet dashboard and the
login concierge in a browser. See docs/web-ui.md.

Local only: it binds 127.0.0.1, admits a browser through a one-time link, and
checks Host, Origin and a CSRF token on every change (security.py).
"""
