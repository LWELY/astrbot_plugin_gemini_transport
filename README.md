# 哈基咪等模型假报错转换 (astrbot_plugin_gemini_transport)

版本：`v0.12.1`  
作者：苏九儿  
仓库：https://github.com/LWELY/astrbot_plugin_gemini_transport  
致谢：合并参考了 astrbot_plugin_prompt_override 的出厂词中文化思路。

---

## 一、插件定位与设计理念

本插件专门解决 **Gemini 等中转源在 AstrBot 框架下的兼容性与假 429 报错**，并全面支持**多模态（文字+图片）及大型插件生态**原生调用。

在 `v0.10.0` 中，本插件完成了与 `astrbot_plugin_prompt_override`（出厂提示词覆盖）的**深度合体**与**减法瘦身**：
1. **大道至简（语义级中文化）**：依据 CLIProxyAPI issue #7553 确证，上游中转站（如 Antigravity / blameupstream）的内容过滤命中出厂英文短语 `list, poll, write raw text or complete lines to, interrupt, or terminate` 时，会直接抛出假 429 / 503（"All available accounts are currently rate-limited" 或 "No available accounts"）。合体后直接在请求前将出厂英文段落正则替换为纯中文版，不触发上游内容审查。
2. **拒绝过度改造（保全多模态与工具生态）**：
   - **坚决关闭正文拍平 (`flatten_mode: false`)**：不再破坏 Responses API 规范中带图片的 `type: message` 结构，彻底解决发送「文字 + 图片」时上游反序列化失败导致的崩溃。
   - **原生工具直通 (`alias_tools: false`, `tool_whitelist: []`)**：不再剥离 `astrbot_` 前缀并在流式返回里复杂反向映射，不再过滤工具，零中间层开销，让 CAD、本机 QQ 音乐、代码执行、文件读写等全部大型插件直接原生调用。
   - **请求头脱敏**：剥离 `x-stainless-*` 诊断头、`x-astrbot-conversation-id` 及自带客户端特征的 User-Agent，让请求干干净净出境。

---

## 二、核心特性

| 功能模块 | 实现机制 | 效果 |
|---|---|---|
| **出厂提示词中文化** | `@filter.on_llm_request()` 内存级正则替换 | 根治上游假 429 / 503；Windows 与 Linux 的 shell 提示词按运行环境自动生成 |
| **多模态完整保全** | 保持 Responses API 原生对象结构，不强行字符串化 | 纯文本、多轮对话、引用回复、带图提问全部正常支持 |
| **HTTP 头清洗** | HTTPX Request Hook 底层自动剔除指纹头 | 抹除 SDK 与客户端特征头，降低被风控分桶风险 |
| **零性能损耗** | 纯文本替换 + 请求头修剪，无复杂 JSON 递归重写 | 延迟最低，流式响应不卡顿、不切片乱码 |
| **可视化控制台** | 挂载面板 WebUI 独立管理页面 | 实时查看挂载状态、在线 Provider 列表与出站快照 |

---

## 三、提示词全部可改

五段提示词加一个路径覆盖，既能在「面板 → 插件 → 本插件 → 配置」里改，也能在插件控制台「出厂提示词」区直接改，不用碰代码：

| 配置键 | 对应出厂原文 |
|---|---|
| `prompt_zh_sandbox` | `SANDBOX_MODE_PROMPT` |
| `prompt_zh_toolcall` | `TOOL_CALL_PROMPT` |
| `prompt_zh_toolcall_skills` | `TOOL_CALL_PROMPT_SKILLS_LIKE_MODE` |
| `prompt_zh_workspace` | 工作区提示词，`{workspace}` 占位 |
| `prompt_zh_workspace_path` | 工作区路径覆盖，留空则沿用框架自动生成 |
| `prompt_zh_shell` | 本地模式 shell 提示词，默认 `auto` |

`prompt_zh_shell` 填 `auto` 会按当前系统与 PowerShell 版本在运行时生成；想自己写就填文本，可用 `{os}` 和 `{shell_hint}` 两个占位符。两层（请求前 / HTTP 兜底）用的是同一份文本，改一处两边都变。

## 三·五、配置项说明

配置文件路径：`data/config/astrbot_plugin_gemini_transport_config.json`，也可在面板直接修改：

```json
{
  "enabled": true,
  "source_ids": [
    "反重力哈基米"
  ],
  "prompt_zh_enabled": true,
  "prompt_zh_all_providers": false,
  "flatten_mode": false,
  "alias_tools": false,
  "tool_whitelist": [],
  "rewrite_system_prefix": "",
  "scrub_markers": true,
  "neutralize_factory": true
}
```

