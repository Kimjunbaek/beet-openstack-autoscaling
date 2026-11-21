"""K3s API — 회수할 노드 비우기(cordon → 파드 내보내기) · 노드 삭제 (외부 라이브러리 없이 REST 직접 호출)

설정: secrets/k3s.json  (준백님이 값 채움, Git에 안 올라감)
  {
    "api_url": "https://<K3s 서버>:6443",
    "token": "...",          # BEET 전용 서비스어카운트 토큰 (노드 cordon·삭제, 파드 eviction 권한)
    "ca_file": ""            # 비우면 인증서 검사 안 함 (사내 시연용)
  }
"""
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import List, Optional

import config


class K8sError(RuntimeError):
    pass


class K8s:
    def __init__(self, cfg: Optional[dict] = None):
        if cfg is None:
            path = os.path.join(config.SECRETS_DIR, "k3s.json")
            try:
                with open(path) as f:
                    cfg = json.load(f)
            except FileNotFoundError:
                raise K8sError(f"{path} 없음 — 준백님께 K3s API 주소·토큰 받아서 채우기")
        self.url = cfg["api_url"].rstrip("/")
        self.token = cfg["token"]
        if cfg.get("ca_file"):
            self.ctx = ssl.create_default_context(cafile=os.path.join(config.SECRETS_DIR, cfg["ca_file"]))
        else:
            self.ctx = ssl._create_unverified_context()

    def _req(self, method: str, path: str, body=None, ctype="application/json"):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise K8sError(f"{method} {path} → {e.code} {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            raise K8sError(f"{method} {path} 연결 실패: {e.reason}")

    def cordon(self, node: str) -> None:
        """새 파드가 이 노드로 안 오게 막음."""
        self._req("PATCH", f"/api/v1/nodes/{node}", {"spec": {"unschedulable": True}},
                  ctype="application/merge-patch+json")

    def movable_pods(self, node: str) -> List[dict]:
        """노드에서 옮겨야 하는 파드 (DaemonSet·static 파드는 제외 — 노드마다 하나씩 도는 것들)."""
        data = self._req("GET", f"/api/v1/pods?fieldSelector=spec.nodeName={node}") or {"items": []}
        out = []
        for p in data["items"]:
            owners = p["metadata"].get("ownerReferences", [])
            if any(o.get("kind") in ("DaemonSet", "Node") for o in owners):
                continue
            if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            out.append(p)
        return out

    def evict_all(self, node: str) -> int:
        """노드 위 파드를 내보냄 → 다른 노드에서 다시 뜸. 내보낸 수 반환."""
        pods = self.movable_pods(node)
        for p in pods:
            ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
            self._req("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction",
                      {"apiVersion": "policy/v1", "kind": "Eviction",
                       "metadata": {"name": name, "namespace": ns}})
        return len(pods)

    def delete_node(self, node: str) -> None:
        self._req("DELETE", f"/api/v1/nodes/{node}")
