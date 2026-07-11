#!/usr/bin/env python3
"""BaseCamp Loop 파이프라인 — 기획서 질문 3의 5단계 구현.

  ① 온톨로지 로드    스키마 검증 + 조합 스코어(루프 상태) 로드
  ② 실험 설계        처치 이원화 — 쌍 지점 처치/대조 배정 + 크리에이터 시차(staggered)
                     설계, 조합별 추적 코드 발급, 검정력 미달 신호는 제외·사유 보고
  ③ 브리프 생성      핵심 메시지 × 커뮤니티 맥락 결합(온톨로지가 아니라 이 시점에),
                     금지 표현·협찬 고지·추적 코드 포함
  ④ 신호 수집        signal_adapter_mock(모의) — 실서비스에서 acquisition 경로로 교체
  ⑤ 이중 루프        주간: 선행 신호 → 조합 스코어 EMA 갱신 → 예산 재배분(밴딧식)
                     분기: 도착한 재구매로 선행 신호 가중치 보정(프록시 보정)

정보가 부족하면 추측하지 않는다 — '판단 불가 + 필요한 데이터'를 보고하고,
전제(시딩 가능 크리에이터)가 없으면 exit 2로 중단한다.

사용: python3 pipeline.py --data ../data/mock/mock_data.json --state-dir <DIR>
      [--weeks 1] [--seed 42] [--scenario effective|null] [--compress 1]
      [--budget 10000000] [--product pr_001]
"""

import argparse
import json
import random
import statistics
from pathlib import Path

try:
    import jsonschema
except ImportError:  # 외부 패키지 없는 환경 — 동봉된 최소 검증기로 대체
    import schema_lite as jsonschema

import signal_adapter_mock as adapter

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "data" / "ontology" / "schema.json"

EXIT_UNDECIDABLE = 2
STAGGER_WEEKS = 3          # 시차 설계: 게시 주차를 0..2주로 분산
MAX_CREATORS_PER_BATCH = 6
EMA_ALPHA = 0.4
EXPLORE_FLOOR = 0.1        # 밴딧식 재배분의 탐색 하한(스코어가 낮아도 예산 전멸 금지)
T_DETECT = 2.0             # 리프트 검출 임계(t 통계량)


# ── ① 온톨로지 로드 ──────────────────────────────────────────────

def load_ontology(data_path):
    schema = json.loads(SCHEMA_PATH.read_text())
    data = json.loads(Path(data_path).read_text())
    jsonschema.validate(data, schema)
    print(f"① 온톨로지 로드 — 스키마 v{schema['version']} 적합 · "
          f"커뮤니티 {len(data['communities'])} · 크리에이터 {len(data['creators'])} · "
          f"제품 {len(data['products'])} · 신호 {len(data['conversion_signals'])}")
    return data


def load_state(state_dir, signals):
    p = state_dir / "loop_state.json"
    if p.exists():
        return json.loads(p.read_text())
    leading = [s for s in signals if s["phase"] == "leading"]
    total = sum(s.get("weight", 0) for s in leading) or 1.0
    return {
        "week": 1,
        "signal_weights": {s["id"]: round(s.get("weight", 0) / total, 4) for s in leading},
        "combos": {},              # "cr|com|pr" -> score (사전값 = prior 평균)
        "signal_agg": {},          # cr_id -> {sig_id: 누적 관측치} (분기 보정의 재료)
        "batches": [],             # 주차별 배치(시차 설계 포함) — 게시는 배치 주차+오프셋에 일어난다
        "pending_lagging": [],     # [{creator, due_week}]
        "arrived_rates": {},       # cr_id -> 도착한 재구매율(누적) — 분기 보정의 근거
        "history": [],
    }


