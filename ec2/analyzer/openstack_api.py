"""OpenStack API — VM3 만들기·지우기 (외부 라이브러리 없이 REST 직접 호출)

설정: secrets/openstack.json  (홍석님이 값 채움, Git에 안 올라감)
  {
    "auth_url": "http://<OpenStack 호스트>:5000/v3",
    "application_credential_id": "...",
    "application_credential_secret": "...",
    "compute_url": "",            # 비워두면 Keystone 카탈로그에서 찾음. 카탈로그 주소가 EC2에서 안 닿으면 직접 적기
    "image_id": "...",            # openstack image list
    "flavor_id": "...",           # openstack flavor list
    "network_id": "...",          # openstack network list
    "security_group": "default",  # 선택
    "key_name": ""                # 선택
  }
K3s 자동 합류용 cloud-init: secrets/vm3-user-data  (준백님이 채움)
"""
import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional

import config

VM3_NAME = "beet-k3s-w2"


class OpenStackError(RuntimeError):
    pass


def _load() -> dict:
    path = os.path.join(config.SECRETS_DIR, "openstack.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise OpenStackError(f"{path} 없음 — 홍석님께 OpenStack 접속 정보 받아서 채우기")


def _request(method: str, url: str, token: Optional[str] = None, body: Optional[dict] = None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.headers, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise OpenStackError(f"{method} {url} → {e.code} {detail}")
    except urllib.error.URLError as e:
        raise OpenStackError(f"{method} {url} 연결 실패: {e.reason}")


class Nova:
    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or _load()
        self.token, self.compute = self._auth()

    def _auth(self):
        c = self.cfg
        body = {"auth": {"identity": {
            "methods": ["application_credential"],
            "application_credential": {"id": c["application_credential_id"],
                                       "secret": c["application_credential_secret"]}}}}
        headers, data = _request("POST", c["auth_url"].rstrip("/") + "/auth/tokens", body=body)
        token = headers.get("X-Subject-Token")
        if not token:
            raise OpenStackError("Keystone 토큰을 못 받음")
        compute = c.get("compute_url")
        if not compute:
            for svc in data["token"].get("catalog", []):
                if svc["type"] == "compute":
                    eps = [e for e in svc["endpoints"] if e["interface"] == "public"] or svc["endpoints"]
                    compute = eps[0]["url"]
        if not compute:
            raise OpenStackError("카탈로그에 compute(Nova) 주소 없음 — openstack.json에 compute_url 적기")
        return token, compute.rstrip("/")

    def find(self, name: str = VM3_NAME) -> Optional[dict]:
        _, data = _request("GET", f"{self.compute}/servers?name=^{name}$", self.token)
        servers = data.get("servers", [])
        return servers[0] if servers else None

    def status(self, server_id: str) -> str:
        _, data = _request("GET", f"{self.compute}/servers/{server_id}", self.token)
        return data["server"]["status"]          # BUILD / ACTIVE / ERROR ...

    def create(self, name: str = VM3_NAME) -> str:
        c = self.cfg
        server = {"name": name, "imageRef": c["image_id"], "flavorRef": c["flavor_id"],
                  "networks": [{"uuid": c["network_id"]}]}
        if c.get("security_group"):
            server["security_groups"] = [{"name": c["security_group"]}]
        if c.get("key_name"):
            server["key_name"] = c["key_name"]
        ud = os.path.join(config.SECRETS_DIR, "vm3-user-data")
        if os.path.exists(ud):
            with open(ud, "rb") as f:
                server["user_data"] = base64.b64encode(f.read()).decode()
        _, data = _request("POST", f"{self.compute}/servers", self.token, {"server": server})
        return data["server"]["id"]

    def delete(self, server_id: str) -> None:
        _request("DELETE", f"{self.compute}/servers/{server_id}", self.token)
