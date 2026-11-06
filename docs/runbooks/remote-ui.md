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

- **A paired device is the whole fleet.** Whoever holds a paired browser on the remote
  address can do anything the UI does: create, roll back and destroy Ships, trust
  machines, and use FleetLedger's admin key, both CA keys and the Proxmox token. A
  trusted machine's certificate, by contrast, opens only `agent` (and your owner login)
  on the Ships in its scope, for 7 days. A stolen unlocked laptop, a compromised phone,
  or a compromised Tailscale account plus a pairing link would all be enough. What
  limits it: only the Tailscale logins you list get in; a device lapses after 12 hours
  unused and 30 days after pairing; destroy, rollback, trusting machines, setup's keys
  and pairing another device need a device paired in the last 4 hours; you can revoke
  any device at once; and the destructive actions keep their typed confirmations. Still,
  treat every device you pair as holding the fleet.
- **It makes the desktop a control plane on the network.** Until now nothing outside
  the desktop could reach FleetLedger at all.
- **The desktop must be on, awake and logged in**, with `ship-fleet ui` running (the
  keyring that holds FleetLedger's passphrases is open only while you are logged in).
  When it sleeps, the laptop loses FleetLedger. That is why a small always-on VM is
  the end state; this works before it exists.
- **Programs on the desktop can forge Tailscale's identity headers**, since they can
  call `127.0.0.1` directly. So the identity Serve passes on is never the only check:
  every browser still pairs once with FleetLedger's own one-time link, and a device's
  session is a random secret only that browser holds.
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

Say which Tailscale logins may use it: yours, as the admin console shows it under
Users (`you@example.com`, or `example-user@github` for a GitHub sign-in). A request
from any other login is refused before FleetLedger's own sign-in, static files
included. Every login on the list can control the whole fleet, so list only yourself.

```sh
ship-fleet ui remote https://fleetledger.example-tailnet.ts.net --port 8765 --login you@example.com
ship-fleet ui remote          # what is set, who may, and the check again
ship-fleet ui remote --login you@example.com --login example-user@github   # replaces the list
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

## 4. Pair the laptop

Each browser pairs once. The pairing link comes from the desktop, so you need either
the desktop's own screen or ssh to it:

```sh
ssh desktop ship-fleet ui link --name laptop
```

Open the printed `https://fleetledger.example-tailnet.ts.net/login?token=...` on the
laptop, signed in to Tailscale as a login you allowed. The link works once, within 15
minutes, on the remote address only, and only while `ship-fleet ui` runs (it can be
made before the UI starts). The laptop is then a **device** called "laptop": it stays
signed in when you close the browser or restart `ship-fleet ui`, until it is unused for
12 hours, 30 days after pairing, or you revoke it.

The link for your other devices that `ship-fleet ui` prints in its terminal works too,
once, within 15 minutes (Enter there prints a new one). A device paired that way is
named after its browser; rename it in Settings ▸ Devices.

## 5. Pair the phone

On the desktop, or on the laptop once it is paired: **Settings ▸ Devices ▸ Pair a
device**, name it "phone", and scan the QR code with the phone's camera (the phone
needs the Tailscale app, signed in as an allowed login). Or from a terminal:

```sh
ssh desktop ship-fleet ui link --name phone --qr
```

The page shows the link once and never again. Pairing another device from the laptop
needs the laptop to have been paired in the last 4 hours (below).

## What a device may do, and for how long

- Everything the UI does, as from the desktop.
- **Destroy, rollback, deleting a snapshot, accepting a new host key, trusting or
  untrusting a machine, changing its Ships, removing an image, setup's keys, CA and
  tokens, and pairing another device** need a device paired in the last **4 hours**, so
  a stolen laptop's week-old session cannot destroy Ships or mint itself a new pairing.
  The UI says so when it refuses; pair that browser again (a new link from
  `ssh desktop ship-fleet ui link`, opened in the same browser, replaces its device).
  The desktop's own browser is never held to this.
