#!/usr/bin/env python3
"""모의 데이터 생성기 — BaseCamp Loop 온톨로지 인스턴스 생성.

기획서 질문 5의 시뮬레이션 스케일(크리에이터 30 × 커뮤니티 12단위)로
온톨로지 스키마에 적합한 모의 데이터를 결정론적으로 생성한다(--seed로 재현).
신호 정의(conversion_signals)는 세계 상태가 아니라 측정 설정이므로 seed.json의
정의를 그대로 복사한다.

사용: python3 generate_mock_data.py [--seed 42] [--out ../data/mock/mock_data.json]
"""

import argparse
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
ONTOLOGY_DIR = HERE.parent / "data" / "ontology"

N_COMMUNITIES = 12
N_CREATORS = 30
TP_PER_COMMUNITY = 2

CREATOR_TYPES = [
    ("veteran_vlogger", "youtube", 0.4),
    ("px_item_reviewer", "youtube_shorts", 0.35),
    ("enlistment_prep", "youtube", 0.25),
]

PRODUCTS = [
    {
        "id": "pr_001",
        "name": "옴므 올인원 크림 (가상)",
        "category": "skincare",
        "price_krw": 18000,
        "consumption_cycle_days": 45,
        "appeal_points": [
            {"id": "ap_001", "claim": "세안 후 이거 하나로 끝"},
            {"id": "ap_002", "claim": "환절기 각질 진정"},
        ],
        "forbidden_claims": ["피부 질환 치료", "여드름 완치", "부대·군 공식 추천 암시"],
        "required_disclosure": ["유료 광고 포함 표기", "공정위 추천·보증 심사지침 준수 문구"],
        "available_channels": ["coupang", "oliveyoung", "px"],
    },
    {
        "id": "pr_002",
        "name": "쿨링 폼 클렌저 (가상)",
        "category": "skincare",
        "price_krw": 12000,
        "consumption_cycle_days": 60,
        "appeal_points": [
            {"id": "ap_003", "claim": "훈련 후 유분 한 번에"},
            {"id": "ap_004", "claim": "쿨링감으로 여름 세안"},
        ],
        "forbidden_claims": ["피부 질환 치료", "부대·군 공식 추천 암시"],
        "required_disclosure": ["유료 광고 포함 표기"],
        "available_channels": ["coupang", "px"],
    },
]


def gen_communities(rng: random.Random):
    communities = []
    all_touchpoints = []  # (community_idx, tp dict) — pair_group은 전체 생성 후 배정
    for i in range(1, N_COMMUNITIES + 1):
        com = {
            "id": f"com_mil_{i:02d}",
            "name": f"제{i}지역 부대 생활권 (가상)",
            "slot_type": "military",
            "measurement_unit": "부대",
            "wom_density": round(rng.uniform(0.55, 0.9), 2),
            "population": rng.randrange(5000, 12001, 500),
            "contamination_risk": round(rng.uniform(0.05, 0.35), 2),
            "post_exit_channel": "coupang",
            "prior_score": 0.5,
            "touchpoints": [],
        }
        for j in range(1, TP_PER_COMMUNITY + 1):
            traffic = rng.randrange(600, 3001, 50)
            # 대부분 pr_001 취급, 일부는 2종 모두, 일부는 미취급(입점 비전제 — 기획서 한계)
            roll = rng.random()
            if roll < 0.15:
                carried = []
            elif roll < 0.45:
                carried = ["pr_001", "pr_002"]
            else:
                carried = ["pr_001"]
            tp = {
                "id": f"tp_px_{i:02d}{j}",
                "name": f"PX {i:02d}-{j}지점",
                "type": "px_store",
                "pair_group": "",  # 아래에서 배정
                "weekly_traffic": traffic,
                "carried_product_ids": carried,
            }
            com["touchpoints"].append(tp)
            all_touchpoints.append(tp)
        communities.append(com)

    # 유사 지점 쌍 묶기(기획서 질문 3 ②): 주간 방문 규모로 정렬해 인접 지점끼리 쌍.
    # 같은 pair_group 안에서 처치/대조를 배정한다.
    all_touchpoints.sort(key=lambda t: t["weekly_traffic"])
    for k in range(0, len(all_touchpoints), 2):
        group = f"pg_{k // 2 + 1:02d}"
        for tp in all_touchpoints[k : k + 2]:
            tp["pair_group"] = group
    return communities


def gen_creators(rng: random.Random):
    creators = []
    for i in range(1, N_CREATORS + 1):
        r = rng.random()
        acc = 0.0
        for ctype, platform, w in CREATOR_TYPES:
            acc += w
            if r <= acc:
                break
        reach = int(rng.lognormvariate(10.5, 0.9))  # 수천~수십만 분포
        reach = max(2000, min(reach, 800000))
        views_ratio = rng.uniform(0.6, 2.5) if platform == "youtube_shorts" else rng.uniform(0.1, 0.5)
        creators.append({
            "id": f"cr_{i:03d}",
            "name": f"크리에이터 {i:03d} (가상)",
            "creator_type": ctype,
            "platform": platform,
            "reach_scope": "national",
            "reach_size": reach,
            "avg_views": int(reach * views_ratio),
            "commercially_eligible": True,
            "brand_safety_flags": [],
            "cost_per_seeding": int(reach * rng.uniform(5, 12) // 1000 * 1000),
            "prior_score": 0.5,
        })
    # 예외 케이스를 의도적으로 심는다:
    # 현역(영리 금지 — 하드 제외) 3명, 브랜드 안전성 검토 대상 2명
    for idx in rng.sample(range(N_CREATORS), 3):
        c = creators[idx]
        c["creator_type"] = "active_duty"
        c["commercially_eligible"] = False
        c["brand_safety_flags"] = ["active_duty_no_commerce"]
        c["reach_scope"] = "local"
        c["cost_per_seeding"] = 0
        c["prior_score"] = 0.0
    flagged = [i for i in range(N_CREATORS) if creators[i]["commercially_eligible"]]
    for idx in rng.sample(flagged, 2):
        creators[idx]["brand_safety_flags"] = [rng.choice(["past_controversy", "competitor_partnership"])]
    return creators


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=HERE.parent / "data" / "mock" / "mock_data.json")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    signals = json.loads((ONTOLOGY_DIR / "seed.json").read_text())["conversion_signals"]
    data = {
        "_comment": f"모의 생성 데이터 (generate_mock_data.py --seed {args.seed}). 전부 가상.",
        "communities": gen_communities(rng),
        "creators": gen_creators(rng),
        "products": PRODUCTS,
        "conversion_signals": signals,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    n_tp = sum(len(c["touchpoints"]) for c in data["communities"])
    print(f"생성 완료: {args.out}")
    print(f"  커뮤니티 {len(data['communities'])} · 접점 {n_tp} · 크리에이터 {len(data['creators'])}"
          f" · 제품 {len(data['products'])} · 신호 {len(data['conversion_signals'])}")


if __name__ == "__main__":
    main()
