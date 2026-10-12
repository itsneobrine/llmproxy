"""
summarize.py — сводка по llm_records.jsonl и прототип атрибуции AI-строк.

Примеры (PowerShell):
  python summarize.py
  python summarize.py --repo C:\\path\\to\\repo                 # базовая ветка определяется сама
  python summarize.py --repo C:\\path\\to\\repo --base origin/develop
  python summarize.py --repo C:\\path\\to\\repo --last 1        # только последний коммит

Без --repo печатает сводку по сессиям: запросы, модели, токены, tool calls,
проблемы формата. С --repo дополнительно сравнивает добавленные строки
`git diff <base>...HEAD` с фрагментами из edit tool calls и считает долю AI-строк
(то же правило, что в главе 5 документа, но по точному совпадению строки).
"""
import argparse
import collections
import hashlib
import json
import os
import pathlib
import subprocess

DEFAULT_DIR = pathlib.Path(os.environ.get(
    "MITM_LOG_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "mitm-llm")))

SKIP_LINES = {"{", "}", "(", ")", "[", "]", "};", "*/", "/*", "else", "else {", "} else {", "end"}


def line_hash(line: str) -> str:
    return hashlib.sha1(line.strip().encode()).hexdigest()[:16]


def load(path: pathlib.Path) -> list:
    if not path.exists():
        raise SystemExit(f"нет файла {path}")
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def summarize(records: list) -> dict:
    sessions = collections.defaultdict(lambda: {
        "requests": 0, "models": collections.Counter(), "input": 0, "output": 0, "cached": 0,
        "tools": collections.Counter(), "fragments": 0, "problems": collections.Counter(),
        "repo": None, "jira": None, "tool": None})
    for r in records:
        s = sessions[r.get("session_id") or "(без сессии)"]
        s["requests"] += 1
        s["models"][r.get("routed_model") or r.get("model_requested") or "?"] += 1
        s["input"] += r.get("input_tokens") or 0
        s["output"] += r.get("output_tokens") or 0
        s["cached"] += r.get("cached_tokens") or 0
        s["tools"].update(r.get("tool_calls") or [])
        s["fragments"] += sum(f["lines_count"] for f in r.get("edit_fragments") or [])
        s["problems"].update(r.get("format_problems") or [])
        s["repo"] = s["repo"] or r.get("repo")
        s["jira"] = s["jira"] or r.get("jira_key")
        s["tool"] = s["tool"] or r.get("tool")
    return sessions


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def detect_base(repo: str) -> str | None:
    """Базовая ветка: ветка по умолчанию origin, иначе первая существующая из типовых."""
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    candidates = ([head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []) + [
        "main", "master", "develop", "origin/main", "origin/master", "origin/develop"]
    for c in candidates:
        if git(repo, "rev-parse", "--verify", "--quiet", c + "^{commit}").returncode == 0:
            return c
    return None


def added_lines(repo: str, rev_range: str) -> list:
    r = git(repo, "diff", "--unified=0", rev_range)
    if r.returncode != 0:
        raise SystemExit(f"git diff {rev_range} не выполнился: {r.stderr.strip()}\n"
                         f"Укажите базовую ветку явно (--base origin/<ветка>) или сравните последние коммиты (--last N).")
    out = []
    for l in r.stdout.splitlines():
        if l.startswith("+") and not l.startswith("+++"):
            code = l[1:]
            if code.strip() and code.strip() not in SKIP_LINES:
                out.append(code)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--repo")
    ap.add_argument("--base", help="базовая ветка; по умолчанию определяется автоматически")
    ap.add_argument("--last", type=int, help="сравнить последние N коммитов вместо ветки с базой")
    a = ap.parse_args()

    records = load(pathlib.Path(a.dir) / "llm_records.jsonl")
    for sid, s in summarize(records).items():
        print(f"\n=== сессия {sid}  tool={s['tool']}  repo={s['repo']}  jira={s['jira']}")
        print(f"запросов: {s['requests']}; модели: {dict(s['models'])}")
        print(f"токены: вход {s['input']} (из кэша {s['cached']}), выход {s['output']}")
        print(f"tool calls: {dict(s['tools'])}; строк кода в edit-фрагментах: {s['fragments']}")
        for p, n in s["problems"].items():
            print(f"  ! {p} — {n} запрос(ов)")

    if a.repo:
        ai_hashes = {h for r in records for f in r.get("edit_fragments") or [] for h in f["line_hashes"]}
        if a.last:
            rev_range = f"HEAD~{a.last}..HEAD"
        else:
            base = a.base or detect_base(a.repo)
            if not base:
                raise SystemExit("Не нашёл базовую ветку (main/master/develop). Укажите --base или --last N.")
            rev_range = f"{base}...HEAD"
        lines = added_lines(a.repo, rev_range)
        ai = [l for l in lines if line_hash(l) in ai_hashes]
        share = (len(ai) / len(lines) * 100) if lines else 0
        print(f"\n=== атрибуция {rev_range}: добавлено значимых строк {len(lines)}, "
              f"совпали с фрагментами агента {len(ai)} → {share:.0f}% AI")
        for l in lines[:40]:
            print(("  AI  " if line_hash(l) in ai_hashes else "  чел ") + l.rstrip()[:100])


if __name__ == "__main__":
    main()
