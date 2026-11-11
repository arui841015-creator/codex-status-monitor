# Codex Status Monitor

独立的 Windows 本地状态监听与 MQTT 上报应用。它不启动 Codex × 小智 MCP 桥接，也不修改任务数据，只读取：

- Codex Desktop 指定 thread 的只读回合状态；
- Workbuddy 的只读 `workbuddy.db` 会话状态；
- 将统一状态通过设备专属 MQTT 主题发布。

## 使用

1. 解压发行包后，直接双击 `app\\CodexStatusMonitor.exe`（无需另外安装 Python）。
2. 填写 **Device ID**（唯一必填项）。
3. 勾选要监听的程序：**Codex Desktop**、**Workbuddy**，可多选。
4. 点击「启动监听」。

没有「添加通道 / 检索 / 锁定」这类步骤。MQTT Broker 已预填默认值（`117.72.43.16:1883`），需要时在「③ MQTT Broker」里改。

### 自动监听规则

- 每个勾选的渠道自动取**最近 N 个任务**（默认 3，可调 1-10）。
- 出现新任务时自动接管，并把时间最远的任务顶掉，无需人工登记。
- 任务进入窗口或状态发生变化时会上报一次；状态不变不会重复上报。
- 点「预览将跟踪的任务」可只读查看当前窗口内容，不影响监听。
- Codex 侧读取 `thread/list` 的真实顺序（`recencyAt`/`updatedAt`），不再依赖手工 `thread_id`；无回合的空线程自动跳过。
- 已完成/已失败的旧任务同样会在启动时上报一次，不会因为没有“活跃状态”而静默。

默认主题：`sdgoods/codex/v1/devices/{device_id}/status`。身份不完整或使用 `default`、`REPLACE_ME` 时不会启动发布。

## 配置与安全

配置保存为本目录的 `monitor_config.json`（schema 版本 2）。旧版基于 `channels` 的配置会在首次读取时自动迁移为 `watch` 结构，无需手工改写。MQTT 密码只写入本机配置文件，不会出现在状态 payload；生产环境建议使用只允许发布到设备主题的 Broker 账号。

状态 payload 的 `message` 为设备友好的 ASCII 单行：`CODEX | project | WORKING` 或 `WORKBUDDY | project | COMPLETED`。原始项目/任务名称保留在 `detail.project_raw` 与 `detail.task_name_raw`。

## 构建与源码模式（隔离环境）

源码模式：安装 Python 3.11+（须带 tkinter），运行 `install.bat` 装依赖，再运行 `start-monitor.bat`。

编译与运行都使用项目内 `.venv`，不写入系统 Python 的 `site-packages`，因此不会影响本机其他项目的编译：

- `build-exe.bat` / `install.bat` 检测到 `.venv` 缺失时会用 `py -3.11`（回退 `python`）自动创建，并把依赖装进 `.venv`。
- 创建后会先校验 `import tkinter`；缺少 Tcl/Tk 的解释器会直接报错退出，避免打出起不来的 exe。
- 打包前清空 `PYTHONPATH`，隔离宿主环境注入的模块搜索路径。
- 需要 Python 3.11+ 且带 tkinter。若系统 `python` 是无 tkinter 的精简版，请确保 `py -3.11` 指向完整 CPython。
- 配置模板在 PyInstaller 6 的 onedir 布局中会被放进 `_internal`，`build-exe.bat` 构建后额外复制一份到 exe 同级；程序本身也会回退到内置默认配置，模板缺失不会导致启动崩溃。
- 产物：`build\\dist\\CodexStatusMonitor\\CodexStatusMonitor.exe`；再运行 `python build_release.py` 生成 `codex-status-monitor-release.zip`。

## 目录

| 文件 | 用途 |
|---|---|
| `monitor_app.py` | Tk 图形前端（只填 Device ID + 勾选渠道） |
| `monitor_engine.py` | 自动滚动窗口监听、状态映射和 MQTT 发布 |
| `codex_reader.py` | Codex app-server 只读 JSON-RPC 客户端 |
| `monitor_config.example.json` | 配置模板（schema v2，`watch` 结构） |
| `test_monitor_engine.py` | 引擎单元测试（`python -m unittest test_monitor_engine`） |
| `build-exe.bat` | 在项目内 `.venv` 中编译 EXE |
| `build_release.py` | 生成独立 zip 包（自动从 `build\dist` 取二进制） |
| `.venv\\` | 项目专属虚拟环境（不入发行包） |
| `app\\CodexStatusMonitor.exe` | Windows GUI 可执行版本 |

