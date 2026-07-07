#!/usr/bin/env python3
"""모의 신호 어댑터 — 파이프라인 ④(신호 수집)의 데이터 소스.

실서비스에서는 이 모듈이 온톨로지 conversion_signal.acquisition에 명시된
실경로(검색 트렌드 API, 부대별 발주량, 추적 코드 집계, 자사 주문 데이터)로
교체된다. 파이프라인은 이 인터페이스(simulate_week / repurchase_rate)만 안다.

설계 원칙: 숨은 실효과(ground truth)를 크리에이터 유형 × 커뮤니티 밀도로
심어 두고 노이즈를 얹어 신호를 생성한다. 그래야 검증 하네스가 '루프가 노이즈가
아니라 진짜 효과를 학습하는가'를 검사할 수 있다(기획서 질문 5).
scenario="null"이면 실효과·처치 효과 전부 0 — '효과 없는 데이터에서
효과 없음을 내는가' 검증용. scenario="decor_code"면 추적 코드 신호만
실효과와 무상관(고정 유사효과)으로 생성 — '선행 신호와 재구매의 상관을
일부러 다르게 심었을 때 분기 보정이 가중치를 올바르게 낮추는가'(프록시
보정) 검증용. 재구매는 여전히 실효과를 따른다.
"""

BASE_EFFECT = {"px_item_reviewer": 0.35, "veteran_vlogger": 0.25, "enlistment_prep": 0.15}


def true_effect(creator, scenario):
    """숨은 실효과. 파이프라인은 이 값을 절대 직접 읽지 않는다 — 신호로만 관측."""
    if scenario == "null":
        return 0.0
    digits = "".join(ch for ch in creator["id"] if ch.isdigit()) or "0"
    jitter = (int(digits) % 7) * 0.01
    return BASE_EFFECT.get(creator["creator_type"], 0.10) + jitter


def simulate_week(published, communities, pair_assignments, rng, scenario):
    """이번 주 선행 신호 관측치 생성.

    published: 이번 주까지 게시 완료된 크리에이터 목록(시차 설계 반영)
    pair_assignments: [(pair_group, treatment_tp, control_tp)]
    반환: {"search": {com_id: {"pre", "post"}}, "code": {cr_id: n},
           "sellthrough": {pair_group: {"treatment", "control"}}}
    """
    ambient = sum(true_effect(c, scenario) for c in published) * 0.4  # 전국 도달 → 모든 커뮤니티에 동일 노출
    obs = {"search": {}, "code": {}, "sellthrough": {}}

    for com in communities:
        pre = 100.0 * (1 + rng.gauss(0, 0.03))
        post = pre * (1 + ambient * com["wom_density"] + rng.gauss(0, 0.03))
        obs["search"][com["id"]] = {"pre": round(pre, 1), "post": round(post, 1)}

    for c in published:
        code_eff = 0.15 if scenario == "decor_code" else true_effect(c, scenario)
        mean = c["avg_views"] * 0.008 * (code_eff + 0.02)  # 조회수의 ~0.8%가 코드 사용
        obs["code"][c["id"]] = max(0, int(rng.gauss(mean, mean ** 0.5 + 1)))

    promo = 0.0 if scenario == "null" else 0.25  # 지점 처치(샘플링·프로모션) 자체 효과
    for pg, t_tp, c_tp in pair_assignments:
        base_t = t_tp["weekly_traffic"] * 0.008
        base_c = c_tp["weekly_traffic"] * 0.008
        obs["sellthrough"][pg] = {
            "treatment": max(0, int(base_t * (1 + promo + ambient * 0.5) * (1 + rng.gauss(0, 0.05)))),
            "control": max(0, int(base_c * (1 + rng.gauss(0, 0.05)))),
        }
    return obs


def repurchase_rate(creator, rng, scenario):
    """후행 신호: 소모 주기 경과 후 도착하는 재구매율. 실효과와 상관되게 생성 —
    분기 루프가 '선행 신호의 예측력'을 보정할 근거."""
    rate = 0.04 + 0.6 * true_effect(creator, scenario) + rng.gauss(0, 0.01)
    return round(min(1.0, max(0.0, rate)), 4)
