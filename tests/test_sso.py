import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from werkzeug.security import generate_password_hash

import metrics_store

_TMP = tempfile.TemporaryDirectory()
metrics_store.DB_PATH = os.path.join(_TMP.name, "metrics.db")

import yaml  # noqa: E402

import app  # noqa: E402
import sso  # noqa: E402

ISSUER = "https://account.example/"
SITE = "https://panopticon.example"
CLIENT_ID = "panopticon-client"
CLIENT_SECRET = "s3cret/with+odd=chars"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "k1"
PASSWORD_HASH = generate_password_hash("pw")

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "oauth2/auth",
    "token_endpoint": ISSUER + "oauth2/token",
    "jwks_uri": ISSUER + ".well-known/jwks.json",
    "end_session_endpoint": ISSUER + "oauth2/sessions/logout",
}


def _jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    jwk.update(kid=KID, alg="RS256", use="sig")
    return {"keys": [jwk]}


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sso.requests.HTTPError(str(self.status_code))


class FakeIdP:
    """Stands in for Hydra: discovery, JWKS and the token endpoint. claims() shapes the ID token."""

    def __init__(self):
        self.extra = {"roles": ["panopticon:superadmin"]}
        self.drop = ()
        self.override = {}
        self.key = KEY
        self.alg = "RS256"
        self.token_status = 200
        self.token_requests = []
        self.challenge = None
        self.nonce = None

    def get(self, url, timeout=None):
        assert url == ISSUER.rstrip("/") + "/.well-known/openid-configuration", url
        return Resp(200, DISCOVERY)

    def post(self, url, timeout=None, headers=None, data=None):
        assert url == DISCOVERY["token_endpoint"]
        self.token_requests.append({"headers": headers, "data": data})
        if self.token_status != 200:
            return Resp(self.token_status, {"error": "invalid_grant"})
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": [CLIENT_ID], "sub": "user-1", "iat": now, "exp": now + 300,
                  "nonce": self.nonce, "email": "airi@example.com", "name": "Airi", **self.extra}
        claims.update(self.override)
        for k in self.drop:
            claims.pop(k, None)
        signing = CLIENT_SECRET if self.alg == "HS256" else self.key
        token = jwt.encode(claims, signing, algorithm=self.alg, headers={"kid": KID})
        return Resp(200, {"access_token": "opaque", "id_token": token, "token_type": "bearer"})


class SsoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_config = app.CONFIG_PATH
        app.CONFIG_PATH = os.path.join(self.tmp.name, "config.yaml")
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()
        self.write_config(self.config())
        self.client = app.app.test_client()
        self.idp = FakeIdP()
        sso.provider = sso.Provider()
        patches = [
            mock.patch.object(sso.requests, "get", self.idp.get),
            mock.patch.object(sso.requests, "post", self.idp.post),
            mock.patch.object(jwt.PyJWKClient, "fetch_data", lambda _self: _jwks()),
            mock.patch.object(app, "_refresh_remote_async", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        app.CONFIG_PATH = self.old_config
        self.tmp.cleanup()

    def config(self, **sso_overrides):
        return {
            "dashboard_auth": {"enabled": True, "username": "mizuki", "password_hash": PASSWORD_HASH},
            "sso": {"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                    "redirect_uri": SITE + "/auth/callback", "post_logout_redirect_uri": SITE + "/login",
                    "session_minutes": 60, **sso_overrides},
        }

    def write_config(self, cfg):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)

    def get(self, path, **kw):
        return self.client.get(path, base_url=SITE, **kw)

    def start(self, next_hash=None):
        resp = self.get("/auth/login" + (f"?next={next_hash}" if next_hash else ""))
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertTrue(location.startswith(DISCOVERY["authorization_endpoint"] + "?"), location)
        q = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        self.idp.challenge = q["code_challenge"]
        self.idp.nonce = q["nonce"]
        return q

    def sign_in(self, next_hash=None, state=None):
        q = self.start(next_hash)
        return self.get(f"/auth/callback?code=abc&state={state or q['state']}")


class FlowTests(SsoTestCase):
    def test_authorize_request_uses_pkce_state_and_nonce(self):
        q = self.start()
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["redirect_uri"], SITE + "/auth/callback")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["scope"], "openid email profile")
        self.assertTrue(q["state"] and q["nonce"])

    def test_superadmin_signs_in_and_reaches_settings(self):
        resp = self.sign_in("%23p%3D1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/#p=1")
        req = self.idp.token_requests[0]
        verifier = req["data"]["code_verifier"]
        self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
                         self.idp.challenge)
        basic = base64.b64decode(req["headers"]["Authorization"].split()[1]).decode()
        self.assertEqual(basic, "panopticon-client:s3cret%2Fwith%2Bodd%3Dchars")
        self.assertEqual(req["data"]["redirect_uri"], SITE + "/auth/callback")
        me = self.get("/api/me").get_json()
        self.assertEqual(me, {"role": "superadmin", "via": "sso", "name": "Airi"})
        self.assertEqual(self.get("/api/config").status_code, 200)

    def test_admin_sees_the_dashboard_but_not_settings(self):
        self.idp.extra = {"roles": ["panopticon:admin"], "ssh_hosts": ["tokyo"]}
        self.sign_in()
        self.assertEqual(self.get("/api/me").get_json()["role"], "admin")
        for path in ("/", "/api/status", "/api/metrics/history?panel=x", "/api/appearance", "/api/kaomoji"):
            self.assertEqual(self.get(path).status_code, 200, path)
        for path in ("/api/config", "/api/agents", "/api/agent/hosts", "/api/webauthn/passkeys",
                     "/api/webauthn/register/options"):
            self.assertEqual(self.get(path).status_code, 403, path)
        for path in ("/api/config", "/api/appearance", "/api/kaomoji", "/api/notifications/test",
                     "/api/agent/deploy", "/api/agents/x/retire"):
            self.assertEqual(self.client.post(path, base_url=SITE, json={}).status_code, 403, path)
        self.assertEqual(self.client.delete("/api/agent/hosts/x", base_url=SITE).status_code, 403)

    def test_next_only_takes_a_dashboard_hash(self):
        resp = self.sign_in("https%3A%2F%2Fevil.example")
        self.assertEqual(resp.headers["Location"], "/")

    def test_opening_from_another_address_moves_to_the_registered_one_first(self):
        resp = self.client.get("/auth/login?next=%23p%3D2", base_url="http://10.10.0.1:1810")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], SITE + "/auth/login?next=%23p%3D2")

    def test_session_runs_out_and_has_to_go_back_to_sso(self):
        self.sign_in()
        later = time.time() + 3601
        with mock.patch.object(app.time, "time", lambda: later):
            self.assertEqual(self.get("/api/status").status_code, 401)
        self.assertEqual(self.get("/api/status").status_code, 401)  # the expired session was cleared

    def test_sign_out_also_signs_out_of_sso(self):
        self.sign_in()
        body = self.client.post("/logout", base_url=SITE).get_json()
        q = parse_qs(urlsplit(body["redirect"]).query)
        self.assertTrue(body["redirect"].startswith(DISCOVERY["end_session_endpoint"] + "?"))
        self.assertEqual(q["post_logout_redirect_uri"], [SITE + "/login"])
        self.assertTrue(q["id_token_hint"][0].count(".") == 2)
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_login_methods_say_whether_sso_is_on(self):
        self.assertEqual(self.get("/api/login/methods").get_json(), {"sso": True})
        self.write_config(self.config(client_secret=""))
        self.assertEqual(self.get("/api/login/methods").get_json(), {"sso": False})
        self.assertEqual(self.get("/auth/login").headers["Location"], "/login")


class RefusalTests(SsoTestCase):
    def assertRefused(self, resp, code):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], f"/login?sso_error={code}")
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_missing_roles_claim_fails_closed(self):
        self.idp.extra = {}
        self.assertRefused(self.sign_in(), "no_roles")

    def test_roles_that_are_not_a_list_fail_closed(self):
        self.idp.extra = {"roles": "panopticon:superadmin"}
        self.assertRefused(self.sign_in(), "no_roles")

    def test_no_panopticon_role_is_denied(self):
        self.idp.extra = {"roles": []}
        self.assertRefused(self.sign_in(), "denied")
        self.idp.extra = {"roles": ["pns:superadmin", "superadmin"]}
        self.assertRefused(self.sign_in(), "denied")

    def test_state_mismatch(self):
        self.assertRefused(self.sign_in(state="forged"), "expired")

    def test_callback_without_a_started_login(self):
        self.assertRefused(self.get("/auth/callback?code=abc&state=x"), "expired")

    def test_pending_login_is_used_once(self):
        q = self.start()
        self.get(f"/auth/callback?code=abc&state={q['state']}")
        self.client.post("/logout", base_url=SITE)
        self.assertRefused(self.get(f"/auth/callback?code=abc&state={q['state']}"), "expired")

    def test_error_from_sso(self):
        q = self.start()
        self.assertRefused(self.get(f"/auth/callback?error=access_denied&state={q['state']}"), "cancelled")

    def test_wrong_nonce(self):
        self.idp.override = {"nonce": "other"}
        self.assertRefused(self.sign_in(), "failed")

    def test_wrong_audience(self):
        self.idp.override = {"aud": ["someone-else"]}
        self.assertRefused(self.sign_in(), "failed")

    def test_wrong_issuer(self):
        self.idp.override = {"iss": "https://evil.example/"}
        self.assertRefused(self.sign_in(), "failed")

    def test_expired_token(self):
        self.idp.override = {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}
        self.assertRefused(self.sign_in(), "failed")

    def test_signed_by_another_key(self):
        self.idp.key = OTHER_KEY
        self.assertRefused(self.sign_in(), "failed")

    def test_hs256_signed_with_the_client_secret_is_refused(self):
        self.idp.alg = "HS256"
        self.assertRefused(self.sign_in(), "failed")

    def test_token_endpoint_error(self):
        self.idp.token_status = 400
        self.assertRefused(self.sign_in(), "failed")

    def test_discovery_down(self):
        def down(url, timeout=None):
            raise sso.requests.ConnectionError("nope")
        with mock.patch.object(sso.requests, "get", down):
            resp = self.get("/auth/login")
        self.assertEqual(resp.headers["Location"], "/login?sso_error=unavailable")


