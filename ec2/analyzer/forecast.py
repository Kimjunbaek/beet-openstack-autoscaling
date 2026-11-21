"""예측 모듈 — Prometheus의 원본 지표를 읽어서 프로그램이 직접 예측한다.

방식: 스파이크 필터(Hampel) + 급증(계단) 필터 + 직선 예측(최소제곱) + 예측 범위(95%)
     클러스터 예약률은 "포화 시점 범위"(가장 빠름·예상·가장 늦음, 90%)도 계산 → 리드타임 역산에 사용
  - 1분마다의 변화량을 보고, 평소보다 유난히 큰 한 번의 변화는 '계단'으로 보고 추세 계산에서 뺀다.
  - 이미지 설치·백업처럼 한 번 쓰고 끝난 변화에 예측이 쭉 떨어지는 문제(predict_linear의 약점)를 막음.
  - 트래픽·클러스터 예약률은 계단 자체가 진짜 증가라서 필터를 끈다.
  - 예측값 하나만이 아니라 "이 정도 범위 안일 것"(하한~상한)도 같이 낸다.
    데이터가 들쭉날쭉하면 범위가 넓어지고, 꾸준하면 좁아진다.
  - 보는 기간은 예측 거리의 절반 이상 (너무 짧게 보고 멀리 예측하면 작은 기울기도 크게 부풀려짐).
"""
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

import config
import saturation


@dataclass
class Target:
    name: str            # 내보낼 지표의 target 라벨 값
    query: str           # 원본 지표 PromQL
    window_sec: int      # 추세를 볼 기간
    horizon_sec: int     # 얼마나 뒤를 예측할지
    destep: bool         # 급증(계단) 필터 사용 여부
    despike: bool = True                   # 스파이크(Hampel) 필터 사용 여부
    saturate_at: Optional[float] = None    # 값이 이 기준에 닿는 시점(포화 시점 범위)도 계산


# 예측 대상 (기준값은 rules/ 의 알림 조건과 맞춤)
SAT_WINDOW_SEC = (5 if config.DEMO_MODE else 40) * 60   # 예약률 추세를 보는 긴 창 (시연 5분 / 운영 40분)

TARGETS: List[Target] = [
    # Absolute requests keep VM sizing consistent when the number of nodes changes.
    Target("cluster_cpu_requested_cores", 'sum(kube_pod_container_resource_requests{resource="cpu"})',
           window_sec=SAT_WINDOW_SEC, horizon_sec=int((config.LEAD_TIME_VM_MIN + config.SAFETY_MIN) * 60), destep=False),
    Target("cluster_mem_requested_bytes", 'sum(kube_pod_container_resource_requests{resource="memory"})',
           window_sec=SAT_WINDOW_SEC, horizon_sec=int((config.LEAD_TIME_VM_MIN + config.SAFETY_MIN) * 60), destep=False),
    Target("disk_avail_bytes",
           'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|nsfs"}',
           window_sec=2 * 3600, horizon_sec=4 * 3600, destep=True),     # 2시간 보고 4시간 뒤
    Target("memory_avail_bytes", "node_memory_MemAvailable_bytes",
           window_sec=30 * 60, horizon_sec=3600, destep=True),          # 30분 보고 1시간 뒤
    # 클러스터 예약률: 운영 40분치 / 시연 5분치로 포화 시점 범위 계산 → 리드타임 역산
    Target("cluster_cpu_requests_ratio", "beet:cluster_cpu_requests:ratio",
           window_sec=SAT_WINDOW_SEC, horizon_sec=600, destep=False, saturate_at=config.SCALE_OUT_THRESHOLD),
    Target("cluster_mem_requests_ratio", "beet:cluster_mem_requests:ratio",
           window_sec=SAT_WINDOW_SEC, horizon_sec=600, destep=False, saturate_at=config.SCALE_OUT_THRESHOLD),
    # 트래픽 급증은 진짜 신호라서 필터 둘 다 끔
    Target("nextcloud_rps", "beet:nextcloud_rps",
           window_sec=10 * 60, horizon_sec=300, destep=False, despike=False),
]

