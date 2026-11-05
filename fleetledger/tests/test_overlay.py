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


def test_ships_are_tagged_tag_ship_unless_the_inventory_says_otherwise():
    assert overlay.tags({}) == ["tag:ship"]
    assert overlay.tags({"tags": ["tag:a", "tag:b"]}) == ["tag:a", "tag:b"]


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


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_api_refuses_a_secret_file_others_can_read(api_config, tmp_path, mode):
    secret = tmp_path / "tailscale-oauth"
    secret.chmod(mode)
    opener, seen = recording([])
    with pytest.raises(ConfigError, match="chmod 600"):
        overlay.key_source(api_config, opener=opener).key(SHIP)
    assert seen == []


def test_sign_in_errors_never_carry_the_secret(api_config):
    for answer in ({"no_token": SECRET}, {"access_token": ""}):
        opener, _ = recording([answer])
        with pytest.raises(FleetError) as exc:
            overlay.key_source(api_config, opener=opener).key(SHIP)
        assert SECRET not in str(exc.value) and "access token" in str(exc.value)


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


# --- the Tailscale API client ----------------------------------------------------------

from fleetledger.overlay import providers  # noqa: E402
from fleetledger.overlay.tsapi import TailscaleAPI  # noqa: E402


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class TaggedResponse(Response):
    def __init__(self, body, etag=None):
        super().__init__(body)
        self.headers = Headers({"ETag": etag} if etag else {})
        self.status = 200


def client(answers, secret=SECRET):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        body, etag = answer if isinstance(answer, tuple) else (answer, None)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return TaggedResponse(raw, etag)

    api = TailscaleAPI({"oauth_client_id": "kExampleClient", "oauth_secret_file": "/unused"},
                       opener=opener, secret=lambda: secret)
    return api, seen


def test_api_reads_the_granted_scopes():
    api, _ = client([{"access_token": TOKEN, "scope": "auth_keys devices:core"}])
    api.token()
    assert api.granted == {"auth_keys", "devices:core"}
    api, _ = client([{"access_token": TOKEN}])
    api.token()
    assert api.granted is None  # not said: unknown, not "none"


def test_api_lists_and_removes_devices():
    api, seen = client([{"access_token": TOKEN},
                        {"devices": [{"nodeId": "nAbc123CNTRL", "name": "ship-03.ts.example"}]},
                        {}])
    assert api.devices()[0]["nodeId"] == "nAbc123CNTRL"
    api.delete_device("nAbc123CNTRL")
    assert seen[1].full_url.endswith("/tailnet/-/devices")
    assert (seen[2].get_method(), seen[2].full_url) == (
        "DELETE", "https://api.tailscale.com/api/v2/device/nAbc123CNTRL")


@pytest.mark.parametrize("odd", ["../tailnet/-/keys", "n1/../2", "", "a b"])
def test_api_refuses_an_odd_device_id(odd):
    api, seen = client([{"access_token": TOKEN}])
    with pytest.raises(FleetError, match="odd device id"):
        api.delete_device(odd)
    assert seen == []  # not even a sign-in


def test_api_policy_text_keeps_comments_and_etag():
    text = '{\n  // my rules\n  "acls": [],\n}\n'
    api, seen = client([{"access_token": TOKEN}, (text.encode(), '"etag-1"')])
    assert api.policy_text() == (text, '"etag-1"')
    assert seen[1].get_header("Accept") == "application/hujson"


def test_api_changes_the_policy_only_with_its_etag():
    api, seen = client([{"access_token": TOKEN}, {}])
    with pytest.raises(FleetError, match="no ETag"):
        api.set_policy("{}", None)
    api.set_policy('{"acls": []}', '"etag-1"')
    assert seen[-1].get_header("If-match") == '"etag-1"'
    assert seen[-1].get_method() == "POST" and seen[-1].full_url.endswith("/tailnet/-/acl")


