from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import duckdb

from . import SCHEMA_VERSION, __version__
from .collector import PROJECT_ROOT, collect, collect_reports, login
from .common import MusicError, parse_json
from .devices import list_devices, pull_pano, receive_configured
from .lake import publish, query, resolve_version, verify_version
from .maintenance import backup, replay, status, verify
from .netease import import_netease
from .pano import import_pano
from .store import Workspace


def parser():
    root = argparse.ArgumentParser(description="个人音乐数据底座；默认所有资料仅保存在当前项目。")
    root.add_argument("--home", type=Path, default=PROJECT_ROOT, help="独立资料目录")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "verify"):
        commands.add_parser(name)
    auditing = commands.add_parser("audit", help="校验分析快照、日志水位，可选真实原件恢复演练")
    auditing.add_argument("--replay-check", action="store_true")
    scheduled = commands.add_parser("automation-run", help="运行一次已配置的到期任务；不接收 USB")
    scheduled.add_argument("--config", type=Path)
    pano = commands.add_parser("import-pano", help="导入同一个日志实例的完整 JSONL 副本")
    pano.add_argument("file", type=Path)
    for cmd in (pano,):
        cmd.add_argument("--person", default="me")
        cmd.add_argument("--device", required=True)
        cmd.add_argument("--stream", required=True, help="稳定日志实例名；复制文件不能换名")
        cmd.add_argument("--source-timezone")
    pano.add_argument("--collector-version", default="unknown")
    pano.add_argument("--rule-note", default="unknown")
    netease = commands.add_parser("import-netease")
    netease.add_argument("file", type=Path)
    netease.add_argument("--person", default="me")
    publication = commands.add_parser("publish")
    publication.add_argument("--timezone", default="Asia/Shanghai")
    querying = commands.add_parser("query", help="查询已发布快照，不访问活动 SQLite")
    querying.add_argument("sql")
    querying.add_argument("--lake", type=Path)
    querying.add_argument("--generation")
    querying.add_argument("--limit", type=int, default=1000)
    verifying = commands.add_parser("verify-lake")
    verifying.add_argument("--lake", type=Path)
    verifying.add_argument("--generation")
    saving = commands.add_parser("backup")
    saving.add_argument("output", type=Path)
    rebuilding = commands.add_parser("replay")
    rebuilding.add_argument("destination", type=Path)
    auth = commands.add_parser("login")
    auth.add_argument("--timeout", type=int, default=180)
    auth.add_argument("--no-open", action="store_true")
    collecting = commands.add_parser("collect")
    collecting.add_argument("--person", default="me")
    collecting.add_argument("--scopes", default="recent,week,all,likes,playlists")
    collecting.add_argument("--delay", type=float, default=3)
    reports = commands.add_parser("collect-reports", help="只读采集网易云月报、实时收听、曲风偏好与累计时长")
    reports.add_argument("--person", default="me")
    reports.add_argument("--delay", type=float, default=3)
    report_import = commands.add_parser("import-reports", help="导入已归档格式的平台报告快照")
    report_import.add_argument("file", type=Path)
    report_import.add_argument("--person", default="me")
    tags = commands.add_parser("collect-tags", help="按网易云歌曲 ID 补采全库标签；成功空结果也缓存，支持断点续采")
    tags.add_argument("--person", default="me")
    tags.add_argument("--limit", type=int, help="本轮最多请求的歌曲数；默认处理所有待采歌曲")
    tags.add_argument("--delay", type=float, default=3)
    tags.add_argument("--plan", action="store_true", help="仅查看去重后的范围和待采数量，不请求平台")
    tag_import = commands.add_parser("import-tags", help="导入已归档格式的歌曲标签快照")
    tag_import.add_argument("file", type=Path)
    tag_import.add_argument("--person", default="me")
    devices = commands.add_parser("devices")
    devices.add_argument("--adb")
    pulling = commands.add_parser("usb-pull")
    pulling.add_argument("--adb")
    pulling.add_argument("--serial", required=True)
    pulling.add_argument("--device", required=True)
    pulling.add_argument("--stream", required=True)
    pulling.add_argument("--remote", required=True)
    pulling.add_argument("--person", default="me")
    pulling.add_argument("--source-timezone")
    pulling.add_argument("--collector-version", default="unknown")
    pulling.add_argument("--rule-note", default="unknown")
    receiving = commands.add_parser("receive-phone", help="按私有配置接收已绑定手机的日志，然后发布")
    receiving.add_argument("--config", type=Path)
    dashboard = commands.add_parser("dashboard", help="本机音乐数据看板")
    dashboard.add_argument("--port", type=int, default=8767)
    dashboard.add_argument("--no-open", action="store_true")
    dashboard.add_argument("--background", action="store_true")
    scanning = commands.add_parser("scan", help="导入本机已配置日志，然后发布快照；不联网")
    scanning.add_argument("--config", type=Path)
    return root