STEP_SEC = 10 if config.DEMO_MODE else 30         # 원본 지표를 몇 초 간격으로 읽을지
MIN_POINTS = 6         # 이보다 적으면 예측 안 함 (데이터 부족)
STEP_K = 5.0           # 변화량이 평소(중앙값)에서 이 배수 이상 벗어나면 계단
STEP_MIN_REL = 0.01    # 그리고 현재 값의 1% 이상 변해야 계단 (잡음 무시)
MIN_SPAN_RATIO = 0.5   # 쌓인 데이터가 보는 기간의 절반도 안 되면 예측 안 함 (막 시작했을 때 과한 예측 방지)
Z_95 = 1.96            # 95% 범위
SAT_SHORT_SEC = (2 if config.DEMO_MODE else 10) * 60  # 포화 시점은 긴 창(전체)과 짧은 창(최근 10분) 둘 다 보고 더 빠른 쪽을 씀
                         # → 천천히 오르는 추세는 긴 창이, 갑자기 몰리는 부하는 짧은 창이 잡음


@dataclass
class Forecast:
    target: str
    labels: dict
    current: float
    predicted: float
    slope_per_sec: float
    horizon_sec: int
    lower: float = 0.0          # 예측 범위 하한 (95%)
    upper: float = 0.0          # 예측 범위 상한 (95%)
    steps_removed: int = 0
    spikes_removed: int = 0
    saturation: Optional[dict] = None   # 포화 시점 범위 (분): earliest / expected / latest


# ── 계산 (외부 연결 없음 → 테스트하기 쉬움) ─────────────────
def remove_steps(values) -> (np.ndarray, int):
    """급증(계단)을 빼서 이어붙인 값과, 뺀 계단 수를 돌려준다."""
    v = np.asarray(values, dtype=float)
    if len(v) < 3:
        return v, 0
    diffs = np.diff(v)                              # 이웃한 점끼리의 변화량
    med = np.median(diffs)                          # 평소 변화량
    mad = np.median(np.abs(diffs - med))            # 평소 흔들림 폭
    scale = max(abs(v[-1]), 1e-9)
    is_step = (np.abs(diffs - med) > STEP_K * max(mad, 1e-12)) & (np.abs(diffs) > STEP_MIN_REL * scale)
    # 계단 크기(평소 변화량은 추세로 남겨둠)를 그 이후 점들에서 누적해서 빼기
    jumps = np.where(is_step, diffs - med, 0.0)
    shift = np.concatenate([[0.0], np.cumsum(jumps)])
    return v - shift, int(is_step.sum())


def linear_slope(ts, vs) -> float:
    """최소제곱 직선의 기울기 (단위: 1초당 변화량)."""
    t = np.asarray(ts, dtype=float)
    if np.ptp(t) == 0:
        return 0.0
    slope, _ = np.polyfit(t - t[0], np.asarray(vs, dtype=float), 1)   # 시간 기준을 0부터로 맞춰서 계산 오차 방지
    return float(slope)


def prediction_halfwidth(ts, vs, slope: float, horizon_sec: int) -> float:
    """예측 범위의 반폭 (예측값 ± 이 값이 95% 범위).

    직선에서 점들이 얼마나 흩어져 있는지(잔차)를 보고 계산한다.
    - 점들이 직선에 딱 붙어 있으면 범위가 좁고, 들쭉날쭉하면 넓다.
    - 멀리 예측할수록 넓어진다.
    """
    t = np.asarray(ts, dtype=float) - ts[0]
    v = np.asarray(vs, dtype=float)
    n = len(t)
    intercept = v.mean() - slope * t.mean()
    resid = v - (intercept + slope * t)
    s = np.sqrt((resid ** 2).sum() / (n - 2))          # 직선에서 흩어진 정도
    sxx = ((t - t.mean()) ** 2).sum()
    t_future = t[-1] + horizon_sec
    return float(Z_95 * s * np.sqrt(1 + 1 / n + (t_future - t.mean()) ** 2 / sxx))


