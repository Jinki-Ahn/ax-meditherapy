#!/usr/bin/env python3
"""Stop-hook 리뷰 하네스 — Layer 1 디스패처 (결정론, 인라인, 빠름).

매 턴 Stop 훅으로 자동 실행된다. 하는 일:
  1. stdin 훅 JSON(transcript_path/cwd/session_id) 파싱 — save_log.py와 동일 계약.
  2. 루프 가드: 환경변수 TRIP_REVIEW_ACTIVE가 있으면 즉시 종료(리뷰가 리뷰를 재유발 방지).
  3. review.config.json의 산출물 glob을 확장(reviews/·logs/ 등 제외).
  4. reviews/.manifest.json(경로→sha256)로 변경된 산출물만 추림.
     첫 실행은 베이스라인만 기록하고 리뷰하지 않는다(턴1 전량 리뷰 소음 방지).
  5. Layer 1 결정론 검사(글자수·죽은 링크·인용 URL·마커·문서-코드 대조·py_compile·시나리오)
     를 변경분에 대해 실행하고 reviews/layer1__<ts>.{json,md}로 기록.
  6. Layer 2(적대적 LLM 리뷰)를 분리 프로세스로 띄우고 즉시 종료(훅에 LLM 지연 0).

save_log.py 계약을 그대로 따른다: stdout에 절대 쓰지 않고(Codex가 Stop 훅 stdout을
결정으로 파싱), 무슨 일이 있어도 exit 0(리뷰 실패가 세션을 막지 않게).
편집·실행할 필요 없는 파일이다 — 훅이 자동으로 부른다.
"""
import argparse
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

URL_RE = re.compile(r"https?://[^\s\)\]\}<>\"']+")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SECTION_RE = re.compile(r"^##\s+(문항\s*\d+)", re.MULTILINE)


def log(msg):
    """진단은 stderr로만. stdout은 절대 건드리지 않는다."""
    print(f"review_deliverables: {msg}", file=sys.stderr)


def project_root(cwd):
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or cwd
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def load_config(root):
    path = os.path.join(root, "review.config.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def is_excluded(relpath, patterns):
    norm = relpath.replace(os.sep, "/")
    for pat in patterns:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, pat.rstrip("/*") + "/*"):
            return True
        # match a leading directory segment for patterns like "logs/**"
        base = pat.split("/**", 1)[0].rstrip("/")
        if base and (norm == base or norm.startswith(base + "/")):
            return True
    return False


