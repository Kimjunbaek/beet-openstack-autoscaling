"""판단 결과를 알리고 실행을 시작하는 부분.

  VM3 추가/회수  → executor.py (EC2 단계 제어 → SSH → CP 단일 단계 실행)
  재배치         → 담당자 구현 대기 (홍석·준백)
  AWS 합류/회수  → 담당자 구현 대기 (경빈)
"""
import json
import logging
import urllib.request

import card
import config
from decide import Action, Decision

log = logging.getLogger("analyzer")

TITLES = {   # 실패 알림 등 텍스트용
    Action.RELOCATE: "재배치", Action.ADD_VM: "VM 추가", Action.ADD_AWS: "AWS 확장",
    Action.SATURATED: "사내 포화", Action.REMOVE_AWS: "AWS 회수", Action.REMOVE_VM: "VM3 회수",
}


def notify_discord(text: str = "", embed: dict = None) -> None:
    try:
        with open(config.DISCORD_WEBHOOK_FILE) as f:
            url = f.read().strip()
    except FileNotFoundError:
        log.warning("디스코드 웹후크 파일 없음 → 알림 생략")
        return
    req = urllib.request.Request(
        url,
        data=json.dumps({"username": "BEET에 몸을 맡겨라!",
                         **({"embeds": [embed]} if embed else {"content": text})}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "beet-analyzer"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # 알림 실패가 판단 루프를 멈추면 안 됨
        log.error("디스코드 전송 실패: %s", e)


# ── 실제 실행 ─────────────────────────────────────────
# VM3 증설·회수는 executor가 여러 주기에 걸쳐 진행 (생성 → 합류 확인 / 비우기 → 삭제)
# 재배치·AWS는 아직 담당자 구현 대기
def relocate(vm: str) -> None:
    raise NotImplementedError("과밀 VM 자원 제한 + 파드 이동 (홍석·준백)")


def add_aws() -> None:
    raise NotImplementedError("Lambda 호출 → ASG 희망 용량 +1 (경빈)")


def remove_aws() -> None:
    raise NotImplementedError("AWS 노드 drain → ASG 희망 용량 -1 (경빈·준백)")


def is_live(action: Action) -> bool:
    """이 판단을 실제로 실행하는지 (아니면 알림만).
    LIVE_ADD_VM=true: VM3 추가만 실제 실행, 나머지는 알림만 / DRY_RUN=true: 전부 알림만 / 시연 모드: 전부 알림만"""
    if config.DEMO_MODE or action == Action.SATURATED:
        return False
    if config.LIVE_WORKER_SCALING:
        return action in (Action.ADD_VM, Action.REMOVE_VM)
    if config.LIVE_ADD_VM:
        return action == Action.ADD_VM
    return not config.DRY_RUN


def execute(d: Decision, ex, now: float) -> bool:
    """판단 카드를 보내고 조치를 시작. 실제로 무언가 했으면 True (쿨다운 시작 기준).

    ex: executor.Executor — VM3 생성·회수 담당
    """
    if d.action in (Action.NONE, Action.NO_DATA):
        return False
    if d.action in (Action.ADD_VM, Action.REMOVE_VM) and ex.busy:
        log.info("VM3 작업 진행 중(%s) → %s 보류", ex.job.stage, d.action.value)
        return False

    embed = card.build(d)
    if d.action in (Action.ADD_VM, Action.REMOVE_VM):
        embed["footer"] = {"text": "EC2 판단 → CP 실행" if is_live(d.action) else "DRY-RUN · 실제 실행 안 함"}
    notify_discord(embed=embed)

    if d.action == Action.SATURATED:
        return False
    if not is_live(d.action):
        return True

    try:
        if d.action == Action.ADD_VM:
            ex.start_add(now, d.flavor, request=d.request)
        elif d.action == Action.REMOVE_VM:
            ex.start_remove(now, request=d.request)
        elif d.action == Action.RELOCATE:
            relocate(d.target)
        elif d.action == Action.ADD_AWS:
            add_aws()
        elif d.action == Action.REMOVE_AWS:
            remove_aws()
    except NotImplementedError as e:
        log.warning("아직 구현 안 됨: %s", e)
    except Exception as e:
        log.error("실행 실패 (%s): %s", d.action.value, e)
        notify_discord(f"⚠️ {TITLES[d.action]} 실행 실패: {e}")
    return True
