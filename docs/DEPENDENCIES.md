# 依赖与运行边界

开发环境：Windows x64、Python 3.14.2、Node 24.13.0、DuckDB 1.5.6、tzdata 2026.4。Python 最低要求 3.11、Node 最低要求 22；未在其他系统做整套验收。

- Python 运行依赖固定在 requirements-lock.txt 与 pyproject.toml，构建工具为 setuptools 82.0.1。
- 网易云固定 `@neteasecloudmusicapienhanced/api` 4.40.1，传递依赖完整锁在 package-lock.json，安装使用 `--ignore-scripts`。
- 设备端使用 Pano Scrobbler 4.46（GitHub 发行版或 Google Play 版），安装与校验见 [设备接入](DEVICES.md)。
- Playwright 1.63.0 为开发验收依赖，使用本机 Edge；前台运行不需要它，也没有新增前端框架或在线 CDN。

## SQLite

系统 Python 自带 SQLite 3.50.4。为避开官方已公布的旧运行库 WAL-reset 风险，当前采用 DELETE journal 与 FULL synchronous；不替换系统 DLL。各导入批次 BEGIN IMMEDIATE，在短事务内完成更新，忙等待 30 秒；发布读取同一事务快照。后续升级运行库后再评估 WAL。[官方说明](https://www.sqlite.org/wal.html#walreset)。

## npm 审计

首次审计发现 basic-ftp 与 node-forge 两条公告，经父依赖传播显示为 5 个 high 包。已将 basic-ftp 覆盖固定到修复版 6.2.1；`get-uri` 的 FTP 功能不在采集范围，模块加载与本地 QR 已回归，未声称测试过 FTP 代理链路。

剩余 **GHSA-86w9-cpqp-85rv**：node-forge 1.4.0 的 RSA PKCS#1 v1.5 签名验证问题，检索时 npm 报告无修复版本；因此审计仍返回非零，并显示 node-forge 及其上游两个 high 包，不能记录为零漏洞。源码检查显示本适配器所用上游 util/crypto.js 调用公钥解析与 `encrypt(..., 'NONE')`，未见公告中的签名 verify 调用。此检查限制了已知使用路径，不构成全面安全证明。[公告](https://github.com/advisories/GHSA-86w9-cpqp-85rv)。

桥接器只加载白名单模块，通过 stdin/stdout 单次调用；没有启动全量 HTTP 服务，也不接受任意方法。普通日志不输出上游异常对象、Cookie 或认证请求。后续版本应优先复核这条公告及上游修复，再更新依赖锁和采集回归证据。