def expand_deliverables(root, config):
    """설정 glob을 실제 파일 목록으로 확장하고 카테고리 태그를 붙인다."""
    exclude = config.get("exclude", [])
    tagged = {}  # relpath -> category
    for category in ("docs", "code"):
        spec = config["deliverables"].get(category, {})
        for pattern in spec.get("globs", []):
            for abspath in glob.glob(os.path.join(root, pattern), recursive=True):
                if not os.path.isfile(abspath):
                    continue
                rel = os.path.relpath(abspath, root)
                if is_excluded(rel, exclude):
                    continue
                tagged.setdefault(rel, category)
    return tagged


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ─────────────────────────── Layer 1 검사들 ───────────────────────────
def _split_sections(text):
    """'## 문항 N ...' 기준으로 (라벨, 본문) 목록. 라벨은 '문항 N'."""
    matches = list(SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((label, text[start:end]))
    return out


def _prose_len(body):
    """본문 글자수. 하위 '### 출처'/'### 근거' 블록은 별도 필드로 보고 제외."""
    cut = re.split(r"^###\s+(출처|근거)", body, maxsplit=1, flags=re.MULTILINE)[0]
    return len(cut.strip())


def check_char_limits(root, rel, config, findings):
    limits = config.get("char_limits", {}).get(os.path.basename(rel))
    if not isinstance(limits, dict):
        return
    near = config.get("char_limits", {}).get("_near_limit_ratio", 0.95)
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    sections = dict(_split_sections(text))
    for label, limit in limits.items():
        if label.startswith("_") or not isinstance(limit, int):
            continue
        body = sections.get(label)
        if body is None:
            findings.append(("char_limits", "WARN", rel, f"{label} 섹션을 못 찾음"))
            continue
        n = _prose_len(body)
        if n > limit:
            findings.append(("char_limits", "ERROR", rel, f"{label} {n}자 > {limit}자 (초과 {n - limit})"))
        elif n >= limit * near:
            findings.append(("char_limits", "WARN", rel, f"{label} {n}자 (제한 {limit}자에 근접)"))
        else:
            findings.append(("char_limits", "PASS", rel, f"{label} {n}/{limit}자"))


def check_dead_links(root, rel, findings):
    doc_dir = os.path.dirname(os.path.join(root, rel))
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    bad = []
    for target in MD_LINK_RE.findall(text):
        t = target.strip().split()[0]  # drop optional "title"
        if t.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = os.path.normpath(os.path.join(doc_dir, t.split("#", 1)[0]))
        if not os.path.exists(resolved):
            bad.append(t)
    if bad:
        findings.append(("dead_links", "ERROR", rel, f"깨진 로컬 링크 {len(bad)}개: {bad[:5]}"))
    else:
        findings.append(("dead_links", "PASS", rel, "로컬 링크 정상"))


def check_citation_urls(root, rel, findings):
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    has_src_section = bool(re.search(r"(출처|근거 링크|근거)\b", text))
    urls = URL_RE.findall(text)
    if has_src_section and not urls:
        findings.append(("citation_urls", "ERROR", rel, "출처/근거 섹션이 있으나 URL 없음"))
    if re.search(r"\(\s*URL\s*\)", text) and not urls:
        findings.append(("citation_urls", "WARN", rel, "'(URL)' 자리표시자만 있음"))


def check_markers(root, rel, config, findings):
    markers = config.get("markers", [])
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    hits = []
    for line in text.splitlines():
        # 완료 체크박스 '[x] ~~...~~'는 정상(완료 기록)이므로 마커 오탐 제외
        if line.lstrip().startswith("- [x]"):
            continue
        for mk in markers:
            if mk in line:
                hits.append(mk)
    hits = sorted(set(hits))
    if hits:
        findings.append(("markers", "WARN", rel, f"미완료/미검증 마커: {hits}"))
    else:
        findings.append(("markers", "PASS", rel, "잔여 마커 없음"))


def check_url_wellformed(root, rel, findings):
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    strict = re.compile(r"^https?://[\w.-]+(:\d+)?(/[^\s]*)?$")
    malformed = [u for u in URL_RE.findall(text) if not strict.match(u)]
    if malformed:
        findings.append(("url_wellformed", "WARN", rel, f"형식 의심 URL {len(malformed)}개: {malformed[:3]}"))
    else:
        findings.append(("url_wellformed", "PASS", rel, "URL 형식 정상"))


def check_py_compile(root, rel, findings):
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.join(root, rel)],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        findings.append(("py_compile", "PASS", rel, "컴파일 OK"))
    else:
        findings.append(("py_compile", "ERROR", rel, proc.stderr.strip().splitlines()[-1:] or "컴파일 실패"))


def check_run_scenarios(root, config, findings):
    rel = config["cross_doc_assertions"]["check_count"]["run_scenarios"]
    script = os.path.join(root, rel)
    if not os.path.isfile(script):
        findings.append(("run_scenarios", "WARN", rel, "run_scenarios.py 없음"))
        return None
    proc = subprocess.run(
        [sys.executable, script], cwd=os.path.dirname(script),
        capture_output=True, text=True,
    )
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*checks passed", proc.stdout)
    passed = int(m.group(1)) if m else None
    total = int(m.group(2)) if m else None
    if proc.returncode == 0 and passed == total and total:
        findings.append(("run_scenarios", "PASS", rel, f"{passed}/{total} 통과"))
    else:
        findings.append(("run_scenarios", "ERROR", rel, f"exit {proc.returncode}, {passed}/{total}"))
    return total


def check_cross_doc(root, config, findings, scenario_total):
    # (a) run_scenarios 실측 개수 ↔ 문서의 '체크 N개' 주장 대조
    claim_re = re.compile(config["cross_doc_assertions"]["check_count"]["docs_claim_regex"])
    if scenario_total is not None:
        for rel in ("제출용_답변_최종.md", "submission/README.md"):
            p = os.path.join(root, rel)
            if not os.path.isfile(p):
                continue
            claims = {int(g) for m in claim_re.finditer(open(p, encoding="utf-8").read()) for g in m.groups() if g}
            mismatched = {c for c in claims if c != scenario_total}
            if mismatched:
                findings.append(("cross_doc", "ERROR", rel,
                                 f"문서가 주장한 체크 수 {sorted(mismatched)} ≠ 실측 {scenario_total}"))
            elif claims:
                findings.append(("cross_doc", "PASS", rel, f"체크 수 {scenario_total} 일치"))
    # (b) 두 문서의 출처 URL 집합 일치
    files = config["cross_doc_assertions"]["source_urls_match"]["files"]
    url_sets = {}
    for rel in files:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            url_sets[rel] = set(URL_RE.findall(open(p, encoding="utf-8").read()))
    if len(url_sets) == 2:
        a, b = list(url_sets.values())
        only = (a - b) | (b - a)
        # 답변이 기획서의 부분집합이면(기획서가 더 많은 근거) 정상으로 본다
        f0, f1 = files
        if url_sets[f1] - url_sets[f0]:
            findings.append(("cross_doc", "WARN", f1,
                             f"답변에만 있고 기획서에 없는 URL {len(url_sets[f1] - url_sets[f0])}개"))
        else:
            findings.append(("cross_doc", "PASS", f1, "답변 출처가 기획서 근거의 부분집합"))