def test_api_validate_reports_problems():
    api, _ = client([{"access_token": TOKEN},
                     {"message": "test(s) failed",
                      "data": [{"user": "tag:ship", "errors": ["tag:ship:22 allowed"]}]},
                     {}])
    assert api.validate("{}") == ["test(s) failed", "tag:ship: tag:ship:22 allowed"]
    assert api.validate("{}") == []


# --- providers -------------------------------------------------------------------------


def test_providers_by_name():
    assert providers.get({"provider": "tailscale"}).id == "tailscale"
    assert providers.get({"provider": "headscale",
                          "login_server": "https://hs.example.com"}).id == "headscale"
    with pytest.raises(FleetError, match="Unknown overlay provider"):
        providers.get({"provider": "zerotier"})


def test_headscale_joins_with_its_server_and_pasted_keys():
    p = providers.get({"provider": "headscale", "login_server": "https://hs.example.com",
                       "key_source": "tailscale-api"})  # never the API, whatever it says
    assert isinstance(p.keys(), overlay.PromptKeys)
    args = p.join_args(SHIP)
    assert args[args.index("--login-server") + 1] == "https://hs.example.com"
    removal = p.remove(SHIP)
    assert not removal.done and "headscale nodes delete" in removal.messages[0]


class FakeTS:
    def __init__(self, devices):
        self._devices = devices
        self.deleted = []

    def devices(self):
        return self._devices

    def delete_device(self, device_id):
        self.deleted.append(device_id)


def api_provider(devices, tags=("tag:ship",)):
    fake = FakeTS(devices)
    config = {"provider": "tailscale", "tags": list(tags), "key_source": "tailscale-api",
              "api": {"oauth_client_id": "x", "oauth_secret_file": "/unused",
                      "remove_devices": True}}
    return providers.get(config, api_factory=lambda api: fake), fake


def test_remove_deletes_the_ships_device_by_its_recorded_name():
    ship = Ship(**{**SHIP.__dict__, "names": ("ship-03.ts.example", "ship-03.local")})
    p, fake = api_provider([
        {"nodeId": "nOther", "name": "laptop.ts.example", "hostname": "laptop", "tags": []},
        {"nodeId": "nShip3", "name": "ship-03.ts.example.", "hostname": "renamed",
         "tags": ["tag:ship"]},
    ])
    removal = p.remove(ship)
    assert removal.done and fake.deleted == ["nShip3"]
    assert "tailnet name or address" in removal.messages[0]


def test_remove_by_node_id_wins_over_names():
    """The node id recorded at join is the identity: a renamed device still matches,
    and a device that merely has the Ship's name does not."""
    ship = Ship(**{**SHIP.__dict__, "names": ("ship-03.ts.example",), "overlay_node": "nShip3"})
    p, fake = api_provider([
        {"nodeId": "nImpostor", "name": "ship-03.ts.example", "hostname": "ship-03",
         "tags": ["tag:ship"]},
        {"nodeId": "nShip3", "name": "something-else.ts.example", "hostname": "x",
         "tags": ["tag:ship"]},
    ])
    removal = p.remove(ship)
    assert removal.done and fake.deleted == ["nShip3"] and "node id" in removal.messages[0]


def test_remove_uses_the_live_node_id_and_addresses():
    p, fake = api_provider([{"nodeId": "nLive", "name": "a.ts.example", "hostname": "a",
                             "tags": ["tag:ship"], "addresses": ["192.0.2.10"]}])
    assert p.remove(SHIP, {"node_id": "nLive"}).done and fake.deleted == ["nLive"]
    p, fake = api_provider([{"nodeId": "nAddr", "name": "a.ts.example", "hostname": "a",
                             "tags": ["tag:ship"], "addresses": ["192.0.2.10"]}])
    assert p.remove(SHIP, {"addresses": ["192.0.2.10"]}).done and fake.deleted == ["nAddr"]


