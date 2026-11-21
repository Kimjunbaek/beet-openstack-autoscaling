# BEET 자동화 — EC2가 판단·사양·명령 순서를 결정하고 CP가 한 단계씩 실행

2026-11-20 수정본. 이전의 `scale` 전체 작업 위임 방식은 사용하지 않는다.

CP 및 EC2 반영 완료. EC2 실제 이미지에서 19개 테스트와 개별 명령 미리보기가 통과했고,
EC2가 `t3.medium`을 선택하는 읽기 전용 검증을 확인했다. 이 미리보기는 VM을 생성하지 않았다.
2026-11-20 10:25:16 UTC 운영 분석기는 `none — 정상 범위`를 기록했다.
CP 백업: `/home/ubuntu/beet-worker-automation/backups/ec2-orchestrator-20261120T101820Z`
EC2 백업: `/home/ubuntu/monitoring/backups/ec2-orchestrator-20261120T102455Z`

## 역할

| EC2 `/home/ubuntu/monitoring/analyzer` | CP `/home/ubuntu/beet-worker-automation` |
|---|---|
| Prometheus 관측·부하 예측 | 요청한 상태·자원 정보를 반환 |
| 확장·축소 여부와 대상 결정 | 요청 형식·소유권·최소 안전 조건 검사 |
| 실제 Flavor 선택 | 지정된 Flavor를 수락 또는 거절. 다른 Flavor를 선택하지 않음 |
| JSON 주문서 생성·각 단계 트리거 | 주문서에 명시된 작업 한 개 실행 |
| 결과 확인·다음 단계 선택 | 실행 결과만 반환. 다음 단계를 자동 실행하지 않음 |
| 재시도·복구·중단 결정 | 명령 중복 방지와 자원 식별자 검증 |

CP의 `capacity-controller.py`는 이름만 유지한 실행 보조 라이브러리다.
예측·Flavor 선택·확장/회수 정책·전체 작업 진행 제어 함수는 제거했다.
직접 실행하면 종료하며, `beet-capacity.timer`도 비활성 상태로 유지한다.

## EC2가 지정하는 확장 순서

`observe → reserve_port → create_vm → join → verify → lb_add → health → complete`

EC2는 `observe`로 받은 Nova Flavor·할당량과 노드 상태, 자신이 수집한 물리 호스트 지표를 비교한다.
예측된 부족 CPU·RAM을 계산해 적합한 작은 Flavor를 우선 선택하고 JSON 주문서의 `flavor`에 기록한다.
그 뒤 각 단계의 결과를 확인해야 다음 명령을 보낸다. w2가 먼저이며 이후 w3를 확장한다.

Ansible `provision.yml`은 EC2가 지정한 태그 `create_vm`, `join`, `verify`, `lb_add` 중 하나만 실행한다.
예를 들어 `join` 명령이 VM 생성이나 Octavia 등록을 이어서 수행하지 않는다.
다른 단계에서 사용하는 기존 포트·SSH inventory를 읽는 작업은 실행 준비이며 자원 생성 판단을 하지 않는다.

## EC2가 지정하는 회수 순서

`observe → lb_disable → cordon → drain → verify_relocated → lb_remove → delete_vm → delete_node → delete_port → health → complete`

CPU·RAM 예측 수요가 50% 미만으로 15분 유지되고 회수 후 잔여 점유율도 각각 70% 이하면
EC2가 w3 → w2 순서로 회수를 결정한다. EC2는 drain 후 반환된 현장 상태로 잔여 자원을 다시 판단한다.
처리 완료 후 10분 쿨다운을 적용하며, 노드 구성 변경이나 관측 중단 시 저부하 유지 시간을 초기화한다.

drain 등 삭제 이전 단계가 실패하면 EC2가 `uncordon`, 필요한 `lb_enable`, `health`, `complete` 복구 명령을 보낸다.
VM 삭제가 진행되는 단계나 복구 자체가 실패하면 자동으로 다음 작업을 강행하지 않고 중단 기록을 남긴다.
파드가 남았다는 이유로 강제 삭제하지 않는다. 생성 중 실패한 VM도 임의 삭제하지 않고 복구 확인 대상으로 남긴다.

## 주문서와 실행 제한

