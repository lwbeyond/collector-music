// A private stdin/stdout bridge. No HTTP server, dynamic API names or remote commands.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
const require = createRequire(import.meta.url);
const allowed = new Set([
  'login_qr_key', 'login_qr_create', 'login_qr_check', 'login_status',
  'record_recent_song', 'user_record', 'likelist', 'user_playlist',
  'song_detail',
  'style_preference', 'listen_data_report', 'listen_data_realtime_report',
  'listen_data_total', 'song_wiki_summary',
  'comment_hot', 'song_red_count', 'playlist_detail',
]);
const originalWrite = process.stdout.write.bind(process.stdout);
// Upstream logging can include request objects; only our structured response goes out.
console.log = console.warn = console.error = console.info = () => {};
process.stdout.write = () => true;
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  if (input.length > 1024 * 1024) process.exit(2);
}
try {
  const request = JSON.parse(input);
  if (!allowed.has(request.method)) throw new Error('Unsupported method');
  const root = path.dirname(require.resolve('@neteasecloudmusicapienhanced/api'));
  const api = require(path.join(root, 'module', request.method + '.js'));
  const { cookieToJson } = require(path.join(root, 'util'));
  const params = { ...(request.params || {}), unblock: false };
  params.cookie = typeof params.cookie === 'string' ? cookieToJson(params.cookie) : (params.cookie || {});
  const response = await api(params, async (...args) => {
    const tokenFile = path.join(os.tmpdir(), 'anonymous_token');
    if (!fs.existsSync(tokenFile)) fs.writeFileSync(tokenFile, '', { flag: 'wx', mode: 0o600 });
    return require(path.join(root, 'util/request'))(...args);
  });
  originalWrite(JSON.stringify({ ok: true, status: response.status, body: response.body,
    cookies: request.method.startsWith('login_') ? response.cookie : undefined }));
} catch (error) {
  // Deliberately omit arbitrary upstream exceptions, URLs and request configuration.
  const code = Number(error?.body?.code || error?.status || 0);
  originalWrite(JSON.stringify({ ok: false, code, error: 'NetEase request failed' }));
  process.exitCode = 1;
}
