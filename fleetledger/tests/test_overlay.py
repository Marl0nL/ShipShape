"""Overlay keys (phase 5): pasted keys and keys minted through the Tailscale API.

The OAuth client secret, the access token and every minted key are secrets: each
test that handles one checks it is not in any message.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Ship

from fleetledger import overlay

SECRET = "tskey-client-kExample3CNTRL-OAUTHSECRET0123456789"
TOKEN = "tskey-api-ACCESSTOKEN0123456789abcdef"
MINTED = "tskey-auth-kExample4CNTRL-MINTEDKEY0123456789"
SHIP = Ship(name="ship-03", id="0b9f6c1e-7d7a-4d4c-8f39-2f1d7e1f0c11", driver="manual",
            names=("ship-03.local",), host_key="ssh-ed25519 AAAA")


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api_config(tmp_path):
    secret = tmp_path / "tailscale-oauth"
    secret.write_text(SECRET + "\n")
    secret.chmod(0o600)
    return {"provider": "tailscale", "tags": ["tag:ship", "tag:ship-work"],
            "key_source": "tailscale-api",
            "api": {"oauth_client_id": "kExampleClient", "oauth_secret_file": str(secret),
                    "key_expiry_s": 300}}


def recording(answers):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(json.dumps(answer).encode())

    return opener, seen


def test_api_mints_a_single_use_preapproved_tagged_key(api_config):
    opener, seen = recording([{"access_token": TOKEN}, {"id": "k1", "key": MINTED}])
    keys = overlay.key_source(api_config, opener=opener)
    assert keys.key(SHIP) == MINTED
    token_req, key_req = seen
    assert token_req.full_url == "https://api.tailscale.com/api/v2/oauth/token"
    form = urllib.parse.parse_qs(token_req.data.decode())
    assert form == {"client_id": ["kExampleClient"], "client_secret": [SECRET],
                    "grant_type": ["client_credentials"]}
    assert key_req.full_url == "https://api.tailscale.com/api/v2/tailnet/-/keys"
    assert key_req.get_header("Authorization") == f"Bearer {TOKEN}"
    body = json.loads(key_req.data)
    create = body["capabilities"]["devices"]["create"]
    assert create == {"reusable": False, "ephemeral": False, "preauthorized": True,
                      "tags": ["tag:ship", "tag:ship-work"]}
    assert body["expirySeconds"] == 300 and body["description"] == "ShipShape ship-03"


def test_api_keys_expire_in_ten_minutes_by_default(api_config):
    del api_config["api"]["key_expiry_s"]
    opener, seen = recording([{"access_token": TOKEN}, {"key": MINTED}])
    overlay.key_source(api_config, opener=opener).key(SHIP)
    assert json.loads(seen[1].data)["expirySeconds"] == 600


def test_api_reuses_its_access_token(api_config):
    opener, seen = recording([{"access_token": TOKEN}, {"key": MINTED}, {"key": MINTED}])
    keys = overlay.key_source(api_config, opener=opener)
    keys.key(SHIP)
    keys.key(SHIP)
    assert len(seen) == 3


def test_api_errors_never_carry_secrets(api_config):
    body = json.dumps({"message": "requested tags [tag:ship] are invalid or not permitted"})
    error = urllib.error.HTTPError("https://api.tailscale.com/api/v2/tailnet/-/keys", 403,
                                   "Forbidden", {}, io.BytesIO(body.encode()))
    opener, _ = recording([{"access_token": TOKEN}, error])
    with pytest.raises(FleetError) as exc:
        overlay.key_source(api_config, opener=opener).key(SHIP)
    text = str(exc.value) + repr(exc.value) + (exc.value.hint or "")
    assert "403" in text and "not permitted" in text
    for secret in (SECRET, TOKEN, MINTED):
        assert secret not in text
    assert exc.value.__cause__ is None and exc.value.__context__ is not None
    assert exc.value.__suppress_context__  # the request (with the token) is not chained


def test_api_refuses_a_secret_file_others_can_read(api_config, tmp_path):
    secret = tmp_path / "tailscale-oauth"
    secret.chmod(0o644)
    opener, seen = recording([])
    with pytest.raises(ConfigError, match="chmod 600"):
        overlay.key_source(api_config, opener=opener).key(SHIP)
    assert seen == []


def test_api_answer_must_be_a_key(api_config):
    opener, _ = recording([{"access_token": TOKEN}, {"key": "not a key at all"}])
    with pytest.raises(FleetError, match="not an auth key"):
        overlay.key_source(api_config, opener=opener).key(SHIP)


def test_prompt_needs_a_terminal_and_reads_without_echo():
    config = {"provider": "tailscale"}
    asked = []
    keys = overlay.key_source(config, ask=lambda p: asked.append(p) or MINTED + "\n",
                              isatty=lambda: True)
    assert keys.key(SHIP) == MINTED
    assert "ship-03" in asked[0] and "tag:ship" in asked[0] and "not shown" in asked[0]
    with pytest.raises(FleetError, match="needs a terminal"):
        overlay.key_source(config, ask=lambda p: MINTED, isatty=lambda: False).key(SHIP)
    with pytest.raises(FleetError, match="not an auth key"):
        overlay.key_source(config, ask=lambda p: "two words", isatty=lambda: True).key(SHIP)


def test_prompt_for_headscale_says_where_to_get_a_key():
    keys = overlay.key_source({"provider": "tailscale", "login_server": "https://hs.example.com"},
                              ask=lambda p: pytest.fail(p) if "headscale" not in p else MINTED,
                              isatty=lambda: True)
    assert keys.key(SHIP) == MINTED


def test_names_with_and_without():
    joined = overlay.names_with(SHIP, "ship-03.example-tailnet.ts.net")
    assert joined == ["ship-03.example-tailnet.ts.net", "ship-03.local"]
    ship = Ship(**{**SHIP.__dict__, "names": tuple(joined)})
    assert overlay.names_with(ship, "ship-03.example-tailnet.ts.net") == joined
    assert overlay.names_without(ship, "ship-03.example-tailnet.ts.net") == ["ship-03.local"]
    assert overlay.names_with(SHIP, None) == ["ship-03.local"]