제한된 기존 SSH 키로 `step` 또는 읽기 전용 `step-preview`를 호출하고 표준 입력으로 JSON을 전달한다.
주문서는 버전, operation ID, command ID, 발송 시각, 단계, 대상 워커, 자원 식별자를 포함한다.
`create_vm`에는 EC2가 선택한 Flavor와 최신 물리 호스트 지표도 포함한다.

CP는 w2/w3 이외 대상, 120초를 넘긴 주문서, 변경된 VM/포트/노드 UID, 다른 작업의 중복 실행을 거절한다.
생성 전에는 지정 사양의 프로젝트 할당량과 물리 CPU·RAM·디스크 수용 여부를 재검사한다.
조건에 맞지 않아도 CP가 작은 사양으로 바꾸지 않는다. 거절 결과를 받은 EC2가 후속 처리를 결정한다.

물리 메모리는 실제 MemAvailable과 Nova 미사용 메모리 중 작은 값에서 새 VM을 빼도 2GiB를 남긴다.
vCPU는 과할당을 가정하지 않고 예비 1코어를 남긴다. 실제 CPU 사용률은 85% 미만이어야 한다.
디스크는 물리 루트 파일시스템과 Nova free disk 중 작은 여유에서 새 VM을 빼도 10GiB를 남긴다.
현재는 단일 호스트 `beet-host1` 대상이며 다중 호스트는 별도 Placement·저장 경로 정책이 필요하다.
CP의 Placement 조회는 403이므로 EC2 관측을 사용하며, Nova의 최종 배치 성공을 보장하는 검사는 아니다.

## 상태 저장

EC2는 Docker 영속 볼륨의 `/var/lib/beet-analyzer/worker-job.json`에 대상·선택 사양·현재 단계·명령 ID를 저장한다.
재시작 후 같은 명령 ID로 실행 결과를 재확인한다. 실제 변경 명령의 재시도는 EC2가 제어한다.
CP는 `worker-step-state.json`에 명령별 결과와 자원 식별자만 저장하며 다음 단계 계획을 저장하지 않는다.
공유 `provision.lock`은 CP에서 개별 명령 간 충돌을 막는다.

## 파일

| 위치 | 파일 | 기능 |
|---|---|---|
| EC2 | `prom.py`, `forecast.py`, `decide.py` | 관측·예측·확장/축소·Flavor 선택 |
| EC2 | `executor.py` | 전체 단계 순서, JSON 주문서, 결과 판단, 재시도·복구·영속 상태 |
| EC2 | `actions.py`, `config.py`, `main.py` | 실행 스위치·알림·반복 실행 연결 |
| CP | `worker-exec.py` | 명시된 작업 한 개만 실행 |
| CP | `capacity-controller.py` | 상태 조회·단위 변환·소유권·실행 안전 검사 보조. 기존 정책 코드는 제거 |
| CP | `analyzer-ssh-command.sh` | `check`, `step`, `step-preview` 명령 제한 |
| CP | `provision.yml`, `verify.yml` | 개별 태그 기반 VM/조인/검증/Octavia 작업 |
| CP | `console-host-key.py`, `octavia-member.py` | 신뢰 가능한 SSH 키 확인·멤버 등록 보조 |

## 검증

CP SDK 환경:

```bash
cd /home/ubuntu/beet-worker-automation
/home/ubuntu/.local/share/uv/tools/ansible-core/bin/python -m unittest test_capacity test_worker_exec -q
```

EC2 분석기 이미지:

```bash
sudo docker exec analyzer python -m unittest test_worker_roles test_ec2_orchestration -q
```

현재 대상 테스트는 CP 20개, EC2 19개다. 이전 CP 독립 정책 테스트는 `legacy-tests`에 기록으로 보관했다.
EC2 순서·사양 선택, 생성/회수 단계 분리, 실패 시 EC2 복구 결정, 동일 명령 재시도, 재시작 복원 등을 검사한다.
테스트 성공은 실제 부하를 가한 전체 VM 생성·회수 시험 성공을 뜻하지 않는다. 실제 자원 변경 통합시험은 별도다.

운영 스위치는 `LIVE_WORKER_SCALING=true`, `DRY_RUN=true`, `DEMO_MODE=false`다.
관리 워커 추가·회수만 실제 실행하며 AWS 확장은 비활성화한다.
인증 파일, 개인키, `.env`, 실제 운영 상태 파일은 소스 패키지에 포함하지 않는다.
