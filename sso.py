"""
用 Palintropic 账户登录（OIDC 授权码流程 + PKCE，机密 client，client_secret_basic）。

SSO 负责「你是谁、你是哪一档」，Panopticon 只按 ID token 里的声明放行，自己不存用户和密码：
- 档位在 roles 声明里，只认 panopticon:superadmin / panopticon:admin 两档；
- roles 这个声明整个缺失时一律拒绝登录（fail-closed）。只有 SSO 的 token hook 会写它，缺了说明
  hook 没生效，这时放行就等于谁登录都不知道档位；空数组则是正常的「没有权限」；
- 不拿 refresh token。Panopticon 自己的会话到期后回 SSO 重新授权，SSO 会话还在的话用户无感，
  档位每次都是现查的，撤销最迟在一个会话时长后生效。
access token 不当身份凭据用，拿到就丢。
"""
import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

import jwt
import requests

APP = "panopticon"
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
SCOPE = "openid email profile"
PENDING_MAX_AGE = 600   # 秒，从跳去 SSO 到回来最多这么久，超过就当这次登录作废
HTTP_TIMEOUT = 10       # 秒
DISCOVERY_TTL = 3600    # 秒
# Hydra 默认用 RS256 签 ID token；这里只放行非对称算法，绝不接受 none 和 HS*（HS 用的是
# client_secret，任何拿到密钥的人都能自己签一个）
ALLOWED_ALGS = ("RS256", "ES256", "EdDSA")
DEFAULT_SESSION_MINUTES = 60


