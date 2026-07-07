#!/usr/bin/env python3
"""결정 커버리지 테스트 스위트 — 살충제 패러독스 대응.

같은 happy-path 데이터만 반복 검증하면 하네스는 새 결함을 잡지 못한다
(살충제 패러독스). 이 스위트는 구조가 다른 입력들 —
  · mock_data.json        (생성기 happy-path, 대규모)
  · seed.json             (소형 — 검정력 미달 경로)
  · mock_data_edge.json   (적대 데이터 — 배정 전면 불가·홀수 쌍·셀스루 신호 부재)
  · 표적 변형             (스키마 위반·중복 ID·끊긴 참조·후보 풀 소멸·대조 정의 공백)
  · 시나리오 변형          (effective/null, 1주/4주 회전)
— 으로 검증 하네스(validate_ontology.py)의 결정 D1~D6b와
파이프라인(pipeline.py)의 결정 P1~P7이 모두 참/거짓 양방향으로 실행되게
한다(결정 커버리지). 각 케이스는 기대 종료 코드·출력 마커·산출물(post 검사)을
단언하고, 마지막에 결정×결과 커버리지 매트릭스를 출력한다. 미커버 결정이
있으면 exit 1.

객관 측정: --coverage 플래그로 실행하면 coverage.py 브랜치 커버리지를 함께
리포트한다(validate_ontology.py는 100%가 게이트, 파이프라인 계열은 참고 지표).

사용: python3 test_decision_coverage.py [--coverage]
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
VALIDATOR = HERE / "validate_ontology.py"
PIPELINE = HERE / "pipeline.py"
MOCK = DATA / "mock" / "mock_data.json"
EDGE = DATA / "mock" / "mock_data_edge.json"
SEED = DATA / "ontology" / "seed.json"

DECISIONS = {
    # 검증 하네스 (validate_ontology.py)
    "D1":  "스키마 적합 여부",
    "D2a": "중복 ID 존재",
    "D2b": "끊긴 제품 참조 존재",
    "D3a": "pair_group 지점 수 ≥ 2",
    "D3b": "쌍 내 공통 취급 제품 존재",
    "D3c": "배정 가능 쌍 > 0",
    "D4a": "시딩 후보 풀 > 0",
    "D4b": "안전성 검토 대상 존재",
    "D5a": "acquisition 누락 존재",
    "D5b": "리프트 신호 대조 정의 공백",
    "D6a": "셀스루 신호 존재",
    "D6b": "검정력(min_sample) 충족",
    # 파이프라인 (pipeline.py)
    "P1": "지점 처치/대조 배정 가능",
    "P2": "시딩 가능 크리에이터 존재",
    "P3": "안전성 플래그 → 자동 배정 제외 발생",
    "P4": "셀스루 신호 검정력 충족",
    "P5": "리프트 검출(실효과 존재)",
    "P6": "후행 신호 도착 → 분기 보정 발동",
    "P7": "브리프 생성 실행",
    "P8": "예산 제약으로 후보 스킵 발생",
    "P9": "증거 패키지에 판단 불가 항목 존재",
    "P10": "대상 제품 온톨로지에 존재",
    "P11": "저장된 루프 상태 재개",
    "P12": "예산 내 선정 가능 크리에이터 존재",
}


def mut_dup_id(d):
    d["creators"][1]["id"] = d["creators"][0]["id"]


def mut_dangling_ref(d):
    d["communities"][0]["touchpoints"][0]["carried_product_ids"] = ["pr_999"]


def mut_no_acquisition(d):
    del d["conversion_signals"][0]["acquisition"]


def mut_empty_pool(d):
    for c in d["creators"]:
        c["commercially_eligible"] = False


def mut_all_carry(d):
    for com in d["communities"]:
        for tp in com["touchpoints"]:
            tp["carried_product_ids"] = ["pr_001"]


def mut_sell_min99(d):
    next(s for s in d["conversion_signals"]
         if s["source"] == "px_sellthrough")["min_sample"] = 99


def mut_ctrl_empty(d):
    lift = next(s for s in d["conversion_signals"]
                if s["measurement"] == "treatment_vs_control_lift")
    lift["control_definition"] = ""  # 존재하지만 공백 — 스키마는 통과, [5]가 잡아야 함


def post_briefs_and_package(out_dir):
    """pipe_mock_4w 산출물 검사: 브리프 가드레일과 수요 증거 패키지."""
    probs = []
    briefs = sorted((out_dir / "week_01" / "briefs").glob("*.md"))
    if not briefs:
        return ["week_01 브리프 없음"]
    text = briefs[0].read_text()
    for needle in ["협찬 고지", "유료 광고", "금지 표현", "추적 코드: W01-", "게시 주차"]:
        if needle not in text:
            probs.append(f"브리프에 {needle!r} 없음")
    pkg = out_dir / "week_01" / "evidence_package.md"
    if not pkg.exists():
        probs.append("수요 증거 패키지 없음")
    elif "판단 불가" not in pkg.read_text():
        probs.append("증거 패키지에 판단 불가 절 없음")
    return probs


# name / prog / data(경로 | 변형 함수) / args / want_exit / must / must_not / covers / post
CASES = [
    # ── 검증 하네스 ──
    dict(name="mock_happy", prog="validator", data=None, want_exit=0,
         must=["[OK ] 스키마 적합", "[OK ] 참조 무결성", "안전성 검토 2",
               "acquisition 5/5", "판단 가능", "온톨로지 구동 검증 통과"],
         must_not=["FAIL"],
         covers={"D1": True, "D2a": False, "D2b": False, "D3a": True, "D3b": True,
                 "D3c": True, "D4a": True, "D4b": True, "D5a": False, "D5b": False,
                 "D6a": True, "D6b": True}),
    dict(name="seed_small", prog="validator", data=SEED, want_exit=0,
         must=["안전성 검토 0", "판단 불가 보고(정상 경로)", "온톨로지 구동 검증 통과"],
         must_not=["FAIL"],
         covers={"D4b": False, "D6b": False}),
    dict(name="edge_adversarial", prog="validator", data=EDGE, want_exit=1,
         must=["[FAIL] 실험 설계 가능성", "배정 가능 0", "pg_solo", "pg_cross", "pg_none"],
         must_not=["검정력 시야"],
         covers={"D3a": False, "D3b": False, "D3c": False, "D6a": False}),
    dict(name="mut_schema_break", prog="validator", data=mut_no_acquisition, want_exit=1,
         must=["[FAIL] 스키마 적합", "[FAIL] 신호 획득 경로", "acquisition 4/5"],
         covers={"D1": False, "D5a": True}),
    dict(name="mut_dup_id", prog="validator", data=mut_dup_id, want_exit=1,
         must=["[FAIL] 참조 무결성", "중복"], covers={"D2a": True}),
    dict(name="mut_dangling_ref", prog="validator", data=mut_dangling_ref, want_exit=1,
         must=["[FAIL] 참조 무결성", "pr_999"], covers={"D2b": True}),
    dict(name="mut_empty_pool", prog="validator", data=mut_empty_pool, want_exit=1,
         must=["[FAIL] 시딩 후보 풀", "배정 가능 0"], covers={"D4a": False}),
    dict(name="mut_ctrl_empty", prog="validator", data=mut_ctrl_empty, want_exit=1,
         must=["[FAIL] 신호 획득 경로", "control_definition 1/2"], covers={"D5b": True}),
    # ── 파이프라인 ──
    dict(name="pipe_mock_4w", prog="pipeline", data=None,
         args=["--weeks", "4", "--compress", "8"], want_exit=0,
         must=["① 온톨로지 로드", "② 실험 설계 — 지점 처치/대조 배정 9쌍",
               "안전성 검토로 제외 2명", "시차 오프셋 분포", "③ 브리프 생성 6건",
               "리프트 검출", "분기 보정 — 누적 재구매", "파이프라인 정상 종료"],
         covers={"P1": True, "P2": True, "P3": True, "P4": True, "P5": True,
                 "P6": True, "P7": True, "P8": False, "P9": True, "P10": True,
                 "P12": True},
         post=post_briefs_and_package),
    dict(name="pipe_mock_1w", prog="pipeline", data=None, want_exit=0,
         must=["(W01)", "후행 신호 미도착 — 가중치 유지", "파이프라인 정상 종료"],
         covers={"P6": False, "P11": False}),
    dict(name="pipe_null_scenario", prog="pipeline", data=None,
         args=["--scenario", "null"], want_exit=0,
         must=["효과 없음 — 검출된 리프트 없음", "파이프라인 정상 종료"],
         must_not=["→ 리프트 검출"],
         covers={"P5": False}),
    dict(name="pipe_edge", prog="pipeline", data=EDGE, want_exit=0,
         must=["지점 처치/대조 배정 불가", "제외: PX 셀스루 — 신호 정의 없음",
               "안전성 검토 대상 없음", "파이프라인 정상 종료"],
         covers={"P1": False, "P3": False}),
    dict(name="pipe_seed_power", prog="pipeline", data=SEED, want_exit=0,
         must=["판단 불가 — PX 셀스루 제외", "min_sample", "파이프라인 정상 종료"],
         covers={"P4": False}),
    dict(name="pipe_empty_pool", prog="pipeline", data=mut_empty_pool, want_exit=2,
         must=["판단 불가: 시딩 가능 크리에이터 없음"],
         must_not=["③ 브리프 생성"],
         covers={"P2": False, "P7": False}),
    dict(name="pipe_tight_budget", prog="pipeline", data=mut_all_carry,
         args=["--budget", "300000"], want_exit=0,
         must=["판단 불가 0건 명시", "파이프라인 정상 종료"],
         must_not=["크리에이터 6명 선정"],
         covers={"P8": True, "P9": False}),
    dict(name="pipe_no_product", prog="pipeline", data=None,
         args=["--product", "pr_999"], want_exit=2,
         must=["판단 불가: 대상 제품 pr_999"],
         covers={"P10": False}),
    dict(name="pipe_resume", prog="pipeline", data=None, prep_weeks=1, want_exit=0,
         must=["(W02)", "파이프라인 정상 종료"],
         covers={"P11": True}),
    # 신호 무변동 분기: 셀스루 강제 제외(min_sample=99) → 해당 신호 누적치 전부 0
    # → 분기 보정에서 예측력 판정 불가 → 기존 가중치 유지 (branch 커버 전용, 매트릭스 결정 없음)
    dict(name="pipe_zero_variance", prog="pipeline", data=mut_sell_min99,
         args=["--weeks", "4", "--compress", "8"], want_exit=0,
         must=["판단 불가 — PX 셀스루 제외", "분기 보정 — 누적 재구매", "파이프라인 정상 종료"],
         covers={}),
    dict(name="pipe_budget_too_small", prog="pipeline", data=None,
         args=["--budget", "5000"], want_exit=2,
         must=["판단 불가: 예산 내 선정 가능한 크리에이터 없음"],
         must_not=["③ 브리프 생성"],
         covers={"P12": False}),
    # 학습 없는 무작위 선정 베이스라인(검증 하네스의 반사실 비교 경로) 회귀 확인
    dict(name="pipe_random_baseline", prog="pipeline", data=None,
         args=["--selection", "random"], want_exit=0,
         must=["파이프라인 정상 종료"],
         covers={}),
]


def run_case(case, tmp, base, use_coverage, env):
    src = case["data"]
    if callable(src):
        d = copy.deepcopy(base)
        src(d)
        path = Path(tmp) / f"{case['name']}.json"
        path.write_text(json.dumps(d, ensure_ascii=False))
    else:
        path = src  # None이면 기본 경로 분기(mock)도 커버

    prog = VALIDATOR if case["prog"] == "validator" else PIPELINE
    cmd = [sys.executable]
    if use_coverage:
        cmd += ["-m", "coverage", "run", "--branch", "--append", str(prog)]
    else:
        cmd.append(str(prog))
    out_dir = None
    if case["prog"] == "pipeline":
        out_dir = Path(tmp) / f"state_{case['name']}"
        cmd += ["--state-dir", str(out_dir)]
        if path is not None:
            cmd += ["--data", str(path)]
        cmd += case.get("args", [])
        for _ in range(case.get("prep_weeks", 0)):  # 상태 재개 케이스: 선행 회전 실행
            subprocess.run(cmd, capture_output=True, text=True, cwd=HERE, env=env, check=True)
    elif path is not None:
        cmd.append(str(path))
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE, env=env)

    probs = []
    if r.returncode != case["want_exit"]:
        probs.append(f"exit {r.returncode} (기대 {case['want_exit']})")
    probs += [f"마커 없음: {m!r}" for m in case["must"] if m not in r.stdout]
    probs += [f"금지 마커 출현: {m!r}" for m in case.get("must_not", []) if m in r.stdout]
    if not probs and case.get("post"):
        probs += case["post"](out_dir)
    return probs


def main():
    use_coverage = "--coverage" in sys.argv
    base = json.loads(MOCK.read_text())
    covered = {k: {} for k in DECISIONS}
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, COVERAGE_FILE=str(Path(tmp) / ".coverage"))
        for case in CASES:
            probs = run_case(case, tmp, base, use_coverage, env)
            status = "PASS" if not probs else "FAIL"
            print(f"[{status}] {case['name']}" + (f" — {'; '.join(probs)}" if probs else ""))
            if probs:
                failures.append(case["name"])
            else:
                for dec, outcome in case["covers"].items():
                    covered[dec].setdefault(outcome, case["name"])

        print("\n결정 커버리지 매트릭스:")
        all_covered = True
        for dec, desc in DECISIONS.items():
            row = []
            for outcome in (True, False):
                name = covered[dec].get(outcome)
                row.append(f"{'참' if outcome else '거짓'}={name or '미커버'}")
                all_covered &= name is not None
            print(f"  {dec:4s} {desc:26s} {' · '.join(row)}")

        if use_coverage:
            # __main__ 가드의 거짓 분기(모듈 import 경로) + 스크립트 실행으로는 도달
            # 불가한 방어 분기(분기 보정: per_* 신호의 변동 0) 단위 커버
            imp = Path(tmp) / "import_only.py"
            imp.write_text(
                "import random\n"
                "import validate_ontology\n"
                "import pipeline\n"
                "import signal_adapter_mock\n"
                "state = {'week': 9, 'signal_weights': {'sig_code': 1.0},\n"
                "         'signal_agg': {c: {'sig_code': 1.0, 'weeks': 1} for c in 'abc'},\n"
                "         'pending_lagging': [{'creator': c, 'due_week': 1} for c in 'abc'],\n"
                "         'arrived_rates': {}, 'batches': [], 'history': [], 'combos': {}}\n"
                "data = {'conversion_signals': [{'id': 'sig_code',\n"
                "                                'acquisition': {'granularity': 'per_code_daily'}}],\n"
                "        'creators': [{'id': c, 'creator_type': 'x'} for c in 'abc']}\n"
                "pipeline.quarterly_calibration(data, state, random.Random(0), 'effective')\n"
                "assert state['signal_weights'] == {'sig_code': 1.0}  # 변동 0 → 가중치 유지\n")
            subprocess.run([sys.executable, "-m", "coverage", "run", "--branch",
                            "--append", str(imp)],
                           capture_output=True, text=True, cwd=HERE,
                           env=dict(env, PYTHONPATH=str(HERE)), check=True)
            print("\ncoverage.py 브랜치 커버리지:")
            rep = subprocess.run(
                [sys.executable, "-m", "coverage", "report", "-m",
                 f"--include={VALIDATOR},{PIPELINE},{HERE / 'signal_adapter_mock.py'}"],
                capture_output=True, text=True, cwd=HERE, env=env)
            print(rep.stdout or rep.stderr)

    ok = not failures and all_covered
    print("결과:", "결정 커버리지 충족 · 전 케이스 기대 결과 일치" if ok
          else f"실패 — 케이스 {failures or '없음'} · 커버리지 {'충족' if all_covered else '미충족'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
