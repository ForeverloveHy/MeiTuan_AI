from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import shutil
import urllib.error
import urllib.request
from typing import Any



DEFAULT_MODEL = "LongCat-Flash-Lite"
DEFAULT_BASE_URL = "https://api.longcat.chat/openai"
DEFAULT_TIMEOUT: int | None = None
DEFAULT_MAX_TOKENS = int(os.getenv("LONGCAT_MAX_TOKENS", "16000") or "16000")
FALLBACK_MODELS = ["LongCat-Flash-Lite", "LongCat-2.0-Preview"]


def build_endpoint(base_url: str | None) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/openai/v1"):
        return base + "/chat/completions"
    if base.endswith("/openai"):
        return base + "/v1/chat/completions"
    if base.endswith("api.longcat.chat"):
        return base + "/openai/v1/chat/completions"
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _rough_tokens(text: str) -> int:
    text = str(text or "")
    return max(1, int(len(text) / 1.6) + 1) if text else 0


def _strip_code_fence(text: str) -> str:
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json|JSON)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _normalize_json_text(text: str) -> str:
    """Normalize wrappers without damaging valid JSON string content.

    Important: do *not* globally convert Chinese/curly quotes to ASCII quotes.
    LongCat often writes values like ``"新增“选项A”选项"``. That is legal JSON,
    but converting the inner curly quotes to ASCII would turn it into
    ``"新增"选项A"选项"`` and create exactly the kind of invalid JSON seen in
    local runs. Structural curly quotes are handled later as a last-resort
    repair, not here.
    """
    s = _strip_code_fence(text)
    s = s.replace("\ufeff", "").replace("\u200b", "")
    return s.strip()


def _balanced_json_spans(text: str) -> list[tuple[int, int]]:
    """Return candidate object spans, respecting JSON strings.

    Using first ``{`` and last ``}`` is brittle when the model adds examples or
    diagnostic text. This scanner keeps only balanced top-level object spans.
    """
    spans: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    in_str = False
    esc = False
    start = -1
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if not stack:
                start = i
            stack.append(("{", i))
        elif ch == "}":
            if not stack:
                continue
            stack.pop()
            if not stack and start >= 0:
                spans.append((start, i + 1))
                start = -1
    # Longest first normally corresponds to the full schema object.
    spans.sort(key=lambda x: x[1] - x[0], reverse=True)
    return spans




def _escape_unescaped_quotes_inside_strings(s: str) -> str:
    """Escape ASCII quotes that are clearly inside a JSON string value.

    LLMs sometimes copy feature names as English quotes inside a value, e.g.
    ``"text": "新增"选项A"选项"``. A quote inside a string is treated as a
    real closing quote only when the next non-space character is one of JSON's
    structural separators. Otherwise it is escaped. This is generic JSON syntax
    repair and contains no business-specific vocabulary.
    """
    out: list[str] = []
    in_str = False
    esc = False
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
                esc = False
            i += 1
            continue
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            nxt = s[j] if j < n else ""
            if nxt in {":", ",", "}", "]", ""}:
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_structural_curly_quotes(s: str) -> str:
    """Last-resort repair when the model used curly quotes as JSON delimiters.

    Target only keys and simple boundary values. Inner curly quotes inside an
    already well-formed ASCII-quoted value are preserved.
    """
    # Curly quoted keys: { “id”: ... } or , “id”: ...
    s = re.sub(r'([\{,]\s*)[“”]([^“”\n\r]{1,120})[“”](\s*:)', r'\1"\2"\3', s)
    # Curly quoted string values whose closing quote is followed by a JSON separator.
    s = re.sub(r'(:\s*)[“”]([^“”\n\r]*?)[“”](\s*[,}\]])', r'\1"\2"\3', s)
    return s