class SsoError(Exception):
    """登录失败。code 给登录页挑提示语用，detail 只进日志，不回显给浏览器。"""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def settings_from(cfg):
    """config.yaml 的 sso 段。没开或者没填全时返回 None，登录页就不显示 SSO 按钮。"""
    s = (cfg or {}).get("sso") or {}
    if not s.get("enabled"):
        return None
    out = {k: str(s.get(k) or "").strip() for k in
           ("issuer", "client_id", "client_secret", "redirect_uri", "post_logout_redirect_uri")}
    if not all(out[k] for k in ("issuer", "client_id", "client_secret", "redirect_uri")):
        return None
    try:
        minutes = int(s.get("session_minutes") or DEFAULT_SESSION_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_SESSION_MINUTES
    out["session_seconds"] = max(5, minutes) * 60
    return out


def origin_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _https_or_loopback(url):
    """client 密钥只能发到 https，本机开发（localhost / 127.0.0.1）例外。"""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Provider:
    """discovery 文档和签名公钥的缓存，按 issuer 各存一份；配置改了 issuer 自然换一份。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._discovery = {}   # issuer -> (fetched_at, doc)
        self._jwks = {}        # jwks_uri -> PyJWKClient

    def discovery(self, issuer):
        with self._lock:
            hit = self._discovery.get(issuer)
            if hit and time.time() - hit[0] < DISCOVERY_TTL:
                return hit[1]
        if not _https_or_loopback(issuer):
            raise SsoError("failed", f"issuer must be https: {issuer}")
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            doc = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise SsoError("unavailable", f"discovery {url}: {e}")
        # 配置里写不写末尾的 / 都行，但 discovery 自己报的 issuer 必须是同一个，后面验 iss 用它的原文
        if not isinstance(doc, dict) or str(doc.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise SsoError("failed", f"discovery issuer mismatch: {doc.get('issuer') if isinstance(doc, dict) else doc!r}")
        # 各个地址都得跟 issuer 同源：discovery 被篡改或写错时，不能把 client 密钥发到别处、
        # 也不能从别处拿验签公钥
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not isinstance(value, str) or origin_of(value) != origin_of(issuer):
                raise SsoError("failed", f"discovery {key} not on the issuer's origin: {value!r}")
        end_session = doc.get("end_session_endpoint")
        if end_session is not None and (not isinstance(end_session, str) or origin_of(end_session) != origin_of(issuer)):
            raise SsoError("failed", f"discovery end_session_endpoint not on the issuer's origin: {end_session!r}")
        with self._lock:
            self._discovery[issuer] = (time.time(), doc)
        return doc

    def signing_key(self, jwks_uri, token):
        with self._lock:
            client = self._jwks.get(jwks_uri)
            if client is None:
                client = self._jwks[jwks_uri] = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=DISCOVERY_TTL,
                                                                timeout=HTTP_TIMEOUT)
        try:
            return client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as e:
            raise SsoError("unavailable", f"jwks: {e}")
        except jwt.PyJWKClientError as e:
            raise SsoError("failed", f"jwks: {e}")


provider = Provider()


def begin(settings):
    """返回 (跳去 SSO 的地址, 要存进会话的 pending)。state / nonce / PKCE verifier 都在 pending 里。"""
    doc = provider.discovery(settings["issuer"])
    verifier = _b64url(secrets.token_bytes(32))
    pending = {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": verifier,
        "ts": time.time(),
    }
    params = {
        "response_type": "code",
        "client_id": settings["client_id"],
        "redirect_uri": settings["redirect_uri"],
        "scope": SCOPE,
        "state": pending["state"],
        "nonce": pending["nonce"],
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}", pending


def _role_from(roles):
    if f"{APP}:{ROLE_SUPERADMIN}" in roles:
        return ROLE_SUPERADMIN
    if f"{APP}:{ROLE_ADMIN}" in roles:
        return ROLE_ADMIN
    return None


def finish(settings, pending, args):
    """
    处理 SSO 回调。args 是回调的查询参数。成功返回 {sub, name, email, role, id_token}，
    其余情况抛 SsoError：
      cancelled —— SSO 那边报了错（用户拒绝、取消等）
      expired   —— 没有对应的 pending、state 对不上或者等太久了
      no_roles  —— ID token 里没有 roles 声明（SSO 的 hook 没生效），拒绝
      denied    —— 有 roles，但没有 Panopticon 的档位
      unavailable / failed —— 连不上 SSO / 其它校验失败
    """
    if not pending or not isinstance(pending, dict):
        raise SsoError("expired", "no pending login in session")
    if args.get("error"):
        raise SsoError("cancelled", f"{args.get('error')}: {args.get('error_description', '')}")
    state = args.get("state") or ""
    if not secrets.compare_digest(state.encode(), str(pending.get("state", "")).encode()):
        raise SsoError("expired", "state mismatch")
    if time.time() - float(pending.get("ts") or 0) > PENDING_MAX_AGE:
        raise SsoError("expired", "pending login too old")
    code = args.get("code")
    if not code:
        raise SsoError("failed", "callback without code")

    doc = provider.discovery(settings["issuer"])
    # client_secret_basic：按 RFC 6749 §2.3.1，id 和密钥先做表单编码再拼进 Basic
    basic = base64.b64encode(
        f"{quote(settings['client_id'], safe='')}:{quote(settings['client_secret'], safe='')}".encode()
    ).decode("ascii")
    try:
        resp = requests.post(doc["token_endpoint"], timeout=HTTP_TIMEOUT, headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
        }, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings["redirect_uri"],
            "code_verifier": pending.get("verifier", ""),
        })
    except requests.RequestException as e:
        raise SsoError("unavailable", f"token endpoint: {e}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        raise SsoError("failed", f"token endpoint HTTP {resp.status_code}: {body.get('error', '')}")
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str):
        raise SsoError("failed", "token response without id_token")

    claims = verify_id_token(settings, doc, id_token, pending.get("nonce", ""))

    roles = claims.get("roles")
    if not isinstance(roles, list):
        raise SsoError("no_roles", f"id_token for {claims.get('sub')} has no roles claim")
    role = _role_from(roles)
    if role is None:
        raise SsoError("denied", f"{claims.get('sub')} has no {APP} role")
    return {
        "sub": claims["sub"],
        "name": claims.get("name") if isinstance(claims.get("name"), str) else "",
        "email": claims.get("email") if isinstance(claims.get("email"), str) else "",
        "role": role,
        "id_token": id_token,
    }


def verify_id_token(settings, doc, id_token, nonce):
    try:
        alg = jwt.get_unverified_header(id_token).get("alg")
    except jwt.InvalidTokenError as e:
        raise SsoError("failed", f"id_token header: {e}")
    if alg not in ALLOWED_ALGS:
        raise SsoError("failed", f"id_token alg {alg!r} not allowed")
    key = provider.signing_key(doc["jwks_uri"], id_token)
    try:
        claims = jwt.decode(
            id_token, key, algorithms=[alg], audience=settings["client_id"], issuer=doc["issuer"],
            leeway=60, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as e:
        raise SsoError("failed", f"id_token: {e}")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
        raise SsoError("failed", "id_token nonce mismatch")
    aud = claims.get("aud")
    multi = isinstance(aud, list) and len(aud) > 1
    if (multi or "azp" in claims) and claims.get("azp") != settings["client_id"]:
        raise SsoError("failed", "id_token azp mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SsoError("failed", "id_token without sub")
    return claims


def logout_url(settings, id_token):
    """SSO 那边也退出的地址；拿不到（没配登出回调、SSO 连不上）就返回 None，只退本地。"""
    if not settings or not id_token or not settings.get("post_logout_redirect_uri"):
        return None
    try:
        endpoint = provider.discovery(settings["issuer"]).get("end_session_endpoint")
    except SsoError:
        return None
    if not isinstance(endpoint, str) or not endpoint:
        return None
    return f"{endpoint}?" + urlencode({
        "id_token_hint": id_token,
        "post_logout_redirect_uri": settings["post_logout_redirect_uri"],
    })
