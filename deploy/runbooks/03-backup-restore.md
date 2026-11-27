# 03 · 备份恢复运维手册

## 1. 备份对象与边界

- **已实现并实测演练**：PostgreSQL 全库备份（`pg_dump -Fc` 自定义格式）——`aitrace-backup` CLI，systemd 每日 03:00 定时（`Persistent=true`，错过补跑）。
- **L2 rawlogs 说明**：原始日志按 case 从 aitest 日志下载接口可重新拉取（`GET /api/v1/aitest/logs/*`，LogClient 同源）；本地 `data/rawlogs` 属可再生数据，当前 CLI **不**自动打包。如要求本地冷备，另行 rsync（自定），不计入 AITrace 备份责任。

## 2. 关键配置（`BackupSettings` 默认值）

| 配置 | 默认值 | 环境变量 |
|---|---|---|
| 暂存目录 | `data/backups` | `AITRACE_BACKUP__STAGING_DIR` |
| 保留份数 | 3 | `AITRACE_BACKUP__KEEP_COUNT` |
| 异盘/异地推送 | 无（本地保留） | `AITRACE_BACKUP__REMOTE_DEST` |
| 备份前最小可用空间 | 4 GiB | `AITRACE_BACKUP__MIN_FREE_BYTES` |

- 空间预检：暂存目录可用空间 < `min_free_bytes` 时跳过本次备份并告警（`SpaceShortageError`），不写爆数据盘、不触发 `DISK_FULL` 背压。
- 每次备份产出：`aitrace-YYYYMMDD-HHMMSS.dump` + 同名 `.digest`（SHA-256），dump 完成后用 `pg_restore --list` 做清单校验，失败即告警并保留 dump 待人工核查。
- 按 `keep_count` 清理旧备份；`remote_dest` 推送失败降级为「本地保留 + WARNING 日志」。

## 3. 命令形态（与 `tests/ops/test_backup_drill.py` 实测一致）

单元演练（破坏性恢复需显式开启）：

```bash
make ops   # AITRACE_OPS_DRILL=1 + TEST_DATABASE_URL + PATH 中有 pg_dump/pg_restore
```

生产中由 systemd 单元执行：

```bash
systemctl list-timers aitrace-backup.timer
systemctl start aitrace-backup.service      # 手工触发一次
systemctl status aitrace-backup.service     # 确认 ExitStatus=0
```

手动执行等价 CLI：

```bash
/opt/aitrace/current/venv/bin/aitrace-backup backup --config /etc/aitrace/aitrace.yaml
/opt/aitrace/current/venv/bin/aitrace-backup restore <dump 路径> --force
```

`restore --force` 为破坏性操作：非演练环境执行前必须通知相关方并确认无在途投递。

## 4. 恢复演练步骤

1. 停服务（`systemctl stop aitrace.service`）；
2. 记录当前版本与最近备份清单（`*.dump` + `*.digest` 配对检查 SHA-256）；
3. 执行恢复（见上）；恢复前无 `--force` 时 CLI 会列出当前 `task_queue` 行数并要求确认；
4. 启动服务并验证：
   - `/readyz` 启动自检五查通过（DB / 迁移 / 画像 / 目录 / 资源）；
   - 审计哈希链：监控快照 `audit.verified == true`（等价于 `AuditService.verify_chain()`）；
   - 抽样确认 `task_queue`/`user_queue` 状态与恢复时点一致；
5. 恢复后首轮 tick 观察：清洗池与 AI Worker 租约抢占、回调 seq 不回退不重复。

## 5. 恢复后必查清单

- [ ] `alembic_version` 存在且为当前 head（迁移历史随库恢复）；
- [ ] 审计链验证通过；
- [ ] 看板⑥实时监控台无哈希链失败、无 fencing 拒绝告警；
- [ ] 恢复演练记录（时间 / dump 名 / 验证结果）归档。