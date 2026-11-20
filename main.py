"""Opt-in per-source adapter; never patches SDK classes or core files."""
import asyncio
import json
import pathlib
import tempfile
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.web import error_response, json_response, request
from astrbot.api.star import Context, Star, register

from .profiles import PROMPT_KEYS, apply_text_rules, merged_prompts, select_profile
from .prompt_zh import (
    DEFAULT_SANDBOX_CN,
    DEFAULT_TOOLCALL_CN,
    DEFAULT_TOOLCALL_SKILLS_CN,
    DEFAULT_WORKSPACE_CN,
    localize,
)

# 面板上允许直接改的文本项；其它键一律不认，避免把内部状态写进配置。
PROMPT_TEXT_KEYS = (
    "prompt_zh_sandbox",
    "prompt_zh_toolcall",
    "prompt_zh_toolcall_skills",
    "prompt_zh_workspace",
    "prompt_zh_workspace_path",
    "prompt_zh_shell",
)
PROMPT_BOOL_KEYS = ("prompt_zh_enabled", "prompt_zh_all_providers", "prompt_zh_tools", "prompt_zh_extra")
PROMPT_DEFAULTS = {
    "prompt_zh_sandbox": DEFAULT_SANDBOX_CN,
    "prompt_zh_toolcall": DEFAULT_TOOLCALL_CN,
    "prompt_zh_toolcall_skills": DEFAULT_TOOLCALL_SKILLS_CN,
    "prompt_zh_workspace": DEFAULT_WORKSPACE_CN,
    "prompt_zh_workspace_path": "",
    "prompt_zh_shell": "auto",
}

from .transport import (
    DEFAULT_PREFIX,
    dump_response,
    normalize_request,
    restore_tool_names,
)