def save_state(state_dir, state):
    (state_dir / "loop_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── ② 실험 설계 ─────────────────────────────────────────────────

def design_experiment(data, state, product, rng):
    week = state["week"]
    undecidable = []  # (대상, 사유, 필요한 데이터/조치)

    # 지점 처치: 쌍 안에서 대상 제품을 취급하는 지점 2곳 → 처치/대조 배정
    pairs = {}
    for com in data["communities"]:
        for tp in com["touchpoints"]:
            pairs.setdefault(tp["pair_group"], []).append((com, tp))
    assignments = []
    for pg, members in sorted(pairs.items()):
        carrying = [(c, t) for c, t in members if product["id"] in t.get("carried_product_ids", [])]
        if len(carrying) < 2:
            undecidable.append((f"쌍 {pg}", "처치/대조 배정 불가(대상 제품 취급 지점 2곳 미만)",
                                "대상 제품 취급 지점 확보 또는 쌍 재구성"))
            continue
        t, c = rng.sample(carrying[:2], 2)
        assignments.append({"pair_group": pg,
                            "treatment": t[1], "treatment_com": t[0]["id"],
                            "control": c[1], "control_com": c[0]["id"]})
    if assignments:
        print(f"② 실험 설계 — 지점 처치/대조 배정 {len(assignments)}쌍"
              f" (대조 정의: pair_group 내 미처치 지점의 동일 기간)")
    else:
        print("② 실험 설계 — 지점 처치/대조 배정 불가(배정 가능 쌍 0) → 지점 신호 없이 진행")

    # 검정력 점검: 셀스루 신호의 min_sample 대비 배정 쌍 수
    excluded_signals = {}
    sell = next((s for s in data["conversion_signals"] if s["source"] == "px_sellthrough"), None)
    if sell is None:
        excluded_signals["sig_sellthrough"] = "온톨로지에 셀스루 신호 정의 없음"
        print("   제외: PX 셀스루 — 신호 정의 없음")
    elif len(assignments) < sell["min_sample"]:
        excluded_signals[sell["id"]] = (f"검정력 미달(배정 쌍 {len(assignments)} < min_sample {sell['min_sample']})")
        print(f"   판단 불가 — PX 셀스루 제외: 배정 쌍 {len(assignments)} < min_sample {sell['min_sample']}"
              f" · 필요한 데이터: 취급 지점 쌍 추가 확보")

    # 크리에이터 시차 설계: 영리 불가 하드 제외, 안전성 플래그는 인간 검토로 분리
    eligible, review = [], []
    for c in data["creators"]:
        if not c["commercially_eligible"]:
            continue
        (review if c.get("brand_safety_flags") else eligible).append(c)
    if review:
        print(f"   안전성 검토로 제외 {len(review)}명({', '.join(c['id'] for c in review)}) — 인간 판단 대기")
    else:
        print("   안전성 검토 대상 없음")
    if not eligible:
        print("판단 불가: 시딩 가능 크리에이터 없음 — 필요한 데이터: 영리 활동 가능한 후보 리스트 보강")
        return None, undecidable

    def creator_score(c):
        scores = [v for k, v in state["combos"].items() if k.startswith(c["id"] + "|")]
        return statistics.mean(scores) if scores else c.get("prior_score", 0.5)

    def creator_value(c):
        # 밴딧의 팔 가치 = 원당 기대 성과. 스코어(전환 양)만 좇으면 비싼 대형
        # 크리에이터로 예산이 쏠려 CPA가 무작위보다 나빠진다(검증 하네스 실측).
        return creator_score(c) / max(c["cost_per_seeding"], 1)

    eligible.sort(key=lambda c: (-creator_value(c), c["id"]))
    if ARGS.selection == "random":  # 학습 없는 베이스라인(검증 하네스의 반사실 비교용)
        rng.shuffle(eligible)
    selected, spent, budget = [], 0, ARGS.budget

    # 탐색 슬롯(밴딧의 탐색): 매주 1자리는 신호 이력이 없는 후보에게. 탐색이 없으면
    # 같은 소수에 수렴해 재구매 표본이 멈추고 분기 보정의 검정력이 안 나온다(하네스 실측).
    explored = set(state["signal_agg"])
    explore_pick = next((c for c in eligible if c["id"] not in explored
                         and c["cost_per_seeding"] <= budget), None)
    cap = MAX_CREATORS_PER_BATCH - (1 if explore_pick else 0)
    for c in eligible:
        if c is explore_pick:
            continue
        if len(selected) >= cap:
            break
        if spent + c["cost_per_seeding"] > budget:
            continue
        selected.append(c)
        spent += c["cost_per_seeding"]
    if explore_pick and spent + explore_pick["cost_per_seeding"] <= budget:
        selected.append(explore_pick)
        spent += explore_pick["cost_per_seeding"]
    if not selected:
        print("판단 불가: 예산 내 선정 가능한 크리에이터 없음 — 필요한 조치: 예산 증액 또는 저비용 후보 보강")
        return None, undecidable
    offsets = {c["id"]: i % STAGGER_WEEKS for i, c in enumerate(selected)}
    codes = {c["id"]: f"W{week:02d}-{c['id'].upper()}-{product['id'].upper()}" for c in selected}
    dist = {}
    for off in offsets.values():
        dist[off] = dist.get(off, 0) + 1
    print(f"   크리에이터 {len(selected)}명 선정(예산 {spent:,}/{budget:,}원) · "
          f"시차 오프셋 분포 {dict(sorted(dist.items()))} · 추적 코드 {len(codes)}건 발급")

    return {"week": week, "product": product["id"], "assignments": assignments,
            "selected": selected, "offsets": offsets, "codes": codes,
            "excluded_signals": excluded_signals, "budget_spent": spent}, undecidable


# ── ③ 브리프 생성 ────────────────────────────────────────────────

CONTEXT_BY_SLOT = {  # 핵심 메시지 × 커뮤니티 맥락 결합은 온톨로지가 아니라 여기서 일어난다
    "military": "점호 전 짧은 정비 시간·야외 훈련 후 회복 같은 부대 생활 장면에 핵심 메시지를 얹을 것",
}


def generate_briefs(design, data, product, out_dir):
    slot_types = {c["slot_type"] for c in data["communities"]}
    context = " / ".join(CONTEXT_BY_SLOT.get(s, "커뮤니티의 일상 장면에 핵심 메시지를 얹을 것") for s in sorted(slot_types))
    brief_dir = out_dir / "briefs"
    brief_dir.mkdir(parents=True, exist_ok=True)
    for c in design["selected"]:
        lines = [
            f"# 시딩 브리프 — {c['name']} ({c['id']})",
            f"- 제품: {product['name']} ({product['id']})",
            f"- 게시 주차: W{design['week'] + design['offsets'][c['id']]:02d} (시차 설계 — 임의 변경 금지)",
            f"- 추적 코드: {design['codes'][c['id']]} (콘텐츠·댓글 고정 표기)",
            f"- 핵심 메시지: " + " / ".join(ap["claim"] for ap in product["appeal_points"]),
            f"- 커뮤니티 맥락: {context}",
            f"- 금지 표현: " + ", ".join(product.get("forbidden_claims", [])),
            f"- 의무 표기(협찬 고지): " + ", ".join(product.get("required_disclosure", [])),
        ]
        (brief_dir / f"{c['id']}.md").write_text("\n".join(lines))
    print(f"③ 브리프 생성 {len(design['selected'])}건 — 핵심 메시지×커뮤니티 맥락·금지 표현·협찬 고지·추적 코드 포함")


# ── ④ 신호 수집 ─────────────────────────────────────────────────

def published_creators(data, state):
    """시차 설계 반영: 모든 활성 배치에서 게시 주차(배치 주차+오프셋)가 도래했고
    관측 창(2주) 안에 있는 크리에이터."""
    week = state["week"]
    by_id = {c["id"]: c for c in data["creators"]}
    pub = set()
    for b in state["batches"]:
        for cid in b["selected"]:
            pw = b["week"] + b["offsets"][cid]
            if pw <= week <= pw + 1:
                pub.add(cid)
    return [by_id[i] for i in sorted(pub)]


def collect_signals(design, data, state, rng, scenario):
    week = state["week"]
    published = published_creators(data, state)
    pair_args = [(a["pair_group"], a["treatment"], a["control"]) for a in design["assignments"]]
    if "sig_sellthrough" in design["excluded_signals"]:
        pair_args = []
    obs = adapter.simulate_week(published, data["communities"], pair_args, rng, scenario)
    print(f"④ 신호 수집(모의 어댑터) — 게시 완료 {len(published)}명 · 검색 {len(obs['search'])}개 커뮤니티"
          f" · 코드 {len(obs['code'])}건 · 셀스루 {len(obs['sellthrough'])}쌍")
    return obs, published


def detect_lift(obs):
    """처치 vs 대조 리프트의 t 통계량 검정. 검출 없으면 '효과 없음' — 추측 금지."""
    findings = []
    lifts = [(v["post"] - v["pre"]) / v["pre"] for v in obs["search"].values()]
    if len(lifts) >= 3 and statistics.stdev(lifts) > 0:
        t = statistics.mean(lifts) / (statistics.stdev(lifts) / len(lifts) ** 0.5)
        findings.append(("검색 리프트", statistics.mean(lifts), t))
    diffs = [(v["treatment"] - v["control"]) / max(v["control"], 1) for v in obs["sellthrough"].values()]
    if len(diffs) >= 2 and statistics.stdev(diffs) > 0:
        t = statistics.mean(diffs) / (statistics.stdev(diffs) / len(diffs) ** 0.5)
        findings.append(("PX 셀스루", statistics.mean(diffs), t))
    detected = [(n, l, t) for n, l, t in findings if t > T_DETECT and l > 0]
    for name, lift, t in findings:
        verdict = "리프트 검출" if (name, lift, t) in [d for d in detected] else "효과 없음(임계 미달)"
        print(f"   {name}: 평균 {lift:+.1%}, t={t:.1f} → {verdict}")
    if not detected:
        print("   종합: 효과 없음 — 검출된 리프트 없음(추측 대신 보고)")
    return detected


# ── ⑤ 이중 루프 ─────────────────────────────────────────────────

def weekly_update(design, data, state, obs, published, compress):
    """주간 루프: 선행 신호 → 조합 스코어 EMA 갱신 → 예산 재배분(밴딧식)."""
    week = state["week"]
    weights = dict(state["signal_weights"])
    for sid in design["excluded_signals"]:
        weights.pop(sid, None)
    total_w = sum(weights.values()) or 1.0
    weights = {k: v / total_w for k, v in weights.items()}

    max_code = max(list(obs["code"].values()) + [1])
    com_lift = {cid: max(0.0, (v["post"] - v["pre"]) / v["pre"]) for cid, v in obs["search"].items()}
    max_lift = max(list(com_lift.values()) + [0.01])
    pair_lift_by_com = {}
    for a in design["assignments"]:
        st = obs["sellthrough"].get(a["pair_group"])
        if st:
            pair_lift_by_com[a["treatment_com"]] = max(
                0.0, (st["treatment"] - st["control"]) / max(st["control"], 1))
    max_pair = max(list(pair_lift_by_com.values()) + [0.01])

    mean_lift = statistics.mean(com_lift.values()) if com_lift else 0.0
    mean_pair = statistics.mean(pair_lift_by_com.values()) if pair_lift_by_com else 0.0
    updated = 0
    for c in published:
        # 분기 보정용 누적치: 도달 보정(코드=1천뷰당)·주당 평균으로 정규화해야
        # 크리에이터 간 비교가 가능하다(정규화 없인 프록시 보정 방향 정답률 40%).
        agg = state["signal_agg"].setdefault(
            c["id"], {"sig_search": 0.0, "sig_code": 0.0, "sig_sellthrough": 0.0, "weeks": 0})
        agg["weeks"] += 1
        agg["sig_code"] += obs["code"].get(c["id"], 0) / max(c["avg_views"] / 1000, 1)
        agg["sig_search"] += mean_lift
        agg["sig_sellthrough"] += mean_pair
        for com in data["communities"]:
            composite = (
                weights.get("sig_search", 0) * com_lift.get(com["id"], 0) / max_lift
                + weights.get("sig_code", 0) * obs["code"].get(c["id"], 0) / max_code
                + weights.get("sig_sellthrough", 0) * pair_lift_by_com.get(com["id"], 0) / max_pair
            )
            key = f"{c['id']}|{com['id']}|{design['product']}"
            prev = state["combos"].get(key, c.get("prior_score", 0.5))
            state["combos"][key] = round((1 - EMA_ALPHA) * prev + EMA_ALPHA * composite, 4)
            updated += 1

    # 예산 재배분(밴딧식): 크리에이터 평균 조합 스코어 비례 + 탐색 하한
    scores = {}
    for c in design["selected"]:
        vals = [v for k, v in state["combos"].items() if k.startswith(c["id"] + "|")]
        scores[c["id"]] = max(statistics.mean(vals) if vals else 0.5, EXPLORE_FLOOR)
    total = sum(scores.values()) or 1.0
    alloc = {cid: round(ARGS.budget * s / total) for cid, s in scores.items()}
    print(f"⑤ 주간 루프 — 조합 스코어 갱신 {updated}건(EMA α={EMA_ALPHA}) · "
          f"차주 예산 재배분: " + ", ".join(f"{k} {v:,}원" for k, v in sorted(alloc.items(), key=lambda x: -x[1])[:3]) + " …")

    # 후행 신호 도착 예약(소모 주기, 시뮬레이션 압축 반영) — 이번 주에 '처음 게시된' 크리에이터만
    product = next(p for p in data["products"] if p["id"] == design["product"])
    delay = max(1, round(product["consumption_cycle_days"] / (7 * compress)))
    newly = {cid for b in state["batches"] for cid in b["selected"]
             if b["week"] + b["offsets"][cid] == week}
    scheduled = {p["creator"] for p in state["pending_lagging"]} | set(state["arrived_rates"])
    for cid in sorted(newly - scheduled):
        state["pending_lagging"].append({"creator": cid, "due_week": week + delay})
    conversions = sum(obs["code"].values())  # 코드 첫 구매 = 귀속 가능한 전환
    state["history"].append({"week": week, "alloc": alloc,
                             "spent": design["budget_spent"], "conversions": conversions,
                             "excluded": design["excluded_signals"]})
    return alloc


def quarterly_calibration(data, state, rng, scenario):
    """분기 루프: 도착한 재구매율로 선행 신호 가중치를 보정(프록시 보정)."""
    week = state["week"]
    due = [p for p in state["pending_lagging"] if p["due_week"] <= week]
    if not due:
        print("   분기 보정: 후행 신호 미도착 — 가중치 유지")
        return None
    state["pending_lagging"] = [p for p in state["pending_lagging"] if p["due_week"] > week]
    creators = {c["id"]: c for c in data["creators"]}
    for p in due:
        state["arrived_rates"][p["creator"]] = adapter.repurchase_rate(creators[p["creator"]], rng, scenario)

    rates = state["arrived_rates"]  # 누적 — 분기 보정은 도착분 전체를 근거로 삼는다
    if len(rates) < 3:
        print(f"   분기 보정: 표본 부족(누적 재구매 {len(rates)}건 < 3) — 판단 불가, 가중치 유지")
        return None
    old = dict(state["signal_weights"])
    shrink = min(1.0, len(rates) / 12)  # 표본 크기 비례 수축 — 소표본의 스퓨리어스 상관으로
    corrs = {}                          # 가중치가 요동치는 것을 막는다(하네스 실측으로 필요 확인)
    gran = {s["id"]: s.get("acquisition", {}).get("granularity", "")
            for s in data["conversion_signals"]}
    held = []
    for sid in old:
        # 크리에이터 귀속이 가능한 신호(acquisition.granularity=per_*)만 재구매와 상관 검정.
        # 커뮤니티 단위 신호를 크리에이터별로 누적하면 '게시 재직 기간' 아티팩트가 선정
        # 순서와 얽혀 가짜 유의 상관을 만든다(하네스 실측: r=-0.84의 교란). 커뮤니티 단위
        # 재구매 데이터가 생기기 전까지 해당 신호의 예측력 검정은 보류 — 판단 불가 원칙.
        if not gran.get(sid, "").startswith("per_"):
            corrs[sid] = old[sid]
            held.append(sid)
            continue
        xs = []
        for cid in rates:  # 주당 평균으로 정규화 — 게시 주차 수가 다른 크리에이터 간 비교
            a = state["signal_agg"].get(cid, {})
            xs.append(a.get(sid, 0.0) / max(a.get("weeks", 1), 1))
        ys = list(rates.values())
        if statistics.stdev(xs) > 0 and statistics.stdev(ys) > 0:
            r = statistics.correlation(xs, ys)
            n = len(xs)
            t = abs(r) * ((n - 2) / max(1 - r * r, 1e-9)) ** 0.5
            # 유의성 게이트: 유의하지 않으면 '예측력 없음'(바닥값). max(r, 바닥) 클리핑만
            # 쓰면 음의 스퓨리어스 상관은 눌리고 양은 살아남는 비대칭 때문에 무상관
            # 신호도 절반 확률로 가중치가 오른다(하네스 실측: 방향 정답률 50%).
            evidence = max(r, 0.05) if (t > 2 and r > 0) else 0.05
            corrs[sid] = shrink * evidence + (1 - shrink) * old[sid]
        else:
            corrs[sid] = old[sid]  # 신호 변동 없음 — 예측력 판정 불가, 기존값 유지
    total = sum(corrs.values()) or 1.0
    state["signal_weights"] = {k: round(v / total, 4) for k, v in corrs.items()}
    note = f" · 검정 보류(귀속 불가): {', '.join(held)}" if held else ""
    print(f"   분기 보정 — 누적 재구매 {len(rates)}건 · 선행 가중치 {old} → {state['signal_weights']}{note}")
    return rates


# ── 수요 증거 패키지 ─────────────────────────────────────────────

def evidence_package(design, data, state, obs, detected, alloc, undecidable, out_dir):
    lines = [f"# 수요 증거 패키지 — W{state['week']:02d}", "",
             "## 커뮤니티별 검색 리프트 (처치=게시 후 / 대조=게시 전 시차)", ""]
    for cid, v in sorted(obs["search"].items()):
        lines.append(f"- {cid}: {(v['post'] - v['pre']) / v['pre']:+.1%}")
    lines += ["", "## 셀스루 (처치 vs 대조 지점)", ""]
    for pg, v in sorted(obs["sellthrough"].items()):
        lines.append(f"- {pg}: 처치 {v['treatment']} vs 대조 {v['control']}")
    lines += ["", "## 검출 판정", ""]
    lines += [f"- {n}: 평균 {l:+.1%} (t={t:.1f})" for n, l, t in detected] or ["- 검출된 리프트 없음"]
    lines += ["", "## 차주 예산 재배분", ""]
    lines += [f"- {k}: {v:,}원" for k, v in sorted(alloc.items(), key=lambda x: -x[1])]
    if undecidable:
        lines += ["", "## 판단 불가 항목 (추측 금지 — 필요한 데이터 명시)", ""]
        lines += [f"- {t}: {r} → {need}" for t, r, need in undecidable]
    out = out_dir / "evidence_package.md"
    out.write_text("\n".join(lines))
    print(f"   수요 증거 패키지 저장: {out.name} (판단 불가 {len(undecidable)}건 명시)")


# ── 메인 ─────────────────────────────────────────────────────────

def run_week(data, state, state_dir, rng, args):
    product = next((p for p in data["products"] if p["id"] == args.product), None)
    if product is None:
        print(f"판단 불가: 대상 제품 {args.product} 이 온톨로지에 없음")
        raise SystemExit(EXIT_UNDECIDABLE)
    out_dir = state_dir / f"week_{state['week']:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    design, undecidable = design_experiment(data, state, product, rng)
    if design is None:
        raise SystemExit(EXIT_UNDECIDABLE)
    state["batches"].append({"week": design["week"], "product": design["product"],
                             "offsets": design["offsets"],
                             "selected": [c["id"] for c in design["selected"]]})
    (out_dir / "assignment.json").write_text(json.dumps(
        {"assignments": [{"pair_group": a["pair_group"], "treatment": a["treatment"]["id"],
                          "control": a["control"]["id"]} for a in design["assignments"]],
         "offsets": design["offsets"], "codes": design["codes"],
         "excluded_signals": design["excluded_signals"]}, ensure_ascii=False, indent=2))
    generate_briefs(design, data, product, out_dir)
    obs, published = collect_signals(design, data, state, rng, args.scenario)
    detected = detect_lift(obs)
    alloc = weekly_update(design, data, state, obs, published, args.compress)
    quarterly_calibration(data, state, rng, args.scenario)
    evidence_package(design, data, state, obs, detected, alloc, undecidable, out_dir)
    state["week"] += 1
    save_state(state_dir, state)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=HERE.parent / "data" / "mock" / "mock_data.json")
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--weeks", type=int, default=1)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scenario", choices=["effective", "null", "decor_code"], default="effective")
    ap.add_argument("--selection", choices=["score", "random"], default="score",
                    help="random = 학습 없는 무작위 선정(검증 하네스의 베이스라인)")
    ap.add_argument("--compress", type=float, default=1.0,
                    help="소모 주기 압축 배율(시뮬레이션용, 기획서 질문 5)")
    ap.add_argument("--budget", type=int, default=10_000_000)
    ap.add_argument("--product", default="pr_001")
    ARGS = ap.parse_args()

    data = load_ontology(ARGS.data)
    ARGS.state_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(ARGS.state_dir, data["conversion_signals"])
    for i in range(ARGS.weeks):
        print(f"\n=== 주간 회전 {i + 1}/{ARGS.weeks} (W{state['week']:02d}) ===")
        rng = random.Random(ARGS.seed * 1000 + state["week"])
        run_week(data, state, ARGS.state_dir, rng, ARGS)
    print("\n파이프라인 정상 종료")


if __name__ == "__main__":
    main()
