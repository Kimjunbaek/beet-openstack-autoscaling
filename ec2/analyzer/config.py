"""분석 프로그램 설정 — 기준값은 prometheus/rules/scaling.yml 과 같게 유지할 것."""
import os


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


# ── 연결 ──────────────────────────────────────────────
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
DISCORD_WEBHOOK_FILE = os.getenv("DISCORD_WEBHOOK_FILE", "/run/secrets/discord_webhook")
SECRETS_DIR = os.getenv("SECRETS_DIR", "/run/secrets")   # openstack.json · k3s.json · vm3-user-data

METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))   # 예측값을 내보내는 /metrics 포트

# VM3 증설은 SSH로 cp에 요청 → cp의 Ansible이 실행 (openstack/worker-automation)
CP_SSH_TARGET = os.getenv("CP_SSH_TARGET", "ubuntu@192.0.2.40")
CP_SSH_KEY = os.getenv("CP_SSH_KEY", "/run/secrets/beet-worker-ssh")
CP_SSH_KNOWN_HOSTS = os.getenv("CP_SSH_KNOWN_HOSTS", "/run/secrets/beet-cp-known-hosts")

# ── 시연 모드 ─────────────────────────────────────────
# 발표 시연용: 같은 계산을 시간만 짧게 돌려서 부하를 걸면 1~2분 안에 알림이 오게 함.
# 실제 운영은 DEMO_MODE=false (아래 기본값들) — 계산 방식은 똑같고 "보는 기간·기다리는 시간"만 다름.
DEMO_MODE = _env_bool("DEMO_MODE", False)

# ── 동작 방식 ─────────────────────────────────────────
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10" if DEMO_MODE else "30"))  # 몇 초마다 판단할지
DRY_RUN = _env_bool("DRY_RUN", True)                    # True면 판단만 하고 실제 증설은 안 함
LIVE_ADD_VM = _env_bool("LIVE_ADD_VM", False) and not DEMO_MODE
LIVE_WORKER_SCALING = _env_bool("LIVE_WORKER_SCALING", False) and not DEMO_MODE
MANAGED_WORKERS = ("beet-k3s-w2", "beet-k3s-w3")
PHYSICAL_HOST = "beet-host1"
WORKER_JOB_FILE = os.getenv("WORKER_JOB_FILE", "/var/lib/beet-analyzer/worker-job.json")
WORKER_CONNECTION_DRAIN_SEC = int(os.getenv("WORKER_CONNECTION_DRAIN_SEC", "20"))
WORKER_TARGET_UTILIZATION = .75
AWS_ENABLED = _env_bool("AWS_ENABLED", False)           # AWS 확장은 선택 범위 → 기본 꺼둠

# ── 기준값 (scaling.yml 과 동일) ──────────────────────
STEAL_THRESHOLD = 0.10          # 1단계: VM CPU steal 10% 넘으면 과밀 → 재배치
SCALE_OUT_THRESHOLD = 0.85      # 2·3단계: 클러스터 예약률이 85%에 닿는 시점을 "포화"로 봄

# ── 리드타임 역산 (가장 빠른 포화 시점 − 리드타임 − 안전 여유 ≤ 0 이면 지금 확장 시작) ──
LEAD_TIME_VM_MIN = float(os.getenv("LEAD_TIME_VM_MIN", "6"))     # VM3 생성 → K3s 합류 (가정값, 실측 후 수정)
LEAD_TIME_AWS_MIN = float(os.getenv("LEAD_TIME_AWS_MIN", "10"))  # EC2 기동 → K3s 합류 (가정값, 실측 후 수정)
SAFETY_MIN = 5.0                # 안전 여유 (분) — 리드타임·여유는 시연에서도 실제값 그대로
SAT_CONFIDENCE = 0.90           # 포화 시점 범위의 신뢰수준 (추세선 기준)
QUOTA_LIMIT = 0.80              # OpenStack 할당량 80% 미만이면 "사내 여유 있음"
SCALE_IN_THRESHOLD = 0.50       # 회수: 현재 CPU 예약률 50% 미만이
SCALE_IN_HOLD_SEC = (2 if DEMO_MODE else 15) * 60   # 15분 이상 이어지면 회수 (축소는 천천히)
COOLDOWN_SEC = (2 if DEMO_MODE else 10) * 60          # 한 번 조치한 뒤 다음 증설·회수까지 기다리는 시간

# ── VM3 사양(flavor) 선택 ─────────────────────────────
# 작은 것부터. 메모리가 먼저 차는 상황이면 큰 것을 먼저 시도. cp의 analyzer-ssh-command.sh 허용 목록과 같게.
FLAVORS = [("t3.medium", 4.0), ("t3.medium-6g", 6.0)]   # (이름, 메모리 GB)
HOST_MEM_RESERVE_GB = 2.0       # VM을 띄운 뒤에도 물리 호스트에 이만큼은 남아야 함 (11/13 호스트 장애 재발 방지)
