"""
log_llm.py — аддон mitmproxy для отладки выгрузки логов LLM-агентов.

Пишет файлы в %LOCALAPPDATA%\\mitm-llm (или в папку из MITM_LOG_DIR):
  llm_requests.jsonl  — сырой запрос: заголовки (ключ замаскирован) и тело;
  llm_responses.jsonl — собранный ответ модели: текст и tool calls с аргументами (там есть код!);
  llm_records.jsonl   — одна запись на запрос в целевом формате топика llm.requests
                        (поля как в staging.llm_requests): код из ответа в ней только в виде отпечатков.

Переменные окружения:
  MITM_STREAM=0         — не стримить ответы: mitmweb покажет тело ответа целиком,
                          но агент получит ответ только после окончания генерации;
  MITM_SAVE_RESPONSES=0 — не писать llm_responses.jsonl.

Запуск (PowerShell, одной строкой):
  mitmweb --mode reverse:https://<адрес-шлюза> --listen-host 127.0.0.1 --listen-port 8899 -s "C:\\путь\\log_llm.py"
"""
import hashlib
import json
import logging
import os
import pathlib
import re
import time
import uuid
from datetime import datetime, timezone

LOG_DIR = pathlib.Path(os.environ.get(
    "MITM_LOG_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "mitm-llm"),
))
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_FILE = LOG_DIR / "llm_requests.jsonl"
REC_FILE = LOG_DIR / "llm_records.jsonl"
RESP_FILE = LOG_DIR / "llm_responses.jsonl"
STREAM = os.environ.get("MITM_STREAM", "1") != "0"
SAVE_RESPONSES = os.environ.get("MITM_SAVE_RESPONSES", "1") != "0"

SECRET_HEADERS = {"authorization", "x-api-key", "api-key"}
CONTEXT_HEADERS = {
    "x-tool": "tool",
    "x-session-id": "session_id",
    "x-repo": "repo",
    "x-branch": "branch",
    "x-jira-key": "jira_key",
}
# Ключи аргументов tool calls, в которых модель передаёт новый код (разные агенты называют их по-разному)
NEW_CODE_KEYS = ("newString", "new_string", "new_str", "content", "file_text", "text", "code",
                 "replace", "replacement", "new_content", "insert_text", "contents")
PATCH_KEYS = ("patchText", "patch", "diff", "input")
SHELL_KEYS = ("command", "cmd", "script")
PATH_KEYS = ("filePath", "file_path", "path", "target_file", "filename")
# Инструменты, которые точно не пишут код: их аргументы не разбираем
READ_ONLY_TOOLS = {"read", "read_file", "grep", "glob", "list", "ls", "list_files", "search_files",
                   "webfetch", "websearch", "todowrite", "todoread", "task", "question"}

# ---------- чистые функции (не зависят от mitmproxy) ----------

def key_fingerprint(headers: dict) -> str | None:
    """Отпечаток ключа вместо самого ключа: по нему шлюз сопоставит разработчика."""
    auth = headers.get("authorization") or headers.get("x-api-key") or ""
    token = auth.split(" ", 1)[-1].strip()
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else None


