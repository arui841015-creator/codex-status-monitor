Codex Status Monitor（macOS）使用说明
=====================================

一、安装
  1. 解压后把 CodexStatusMonitor.app 拖到「应用程序」文件夹（就地运行也可以）。
  2. 首次打开请用【右键 → 打开】，再点「打开」。

     本版本没有做 Apple 公证（需要付费开发者账号），直接双击会被
     Gatekeeper 拦下并提示「无法验证开发者」。

     如果仍然打不开，在「终端」里执行下面这行再试：
         xattr -dr com.apple.quarantine /Applications/CodexStatusMonitor.app

二、配置（三步）
  1. 填写 Device ID（唯一必填项）。
  2. 勾选要监听的程序：Codex Desktop / Workbuddy，可多选。
  3. 点「启动监听」。

  MQTT Broker 已预填默认地址，只有需要换服务器时才改。

三、随系统自动启动
  勾选后写入 ~/Library/LaunchAgents/com.sdgods.codexstatusmonitor.plist，
  下次登录生效。登录后只驻留菜单栏图标，不会弹窗。
  取消勾选即彻底删除该文件，不需要管理员权限。

四、文件位置
  配置：~/Library/Application Support/CodexStatusMonitor/monitor_config.json
  日志：~/Library/Logs/CodexStatusMonitor/monitor.log

  配置里可能含 MQTT 密码，请不要外发。

五、退出与卸载
  - 点窗口左上角关闭按钮只是隐藏到菜单栏，监听仍在继续。
    真正退出请点菜单栏图标 → 退出。
  - 卸载：退出程序 → 取消勾选自动启动 → 删除 .app。
    想彻底清理再删掉第四节的配置和日志目录。

六、常见问题
  - 提示「已在运行」：程序只允许一份实例。菜单栏图标右键可打开主界面。
  - 勾了 Codex 却说找不到可执行文件：macOS 上 codex 通常在
    /opt/homebrew/bin 或 ~/.local/bin。若不在默认 PATH 里，
    请在「codex 可执行文件」一项填绝对路径。
  - 上报不到设备：先确认 Device ID 与订阅端完全一致，
    再看「运行日志」页是否显示 MQTT 已连接。
  - 托盘/菜单栏图标没有出现：看 ~/Library/Logs/CodexStatusMonitor/monitor.log，
    里面会写明是不可用还是初始化失败；此时关闭窗口会直接退出程序。
