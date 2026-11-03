"""A small client for Tailscale's API, signed in with a scoped OAuth client.

Scopes (Tailscale's "trust credentials" reference; the names are checked against
the current docs, see docs/overlay-providers.md for the three levels):

- auth_keys (level 1, the default): make the single-use keys Ships join with;
- devices:core (level 2, overlay.api.remove_devices, opt-in): find and remove a
  destroyed Ship's device. Tailscale makes you pick tags for a client with this
  scope, but the tags only bind the keys it makes: the scope itself reaches every
  device in the tailnet, so a leaked secret could remove any of them;
- policy_file (level 3, overlay.api.manage_policy, opt-in; needs devices:core:read
  and devices:posture_attributes): read, validate and change the tailnet policy.

The client secret is read for each sign-in from its store (secret_store: the OS
keyring or a 0600 file). Neither it, the
access token, nor a minted key is ever printed, logged or put in an exception,
and errors are raised `from None` so the request (which carries the token) is not
chained into the traceback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fleetledger.errors import FleetError
from fleetledger.overlay import secret_store

API = "https://api.tailscale.com/api/v2"

Opener = Callable[[urllib.request.Request, float], Any]


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (https only)


def read_secret(path_value: str) -> str:
    """The secret from a 0600 file (kept for callers; see secret_store)."""
    return secret_store.read_file(path_value)


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes
    etag: str | None


class TailscaleAPI:
    def __init__(self, api: Mapping[str, Any], *, opener: Opener = default_opener,
                 secret: Callable[[], str] | None = None) -> None:
        self.api = api
        self.opener = opener
        self._secret = secret or (lambda: secret_store.load(api))
        self._token: str | None = None
        # The scopes the sign-in reported; None when Tailscale did not say.
        self.granted: frozenset[str] | None = None

    @property
    def tailnet(self) -> str:
        return urllib.parse.quote(self.api.get("tailnet", "-"), safe="-.@")

    # --- transport --------------------------------------------------------------
    def _send(self, request: urllib.request.Request, what: str) -> Answer:
        try:
            with self.opener(request, 30) as response:
                headers = getattr(response, "headers", None)
                return Answer(getattr(response, "status", 200), response.read(),
                              headers.get("ETag") if headers is not None else None)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read() or b"{}")
                detail = str(body.get("message", ""))[:300] if isinstance(body, dict) else ""
            except (ValueError, OSError):
                pass
            raise FleetError(f"Tailscale API: {what} failed ({exc.code})"
                             + (f": {detail}" if detail else "") + ".") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FleetError(f"Tailscale API: cannot reach it for {what} ({reason}).") from None

    @staticmethod
    def _json(answer: Answer, what: str) -> Any:
        try:
            return json.loads(answer.body) if answer.body else {}
        except ValueError:
            raise FleetError(f"Tailscale API: {what} did not answer JSON.") from None

    def token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode({
                "client_id": self.api["oauth_client_id"],
                "client_secret": self._secret(),
                "grant_type": "client_credentials",
            }).encode()
            request = urllib.request.Request(f"{API}/oauth/token", data=body, method="POST")
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            doc = self._json(self._send(request, "the OAuth sign-in"), "the OAuth sign-in")
            token = doc.get("access_token") if isinstance(doc, dict) else None
            if not isinstance(token, str) or not token:
                raise FleetError("Tailscale API: the OAuth sign-in returned no access token.")
            self._token = token
            scope = doc.get("scope")
            self.granted = frozenset(scope.split()) if isinstance(scope, str) else None
        return self._token

    def call(self, method: str, path: str, what: str, *, body: bytes | None = None,
             content_type: str | None = None, accept: str = "application/json",
             headers: Mapping[str, str] | None = None) -> Answer:
        request = urllib.request.Request(f"{API}{path}", data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token()}")
        request.add_header("Accept", accept)
        if content_type:
            request.add_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        return self._send(request, what)

    # --- keys -------------------------------------------------------------------
    def create_key(self, tags: list[str], expiry_s: int, description: str) -> str:
        body = json.dumps({
            "capabilities": {"devices": {"create": {
                "reusable": False,
                "ephemeral": False,
                "preauthorized": True,
                "tags": tags,
            }}},
            "expirySeconds": int(expiry_s),
            "description": description[:50],
        }).encode()
        answer = self.call("POST", f"/tailnet/{self.tailnet}/keys", "making a key", body=body,
                           content_type="application/json")
        key = self._json(answer, "making a key")
        key = key.get("key") if isinstance(key, dict) else None
        if not isinstance(key, str):
            raise FleetError("Tailscale API: no key in the answer.")
        return key

    # --- devices ----------------------------------------------------------------
    def devices(self) -> list[dict]:
        doc = self._json(self.call("GET", f"/tailnet/{self.tailnet}/devices",
                                   "listing devices"), "listing devices")
        found = doc.get("devices") if isinstance(doc, dict) else None
        return [d for d in found or [] if isinstance(d, dict)]

    def delete_device(self, device_id: str) -> None:
        if not device_id.replace("-", "").isalnum():
            raise FleetError(f"Tailscale API: odd device id {device_id!r}; not deleting it.")
        self.call("DELETE", f"/device/{urllib.parse.quote(device_id, safe='')}",
                  "removing a device")

    # --- the policy file ----------------------------------------------------------
    def policy_text(self) -> tuple[str, str | None]:
        """The policy as written (HuJSON, comments kept) and its ETag."""
        answer = self.call("GET", f"/tailnet/{self.tailnet}/acl", "reading the policy",
                           accept="application/hujson")
        return answer.body.decode("utf-8"), answer.etag

    def policy_json(self) -> dict:
        doc = self._json(self.call("GET", f"/tailnet/{self.tailnet}/acl",
                                   "reading the policy"), "reading the policy")
        if not isinstance(doc, dict):
            raise FleetError("Tailscale API: the policy is not an object.")
        return doc

    def validate(self, text: str) -> list[str]:
        """Problems Tailscale finds in a policy, its tests included (empty: none)."""
        answer = self.call("POST", f"/tailnet/{self.tailnet}/acl/validate",
                           "validating the policy", body=text.encode("utf-8"),
                           content_type="application/hujson")
        doc = self._json(answer, "validating the policy")
        if not isinstance(doc, dict) or not doc:
            return []
        problems = [str(doc["message"])] if doc.get("message") else []
        for item in doc.get("data") or []:
            if isinstance(item, dict):
                for err in item.get("errors") or []:
                    problems.append(f"{item.get('user', '')}: {err}".strip(": "))
        return problems[:20]

    def set_policy(self, text: str, etag: str | None) -> None:
        """Replace the policy, only if nobody changed it since it was read."""
        if not etag:
            raise FleetError("Tailscale API: no ETag for the policy; not changing it.")
        self.call("POST", f"/tailnet/{self.tailnet}/acl", "changing the policy",
                  body=text.encode("utf-8"), content_type="application/hujson",
                  headers={"If-Match": etag})
