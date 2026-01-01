# 数据契约 1.2.0

代码版本 0.1.0；指标版本 1.0.0。首版公开出口为版本化 Parquet，无需常驻 HTTP 服务。

解析器版本 1.3.0 增加歌曲标签历史，契约 1.2.0 新增 `song_tags` 数据集；1.1.0 增加的平台报告继续保留。既有表字段与日级指标口径不变；SQLite 以追加表方式迁移至 user_version=3，保留 database_id。新版校验器及可复制读取器兼容原有 1.0.0、1.1.0 发布；使用旧版严格读取器的消费方需要更新读取器后读取新的 1.2.0 发布。旧版仅含 ID 的原始快照仍可导入和重放。

## 版本与一致性

消费者先读 `data/lake/current.json`，校验它指向的 manifest 哈希，固定 generation 后再读取该版本列出的文件。manifest 包含 database_id、data_revision、schema_version、metric_version、published_at_utc、analysis_timezone，以及每个数据集的路径、SHA-256、字段类型和行数。

database_id 识别资料库；data_revision 来自 changes.seq，标记一次 SQLite 快照内的已提交变更。generation 是发布版本，同一修订可用不同分析时区再次发布。重放到新资料库会获得新的 database_id 和变更序号，消费者须重置增量状态。

## 公开数据集

| 数据集 | 粒度 |
| --- | --- |
| imports | 一个通过规范化或明确部分失败的来源批次；非法格式只留原始归档，不算成功导入 |
| observations | 一个来源听歌/喜欢动作观测；保留全部来源字段 JSON 和原件指针 |
| observation_evidence | 观测与每个发现它的批次/JSON 指针的关联 |
| ranking_items | 一个采集批次、榜单范围、排名位置；计数不跨快照相加 |
| library_items | 一个采集批次下的红心歌曲 ID 或用户歌单条目；不代表歌单全部成员 |
| coverage | 每批来源范围的传输结果、观测跨度和限制 |
| changes | 本库单调递增变更序号；首版为观测新增和批次导入结果 |
| daily_music_features | person + source + device/account + 分析时区自然日的派生统计 |
| platform_reports | 一次采集中的一个平台报告类型；保存成功、空结果或失败尝试，独立于逐次播放事实 |
| song_tags | 一次采集对一个来源歌曲 ID 的标签读取尝试；保留成功、有明确空结果及失败历史 |

SQLite 内部 streams 是日志续读状态，source_accounts 是账号到 person 的绑定；两者不作为公开事实数据集发布。首版没有实体匹配编辑或人工修订接口。关系差异仅在下述分析视图中推导，不生成精确时间的喜欢/取消喜欢事实。

## 网易云平台报告

原始格式 `collector-music-netease-reports` v1，归档 kind 为 `netease_reports`。采集 `monthly`（完整月报）、`realtime`（当前月实时报告）、`preference`（曲风偏好）、`total`（累计时长原值），由原始响应直接规范化，不使用大语言模型。采集前验证账号；认证字段不进入原始归档。歌曲百科覆盖抽查保存在私有验证目录，正式歌曲标签按下一节独立归档。

`platform_reports` 包含 id、import_id、person_key、account_key、report_kind、observed_at_utc、status、period_start_utc、period_end_utc、normalized_json、payload_json。每次尝试留一行，同一原件重复导入不新增；下一次采集属于新的观察快照。`status=ok/empty/failed` 分别表示成功、有明确空结果、请求或解析失败。失败不会清除此前成功快照；成功空结果会取代当前展示，不能复活更早的值。

报告日期取自上游 `startTime/endTime`，不根据请求日或文件名猜月份。默认完整月报通常返回上一个月、实时报告返回本月；完整月报结束值为最后一天的零点，因此保留原始边界，在界面按日期展示，不声称是半开时间区间。曲风偏好和累计时长没有上游明确的统计区间，不能标成当前月份。