class ReviewFixTests(SsoTestCase):
    def test_sso_on_requires_sign_in_even_with_dashboard_auth_off(self):
        cfg = self.config()
        cfg["dashboard_auth"]["enabled"] = False
        self.write_config(cfg)
        for path in ("/api/status", "/api/me", "/api/config", "/api/webauthn/register/options", "/api/agent/hosts"):
            self.assertEqual(self.get(path).status_code, 401, path)
        self.assertEqual(self.client.post("/api/webauthn/register/verify", base_url=SITE, json={}).status_code, 401)

    def test_half_filled_sso_section_still_requires_sign_in(self):
        cfg = self.config(client_secret="")
        cfg["dashboard_auth"]["enabled"] = False
        self.write_config(cfg)
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_two_tabs_signing_in_do_not_cancel_each_other(self):
        first = self.start()
        self.start()
        self.idp.nonce = first["nonce"]
        resp = self.get(f"/auth/callback?code=abc&state={first['state']}")
        self.assertEqual(resp.headers["Location"], "/")
        self.assertEqual(self.get("/api/me").get_json()["role"], "superadmin")

    def test_pending_login_runs_out(self):
        q = self.start()
        later = time.time() + sso.PENDING_MAX_AGE + 1
        with mock.patch.object(app.time, "time", lambda: later):
            resp = self.get(f"/auth/callback?code=abc&state={q['state']}")
        self.assertEqual(resp.headers["Location"], "/login?sso_error=expired")

    def test_plain_http_issuer_is_refused(self):
        self.write_config(self.config(issuer="http://account.example/"))
        self.assertEqual(self.get("/auth/login").headers["Location"], "/login?sso_error=failed")
        self.assertEqual(self.idp.token_requests, [])

    def test_token_endpoint_on_another_origin_is_refused(self):
        evil = dict(DISCOVERY, token_endpoint="http://attacker.example/token")
        with mock.patch.object(sso.requests, "get", lambda url, timeout=None: Resp(200, evil)):
            self.assertEqual(self.get("/auth/login").headers["Location"], "/login?sso_error=failed")

    def test_wrong_azp_with_a_single_audience(self):
        self.idp.override = {"azp": "someone-else"}
        resp = self.sign_in()
        self.assertEqual(resp.headers["Location"], "/login?sso_error=failed")

    def test_cookie_is_secure_over_https_only(self):
        resp = self.client.post("/login", base_url=SITE, json={"username": "mizuki", "password": "pw"})
        self.assertIn("Secure", resp.headers["Set-Cookie"])
        resp = self.client.post("/login", base_url="http://10.10.0.1:1810", json={"username": "mizuki", "password": "pw"})
        self.assertNotIn("Secure", resp.headers["Set-Cookie"])

    def test_local_session_lasts_twelve_hours(self):
        self.client.post("/login", base_url=SITE, json={"username": "mizuki", "password": "pw"})
        later = time.time() + app.LOCAL_SESSION_SECONDS + 1
        with mock.patch.object(app.time, "time", lambda: later):
            self.assertEqual(self.get("/api/status").status_code, 401)


class LocalLoginTests(SsoTestCase):
    def test_local_password_is_the_superadmin_way_in(self):
        resp = self.client.post("/login", base_url=SITE, json={"username": "mizuki", "password": "pw"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.get("/api/me").get_json(), {"role": "superadmin", "via": "local", "name": ""})
        self.assertEqual(self.get("/api/config").status_code, 200)
        self.assertNotIn("redirect", self.client.post("/logout", base_url=SITE).get_json())

    def test_a_session_from_before_roles_must_sign_in_again(self):
        with self.client.session_transaction(base_url=SITE) as s:
            s["authed"] = True
        self.assertEqual(self.get("/api/status").status_code, 401)

    def test_saving_settings_keeps_the_sso_section(self):
        self.client.post("/login", base_url=SITE, json={"username": "mizuki", "password": "pw"})
        cfg = self.get("/api/config").get_json()
        self.assertTrue(cfg["sso_enabled"])
        self.assertNotIn(CLIENT_SECRET, json.dumps(cfg))
        resp = self.client.post("/api/config", base_url=SITE, json={"dashboard_auth": {"enabled": True}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(app.load_config()["sso"]["client_secret"], CLIENT_SECRET)


if __name__ == "__main__":
    unittest.main()
