# 日常操作、备份与恢复

以下命令在 collector-music 项目根目录执行。默认数据位于本项目；全局参数 `--home 路径` 可指定隔离资料目录，必须放在子命令前。不要在测试中省略它。

## 环境

先准备 Python 3.11+ 和 Node 22+，双击 `安装环境.cmd`，或执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm.cmd ci --ignore-scripts --no-audit --no-fund
.\.venv\Scripts\python.exe -m collector_music init
```

Python 包也通过了 `pip install --no-deps -e .` 的可编辑安装检查；日常从仓库根运行不要求先安装项目包。Node 只在网易云登录/采集时使用，Pano 导入、Parquet 和查询不依赖联网。

## 导入设备日志

从设备取得完整 JSONL 文件后，在控制台选“导入手机日志”或“导入平板日志”（设备标签分别为 `phone`、`tablet`，也可用命令行自定义）。默认实例名 `pano-main-001`；文件的新副本仍使用同名实例。

```powershell
.\.venv\Scripts\python.exe -m collector_music import-pano "inbox/phone/pano.jsonl" --device phone --stream pano-main-001 --collector-version 4.46
.\.venv\Scripts\python.exe -m collector_music import-pano "inbox/tablet/pano.jsonl" --device tablet --stream pano-main-001 --collector-version 4.46
```

默认不推断来源历史时区。确认日志发生期间的来源时区后，才使用 `--source-timezone Asia/Shanghai`。记录阈值用 `--rule-note "实际设置"` 归档；未知保留 unknown。

完成一次设备绑定和 `.local/phone.json` 配置后，连接 USB 并允许调试，可双击 `03-接收手机日志.cmd`、控制台选择 10，或执行：

```powershell
.\.venv\Scripts\python.exe -m collector_music receive-phone
```

该入口只寻找原绑定设备，读取后发布快照；其他手机即使同时连接也不会被代选。新环境先用 `usb-pull --serial ...` 显式建立绑定，再参考 `phone.example.json` 建立 `.local/phone.json`。`usb-pull` 也支持 `--collector-version` 与 `--rule-note`，用来记录已核实的采集器和阈值。设置变更后要更新配置，资料迁移时一并备份 `.local/phone.json` 和 `.local/devices/`。

首版读取每份累计日志，校验已确认前缀后只导入新增完整行；单文件上限 256 MiB。它不是仅读取新字节的高容量流式引擎。文件未写完的尾行保留原字节并报告 pending_tail；新增完整行中出现错误时整批事务回滚，原始归档仍保留。

如果报告前缀变化或截断，不要靠更换 stream 强行重导同一份旧日志。先确认设备是否创建了真正的新日志实例；新实例可能与旧实例有重复范围，首版不会跨实例猜测去重。两台设备播放相同歌曲仍分别保留，未自动推断为同一次收听。

## 本地扫描

按 `config.example.json` 在 `.local/config.json` 填写确实存在的日志路径。只有一台设备时只配置一项，不能用空文件冒充另一设备覆盖。

```powershell
.\.venv\Scripts\python.exe -m collector_music scan
```

scan 只导入本机已配置文件，再发布快照；不从设备拉取、不联网。某个文件不存在或校验失败时，停止本轮发布，旧发布快照继续可用；此前已完成的独立导入事务仍保留，重跑会幂等处理。设备接收和本地扫描不在本项目定时任务中，留待跨项目统一上传开关协调。

## 网易云

```powershell
.\.venv\Scripts\python.exe -m collector_music login
.\.venv\Scripts\python.exe -m collector_music collect
.\.venv\Scripts\python.exe -m collector_music publish
```

登录生成本机二维码并默认打开本地说明页；凭据仅保存在 `.local/session.json`。不要复制凭据到聊天或源码。采集前再次核实会话账号；失败不改变已有事实。

默认采集最近播放、周榜、总榜、红心 ID 与歌曲详情、用户歌单清单。红心详情按每批 200 个 ID 获取，匹配歌曲 ID，不依赖返回顺序；完整关系清单与各批响应一起归档。**尚未采集每个歌单的成员列表或黑胶时光机。**当前 user_playlist 清单不能说成完整歌单内容备份。

仅补采红心及其详情可以运行 `collect --scopes likes`，然后 `publish`。详情缺失或中断时完整红心 ID 仍保留，未取得名称的歌曲不会被当作取消收藏；命令结果 `likes_detail_status=partial`、整体 `status=partial`，原始响应记录缺失 ID 和已完成批次。看板显示详情覆盖数量，不把已知名称数量当作红心总数。后续重新采集会重新读取关系和详情；本轮未引入永久详情缓存。

```powershell
.\.venv\Scripts\python.exe -m collector_music collect --scopes recent,likes --delay 3
```

请求串行、间隔至少 3 秒；遇到请求失败停止后续范围，保存范围状态。用户歌单按 more 翻页，重复 ID、无明确终点或空页且 more=true 都会记录失败，不假报完整。分页原始响应保留；其他核心接口每轮一个窗口。网易云近期记录的 playTime 语义仍标为待实测，不承诺完整重复播放历史。

桥接器通过私有 stdin/stdout 调用固定白名单方法，不运行上游 HTTP 服务。没有修改红心、歌单、播放数、账号设置等方法。`collect` 返回 partial 时退出码为 2，表示已保存部分可用范围与失败证据；一般错误为 1，正常为 0。

## 网易云平台报告

```powershell
.\.venv\Scripts\python.exe -m collector_music collect-reports
.\.venv\Scripts\python.exe -m collector_music publish
```

使用现有本地登录态，串行读取完整月报、本月实时收听、曲风偏好和累计时长；与普通 collect 共用互斥锁，请求间隔至少 3 秒。四类结果独立保存，单接口失败不丢弃其他已成功报告；登录失效则停止后续请求。最新原始文件位于 `.local/last-reports.json`，正式原件另外进入 raw，规范化结果进入独立的 platform_reports 表及 Parquet 发布。

看板的“网易云报告”按平台实际月份切换，刷新后保留选中的月份/类型。完整月报与实时快照分开展示，曲风偏好保留自己的采集时间。请求失败显示此前成功数据并标明旧快照；成功空结果不沿用旧值。平台时长原值已保存，单位和统计范围核实前不换算成分钟或小时。已有逐次播放记录及其实际时长字段不改变。

已保存的标准报告文件可以用 `import-reports 文件路径` 重导；同一文件重复导入为 duplicate。`audit --replay-check` 会校验并从原件重建报告表。歌曲标签接入后，当前发布契约为 1.2.0，跨项目使用 `integrations/music_snapshot.py` 时同步更新这份读取器；旧 1.0.0、1.1.0 发布仍可读取。

歌曲百科覆盖检查是独立的按需工具，结果仅存私有验证目录，不触发全库补采或模型打标：

```powershell
.\.venv\Scripts\python.exe scripts/check_tag_coverage.py --limit 30
```

默认从近期播放与最近成功红心清单取不重复样本，报告每个字段的成功覆盖数、缺失数、接口失败和未请求数。定向小样本不能当作全库覆盖率；完整参数用 `--help` 查看。

## 全库歌曲标签补采

```powershell
# 只读预览去重后的歌曲范围、缓存数和待采数
.\.venv\Scripts\python.exe -m collector_music collect-tags --plan
# 补采本库所有尚无成功结果的网易云歌曲；中断后重复执行即可续采
.\.venv\Scripts\python.exe -m collector_music collect-tags
# 可用 --limit 50 控制单批数量；完成后发布供其他项目读取
.\.venv\Scripts\python.exe -m collector_music publish
```

范围是本地已归档的网易云近期记录、历次红心歌曲和榜单中的歌曲 ID 并集；歌单清单中的歌单 ID 不是歌曲 ID，也不会为此自动遍历歌单全部成员。Pano 没有网易云歌曲 ID 的记录保持未匹配。不同来源或历史快照出现同一个歌曲 ID，只请求一次，共用平台标签；同名但不同 ID 的现场版、翻唱、重制版各自保留，不根据名字或标签相同推断为同一录音。

仅使用网易云歌曲百科 BASIC 区的曲风、语种、BPM、推荐标签。字段内重复文本去重，BPM 同数值去重；复合标签保留平台原文，缺失为 `[]`，不填默认曲风、不把 BPM 填零，也不使用模型补齐。成功响应即使所有字段为空也计入缓存，重复运行不会为这些空字段反复请求。请求或解析失败单列，可在续采时重试；此前成功标签不会被失败擦除。

每首响应先持久化到私有暂存，再写入正式 raw 和 `song_tags` 历史表。进度位于 `.local/tag-collection/progress.json`；中断后先恢复暂存，再从全局 `netease + platform_track_id` 缓存计算待采列表。`tag-collect` 锁防止两个补采进程重复请求，平台请求串行且间隔至少 3 秒，与普通采集共用请求互斥；锁忙会延后，登录失效、风控或异常响应停止当前批次并保留已完成结果。全库补采仍是按需命令，自动调度只补尚无成功结果的歌曲，不周期性重抓全库。

新歌在近期记录、红心或榜单采集入库后进入增量队列，尚未被这些来源采到的歌曲不会提前出现。现有 Windows 任务每 15 分钟检查到期工作，`tags` 默认间隔为 900 秒，每批最多 20 首，优先新 ID、再重试失败项；队列为空时只检查本地缓存，不请求网易云。已成功返回的空字段保持空白，不反复查询或用模型补齐。采集后随发布更新看板，页面点击“刷新展示”即可读入；该按钮本身不采集。Pano 无网易云 ID 的歌曲仍未匹配。

`import-tags 文件路径` 可导入标准歌曲标签归档。同一原件重复导入不新增历史记录；未来新的采集证据可以新增快照，当前视图始终每个来源与歌曲 ID 一行。下游使用 `MusicSnapshot.attach()` 的 `music.current_song_tags` 关联歌曲 ID，可直接读取 `genre`、`language`、`bpm`、`recommended_tags` 数组；历史、失败和采集时间见数据契约。

## 自动化：定时平台采集

`scripts/install_scheduled_task.ps1` 注册 Windows 任务 `CollectorMusic-AutomaticArchive`。默认近期采集间隔 3 小时（可在 `.local/automation.json` 调整）。**USB 自动接收不启用**，调度器不调用设备列表、ADB、receive-phone 或 scan；设备日志保持手动接收。

任务以当前用户普通权限运行 Python 的无窗口入口，无须语言模型、浏览器或公网服务。Windows 每 15 分钟检查到期工作，用户登录时也检查；它不是每 15 分钟请求网易云。每个工作的截止时间独立保存在 `.local/automation/state.json`，到期后才执行。

| 通道 | 默认频率 | 执行与边界 |
| --- | --- | --- |
| 网易云近期观测 | 每 3 小时到期 | collect --scopes recent，随后发布；该接口没有时间游标，不能保证完整流水 |
| 红心、歌单清单、榜单 | 每天一次 | collect --scopes likes,playlists,week,all，随后发布；当前红心详情每轮全取，后续再做带有效期的元数据缓存 |
| 平台报告 | 每天一次 | collect-reports，随后发布；独立退避，不把平台汇总写成播放流水 |
| 新歌曲标签 | 每 15 分钟到期 | 本地检查待采 ID，每批最多 20 首；成功/空结果复用缓存，未采完的下轮继续 |
| USB / 无线设备日志 | 不启用 | 保留手动入口 |
| 一致性备份 | 每 24 小时检查 | 有新资料库修订才备份到 data/backups/automatic；没有变化不反复复制 |
| 恢复校验 | 每 7 天 | audit --replay-check；保留检查报告，临时恢复目录自动清理 |

任务只在当前用户已登录、电脑醒着时运行；锁屏仍可运行，注销、休眠或关机时不执行，也不唤醒电脑。错过的到期工作恢复后只补做一次，不堆积逐小时补采。正常检查可能使实际运行比到期时间晚约 15 分钟；每次完成后再计算下次到期。

运行规则：Windows 的 IgnoreNew 和本地 automation 锁防止重叠；平台采集复用 collect 锁，与手动采集互斥。一般失败按 30 分钟、1 小时、2 小时等逐步退避，最长 12 小时；部分成功先保留和发布可用事实，再重试。登录失效暂停近期、收藏榜单、报告和歌曲标签四个平台通道，重新扫码更新 `.local/session.json` 后恢复；不自动弹扫码窗口，不把凭据写入任务参数或日志。平台失败不妨碍已有本地数据发布、备份和校验。

SQLite 修订、资料库身份或发布契约变化时才新建 Parquet 发布，空检查不会制造重复快照；每次真实平台采集仍保留其原始响应、证据和观察时间。原始历史、有效发布和备份不自动删除。备份仍在同一台电脑，完整私人备份还应包括 raw 和私有配置；这些任务不负责异机备份。

`.local/automation.json` 由 [配置示例](../automation.example.json) 复制而来，可固定 Node 的路径，避免计划任务 PATH 不同导致找不到适配器。修改间隔对之后安排的截止时间生效；已排定的下一次时间保留。状态和日志只在本机：

- `.local/automation/state.json`：每个通道的结果、下次到期、连续失败次数、登录暂停状态。
- `.local/automation/last-run.json`：最近检查摘要；0 为成功/无到期工作，2 为需要关注，1 为执行器错误，对应计划任务退出码。
- `.local/automation/events.jsonl`：实际采集、状态变化和恢复记录；空检查不追加日志，没有弹窗或外部通知。
- `.local/automation/last-audit.json`：最近恢复演练报告。

重新扫码继续使用原 `login` 入口。检查状态、临时运行到期工作或停用任务：

```powershell
Get-ScheduledTaskInfo -TaskName 'CollectorMusic-AutomaticArchive'
Get-Content .local/automation/last-run.json
.\.venv\Scripts\python.exe -m collector_music automation-run
# 仅停用未来触发；正在运行的一轮会正常结束。
Disable-ScheduledTask -TaskName 'CollectorMusic-AutomaticArchive'
# 恢复未来触发。
Enable-ScheduledTask -TaskName 'CollectorMusic-AutomaticArchive'
# 换机器/路径后注册，或删除本项目匹配的任务（数据保留）。
.\scripts\install_scheduled_task.ps1
.\scripts\install_scheduled_task.ps1 -Remove
```

首次注册不会自动要求管理员权限或保存用户密码。安装脚本只替换名称、描述和入口均匹配本项目的任务，不影响其他项目。全局关闭也可把 `.local/automation.json` 的 enabled 改为 false；该方式保留系统触发器但不执行工作。Windows 定时/登录触发能力见 [官方计划任务说明](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger)。

手机仍可离线记日志；调度器不会自动接回这些日志，也不能保证补回平台窗口已消失的记录。手机和平板应保持独立 device/stream；不要双向同步同一份 JSONL，也不要通过更换 stream 强行绕过前缀变化检查。

## 开始分析前

```powershell
.\.venv\Scripts\python.exe -m collector_music audit --replay-check
```

该命令不联网、不改动事实库。已发布表与 SQLite 逐行比对，设备日志水位/前缀校验，并可从原件恢复到临时目录比较来源事实。退出码 0 表示检查通过，2 表示发布落后或来源状态等警告，1 表示校验失败。输出中的 limits 仍然适用；审计成功不能当作连续后台覆盖或真实播放时长已验证。

分析侧优先使用 [可复制读取器](../integrations/music_snapshot.py) 的 current_likes/current_playlists/current_rankings，避免累计多批快照；听歌使用独立的 pano_listening_events 或 netease_recent_observations。完整说明见 [数据契约](DATA_CONTRACT.md)。

历史分析使用 library_snapshots、library_membership_history、library_changes、ranking_history；通过 generation 固定输入，通过带时区的 as_of 筛选采集截止时间。首次关系快照只是历史基线；变化区间不是精确收藏日期。旧的发布版本、原始日志和响应均保留；基础表/日级表不受 as_of 自动过滤。尚无网页历史时点切换器，以上能力供本地分析脚本使用。

## 发布与查询

```powershell
.\.venv\Scripts\python.exe -m collector_music publish
.\.venv\Scripts\python.exe -m collector_music verify-lake
.\.venv\Scripts\python.exe -m collector_music query "SELECT date,source,device_key,record_count FROM daily_music_features ORDER BY date DESC"
```

SQLite 单一读取事务固定来源修订；写入新版本、核验全部文件与字段后才原子替换 `data/lake/current.json`。失败时临时版本可留在 versions 下，但不会切换 current。按 current 的 manifest 读取，不能递归混读全部版本。

query 接受一条 SELECT/WITH，先校验并加载快照到内存，再关闭外部文件访问；默认最多返回 1000 行，可用 `--limit` 指定 1–10000，超出返回 truncated=true。这是音乐单项目的受限查询入口；跨项目消费者可使用数据契约里的独立读取示例。

```powershell
# 固定旧版本
.\.venv\Scripts\python.exe -m collector_music query "SELECT count(*) AS n FROM observations" --generation 完整版本号
# 仅复制 lake 到其他地方也可查询，不需要活动 SQLite 或账号
.\.venv\Scripts\python.exe -m collector_music query "SELECT * FROM daily_music_features" --lake "path\to\music-lake"
```

## 备份与恢复

成功发布后自动回收旧 `data/lake/versions`，只保留当前发布和最近一小时的读取缓冲。长期保存的分析需自行保存所用输入；已过期的 generation 不能静默替换成新版本继续分页。当前发布失败时不清理，raw 和业务 SQLite 不参与回收。

自动任务的 `data/backups/automatic` 只保留最近一份已通过完整性检查的数据库备份；新备份失败时保留旧备份。用户显式指定路径的手工 backup 不受这条规则影响。发布和自动备份的清理沿用各自的进程锁，文件占用或目录联接会跳过并提示，后续成功任务再尝试。

```powershell
.\.venv\Scripts\python.exe -m collector_music verify
.\.venv\Scripts\python.exe -m collector_music backup "data/backups/music-YYYY-MM-DD.sqlite"
.\.venv\Scripts\python.exe -m collector_music replay "path\to\music-rebuilt"
```

backup 使用 SQLite 一致性备份 API 并检查完整性；目标必须不存在，不覆盖旧备份。完整私人备份还需要 `raw/`、`.local/config.json` 和设备绑定配置；登录态按需要独立安全保存或重新登录。账号 QR、临时会话文件不进入分析导出。

replay 验证原始归档，在新目录复制原件并重建；有非法原始批次时逐个记录 rejected，退出码为 2，不能把它当成全部恢复成功。重建后的入库时间、数据库身份和 changes 序号不同；消费方应当作新资料库。首版没有人工标注写入功能，未来人工修订不能仅靠来源原件恢复。

设备累计日志按前缀长度重放；平台快照按原始归档到达时间重放，保留重复观测首次收到的正文与主来源，避免补入较早历史快照后恢复时发生替换。原始重放不等于数据库逐字节备份，精确保留原入库状态应使用 SQLite 一致性备份；恢复演练会核对来源事实并报告差异。

`verify` 校验 SQLite、外键、归档哈希和清单身份，但不验证上游数据本身真实或设备覆盖完整。原始归档按约定不可变，不是防篡改存储。