def _remove_trailing_commas(s: str) -> str:
    # Safe for common LLM JSON because patterns only target comma before ]/}.
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _insert_missing_commas_between_fields(s: str) -> str:
    """Repair the most common LongCat/LLM JSON error: missing comma.

    It handles cases like:
      "a": "x"\n      "b": 1
    and:
      "items": []\n      "next": {}
    The regex intentionally anchors to a following quoted key so it does not
    insert commas inside normal Chinese text.
    """
    prev = None
    # Allow several passes because nested objects can contain multiple misses.
    while prev != s:
        prev = s
        s = re.sub(
            r'("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null|\]|\})\s*(\n\s*"[A-Za-z0-9_\-.\u4e00-\u9fff]+"\s*:)',
            r"\1,\2",
            s,
            flags=re.S,
        )
        # Adjacent array items, e.g. {"a":1}
        # {"b":2}. Limit to a newline boundary to avoid touching inline text.
        s = re.sub(
            r'("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null|\]|\})\s*(\n\s*[\{\[])',
            r"\1,\2",
            s,
            flags=re.S,
        )
    return s


def _quote_bare_keys_light(s: str) -> str:
    # Lightweight fallback for rare {key: value} fragments. Avoid touching URLs.
    return re.sub(r'([,{]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)', r'\1"\2"\3', s)


