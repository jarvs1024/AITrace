# 04 · 发布回滚运维手册

## 1. 发布前完整门禁顺序（与 README 一致，禁止跳过）

1. 停常驻演示服务；
2. `make verify`（lint + test + contract + e2e）；
3. `make test-security`（token / probe / audit / whitelist / credential / startup / backup 关键词安全集）；
4. `make ops`（`AITRACE_OPS_DRILL=1`，真实 `pg_dump`/`pg_restore` 恢复演练）；
5. `make perf` 快配：`tools/perf_harness.py --profile all --batches 8`（输出 `perf_report.json`，对照 §7.1 SLO 打 PASS/FAIL 摘要）；
6. `make lint`。

全部绿勾后方可进入发布步骤。

## 2. 发布步骤（六步，`deploy/scripts`）

| 步骤 | 脚本 | 说明 |
|---|---|---|
| 1 安装发布目录 | `install_release.sh <release-dir> [--make-current]` | 要求源目录含 `REVISION`；复制到 `${AITRACE_INSTALL_ROOT:-/opt/aitrace}/releases/<revision>`，建 venv 装 wheel，生成 `alembic.ini` |
| 2 停旧版本并备份检查 | `upgrade.sh [--skip-backup-check] <release-dir>` 内建步骤 | 升级前要求当前版本完成一次备份（`data/backups` 内存在即可），可显式跳过 |
| 3 DB migrate | `migrate.sh <release-dir>` | 用发布目录 venv 的 alembic `upgrade head`。迁移策略：**只增不改**，回退不回滚数据 |
| 4 切换 current | `upgrade.sh` 内 `ln -sfn <release-dir> ${ROOT}/current` | 原子软链切换 |
| 5 启动 + readyz | `upgrade.sh` 内 systemctl start + `/readyz` 轮询 | 默认 60 次 × 2s；自检五查通过才算升级成功 |
| 6 升版收尾 | 观察周期 | 灰度观察（见手册 05）、告警群静默、备份任一次成功 |

全过程日志：`${AITRACE_UPGRADE_LOG:-/var/log/aitrace/upgrade.log}`。

## 3. 回滚（`rollback.sh <release-dir>`）

```
systemctl stop aitrace.service
ln -sfn <旧版本 release-dir> /opt/aitrace/current
systemctl start aitrace.service
```

回滚边界声明：

- **代码回旧版本，数据库不回滚**——迁移只增不改，旧代码与新 schema 兼容（`extra="forbid"` 配置模型要求发布前已核对）；
- 灰度配置回位：若新版本调整过 `AITRACE_GRAYSCALE__*`，回滚后恢复旧值；
- 令牌不受影响：令牌驻 aitest 侧，任何发布动作不触碰令牌；回滚后跑 `make test-security` 验证令牌链路；
- 崩溃/数据损坏场景不走本流程，走手册 03 恢复。

## 4. 保留策略

发布目录保留在 `${ROOT}/releases/<revision>`（同 revision 幂等拒绝覆盖）；历史版本保留数与清理频率由运维按磁盘水位（70% / 85% 告警线，手册 02）决定，删除前确认 `rollback.sh` 目标不再需要。

## 5. 发布事后必查

- [ ] `/readyz` 返回 healthy；
- [ ] 看板⑥审计链 verified、无 fencing 拒绝与哈希链告警；
- [ ] 灰度观察期指标（手册 05）正常；
- [ ] 本次发布记录归档（版本 / 迁移版本号 / 观察结论）。