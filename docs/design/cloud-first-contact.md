# Design note: first contact for cloud Ships

Status: a note for later, not built. It records the options so the GCP (or other
cloud) driver starts from a decision, not a blank page.

## The problem

On a LAN, FleetLedger reaches a new Ship over SSH as soon as it boots: the seed
carries the Ship's host key, so the first connection is already pinned, and the
overlay key follows over that SSH session (`ship-ctl overlay join`).

A cloud Ship has no shared LAN, and two things change:

- **Reachability.** The Ship is behind the cloud's network. Opening port 22 to the
  internet would work but goes against "nothing listening that you did not choose".
- **Secrets at boot.** Cloud metadata (user-data, instance attributes) is readable
  by every process on the VM, for the VM's whole life. The rule today is that no
  key ever goes into a seed or metadata; the host *private* key already cannot
  (the plan's GCP note: read host keys from guest attributes instead).

So the question is: how does a fresh cloud Ship get onto the overlay, or
otherwise become reachable over SSH, without a long-lived secret in metadata?

## Options

### A. A short-lived overlay key in metadata

The driver mints a single-use, pre-approved, tagged key that expires in minutes
and puts it in user-data; the Ship joins at boot, and FleetLedger then reaches it
over the overlay and pins its host key from guest attributes.

- For: simplest; works with any cloud and any overlay that has pre-auth keys.
- Against: the key sits in metadata for the VM's life. It is used up at once and
  expires within minutes, but anything on the Ship (an agent) can read it, and if
  the Ship fails to join before expiry it is stuck. It also breaks the "never in
  metadata" rule, so it needs the cloud's metadata access cut down after boot
  (where the cloud allows that) and a clear exception in the threat model.

### B. The cloud's own tunnel for first contact (for example IAP TCP forwarding)

FleetLedger reaches port 22 through the cloud's authenticated tunnel, with no
public port and nothing secret in metadata, and then does exactly what it does on
the LAN: pinned SSH (host key from guest attributes), `overlay join` over SSH.

- For: keeps every current rule intact. First contact is authenticated by your
  cloud identity, not by a secret on the VM. The tunnel can be closed again once
  the Ship is on the overlay.
- Against: cloud-specific (each driver needs its tunnel). It needs the cloud CLI
  and suitable permissions on your computer, and a firewall rule for the
  tunnel's address range.

### C. Identity-based join (workload identity)

The Ship proves who it is with the cloud's instance identity (a signed identity
token from the metadata server), and the overlay accepts it directly: for example
Tailscale's workload identity federation, which trades a cloud-signed token for a
tagged node without any pre-shared key.

- For: no secret at all, anywhere; the strongest story.
- Against: depends on the overlay supporting the cloud's identity, and on getting
  the trust conditions right (project, service account, audience). It is newer and
  differs per overlay, so a provider that lacks it needs A or B.

## Recommendation

Make **B** the default for the first cloud driver: it changes no rule and reuses
everything built for the LAN. Add **C** per overlay where it exists (it removes
even the first-contact dependency on the tunnel). Keep **A** only as a documented
fallback for clouds with neither, with the exception written into the threat
model.

Whichever is chosen, keep these unchanged: the host key is known before the first
SSH connection (guest attributes, never trust-on-first-use), SSH stays the only
way in, and the overlay rules give Ships no destinations.