`normalized_json` 保存经校验的曲风/语种百分比、年代歌曲数、最常听歌曲/艺人、听歌天数及每日原值。分类占比可能只覆盖部分标签，不强制凑成 100%。平台推荐标签描述歌曲，不能解释成听者当时情绪。

时长字段保留为 `duration_raw`，`duration_unit=unknown`、`duration_minutes=null`。本次样本的峰值摘要写“134分”，同日明细原值为 139；数值单位和口径尚不足以可靠换算。原值不写入 observations.actual_listened_seconds，也不与 Pano/近期播放计数相加。平台 `reachLimit`、缺失字段和播客/有声书原值分别保留；缺失日期不自动补零。

平台报告提供 `platform_report_history`（各次尝试）、`latest_platform_report_attempt`（各账号/类型最近尝试）和 `current_platform_reports`（各账号/类型最近成功或空结果，并带 snapshot_stale）。这些视图遵守采集完成时间的 as_of；底层 platform_reports 仍是整个固定 generation。历史月份由 history 查询，不能把历次同月快照相加。

## 歌曲标签

原始格式 `collector-music-netease-song-tags` v1，归档 kind 为 `netease_song_tags`。文档包含 account_key、collected_at_utc、adapter_version 和 songs 列表；每项保存 song_id、observed_at_utc、请求状态以及去除认证字段后的响应或错误。整批采集完成时间写入 imports.observed_at_utc，原件重复导入为幂等操作。标签采集不增加 observations、红心关系或平台播放次数。

`song_tags` 字段为 id、import_id、person_key、account_key、source、platform_track_id、observed_at_utc、status、normalized_json、payload_json。当前 source 为 `netease`，唯一歌曲身份使用网易云歌曲 ID；相同名字的不同 ID 不合并。同一 ID 可被多次采集，保留全部尝试与来源账号供追溯；当前标签缓存跨账号按 `(source, platform_track_id)` 取值。

仅从音乐百科的 `SONG_PLAY_ABOUT_SONG_BASIC` 区块提取标签，不用相关歌单名称、推荐歌曲或评论反推分类。`normalized_json` 包含：

- `basic_block_present`：响应中是否存在音乐百科基础区块。
- `fields.genre`、`fields.language`、`fields.recommended_tags`：去除空文本及明确未知占位后的平台字符串列表。
- `fields.bpm`：有限且大于零的 BPM 数值列表；`fields.bpm_raw` 保留对应原始文本，不能把无法解析的文本猜成数值。
- `available`：曲风、语种、BPM、推荐标签各自是否有有效值。列表为空表示平台未提供可用标签，不表示歌曲客观上没有曲风或语种。

`status=ok` 表示至少一个有效标签字段存在；有效响应中没有任何有效标签为 `empty`；请求或格式解析失败为 `failed`。`current_song_tags` 同时把 ok 与 empty 视为最近成功结果，之后失败时保留该结果并设置 `snapshot_stale=true`。成功空结果会清除旧标签的当前展示；成功空结果之后再失败也不能恢复更早的非空标签。只有失败、从未取得成功结果的歌曲出现在 latest/history 视图，current 中没有可用行。

三个标签视图均经 `analysis_imports` 按批次采集完成时间应用 as_of；即使单曲请求开始较早，尚未完成的批次也不能提前进入该时点。相同时间按 import_id、id 确定性排序，排序方式与核心缓存一致。`current_song_tags` 另外提供可直接查询的 genre、language、bpm、bpm_raw、recommended_tags 数组，以及 latest_attempt_at_utc、latest_attempt_status 和 snapshot_stale。

```sql
-- 按平台歌曲 ID 关联当前标签；不会把同名不同版本合并。
SELECT o.event_time_utc, o.track, t.genre, t.language, t.bpm,
       t.recommended_tags, t.snapshot_stale
FROM music.netease_recent_observations o
LEFT JOIN music.current_song_tags t
  ON t.source = 'netease' AND t.platform_track_id = o.platform_track_id;
```