def forecast_series(ts: List[float], vs: List[float], horizon_sec: int,
                    destep: bool, window_sec: Optional[int] = None,
                    despike: bool = False, saturate_at: Optional[float] = None) -> Optional[dict]:
    """현재값·예측값·범위·기울기·뺀 계단/스파이크 수·포화 시점 범위. 데이터가 부족하면 None."""
    if len(vs) < MIN_POINTS:
        return None
    if window_sec and (ts[-1] - ts[0]) < MIN_SPAN_RATIO * window_sec:
        return None
    clean, spikes = saturation.hampel(vs) if despike else (np.asarray(vs, dtype=float), [])
    fit_vs, removed = remove_steps(clean) if destep else (clean, 0)
    slope = linear_slope(ts, fit_vs)
    current = float(vs[-1])
    predicted = current + slope * horizon_sec
    half = prediction_halfwidth(ts, fit_vs, slope, horizon_sec)
    sat = None
    if saturate_at is not None:
        t_all = np.asarray(ts, dtype=float)
        candidates = []
        recent = t_all >= t_all[-1] - SAT_SHORT_SEC
        # 최근 구간이 내려가는 중이면 포화로 안 봄 (긴 창에 남은 상승 추세 때문에 생기는 오탐 방지)
        if recent.sum() >= MIN_POINTS and linear_slope(list(t_all[recent]), list(np.asarray(fit_vs)[recent])) < 0:
            candidates = None
        for basis, mask in (("긴 창", np.ones(len(t_all), bool)),
                            (f"최근 {SAT_SHORT_SEC // 60}분", t_all >= t_all[-1] - SAT_SHORT_SEC)):
            if candidates is None or mask.sum() < MIN_POINTS:
                continue
            t_min = (t_all[mask] - t_all[mask][0]) / 60.0
            s = saturation.time_to_threshold(t_min, np.asarray(fit_vs)[mask], saturate_at,
                                             confidence=config.SAT_CONFIDENCE,
                                             anchor=float(np.asarray(fit_vs)[-1]))
            if s and s["earliest"] is not None:
                candidates.append(dict(s, basis=basis))
        if candidates:
            sat = min(candidates, key=lambda c: c["earliest"])
    return {"current": current, "predicted": predicted, "lower": predicted - half,
            "upper": predicted + half, "slope": slope, "steps_removed": removed,
            "spikes_removed": len(spikes), "saturation": sat}


# ── Prometheus 조회 ─────────────────────────────────────────
def _query_range(expr: str, window_sec: int) -> list:
    end = time.time()
    params = urllib.parse.urlencode({
        "query": expr, "start": end - window_sec, "end": end, "step": STEP_SEC})
    url = f"{config.PROMETHEUS_URL}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(f"query_range 실패: {expr}")
    return body["data"]["result"]


def run() -> List[Forecast]:
    results: List[Forecast] = []
    for tg in TARGETS:
        for series in _query_range(tg.query, tg.window_sec):
            ts = [float(t) for t, _ in series["values"]]
            vs = [float(v) for _, v in series["values"]]
            r = forecast_series(ts, vs, tg.horizon_sec, tg.destep, tg.window_sec,
                                tg.despike, tg.saturate_at)
            if r is None:
                continue
            labels = {k: v for k, v in series["metric"].items() if k != "__name__"}
            results.append(Forecast(tg.name, labels, r["current"], r["predicted"], r["slope"],
                                    tg.horizon_sec, r["lower"], r["upper"], r["steps_removed"],
                                    r["spikes_removed"], r["saturation"]))
    return results


def get(results: List[Forecast], target: str) -> Optional[Forecast]:
    """라벨 없는 단일 값 대상(클러스터 예약률 등)을 꺼낼 때 사용."""
    return next((f for f in results if f.target == target), None)
