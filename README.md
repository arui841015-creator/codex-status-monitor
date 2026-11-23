# Codex Status Monitor

独立的本地状态监听与 MQTT 上报应用（Windows / macOS）。它不启动 Codex × 小智 MCP 桥接，也不修改任务数据，只读取：

- Codex Desktop 指定 thread 的只读回合状态；
- Workbuddy 的只读 `workbuddy.db` 会话状态；
- 将统一状态通过设备专属 MQTT 主题发布。

## 使用

**Windows**：解压 `CodexStatusMonitor-v2.2.0-win64.zip`，进入 `CodexStatusMonitor\`，双击 `CodexStatusMonitor.exe`（无需安装 Python）。

**macOS**：解压 `CodexStatusMonitor-v2.2.0-macos-<arch>.zip`，把 `CodexStatusMonitor.app` 拖进「应用程序」。首次启动必须**右键 → 打开**（未做 Apple 公证），详见包内 `使用说明.txt`。

然后：

1. 填写 **Device ID**（唯一必填项）。
2. 勾选要监听的程序：**Codex Desktop**、**Workbuddy**，可多选。
3. 点击「启动监听」。
4. 可选：勾选「④ 启动方式」中的**随系统自动启动**，此后开机无需再打开界面。

程序是无控制台的窗口应用，启动时**不会**弹出黑色 CMD 窗口；它拉起的 `codex app-server` 子进程也用隐藏窗口方式创建。

没有「添加通道 / 检索 / 锁定」这类步骤。MQTT Broker 已预填默认值（`117.72.43.16:1883`），需要时在「③ MQTT Broker」里改。

### 随系统自动启动与系统托盘

| | Windows | macOS |
|---|---|---|
| 启动项位置 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`（值名 `CodexStatusMonitor`） | `~/Library/LaunchAgents/com.sdgods.codexstatusmonitor.plist` |
| 生效时机 | 下次登录 | 下次登录（写入但不 `launchctl bootstrap`，否则会立刻多起一份） |
| 托盘位置 | 通知区域 | 菜单栏 |

- 两者都是**当前用户级**，不需要管理员权限，不改系统设置；取消勾选即彻底删除。
- 启动项携带 `--minimized`：登录后程序**不显示窗口**，直接开始监听并驻留托盘。
- 图标颜色即状态：绿=监听中，橙=MQTT 未连接，红=已停止。右键菜单可**显示主界面 / 启动或暂停监听 / 退出**。
- 点击窗口关闭按钮不再退出程序，而是隐藏到托盘并继续监听；退出请用托盘菜单的「退出」。
- 自启动时若配置不完整（缺 Device ID 或未勾选渠道），程序会改为**显示主界面**并写日志，避免“静默失败”。
- 单实例锁：同一台机器只允许一个实例运行，防止自启动与手动启动并存时向同一设备主题重复上报（Windows 用命名互斥体，macOS 用文件锁）。
- 移动/重命名程序目录后启动项会失效，程序下次运行时会自动修正指向；也可在界面里取消再勾选一次。

### 运行日志

后台静默运行时没有窗口可看，日志会同时写入：

```
Windows : %LOCALAPPDATA%\CodexStatusMonitor\monitor.log
macOS   : ~/Library/Logs/CodexStatusMonitor/monitor.log
```

界面「上报与日志」页也会显示当前日志路径。文件超过 512 KB 时自动清空重写。

### 自动监听规则

- 每个勾选的渠道自动取**最近 N 个任务**（默认 1，即可调 1-10）。默认只跟最新一条，与设备单行显示的需求对齐。
- 出现新任务时自动接管，并把时间最远的任务顶掉，无需人工登记。
- 任务进入窗口或状态发生变化时会上报一次；状态不变不会重复上报。
- 点「预览将跟踪的任务」可只读查看当前窗口内容，不影响监听。
- Codex 侧读取 `thread/list` 的真实顺序（`recencyAt`/`updatedAt`），不再依赖手工 `thread_id`；无回合的空线程自动跳过。
- 已完成/已失败的旧任务同样会在启动时上报一次，不会因为没有“活跃状态”而静默。

默认主题：`sdgoods/codex/v1/devices/{device_id}/status`。身份不完整或使用 `default`、`REPLACE_ME` 时不会启动发布。

## 配置与安全

配置保存位置（schema 版本 2）：

```
Windows : <exe 同级>\monitor_config.json
macOS   : ~/Library/Application Support/CodexStatusMonitor/monitor_config.json
```

macOS 下之所以不写在 `.app` 内部：那会破坏代码签名，而且下次替换 app 时配置会跟着丢失。旧版基于 `channels` 的配置会在首次读取时自动迁移为 `watch` 结构，无需手工改写。MQTT 密码只写入本机配置文件，不会出现在状态 payload；生产环境建议使用只允许发布到设备主题的 Broker 账号。

