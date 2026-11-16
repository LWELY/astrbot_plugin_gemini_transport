import fnmatch
import json

# 每张模型卡可以带自己的一份提示词，留空就沿用默认那份。
PROMPT_KEYS = (
    "prompt_zh_sandbox",
    "prompt_zh_toolcall",
    "prompt_zh_toolcall_skills",
    "prompt_zh_workspace",
    "prompt_zh_workspace_path",
    "prompt_zh_shell",
)


def merged_prompts(settings, profile):
    """把单张模型卡的提示词叠到全局设置上，空字符串视为「沿用默认」。"""
    merged = dict(settings or {})
    if not isinstance(profile, dict):
        return merged
    prompts = profile.get("prompts")
    if not isinstance(prompts, dict):
        return merged
    for key in PROMPT_KEYS:
        value = prompts.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value
    return merged


def select_profile(settings, cfg):
    for rule in settings.get("profiles", []) or []:
        if not rule.get("enabled", True):
            continue
        sources = rule.get("source_ids") or []
        if not sources or not ({str(cfg.get("id", "")), str(cfg.get("provider_source_id", ""))} & set(map(str, sources))):
            continue
        patterns = rule.get("models") or ["*"]
        if any(fnmatch.fnmatchcase(str(cfg.get("model", "")), pat) for pat in patterns):
            return dict(rule)
    if settings.get("legacy_enabled", True):
        sources = set(settings.get("source_ids", ["反重力哈基米"]))
        if (cfg.get("id") in sources or cfg.get("provider_source_id") in sources) and str(cfg.get("model", "")).startswith("gemini-"):
            return dict(settings)
    return None


def apply_text_rules(request, rules):
    if isinstance(rules, str):
        rules = json.loads(rules or "[]")
    if not isinstance(rules, list):
        raise ValueError("text_replacements must be a JSON array")
    if not rules:
        return
    body = json.loads(request.content)
    def replace(text):
        for rule in rules:
            old, new = rule.get("find", ""), rule.get("replace", "")
            if old:
                text = text.replace(old, new)
        return text
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                # Never touch tool schemas, names, arguments, IDs, or routing metadata.
                if key in {"tools", "arguments", "parameters"}:
                    continue
                if key in {"instructions", "content", "text"} and isinstance(item, str):
                    value[key] = replace(item)
                elif isinstance(item, (list, dict)):
                    visit(item)
    visit(body)
    from .transport import _replace_body
    _replace_body(request, body)