上述查询用当前标签解释既有观测；需要复现某次分析时必须固定 generation/as_of，不能把新标签伪装成历史时点已知的信息。BPM、情绪或场景推荐标签均属于平台歌曲描述，不代表实际收听时长或听者当时情绪；本轮不调用本地模型补猜缺失字段。

## 观测语义

`source=pano` 的 `event_kind` 为 scrobble、love、unlove。只有 scrobble 进入听歌日级指标；love/unlove 是 Pano 文件目标的动作记录，不保证已同步到网易云红心。

Pano 的 source_record_id 由 person/device/stream 命名空间及原文件字节位置组成。身份无网易云 ID 时用原始 track、artist、album、albumArtist 的组合生成 `pano-label:` recording_key；这是来源标签键，不代表已识别标准音乐作品。两个完全相同的日志行，只要位置不同就保留为两次事件。

`source=netease_recent` 的 `event_kind=recent_observation`。用账号 + 歌曲 ID + 上游 playTime 去重，时间缺失时按批次位置保存独立观测。前者可能折叠同曲同毫秒事件，后者不能推断发生日期；证据都保留。`upstream_playTime_unverified` 显式标识未实测确认的时间语义。API 未提供可信设备字段时，device_key 为 null。

Pano 与网易云观测不自动合并，不把二者相加称“总播放次数”。相同歌曲的跨设备记录同样不做猜测去重。

看板的 `overlap_candidates` 是读取时派生的核对线索，不是事实表或已去重播放流水。规则 `metadata-time-candidate-1` 仅比较同一 person 的 Pano scrobble（播放器包名为网易云）与平台近期观测：歌名、艺人展示标签、专辑经 NFC/大小写/空白规范化后均一致；两个曲长均已知且相差不超过 2 秒；两个带时区的时间相差不超过 10 秒；若双方都有平台 ID，必须一致。10 秒是保守候选筛选窗口，不是已经校准的 API 时钟误差范围。元数据缺失不猜测匹配；多个设备/账号的候选冲突标为 ambiguous，不自动选最近的一条。

候选仅列出原观察 ID、时间差、规则版本和 candidate/ambiguous 状态，不修改原始时间、平台 ID、来源计数或证据。仅覆盖看板已载入的记录；不代表全历史匹配。看板默认按单一来源浏览，全部来源模式明确用于核对；所有内置图表均按来源和时间范围独立统计，love/unlove 不进入听歌指标。

`occurred_at_utc`、`observed_at_utc`、`ingested_at_utc` 分别保存来源发生时间、采集观测时间和本库入库时间。网易云分页 scope 还在 raw 中保存采集开始/结束与各页响应；关系快照不能当作同时发生的一组动作。未知来源时区为空，分析时区另存。

`track_duration_ms` 是歌曲元数据长度；`actual_listened_seconds` 始终 null。人工或算法推导的真实时长不在首版契约中。

## 红心详情

`library_items.scope=likes` 的 `entity_id` 始终为红心歌曲 ID。旧版 `payload_json` 是原始 ID；解析器 1.1.0 对含详情的新快照保存 `{id, song, metadata_status}`，其中 `song` 为上游歌曲对象，未取得时为 null。`song.ar` / `song.artists`、`song.al` / `song.album`、`song.dt` / `song.duration` 保留上游信息；名称缺失不等于关系移除，不生成听歌观测，也不增加日级听歌条数。

原始 `likes.body` 附加 `songs`、`detail_pages`、`missing_detail_ids`、`detail_status` 和 `detail_error`，按 ID 校验并关联。关系清单请求成功而补详情失败时，该范围的 `transport_status=ok` 表示清单成功，coverage.detail 记录详情覆盖数；批次整体 `status=partial`，不能据此断言详情齐全。详情声称完整却缺 ID、返回陌生或重复 ID 的快照会拒绝规范化。

## 日级统计

