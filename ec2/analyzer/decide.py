"""판단 로직 — 지표 값만 받아서 '무엇을 할지'를 정한다. (외부 연결 없음 → 테스트하기 쉬움)

판단 순서 (기획안 대응 판단 흐름)
  1단계  특정 VM 과밀          → 재배치 (증설해도 안 풀리니까)
  2단계  자원 소진 예측 + 사내 여유  → OpenStack VM 추가
         "자원 소진 예측" = 리드타임 역산: 가장 빠른 포화 시점 − 확장 리드타임 − 안전 여유 ≤ 0
  3단계  자원 소진 예측 + 사내 포화  → AWS 확장 (꺼져 있으면 알림만)
  회수   부하 감소가 오래 지속      → AWS부터, 그다음 VM3
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

import config
import saturation


class Action(str, Enum):
    NONE = "none"
    NO_DATA = "no_data"          # 지표가 없어서 판단 못 함 → 아무것도 안 함
    RELOCATE = "relocate"        # 1단계 재배치
    ADD_VM = "add_vm"            # 2단계 VM3 추가
    ADD_AWS = "add_aws"          # 3단계 AWS 노드 합류
    SATURATED = "saturated"      # 3단계인데 AWS 꺼져 있음 → 알림만
    REMOVE_AWS = "remove_aws"    # 회수 1순위
    REMOVE_VM = "remove_vm"      # 회수 2순위


ACTIONS_FOR_EXPORT = [a.value for a in Action]


@dataclass
class Metrics:
    """Prometheus에서 읽어온 값. 못 읽은 값은 None."""
    steal_by_vm: dict = field(default_factory=dict)   # {"beet-k3s-w1": 0.03, ...}
    requests_saturation: Optional[dict] = None        # 클러스터 예약률 85% 도달 시점 범위 (분, CPU·메모리 중 빠른 쪽)
    cpu_requests_now: Optional[float] = None          # 현재 CPU 예약률
    openstack_quota_used: Optional[float] = None      # OpenStack 할당량 사용률
    aws_nodes: int = 0
    vm3_nodes: int = 0
    host_cpu: Optional[float] = None                  # 물리 호스트 CPU 사용률 (알림 카드용)
    host_mem: Optional[float] = None                  # 물리 호스트 메모리 사용률 (알림 카드용)
    host_mem_avail_gb: Optional[float] = None         # 물리 호스트 실제 남은 메모리 (VM 사양 고를 때)
    capacity: Optional[dict] = None


@dataclass
class Decision:
    action: Action
    reason: str
    target: Optional[str] = None    # 재배치 대상 VM 등
    trend: Optional[list] = None    # 회수 알림용 하락 흐름 [최고점, 중간, 현재]
    metrics: Optional[Metrics] = None   # 알림 카드에 3계층 근거를 보여주려고 같이 넘김
    lead_min: Optional[float] = None    # 이번 조치의 리드타임
    flavor: Optional[str] = None        # VM3 추가할 때 고른 사양
    request: Optional[dict] = None


@dataclass
class State:
    """판단 사이에 기억해야 하는 것 (쿨다운, 회수 조건 유지 시간)."""
    last_action_at: float = 0.0
    low_load_since: Optional[float] = None
    sim_vm3: int = 0   # DRY-RUN에서 "VM3를 띄웠다고 치는" 수 → 회수 판단까지 시연 가능
    history: list = field(default_factory=list)   # [(시각, CPU 예약률)] — 회수 알림에 "어디서 내려왔는지" 보여주려고
    last_dry: Optional[tuple] = None   # 실행 안 하고 알림만 보낸 마지막 판단 (조치, 그때 VM3 수) → 같은 알림 반복 방지
    capacity_nodes: Optional[tuple] = None
    capacity_seen: float = 0


HISTORY_SEC = 2 * 3600


def choose_flavor(m: Metrics, resource: Optional[str]) -> tuple:
    """(flavor 이름 또는 None, 이유). 호스트에 안전선(HOST_MEM_RESERVE_GB)을 남기고 들어가는 것만 고름.

    메모리가 먼저 차는 상황이면 큰 사양부터, CPU면 작은 사양부터 (둘 다 vCPU는 2개라 작은 걸로 충분).
    호스트 여유를 못 읽으면 가장 작은 사양 (판단을 막지 않되 호스트 부담은 최소로).
    """
    order = sorted(config.FLAVORS, key=lambda f: f[1], reverse=(resource == "메모리"))
    avail = m.host_mem_avail_gb
    if avail is None:
        return None, "물리 호스트 메모리 지표 없음 — 증설 보류"
    for name, gb in order:
        if avail - gb >= config.HOST_MEM_RESERVE_GB:
            why = "메모리가 먼저 참" if resource == "메모리" else "CPU가 먼저 참"
            return name, (f"{name}({gb:.0f}GB) — {why}, 호스트 여유 {avail:.1f}GB → "
                          f"띄운 뒤 {avail - gb:.1f}GB 남음")
    return None, (f"호스트 여유 {avail:.1f}GB — 가장 작은 VM({min(f[1] for f in config.FLAVORS):.0f}GB)을 띄우면 "
                  f"안전선 {config.HOST_MEM_RESERVE_GB:.0f}GB 아래로 내려감")


def is_repeat(state: State, d: "Decision", vm3_nodes: int) -> bool:
    """실행 안 하는 모드(DRY_RUN 등)에서 방금 알린 판단과 같은지.

    실제로 VM을 안 지우니까 쿨다운이 끝나면 같은 회수 판단이 또 나옴 → 10분마다 같은 알림.
    VM3 수가 바뀌거나 다른 판단이 나오면 다시 알림.
    """
    return state.last_dry == (d.action, vm3_nodes)


def remember_dry(state: State, d: "Decision", vm3_nodes: int) -> None:
    state.last_dry = (d.action, vm3_nodes)


def falling_trend(history: list) -> Optional[list]:
    """기록 중 최고점 → 그 뒤 중간 시점 → 현재. 최고점이 현재보다 5%p 이상 높을 때만."""
    if len(history) < 3:
        return None
    i = max(range(len(history)), key=lambda k: history[k][1])
    peak, cur = history[i][1], history[-1][1]
    if peak - cur < 0.05 or i == len(history) - 1:
        return None
    # 중간 = 내려오는 길에서 최고점과 현재의 한가운데쯤 값
    mid = min((v for _, v in history[i:]), key=lambda v: abs(v - (peak + cur) / 2))
    if mid - cur < 0.03 or peak - mid < 0.03:
        return [peak, cur]
    return [peak, mid, cur]


def decide(m: Metrics, state: State, now: float) -> Decision:
    if m.capacity is not None:
        return capacity_decision(m, state, now)
    d = legacy_decide(m, state, now)
    if config.LIVE_WORKER_SCALING and d.request is None and d.action in (Action.ADD_VM, Action.REMOVE_VM):
        if d.action == Action.ADD_VM:
            return Decision(Action.SATURATED, f"용량 지표를 못 읽어 증설 보류 — {d.reason}", metrics=m)
        return Decision(Action.NONE, f"용량 지표를 못 읽어 회수 보류 — {d.reason}")
    return d


def legacy_decide(m: Metrics, state: State, now: float) -> Decision:
    """Retain non-worker decisions when optional capacity telemetry is unavailable."""
    # 지표가 하나도 없으면 판단하지 않음 (수집이 끊긴 걸 '부하 없음'으로 착각하지 않도록)
    if m.requests_saturation is None and m.cpu_requests_now is None and not m.steal_by_vm:
        return Decision(Action.NO_DATA, "Prometheus에서 판단에 필요한 지표를 못 읽음")

    m.vm3_nodes = max(m.vm3_nodes, state.sim_vm3)
    # VM3가 늘거나 줄었으면 "방금 알린 판단" 기억을 지움 → 같은 판단이라도 상황이 바뀐 거니까 다시 알림 (매 주기 확인)
    if state.last_dry and state.last_dry[1] != m.vm3_nodes:
        state.last_dry = None

    # 예약률 기록 (최근 2시간만)
    if m.cpu_requests_now is not None:
        state.history.append((now, m.cpu_requests_now))
        state.history = [(t, v) for t, v in state.history if t >= now - HISTORY_SEC]

    # 회수 조건이 얼마나 오래 이어졌는지 기록 (쿨다운 중에도 시간은 잼)
    low_load = m.cpu_requests_now is not None and m.cpu_requests_now < config.SCALE_IN_THRESHOLD
    if low_load:
        if state.low_load_since is None:
            state.low_load_since = now
    else:
        state.low_load_since = None

    # 1단계: 과밀 — 재배치는 쿨다운과 상관없이 먼저 판단 (증설로 해결 안 되는 문제라서)
    if m.steal_by_vm:
        vm, steal = max(m.steal_by_vm.items(), key=lambda kv: kv[1])
        if steal > config.STEAL_THRESHOLD:
            return Decision(Action.RELOCATE,
                            f"{vm} CPU steal {steal:.0%} — 물리 호스트 과밀, 증설 대신 재배치", target=vm, metrics=m)

    since = now - state.last_action_at
    in_cooldown = since < config.COOLDOWN_SEC
    # 증설은 방금 띄운 VM이 합류할 때까지(리드타임) 다음 증설을 안 함 — 아직 뜨는 중인데 "또 띄워" 방지
    in_scaleout_cooldown = since < max(config.COOLDOWN_SEC, config.LEAD_TIME_VM_MIN * 60)

    # 2·3단계: 자원 소진 예측 — 리드타임 역산
    sat = m.requests_saturation
    if sat is not None and sat.get("earliest") is not None:
        # 확장 대상을 먼저 정해야 리드타임이 정해짐 (VM3가 AWS보다 빠름)
        quota = m.openstack_quota_used
        # 할당량을 못 읽으면(None) 사내 자원부터 시도 — VM3가 없을 때만
        flavor, flavor_why = None, ""
        if (quota is None or quota < config.QUOTA_LIMIT) and m.vm3_nodes == 0:
            flavor, flavor_why = choose_flavor(m, sat.get("resource"))
        if flavor:
            q = "확인 불가" if quota is None else f"{quota:.0%}"
            action, lead, where = (Action.ADD_VM, config.LEAD_TIME_VM_MIN,
                                   f"OpenStack 여유(할당량 {q}) → VM3 추가 · {flavor_why}")
        elif flavor_why and config.AWS_ENABLED:     # 할당량은 있는데 호스트 메모리가 모자람
            action, lead, where = Action.ADD_AWS, config.LEAD_TIME_AWS_MIN, f"{flavor_why} → AWS 노드 합류"
        elif flavor_why:
            action, lead, where = Action.SATURATED, config.LEAD_TIME_AWS_MIN, f"{flavor_why} → 알림만"
        elif config.AWS_ENABLED:
            action, lead, where = Action.ADD_AWS, config.LEAD_TIME_AWS_MIN, "사내 포화 → AWS 노드 합류"
        else:
            action, lead, where = Action.SATURATED, config.LEAD_TIME_AWS_MIN, "사내 포화 (AWS 확장 꺼짐) → 알림만"

        slack = saturation.lead_time_slack(sat, lead, config.SAFETY_MIN)
        basis = (f"예약률 {config.SCALE_OUT_THRESHOLD:.0%} 도달 {saturation.describe(sat)} · "
                 f"리드타임 {lead:.0f}분 + 여유 {config.SAFETY_MIN:.0f}분")
        if slack <= 0:
            if in_scaleout_cooldown:
                return Decision(Action.NONE, f"지금 확장해야 하지만 쿨다운 중 — {basis}")
            return Decision(action, f"{basis} → 지금 시작해야 늦지 않음 · {where}",
                            metrics=m, lead_min=lead, flavor=flavor)
        # 아직 여유 있음: 대기 (판단 근거는 남겨둠)
        return Decision(Action.NONE, f"{slack:.0f}분 뒤까지는 확장을 시작하지 않아도 됨 — {basis}")

    # 회수: 낮은 부하가 충분히 오래 이어졌을 때만, AWS부터
    if low_load and now - state.low_load_since >= config.SCALE_IN_HOLD_SEC and not in_cooldown:
        held = int((now - state.low_load_since) // 60)
        if m.aws_nodes > 0:
            return Decision(Action.REMOVE_AWS, f"CPU 예약률 {m.cpu_requests_now:.0%} {held}분 지속 → AWS 노드부터 회수", metrics=m,
                            trend=falling_trend(state.history))
        if m.vm3_nodes > 0:
            return Decision(Action.REMOVE_VM, f"CPU 예약률 {m.cpu_requests_now:.0%} {held}분 지속 → VM3 회수", metrics=m,
                            trend=falling_trend(state.history))

    return Decision(Action.NONE, "정상 범위")


def capacity_decision(m, state, now):
    """EC2 chooses the operation; CP uses actual flavor metadata and performs safety admission."""
    c = m.capacity
    nodes, demand = c["nodes"], c["demand"]
    names = tuple(sorted(nodes))
    if names != state.capacity_nodes or now - state.capacity_seen > 90:
        state.low_load_since = None
        state.last_dry = None
    state.capacity_nodes, state.capacity_seen = names, now
    managed = [w for w in config.MANAGED_WORKERS if w in nodes]
    m.vm3_nodes = len(managed)
    if (not {"beet-k3s-cp", "beet-k3s-w1"}.issubset(nodes)
            or set(nodes) - {"beet-k3s-cp", "beet-k3s-w1", *config.MANAGED_WORKERS}
            or any(not n["ready"] for n in nodes.values())
            or not -10 <= now - c["observed_at"] <= 120):
        state.low_load_since = None
        return Decision(Action.NO_DATA, "노드 구성·Ready·지표 최신성 확인 필요", metrics=m)
    capacity = {r: sum(n[r] for n in nodes.values()) for r in ("cpu", "memory")}
    if min(capacity.values()) <= 0:
        return Decision(Action.NO_DATA, "노드 가용 자원 없음", metrics=m)
    if m.steal_by_vm:
        vm, steal = max(m.steal_by_vm.items(), key=lambda kv: kv[1])
        if steal > config.STEAL_THRESHOLD:
            state.low_load_since = None
            return Decision(Action.RELOCATE, f"{vm} CPU steal {steal:.0%} — 물리 호스트 과밀, 증설 대신 재배치",
                            target=vm, metrics=m)
    sat = m.requests_saturation
    urgent = bool(sat and sat.get("earliest") is not None
                  and saturation.lead_time_slack(sat, config.LEAD_TIME_VM_MIN, config.SAFETY_MIN) <= 0)
    pressure = any(n["used_memory"] / n["memory"] >= config.SCALE_OUT_THRESHOLD
                   for name, n in nodes.items() if name in config.MANAGED_WORKERS)
    grow = urgent or pressure or any(demand[r] / capacity[r] >= config.SCALE_OUT_THRESHOLD for r in capacity)
    cooldown = now - state.last_action_at < max(config.COOLDOWN_SEC, config.LEAD_TIME_VM_MIN * 60)
    action, target, reason = Action.NONE, None, "정상 범위"
    if grow:
        state.low_load_since = None
        quota = m.openstack_quota_used
        if cooldown:
            reason = "확장 필요 — 이전 작업 안정화 대기"
        elif len(managed) == len(config.MANAGED_WORKERS):
            action, reason = Action.SATURATED, "w2·w3 확장 한도 도달"
        elif quota is None or quota >= config.QUOTA_LIMIT:
            action, reason = Action.SATURATED, "프로젝트 할당량 확인 불가 또는 부족"
        elif c["host"]["cpu_used_ratio"] >= .85 or c["host"]["free_memory"] < 6 * 1024**3:
            action, reason = Action.SATURATED, "물리 호스트 CPU·메모리 안전 여유 부족"
        else:
            target = next(w for w in config.MANAGED_WORKERS if w not in nodes)
            action, reason = Action.ADD_VM, "예측 자원 수요 또는 워커 RAM 압박 → EC2 사양 선택 후 단계별 실행 요청"
    else:
        low = all(demand[r] / capacity[r] < config.SCALE_IN_THRESHOLD for r in capacity)
        if not low:
            state.low_load_since = None
        elif state.low_load_since is None:
            state.low_load_since = now
        if (low and managed and not cooldown and state.low_load_since is not None
                and now - state.low_load_since >= config.SCALE_IN_HOLD_SEC):
            target = managed[-1]
            if all(demand[r] <= (capacity[r] - nodes[target][r]) * .70 for r in capacity):
                action, reason = Action.REMOVE_VM, "CPU·RAM 안정화 유지 및 잔여 자원 확인 → 마지막 워커부터 회수"
    request = None
    if action in (Action.ADD_VM, Action.REMOVE_VM):
        request = {"version": 1, "request_id": uuid.uuid4().hex, "observed_at": c["observed_at"],
                   "action": "add" if action == Action.ADD_VM else "remove", "worker": target,
                   "demand": demand, "host": c["host"]}
    return Decision(action, reason, target=target, metrics=m, request=request,
                    lead_min=config.LEAD_TIME_VM_MIN if action == Action.ADD_VM else None)


def select_worker_flavor(observation, request):
    """EC2 selects the exact flavor. CP supplies raw inventory and may only accept/reject it."""
    c = observation["constraints"]
    s = observation["snapshot"]
    demand = {r: max(s["demand"][r], request["demand"][r]) for r in ("cpu", "memory")}
    need = {r: max(0, demand[r] / config.WORKER_TARGET_UTILIZATION - sum(n[r] for n in s["nodes"].values()),
                   s["largest_pending"][r]) for r in demand}
    limits, host = observation["limits"], request["host"]
    candidates = []
    for f in observation["flavors"]:
        if f["name"] not in c["allowed_flavors"] or f["disk"] < c["minimum_disk_gb"]:
            continue
        if any(limits[f"max_{r}"] >= 0 and limits[f"used_{r}"] + amount > limits[f"max_{r}"]
               for r, amount in (("cores", f["vcpus"]), ("ram", f["ram"]), ("instances", 1))):
            continue
        if (host["name"] != c["physical_host"] or host["cpu_used_ratio"] >= c["host_cpu_limit"]
                or f["vcpus"] + c["host_cpu_reserve"] > host["free_vcpus"]
                or f["ram"] * 1024**2 + c["host_memory_reserve"] > host["free_memory"]
                or f["disk"] * 1024**3 + c["host_disk_reserve"] > host["free_disk"]):
            continue
        candidates.append(f)
    if not candidates:
        raise ValueError("No flavor fits the physical host, project quota and image")
    candidates.sort(key=lambda f: (f["ram"], f["vcpus"], f["disk"], f["name"]))
    for f in candidates:
        if (f["vcpus"] - c["node_cpu_reserve"] >= need["cpu"]
                and f["ram"] * 1024**2 - c["node_memory_reserve"] >= need["memory"]):
            return f["name"]
    return max(candidates, key=lambda f: (
        min((f["vcpus"] - c["node_cpu_reserve"]) / max(need["cpu"], .001),
            (f["ram"] * 1024**2 - c["node_memory_reserve"]) / max(need["memory"], 1)),
        f["ram"], f["vcpus"]))["name"]
