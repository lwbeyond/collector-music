"""Read-only, bounded coverage check for NetEase's existing song labels.

Run from the project environment. Private evidence goes under .local/validation;
the command does not add tags to the music database or update scheduled work.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector_music.collector import account_from_status, bridge, clean_body
from collector_music.common import LoginRequired, MusicError, atomic_write, json_bytes, now, parse_json
from collector_music.netease import platform_id, reject_credentials
from collector_music.store import Workspace
from collector_music.song_tags import FIELD_TITLES, extract_labels


def spread(items, count):
    """Deterministic coverage across a list, including both ends when possible."""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[index * (len(items) - 1) // (count - 1)] for index in range(count)]


def select_sample(connection, account, limit=30):
    if type(limit) is not int or not 1 <= limit <= 60:
        raise MusicError("覆盖抽查仅支持 1–60 首歌曲。")
    recent = [dict(row) for row in connection.execute("""SELECT platform_track_id AS song_id,
        max(occurred_at_utc) AS last_observed_at FROM observations
        WHERE source='netease_recent' AND account_key=? AND event_kind='recent_observation'
          AND platform_track_id IS NOT NULL
        GROUP BY platform_track_id ORDER BY last_observed_at DESC,song_id""", (account,))]
    latest = connection.execute("""SELECT * FROM coverage WHERE source='netease' AND scope='likes'
        AND account_key=? ORDER BY observed_at_utc DESC,import_id DESC LIMIT 1""", (account,)).fetchone()
    good = connection.execute("""SELECT * FROM coverage WHERE source='netease' AND scope='likes'
        AND account_key=? AND transport_status='ok'
        ORDER BY observed_at_utc DESC,import_id DESC LIMIT 1""", (account,)).fetchone()
    liked = []
    if good:
        liked = [dict(row) for row in connection.execute("""SELECT entity_id AS song_id,position
            FROM library_items WHERE import_id=? AND scope='likes' AND account_key=?
            ORDER BY position,entity_id""", (good['import_id'], account))]
    # Deduplicate the source pool without assuming every API list is unique.
    liked = list({row['song_id']: row for row in reversed(liked)}.values())[::-1]
    selected = [dict(row, sample_source='recent') for row in recent[:(limit + 1) // 2]]
    seen = {row['song_id'] for row in selected}
    eligible = [row for row in liked if row['song_id'] not in seen]
    selected.extend(dict(row, sample_source='likes') for row in spread(eligible, limit - len(selected)))
    seen = {row['song_id'] for row in selected}
    if len(selected) < limit:
        selected.extend(dict(row, sample_source='recent') for row in recent
                        if row['song_id'] not in seen)
        selected = selected[:limit]
    basis = dict(method='latest_distinct_recent_then_evenly_spaced_current_likes',
        requested=limit, selected=len(selected), recent_pool=len(recent), liked_pool=len(liked),
        sources={source: sum(row['sample_source'] == source for row in selected) for source in ('recent', 'likes')},
        likes_snapshot_at=good['observed_at_utc'] if good else None,
        likes_snapshot_import_id=good['import_id'] if good else None,
        likes_snapshot_stale=bool(good and latest and latest['transport_status'] != 'ok'),
        note='定向小样本，不是随机抽样；覆盖率不代表整个曲库。')
    return selected, basis


def summarize(results, basis):
    successful = [row for row in results if row['status'] == 'ok']
    attempted = [row for row in results if row['status'] != 'not_attempted']
    counts = {field: sum(row['available'][field] for row in successful) for field in FIELD_TITLES.values()}
    denominator = len(successful)
    return dict(format='collector-music-tag-coverage', version=1, checked_at_utc=now(), sample=basis,
        attempted=len(attempted), succeeded=denominator, failed=len(attempted) - denominator,
        not_attempted=max(basis['selected'] - len(attempted), 0),
        coverage={field: dict(present=count, missing=denominator-count,
            fraction=count/denominator if denominator else None) for field, count in counts.items()},
        results=results,
        interpretation='覆盖分母仅包含成功响应；失败与未请求单列。推荐标签属于平台歌曲标签，不代表听者情绪。')


def check_coverage(workspace, output, limit=30, delay=3, notify=print):
    if delay < 3:
        raise MusicError("采集请求间隔不得小于 3 秒。")
    output = Path(output).resolve()
    validation_root = (workspace.home / '.local/validation').resolve()
    if not output.is_relative_to(validation_root):
        raise MusicError("覆盖验证结果必须保存在项目 .local/validation 下。")
    if output.exists() and any(output.iterdir()):
        raise MusicError("验证目录已有结果，请使用新的目录以保留历史证据。")
    session = parse_json((workspace.home / '.local/session.json').read_bytes())
    cookie, account = session.get('cookie'), platform_id(session.get('account_key'))
    if not isinstance(cookie, str) or not cookie:
        raise LoginRequired("本机会话无效，请重新登录。")
    results = []
    with workspace.lock('collect'):
        verified = account_from_status(bridge('login_status', {'cookie': cookie})['body'])
        if verified != account:
            raise LoginRequired("登录账号与本地绑定不一致，停止抽查。")
        with closing(workspace.connect(readonly=True)) as connection:
            connection.execute('BEGIN')
            selected, basis = select_sample(connection, account, limit)
            basis['data_revision'] = connection.execute('SELECT coalesce(max(seq),0) FROM changes').fetchone()[0]
            connection.rollback()
        if not selected:
            raise MusicError("当前账号没有可供抽查的已采集歌曲。")
        stopped = False
        for index, song in enumerate(selected, 1):
            record = dict(song, status='not_attempted')
            if stopped:
                record['reason'] = '此前请求失败，本轮停止'
            else:
                time.sleep(delay)
                record['observed_at_utc'] = now()
                try:
                    body = clean_body(bridge('song_wiki_summary', {'cookie': cookie, 'id': song['song_id']})['body'])
                    reject_credentials(body)
                    atomic_write(output / 'responses' / (str(index).zfill(2) + '.json'),
                        json_bytes(dict(method='song_wiki_summary', song_id=song['song_id'],
                            observed_at_utc=record['observed_at_utc'], body=body)))
                    if isinstance(body, dict) and body.get('code') in (301, 401):
                        raise LoginRequired("网易云登录已失效，请重新扫码。")
                    record.update(extract_labels(body), status='ok')
                except MusicError as error:
                    record.update(status='failed', reason=str(error), needs_login=isinstance(error, LoginRequired))
                    stopped = True
                except (KeyError, TypeError, ValueError):
                    record.update(status='failed', reason='歌曲百科响应结构无效')
                    stopped = True
            results.append(record)
            atomic_write(output / 'summary.json', json_bytes(summarize(results, basis)))
            notify(f"标签抽查 {index}/{len(selected)}：{record['status']}")
    return summarize(results, basis)


def main(argv=None):
    parser = argparse.ArgumentParser(description='抽查网易云现成歌曲标签覆盖率，结果仅保存在私有验证目录。')
    parser.add_argument('--home', type=Path, default=ROOT)
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    workspace = Workspace(args.home)
    config_path = workspace.home / '.local/automation.json'
    if config_path.exists():
        node = parse_json(config_path.read_bytes()).get('node_executable')
        if node:
            os.environ['MUSIC_NODE_EXE'] = node
    suffix = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    output = args.output or workspace.home / '.local/validation' / ('tag-coverage-' + suffix)
    try:
        report = check_coverage(workspace, output, args.limit, notify=lambda message: print(message, flush=True))
        print(json.dumps({key: value for key, value in report.items() if key != 'results'}, ensure_ascii=False, indent=2))
        print(f"验证结果：{output.resolve() / 'summary.json'}")
        return 2 if report['failed'] or report['not_attempted'] else 0
    except (MusicError, OSError) as error:
        message = str(error) if isinstance(error, MusicError) else '无法读取本地配置或保存验证结果。'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