- 默认分析时区 Asia/Shanghai，可在 publish 时选择其他 IANA 时区。
- record_count：该来源、设备/账号、该日的已记录听歌观测数；不混合 love/unlove。
- unique_recording_keys：同一组中不同来源标签键或平台歌曲 ID 的数量，identity_basis 说明是哪一种。
- night_record_count：分析时区 00:00:00 至 05:59:59 的观测数。
- actual_listened_seconds=null；没有记录的日期不自动生成零行。
- coverage_status=observed_only，不能解释为完整一天或所有设备覆盖。
- 网易云时间语义尚未实测，保留 time_basis，消费者应自行选择是否纳入分析。

## 其他项目如何读取

可复制 `integrations/music_snapshot.py` 到分析项目，只依赖 DuckDB 和 Python 标准库。它固定清单、校验文件哈希，在 attach 时检查字段和行数，并在单个事务内建立新 schema；失败全部回滚。使用没有活动事务的连接；不会启动本项目或访问凭据。

```python
import duckdb
from music_snapshot import MusicSnapshot

snapshot = MusicSnapshot(r"data/lake")
con = duckdb.connect()
snapshot.attach(con, schema="music")
rows = con.execute("SELECT * FROM music.daily_music_features ORDER BY date").fetchall()
print(snapshot.generation, snapshot.manifest["data_revision"])
```

跨域分析自行在同一连接注册 Garmin 等来源的固定版本，记录所有输入 generation/as_of 和分析 SQL。这个示例的通用 DuckDB 连接供受信任本地分析使用，不是可接收远程 SQL 的服务。来源记录可通过 imports.raw_ref 与 observations.raw_pointer 返回对应原始字节/JSON 位置。

## 分析视图 1.2.0

可复制读取器在同一个事务中建立以下视图；它们由固定 generation 的发布表派生，不新增事实或修改 SQLite。契约 1.2.0 共 10 个发布数据集，1.1.0 为 9 个，1.0.0 为 8 个；报告和标签视图分别仅在存在 platform_reports、song_tags 时建立。仅用于 `MusicSnapshot.attach()`，CLI `query` 提供基础发布表。解析器、底层字段契约和分析视图分别版本化。

| 视图 | 用途与口径 |
| --- | --- |
| analysis_imports | 截止 as_of 已完成采集/接收的批次；未指定时为全部批次 |
| latest_source_attempt | 按 person/source/account/device/scope 取最后一次尝试，保留失败与待补尾行 |
| latest_successful_source | 每个相同来源范围的最近成功快照；时间相同时按 import_id 确定性排序 |
| library_snapshots | 历次关系采集尝试，含失败、成功空清单、关系完整标记及批次完成时间 |
| library_membership_history | 历次红心/歌单清单的原有关系和当时元数据，不用新详情回填旧快照 |
| library_changes | 相邻成功关系快照的基线、新增、移除及观测区间；不等于操作流水 |
| ranking_history | 历次周榜/总榜条目，保留批次和采集时间，禁止累计当作逐次播放 |
| current_likes | 当前红心关系、歌名、艺人名称/ID 数组、专辑名称/ID、曲长、上游热度、详情状态和 snapshot_stale；不累计历史快照 |
| current_playlists | 当前创建/收藏歌单清单、上游歌曲数/订阅数/播放数；membership_status=not_collected，不冒称已备份成员 |
| current_rankings | 每账号、每周榜/总榜范围的最近成功汇总；保留快照日期和过期标记 |
| pano_listening_events | 仅 Pano scrobble；love/unlove 排除 |
| netease_recent_observations | 仅平台近期观测；不能与前一个视图直接累计为真实总播放次数 |
| platform_report_history | 截止 as_of 的平台报告尝试，包含成功、空结果、失败及原始时间范围 |
| latest_platform_report_attempt | 各账号/报告类型最近尝试；并列按 import_id、id 确定性排序 |
| current_platform_reports | 各账号/报告类型最近成功或空结果，附最近尝试时间和 snapshot_stale；不得累计各次快照 |
| song_tag_history | 截止 as_of 已完成采集的全部歌曲标签尝试，包含成功、空标签及失败 |
| latest_song_tag_attempt | 各 source + platform_track_id 最近一次尝试，不按账号重复计算歌曲 |
| current_song_tags | 各 source + platform_track_id 最近成功或空标签，附类型化标签数组及 snapshot_stale；失败不恢复已被成功空结果取代的旧标签 |