- A device works only with the Tailscale login that paired it, and only while that
  login is on the list. Taking a login off (`ship-fleet ui remote --login ...` without
  it) locks its devices out at once, in a running UI too.

On the remote address, besides:

- a request must come through Serve, over HTTPS, from a device signed in to your
  tailnet as a person: Serve adds who you are, and adds nothing for Funnel or for
  tagged devices, which are refused;
- the cookie is `__Host-fleetledger`: `Secure`, `HttpOnly`, `SameSite=Strict`, for this
  name only, kept for 30 days;
- every change needs the exact `Origin` `https://fleetledger.example-tailnet.ts.net`
  and the device's CSRF token, as on this computer;
- a session made on one address never works on the other, and a request through Serve
  that names `127.0.0.1` or `localhost` as its `Host` is refused (Serve keeps the
  browser's `Host`, so this is how FleetLedger tells it from a browser on this computer);
- the live terminal (the concierge's fallback) uses `wss://` on the same name.

FleetLedger keeps the devices in `~/.local/share/fleetledger/web/devices.json` (0600):
each one's name, login, origin and times, and only a SHA-256 of its session, so the
file opens nothing. Pairing links waiting to be opened are kept the same way in
`pairings.json`. A file that cannot be read counts as empty: nobody is signed in.

## Revoke a device

A lost phone, a laptop you no longer use, or anything you are unsure of:

```sh
ship-fleet ui devices                 # every paired device, its id, login and last use
ship-fleet ui revoke phone            # by name, or by its id
ship-fleet ui revoke --all            # every device
```

Or **Settings ▸ Devices ▸ Revoke** (or **Sign out every other device**). The running UI
refuses the device at its next request; it needs a new pairing link to get back in.
If the device itself may be compromised, also consider whether its Tailscale login was
(sign it out in the admin console, and take it off with `ship-fleet ui remote --login`).
Sessions on the desktop's own browser are not devices: stopping `ship-fleet ui` ends
them, as before.

## Turning it off

```sh
ship-fleet ui remote --off
tailscale serve --https=443 off     # or: tailscale serve reset (clears every Serve setting)
```

A `ship-fleet ui` already running refuses every request on the remote address from
then on. Paired devices stay listed (and refused while it is off); `ship-fleet ui
revoke --all` forgets them.

## When it refuses

| It says | Do |
|---|---|
| Tailscale is not running here | `tailscale up`, or `ship-fleet ui --local` |
| … is not this computer's Tailscale name | use the name it says (`Self.DNSName` in `tailscale status --json`) |
| Tailscale has no HTTPS certificate for … | turn on HTTPS certificates (admin console ▸ DNS) |
| Tailscale Serve is not serving … | `tailscale serve --bg 127.0.0.1:8765` |
| Funnel is on for … | `tailscale serve reset`, then serve it again on the tailnet only |
| … somewhere besides this UI | give FleetLedger the name to itself: `tailscale serve reset`, then serve it again |
| Tailscale Serve also serves … on port … | the same: FleetLedger needs every port of the name, since your browser sends its cookie to all of them |
| … not to this UI on 127.0.0.1:8765 | serve the port FleetLedger uses, or `ship-fleet ui remote … --port` the one Serve uses |
| The remote web UI is set up on port … | leave out `--port`, or add `--local` |
| Nobody may use … yet | `ship-fleet ui remote --login you@example.com` |
| (in the browser) This address works only through Tailscale Serve … | open it from a device signed in to the tailnet as you, not a tagged device |
| (in the browser) This Tailscale login may not use this FleetLedger | sign the device in to Tailscale as an allowed login, or add it: `ship-fleet ui remote --login ...` (the whole list) |
| (in the browser) This device is not signed in … Pair it once | `ssh desktop ship-fleet ui link --name NAME`, open the link there |
| (in the browser) … needs a fresh pairing | the same: pair that browser again, then retry |
| (in the browser) That link is used, expired or wrong | a new one: `ship-fleet ui link` |
