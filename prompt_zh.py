"""出厂英文提示词中文化（合并自 astrbot_plugin_prompt_override 的思路）。

只做一件事：把 AstrBot 出厂自带的那几段英文系统提示词换成中文。

为什么要换：上游中转站（Antigravity 一类）会对请求里若干英文枚举短语做内容过滤，
命中后不回正常错误，而是直接甩一个假 429（"All available accounts are currently
rate-limited"），再往上抛就成了 503 "No available accounts"。出厂的本地模式提示词
里那句
    list, poll, write raw text or complete lines to, interrupt, or terminate
与社区定位到的触发串逐字相同（CLIProxyAPI issue #7553）。

两条自我约束：

1. **等义**。中文版是原文的逐句对应，不增不减。出厂原文说的是沙盒环境，就不能
   写成"宿主本地执行能力"；原文只说 shell 与 Python，就不能替它扩权。
2. **不写死平台**。出厂原文本来就按 platform.system() 和 PowerShell 版本自动切换，
   中文版同样在运行时探测，换台机器装上也不会给模型错误的运行环境信息。
   要覆盖就填 auto 以外的文本，可用 {os} 与 {shell_hint} 两个占位符。
"""

from __future__ import annotations

import platform
import re
import shutil

# ---------------------------------------------------------------------------
# 出厂英文锚点
# ---------------------------------------------------------------------------

_SANDBOX_RE = re.compile(
    r"You have access to[^\r\n]*sandbox[^\r\n]*(?:\r?\n|$)", re.I
)
_TOOLCALL_RE = re.compile(r"When using tools[^\r\n]*(?:\r?\n|$)")
_TOOLCALL_SKILLS_RE = re.compile(
    r"You MUST NOT return an empty response.*?throughout the conversation\.", re.S
)
# 工作区那段有三行英文，得整块吃掉，只换第一句会留下半截英文。
_WORKSPACE_RE = re.compile(
    r"Current workspace:\s*`(?P<path>[^`]*)`\..*?do not assume this behavior for other tools\.",
    re.S | re.I,
)
_SHELL_RE = re.compile(
    r"Local computer tools are available.*?for ordinary long-running commands\.",
    re.S,
)

# ---------------------------------------------------------------------------
# 中文默认值（出厂原文的等义翻译）
# ---------------------------------------------------------------------------

# 原文：You have access to a sandboxed environment and can execute shell
#       commands and Python code securely.
DEFAULT_SANDBOX_CN = (
    "我可以使用一个沙盒环境，能够安全地执行 shell 命令和 Python 代码。"
)

# 原文：When using tools: never return an empty response; briefly explain the
#       purpose when starting a new type of task, but not before every tool
#       call; follow the tool schema exactly and do not invent parameters;
#       keep the conversation style consistent.
DEFAULT_TOOLCALL_CN = (
    "使用工具时：绝不返回空回复；在开始一类新任务时简要说明目的，"
    "但不必在每次工具调用前都说明；严格遵循工具的参数定义，不臆造参数；"
    "保持对话风格一致。"
)

# 原文是 skills-like 模式那一版，语气更强，且说明参数分两步下发。
DEFAULT_TOOLCALL_SKILLS_CN = (
    "我绝不返回空回复，尤其是在调用工具之后。"
    "在调用任何工具之前，先给用户一句简短的说明，讲清这次调用的目的。"
    "工具的参数定义分两步下发：先只给名称与描述；"
    "我决定使用某个工具后，完整参数定义会在下一步给出。"
    "在看到参数定义之前，不要猜参数。"
    "工具调用完成后，必须把工具返回的结果向用户简要总结。"
    "整段对话中保持角色扮演与风格一致。"
)

# 原文：Current workspace: `{path}`. ... do not assume this behavior for other tools.
DEFAULT_WORKSPACE_CN = (
    "当前工作区：`{workspace}`。"
    "`astrbot_execute_shell` 与 `astrbot_execute_python` 以它为工作目录。"
    "`astrbot_file_read_tool`、`astrbot_file_write_tool`、"
    "`astrbot_file_edit_tool` 与 `astrbot_grep_tool` 以它为基准解析相对路径。"
    "优先使用工作区内的相对路径；不要假定其他工具也如此。"
)


def _resolve_shell_kind():
    """按出厂那套口径判断当前运行环境。

    返回 (系统名, unix / pwsh / powershell5)。
    """
    system_name = platform.system() or "Unknown"
    if system_name.lower() != "windows":
        return system_name, "unix"
    try:
        from astrbot.core.astr_main_agent import resolve_windows_shell

        shell = resolve_windows_shell()
        return system_name, "pwsh" if str(shell).lower() == "pwsh.exe" else "powershell5"
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("pwsh.exe") or shutil.which("pwsh"):
        return system_name, "pwsh"
    return system_name, "powershell5"


