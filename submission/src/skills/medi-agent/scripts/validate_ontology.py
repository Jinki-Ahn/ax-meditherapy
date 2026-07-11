#!/usr/bin/env python3
"""온톨로지 구동 검증 하네스.

온톨로지 데이터(seed 또는 모의 생성물)가 '파이프라인 ① 온톨로지 로드'의
입력으로 실제 작동하는지 6개 검사로 확인한다. 실패 시 exit code 1.

  [1] 스키마 적합       — schema.json(JSON Schema)에 대한 검증
  [2] 참조 무결성       — ID 유일성, carried_product_ids → product 존재
  [3] 실험 설계 가능성  — pair_group별 처치/대조 배정 가능 여부(동일 제품 취급 쌍)
  [4] 시딩 후보 풀      — commercially_eligible=false 하드 제외 후 풀이 남는가
  [5] 신호 획득 경로    — 전 신호 acquisition 존재, 리프트 신호 control_definition 존재
  [6] 검정력 시야       — 셀스루 신호 min_sample 대비 배정 가능 쌍 수 → 판단 가능/불가

[3]에서 배정 불가 쌍이 나오는 것은 실패가 아니라 '판단 불가 + 필요한 데이터'
보고 경로의 정상 작동이다(기획서 질문 3 말미).

사용: python3 validate_ontology.py [데이터경로]  (기본: ../data/mock/mock_data.json)
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import jsonschema
except ImportError:  # 외부 패키지 없는 환경 — 동봉된 최소 검증기로 대체
    import schema_lite as jsonschema

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "data" / "ontology" / "schema.json"


def check(label, ok, detail):
    mark = "OK " if ok else "FAIL"
    print(f"[{mark}] {label} — {detail}")
    return ok


def main():
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "data" / "mock" / "mock_data.json"
    schema = json.loads(SCHEMA_PATH.read_text())
    data = json.loads(data_path.read_text())
    print(f"대상: {data_path.name} · 스키마 v{schema['version']}\n")
    ok = True

    # [1] 스키마 적합
    try:
        jsonschema.validate(data, schema)
        counts = (f"커뮤니티 {len(data['communities'])} · "
                  f"접점 {sum(len(c['touchpoints']) for c in data['communities'])} · "
                  f"크리에이터 {len(data['creators'])} · 제품 {len(data['products'])} · "
                  f"신호 {len(data['conversion_signals'])}")
        ok &= check("스키마 적합", True, counts)
    except jsonschema.ValidationError as e:
        ok &= check("스키마 적합", False, f"{list(e.absolute_path)}: {e.message}")

    # [2] 참조 무결성
    all_ids = ([c["id"] for c in data["communities"]]
               + [t["id"] for c in data["communities"] for t in c["touchpoints"]]
               + [c["id"] for c in data["creators"]]
               + [p["id"] for p in data["products"]]
               + [s["id"] for s in data["conversion_signals"]])
    dup = {i for i in all_ids if all_ids.count(i) > 1}
    product_ids = {p["id"] for p in data["products"]}
    dangling = [(t["id"], pid) for c in data["communities"] for t in c["touchpoints"]
                for pid in t.get("carried_product_ids", []) if pid not in product_ids]
    ok &= check("참조 무결성", not dup and not dangling,
                f"ID {len(set(all_ids))}개 유일" if not (dup or dangling)
                else f"중복 {sorted(dup)} · 끊긴 참조 {dangling}")

    # [3] 실험 설계 가능성 — pair_group 안에 같은 제품을 취급하는 지점이 2곳 이상이어야
    #     처치/대조 배정이 가능하다(기획서 질문 3 ②).
    pairs = defaultdict(list)
    for c in data["communities"]:
        for t in c["touchpoints"]:
            pairs[t["pair_group"]].append(t)
    assignable, blocked = [], []
    for pg, tps in sorted(pairs.items()):
        common = set(product_ids)
        for t in tps:
            common &= set(t.get("carried_product_ids", []))
        (assignable if len(tps) >= 2 and common else blocked).append(pg)
    ok &= check("실험 설계 가능성", len(assignable) > 0,
                f"pair_group {len(pairs)}개 중 배정 가능 {len(assignable)} / "
                f"불가 {len(blocked)} {blocked} → 불가 쌍은 '판단 불가' 보고 대상(검색·코드 신호만)")

    # [4] 시딩 후보 풀 — 영리 금지 하드 제외, 브랜드 안전성 플래그는 검토 대상 분리
    eligible = [c for c in data["creators"] if c["commercially_eligible"]]
    excluded = [c for c in data["creators"] if not c["commercially_eligible"]]
    review = [c for c in eligible if c.get("brand_safety_flags")]
    ok &= check("시딩 후보 풀", len(eligible) > 0,
                f"전체 {len(data['creators'])} → 배정 가능 {len(eligible)} · "
                f"하드 제외 {len(excluded)}({', '.join(c['id'] for c in excluded)}) · "
                f"안전성 검토 {len(review)}")

    # [5] 신호 획득 경로
    sigs = data["conversion_signals"]
    no_acq = [s["id"] for s in sigs if "acquisition" not in s]
    lifts = [s for s in sigs if s.get("measurement") == "treatment_vs_control_lift"]
    no_ctrl = [s["id"] for s in lifts if not s.get("control_definition")]
    leading = [s for s in sigs if s["phase"] == "leading"]
    weight_sum = round(sum(s.get("weight", 0) for s in leading), 4)
    ok &= check("신호 획득 경로", not no_acq and not no_ctrl,
                f"acquisition {len(sigs) - len(no_acq)}/{len(sigs)} · 리프트 신호 control_definition {len(lifts) - len(no_ctrl)}/{len(lifts)}"
                f" · 선행 가중치 합 {weight_sum}")

    # [6] 검정력 시야 — 셀스루 신호의 min_sample 대비 배정 가능 쌍 수
    st = next((s for s in sigs if s["source"] == "px_sellthrough"), None)
    if st:
        feasible = len(assignable) >= st["min_sample"]
        ok &= check("검정력 시야", True,  # 미달이어도 '판단 불가'를 내면 정상 — 판정만 보고
                    f"배정 가능 쌍 {len(assignable)} vs min_sample {st['min_sample']} → "
                    + ("판단 가능" if feasible else "판단 불가 보고(정상 경로)"))

    print("\n결과:", "온톨로지 구동 검증 통과" if ok else "검증 실패")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
