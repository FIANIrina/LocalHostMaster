[English](README.md) | 中文

# LocalhostMaster

一个轻量级的 Windows 终端界面（TUI），用于查看本机正在监听的端口以及背后的进程。
基于 **Python 3**、**psutil**（直接调用 Windows 原生 IP Helper）与 **prompt_toolkit** 构建。

```
┌ LocalhostMaster 0.1.0 ── endpoints: 12/136 | sort: port filter: all | docker: ok ┐
│  PROTO  ADDRESS                 PORT     PID  PROCESS              CATEGORY     OPEN │
│  TCP    127.0.0.1               8081   28200  llama-server.exe     llama.cpp    yes  │
│  TCP    127.0.0.1               8082   21664  llama-server.exe     llama.cpp    yes  │
│  TCP    ::                      3002   30812  com.docker.backend   docker       yes  │
│  UDP    127.0.0.1              14022   15896  rustrover            dev-server   -    │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ [Enter] arm   [r] refresh   [/] search   [f] filter   [C] categories   [?] help      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

## 功能特性

- TCP **LISTEN** 与 UDP **bound** 端点，支持 IPv4 / IPv6。
- 可选的已连接 TCP 行（ESTABLISHED / TIME_WAIT / …）及其**远端地址**，仅在
  使用 `--show-established` 或按 `a` 时显示。
- 每个端点显示进程名、PID、由可执行文件派生的元数据以及分类。
- 双击 `Enter` 在**系统默认浏览器**中打开一个 TCP **监听**端点。
- 分类系统（内置 + 用户自定义），支持颜色、图标与匹配规则。
- 后台刷新不阻塞输入；Docker 增强为可选。
- 面向脚本的非交互式纯文本 / JSON 输出。

## 运行环境

- Windows 11（首个版本仅面向 Windows；保留了少量平台接缝）。
- Python 3.11+（**当前仅在 Python 3.14.6 上验证过**；开发机未安装 3.11，未做验证）。
- `psutil>=5.9`、`prompt_toolkit>=3.0`。

## 运行方式

在项目根目录下：

```powershell
$env:PYTHONPATH = "src"
python -m localhostmaster            # 全屏 TUI
python -m localhostmaster --once     # 扫描一次，输出纯文本后退出
python -m localhostmaster --json     # 扫描一次，输出 JSON 后退出
python -m localhostmaster --version
```

安装后即可使用 `localhostmaster` 控制台脚本。本地安装路径已在一次性虚拟环境中验证：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install . --no-deps
.venv\Scripts\localhostmaster --version     # localhostmaster 0.1.0
```

> 离线环境：由于构建后端需要 `setuptools`，可先预装它并使用
> `--no-build-isolation`，即可全程离线完成本地安装。

### 命令行选项

| 选项 | 含义 |
| --- | --- |
| `--version` | 打印版本并退出 |
| `--once` | 扫描一次，输出纯文本表格后退出 |
| `--json` | 扫描一次，输出 JSON 后退出 |
| `--show-established` | 包含非监听 TCP 连接（ESTABLISHED、TIME_WAIT 等）及其远端地址 |
| `--no-docker` | 关闭 Docker 增强 |
| `--config PATH` | 使用指定的 `config.toml` |
| `--refresh-ms N` | 覆盖自动刷新间隔（毫秒，最小 250） |
| `--no-color` | 关闭颜色 |
| `--debug` | 将滚动日志写入 `%LOCALAPPDATA%\LocalhostMaster\logs` |

当标准输出不是 TTY（被重定向 / 管道）时，程序会打印一次纯文本表格后退出，
而不会进入全屏界面。

## 快捷键

| 按键 | 操作 |
| --- | --- |
| `Up`/`Down` | 移动光标 |
| `PageUp`/`PageDown` | 翻页 |
| `Home`/`End` | 跳到首行 / 末行 |
| `Enter` | 预备；2.5 秒内再次按下则在浏览器中打开 |
| `k` | 预备强制关闭；2.5 秒内再次按下则终止进程 |
| `Esc` | 取消确认 / 关闭对话框 |
| `r` | 立即刷新 |
| `Space` | 暂停 / 恢复自动刷新 |
| `/` | 搜索（地址、端口、进程、分类） |
| `f` | 过滤（all / container / openable / 分类） |
| `s` | 切换排序（port / process / category / pid） |
| `a` | 切换是否显示已建立连接 |
| `c` | 将选中 TCP 的 URL 复制到剪贴板 |
| `C` | 分类管理器 |
| `?` | 帮助 |
| `q`、`Ctrl-C` | 退出 |