def shell_hint_cn(kind):
    if kind == "unix":
        return "运行时 shell 是 Unix 系，使用 POSIX 兼容命令。"
    if kind == "pwsh":
        return (
            "运行时 shell 是 PowerShell 7（pwsh.exe）。"
            "使用兼容 PowerShell 7 的语法与 cmdlet，"
            "不要假定有完整的 Unix 用户态或 GNU 工具可用。"
        )
    return (
        "运行时 shell 是 Windows PowerShell 5.1（powershell.exe）。"
        "使用兼容 Windows PowerShell 5.1 的语法与 cmdlet；"
        "不要使用 PowerShell 7 专有语法，"
        "也不要假定 cat、ls、grep 这类 Unix 命令可用。"
    )


def dynamic_shell_prompt():
    """生成与当前机器相符的中文 shell 提示词。

    结构与出厂原文一一对应，只把枚举里的英文短语换成中文。
    """
    system_name, kind = _resolve_shell_kind()
    return (
        "本地计算机工具可用，受我所在角色的文件与执行权限限制。"
        f"当前操作系统：{system_name}。"
        f"{shell_hint_cn(kind)} "
        "本地 shell 命令若超出初次等待时间仍未结束，会自动转为受管会话。"
        "使用 astrbot_shell_session 列出、轮询、写入原始文本或整行内容、"
        "中断或终止这些会话。"
        "面向行的程序请使用它的 write_line 动作，确保会话收到真正的换行。"
        "普通长命令不要自行添加 &、nohup 或其他分离包装。"
    )


def _cfg(settings, key, default):
    """取配置项。

    注意：配置里没有这个键才用默认值；显式写成空字符串表示「这一段不换」，
    不能悄悄回落到默认值，否则用户想关掉某段就关不掉。
    """
    if not isinstance(settings, dict) or key not in settings:
        return default
    value = settings.get(key)
    return default if value is None else value


def _render_shell(settings):
    """配置里填 auto 就走运行时探测，填别的就照填（支持占位符）。"""
    raw = str(_cfg(settings, "prompt_zh_shell", "auto")).strip()
    if raw.lower() in {"", "auto", "default", "detect"}:
        return dynamic_shell_prompt()
    system_name, kind = _resolve_shell_kind()
    return raw.replace("{os}", system_name).replace("{shell_hint}", shell_hint_cn(kind))


def _replace_workspace(text, template, custom_path=""):
    match = _WORKSPACE_RE.search(text)
    if not match:
        return text, False
    final_path = str(custom_path or "").strip() or match.group("path")
    replacement = str(template).replace("{workspace}", final_path)
    return _WORKSPACE_RE.sub(lambda _: replacement, text, count=1), True


def localize(system_prompt, is_group, settings):
    """把出厂英文提示词换成中文。

    返回 (新提示词, 是否有改动)。纯字符串替换，不碰任何数据结构。
    """
    if not isinstance(system_prompt, str) or not system_prompt:
        return system_prompt, False

    text = system_prompt
    changed = False

    sandbox = str(_cfg(settings, "prompt_zh_sandbox", DEFAULT_SANDBOX_CN))
    if sandbox:
        text, hit = _SANDBOX_RE.subn(lambda _: sandbox + "\n", text, count=1)
        changed = changed or hit > 0

    # 两种工具调用规范互斥，谁在文里就换谁。
    toolcall = str(
        _cfg(settings, "prompt_zh_toolcall", DEFAULT_TOOLCALL_CN)
    )
    if toolcall:
        text, hit = _TOOLCALL_RE.subn(lambda _: toolcall + "\n", text, count=1)
        changed = changed or hit > 0
    toolcall_skills = str(
        _cfg(settings, "prompt_zh_toolcall_skills", DEFAULT_TOOLCALL_SKILLS_CN)
    )
    if toolcall_skills:
        text, hit = _TOOLCALL_SKILLS_RE.subn(lambda _: toolcall_skills, text, count=1)
        changed = changed or hit > 0

    workspace = str(_cfg(settings, "prompt_zh_workspace", DEFAULT_WORKSPACE_CN))
    if workspace:
        text, hit = _replace_workspace(
            text, workspace, _cfg(settings, "prompt_zh_workspace_path", "")
        )
        changed = changed or hit

    shell = _render_shell(settings)
    if shell:
        text, hit = _SHELL_RE.subn(lambda _: shell, text, count=1)
        changed = changed or hit > 0

    return text, changed
