# CD 실환경 검증 기록 — 2026-11-24

대상은 기존 BEET 운영 코드가 아닌 `beet-cd-demo` 테스트 앱이다.
CP에서 배포기를 실행하고 실제 파드는 기존 워커 w1에서 동작했다.

| 시험 | 관찰한 결과 | 근거 |
|---|---|---|
| CD 회귀 검사 | Linux에서 10개 통과. 성공/실패/진행 중/다른 커밋/외부 PR 배포 판정 및 격리 설정 확인 | `cd/test_cd.py`, GitHub Actions CD regression tests |
| 기존 CI 유지 | CP 20개, EC2 19개와 CD 10개 및 소스 패키징 성공 | [CI 실행](https://github.com/Kimjunbaek/beet-openstack-autoscaling/actions/runs/45305198293) |
| 첫 배포 | rollout 완료, HTTP 200 및 release가 `efc0a4b746939dcc2623ead4eae3a8828aecfb66`와 일치 | CP `state.json`, `deployed_at=2026-11-24T01:29:35.870932+00:00` |
| CI 실행 중 보류 | `WAIT: current main has no successful eligible CI` 로그, 해당 시점 배포 없음 | CP journal |
| 다음 버전 자동 반영 | 앱 메시지를 v2로 커밋한 뒤 수동 배포 요청 없이 systemd timer가 반영. HTTP release가 `32969105aadba1c412fab71f50fdc4b8d5c9ad7d`로 변경 | [v2 CI](https://github.com/Kimjunbaek/beet-openstack-autoscaling/actions/runs/45305473272), `deployed_at=2026-11-24T01:32:42.527306+00:00` |
| 실패 복원 | 실제 테스트 앱의 status를 의도적으로 잘못 반환시켜 HTTP 의미 검사 실패 유도. 이전 release 복원 및 정상 응답 확인 | CP `rollback-verification.json`, `ROLLBACK VERIFIED` 로그 |
| 성공 기록 보존 | 실패 시험 이후 `state.json`의 기존 성공 버전이 변경되지 않음 | probe의 before/after 비교 |

위 UTC 시각은 한국 시각으로 각각 10:29:35, 10:32:42다. 첫 배포는 최초 설치 뒤
service를 수동 시작했고, v2는 커밋 이후 정기 타이머로 배포했다.

## 오류와 수정

최초 점검에서는 namespace 생성 전 배포기를 실행해 `namespaces beet-cd-demo not found`가 발생했다.
`setup.py`로 전용 namespace와 timer를 설치한 뒤 새 커밋 배포를 완료했다.

복원 시험에서는 Deployment rollout 완료 직후 Service 연결 대상이 갱신되는 짧은 지연을 확인했다.
HTTP 상태 검사에 제한된 재시도를 추가했고, 같은 실패 시험을 다시 실행해
`ROLLBACK VERIFIED`와 이전 release의 정상 응답을 확인했다. 회귀 테스트에도 일시적인 접속 실패 후
재시도 성공 검사를 포함했다.

## 실패 주입 범위

복원 시험은 배포 함수에 의도적인 오류 앱을 넘긴 **수동 통합 시험**이다.
`ffffffffffffffffffffffffffffffffffffffff`는 시험용 release 식별자이며 실제 GitHub 커밋이 아니다.
GitHub에서 실패한 CI를 우회해 자동 배포했다고 해석하면 안 된다.
오류 앱의 Kubernetes readiness HTTP는 성공하지만 본문의 `status`가 잘못되어,
별도의 의미 검사가 이를 차단하고 이전 manifest를 재적용하는 경로를 검증했다.

이미지 다운로드 실패, 물리 노드 장애, CP 중단, GitHub 장기 장애의 전체 복구 시험은 수행하지 않았다.
이 결과는 운영 분석기·Nextcloud·Redis·워커 자동화 코드 전체의 CD 완료를 뜻하지 않는다.

## 현장에서 재확인

```bash
sudo systemctl status beet-demo-cd.timer --no-pager
sudo journalctl -u beet-demo-cd.service -n 60 --no-pager
cat /home/ubuntu/beet-demo-cd/state.json
cat /home/ubuntu/beet-demo-cd/rollback-verification.json
sudo -n k3s kubectl -n beet-cd-demo get pods,service -o wide
```

이후 커밋이 배포되면 state.json의 SHA는 위 시험 버전보다 최신 값으로 바뀌는 것이 정상이다.
