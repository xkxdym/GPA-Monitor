# CPA-Monitor

CPA-Monitor 是一个基于 Python + SQLite 的本地监控工具，支持多配置档统计、趋势分析、调用日志查看，并已集成 CPA 授权文件同步到 sub2api 的能力。

## 1. 启动

```powershell
python server.py
```

默认访问地址：

```text
http://127.0.0.1:8088
```

页面入口：
- 监控页：`/`
- 配置页：`/config.html`
- 授权同步页：`/auth-sync.html`

## 2. 主要功能

- 多配置档（profile）管理：新增、编辑、删除、切换 active
- 手动拉取和自动定时拉取统计数据
- 模型、来源账号、真实用户维度统计
- 趋势图、健康度、日志与明细查询
- 过期数据自动清理与手动清理
- 授权同步：将 CPA `/auth-files` 授权文件转换并推送到 sub2api

## 3. 授权同步说明

参数配置入口：`/config.html`（授权同步配置）
- 同步周期与策略
- sub2 基础地址与授权（默认 `x-api-key`）
- 是否校验 SSL、最大处理文件数等
- 文件过滤策略（含“仅同步已启用文件”）

执行与查看入口：`/auth-sync.html`
- 查看同步状态（运行状态、下次执行、上次结果）
- 手动获取 CPA 认证文件列表并缓存到数据库
- 勾选文件后执行“同步选中认证文件”
- 查看同步记录（数据库保存，保留 7 天自动清理）

## 4. API

### 监控相关
- `GET /api/health`
- `GET /api/config`
- `GET /api/profiles`
- `GET /api/stats?hours=24&keyword=&profile_id=1`
- `GET /api/trend?hours=24&limit=200&profile_id=1`
- `GET /api/logs?limit=20&profile_id=1`
- `GET /api/records?hours=24&limit=300&keyword=&profile_id=1`
- `POST /api/config`
- `POST /api/profiles/upsert`
- `POST /api/profiles/select`
- `POST /api/profiles/delete`
- `POST /api/refresh`
- `POST /api/cache/prune`
- `POST /api/cache/clear`

### 授权同步相关
- `GET /api/auth-sync/status`
- `GET /api/auth-sync/config`
- `GET /api/auth-sync/records?limit=200`
- `GET /api/auth-sync/files`
- `GET /api/auth-sync/files-last`
- `POST /api/auth-sync/config`
- `POST /api/auth-sync/run`
- `POST /api/auth-sync/sync-selected`
- `POST /api/auth-sync/files-enabled`
- `POST /api/auth-sync/sub2-validate`

## 5. 数据库

默认数据库文件：`stats.db`

主要表：
- `app_config`：全局监控配置
- `auth_sync_settings`：授权同步参数配置
- `auth_sync_cached_files`：最近一次获取的 CPA 认证文件缓存
- `auth_sync_file_records`：文件级同步审计记录
- `auth_sync_records`：周期/文件同步事件记录（7 天自动清理）
- `profiles`：监控配置档
- `usage_records`：聚合明细
- `pull_snapshots`：趋势快照
- `pull_logs`：拉取日志

## 6. Docker Compose 部署

启动：

```powershell
docker compose up -d --build
```

查看状态与日志：

```powershell
docker compose ps
docker compose logs -f
```

停止：

```powershell
docker compose down
```

默认容器访问地址：

```text
http://127.0.0.1:18088
```
