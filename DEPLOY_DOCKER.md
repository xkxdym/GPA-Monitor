# Docker 部署与 GitHub 镜像构建

本文说明如何通过 Git 拉取代码部署，以及如何使用 GitHub Actions 自动构建并推送 Docker 镜像。

## 1. 环境要求

- 已安装 Git
- 已安装 Docker 和 Docker Compose v2
- 服务器可访问代码仓库 / 镜像仓库

检查命令：

```bash
git --version
docker --version
docker compose version
```

## 2. 方式 A：源码 + 本地构建（推荐开发/自托管）

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/xkxdym/GPA-Monitor.git CPA-Monitor
cd CPA-Monitor
docker compose up -d --build
```

查看状态与日志：

```bash
docker compose ps
docker compose logs -f
```

访问地址：

```text
http://服务器IP:18088
```

本机访问：

```text
http://127.0.0.1:18088
```

## 3. 方式 B：直接拉取 GitHub 构建的镜像

推送到 `main` 或打 `v*` 标签后，GitHub Actions 会构建并推送镜像到 GHCR：

```text
ghcr.io/xkxdym/gpa-monitor:latest
ghcr.io/xkxdym/gpa-monitor:sha-<commit>
ghcr.io/xkxdym/gpa-monitor:v1.0.0   # 打 tag 时
```

### 3.1 公开仓库

若 Package 已设为 Public，可直接拉取：

```bash
docker pull ghcr.io/xkxdym/gpa-monitor:latest
```

### 3.2 私有仓库 / 私有 Package

先登录 GitHub Container Registry（使用 Personal Access Token，需 `read:packages` 权限）：

```bash
echo YOUR_GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker pull ghcr.io/xkxdym/gpa-monitor:latest
```

### 3.3 使用预构建镜像启动

可创建 `docker-compose.prod.yml`：

```yaml
services:
  cpa-monitor:
    image: ghcr.io/xkxdym/gpa-monitor:latest
    container_name: cpa-monitor
    restart: unless-stopped
    environment:
      HOST: 0.0.0.0
      PORT: 8088
      DB_PATH: /app/data/stats.db
      PYTHONUNBUFFERED: "1"
    ports:
      - "18088:8088"
    volumes:
      - ./data:/app/data
```

启动：

```bash
docker compose -f docker-compose.prod.yml up -d
```

更新镜像：

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## 4. GitHub Actions 自动构建说明

工作流文件：`.github/workflows/docker-publish.yml`

触发条件：

| 事件 | 行为 |
|------|------|
| push 到 `main` | 构建并推送 `latest`、分支名、`sha-` 标签 |
| 推送 `v*` 标签（如 `v1.0.0`） | 推送语义化版本标签 |
| Pull Request | 仅构建验证，不推送 |
| workflow_dispatch | 手动触发 |

镜像仓库地址（小写）：

```text
ghcr.io/xkxdym/gpa-monitor
```

### 4.1 首次使用后设置 Package 可见性

1. 打开仓库：`https://github.com/xkxdym/GPA-Monitor`
2. 右侧 **Packages** 或个人/组织的 Packages 页面
3. 找到 `gpa-monitor`
4. **Package settings** → 按需设为 Public，或关联到本仓库

私有镜像仅授权用户可拉取。

### 4.2 发布版本示例

```bash
git tag v1.0.0
git push origin v1.0.0
```

Actions 会推送 `ghcr.io/xkxdym/gpa-monitor:v1.0.0` 等标签。

## 5. 数据持久化

`docker-compose.yml` 已将容器内数据库目录挂载到宿主机：

```yaml
volumes:
  - ./data:/app/data
```

SQLite 数据文件保存在：

```text
./data/stats.db
```

更新镜像或重建容器不会删除该数据文件。备份时备份 `data` 目录即可。

## 6. 源码更新部署

```bash
cd /opt/CPA-Monitor
git pull
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

## 7. 回滚到指定版本

查看提交记录：

```bash
git log --oneline -n 10
```

切换到指定提交并重建：

```bash
git checkout <commit-id>
docker compose up -d --build
```

恢复到主分支最新版本：

```bash
git checkout main
git pull
docker compose up -d --build
```

若使用 GHCR 镜像回滚：

```bash
docker pull ghcr.io/xkxdym/gpa-monitor:sha-<旧commit短sha>
# 或修改 compose 中 image 标签后
docker compose -f docker-compose.prod.yml up -d
```

## 8. 常用运维命令

```bash
docker compose down          # 停止
docker compose restart       # 重启
docker compose logs -f       # 实时日志
docker compose logs --tail=200
docker image prune -f        # 清理旧镜像
```

## 9. 端口与配置

默认端口映射：

```yaml
ports:
  - "18088:8088"
```

如需修改外部访问端口，例如改为 `28088`：

```yaml
ports:
  - "28088:8088"
```

修改后执行：

```bash
docker compose up -d --build
```

关键环境变量：

- `HOST=0.0.0.0`
- `PORT=8088`
- `DB_PATH=/app/data/stats.db`
