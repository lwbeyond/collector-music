# collector-music

Archive **your own** music listening data locally: continuous plays logged on your Android phone/tablet by [Pano Scrobbler](https://github.com/kawaiiDango/pano-scrobbler) (local JSONL), plus read-only NetEase Cloud Music (网易云音乐) account snapshots — recent plays, weekly/all-time rankings, liked songs, playlists, platform listening reports and song tags. Everything goes into a raw archive → SQLite → versioned Parquet → DuckDB pipeline with a local read-only dashboard.

在本地归档**你自己的**听歌数据：Android 手机/平板上由 Pano Scrobbler 写入的本地 JSONL 听歌记录，加上网易云音乐账号的只读快照（最近播放、周榜/总榜、红心、歌单、平台听歌报告、歌曲标签）。数据经“原始归档 → SQLite → 版本化 Parquet → DuckDB”处理，并提供本机只读看板。

> This repository contains code and docs only — **no personal data, cookies, account IDs or device identifiers**. See [Privacy](#privacy--隐私).

## How collection works / 采集原理

### 1. Device logs (Pano Scrobbler JSONL) / 设备日志

Pano Scrobbler on the phone writes plays that pass the scrobble threshold to `Download/music/pano.jsonl` (local file target, no Last.fm account needed). The computer imports full copies of that file:

- **Manual copy**: `python -m collector_music import-pano <file> --device phone --stream pano-main-001` (or console options 2/3).
- **USB (ADB, read-only)**: `devices` lists attached devices; `usb-pull --serial <SERIAL> --device phone --stream pano-main-001 --remote /sdcard/Download/music/pano.jsonl` runs `adb pull` only (never deletes or truncates the phone file) and binds the logical device label to a **SHA-256 of the serial**. Afterwards `receive-phone` (`03-接收手机日志.cmd`) uses `.local/phone.json` to find only that bound device. ADB is looked up via `--adb`, `MUSIC_ADB_EXE` or `PATH`.
- Each stream is idempotent: the importer verifies the already-imported prefix hash and only appends new complete lines; truncation/rewrites are rejected; an unfinished last line stays pending.

Setup on the device: [docs/DEVICES.md](docs/DEVICES.md).

### 2. NetEase Cloud Music account snapshots / 网易云账号快照

Uses [NeteaseCloudMusicApiEnhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced) (`@neteasecloudmusicapienhanced/api` 4.40.1) **as a library, not as an HTTP server**: `adapters/netease.mjs` is a one-shot stdin/stdout Node bridge that only allows a fixed whitelist of read-only modules (`login_qr_*`, `login_status`, `record_recent_song`, `user_record`, `likelist`, `user_playlist`, `song_detail`, `listen_data_*`, `style_preference`, `song_wiki_summary`, …) and suppresses upstream logging.

**Login** — `python -m collector_music login` (`01-网易云登录.cmd`):
1. `login_qr_key` → `login_qr_create` produces a QR image at `.local/login-qr.png` and opens a local page `.local/login.html`.
2. Scan with the NetEase Cloud Music app; `login_qr_check` is polled every 3 s (default 180 s timeout; 800 = expired, 801/802 = waiting, 803 = success).
3. On success the cookie string is verified with `login_status` and saved to **`.local/session.json`** (`account_key`, `cookie`, `saved_at_utc`; file mode 0600) inside the project, which is Git-ignored. It is never printed or written to the raw archive.

**Collect** — `python -m collector_music collect [--scopes recent,week,all,likes,playlists] [--delay 3]` (`02-采集账号快照.cmd`), then `publish`:

| Scope | Module | Meaning |
|---|---|---|
| `recent` | `record_recent_song` (limit 100) | recent-play window snapshot; no cursor, not a complete history |
| `week` / `all` | `user_record` type 1 / 0 | ranking snapshots; never summed into plays |
| `likes` | `likelist` + `song_detail` (batches of 200) | liked-song IDs; details may be partial, IDs are kept |
| `playlists` | `user_playlist` (limit 100, paged until `more=false`) | playlist list (not playlist members) |

- Requests are serial with ≥3 s delay; the account is re-verified before collecting; the first failure stops remaining scopes and is recorded (exit code 2 = partial).
- Any `cookie`/`set-cookie`/`authorization`/`MUSIC_U`/`__csrf` key is stripped from responses, and the importer refuses snapshots that still contain them.
- `collect-reports`: monthly report, realtime month report, style preference, total duration (`listen_data_report`, `listen_data_realtime_report`, `style_preference`, `listen_data_total`).
- `collect-tags [--plan] [--limit N]`: song genre/language/BPM/recommended tags from `song_wiki_summary`, deduplicated by song ID, resumable.

### 3. Storage & publishing / 存储与发布

`raw/` (immutable captures + manifests) → `data/music.sqlite` (imports, facts, coverage, changes) → `publish` writes a validated, versioned Parquet generation under `data/lake/versions/` and atomically switches `data/lake/current.json`. `query "<SELECT ...>"` runs DuckDB over the published snapshot only. `integrations/music_snapshot.py` is a copyable reader for other projects. Contract: [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md).

### 4. Scheduling (optional, Windows) / 定时任务

`scripts/install_scheduled_task.ps1` registers `CollectorMusic-AutomaticArchive` (checks every 15 min while you are logged in). Default intervals from `automation.example.json` → `.local/automation.json`: recent every 3 h, likes/playlists/rankings daily, reports daily, new-song tags every 15 min (≤20 songs), DB backup daily, restore audit weekly. **USB/device import is never scheduled.** Remove with `-Remove`. Details: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Setup / 安装

Python 3.11+ and Node 22+ (developed on Windows; the Python/Node code is cross-platform, `.cmd`/`.ps1` helpers are Windows-only).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
npm ci --ignore-scripts --no-audit --no-fund
.\.venv\Scripts\python.exe -m collector_music init
.\.venv\Scripts\python.exe -m collector_music login
.\.venv\Scripts\python.exe -m collector_music collect
.\.venv\Scripts\python.exe -m collector_music publish
.\.venv\Scripts\python.exe -m collector_music dashboard     # http://127.0.0.1:8767
```

Or double-click `安装环境.cmd`, then `00-控制台.cmd` (menu), `01-网易云登录.cmd`, `02-采集账号快照.cmd`, `03-接收手机日志.cmd`, `04-打开音乐看板.cmd`.

Config templates: `config.example.json` → `.local/config.json` (log paths for `scan`), `phone.example.json` → `.local/phone.json` (USB receive), `automation.example.json` → `.local/automation.json`. Optional environment variables: [.env.example](.env.example).

Other commands: `status`, `scan`, `verify`, `verify-lake`, `backup <new.sqlite>`, `replay <dir>`, `audit --replay-check`, `import-netease|import-reports|import-tags <file>`.

## Tests / 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v    # synthetic temp workspaces only
npm run test:analytics; npm run test:reports; npm run test:tags   # Playwright + Microsoft Edge, synthetic fixtures
npm run test:ui    # needs a running dashboard with your own published data
```

## Privacy / 隐私

- No personal data ships with this repo: no logs, raw captures, databases, Parquet lakes, cookies, account IDs, device serials or screenshots. Test fixtures are synthetic.
- The NetEase login cookie lives only in `.local/session.json` (Git-ignored). Delete it to sign out locally. Device bindings store a hash of the serial, not the serial.
- `.gitignore` excludes `.local/`, `raw/`, `data/`, `inbox/`, `output/`, `reports/`, `*.sqlite*`, `*.db`, `*.parquet`, `*.duckdb`, `*.jsonl`, `*.log`, `.env*`, cookie/credential/token files, `node_modules/`.
- The dashboard binds to `127.0.0.1` and is read-only.

隐私：仓库不含任何个人数据；网易云登录 Cookie 只保存在被 Git 忽略的 `.local/session.json`；设备绑定只保存序列号摘要。

## Disclaimer / 声明

Unofficial project, not affiliated with NetEase Cloud Music or Pano Scrobbler. It relies on undocumented APIs via a third-party library that may change or break at any time. Use it only for **your own account**, keep request rates low, and respect the platform's Terms of Service. Upstream data limits apply: recent-play windows and rankings are not a complete listening history, and `actual_listened_seconds` is always null. 仅限采集本人账号数据，请遵守平台用户协议。

## License

[MIT](LICENSE)
