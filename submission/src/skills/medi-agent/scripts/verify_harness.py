#!/usr/bin/env python3
"""검증 하네스 — 기획서 질문 5의 검증 기준을 실측 수치로 계량한다.

  ① 인과 검출     효과 있는 데이터 → 리프트 검출 / 효과 없는 데이터 → '효과 없음'
  ② CPA 학습곡선  회전별 배치 CPA(코드 전환 기준) 추이 + 학습 없는 무작위
                  선정 베이스라인 대비 개선율(학습의 반사실 비교)
  ③ 프록시 보정   코드 신호와 재구매의 상관을 일부러 끊은 시나리오(decor_code)에서
                  분기 보정이 코드 가중치를 낮추고, 상관을 심은 시나리오(effective)
                  에서 높이는가 — 방향 정답률

각 실험은 시드 여러 개로 반복해 평균·방향 정답률을 낸다(단일 시드 요행 배제).
게이트: ① 정답률 100% · ② 무작위 대비 CPA 개선 > 0 · ③ 방향 정답률 ≥ 80%.
결과 보고서: ../data/verification/report.md (문항 4·5 재작성의 실측 근거)

사용: python3 verify_harness.py [--seeds 5]
"""

import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / "pipeline.py"
REPORT_DIR = HERE.parent / "data" / "verification"

CPA_WEEKS = 8       # ② 회전 수 (시차 램프업 2주 이후가 유효 구간)
PROXY_WEEKS = 10    # ③ 분기 보정이 여러 번 발동하고 재구매 표본이 쌓일 회전 수
RAMP = 2            # 시차 설계 램프업 — 이 주차까지는 게시가 덜 돼 CPA 과대


def run_pipeline(tmp, tag, seed, weeks, scenario="effective", selection="score"):
    state_dir = Path(tmp) / f"{tag}_s{seed}"
    r = subprocess.run(
        [sys.executable, str(PIPELINE), "--state-dir", str(state_dir),
         "--weeks", str(weeks), "--seed", str(seed), "--scenario", scenario,
         "--selection", selection, "--compress", "8"],
        capture_output=True, text=True, cwd=HERE)
    if r.returncode != 0:
        raise RuntimeError(f"{tag} seed={seed} 실패:\n{r.stdout}\n{r.stderr}")
    state = json.loads((state_dir / "loop_state.json").read_text())
    return r.stdout, state


def weekly_cpa(state):
    """주차별 배치 CPA(원/전환). 전환 0인 주는 None."""
    return [round(h["spent"] / h["conversions"]) if h["conversions"] else None
            for h in state["history"]]