- **`enabled`** (`bool`): 插件总开关。
- **`all_gemini_default`** (`bool`): **默认 `true`**。总闸开着就不看供应商，只要模型名里带 gemini 就套这整套转换；关掉才退回 `source_ids` 白名单。
- **`source_ids`** (`list`): 白名单，仅在 `all_gemini_default` 关闭时生效，默认 `["反重力哈基米"]`。
- **`prompt_zh_enabled`** (`bool`): 出厂英文提示词中文化总开关，默认 `true`。
- **`prompt_zh_all_providers`** (`bool`): 是否对所有提供商生效（默认 `false`，仅白名单生效）。
- **`flatten_mode`** (`bool`): **严禁开启（默认 `false`）**。开启会强行拍平多模态消息，导致图片报错。
- **`alias_tools`** (`bool`): **默认 `false`**。关闭工具别名转换，直通调用。
- **`tool_whitelist`** (`list`): **默认 `[]`（全放行）**。空列表代表放行全部插件工具。
- **`rewrite_system_prefix`** (`string`): 系统提示词额外前缀，留空代表不加多余英文声明。

---

## 三·六、控制台

插件带一个独立页面（面板 → 插件 → 本插件 → 控制台），从上到下四段：

1. 状态条：钩子几个、上次同步时间、跳过了几个源；
2. 模型供应者：点开列出所有源，点一下选中，加卡片时自动带上；
3. 钩子模型：第一张就是「Gemini 模型（默认选中）」，右边可以一键恢复默认提示词；
4. 模型卡点开就是提示词：沙盒、工具规范两份、工作区、路径覆盖、shell，六项随便改。

要点：
- 每张自定义卡可以存自己的一份提示词，留空就沿用 Gemini 默认那份，不会互相污染；
- 改完点「保存生效」，钩子当场重挂，不用重启；
- 「正文拍平」「摘掉工具定义」这类标了「别开」，打开会弹确认，点错了当场退回；
- 某段提示词留空＝关掉这一段替换，不会回落成默认值。


## 三·七、跨平台

提示词里的 shell 那句不是写死的，运行时按 `platform.system()` 判断：

| 平台 | 生成内容 |
|---|---|
| Windows + powershell.exe | 说明是 Windows PowerShell 5.1，别用 PS7 语法，别假定有 cat/ls/grep |
| Windows + pwsh.exe | 说明是 PowerShell 7，别假定有完整 Unix 用户态 |
| Linux / macOS | 说明是 Unix 系，用 POSIX 兼容命令，路径用 `/`，文件名区分大小写 |

`prompt_zh_shell` 填 `auto`（默认）就走上面这套；想自己写就填文本，支持 `{os}` 与 `{shell_hint}` 两个占位符。模型看到的运行环境描述永远和真实机器一致，不会在 Linux 上告诉模型自己在用 PowerShell。

代码层面没有 Windows 专属调用：路径用 `pathlib`，写盘统一 `encoding="utf-8"`，没有注册表、没有 `startfile`、没有写死的盘符。

Linux 上还有一处专门的退让：容器里插件目录经常是只读的，状态文件 `attach_state.json` 与转储 `last_request.json`、`last_error.json` 写不进去时，会自动改写到系统临时目录（`tempfile.gettempdir()`），钩子不会因为落盘失败而整个失效。

> Linux 备注：`auto` 只按 `platform.system()` 和 `pwsh` 是否存在来判断，遇到容器、WSL、Windows 上跑 Git Bash 这类混着来的环境，可能跟真实 shell 对不上。真发现模型拿错环境说话，就让 bot 自己读一遍当前环境（`uname -a`、`$SHELL`、`which pwsh`），把 `prompt_zh_shell` 从 `auto` 改成实测出来的那段文本，或者写成带 `{os}`、`{shell_hint}` 的短句，别硬撑着用 auto。

## 四、排查与调试

插件根目录下会自动生成脱敏的出境记录（不含 API Key）：
- `attach_state.json`: 当前 HTTPX 钩子挂载状态与 Provider 列表。
- `last_request.json`: 最近一次实际出境的脱敏请求报文。
- `last_error.json`: 最近一次上游返回的错误状态码与报错原文。

---

## 五、版本历史

- **v0.12.1 (2026-11-20)**：
  - `metadata.yaml` 补上仓库地址，市场与更新检查不会再因为缺 repo 失效；
  - 适配 Linux：插件目录只读时，状态与转储文件自动退到系统临时目录；
  - Unix 系 shell 提示词补上路径分隔符与大小写说明。
- **v0.12.0 (2026-11-20)**：
  - 新增总闸 `all_gemini_default`，默认开：不再按供应商源卡人，只要模型名里带 gemini 就整套生效；
  - 关掉总闸才退回老规矩，只认 `source_ids` 白名单里的 gemini 模型；
  - 面板「开关」区加同名开关，「生效的源」提示改成只在关闸时才管用。