def test_remove_never_by_hostname_alone():
    """A tagged device whose hostname is the Ship's name, with nothing recorded to tie it
    to this Ship (another fleet on the tailnet, a stale one): never removed."""
    p, fake = api_provider([{"nodeId": "nTheirs", "name": "ship-03.ts.example",
                             "hostname": "ship-03", "tags": ["tag:ship"]}])
    removal = p.remove(SHIP)  # SHIP.names has only ship-03.local, no node id
    assert not removal.done and fake.deleted == [] and "cannot tell" in removal.messages[0]


def test_remove_never_touches_an_untagged_device():
    ship = Ship(**{**SHIP.__dict__, "names": ("ship-03.ts.example",), "overlay_node": "nMine"})
    p, fake = api_provider([{"nodeId": "nMine", "name": "ship-03.ts.example",
                             "hostname": "ship-03", "tags": []}])
    removal = p.remove(ship)
    assert fake.deleted == [] and not removal.done


def test_remove_does_nothing_when_several_devices_match():
    ship = Ship(**{**SHIP.__dict__, "names": ("ship-03.ts.example",)})
    p, fake = api_provider([
        {"nodeId": "n1", "name": "ship-03.ts.example", "tags": ["tag:ship"]},
        {"nodeId": "n2", "name": "other.ts.example", "tags": ["tag:ship"],
         "addresses": ["192.0.2.10"]},
    ])
    removal = p.remove(ship, {"addresses": ["192.0.2.10"]})
    assert not removal.done and fake.deleted == [] and "Several" in removal.messages[0]


def test_remove_devices_off_prints_the_steps():
    fake = FakeTS([{"nodeId": "nShip3", "name": "ship-03.ts.example", "tags": ["tag:ship"]}])
    config = {"provider": "tailscale", "key_source": "tailscale-api",
              "api": {"oauth_client_id": "x", "oauth_secret_file": "/unused",
                      "remove_devices": False}}
    p = providers.get(config, api_factory=lambda api: (_ for _ in ()).throw(AssertionError))
    removal = p.remove(Ship(**{**SHIP.__dict__, "overlay_node": "nShip3"}))
    assert not removal.done and "remove_devices" in removal.messages[0]
    assert fake.deleted == []


def test_device_removal_is_off_unless_turned_on():
    """The product owner's decision: by default the secret only mints keys."""
    assert providers.REMOVE_DEVICES_DEFAULT is False
    config = {"provider": "tailscale", "key_source": "tailscale-api",
              "api": {"oauth_client_id": "x", "oauth_secret_file": "/unused"}}
    p = providers.get(config, api_factory=lambda api: (_ for _ in ()).throw(AssertionError))
    assert p.removes_devices is False
    ship = Ship(**{**SHIP.__dict__, "names": ("ship-03.ts.example", "ship-03.local"),
                   "overlay_node": "nShip3"})
    removal = p.remove(ship)  # never signs in to the API
    assert not removal.done
    assert "admin console > Machines > ship-03.ts.example, node id nShip3" in removal.messages[0]


def test_writing_the_policy_needs_manage_policy():
    class Recorder(FakeTS):
        def __init__(self):
            super().__init__([])
            self.written = []

        def set_policy(self, text, etag):
            self.written.append((text, etag))

    for manage, allowed in ((False, False), (True, True)):
        fake = Recorder()
        config = {"provider": "tailscale", "key_source": "tailscale-api",
                  "api": {"oauth_client_id": "x", "oauth_secret_file": "/unused",
                          "manage_policy": manage}}
        p = providers.get(config, api_factory=lambda api, f=fake: f)
        if allowed:
            p.write_policy("{}", '"e"')
            assert fake.written == [("{}", '"e"')]
        else:
            with pytest.raises(FleetError, match="manage_policy is off"):
                p.write_policy("{}", '"e"')
            assert fake.written == []
    with pytest.raises(FleetError):
        providers.get({"provider": "tailscale"}).write_policy("{}", '"e"')


def test_remove_with_pasted_keys_says_how():
    p = providers.get({"provider": "tailscale"})
    removal = p.remove(SHIP)
    assert not removal.done and "admin console" in removal.messages[0]