`j`/`k` 仍可在过滤、分类和图标选择列表中作为局部导航键使用；主端口列表只使用
`Up`/`Down`，因此主列表中的 `k` 含义明确、不会被导航占用。

### 双击 Enter

1. 第一次 `Enter` 只是**预备**（arm）选中端点；状态栏显示
   `Press Enter again within 2.5s to open …`。
2. 在 150 毫秒到 2500 毫秒之间的第二次 `Enter` 会在默认浏览器中**打开**它。
   前 150 毫秒内的按键（键盘自动重复）会被忽略。
3. 移动光标、搜索、过滤、排序、切换 scheme 或按 `Esc` 都会取消预备状态。

只有 TCP **LISTEN** 端点可以被打开。已连接的 TCP 行（ESTABLISHED、TIME_WAIT、
CLOSE_WAIT 等）与 UDP 行在 OPEN 列显示 `-`，永远不会进入预备状态；对这些行按
`Enter` 只会说明为何无法打开。表格会根据终端宽度自适应选列：在常见的中等/宽终端
上会显示 OPEN 列，只有极窄的终端才会省略 OPEN（以及其他列）。

### 双击 `k` 强制关闭

第一次 `k` 只是**预备**选中端点；状态栏显示
`Press k again within 2.5s to force-stop …`。在 150 毫秒到 2500 毫秒之间的
第二次 `k` 会提交终止。前 150 毫秒内的按键（键盘自动重复）会被忽略；超过
2.5 秒后再按 `k` 会作为一次新的确认。

安全规则：

- 终止的是该行对应的**整个宿主机进程**，而不是单个 socket。该进程占用的
  其他端口会一并消失，状态栏会提示可能影响的可见端点数。
- 不终止进程树、不递归终止子进程、不自动提权。只使用
  `psutil.Process(pid).kill()`，不调用 shell、PowerShell、`taskkill` 或
  `Stop-Process`。
- 只允许对 **TCP LISTEN** 与 **UDP BOUND** 行发起；已连接的 TCP 行
  （ESTABLISHED、TIME_WAIT 等）以及没有 PID 的行会被拒绝。
- 在真正终止前会再次校验进程身份（`PID` + 创建时间 + 端点），因此被复用的
  PID 不会被误杀；同时会重新扫描确认该端点仍属于同一 PID 且仍在监听。
- LocalhostMaster 自身、PID 0、PID 4、Windows 核心/服务进程以及 Docker 宿主
  代理（`com.docker.backend`、`wslrelay`、`dockerd`、`docker`）始终被拒绝。
- 无法读取 `create_time` 时默认拒绝，而不是在未验证身份的情况下终止。
- 终止在后台线程执行，界面不会卡顿，完成后立即刷新端口列表。
- 结果会区分**已退出**与**请求已发送但退出未确认**
  （`Force-stop was requested, but exit was not confirmed.`）。
  `AccessDenied` 属于正常的权限限制，不是缺陷。

## URL 规则

| 绑定地址 | 浏览器主机 |
| --- | --- |
| `0.0.0.0`、空 | `127.0.0.1` |
| `::`、空 | `[::1]` |
| `127.0.0.1` | `127.0.0.1` |
| `::1` | `[::1]` |
| 其他具体地址 | 原样使用（IPv6 会加方括号） |

scheme 默认是 `http`；分类可将其设为 `https`。只接受 `http` 与 `https`——
其他取值会被忽略并给出警告、回退到 `http`，因此任意 scheme 都不可能传递到
操作系统的 URL 处理器。IPv6 主机始终加方括号。不会进行任何 HTTP 探测。

## 配置

配置位于 `%APPDATA%\LocalhostMaster\`。

- `config.toml` —— 通用设置（用户维护）
- `categories.toml` —— 用户分类（由 TUI 管理，也可手工编辑）

在你第一次保存之前，这两个文件都不会被创建，因此开箱即用时会采用内置默认值。

### `config.toml`

```toml
[general]
refresh_ms = 3000
show_established = false
color_mode = "auto"      # auto | truecolor | 256 | 16 | none
docker_enabled = true
docker_ttl_s = 15
docker_timeout_s = 2
double_enter_ms = 2500
arm_guard_ms = 150
```

优先级：命令行参数 > 用户配置 > 内置默认值。

`refresh_ms` 会被钳制到最小 250 毫秒（并给出警告），因此错误的配置不会造成忙循环；
`--refresh-ms` 小于 250 会被直接拒绝并报错。`docker_timeout_s` 与 `docker_ttl_s`
同样会被钳制为正值。`arm_guard_ms` 会被钳制为不超过 `double_enter_ms` 的一半
（并给出警告），否则防连发窗口会吞掉每一次第二次按键，导致双击 Enter / 双击 `k`
永远无法触发。未知的 `color_mode` 会回退为 `auto` 并给出警告。

### `categories.toml`

```toml
# LocalhostMaster user categories.
# This file may be rewritten by the application; comments are not preserved.