失败的关系采集保留上次成功清单并标记 `snapshot_stale=true`；新的成功空列表会给出空结果，不能复活更早的关系。红心详情失败不使 ID 消失，`metadata_status` 单独表述详情可用性。数值字段未知为 null；ID 保留字符串；曲长单位为毫秒；`event_time_utc` 为 TIMESTAMPTZ，跨域分析自行显式使用分析时区。`reported_popularity` 是上游 pop 值，**不是红心人数、播放次数或本项目自定义评分**。

```python
# 当前红心曲长分布：分析歌曲本身长度，不是本人实际听歌用时。
con.execute("""SELECT count(*) AS tracks,
    count(track_duration_ms) AS duration_known,
    avg(track_duration_ms) / 1000 AS mean_track_seconds
    FROM music.current_likes""").fetchall()

# 已收藏的歌单清单。reported_track_count 是平台报告数，不能当成本地已采成员数。
con.execute("""SELECT name, reported_track_count, membership_status
    FROM music.current_playlists WHERE relationship='subscribed'""").fetchall()

# 分析留痕至少保存这些字段，再保存 SQL、参数、结果及代码版本。
inputs = dict(generation=snapshot.generation,
              database_id=snapshot.manifest['database_id'],
              data_revision=snapshot.manifest['data_revision'],
              schema_version=snapshot.manifest['schema_version'],
              metric_version=snapshot.manifest['metric_version'],
              analysis_view_version=snapshot.analysis_view_version,
              as_of=snapshot.as_of,
              published_at=snapshot.manifest['published_at_utc'])
```

## 历史数据与时点查询

历史分为三层：原始响应/设备累计日志永久归档；事实表追加观测、证据和每批关系/榜单；Parquet 每次发布形成独立 generation。更新 current 指针不会覆盖旧版本。保留实际分析用过的版本；本项目没有自动删除历史的保留策略。

`MusicSnapshot(lake, generation=..., as_of="2026-01-01T00:00:00+08:00")` 在固定版本内按批次采集完成时间筛选上述分析视图；设备日志按电脑接收归档时间筛选。as_of 必须带时区，边界包含该时点。一个范围开始采集但整个批次尚未结束时，其结果不会提前进入时点视图。**8 张基础表以及 daily_music_features 始终是完整 generation 的内容，不受 as_of 筛选**；需要时点日统计应从筛选后的听歌视图重新聚合。

as_of 表示“这份发布版本中，截至某次采集的资料”，不是复原那一刻数据库已入库的严格双时间系统：晚到的历史快照仍可能出现在其采集日期，重复播放观测的正文和主来源保留首次入库版本，其他批次通过 observation_evidence 关联。严格复现某次分析，应固定当时保存的 generation、读取器/指标版本和 SQL；不能仅指定今天资料库上的过去日期。

播放发生时间另用 `event_time_utc` 过滤，不能与采集/接收时间混用。手机离线几天后接收的日志会追加到正确的历史发生日期，不填成接收当天播放；已经发布的旧 generation 保持不变。无记录的日子表示未知，不能补成零。平台总榜不是历史流水，近期窗口已经消失且手机从未记录的播放不能凭空恢复。

`library_changes.change_kind` 的口径：

- `baseline`：该账号/范围第一次成功快照中的条目；区间开始为 null，不能说是那天新收藏。
- `observed_added` / `observed_removed`：相邻成功快照的集合差异。区间从上一快照范围开始观测，到本轮批次完成；是保守的观测边界，不是确定操作时刻。分页期间变化、取消后又加回、两次采样之间的多次操作无法复原。
- 详情补全或歌名更正不算新收藏；失败/部分清单不推导移除；成功空清单会产生移除，之后重新出现会产生新增。账号和 likes/playlists 各自比较，延迟导入按采集时间排列。相同时间的批次只能按 import_id 确定性排序，不能据此断言先后操作。