def parse_sse(raw: bytes) -> dict:
    """Собирает потоковый ответ OpenAI-совместимого API (SSE) в один итог."""
    out = {"id": None, "model": None, "content": "", "finish_reason": None,
           "usage": None, "tool_calls": {}}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        out["id"] = out["id"] or chunk.get("id")
        out["model"] = out["model"] or chunk.get("model")
        if chunk.get("usage"):
            out["usage"] = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            out["content"] += delta.get("content") or ""
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:  # некоторые провайдеры не присылают index: новый id — новый вызов
                    calls = out["tool_calls"]
                    idx = len(calls) if (tc.get("id") or not calls) else max(calls)
                slot = out["tool_calls"].setdefault(idx, {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
            if ch.get("finish_reason"):
                out["finish_reason"] = ch["finish_reason"]
    out["tool_calls"] = [out["tool_calls"][i] for i in sorted(out["tool_calls"])]
    return out


def parse_json_response(raw: bytes) -> dict:
    """Непотоковый ответ chat/completions."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return {"id": None, "model": None, "content": "", "finish_reason": None,
                "usage": None, "tool_calls": []}
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = [{"name": (c.get("function") or {}).get("name", ""),
              "arguments": (c.get("function") or {}).get("arguments", "")}
             for c in msg.get("tool_calls") or []]
    return {"id": body.get("id"), "model": body.get("model"), "content": msg.get("content") or "",
            "finish_reason": choice.get("finish_reason"), "usage": body.get("usage"),
            "tool_calls": calls}


HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b", re.S)
PS_HERESTRING = re.compile(r"@(['\"])\r?\n(.*?)\r?\n\1@", re.S)
ECHO_REDIRECT = re.compile(r"\b(?:echo|printf)\s+(?:-e\s+)?([\"'])(.*?)\1\s*>>?", re.S)


def added_from_patch(text: str) -> list:
    """Добавленные строки из unified diff, *** Begin Patch или SEARCH/REPLACE."""
    if ">>>>>>> REPLACE" in text:
        out = []
        for block in re.findall(r"=======\r?\n(.*?)\r?\n>>>>>>> REPLACE", text, re.S):
            out += block.splitlines()
        return out
    return [l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]


def code_from_shell(cmd: str) -> list:
    """Код, который модель пишет в файлы через shell: heredoc, here-string PowerShell, echo > file."""
    out = []
    for m in HEREDOC.finditer(cmd):
        out += m.group(2).splitlines()
    for m in PS_HERESTRING.finditer(cmd):
        out += m.group(2).splitlines()
    for m in ECHO_REDIRECT.finditer(cmd):
        out += m.group(2).replace("\\n", "\n").splitlines()
    return out


def code_from_args(name: str, args: dict) -> tuple[list, str | None]:
    """Новые строки кода из аргументов одного tool call (без привязки к именам инструментов)."""
    lines: list = []
    for k in PATCH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and ("\n+" in v or v.startswith("+") or ">>>>>>> REPLACE" in v
                                   or "*** Begin Patch" in v):
            lines += added_from_patch(v)
    for k in NEW_CODE_KEYS:
        v = args.get(k)
        if isinstance(v, str):
            lines += v.splitlines()
    for e in args.get("edits") or []:  # multiedit
        if isinstance(e, dict):
            for k in NEW_CODE_KEYS:
                if isinstance(e.get(k), str):
                    lines += e[k].splitlines()
    for k in SHELL_KEYS:
        if isinstance(args.get(k), str):
            lines += code_from_shell(args[k])
    path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), None)
    return lines, path


def edit_fragments(tool_calls: list) -> list:
    """Фрагменты кода из tool calls — сырьё для атрибуции AI-строк (fact_ai_fragment)."""
    frags = []
    for tc in tool_calls:
        name = (tc.get("name") or "").strip()
        if name.lower() in READ_ONLY_TOOLS:
            continue
        try:
            args = json.loads(tc.get("arguments") or "{}")
        except ValueError:
            continue
        if not isinstance(args, dict):
            continue
        lines, path = code_from_args(name, args)
        lines = [l for l in lines if l.strip()]
        if lines:
            frags.append({"tool": name, "file_path": path, "lines_count": len(lines),
                          "line_hashes": [hashlib.sha1(l.strip().encode()).hexdigest()[:16] for l in lines]})
    return frags


def tool_calls_detail(tool_calls: list) -> list:
    """Имена инструментов и ключи их аргументов — для диагностики, без значений."""
    out = []
    for tc in tool_calls:
        try:
            args = json.loads(tc.get("arguments") or "{}")
            keys = sorted(args) if isinstance(args, dict) else []
        except ValueError:
            keys = ["<не JSON>"]
        out.append({"name": tc.get("name"), "arg_keys": keys})
    return out


CODE_BLOCK = re.compile(r"```[^\n`]*\n(.*?)```", re.S)


def _fingerprint(lines: list) -> dict:
    lines = [l for l in lines if l.strip()]
    return {"lines_count": len(lines),
            "line_hashes": [hashlib.sha1(l.strip().encode()).hexdigest()[:16] for l in lines]}


def code_block_fragments(content: str) -> list:
    """Код в markdown-блоках ответа (```...```) — второй источник AI-фрагментов."""
    return [{"tool": "code_block", "file_path": None, **_fingerprint(m.group(1).splitlines())}
            for m in CODE_BLOCK.finditer(content or "")]


def build_record(req_headers: dict, req_body: dict, status: int, resp: dict,
                 t_start: float, t_first: float | None, t_end: float) -> dict:
    """Одна запись в формате топика llm.requests (staging.llm_requests)."""
    h = {k.lower(): v for k, v in req_headers.items()}
    usage = resp.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    rec = {
        "request_id": str(uuid.uuid4()),          # в бою ставит шлюз
        "provider_response_id": resp.get("id"),
        "ts": datetime.fromtimestamp(t_start, timezone.utc).isoformat(timespec="milliseconds"),
        "key_fp": key_fingerprint(h),             # в бою шлюз заменит на developer_id
        "model_requested": req_body.get("model"),
        "routed_model": resp.get("model"),
        "stream": bool(req_body.get("stream")),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "latency_ms": round((t_end - t_start) * 1000),
        "ttft_ms": round((t_first - t_start) * 1000) if t_first else None,
        "status": status,
        "finish_reason": resp.get("finish_reason"),
        "messages": len(req_body.get("messages") or []),
        "tool_calls": [tc["name"] for tc in resp.get("tool_calls") or []],
        "tool_calls_detail": tool_calls_detail(resp.get("tool_calls") or []),
        "edit_fragments": edit_fragments(resp.get("tool_calls") or []) + code_block_fragments(resp.get("content")),
        # Эти поля в бою заполняет шлюз (guardrail), локально их нет:
        "data_class": None, "policy_action": None, "cost_usd": None,
    }
    for header, field in CONTEXT_HEADERS.items():
        rec[field] = h.get(header) or None
    # Проверки целевого формата: что не заполнено и почему это важно
    problems = []
    if not rec["session_id"]:
        problems.append("нет X-Session-Id: запросы не склеятся в сессию")
    if not rec["repo"]:
        problems.append("нет X-Repo: шлюз отправит запрос только на внутренние модели")
    if rec["stream"] and rec["input_tokens"] is None:
        problems.append("в стриме нет usage (клиент не запросил stream_options.include_usage или шлюз его не вернул): "
                        "токены придётся считать на шлюзе")
    rec["format_problems"] = problems
    return rec


def append_jsonl(path: pathlib.Path, obj: dict):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError as e:
        logging.warning("log_llm: не удалось записать %s: %s", path, e)


def decode_body(raw: bytes, content_encoding: str) -> bytes:
    """Снимает сжатие с потока, сохранённого «как есть» (gzip, deflate, br, zstd)."""
    enc = (content_encoding or "").strip().lower()
    if not enc or enc == "identity" or not raw:
        return raw
    try:
        from mitmproxy.net import encoding as menc  # тот же декодер, что использует mitmproxy
        return menc.decode(raw, enc)
    except Exception:
        pass
    import gzip, zlib
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:
        logging.warning("log_llm: не удалось распаковать поток (%s): %s", enc, e)
    return raw


# ---------- хуки mitmproxy ----------

def _is_llm_call(flow) -> bool:
    return flow.request.method == "POST" and flow.request.path.split("?")[0].endswith(
        ("/chat/completions", "/completions", "/responses", "/messages"))


def request(flow):
    flow.metadata["t_start"] = time.time()
    headers = {k: ("***" if k.lower() in SECRET_HEADERS else v) for k, v in flow.request.headers.items()}
    text = flow.request.get_text(strict=False) or ""
    try:
        body = json.loads(text) if text else {}
    except ValueError:
        body = {"raw": text}
    if not isinstance(body, dict):
        body = {"raw": body}
    flow.metadata["req_body"] = body
    if _is_llm_call(flow):
        # Просим шлюз не сжимать ответ: потоковые ответы мы копируем «как есть»
        flow.request.headers["accept-encoding"] = "identity"
    append_jsonl(RAW_FILE, {"ts": time.time(), "path": flow.request.path, "headers": headers, "body": body})


def responseheaders(flow):
    flow.metadata["t_first"] = time.time()
    if STREAM and "text/event-stream" in flow.response.headers.get("content-type", ""):
        buf = bytearray()
        flow.metadata["sse_buf"] = buf

        def tee(chunk: bytes) -> bytes:
            buf.extend(chunk)  # копия для разбора, агенту уходит без изменений
            return chunk

        flow.response.stream = tee


def response(flow):
    # Для потоковых ответов хук вызывается после окончания стрима
    if not _is_llm_call(flow):
        return
    ctype = flow.response.headers.get("content-type", "")
    if "sse_buf" in flow.metadata:
        raw = decode_body(bytes(flow.metadata["sse_buf"]), flow.response.headers.get("content-encoding", ""))
        resp = parse_sse(raw)
        flow.metadata["parse_info"] = {
            "sse_bytes": len(raw),
            "data_lines": raw.count(b"data:"),
            "content_encoding": flow.response.headers.get("content-encoding") or None,
        }
    elif "text/event-stream" in ctype:  # MITM_STREAM=0: ответ буферизован целиком
        resp = parse_sse(flow.response.content or b"")
    else:
        resp = parse_json_response(flow.response.content or b"")
    rec = build_record(dict(flow.request.headers), flow.metadata.get("req_body") or {},
                       flow.response.status_code, resp, flow.metadata.get("t_start", time.time()),
                       flow.metadata.get("t_first"), time.time())
    rec["parse_info"] = flow.metadata.get("parse_info")
    if rec["parse_info"] and rec["parse_info"]["data_lines"] == 0:
        rec["format_problems"].append("поток ответа не разобран: нет строк data: (сжатие или не SSE-формат)")
    append_jsonl(REC_FILE, rec)
    if SAVE_RESPONSES:
        append_jsonl(RESP_FILE, {"request_id": rec["request_id"], "provider_response_id": resp.get("id"),
                                 "session_id": rec["session_id"], "model": resp.get("model"),
                                 "content": resp.get("content"), "tool_calls": resp.get("tool_calls"),
                                 "finish_reason": resp.get("finish_reason"), "usage": resp.get("usage")})
    if rec["format_problems"]:
        logging.warning("log_llm: %s", "; ".join(rec["format_problems"]))