状态 payload 的 `message` 为设备友好的 ASCII 单行：`CODEX | project | WORKING` 或 `WORKBUDDY | project | COMPLETED`。原始项目/任务名称保留在 `detail.project_raw` 与 `detail.task_name_raw`。

## 构建与源码模式（隔离环境）

源码模式：安装 Python 3.11+（须带 tkinter），运行 `install.bat` 装依赖，再运行 `start-monitor.bat`。

编译与运行都使用项目内 `.venv`，不写入系统 Python 的 `site-packages`，因此不会影响本机其他项目的编译：

- `build-exe.bat` / `install.bat` 检测到 `.venv` 缺失时会用 `py -3.11`（回退 `python`）自动创建，并把依赖装进 `.venv`。
- 创建后会先校验 `import tkinter`；缺少 Tcl/Tk 的解释器会直接报错退出，避免打出起不来的 exe。
- 打包前清空 `PYTHONPATH`，隔离宿主环境注入的模块搜索路径。
- 需要 Python 3.11+ 且带 tkinter。若系统 `python` 是无 tkinter 的精简版，请确保 `py -3.11` 指向完整 CPython。
- 配置模板在 PyInstaller 6 的 onedir 布局中会被放进 `_internal`，`build-exe.bat` 构建后额外复制一份到 exe 同级；程序本身也会回退到内置默认配置，模板缺失不会导致启动崩溃。
- 产物：`build\dist\CodexStatusMonitor\CodexStatusMonitor.exe`；再运行 `python build_release.py` 生成两个包：
  - `CodexStatusMonitor-v2.2.0-win64.zip` —— **发给 Windows 用户的绿色包**：解压即用，内含 `README.txt`，无需 Python。
  - `codex-status-monitor-release.zip` —— 源码 + 二进制的开发包。

### macOS 构建

PyInstaller **不能跨平台编译**：`.app` 只能在 macOS 上生成，Windows 上跑不出可用的 mac 二进制。

```bash
./build-mac.sh
```

脚本会先跑单元测试、校验 tkinter，再打出 `build/dist-mac/CodexStatusMonitor.app`，做 ad-hoc 签名（`codesign -s -`），最后用 `ditto` 压成 `CodexStatusMonitor-v2.2.0-macos-<arch>.zip`（内含 `使用说明.txt`）。

- 需要 Python 3.11+ 且带 tkinter；用 python.org 安装包，或 `brew install python-tk`。
- `pystray` 在 macOS 上依赖 `pyobjc-framework-Cocoa`，已写入 `requirements.txt` 的平台条件。
- 托盘在 macOS 走 `run_detached()`：Cocoa 的菜单栏项必须建在主线程，而 Tk 正占着主线程跑自己的事件循环，所以不能用后台线程。
- **没有 Apple 公证**（需付费开发者账号），用户首次打开要右键 → 打开，或 `xattr -dr com.apple.quarantine`。
- 不装 Mac 也能出包：`.github/workflows/build-macos.yml` 在 GitHub 的 macOS 运行器上跑同一个脚本，arm64 与 x86_64 各自出一个 zip（Intel 那台是兜底，标签失效不会拖红整次构建）。

## 目录

| 文件 | 用途 |
|---|---|
| `monitor_app.py` | Tk 图形前端（Device ID + 勾选渠道 + 启动方式） |
| `monitor_engine.py` | 自动滚动窗口监听、状态映射和 MQTT 发布 |
| `codex_reader.py` | Codex app-server 只读 JSON-RPC 客户端 |
| `autostart.py` | 写/删当前用户启动项，并提供 `--minimized` 命令 |
| `tray_icon.py` | 系统托盘图标（pystray，三色状态 + 右键菜单） |
| `monitor_config.example.json` | 配置模板（schema v2，`watch` 结构） |
| `test_monitor_engine.py` | 引擎单元测试（`python -m unittest test_monitor_engine`） |
| `test_app_helpers.py` | 启动参数与自启动逻辑测试 |
| `build-exe.bat` | 在项目内 `.venv` 中编译 Windows EXE |
| `build-mac.sh` | 在 macOS 上编译 `.app` 并打包 zip |
| `README-macOS.txt` | macOS 用户说明（打进 mac 发行包） |
| `.github/workflows/build-macos.yml` | 在 GitHub 的 macOS 运行器上自动出 mac 包 |
| `build_release.py` | 生成 Windows 用户绿色包与源码包 |
| `.venv\` | 项目专属虚拟环境（不入发行包） |
| `app\CodexStatusMonitor.exe` | Windows GUI 可执行版本 |