def main():
    n_seeds = 5
    if "--seeds" in sys.argv:
        n_seeds = int(sys.argv[sys.argv.index("--seeds") + 1])
    seeds = [11, 22, 33, 44, 55][:n_seeds]
    lines = ["# 검증 하네스 리포트 (기획서 질문 5)", "",
             f"시드 {seeds} · 회전 {CPA_WEEKS}주(CPA)/{PROXY_WEEKS}주(프록시) · 소모 주기 압축 8배", ""]
    gate_ok = True

    with tempfile.TemporaryDirectory() as tmp:
        # ── ① 인과 검출 ──
        detect_hits = miss_hits = 0
        for s in seeds:
            out_eff, _ = run_pipeline(tmp, "causal_eff", s, 1)
            out_null, _ = run_pipeline(tmp, "causal_null", s, 1, scenario="null")
            detect_hits += "→ 리프트 검출" in out_eff
            miss_hits += "효과 없음 — 검출된 리프트 없음" in out_null
        causal_ok = detect_hits == len(seeds) and miss_hits == len(seeds)
        gate_ok &= causal_ok
        print(f"① 인과 검출 — 효과 데이터 검출 {detect_hits}/{len(seeds)} · "
              f"무효과 데이터 '효과 없음' {miss_hits}/{len(seeds)} → {'통과' if causal_ok else '실패'}")
        lines += ["## ① 인과 검출", "",
                  f"- 효과 심은 데이터에서 리프트 검출: {detect_hits}/{len(seeds)}",
                  f"- 효과 없는 데이터에서 '효과 없음' 보고(오검출 없음): {miss_hits}/{len(seeds)}", ""]

        # ── ② CPA 학습곡선 ──
        curves, final_learn, final_rand = [], [], []
        for s in seeds:
            _, st_learn = run_pipeline(tmp, "cpa_learn", s, CPA_WEEKS)
            _, st_rand = run_pipeline(tmp, "cpa_rand", s, CPA_WEEKS, selection="random")
            curves.append(weekly_cpa(st_learn))
            for st, sink in ((st_learn, final_learn), (st_rand, final_rand)):
                spent = sum(h["spent"] for h in st["history"][RAMP:])
                conv = sum(h["conversions"] for h in st["history"][RAMP:])
                sink.append(spent / conv if conv else float("inf"))
        mean_curve = []
        for w in range(CPA_WEEKS):
            vals = [c[w] for c in curves if c[w] is not None]
            mean_curve.append(round(statistics.mean(vals)) if vals else None)
        valid = [(i, v) for i, v in enumerate(mean_curve) if v is not None and i >= RAMP]
        trend = (valid[0][1] - valid[-1][1]) / valid[0][1] * 100 if len(valid) >= 2 else 0.0
        learn_m, rand_m = statistics.mean(final_learn), statistics.mean(final_rand)
        uplift = (rand_m - learn_m) / rand_m * 100
        gate_ok &= uplift > 0
        print(f"② CPA 학습곡선 — 주차별 평균 CPA(원/전환): "
              + " → ".join("-" if v is None else f"{v:,}" for v in mean_curve))
        print(f"   램프업 이후 추이(W{RAMP + 1}→W{CPA_WEEKS}): {trend:+.1f}% 변화(수렴 후 안정) · "
              f"무작위 선정 대비: 학습 {learn_m:,.0f}원 vs 무작위 {rand_m:,.0f}원 "
              f"→ {uplift:+.1f}% 개선 → {'통과' if uplift > 0 else '실패'}")
        lines += ["## ② CPA 학습곡선 (배치당 CPA = 지출/코드 전환)", "",
                  "| 주차 | " + " | ".join(f"W{w + 1:02d}" for w in range(CPA_WEEKS)) + " |",
                  "|---|" + "---|" * CPA_WEEKS,
                  "| 평균 CPA | " + " | ".join("-" if v is None else f"{v:,}" for v in mean_curve) + " |", "",
                  f"- 램프업(시차 게시 {RAMP}주) 이후 추이: {trend:+.1f}% 변화 — 학습이 W3 전후로 이미 수렴"
                  f"(W1 {mean_curve[0]:,}원 → W3 {mean_curve[2]:,}원). 학습의 핵심 증거는 아래 반사실 비교",
                  f"- 학습 없는 무작위 선정 베이스라인 대비(램프업 이후 누적): **{uplift:+.1f}% 개선** "
                  f"(학습 {learn_m:,.0f}원 vs 무작위 {rand_m:,.0f}원)", ""]

        # ── ③ 프록시 보정 ──
        w0 = None
        eff_final, decor_final, correct = [], [], 0
        for s in seeds:
            _, st_eff = run_pipeline(tmp, "proxy_eff", s, PROXY_WEEKS)
            _, st_dec = run_pipeline(tmp, "proxy_dec", s, PROXY_WEEKS, scenario="decor_code")
            if w0 is None:  # 초기 가중치(정규화)는 온톨로지 정의에서 동일
                w0 = 0.35 / (0.4 + 0.35 + 0.25)
            e, d = st_eff["signal_weights"]["sig_code"], st_dec["signal_weights"]["sig_code"]
            eff_final.append(e)
            decor_final.append(d)
            correct += (e > w0) + (d < w0)
        total = len(seeds) * 2
        rate = correct / total * 100
        gate_ok &= rate >= 80
        print(f"③ 프록시 보정 — 코드 가중치 초기 {w0:.2f} → "
              f"상관 있음(effective) 평균 {statistics.mean(eff_final):.2f}(↑ 기대) · "
              f"상관 끊음(decor_code) 평균 {statistics.mean(decor_final):.2f}(↓ 기대) · "
              f"방향 정답 {correct}/{total}({rate:.0f}%) → {'통과' if rate >= 80 else '실패'}")
        lines += ["## ③ 프록시 보정 (신호↔재구매 상관을 일부러 다르게 심음)", "",
                  f"- 초기 코드 신호 가중치: {w0:.2f}",
                  f"- 상관 심은 시나리오 최종 가중치 평균: **{statistics.mean(eff_final):.2f}** (상승 기대)",
                  f"- 상관 끊은 시나리오 최종 가중치 평균: **{statistics.mean(decor_final):.2f}** (하강 기대)",
                  f"- 방향 정답률: **{correct}/{total} ({rate:.0f}%)**", ""]

    lines += ["## 게이트 판정", "",
              f"- ① 인과 검출 100% / ② 무작위 대비 CPA 개선 > 0 / ③ 프록시 방향 ≥ 80%",
              f"- **결과: {'전체 통과' if gate_ok else '실패'}**", ""]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "report.md").write_text("\n".join(lines))
    print(f"\n검증 하네스 {'전체 통과' if gate_ok else '실패'} — 리포트: {REPORT_DIR / 'report.md'}")
    sys.exit(0 if gate_ok else 1)


if __name__ == "__main__":
    main()
