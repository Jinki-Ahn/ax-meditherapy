#!/bin/bash
# 제출 직전 최종 패키징 — 이 세션(Claude)이 완전히 종료된 뒤 새 터미널에서 실행할 것.
# 1) 루트 logs/ → submission/logs/ 동기화 (더 큰 쪽만 덮어씀 — 상위집합 로그 보존)
# 2) submission/ 내용으로 submission.zip 생성 (정크 제외)
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1. 로그 동기화 =="
for f in logs/claude-code/*.jsonl; do
  b=$(basename "$f")
  s="submission/logs/claude-code/$b"
  if [ ! -f "$s" ] || [ "$(wc -c <"$f")" -gt "$(wc -c <"$s")" ]; then
    cp -f "$f" "$s" && echo "  복사: $b"
  fi
done
diff <(ls logs/claude-code/) <(ls submission/logs/claude-code/) >/dev/null \
  && echo "  로그 파일 목록 일치" || { echo "  ⚠️ 로그 목록 불일치 — 확인 필요"; exit 1; }

echo "== 2. zip 생성 =="
rm -f submission.zip
(cd submission && zip -rq ../submission.zip . -x "*__pycache__*" -x "*.DS_Store" -x "*.pyc")
echo "  생성: $(pwd)/submission.zip"
unzip -l submission.zip | tail -3

echo "== 완료 — 제출 전 체크 =="
echo "  · 질문지: 제출용_답변.md 내용을 폼에 입력"
echo "  · 올리브영 브랜드관 URL 브라우저 육안 확인 1회"
