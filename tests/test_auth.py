import json
import time
import urllib.parse

import httpx

from google_photos_picker import SCOPE, OAuthCredentials, OAuthFlow


def test_pkce_url_and_exchange():
    flow = OAuthFlow("cid", "sec", [SCOPE], redirect_uri="https://app/cb")
    req = flow.authorization_url()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
    assert q["scope"] == SCOPE and q["code_challenge_method"] == "S256"

    def token(r):
        form = dict(urllib.parse.parse_qsl(r.content.decode()))
        assert form["code_verifier"] == req.code_verifier
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r",
                                         "expires_in": 3600})

    creds = flow.exchange_code("c", req.code_verifier,
                               http=httpx.Client(transport=httpx.MockTransport(token)))
    assert creds.access_token == "a" and not creds.expired


def test_refresh_and_persist(tmp_path):
    p = tmp_path / "t.json"
    OAuthCredentials(access_token="old", refresh_token="r", client_id="c",
                     expires_at=time.time() - 1).save(p)
    creds = OAuthCredentials.load(p)
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"access_token": "new", "expires_in": 3600})))
    assert creds.token(http) == "new"
    assert json.loads(p.read_text())["access_token"] == "new"