def run_layer1(root, config, changed):
    findings = []
    docs = [r for r, c in changed.items() if c == "docs"]
    codes = [r for r, c in changed.items() if c == "code"]
    for rel in docs:
        check_char_limits(root, rel, config, findings)
        check_dead_links(root, rel, findings)
        check_citation_urls(root, rel, findings)
        check_markers(root, rel, config, findings)
        check_url_wellformed(root, rel, findings)
    for rel in codes:
        check_py_compile(root, rel, findings)
    scenario_total = None
    if codes or docs:
        scenario_total = check_run_scenarios(root, config, findings)
        check_cross_doc(root, config, findings, scenario_total)
    return findings


def write_layer1_report(reviews_dir, ts, changed, findings):
    os.makedirs(reviews_dir, exist_ok=True)
    counts = {lvl: sum(1 for f in findings if f[1] == lvl) for lvl in ("ERROR", "WARN", "PASS")}
    payload = {
        "timestamp": ts,
        "changed": changed,
        "counts": counts,
        "findings": [{"check": c, "level": l, "file": f, "detail": d} for c, l, f, d in findings],
    }
    with open(os.path.join(reviews_dir, f"layer1__{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    lines = [f"# Layer 1 결정론 검토 — {ts}", "",
             f"변경 산출물: {', '.join(changed) or '(없음)'}",
             f"결과: ERROR {counts['ERROR']} · WARN {counts['WARN']} · PASS {counts['PASS']}", ""]
    for level in ("ERROR", "WARN", "PASS"):
        rows = [f for f in findings if f[1] == level]
        if not rows:
            continue
        lines.append(f"## {level}")
        for check, lvl, file, detail in rows:
            lines.append(f"- **{check}** `{file}` — {detail}")
        lines.append("")
    with open(os.path.join(reviews_dir, f"layer1__{ts}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def launch_layer2(root, reviews_dir, ts, changed_paths):
    """Layer 2(적대적 LLM 리뷰)를 분리 프로세스로 띄우고 즉시 반환한다."""
    launcher = os.path.join(root, "tools", "run_llm_review.py")
    if not os.path.isfile(launcher):
        return
    env = dict(os.environ)
    env["TRIP_REVIEW_ACTIVE"] = "1"  # 자식/그 훅에 상속 → 리뷰 재유발 차단
    args = [sys.executable, launcher, "--root", root, "--out", reviews_dir, "--ts", ts,
            "--changed", *changed_paths]
    try:
        subprocess.Popen(
            args, cwd=root, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # 파이썬 내장 setsid — 훅보다 오래 살아남음(macOS 이식)
        )
    except Exception as exc:  # noqa: BLE001 - 비치명
        log(f"Layer 2 실행 실패(무시): {exc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", choices=["claude-code", "codex"], default="claude-code")
    args, _ = parser.parse_known_args()

    # 루프 가드 (1차: 프로세스 진입 즉시)
    if os.environ.get("TRIP_REVIEW_ACTIVE"):
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - stdin이 비거나 깨져도 세션을 막지 않는다
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()
    root = project_root(cwd)

    try:
        config = load_config(root)
    except Exception as exc:  # noqa: BLE001
        log(f"설정 로드 실패(무시): {exc}")
        return 0

    reviews_dir = os.path.join(root, "reviews")
    manifest_path = os.path.join(reviews_dir, ".manifest.json")

    try:
        tagged = expand_deliverables(root, config)
        current = {rel: sha256_file(os.path.join(root, rel)) for rel in tagged}

        first_run = not os.path.isfile(manifest_path)
        old = {}
        if not first_run:
            try:
                old = json.load(open(manifest_path, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                old = {}

        changed = {rel: tagged[rel] for rel, h in current.items() if old.get(rel) != h}

        # 감지 직후 매니페스트 즉시 갱신(리뷰 성공과 무관) → 무변경 턴은 아무것도 안 함
        os.makedirs(reviews_dir, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=2, sort_keys=True)

        if first_run:
            log("첫 실행 — 베이스라인 매니페스트만 기록, 리뷰 생략")
            return 0
        if not changed:
            return 0

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        findings = run_layer1(root, config, changed)
        write_layer1_report(reviews_dir, ts, list(changed), findings)
        launch_layer2(root, reviews_dir, ts, list(changed))
    except Exception as exc:  # noqa: BLE001 - 무슨 일이 있어도 세션을 막지 않는다
        log(f"리뷰 중 오류(무시): {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