def _json_loads_relaxed(candidate: str) -> dict[str, Any]:
    attempts: list[str] = []
    base = _normalize_json_text(candidate)
    attempts.append(base)
    no_tail = _remove_trailing_commas(base)
    with_commas = _insert_missing_commas_between_fields(no_tail)
    with_quotes_escaped = _escape_unescaped_quotes_inside_strings(with_commas)
    curly_structural = _repair_structural_curly_quotes(base)
    curly_then_commas = _insert_missing_commas_between_fields(_remove_trailing_commas(curly_structural))

    attempts.append(no_tail)
    attempts.append(with_commas)
    attempts.append(with_quotes_escaped)
    attempts.append(_quote_bare_keys_light(with_quotes_escaped))
    attempts.append(curly_structural)
    attempts.append(curly_then_commas)
    attempts.append(_escape_unescaped_quotes_inside_strings(curly_then_commas))

    last_error: Exception | None = None
    seen: set[str] = set()
    for text in attempts:
        if text in seen:
            continue
        seen.add(text)
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception as exc:
            last_error = exc

    # Optional parser if the user happened to have json5 installed. The project
    # still has no mandatory dependency on it.
    try:
        import json5  # type: ignore

        for text in attempts:
            try:
                obj = json5.loads(text)
                if isinstance(obj, dict):
                    return obj
            except Exception as exc:
                last_error = exc
    except Exception:
        pass

    raise ValueError(str(last_error) if last_error else "未知 JSON 解析错误")


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from model output with conservative repair.

    LongCat sometimes returns almost-correct JSON with one missing comma even
    after a repair prompt. We keep parsing local and schema-driven: no task
    words are hardcoded here, only generic JSON syntax cleanup is attempted.
    """
    s = _normalize_json_text(text)
    if not s:
        raise ValueError("LongCat 返回为空，无法解析 JSON。")

    # Whole output first.
    try:
        return _json_loads_relaxed(s)
    except Exception as whole_error:
        last_error: Exception = whole_error

    # Then balanced object candidates.
    for a, b in _balanced_json_spans(s):
        try:
            return _json_loads_relaxed(s[a:b])
        except Exception as exc:
            last_error = exc

    # Last fallback mirrors the original first/last brace logic.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            return _json_loads_relaxed(s[start : end + 1])
        except Exception as exc:
            last_error = exc
    raise ValueError("没有解析到合法 JSON 对象：" + str(last_error))


def _safe_debug_dir() -> Path | None:
    raw = os.getenv("SCEG_LONGCAT_DEBUG_DIR", "").strip()
    if not raw:
        return None
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def _write_debug_text(prefix: str, text: str) -> str | None:
    p = _safe_debug_dir()
    if not p:
        return None
    name = re.sub(r"[^A-Za-z0-9_\-]+", "_", prefix).strip("_") or "longcat"
    path = p / f"{name}_{int(time.time())}.txt"
    try:
        path.write_text(str(text or ""), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _candidate_models(preferred: str | None) -> list[str]:
    """Return LongCat API model candidates without duplicating names.

    LongCat-Flash-Lite exists as an open-source checkpoint, but the official
    LongCat API may not expose it as a chat-completions model. Keep a fallback
    list so old project configs can still run after the API model list changes.
    """
    names: list[str] = []
    first = (preferred or DEFAULT_MODEL).strip()
    if first:
        names.append(first)
    for name in FALLBACK_MODELS:
        if name and name not in names:
            names.append(name)
    return names


def _looks_like_unsupported_model_error(text: str) -> bool:
    t = (text or "").lower()
    return ("unsupported model" in t or "invalid_parameter" in t) and "model" in t


class LongCatClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, timeout: int | None = None) -> None:
        self.api_key = api_key or os.getenv("LONGCAT_API_KEY", "")
        self.base_url = base_url or os.getenv("LONGCAT_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.getenv("LONGCAT_MODEL", DEFAULT_MODEL)
        self.timeout = int(timeout) if timeout is not None and int(timeout) > 0 else None
        self.max_tokens = int(os.getenv("LONGCAT_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)) or str(DEFAULT_MAX_TOKENS))
        self.transport = (os.getenv("LONGCAT_TRANSPORT", "auto") or "auto").lower().strip()
        self.simulated_dir = os.getenv("SCEG_SIMULATED_LONGCAT_DIR", "").strip()
        self._sim_counter = 0
        self.usage_records: list[dict[str, Any]] = []
        self._last_usage: dict[str, Any] = {}
        self._last_model: str | None = None

    def enabled(self) -> bool:
        return bool((self.api_key or "").strip()) or bool(self.simulated_dir)

    def _read_simulated_content(self, purpose: str) -> str:
        """Replay a captured/simulated LongCat response for offline debugging.

        This is only activated when SCEG_SIMULATED_LONGCAT_DIR is set.  It lets
        the local test environment consume a file as if it were a LongCat API
        response, while production still calls the real LongCat endpoint.  The
        replay files are data artifacts, not evaluator rules, so no business
        vocabulary is embedded in local judge code.
        """
        root = Path(self.simulated_dir)
        if not root.exists():
            raise RuntimeError(f"SCEG_SIMULATED_LONGCAT_DIR 不存在：{root}")
        self._sim_counter += 1
        candidates = [
            root / f"{self._sim_counter:02d}_{purpose}.json",
            root / f"{purpose}_{self._sim_counter:02d}.json",
            root / f"{purpose}.json",
            root / f"{self._sim_counter:02d}.json",
        ]
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        names = ", ".join(x.name for x in sorted(root.glob("*.json"))[:20])
        raise RuntimeError(f"没有找到模拟 LongCat 返回文件，purpose={purpose}，counter={self._sim_counter}，目录={root}，已有={names}")

    def _parse_response(self, raw: str, endpoint: str) -> str:
        try:
            obj = json.loads(raw)
        except Exception:
            raise RuntimeError(f"LongCat 返回不是合法 JSON，接口={endpoint}，前500字符={raw[:500]}")
        self._last_usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        self._last_model = obj.get("model")
        try:
            return obj["choices"][0]["message"]["content"]
        except Exception:
            raise RuntimeError(f"LongCat 返回格式异常，接口={endpoint}，返回={raw[:1000]}")

    def _timeout_hint(self, transport: str, detail: str) -> str:
        return (
            f"LongCat 网络无响应或底层连接中断（传输={transport}，应用层超时=不限制）。\n"
            "系统已取消 LongCat 建图阶段的应用层超时限制；如果仍失败，通常是网络连接、TLS/代理、"
            "平台侧主动断开、模型返回异常，或提示词/输出过长。\n"
            "可尝试：1）稍后重试；2）命令行先执行 set LONGCAT_TRANSPORT=curl；"
            "3）确认终端能访问 LongCat API；4）降低 LONGCAT_MAX_TOKENS 或缩短复杂指令。\n"
            f"底层信息：{detail}"
        )

    def _post_urllib(self, endpoint: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "sceg-latest-demo/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ssl.create_default_context()) as resp:
                raw = resp.read().decode("utf-8")
            return self._parse_response(raw, endpoint)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LongCat HTTP {exc.code}：{detail[:1500]}")
        except (socket.timeout, TimeoutError) as exc:
            raise RuntimeError(self._timeout_hint("urllib", repr(exc)))
        except Exception as exc:
            msg = repr(exc)
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                raise RuntimeError(self._timeout_hint("urllib", msg))
            raise RuntimeError(f"LongCat 网络或 TLS 连接失败：{exc!r}")

    def _post_curl(self, endpoint: str, payload: dict[str, Any]) -> str:
        curl = shutil.which("curl") or shutil.which("curl.exe")
        if not curl:
            raise RuntimeError("未找到 curl，无法使用 curl 兜底传输")
        fd, path = tempfile.mkstemp(prefix="longcat_payload_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            cmd = [
                curl,
                "-sS",
                "--fail-with-body",
                "--retry",
                "1",
                "--retry-delay",
                "3",
                "--retry-all-errors",
                "--http1.1",
                "-X",
                "POST",
                endpoint,
                "-H",
                f"Authorization: Bearer {self.api_key}",
                "-H",
                "Content-Type: application/json",
                "-H",
                "Accept: application/json",
                "--data-binary",
                "@" + path,
            ]
            if self.timeout is not None:
                cmd[3:3] = [
                    "--connect-timeout",
                    str(min(60, max(10, int(self.timeout) // 5))),
                    "--max-time",
                    str(int(self.timeout)),
                ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out = proc.stdout.decode("utf-8", errors="ignore")
            err = proc.stderr.decode("utf-8", errors="ignore")
            if proc.returncode != 0:
                detail = f"curl 返回码={proc.returncode}，stderr={err[:1000]}，stdout={out[:1000]}"
                if proc.returncode == 28 or "timed out" in detail.lower() or "timeout" in detail.lower():
                    raise RuntimeError(self._timeout_hint("curl", detail))
                raise RuntimeError(detail)
            return self._parse_response(out, endpoint)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def chat_with_usage(self, messages: list[dict[str, str]], temperature: float = 0.0, purpose: str = "chat") -> tuple[str, dict[str, Any]]:
        if self.simulated_dir:
            content = self._read_simulated_content(purpose)
            usage = self._usage_record(messages, content, purpose)
            usage["usage_source"] = "simulated_longcat_response"
            usage["simulated_dir"] = self.simulated_dir
            self.usage_records.append(usage)
            return content, usage
        if not self.enabled():
            raise RuntimeError("未配置 LONGCAT_API_KEY。请在界面里填写 API Key，或设置环境变量。")
        endpoint = build_endpoint(self.base_url)
        preferred_model = self.model
        errors: list[str] = []
        for model_name in _candidate_models(preferred_model):
            payload = {"model": model_name, "messages": messages, "temperature": temperature, "stream": False, "max_tokens": int(self.max_tokens)}
            try:
                if self.transport == "curl":
                    content = self._post_curl(endpoint, payload)
                elif self.transport == "urllib":
                    content = self._post_urllib(endpoint, payload)
                else:
                    first_error = None
                    try:
                        content = self._post_urllib(endpoint, payload)
                    except Exception as exc:
                        first_error = str(exc)
                        # HTTP 400/401/429 等不是传输层问题；只有“模型不存在”允许换模型重试。
                        if "LongCat HTTP" in first_error and _looks_like_unsupported_model_error(first_error):
                            raise RuntimeError(first_error)
                        if "LongCat HTTP" in first_error:
                            raise
                        try:
                            content = self._post_curl(endpoint, payload)
                        except Exception as exc2:
                            raise RuntimeError(first_error + "\n已尝试 curl 兜底，但仍失败：\n" + str(exc2))
                fallback_used = model_name != _candidate_models(preferred_model)[0]
                self.model = model_name
                usage = self._usage_record(messages, content, purpose)
                if fallback_used:
                    usage["model_fallback_from"] = preferred_model
                    usage["model_fallback_to"] = model_name
                self.usage_records.append(usage)
                return content, usage
            except Exception as exc:
                msg = str(exc)
                errors.append(f"{model_name}: {msg[:500]}")
                if _looks_like_unsupported_model_error(msg):
                    continue
                raise
        raise RuntimeError(
            "LongCat 模型名不可用。已依次尝试："
            + "、".join(_candidate_models(preferred_model))
            + "。请在界面 Model 中改成 LongCat-2.0-Preview，或检查 LongCat 平台当前支持的模型。\n"
            + "\n".join(errors)
        )

    def _usage_record(self, messages: list[dict[str, str]], content: str, purpose: str) -> dict[str, Any]:
        prompt_text = "\n".join((m.get("role", "") + "\n" + m.get("content", "")) for m in messages)
        raw = self._last_usage or {}
        prompt_tokens = raw.get("prompt_tokens") or raw.get("input_tokens") or _rough_tokens(prompt_text)
        completion_tokens = raw.get("completion_tokens") or raw.get("output_tokens") or _rough_tokens(content)
        total_tokens = raw.get("total_tokens") or int(prompt_tokens) + int(completion_tokens)
        source = "api" if raw else "estimated"
        return {
            "purpose": purpose,
            "model": self._last_model or self.model,
            "base_url": self.base_url,
            "usage_source": source,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
        }

    def generate_json(self, instruction: str, prompt_text: str, purpose: str = "build_graph") -> dict[str, Any]:
        content, _ = self.chat_with_usage(
            [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": instruction},
            ],
            temperature=0.0,
            purpose=purpose,
        )
        try:
            return extract_json_object(content)
        except Exception as first:
            raw_path = _write_debug_text("longcat_raw_invalid_json", content)
            repair_prompt = (
                "你是 JSON 语法修复器。把用户给出的内容修复为严格合法 JSON 对象。"
                "不要改字段含义，不要补充新业务规则，不要解释，不要 Markdown。"
                "必须使用双引号，数组/对象元素之间必须有逗号，不能有尾随逗号。"
                "如果内容前后有说明文字，只保留最外层 JSON 对象。"
                "原解析错误：" + str(first)
            )
            fixed, _ = self.chat_with_usage(
                [
                    {"role": "system", "content": repair_prompt},
                    {"role": "user", "content": content},
                ],
                temperature=0.0,
                purpose="repair_graph_json",
            )
            try:
                return extract_json_object(fixed)
            except Exception as second:
                fixed_path = _write_debug_text("longcat_repair_invalid_json", fixed)
                hint = "LongCat 已返回内容，但不是严格合法 JSON；本地自动修复和二次修复仍未成功。"
                if raw_path:
                    hint += f"\n原始返回已保存：{raw_path}"
                if fixed_path:
                    hint += f"\n修复返回已保存：{fixed_path}"
                hint += "\n建议直接重试一次；如果仍失败，可把 debug 文件发给我定位具体缺失位置。"
                raise RuntimeError(hint + f"\n第一次错误：{first}\n第二次错误：{second}")

    def usage_summary(self) -> dict[str, Any]:
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        for rec in self.usage_records:
            total["calls"] += 1
            total["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
            total["completion_tokens"] += int(rec.get("completion_tokens") or 0)
            total["total_tokens"] += int(rec.get("total_tokens") or 0)
        return {"total": total, "records": list(self.usage_records)}
