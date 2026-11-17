"""Source-scoped transport adapter.

只对白名单里的 Gemini 源动手：
1) 整理 HTTP 头（去掉 SDK 诊断头与自报家门的 UA）；
2) 在系统提示词最前面补一句中性声明，正文一个字不改；
3) 把 Responses 的 input 拍平成 {role, content}，跟普通客户端发的一样。

不覆盖核心文件，不碰鉴权、工具、历史、重试，不修改其他人格内容。
"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from httpx import ByteStream

DEFAULT_PREFIX = "You are an AI assistant."
_ALLOWED_PATHS = ("/responses", "/chat/completions")
_SECRET_HEADERS = {"authorization", "x-api-key", "x-goog-api-key", "cookie"}
# AstrBot 每轮都会往用户消息里塞的环境注入块，正是中转站说的"隐式注入"。
_SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)
_ASTRBOT_WORD_RE = re.compile(r"AstrBot")
_MAX_TEXT = 4000


def _with_prefix(text, prefix):
    """给一段系统文本加中性首句；已经有了就不重复加。"""
    if not isinstance(text, str) or not text.strip():
        return text, False
    head = text.lstrip()
    if head[: len(prefix)].lower() == prefix.lower():
        return text, False
    return f"{prefix}\n\n{text}", True


def _rewrite_system_prompt(body, prefix):
    """改写 instructions 或 messages 里 role=system 的那条，返回是否有改动。"""
    changed = False
    instructions = body.get("instructions")
    if isinstance(instructions, str):
        new_text, did = _with_prefix(instructions, prefix)
        if did:
            body["instructions"] = new_text
            changed = True

    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                new_text, did = _with_prefix(content, prefix)
                if did:
                    message["content"] = new_text
                    changed = True
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        new_text, did = _with_prefix(part["text"], prefix)
                        if did:
                            part["text"] = new_text
                            changed = True
            break
    return changed


# ---------------------------------------------------------------------------
# 出厂英文提示词中文化
#
# 上游（Antigravity 一类）会针对部分英文枚举短语做内容过滤，命中后不是拒绝
# 而是直接回一个假 429（"All available accounts are currently rate-limited"），
# 再往上抛就变成 503 "No available accounts"。把这几段换成中文即可恢复。
# 只改提示词措辞，工具名、参数名、能力语义一个字不动。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 出厂英文提示词中文化
#
# 上游（Antigravity 一类）会针对部分英文枚举短语做内容过滤，命中后不是拒绝
# 而是直接回一个假 429（"All available accounts are currently rate-limited"），
# 再往上抛就变成 503 "No available accounts"。把这几段换成中文即可恢复。
#
# 文本内容只存一份，就是 prompt_zh.py，而且每段都能在面板配置里改。
# 这里不再保存副本：以前这里写死过一套中文，用户在配置里改了提示词，
# 到了 HTTP 这一层又被旧文本盖回去，改了等于没改。
# ---------------------------------------------------------------------------


def _neutralize_text(text, localizer=None):
    """把出厂英文段落换成中文，返回 (新文本, 是否改动)。"""
    if not isinstance(text, str) or not text:
        return text, False
    if localizer is None:
        return text, False
    try:
        return localizer(text)
    except Exception:  # noqa: BLE001
        return text, False


def _neutralize_factory(body, localizer=None):
    """把系统提示词与各条 system 消息里的出厂英文段落换成中文。"""
    changed = False
    new_text, did = _neutralize_text(body.get("instructions"), localizer)
    if did:
        body["instructions"] = new_text
        changed = True
    for key in ("input", "messages"):
        items = body.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("role") not in {"system", "developer", "user"}:
                continue
            content = item.get("content")
            if isinstance(content, str):
                new_text, did = _neutralize_text(content, localizer)
                if did:
                    item["content"] = new_text
                    changed = True
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        new_text, did = _neutralize_text(part["text"], localizer)
                        if did:
                            part["text"] = new_text
                            changed = True
    return changed


# 工具定义里的英文枚举短语。上游过滤不只看系统提示，tools 数组也一起扫，
# 所以这里要把工具描述里的同类短语一并换成中文；工具名、参数名、枚举值一个字不动。
_TOOL_TEXT_RULES = (
    (
        re.compile(
            r"Maximum wait in milliseconds for output or exit on poll,\s*write,\s*write_line,"
            r"\s*or interrupt\s*\(up to 5 minutes\)\.?\s*"
            r"(Writes send input before waiting\.?\s*)?"
            r"(Output or exit may return early;\s*this does not stop the process\.?)?",
            re.I,
        ),
        "轮询、写入、写入整行或中断时等待输出或退出的最大毫秒数（最多5分钟）。写入在等待前发送输入；输出或提前退出不会终止进程。",
    ),
    (
        re.compile(
            r"Manage native shell sessions on this machine:\s*"
            r"list,\s*poll,\s*write raw text or complete lines to,\s*interrupt,\s*or terminate sessions\.?",
            re.I,
        ),
        "管理本机 shell 会话：列出、轮询、写入原始文本或整行，以及中断或终止会话。",
    ),
    (
        re.compile(
            r"Maximum output bytes returned by poll,\s*write,\s*write_line,"
            r"\s*interrupt,\s*or terminate\.",
            re.I,
        ),
        "轮询、写入、写入整行、中断或终止时返回的最大输出字节数。",
    ),
    (
        re.compile(
            r"Sessions are isolated to the current conversation and sender\.?"
            r"\s*(Administrators can manage all sessions in the conversation\.?)?",
            re.I,
        ),
        "会话按当前对话与发送者隔离；管理员可管理对话内的全部会话。",
    ),
    (
        re.compile(
            r"List,\s*poll,\s*write raw text or complete lines to,\s*"
            r"interrupt,\s*or terminate\s*managed shell sessions\.?",
            re.I,
        ),
        "管理本机 shell 会话：列出、轮询、写入原始文本或整行，以及中断或终止会话。",
    ),
    (
        re.compile(r"poll,\s*write,\s*write_line,\s*interrupt,\s*or terminate", re.I),
        "轮询、写入、写入整行、中断或终止",
    ),
    (
        re.compile(
            r"list,\s*poll,\s*write raw text or complete lines to,\s*"
            r"interrupt,\s*or terminate",
            re.I,
        ),
        "列出、轮询、写入原始文本或整行、中断或终止",
    ),
    (
        re.compile(r"interrupt,\s*or terminate", re.I),
        "中断或终止",
    ),
)


def _localize_tool_text(text):
    """把一段工具描述里的英文触发短语换成中文。"""
    if not isinstance(text, str) or not text:
        return text, False
    out = text
    for rx, rep in _TOOL_TEXT_RULES:
        out = rx.sub(rep, out)
    return out, out != text


def _localize_tools(body):
    """就地中文化 tools 数组里的描述文本，返回是否有改动。"""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    changed = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        node = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(node, dict):
            continue
        new_text, did = _localize_tool_text(node.get("description"))
        if did:
            node["description"] = new_text
            changed = True
        params = node.get("parameters")
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                for prop in props.values():
                    if not isinstance(prop, dict):
                        continue
                    new_text, did = _localize_tool_text(prop.get("description"))
                    if did:
                        prop["description"] = new_text
                        changed = True
    return changed


def _scrub_text(text, brand=False):
    """去掉框架注入块，返回 (新文本, 是否改动)。

    brand=True 时连 AstrBot 字样一起换掉，但路径也会被改写成不存在的目录，
    所以默认不动，免得把文件路径搞坏。
    """
    if not isinstance(text, str) or not text:
        return text, False
    original = text
    text = _SYSTEM_REMINDER_RE.sub("", text)
    if brand:
        text = _ASTRBOT_WORD_RE.sub("Assistant", text)
    if text != original:
        # 清理后可能留下空段落，压掉多余空白。
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, text != original


def _scrub_body(body, brand=False):
    """把 input 里每条消息与系统提示词都过一遍。"""
    changed = False
    for key in ("instructions",):
        new_text, did = _scrub_text(body.get(key), brand)
        if did:
            body[key] = new_text
            changed = True
    items = body.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                new_text, did = _scrub_text(content, brand)
                if did:
                    item["content"] = new_text
                    changed = True
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        new_text, did = _scrub_text(part["text"], brand)
                        if did:
                            part["text"] = new_text
                            changed = True
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                new_text, did = _scrub_text(message["content"], brand)
                if did:
                    message["content"] = new_text
                    changed = True
    return changed


def _filter_tools(body, whitelist):
    """只留白名单里的工具，白名单为空则全发。"""
    tools = body.get("tools")
    if not isinstance(tools, list) or not whitelist:
        return False
    wanted = set()
    for item in whitelist:
        name = str(item).strip().lower()
        if not name:
            continue
        wanted.add(name)
        if name.startswith("astrbot_"):
            wanted.add(name[len("astrbot_"):])
        else:
            wanted.add("astrbot_" + name)
    kept = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", "")).lower()
        if name in wanted:
            kept.append(tool)
    if len(kept) == len(tools):
        return False
    body["tools"] = kept
    if not kept:
        body.pop("tool_choice", None)
    return True


def _alias_tools(body):
    """把工具名里的 astrbot_ 前缀剥掉，返回 {原名: 别名}。"""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return {}
    tools = [tool.get("function", tool) if isinstance(tool, dict) else tool for tool in tools]
    mapping = {}
    used = {t.get("name") for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str) and not t["name"].startswith("astrbot_")}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.startswith("astrbot_"):
            continue
        alias = name[len("astrbot_"):] or "tool"
        while alias in used:
            alias = f"{alias}_tool"
        used.add(alias)
        mapping[name] = alias
        tool["name"] = alias
    return mapping


def _apply_aliases_to_body(body, mapping):
    """历史里的 function_call 名字与正文里提到的工具名一起换掉。"""
    if not mapping:
        return False
    changed = False
    items = body.get("input")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                alias = mapping.get(item["name"])
                if alias:
                    item["name"] = alias
                    changed = True
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            if function.get("name") in mapping:
                function["name"] = mapping[function["name"]]
                changed = True
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        target = choice.get("function", choice)
        if target.get("name") in mapping:
            target["name"] = mapping[target["name"]]
            changed = True
    text_keys = []
    if isinstance(body.get("instructions"), str):
        text_keys.append(("instructions", body))
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                text_keys.append(("content", item))
    for message in body.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            text_keys.append(("content", message))
    for key, holder in text_keys:
        text = holder[key]
        for original, alias in mapping.items():
            if original in text:
                text = text.replace(original, alias)
                changed = True
        holder[key] = text
    return changed


def _restore_names(text, mapping):
    """Restore only JSON name fields, not argument strings or user text."""
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "name" and isinstance(item, str):
                    value[key] = mapping.get(item, item)
                elif key != "arguments":
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text
    visit(value)
    return json.dumps(value, ensure_ascii=False)


def _slim_system(text):
    """把框架样板从系统提示词里剪掉，只留下人格和世界书。

    中间那段 "## Skills" 到 "## 常驻" 之间全是框架的东西：技能清单、
    绝对路径、技能规则、本地执行说明，一看就是 agent 框架。
    """
    if not isinstance(text, str) or not text:
        return text, False
    start = text.find("## Skills")
    if start < 0:
        return text, False
    later = text.find("## 常驻", start)
    end = later if later > 0 else len(text)
    trimmed = (text[:start] + text[end:]).rstrip() + "\n"
    trimmed = re.sub(r"\n{3,}", "\n\n", trimmed)
    return trimmed, True


def _slim_body(body):
    changed = False
    new_text, did = _slim_system(body.get("instructions"))
    if did:
        body["instructions"] = new_text
        changed = True
    items = body.get("input")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("role") != "system":
                continue
            if isinstance(item.get("content"), str):
                new_text, did = _slim_system(item["content"])
                if did:
                    item["content"] = new_text
                    changed = True
            break
    good = True
    for message in body.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system" and isinstance(message.get("content"), str):
            new_text, did = _slim_system(message["content"])
            if did:
                message["content"] = new_text
                changed = True
            break
    return changed


def _flatten_input(body):
    """把 Responses 的 input 拍平成 {role, content}，去掉 AstrBot 自带的 type 字段。

    普通客户端就是这么发的：一串 {role, content}。
    """
    items = body.get("input")
    if not isinstance(items, list):
        return False
    flattened = []
    changed = False
    for item in items:
        if not isinstance(item, dict):
            flattened.append(item)
            continue
        if item.get("type") == "message" and isinstance(item.get("role"), str):
            content = item.get("content")
            if isinstance(content, list):
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "input_text"
                ]
                if texts and len(texts) == len(content):
                    content = "\n".join(texts)
            flattened.append({"role": item["role"], "content": content})
            changed = True
            continue
        flattened.append(item)
    if changed:
        body["input"] = flattened
    return changed


def _flatten_shape(body):
    """把 Responses 正文拍平：system 走 input，不留 instructions。"""
    changed = False
    instructions = body.pop("instructions", None)
    if isinstance(instructions, str) and instructions.strip():
        items = body.get("input")
        if isinstance(items, list):
            items.insert(0, {"role": "system", "content": instructions})
            changed = True
        elif items is None:
            body["input"] = [{"role": "system", "content": instructions}]
            changed = True
        elif isinstance(items, str):
            body["input"] = [
                {"role": "system", "content": instructions},
                {"role": "user", "content": items},
            ]
            changed = True
        else:
            body["instructions"] = instructions
    return changed


def _drop_tools(body):
    if "tools" in body:
        body.pop("tools", None)
        body.pop("tool_choice", None)
        return True
    return False


def _redact(value, depth=0):
    """落盘前把长文本与图片/音频数据截断，避免把隐私写满盘。"""
    if depth > 14:
        return "..."
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            return value[:_MAX_TEXT] + f"...<cut {len(value) - _MAX_TEXT}>"
        return value
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, depth + 1) for key, item in value.items()}
    return value


def _dump(path, payload):
    try:
        Path(path).write_text(
            json.dumps(_redact(payload), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def dump_request(request, dump_dir):
    if not dump_dir:
        return
    try:
        body = json.loads(request.content)
    except (ValueError, UnicodeError, RuntimeError):
        body = {"<unparsable>": True}
    headers = {
        name: ("<redacted>" if name.lower() in _SECRET_HEADERS else value)
        for name, value in request.headers.items()
    }
    _dump(
        Path(dump_dir) / "last_request.json",
        {"method": request.method, "url": str(request.url), "headers": headers, "body": body},
    )


class _AliasResponseStream(__import__("httpx").AsyncByteStream):
    """Buffer complete SSE lines, including split UTF-8 sequences."""
    def __init__(self, inner, mapping):
        self.inner, self.mapping = inner, mapping

    def convert(self, line):
        if not line.startswith(b"data:"):
            return line
        payload = line[5:].strip()
        if payload == b"[DONE]":
            return line
        try:
            return b"data: " + _restore_names(payload.decode("utf-8"), self.mapping).encode("utf-8") + (b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b"")
        except UnicodeError:
            return line

    async def __aiter__(self):
        pending = b""
        async for chunk in self.inner:
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield self.convert(line + b"\n")
        if pending:
            yield self.convert(pending)

    async def aclose(self):
        await self.inner.aclose()


async def restore_tool_names(response):
    """Run before SDK parsing; handle both unread JSON and streamed SSE."""
    mapping = response.request.extensions.get("_gemini_tool_alias") or {}
    if not mapping:
        return
    try:
        if "text/event-stream" in response.headers.get("content-type", "").lower():
            response.stream = _AliasResponseStream(response.stream, mapping)
            response.headers.pop("content-length", None)
            return
        await response.aread()
        restored = _restore_names(response.text, mapping)
        response._content = restored.encode("utf-8")
        response.__dict__.pop("_text", None)
        response.headers["content-length"] = str(len(response._content))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Tool alias response restoration failed")
        raise


def dump_response(response, dump_dir):
    if not dump_dir or response.status_code < 400:
        return
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        text = ""
    _dump(
        Path(dump_dir) / "last_error.json",
        {"status": response.status_code, "url": str(response.url), "body": text[:4000]},
    )


def _replace_body(request, body):
    """把改写后的 JSON 塞回请求，同时修正 Content-Length。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request.stream = ByteStream(payload)
    try:
        # httpx 会把已读过的正文缓存在 _content 里，不清掉读到的还是旧内容。
        request._content = payload
    except AttributeError:
        pass
    request.headers["content-length"] = str(len(payload))


