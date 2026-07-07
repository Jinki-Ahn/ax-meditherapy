#!/usr/bin/env python3
"""Layer 2 — 적대적 LLM 리뷰 런처 (분리 프로세스로 실행됨).

review_deliverables.py가 변경 산출물 목록과 함께 이 스크립트를 분리(detached) 프로세스로
띄운다. 하는 일: reviewer.md 루브릭 + 변경 산출물 본문을 하나의 프롬프트로 묶어,
헤드리스 CLI(claude 우선, 없으면 codex)에 stdin으로 투입하고, 심사 결과를
reviews/review__<ts>.md 에 남긴다.

**모든 CLI 불확실성을 이 파일 하나에 격리**한다. 플래그가 틀리거나 CLI가 없어도
크래시하지 않고 reviews/review__<ts>__llm-skipped.md 스텁을 남긴다(절대 raise 안 함).
읽기 전용으로 돈다(--permission-mode plan): 산출물을 편집하지 않아 리뷰 루프를 만들지 않는다.
TRIP_REVIEW_ACTIVE=1 환경에서 실행되므로, 이 프로세스가 유발하는 어떤 Stop 훅도 무시된다.
"""
import argparse
import os
import shutil
import subprocess
import sys

MAX_FILE_BYTES = 60000  # 산출물 한 개당 프롬프트에 넣을 상한(과금·컨텍스트 보호)
CLI_TIMEOUT = 300       # 초


def build_prompt(root, changed):
    reviewer = os.path.join(root, "tools", "reviewer.md")
    parts = [open(reviewer, encoding="utf-8").read(), "", "---", ""]
    for rel in changed:
        path = os.path.join(root, rel)
        parts.append(f"===== FILE: {rel} =====")
        try:
            data = open(path, encoding="utf-8", errors="replace").read()
            if len(data.encode("utf-8")) > MAX_FILE_BYTES:
                data = data[: MAX_FILE_BYTES // 2] + "\n...[중략: 파일이 길어 잘림]...\n"
            parts.append(data)
        except Exception as exc:  # noqa: BLE001
            parts.append(f"[읽기 실패: {exc}]")
        parts.append("")
    return "\n".join(parts)


def try_claude(prompt):
    if not shutil.which("claude"):
        return None
    cmd = [
        "claude", "-p",
        "--permission-mode", "plan",       # 읽기 전용 — 편집 불가
        "--disallowed-tools", "Edit", "Write", "NotebookEdit",
        "--output-format", "text",
    ]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr.strip()[:500]}")


def try_codex(prompt):
    if not shutil.which("codex"):
        return None
    # codex 비대화 실행. 정확한 플래그는 대상 머신에서 `codex exec --help`로 확인 후 조정.
    cmd = ["codex", "exec", "-"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    raise RuntimeError(f"codex exit {proc.returncode}: {proc.stderr.strip()[:500]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ts", required=True)
    parser.add_argument("--changed", nargs="*", default=[])
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    ok_path = os.path.join(args.out, f"review__{args.ts}.md")
    skip_path = os.path.join(args.out, f"review__{args.ts}__llm-skipped.md")

    if not args.changed:
        return 0

    try:
        prompt = build_prompt(args.root, args.changed)
    except Exception as exc:  # noqa: BLE001
        _write(skip_path, f"# LLM 리뷰 스킵\n\n프롬프트 구성 실패: {exc}\n")
        return 0

    header = f"# 적대적 LLM 리뷰 — {args.ts}\n\n변경 산출물: {', '.join(args.changed)}\n\n---\n\n"
    errors = []
    for name, fn in (("claude", try_claude), ("codex", try_codex)):
        try:
            out = fn(prompt)
            if out is None:
                continue  # CLI 미설치 → 다음 후보
            _write(ok_path, header + f"_리뷰어: {name}_\n\n" + out)
            return 0
        except Exception as exc:  # noqa: BLE001 - 다음 후보로
            errors.append(f"{name}: {exc}")

    reason = "; ".join(errors) if errors else "PATH에 claude/codex CLI 없음"
    _write(skip_path, f"# LLM 리뷰 스킵\n\n{reason}\n\n"
                      f"(Layer 1 결정론 검토는 layer1__{args.ts}.md 참조)\n")
    return 0


def _write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception as exc:  # noqa: BLE001
        print(f"run_llm_review: 결과 기록 실패: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 분리 프로세스라도 조용히 죽는다
        print(f"run_llm_review: {exc}", file=sys.stderr)
        sys.exit(0)