```python
# 时间截止只作用于分析视图；底层历史表仍完整保留。
lake = r"data/lake"
past = MusicSnapshot(lake, generation=snapshot.generation,
                     as_of="2026-01-01T00:00:00+08:00")
past.attach(con, schema="music_past")
changes = con.execute("""SELECT scope, entity_id, change_kind,
    interval_start_utc, interval_end_utc, previous_import_id, import_id
    FROM music_past.library_changes ORDER BY interval_end_utc""").fetchall()

# 按真实来源发生日期聚合已收到的 Pano 记录；不能称全设备总播放数。
days = con.execute("""SELECT
    (event_time_utc AT TIME ZONE 'Asia/Shanghai')::DATE AS day,
    person_key, device_key, count(*) AS recorded_scrobbles
    FROM music_past.pano_listening_events
    GROUP BY 1,2,3 ORDER BY 1""").fetchall()
```

## 采集缺口与后续扩展契约

1. **实际聆听时长**：当前两个来源均不能证明；不能用曲长、Pano 达标阈值或网易云 App 前台时长代替。
2. **跨设备/来源事件合并**：目前只做来源内幂等，不提供统一真实播放数。后续匹配应独立保存左右观测 ID、规则版本、时间差、歌曲匹配依据、置信信息和人工确认；候选匹配不能删除原始观测。相同名称的现场版/翻唱/混音不能仅靠文本合并。
3. **歌单成员**：正式数据尚无。后续按 account + playlist_id + observed_at 建立成员快照，以 trackIds/trackCount 校验数量，再分批获取详情。顺序、重复条目、不可见歌曲应保留；歌单元数据数量与采到的成员数量分开。
4. **歌曲红心总数**：`song/red/count` 的 count 是当时平台报告值，应独立按 song_id + observed_at 保存；不能和“本人是否喜欢”混为同一字段，也不能将 countDesc 这样的缩写文本强行当精确值。
5. **热评**：按 song_id + comment_id + observed_at 留快照，保存评论发生时间、正文、点赞数、排序方式/页码。热评是选出的样本，不代表全体评论分布；当前点赞数不能冒充历史时点的点赞数。

歌单详情、歌曲红心总数和热评接口已在适配器白名单中，可用于能力探测，但未并入正式事实表或看板统计。上游依据：[歌单详情](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/v4.40.1/module/playlist_detail.js)、[歌曲红心数量](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/v4.40.1/module/song_red_count.js)、[热评](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/v4.40.1/module/comment_hot.js)。

## 分析前验收

`python -m collector_music audit --replay-check` 检查 raw 哈希、SQLite 完整性与外键、当前发布版本/修订、9 个导出基础表逐行一致性、Pano 水位及已读前缀，再从全部原件在临时目录恢复，逐项对比观测、证据、榜单、关系、报告、歌曲标签、流与账号绑定。标签失败按全局歌曲 ID 汇总为警告；同修订发布遗漏非空 song_tags 表或表内容发生变化会校验失败。恢复过程不读取登录态，也不覆盖原库；临时恢复副本自动清理。

审计通过只证明这些存储和恢复检查通过，不证明设备连续覆盖、历史记录完整或样本足够。开始个人长期规律/跨域分析前仍需积累多日实际设备记录，并完成平板、离线、重启、同曲循环等覆盖验收。

## 容量与演进

首版日志与 Parquet 发布会在内存读取数据，适合当前个人规模；query 为防止重复列静默丢值要求唯一结果列名。发布/查询大库的增量分区与流式处理尚未验证，不宣称无限历史容量。源码变更需要提升相应契约或解析版本；不兼容的历史资料应在独立恢复目录重新验证。