@register(
    "astrbot_plugin_gemini_transport",
    "苏九儿",
    "Gemini 等多模型请求转换：提示词中文化、请求头清洗，不拍平正文",
    "v0.12.1",
)
class GeminiTransport(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.settings = config if config is not None else {}
        self._attached = {}
        self._task = None
        self._warned = set()

    def _migrate_keys(self):
        """老配置里的 tavern_* 键换成 flatten_mode / slim_system，只搬一次。"""
        renamed = {"tavern_mode": "flatten_mode", "tavern_system": "slim_system"}
        touched = False
        for old, new in renamed.items():
            if old in self.settings:
                if new not in self.settings:
                    self.settings[new] = self.settings[old]
                try:
                    del self.settings[old]
                except Exception:  # noqa: BLE001
                    pass
                touched = True
        for rule in self.settings.get("profiles") or []:
            if not isinstance(rule, dict):
                continue
            for old, new in renamed.items():
                if old in rule:
                    rule.setdefault(new, rule[old])
                    rule.pop(old, None)
                    touched = True
        if touched:
            saver = getattr(self.settings, "save_config", None)
            if callable(saver):
                try:
                    saver()
                except Exception:  # noqa: BLE001
                    pass

    # 老版本默认值里写死过 Windows 和「宿主本地执行能力」，
    # 这些文本一旦落到用户配置里，就会盖掉自动探测，在 Linux 上给模型
    # 报错的环境信息。启动时认出来就纠正回默认值。
    _LEGACY_SHELL_MARK = "当前操作系统为 Windows，shell 环境是 Windows PowerShell 5.1"
    _LEGACY_SANDBOX_MARK = "我运行在 AstrBot 框架上，拥有宿主本地的 shell 与 Python 执行能力"

    def _migrate_prompt_texts(self):
        touched = False
        shell = str(self.settings.get("prompt_zh_shell") or "")
        if shell and self._LEGACY_SHELL_MARK in shell:
            self.settings["prompt_zh_shell"] = "auto"
            touched = True
        sandbox = str(self.settings.get("prompt_zh_sandbox") or "")
        if sandbox and self._LEGACY_SANDBOX_MARK in sandbox:
            self.settings["prompt_zh_sandbox"] = DEFAULT_SANDBOX_CN
            touched = True
        if touched:
            logger.info("[GeminiTransport] 已把写死平台的旧提示词纠正为默认（shell=auto）")
            saver = getattr(self.settings, "save_config", None)
            if callable(saver):
                try:
                    saver()
                except Exception:  # noqa: BLE001
                    pass
        return touched

    def _custom_headers(self):
        value = self.settings.get("custom_headers", "{}")
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("custom_headers must be a JSON object")
        return value

    def _prompt_prefix(self):
        value = self.settings.get("rewrite_system_prefix", DEFAULT_PREFIX)
        if value is None:
            return ""
        value = str(value)
        return "" if value.strip().lower() in {"", "off", "false", "none"} else value

    def _write_state(self, report):
        """把当前挂载状态写到插件目录，方便不看日志也能确认钩子挂没挂上。

        Linux 容器里插件目录常常只读，写不进去就退到系统临时目录，
        免得整个钩子因为落盘失败被拖死。
        """
        text = json.dumps(report, ensure_ascii=False, indent=1)
        for path in self._state_candidates():
            try:
                path.write_text(text, encoding="utf-8")
                return
            except Exception:  # noqa: BLE001
                continue

    def _state_candidates(self):
        here = pathlib.Path(__file__).with_name("attach_state.json")
        return [here, pathlib.Path(tempfile.gettempdir()) / "astrbot_gemini_transport_state.json"]

    def _route_slugs(self):
        """面板按插件自己的名字拼路由，这里不能写死。

        以前路由写死成 /sujiuer_gemini_transport/*，插件目录一改名，
        面板拿新名字来匹配就全落空，控制台一条供应商都读不出来。
        """
        slugs = []
        name = str(getattr(self, "name", "") or "").strip()
        if name:
            slugs.append(name)
        dirname = pathlib.Path(__file__).resolve().parent.name
        if dirname and dirname not in slugs:
            slugs.append(dirname)
        return slugs or ["astrbot_plugin_gemini_transport"]

    async def initialize(self):
        try:
            for slug in self._route_slugs():
                self.context.register_web_api(
                    f"/{slug}/state", self.api_state, ["GET"], "拦截状态与提供商列表"
                )
                self.context.register_web_api(
                    f"/{slug}/config", self.api_get_config, ["GET"], "获取插件配置"
                )
                self.context.register_web_api(
                    f"/{slug}/save", self.api_save_config, ["POST"], "保存插件配置"
                )
        except Exception as e:
            logger.warning(f"[GeminiTransport] register_web_api failed: {e}")
        self._migrate_keys()
        self._migrate_prompt_texts()
        self._custom_headers()
        self._sync()
        self._task = asyncio.create_task(self._watch())

    def _sync(self):
        wanted = set(str(x) for x in self.settings.get("source_ids", ["反重力哈基米"]))
        seen = set()
        report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "attached": [], "skipped": []}
        if not self.settings.get("enabled", True):
            self._detach_all()
            report["skipped"].append({"reason": "disabled"})
            self._write_state(report)
            return
        for provider in self.context.get_all_providers():
            cfg = getattr(provider, "provider_config", {})
            prov_id = str(cfg.get("id"))
            profile = select_profile(self.settings, cfg)
            if profile is None:
                continue
            model = str(cfg.get("model", ""))
            base = str(cfg.get("api_base", "")).rstrip("/")
            client = getattr(getattr(provider, "client", None), "_client", None)
            hooks = getattr(client, "event_hooks", None)
            if not base or not isinstance(hooks, dict):
                if prov_id not in self._warned:
                    logger.error(
                        "[GeminiTransport] incompatible provider transport: %s; not patched",
                        prov_id,
                    )
                    self._warned.add(prov_id)
                report["skipped"].append(
                    {
                        "id": prov_id,
                        "reason": "no api_base or no httpx event_hooks",
                        "base": base,
                        "client": type(client).__name__,
                    }
                )
                continue
            key = id(client)
            seen.add(key)
            if key in self._attached:
                report["attached"].append(
                    {"id": prov_id, "model": model, "base": base, "state": "already"}
                )
                continue

            dump_dir = str(pathlib.Path(__file__).parent)

            async def hook(
                request,
                expected_base=base,
                provider_cfg=cfg,
                out_dir=dump_dir,
            ):
                profile = select_profile(self.settings, provider_cfg)
                if profile is None or not self.settings.get("enabled", True):
                    return
                headers = profile.get("custom_headers", "{}")
                if isinstance(headers, str):
                    headers = json.loads(headers)
                prefix = profile.get("rewrite_system_prefix", "")
                if str(prefix).strip().lower() in {"off", "false", "none"}:
                    prefix = ""
                # 中文化走同一份 prompt_zh 文本，用户改了配置这里也跟着改。
                settings_snapshot = dict(self.settings)

                def localizer(text, _settings=settings_snapshot):
                    # 总开关关掉时，HTTP 这一层也不许再动手。
                    if not _settings.get("prompt_zh_enabled", True):
                        return text, False
                    return localize(text, False, _settings)

                matched = normalize_request(
                    request, expected_base=expected_base, model_prefix="",
                    headers=headers, prompt_prefix=prefix,
                    flatten_input=bool(profile.get("flatten_mode", False)),
                    flatten_mode=bool(profile.get("flatten_mode", False)),
                    drop_tools=bool(profile.get("drop_tools", False)),
                    scrub_markers=bool(profile.get("scrub_markers", False)),
                    alias_tools=bool(profile.get("alias_tools", False)),
                    scrub_brand=bool(profile.get("scrub_brand", False)),
                    slim_system=bool(profile.get("slim_system", False)),
                    neutralize_factory=bool(profile.get("neutralize_factory", True)),
                    localize_tool_text=bool(profile.get("prompt_zh_tools", True)),
                    localizer=localizer,
                    tool_whitelist=profile.get("tool_whitelist") or [],
                    dump_dir=out_dir,
                )
                if matched:
                    apply_text_rules(request, profile.get("text_replacements", "[]"))

            async def resp_hook(response, out_dir=dump_dir):
                await restore_tool_names(response)
                dump_response(response, out_dir)
                return response

            hooks.setdefault("request", []).append(hook)
            hooks.setdefault("response", []).append(resp_hook)
            self._attached[key] = (client, (hook, resp_hook))
            logger.info(
                "[GeminiTransport] attached source-scoped hook: %s (%s)", prov_id, base
            )
            report["attached"].append(
                {"id": prov_id, "model": model, "base": base, "state": "new"}
            )
        for key in list(self._attached):
            if key not in seen:
                self._detach(key)
        report["prefix"] = self._prompt_prefix()
        report["hook_total"] = len(
            [1 for client, _ in self._attached.values() if client is not None]
        )
        self._write_state(report)

    def _detach(self, key):
        client, pair = self._attached.pop(key)
        hooks = client.event_hooks
        for name, item in zip(("request", "response"), pair):
            bucket = hooks.get(name, [])
            if item in bucket:
                bucket.remove(item)

    def _detach_all(self):
        for key in list(self._attached):
            self._detach(key)

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            try:
                self._sync()
            except Exception:
                logger.exception(
                    "[GeminiTransport] sync failed; no global fallback patch applied"
                )


    async def _profile_for_event(self, event):
        """取这一轮命中的模型卡和供应商配置。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            return None, {}
        cfg = getattr(provider, "provider_config", {}) or {}
        return select_profile(self.settings, cfg), cfg

    async def _target_provider_matched(self, event) -> bool:
        """判断这一轮用的提供商是不是规则里指定的源。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            return bool(self.settings.get("prompt_zh_all_providers", False))
        cfg = getattr(provider, "provider_config", {}) or {}
        if select_profile(self.settings, cfg) is not None:
            return True
        return bool(self.settings.get("prompt_zh_all_providers", False))

    @filter.on_llm_request()
    async def zh_prompt(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """出厂英文提示词中文化。

        纯字符串替换：不碰正文、图片、工具 schema、历史与鉴权。
        上游过滤的是那句英文枚举，换掉它，假 429 就不再出现。
        """
        if not self.settings.get("prompt_zh_enabled", True):
            return
        if not getattr(req, "system_prompt", ""):
            return
        try:
            if not await self._target_provider_matched(event):
                return
            profile, _cfg = await self._profile_for_event(event)
            if profile is None and not self.settings.get("prompt_zh_all_providers", False):
                return
            use = merged_prompts(self.settings, profile or {})
            is_group = "groupmessage" in (event.unified_msg_origin or "").lower()
            new_text, changed = localize(req.system_prompt, is_group, use)
            if changed:
                req.system_prompt = new_text
        except Exception:  # noqa: BLE001
            logger.exception("[GeminiTransport] 提示词中文化失败，本轮保持原样")

    async def api_state(self):
        providers = {}
        for prov in self.context.get_all_providers():
            c = getattr(prov, "provider_config", {}) or {}
            pid = str(c.get("id", ""))
            base = str(c.get("api_base", "")).rstrip("/")
            client = getattr(getattr(prov, "client", None), "_client", None)
            hooks = getattr(client, "event_hooks", None)
            providers[pid] = {
                "id": pid,
                "source_id": str(c.get("provider_source_id", "")),
                "model": str(c.get("model", "")),
                "api_base": base,
                "hookable": isinstance(hooks, dict) and bool(base),
            }
        known = {p["id"] for p in providers.values()} | {
            p["source_id"] for p in providers.values() if p["source_id"]
        }
        wanted = set()
        if self.settings.get("enabled", True):
            wanted.update(str(x) for x in (self.settings.get("source_ids") or []))
            for rule in self.settings.get("profiles") or []:
                if not isinstance(rule, dict) or not rule.get("enabled", True):
                    continue
                wanted.update(str(x) for x in (rule.get("source_ids") or []))
        missing = sorted(x for x in wanted if x and x not in known)

        report = {}
        report_file = None
        for path in self._state_candidates():
            if path.exists():
                report_file = path
                break
        if report_file is not None:
            try:
                report = json.loads(report_file.read_text("utf-8"))
            except Exception:  # noqa: BLE001
                report = {}
        return json_response(
            {
                "success": True,
                "providers": list(providers.values()),
                "attached": report.get("attached", []),
                "skipped": report.get("skipped", []),
                "missing": missing,
                "synced_at": report.get("ts", ""),
            }
        )

    async def api_get_config(self):
        data = dict(self.settings) if isinstance(self.settings, dict) else {}
        data.pop("custom_headers", None)
        # 没存过的项也把默认值给出去，面板上才好照着改。
        for key, default in PROMPT_DEFAULTS.items():
            # 空字符串要原样留着，它表示「这一段不替换」，不能填回默认值。
            if key not in data or data[key] is None:
                data[key] = default
        for key in PROMPT_BOOL_KEYS:
            data.setdefault(key, key in ("prompt_zh_enabled", "prompt_zh_tools", "prompt_zh_extra"))
        # 总闸默认开：老配置里没这一项时，面板要显示成开着的。
        data.setdefault("all_gemini_default", True)
        return json_response({"success": True, "config": data})

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload:
            return error_response("请求正文必须是非空 JSON 对象")
        try:
            has_profiles = "profiles" in payload
            profiles = payload.get("profiles") or []
            if not isinstance(profiles, list):
                raise ValueError("profiles 必须是列表")
            cleaned = []
            for index, rule in enumerate(profiles, 1):
                if not isinstance(rule, dict):
                    raise ValueError("第 %d 条规则不是对象" % index)
                sources = [str(x).strip() for x in (rule.get("source_ids") or []) if str(x).strip()]
                if not sources:
                    raise ValueError("第 %d 条规则没填源 ID，会匹配不到任何模型" % index)
                replacements = rule.get("text_replacements") or []
                if isinstance(replacements, str):
                    replacements = json.loads(replacements or "[]")
                if not isinstance(replacements, list):
                    raise ValueError("第 %d 条规则的替换词条必须是数组" % index)
                pairs = []
                for item in replacements:
                    if not isinstance(item, dict):
                        continue
                    find = str(item.get("find", ""))
                    if not find:
                        continue
                    pairs.append({"find": find, "replace": str(item.get("replace", ""))})
                cleaned.append(
                    {
                        "name": str(rule.get("name") or "自定义规则"),
                        "enabled": bool(rule.get("enabled", True)),
                        "source_ids": sources,
                        "models": [str(x) for x in (rule.get("models") or ["*"]) if str(x).strip()] or ["*"],
                        "rewrite_system_prefix": str(rule.get("rewrite_system_prefix") or ""),
                        "text_replacements": pairs,
                        "alias_tools": bool(rule.get("alias_tools", False)),
                        "scrub_markers": bool(rule.get("scrub_markers", False)),
                        "flatten_mode": bool(rule.get("flatten_mode", False)),
                        "slim_system": bool(rule.get("slim_system", False)),
                        "neutralize_factory": bool(rule.get("neutralize_factory", True)),
                        "prompt_zh_tools": bool(rule.get("prompt_zh_tools", True)),
                        "prompt_zh_extra": bool(rule.get("prompt_zh_extra", True)),
                        "drop_tools": bool(rule.get("drop_tools", False)),
                        "scrub_brand": bool(rule.get("scrub_brand", False)),
                        "custom_headers": rule.get("custom_headers") or "{}",
                        "prompts": {
                            key: str(value)
                            for key, value in (rule.get("prompts") or {}).items()
                            if key in PROMPT_KEYS and isinstance(value, str)
                        },
                    }
                )
            if has_profiles:
                payload["profiles"] = cleaned
            for key in PROMPT_TEXT_KEYS:
                if key in payload and not isinstance(payload[key], str):
                    raise ValueError("%s 必须是文本" % key)
            for key in PROMPT_BOOL_KEYS:
                if key in payload:
                    payload[key] = bool(payload[key])
            if "source_ids" in payload and not isinstance(payload["source_ids"], list):
                raise ValueError("默认规则的源 ID 必须是列表")
            if "all_gemini_default" in payload:
                payload["all_gemini_default"] = bool(payload["all_gemini_default"])
            self.settings.update(payload)
            saver = getattr(self.settings, "save_config", None)
            if callable(saver):
                saver()
            self._migrate_prompt_texts()
            self._sync()
            return json_response(
                {"success": True, "message": "已保存并同步", "profiles": len(cleaned)}
            )
        except json.JSONDecodeError as exc:
            return error_response("替换词条不是合法 JSON：%s" % exc)
        except ValueError as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[GeminiTransport] save config failed")
            return error_response("保存失败：%s" % exc)

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._detach_all()