[[category]]
id = "user.my-llama"
name = "my-llama"
color = "#7C3AED"
icon = "◆"
priority = 120
open_in_browser = true
scheme = "http"
process_globs = ["llama-server*", "llama-cli*"]

[[category]]
id = "user.grafana"
name = "grafana"
color = "#F59E0B"
priority = 80
ports = [3000]
port_ranges = ["3001-3010"]
protocols = ["TCP"]
```

`icon` 在界面中从预设图标列表选择（见下文 **分类图标**），并以实际字形保存。
文件格式保持不变，手写的 `icon` 值仍然可用。

匹配规则：

- **用户规则始终优先于内置规则。**
- 同一来源内：先按 `priority` 降序，再按文件顺序；首个匹配者胜出。
- 不同字段之间是 **AND** 关系；同一字段内多个 pattern 是 **OR** 关系。
- 任意 `exclude_*` 命中都会否决该规则。
- `process_globs`、`executable_globs`、`command_line_globs` 使用 glob，在 Windows
  上不区分大小写。以 `re:` 前缀的 pattern 是正则（Python `re`）；非法正则会
  被跳过并给出警告。
- 依赖命令行的规则在命令行不可读时不会匹配（见下方“权限”）。

写入是原子的（临时文件 + `os.replace`），损坏的文件永远不会被自动覆盖。

## 内置分类

| 分类 | 颜色 | 匹配 |
| --- | --- | --- |
| `llama.cpp` | 紫色 | `llama-server*`、`llama-cli*`、`llamafile*` |
| `docker` | 蓝色 | `com.docker.backend*`、`wslrelay*`、`dockerd*`、`docker*`，或任意已知容器映射 |
| `database` | 橙色 | `postgres*`、`mysqld*`、`mariadbd*`、`mongod*`、`redis-server*` |
| `dev-server` | 绿色 | `node*`、`deno*`、`bun*`、`python*`、`uvicorn*`、`vite*`、`webpack*`、`next*` |
| `system` | 灰色 | `svchost*`、`services*`、`wininit*`、`lsass*`、`System` |
| `unknown` | 灰色 | 兜底 |

分类始终以**文本**显示；颜色从不是唯一信号。在 `none` / `NO_COLOR` / `--no-color`
模式下，Unicode 图标会回退为 ASCII。

## 分类图标

分类表单不再接受自由文本图标输入。**Icon** 字段是只读选择器：按 `Enter` 或
`Space` 打开选择器，列表中的每一项都直接展示实际字形与可读名称（例如
`◆  Diamond`）。`Up`/`Down`（或 `j`/`k`）移动，`Enter`/`Space` 选择，`Esc`
返回表单且不改变任何内容。

- 新建与编辑的分类必须使用预设图标；默认为 `◆ Diamond`。
- 预设均为单宽度符号，在 Windows Terminal 中显示稳定：
  `◆`、`▣`、`▤`、`▸`、`•`、`·`、`●`、`■`、`▲`、`★`、`◇`、`□`、`○`、`◉`、`⬢`、`✦`。
- 只有在**保存**整个分类表单时，选中的字形才会写入 `categories.toml`；
  取消表单不会产生任何写入。
- 既有手写图标继续可用。不在预设列表中的图标在表单中显示为
  `Legacy/custom`，除非用户主动选择预设，否则会被保留；在 ASCII-only 模式下
  回退为 `*`。未知字形不会导致加载失败。

**Color** 字段必须是 prompt_toolkit 支持的颜色名（如 `red`、`ansiblue`）或
`#RGB`/`#RRGGBB`。非法取值会被表单拒绝；磁盘上已有的非法颜色也不会阻止界面启动
（会回退为默认灰色）。

## 权限

