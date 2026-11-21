"""포화 시점 예측 코어 — ①노이즈 판별 ②포화 시점 범위 ③리드타임 역산

입력: (시각[분], 값) 시계열. Prometheus query_range 결과를 분 단위로 바꿔서 넣는다.
출력: 포화 예상 시점 범위(가장 빠름·예상·가장 늦음) + "지금 확장을 시작해야 하는지" 판단

외부 연결이 없는 순수 계산이라 테스트하기 쉽다.
"""
from typing import Optional

import numpy as np
from scipy import stats


# ---------------------------------------------------------------
# ① 노이즈 판별 — Hampel 필터
#   주변 값들의 중앙값에서 너무 멀리 튄 점을 "일시적 스파이크"로 보고 중앙값으로 대체
#   (한 번 뛰고 그대로 머무는 '계단'은 forecast.remove_steps가 따로 처리)
# ---------------------------------------------------------------
def hampel(values, window: int = 5, n_sigma: float = 3.0):
    v = np.asarray(values, dtype=float)
    cleaned = v.copy()
    spikes = []
    k = 1.4826  # 중앙값 절대편차(MAD)를 표준편차 단위로 바꾸는 상수
    for i in range(len(v)):
        lo, hi = max(0, i - window), min(len(v), i + window + 1)
        med = np.median(v[lo:hi])
        mad = k * np.median(np.abs(v[lo:hi] - med))
        if mad > 0 and abs(v[i] - med) > n_sigma * mad:
            cleaned[i] = med
            spikes.append(i)
    return cleaned, spikes


# ---------------------------------------------------------------
# ② 포화 시점 + 신뢰구간 — 선형회귀
#   추세선 위아래로 "이 정도 안에 들어올 것" 띠를 그리고,
#   띠의 위쪽 선이 임계값에 닿는 시점 = 가장 빠른 포화,
#   추세선이 닿는 시점 = 예상 포화, 띠의 아래쪽 선이 닿는 시점 = 가장 늦은 포화
# ---------------------------------------------------------------
def time_to_threshold(t_min, y, threshold: float, confidence: float = 0.90,
                      horizon: float = 240, step: float = 0.5, mode: str = "trend",
                      anchor: Optional[float] = None) -> Optional[dict]:
    """anchor: 현재값. 주면 추세선을 현재값에서 출발시킴 (회귀선은 창 전체의 평균이라
    급하게 오르내린 뒤엔 현재값과 크게 어긋남 → 내려가는 중인데 '이미 포화'로 보는 오류 방지)"""
    t, y = np.asarray(t_min, float), np.asarray(y, float)
    n = len(t)
    if n < 3 or np.ptp(t) == 0:
        return None
    slope, intercept, *_ = stats.linregress(t, y)
    if slope <= 0:
        return None  # 줄거나 그대로면 포화 안 함 (이미 넘은 경우는 임계치 알림이 담당)

    resid = y - (slope * t + intercept)
    s = np.sqrt(np.sum(resid ** 2) / (n - 2))           # 잔차 표준편차 (흔들림 정도)
    t_mean, sxx = t.mean(), np.sum((t - t.mean()) ** 2)
    tcrit = stats.t.ppf(0.5 + confidence / 2, n - 2)

    now = t[-1]
    future = np.arange(now, now + horizon, step)
    start = anchor if anchor is not None else slope * now + intercept
    pred = start + slope * (future - now)
    # mode="trend": 추세선 자체의 불확실성만 봄 (스파이크는 ①에서 이미 걸렀으므로)
    # mode="point": 개별 측정값의 흔들림까지 포함 (더 넓음)
    extra = 1.0 if mode == "point" else 0.0
    margin = tcrit * s * np.sqrt(extra + 1 / n + (future - t_mean) ** 2 / sxx)

    def first_cross(curve):
        idx = np.where(curve >= threshold)[0]
        return float(future[idx[0]] - now) if len(idx) else None

    return {
        "earliest": first_cross(pred + margin),
        "expected": first_cross(pred),
        "latest": first_cross(pred - margin),
        "slope_per_min": float(slope),
        "noise": float(s),
    }


# ---------------------------------------------------------------
# ③ 리드타임 역산 — "지금 시작해야 늦지 않는가?"
#   가장 빠른 포화 시점(보수적 기준)에서 확장 소요 시간 + 안전 여유를 빼서 판단
# ---------------------------------------------------------------
def lead_time_slack(sat: Optional[dict], lead_time: float, safety: float = 5.0) -> Optional[float]:
    """확장을 미뤄도 되는 시간(분). 0 이하면 지금 시작해야 함. 포화 추세가 없으면 None."""
    if sat is None or sat.get("earliest") is None:
        return None
    return sat["earliest"] - lead_time - safety


def describe(sat: dict) -> str:
    """디스코드·로그용: '가장 빠르면 12분 · 예상 18분 · 늦으면 25분'"""
    def fmt(x):
        return f"{x:.0f}분" if x is not None else "4시간 넘게"
    basis = f" ({sat['basis']} 기준)" if sat.get("basis") else ""
    return f"가장 빠르면 {fmt(sat['earliest'])} · 예상 {fmt(sat['expected'])} · 늦으면 {fmt(sat['latest'])}{basis}"