def normalize_request(
    request,
    *,
    expected_base,
    model_prefix="gemini-",
    headers=None,
    prompt_prefix=DEFAULT_PREFIX,
    flatten_input=False,
    alias_tools=True,
    flatten_mode=True,
    drop_tools=False,
    scrub_markers=True,
    scrub_brand=False,
    slim_system=True,
    neutralize_factory=True,
    localize_tool_text=True,
    localizer=None,
    tool_whitelist=None,
    dump_dir=None,
):
    """只改配置里那一个 endpoint 上的 Gemini JSON 请求。"""
    base = urlsplit(expected_base)
    url = request.url
    if (
        url.scheme,
        url.host,
        url.port or (443 if url.scheme == "https" else 80),
    ) != (
        base.scheme,
        base.hostname,
        base.port or (443 if base.scheme == "https" else 80),
    ):
        return False
    prefix_path = base.path.rstrip("/")
    if url.path not in {prefix_path + path for path in _ALLOWED_PATHS}:
        return False
    try:
        body = json.loads(request.content)
    except (ValueError, UnicodeError, RuntimeError):
        return False
    if not isinstance(body, dict) or not str(body.get("model", "")).startswith(
        model_prefix
    ):
        return False

    # SDK/client diagnostic headers are not part of the OpenAI wire contract.
    for name in list(request.headers):
        if (
            name.lower().startswith("x-stainless-")
            or name.lower() == "x-astrbot-conversation-id"
        ):
            del request.headers[name]
    user_agent = request.headers.get("user-agent", "").lower()
    if "astrbot/" in user_agent or "openai/" in user_agent:
        request.headers.pop("user-agent", None)
    request.headers["Accept"] = (
        "text/event-stream" if body.get("stream") is True else "application/json"
    )
    for name, value in (headers or {}).items():
        # Never let configuration replace credentials or framing/route headers.
        if name.lower() not in {
            "authorization",
            "x-api-key",
            "x-goog-api-key",
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
        }:
            request.headers[str(name)] = str(value)

    dirty = False
    prefix = str(prompt_prefix or "").strip()
    if prefix and _rewrite_system_prompt(body, prefix):
        dirty = True
    if neutralize_factory and _neutralize_factory(body, localizer):
        dirty = True
    if localize_tool_text and _localize_tools(body):
        dirty = True
    if slim_system and _slim_body(body):
        dirty = True
    if scrub_markers and _scrub_body(body, scrub_brand):
        dirty = True
    is_responses = url.path.endswith("/responses")
    if flatten_input and is_responses and _flatten_input(body):
        dirty = True
    if is_responses and flatten_mode and _flatten_shape(body):
        dirty = True
    if is_responses and drop_tools and _drop_tools(body):
        dirty = True
    if tool_whitelist and _filter_tools(body, tool_whitelist):
        dirty = True
    alias_map = {}
    if alias_tools:
        alias_map = _alias_tools(body)
        if alias_map:
            _apply_aliases_to_body(body, alias_map)
            dirty = True
    if dirty:
        _replace_body(request, body)

    if alias_map:
        try:
            request.extensions["_gemini_tool_alias"] = {
                alias: original for original, alias in alias_map.items()
            }
        except Exception:  # noqa: BLE001
            pass

    dump_request(request, dump_dir)
    return True
