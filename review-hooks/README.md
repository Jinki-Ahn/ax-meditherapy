# review-hooks — 재사용 적대적 검토 하네스 (드롭인 템플릿)

`log-hooks/`(로그 저장 훅)와 같은 방식의 **자립형 드롭인 번들**이다. 어떤 프로젝트에든
이 폴더 내용을 프로젝트 루트에 복사하면, 매 턴(Stop 훅)마다 변경된 산출물이 자동으로
**2계층 적대적 검토**를 받는다.

## 설계 원칙

어겨선 안 되는 검사는 **코드(결정론)**, 판단이 필요한 검토는 **LLM**에 둔다.

- **Layer 1 (`tools/review_deliverables.py`)** — 훅에서 인라인 실행. 글자수 제한·죽은 링크·
  인용 URL·미완료 마커·문서↔코드 대조·`py_compile`·시나리오 테스트를 결정론으로 검사하고
  `reviews/layer1__<ts>.{json,md}` 기록. 네트워크 I/O 없음, 항상 exit 0.
- **Layer 2 (`tools/run_llm_review.py`)** — 분리 프로세스로 실행(훅 지연 0). 저장된
  루브릭 `tools/reviewer.md`로 헤드리스 CLI(claude 우선, 없으면 codex)에게 변경 산출물을
  적대적으로 심사시키고 `reviews/review__<ts>.md` 기록. CLI가 없으면 스킵 스텁으로 degrade.

## 설치

```
cp -R review-hooks/.claude review-hooks/.codex .          # 훅 배선
cp review-hooks/tools/* tools/                             # 스크립트+루브릭
cp review-hooks/review.config.json .                      # 산출물 glob·제한 설정
```
이미 `save_log.py` 훅을 쓰고 있으면 `.claude/settings.json`·`.codex/hooks.json`의
`Stop` 배열에 **review 훅 객체만** 형제로 추가하면 된다(기존 save_log 항목은 그대로).

## 재사용 시 바꾸는 것

`review.config.json` 하나만. 산출물 glob, 글자수 제한, 문서-코드 대조 규칙이 전부 여기 있다.
코드(`review_deliverables.py`)는 손대지 않는다.

## 안전장치

- **루프 방지**: `TRIP_REVIEW_ACTIVE=1` 센티넬(셸+파이썬 양쪽 체크) + `reviews/` 제외 +
  첫 실행 베이스라인. 리뷰가 리뷰를 재유발하지 않는다.
- **읽기 전용 LLM**: Layer 2는 `--permission-mode plan`으로 돌아 산출물을 편집하지 않는다.
- **세션 보호**: 어떤 실패도 stdout에 쓰지 않고 항상 exit 0 — 검토 실패가 세션을 막지 않는다.
