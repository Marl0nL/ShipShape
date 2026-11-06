# Runbook: the web UI from your laptop and phone (Tailscale Serve)

Open the FleetLedger running on one computer (today: your desktop) from your other
devices, at `https://fleetledger.example-tailnet.ts.net`, the computer's own name on
your tailnet.

FleetLedger still listens on `127.0.0.1` only. **Tailscale Serve**, on the same
computer, gives that name HTTPS (a certificate Tailscale gets for it) and forwards each
request to the loopback port. Only devices on your tailnet can reach it, and only as
far as your access rules allow. Serve's public variant, **Funnel**, is never used:
FleetLedger refuses to start if it is on for this name, and refuses any request that
did not come from a person's device.

It is off until you turn it on. With it off, `ship-fleet ui` is exactly what it always
was: this computer only.

## Before you turn it on: the risks

Read these first. Doing this on your desktop is a real change to what can reach your
fleet.

- **A session there is the whole fleet.** Whoever holds a signed-in browser session on
  the remote address can do anything the UI does: create, roll back and destroy Ships,
  trust machines, and use FleetLedger's admin key, both CA keys and the Proxmox token.
  A trusted machine's certificate, by contrast, opens only `agent` (and your owner
  login) on the Ships in its scope, for 7 days. A stolen unlocked laptop, a compromised
  phone, or a compromised Tailscale account plus a sign-in link would all be enough.
  The destructive actions keep their typed confirmations, but treat every device you
  sign in from as holding the fleet.
- **It makes the desktop a control plane on the network.** Until now nothing outside
  the desktop could reach FleetLedger at all.
- **The desktop must be on, awake and logged in**, with `ship-fleet ui` running (the
  keyring that holds FleetLedger's passphrases is open only while you are logged in).
  When it sleeps, the laptop loses FleetLedger. That is why a small always-on VM is
  the end state; this works before it exists.
- **Programs on the desktop can forge Tailscale's identity headers**, since they can
  call `127.0.0.1` directly. So the identity Serve passes on is never the only check:
  every browser still needs FleetLedger's own one-time sign-in link.
- **The certificate makes the name public.** Tailscale's HTTPS certificates are logged
  in the public Certificate Transparency logs, so the computer's name and your
  tailnet's name become public. Pick a bland machine name before you start (below).
- **Never Funnel.** Funnel would put FleetLedger on the public internet.

## 1. The tailnet (once)

In the Tailscale admin console:

1. **DNS:** MagicDNS on, and **HTTPS certificates** enabled.
2. **The machine's name.** Give the desktop a bland Tailscale name (Machines ▸ the
   desktop ▸ Edit machine name, or `tailscale set --hostname=fleetledger`). Its full
   name is then `fleetledger.<your tailnet>.ts.net`; `tailscale status --json` shows
   it as `Self.DNSName` (without the final dot).
3. **Access rules.** The policy `ship-fleet overlay policy` prints lets your devices
   reach Ships on port 22 and nothing else, so your laptop cannot reach the desktop
   yet. Add one rule to `acls`: your own devices may reach each other on 443.

   ```json
   {"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self:443"]}
   ```

   `autogroup:self` is the devices signed in as the same person as the source, so
   Ships (tagged devices) are never a destination of it, and the rule gives Ships no
   source: `ship-fleet overlay check` stays clean. To keep a later edit from opening
   it to Ships, also add a `hosts` alias for the desktop's tailnet address and a test:

   ```json
   "hosts": {"fleetledger-ui": "<the desktop's address, from tailscale ip -4>"},
   "tests": [{"src": "tag:ship", "deny": ["fleetledger-ui:443"]}]
   ```

## 2. Serve it (once; it survives restarts)

On the desktop, pick a fixed port for the UI (8765 here) and serve it:

```sh
tailscale serve --bg 127.0.0.1:8765
tailscale serve status        # https://fleetledger.<tailnet>.ts.net (tailnet only) -> 127.0.0.1:8765
```

On Linux, changing Serve needs root or Tailscale's operator: run
`sudo tailscale set --operator=$USER` once, or put `sudo` in front. Serve only that:
FleetLedger must have the name to itself, because anything else served at the same
name is the same site to your browser.

## 3. Turn it on in FleetLedger

```sh
ship-fleet ui remote https://fleetledger.example-tailnet.ts.net --port 8765
ship-fleet ui remote          # what is set, and the check again
```

Before saving, FleetLedger checks, through the local `tailscale` command (read only):

- Tailscale is running here;
- the name is this computer's own Tailscale name, with an HTTPS certificate on offer;
- Serve sends exactly that name, and nothing else at it, to `127.0.0.1:8765`;
- Funnel is off for it.

The setting is kept in `~/.config/fleetledger/remote-ui.json` (0600). It is a fact
about this computer, like `this-computer.json`, not part of the fleet.

Then run the UI as usual, in a terminal that stays open (a `tmux` session, say):

```sh
ship-fleet ui --no-browser
```

It now uses port 8765, runs the same check at every start, and refuses to start if
anything is off, saying what and how to fix it. `ship-fleet ui --local` starts it for
this computer only, without the check.

## 4. Sign in from the laptop or phone

`ship-fleet ui` prints the sign-in link twice: once for this computer
(`http://127.0.0.1:8765/login?...`) and once for your other devices
(`https://fleetledger.example-tailnet.ts.net/login?...`). It is one link: it works
once, for 15 minutes, on whichever address opens it first. Press Enter in that
terminal for a new one.

On the remote address:

- a request must come through Serve, over HTTPS, from a device signed in to your
  tailnet as a person: Serve adds who you are, and adds nothing for Funnel or for
  tagged devices, which are refused;
- the cookie is `__Host-fleetledger`: `Secure`, `HttpOnly`, `SameSite=Strict`, for this
  name only;
- every change needs the exact `Origin` `https://fleetledger.example-tailnet.ts.net`
  and the session's CSRF token, as on this computer;
- a session made on one address never works on the other;
- the live terminal (the concierge's fallback) uses `wss://` on the same name.

Sessions live in memory: stopping `ship-fleet ui` signs every device out.

## Turning it off

```sh
ship-fleet ui remote --off
tailscale serve --https=443 off
```

A `ship-fleet ui` already running keeps the remote address until you stop it.

## When it refuses

| It says | Do |
|---|---|
| Tailscale is not running here | `tailscale up`, or `ship-fleet ui --local` |
| … is not this computer's Tailscale name | use the name it says (`Self.DNSName` in `tailscale status --json`) |
| Tailscale has no HTTPS certificate for … | turn on HTTPS certificates (admin console ▸ DNS) |
| Tailscale Serve is not serving … | `tailscale serve --bg 127.0.0.1:8765` |
| Funnel is on for … | `tailscale serve reset`, then serve it again on the tailnet only |
| … somewhere besides this UI | give FleetLedger the name to itself: `tailscale serve reset`, then serve it again |
| … not to this UI on 127.0.0.1:8765 | serve the port FleetLedger uses, or `ship-fleet ui remote … --port` the one Serve uses |
| The remote web UI is set up on port … | leave out `--port`, or add `--local` |
| (in the browser) This address works only through Tailscale Serve … | open it from a device signed in to the tailnet as you, not a tagged device |
