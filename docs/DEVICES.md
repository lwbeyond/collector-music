# 设备接入（Android 手机 / 平板）

持续听歌记录由听歌设备上的 [Pano Scrobbler](https://github.com/kawaiiDango/pano-scrobbler)（4.46+）写入本地 JSONL 文件，电脑端只读接收、归档和导入。每台设备使用独立的 `device_key`（示例中为 `phone`、`tablet`，可自定义）。

## 安装与校验

1. 从 Google Play 或作者的 [GitHub Releases](https://github.com/kawaiiDango/pano-scrobbler/releases) 安装。GitHub 版建议核对发布页给出的 SHA-256，并用 `apksigner verify --print-certs` 对照项目 README 公布的非 Play 签名指纹。
2. Play 版与 GitHub 版签名不同，不能直接覆盖安装；换版前先导出 Pano 设置并备份 JSONL。

## 设备设置

1. 只选 **本地文件 / JSONL** 作为记录目标即可，不要求注册 Last.fm。
2. 用系统文件选择器创建 `Download/music/pano.jsonl`；每台设备各自写本机文件，电脑端用不同 device_key 区分。
3. 按 Pano 提示在系统设置开启**通知使用权**，使其能读取播放器的媒体会话；这不是普通“允许显示通知”。本项目不会通过 ADB 自动授予该权限。
4. 在 Pano 的应用选择里只启用实际听歌应用（如网易云音乐）。按需要关闭联网元数据、社区账号、Discord 等功能，不能只根据本地文件模式推断全部流量都已禁用。
5. 允许 Pano 在后台运行，检查省电/后台清理是否会杀掉进程。部分厂商系统会在后台解除通知监听服务：权限开关显示“开”并不等于监听已连接。若文件不再增长，可在系统“通知使用权”中关闭再开启 Pano，然后正常播放验证。
6. 把实际阈值和规则版本记录到配置（`rule_note`、`collector_version`）；未达到 scrobble 阈值的短暂试听不会被记录，这不是故障。默认阈值类似 Last.fm：曲长超过 30 秒，播放一半或 4 分钟（取较早者）。

来源：[官方 FAQ](https://github.com/kawaiiDango/pano-scrobbler/blob/main/faq.md)。

## 把日志送到电脑

最直接的方法：通过 USB/MTP 或系统文件分享，把完整 JSONL 副本交给控制台导入（`import-pano`）。旧文件不删除，复制途中不要拿半份文件覆盖已有完整副本。

已开启 USB 调试时可直接使用只读接收命令（ADB 通过 `--adb`、环境变量 `MUSIC_ADB_EXE` 或 PATH 查找）：

```powershell
.\.venv\Scripts\python.exe -m collector_music devices

# 替换为 devices 列出的实际序列号。多台设备同时连接也必须明确选择。
.\.venv\Scripts\python.exe -m collector_music usb-pull --serial <SERIAL> --device phone --stream pano-main-001 --remote /sdcard/Download/music/pano.jsonl
.\.venv\Scripts\python.exe -m collector_music publish
```

接收器读取设备型号，把逻辑设备标签绑定到**序列号的 SHA-256 摘要**（`.local/devices/<device>.json`，不保存明文序列号）；以后同标签收到另一台设备会拒绝。只执行 `adb pull`，不删除或截断设备日志。接收副本和回执保存在 `.local/usb-receipts`，原始字节另归档到 `raw/`。

完成一次绑定后，按 `phone.example.json` 建立 `.local/phone.json`，之后可双击 `03-接收手机日志.cmd`、在控制台选择 10，或运行 `receive-phone`：它只寻找已绑定的那台设备，未连接、未授权或来源不一致时停止。

真正换机或创建新日志要建立明确的新设备/实例标签；不要删除绑定配置来绕过来源冲突。更改 Pano 阈值或应用升级后，同步更新 `.local/phone.json` 的 `rule_note` / `collector_version`。

## 首轮实机验证建议

每台设备分别做一份人工对照：普通播放两首、同曲循环两次、阈值前跳过、暂停/恢复、离线播放、锁屏后台、重启后的记录恢复。记下时间、歌曲和 Pano 实际设置，再比较 JSONL。

Pano 记录的开始时间来自采集器，歌曲元数据可能缺失或经过规则修正，`durationMs` 是曲目长度而不是实际播放时长。这些限制沿数据契约传递到分析端。电脑收到数据前，不应把这段时间记为零。