- 以普通用户身份即可运行，无需提权。
- socket 表、PID 与进程名对所有端点均可见。
- **其他用户拥有的进程（以及受保护的系统进程）的命令行，在没有管理员权限时
  不可读。** 当某条规则依赖命令行而命令行不可用时，该规则不会匹配。

## Docker

Docker 增强是可选的，且永不位于关键路径上：

- 在后台线程运行 `docker ps --format "{{json .}}"`，每个 `docker_ttl_s`
  （默认 15 秒）最多一次，超时为 `docker_timeout_s`（默认 2 秒），且不弹出控制台窗口。
- Docker 缺失 / 已停止 / 响应缓慢时会静默降级；主列表不受影响。
- 容器身份仅用于标注那些操作系统 socket 表已经归属到 Docker 代理进程
  （`com.docker.backend`、`wslrelay`）的条目。这是一种**尽力而为的推断**，措辞为
  “published by container X”，并不声称该进程拥有对应 socket。

## 测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall src
```

测试使用依赖注入与替身（fake）：它们从不打开真实浏览器，也只在随机端口上绑定
临时的回环 socket。强制关闭使用注入的 fake terminator 测试，因此
**任何自动化测试都不会终止真实进程**。

## 性能

在开发机（Windows 11，20 个逻辑核心，约 127 个端点 / 42 个 PID）上测得：

| 指标 | 数值 |
| --- | --- |
| 启动到 `--once`（含解释器） | ~0.78 秒 |
| 仅 socket 扫描 | ~6 毫秒 |
| 首次快照（冷进程缓存） | ~0.19 秒 |
| 稳态快照（热缓存） | ~2.6 毫秒构建（另加 ~6 毫秒 socket 扫描） |
| PID 重新验证轮（约每 45 秒） | ~11 毫秒 |
| 对每个 PID 做全量重解析 | ~200 毫秒（已刻意避免） |
| 1 秒刷新下的空闲 CPU | 约单核的 0.5% |
| 空闲 RSS | ~28 MB |
| Docker `ps` 刷新 | ~0.27 秒（后台、带缓存） |
| 每次稳态扫描的外部进程数 | 0 |

始终执行完整扫描，并用结果快照替换上一份；不存在增量扫描。

## 已知限制

- 目前仅支持 Windows；浏览器打开器与剪贴板均为 Windows 专用。
- 非管理员用户无法读取其他用户的命令行，因此命令行规则可能无法匹配
  系统 / 其他用户的进程。
- Docker 已发布端口映射是对 `docker ps` 的推断，而非来自操作系统 socket 表。
- 任意 TCP **监听**端口都能在浏览器中打开，即使它不是 HTTP；LocalhostMaster
  从不探测协议，因此打开非 HTTP 端口可能看到错误页。
- 已连接的 TCP 行与 UDP 端点无法打开。
- PID 身份约每 45 秒重新验证一次（并在 PID 的端点集合变化时立即验证）；
  若某进程在两次验证之间被杀掉、且其 PID 恰好被回收，仍可能短暂显示陈旧元数据。
- 强制关闭无法做到原子：会在终止前立即重新校验端点与身份，这能缩小但无法完全
  消除“校验”与 `TerminateProcess` 之间的窗口。
- Docker 宿主代理被刻意禁止终止，因此无法从 LocalhostMaster 关闭容器的发布端口
  （请使用 `docker stop`）。
- 默认不产出独立的 `.exe`（未安装、也不要求打包工具）。

## 项目结构

```
src/localhostmaster/
  cli.py                 参数解析与非交互式输出
  scanner.py             psutil 扫描 + 地址归一化
  process_resolver.py    PID -> 进程元数据（性能感知缓存）
  classifier.py          内置 + 用户规则匹配
  config.py              config.toml 加载/校验
  category_store.py      categories.toml（原子写入，精简 TOML 序列化器）
  docker_resolver.py     可选的、后台 Docker 增强
  url_builder.py         纯 URL 构造
  browser.py             系统默认浏览器打开器（可注入）
  clipboard.py           Win32 Unicode 剪贴板
  refresh.py             快照构建 + 后台刷新工作线程
  state.py               双击 Enter 状态机、排序/过滤/选择
  kill_state.py          双击 k 强制关闭状态机
  process_terminator.py  安全的 psutil 终止边界（策略 + 二次校验）
  icons.py               中央预设图标注册表
  tui/                   prompt_toolkit 应用、控件、样式
tests/                   unittest 测试套件（使用替身，不触碰真实浏览器/终端/进程）
```

## 许可证

MIT