def execute(args):
    workspace = Workspace(args.home)
    command = args.command
    if command == "dashboard":
        from .dashboard import launch, serve
        if not 1024 <= args.port <= 65535:
            raise MusicError("端口应在 1024–65535 之间。")
        if args.background:
            return launch(workspace.home, args.port, not args.no_open)
        serve(workspace.home, args.port, not args.no_open)
        return {"status": "stopped"}
    if command == "init":
        workspace.initialize()
        return {"status": "initialized", "home": str(workspace.home), "schema": SCHEMA_VERSION}
    if command == "status":
        return status(workspace)
    if command == "verify":
        return verify(workspace)
    if command == "audit":
        from .audit import audit
        return audit(workspace, args.replay_check)
    if command == "automation-run":
        from .automation import run_tick
        return run_tick(workspace, args.config)
    if command == "import-pano":
        return import_pano(workspace, args.file, person=args.person, device=args.device, stream=args.stream,
                           source_timezone=args.source_timezone, collector_version=args.collector_version, rule_note=args.rule_note)
    if command == "import-netease":
        return import_netease(workspace, args.file, person=args.person)
    if command == "publish":
        return publish(workspace, args.timezone)
    if command == "query":
        return query(args.lake or workspace.data / "lake", args.sql, args.generation, args.limit)
    if command == "verify-lake":
        manifest = verify_version(resolve_version(args.lake or workspace.data / "lake", args.generation))
        return {"status": "ok", "generation": manifest["generation"], "data_revision": manifest["data_revision"]}
    if command == "backup":
        return backup(workspace, args.output)
    if command == "replay":
        return replay(workspace, args.destination)
    if command == "login":
        if not 15 <= args.timeout <= 600:
            raise MusicError("扫码超时范围为 15–600 秒。")
        return login(workspace, args.timeout, not args.no_open, lambda text: print(text, file=sys.stderr, flush=True))
    if command == "collect":
        workspace.initialize()
        with workspace.lock("collect"):
            return collect(workspace, args.person, tuple(args.scopes.split(",")), args.delay)
    if command == "collect-reports":
        workspace.initialize()
        with workspace.lock("collect"):
            return collect_reports(workspace, args.person, args.delay)
    if command == "import-reports":
        from .platform_reports import import_reports
        return import_reports(workspace, args.file, person=args.person)
    if command == "collect-tags":
        from .tag_collection import collect_song_tags, plan_song_tags
        if args.plan:
            return plan_song_tags(workspace, person=args.person, limit=args.limit)
        return collect_song_tags(workspace, person=args.person, limit=args.limit, delay=args.delay,
            notify=lambda progress: print(json.dumps(progress, ensure_ascii=False), file=sys.stderr, flush=True))
    if command == "import-tags":
        from .song_tags import import_song_tags
        return import_song_tags(workspace, args.file, person=args.person)
    if command == "devices":
        return list_devices(args.adb)
    if command == "usb-pull":
        return pull_pano(workspace, serial=args.serial, device=args.device, stream=args.stream,
            remote=args.remote, adb=args.adb, person=args.person, source_timezone=args.source_timezone,
            collector_version=args.collector_version, rule_note=args.rule_note)
    if command == "receive-phone":
        result = receive_configured(workspace, args.config or workspace.home / ".local/phone.json")
        return {"import": result, "publication": publish(workspace)}
    if command == "scan":
        path = args.config or workspace.home / ".local/config.json"
        if not path.exists():
            raise MusicError("尚未配置日志路径；参考 config.example.json 建立 .local/config.json。")
        config = parse_json(path.read_bytes())
        if config.get("version") != 1 or not isinstance(config.get("pano_logs"), list) or not config["pano_logs"]:
            raise MusicError("需要 version=1 和至少一个 pano_logs 来源。")
        workspace.initialize()
        with workspace.lock("scan"):
            imports = []
            for item in config["pano_logs"]:
                log_path = Path(item["path"])
                if not log_path.is_absolute():
                    log_path = workspace.home / log_path
                imports.append(import_pano(workspace, log_path, person=config.get("person_key", "me"),
                    device=item["device_key"], stream=item["stream_name"],
                    source_timezone=item.get("source_timezone"), collector_version=item.get("collector_version", "unknown"),
                    rule_note=item.get("rule_note", "unknown")))
            return {"imports": imports, "publication": publish(workspace, config.get("analysis_timezone", "Asia/Shanghai"))}
    raise MusicError("未知命令。")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
        return 2 if result.get("status") in ("partial", "warning", "stopped", "deferred") or result.get("rejected", 0) else 0
    except (MusicError, OSError, sqlite3.Error, duckdb.Error, KeyError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止，完成的归档仍保留。", file=sys.stderr)
        return 130
