"""BEET에 몸을 맡겨라! — Beet팀 분석 프로그램. INTERVAL_SEC마다 지표를 읽고 → 예측하고 → 판단하고 → 실행한다."""
import logging
import time

import actions
import card
import config
import executor
import exporter
import forecast
import prom
from decide import Action, State, decide, is_repeat, remember_dry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analyzer")


def main() -> None:
    log.info("BEET에 몸을 맡겨라! 시작 — prometheus=%s, %d초 주기, dry_run=%s, 워커 증설·회수 실행=%s, aws=%s, 시연모드=%s",
             config.PROMETHEUS_URL, config.INTERVAL_SEC, config.DRY_RUN, config.LIVE_WORKER_SCALING,
             config.AWS_ENABLED, config.DEMO_MODE)
    exporter.start(config.METRICS_PORT)
    state = State()
    ex = executor.Executor(node_ready=prom.node_ready)
    state.last_action_at = ex.last_action_at
    last = None

    while True:
        now = time.time()
        try:
            forecasts = forecast.run()
            exporter.update(forecasts, now)
            # 진행 중인 VM3 작업(생성·회수)을 한 걸음 진행 → 끝났으면 결과 알림
            ev = ex.tick(now)
            if ev:
                state.last_action_at = now
                state.low_load_since = None
                log.info("실행 결과: %s (%.0f초) %s", ev.kind, ev.elapsed_sec, ev.detail)
                actions.notify_discord(embed=card.result(ev))
            exporter.set_lead_time(ex.last_lead_sec)

            metrics = prom.read_metrics(forecasts)
            d = decide(metrics, state, now)
            exporter.set_decision(d.action.value)
            # 같은 판단이 30초마다 반복돼도 실행·알림은 판단이 바뀔 때 한 번만
            # (증설·회수는 실행 후 쿨다운 동안 NONE이 나오므로, 쿨다운이 끝나고 조건이 그대로면 다시 실행됨)
            current = (d.action, d.target)   # 이유 문장(남은 분)은 매번 바뀌니까 비교에서 뺌
            acting = d.action not in (Action.NONE, Action.NO_DATA)
            if current != last and acting and not actions.is_live(d.action) and is_repeat(state, d, metrics.vm3_nodes):
                # 알림만 하는 모드에선 실제 상태가 안 바뀌니까 쿨다운이 끝나면 같은 판단이 또 나옴 → 다시 안 보냄
                log.info("판단: %s — 방금 알린 것과 같음 (VM3 %d대 그대로) → 알림 생략", d.action.value, metrics.vm3_nodes)
            elif current != last:
                log.info("판단: %s — %s", d.action.value, d.reason)
                if acting and actions.execute(d, ex, now):
                    state.last_action_at = now
                    exporter.count_action(d.action.value)
                    if actions.is_live(d.action):
                        state.last_dry = None          # 실제로 실행했으면 상태가 바뀔 테니 기억 지움
                    else:
                        remember_dry(state, d, metrics.vm3_nodes)
                    if config.DRY_RUN and not actions.is_live(d.action):
                        if d.action == Action.ADD_VM:
                            state.sim_vm3 = 1
                        elif d.action == Action.REMOVE_VM:
                            state.sim_vm3 = 0
            last = current
        except Exception as e:
            log.error("이번 주기 실패: %s", e)
        time.sleep(config.INTERVAL_SEC)


if __name__ == "__main__":
    main()
