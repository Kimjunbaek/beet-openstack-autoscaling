# 검증 기록과 범위

## 기존 실행 기록

초기 SSH 키 지문 불일치와 authorized_keys 등록 오류를 수정하고 공개키 일치를 확인했다. 워커에서 `k3s token create`를 실행한 최초 시도는 `k3s: command not found`였으며, 토큰 생성 주체를 제어 노드로 구분했다. 빈 `K3S_TOKEN`으로 설치한 시도는 설치 스크립트가 거절했다. 이후 노드 조회에 cp/w1/w2가 모두 Ready로 표시되어 조인 성공을 확인했다.

busybox 테스트 파드를 w2에 고정 배치한 뒤 `Completed`와 `kubernetes.default.svc.cluster.local → 10.43.0.1` 응답이 기록됐다. 테스트 파드 완료가 장기 워크로드 Ready를 의미하지는 않는다.

Ansible w3 검증 결과는 `unreachable=0`, `failed=0`이었다. 당시 `Event(kind='add_done', elapsed_sec=12.001...)`는 기존 VM 재사용 여부까지 구분된 신규 생성 소요 시간으로 보지 않는다.

2026-11-18 15:27:18 판단 add_vm → 15:29:20 add_done(122초), 신규 워커 IP의 Octavia 멤버 ACTIVE/ONLINE 및 VIP HTTP 200 메시지가 기록됐다. 이후 사용자가 Nextcloud 접속 화면을 제시했다. 이 기록은 후속 11월20일 역할 분리 구조의 전체 실행 검증과 구분한다.

2026-11-20 기록에서는 CP 20개·EC2 19개 테스트, 실제 EC2 이미지 실행, EC2 Flavor 선택과 명령 미리보기를 확인했다. 미리보기는 VM 생성이 아니다.

## 공개본 재검증 — 2026-11-21

EC2의 `test_worker_roles.py`와 `test_ec2_orchestration.py` 19개가 통과했다. SSH와 리소스 변경은 모의 객체를 사용했다. 명령 순서, 상태 보존·복구, 정책 조건을 확인하며 실제 Nova 생성·VM SSH·drain 성공을 대체하지 않는다.

최초 Windows CP 테스트는 Linux 전용 `fcntl`과 OpenStack SDK 미설치로 import 단계에서 실패했다. 작업용 격리 폴더에 SDK를 설치하고 Windows 보조 실행기에서 `fcntl`만 대체한 뒤 CP 로직 테스트 20개 통과를 확인했다. 운영 코드는 변경하지 않았으며 SDK 자원·명령·저장 동작은 기존 테스트의 모의 객체를 사용했다.

명령 유효성·단계별 실행 제한·중복 요청·자원 식별자·삭제 전제 검사에는 의미가 있지만 Linux `flock`, 실제 OpenStack 권한, Ansible·kubectl·SSH 실행의 검증은 아니다. 이 보조 실행기는 공개 정리 과정에서 추가한 도구이며 과거 Linux 20개 테스트 기록과 따로 구분한다.

## GitHub Actions CI — 2026-11-21 추가

GitHub Ubuntu / Python 3.12에서 CP 20개와 EC2 19개 회귀 테스트를 실행하고 모두 통과했다. Windows 보조 실행기를 사용하지 않고 Linux에서 기존 CP 테스트를 실행한 결과다. 다만 테스트 내부의 자원·명령 모의 객체는 그대로 사용하므로 실제 VM 생성·네트워크·잠금 경합 검증은 아니다.

수동 실패 입력으로 CP의 지정한 교육용 단계를 실패시켰을 때 후속 소스 패키징이 건너뛰어지는 것도 확인했다. 코드의 결함을 발견한 것과 구분한다. 실행 링크와 구성 설명은 [CI.md](CI.md)에 기록했다. 운영 배포 CD는 구현 범위에 포함하지 않았다.