- **v0.11.6 (2026-11-16)**：
  - 插件显示名改成「哈基咪等模型假报错转换」，控制台标题同步。
- **v0.11.5 (2026-11-16)**：
  - 面板配置里 shell 那项的说明不再只提 PowerShell，写清 Windows 分 5.1/7、Linux 与 macOS 走 POSIX；
  - 工作区正则加不区分大小写，出厂文案换大小写也认得；
  - README 补「跨平台」一节，列出三种平台各自生成什么。
- **v0.11.4 (2026-11-16)**：
  - 「模型前缀」标签后面直接写了写法 `（写法：gemini-*）`，底下补一句不能只写 gemini；
  - 自定义卡的「模型匹配」同样标了写法与多条规则。
- **v0.11.3 (2026-11-16)**：
  - 提示词改成一格一行满宽，长提示词不用挤在窄栏里；
  - 输入时文本框自动长高，最多放到 420 像素。
- **v0.11.2 (2026-11-16)**：
  - 提示词区改成固定两列并对齐，不再三列参差；
  - 工作区路径覆盖并进工作区那一格，少了半行空格子。
- **v0.11.1 (2026-11-16)**：
  - 页面背景换成与「苏九儿工具合集」同一套渐变加飘心，风格统一；
  - 分节标题也带上心形装饰。
- **v0.11.0 (2026-11-16)**：
  - 控制台按「钩子 → 模型供应者 → 钩子模型」重排，每张模型卡点开就是提示词，不再单独占一大块；
  - 每张自定义卡能单独存一份提示词，留空则沿用 Gemini 默认那份；
  - 插件显示名改成「Gemini 请求适配（提示词中文化）」，一眼看出它是给 Gemini 做的。
- **v0.10.7 (2026-11-16)**：
  - 控制台里「正文拍平」「摘掉工具定义」「连 AstrBot 字样也换掉」三项加备注并改为二次确认，误点会当场退回；
  - 选项区顶部加了一行说明，提醒这几项是排查用的。
- **v0.10.6 (2026-11-16)**：
  - 总开关关掉时 HTTP 层也停手，之前只有请求前那层听开关的；
  - 某段提示词留空＝关掉这一段替换，不再悄悄回落到默认值（`api_get_config` 也不再填空值）；
  - 沙盒正则收紧为必须匹配 sandbox，不再误伤其它以 "You have access to" 开头的句子。
- **v0.10.5 (2026-11-16)**：
  - 控制台新增「出厂提示词」区，五段文本与两个开关直接在页面上改，改完保存即时生效；
  - 提供「恢复默认提示词」按钮；
  - `api_get_config` 会带出默认值，没存过也能照着改；`api_save_config` 收提示词字段，不再要求必须带 profiles。
- **v0.10.4 (2026-11-16)**：
  - 启动时自动纠正历史配置里写死 Windows 的 shell 提示词与「宿主本地执行能力」那种扩权翻译；
  - 老用户升级后无需手动清配置，`prompt_zh_shell` 会自动回到 `auto`。
- **v0.10.3 (2026-11-16)**：
  - HTTP 层不再保留写死的中文提示词副本，统一走 `prompt_zh.py` 那一份配置；
  - 面板配置里改任何一段提示词，`on_llm_request` 与 HTTP 兜底两层同时生效。
- **v0.10.2 (2026-11-16)**：
  - 中文提示词全部改为出厂原文的等义翻译，不再把「沙盒环境」写成「宿主本地执行能力」；
  - shell 提示词不再写死 Windows，改为运行时按 `platform.system()` 与 PowerShell 版本自动生成，`{os}` / `{shell_hint}` 占位符可覆盖；
  - 工作区提示词整段替换，不再残留半截英文；补齐技能分步模式的工具规范。
- **v0.10.1 (2026-11-16)**：
  - 修复控制台读不到供应商：Web API 路由不再写死旧插件名，改为按插件自身名字注册；
  - 插件改名 `astrbot_plugin_gemini_transport`，显示名去掉个人署名，页面标题同步收敛。
- **v0.10.0 (2026-11-16)**：
  - 合并 `prompt_override` 的出厂提示词中文化能力，直接在 `on_llm_request` 改写出厂英文段；
  - 修正 shell 会话提示词为 Windows PowerShell 5.1 本地环境；
  - 彻底关闭 `flatten_mode` 与 `alias_tools`，修复「文字+图片」发送时 503 报错；
  - 放开工具白名单，全量大型插件原生直通；
  - 规范配置 schema，支持 WebUI 稳定保存。
- **v0.9.0 及早期版本**：
  - HTTPX Request Hook 实现、基础头清理与别名映射试验。
