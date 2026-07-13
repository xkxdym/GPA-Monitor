# CPA-Monitor

CPA-Monitor 鏄竴涓熀浜?Python + SQLite 鐨勬湰鍦扮洃鎺у伐鍏凤紝鏀寔澶氶厤缃。缁熻銆佽秼鍔垮垎鏋愩€佽皟鐢ㄦ棩蹇楁煡鐪嬶紝骞跺凡闆嗘垚 CPA 鎺堟潈鏂囦欢鍚屾鍒?sub2api 鐨勮兘鍔涖€?
## 1. 鍚姩

```powershell
python server.py
```

榛樿璁块棶鍦板潃锛?
```text
http://127.0.0.1:8088
```

椤甸潰鍏ュ彛锛?- 鐩戞帶椤碉細`/`
- 閰嶇疆椤碉細`/config.html`
- 鎺堟潈鍚屾椤碉細`/auth-sync.html`

## 2. 涓昏鍔熻兘

- 澶氶厤缃。锛坧rofile锛夌鐞嗭細鏂板銆佺紪杈戙€佸垹闄ゃ€佸垏鎹?active
- 鎵嬪姩鎷夊彇鍜岃嚜鍔ㄥ畾鏃舵媺鍙栫粺璁℃暟鎹?- 妯″瀷銆佹潵婧愯处鍙枫€佺湡瀹炵敤鎴风淮搴︾粺璁?- 瓒嬪娍鍥俱€佸仴搴峰害銆佹棩蹇椾笌鏄庣粏鏌ヨ
- 杩囨湡鏁版嵁鑷姩娓呯悊涓庢墜鍔ㄦ竻鐞?- 鎺堟潈鍚屾锛氬皢 CPA `/auth-files` 鎺堟潈鏂囦欢杞崲骞舵帹閫佸埌 sub2api

## 3. 鎺堟潈鍚屾璇存槑

鍙傛暟閰嶇疆鍏ュ彛锛歚/config.html`锛堟巿鏉冨悓姝ラ厤缃級
- 鍚屾鍛ㄦ湡涓庣瓥鐣?- sub2 鍩虹鍦板潃涓庢巿鏉冿紙榛樿 `x-api-key`锛?- 鏄惁鏍￠獙 SSL銆佹渶澶у鐞嗘枃浠舵暟绛?- 鏂囦欢杩囨护绛栫暐锛堝惈鈥滀粎鍚屾宸插惎鐢ㄦ枃浠垛€濓級

鎵ц涓庢煡鐪嬪叆鍙ｏ細`/auth-sync.html`
- 鏌ョ湅鍚屾鐘舵€侊紙杩愯鐘舵€併€佷笅娆℃墽琛屻€佷笂娆＄粨鏋滐級
- 鎵嬪姩鑾峰彇 CPA 璁よ瘉鏂囦欢鍒楄〃骞剁紦瀛樺埌鏁版嵁搴?- 鍕鹃€夋枃浠跺悗鎵ц鈥滃悓姝ラ€変腑璁よ瘉鏂囦欢鈥?- 鏌ョ湅鍚屾璁板綍锛堟暟鎹簱淇濆瓨锛屼繚鐣?7 澶╄嚜鍔ㄦ竻鐞嗭級

## 4. API

### 鐩戞帶鐩稿叧
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

### 鎺堟潈鍚屾鐩稿叧
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

## 5. 鏁版嵁搴?
榛樿鏁版嵁搴撴枃浠讹細`stats.db`

涓昏琛細
- `app_config`锛氬叏灞€鐩戞帶閰嶇疆
- `auth_sync_settings`锛氭巿鏉冨悓姝ュ弬鏁伴厤缃?- `auth_sync_cached_files`锛氭渶杩戜竴娆¤幏鍙栫殑 CPA 璁よ瘉鏂囦欢缂撳瓨
- `auth_sync_file_records`锛氭枃浠剁骇鍚屾瀹¤璁板綍
- `auth_sync_records`锛氬懆鏈?鏂囦欢鍚屾浜嬩欢璁板綍锛? 澶╄嚜鍔ㄦ竻鐞嗭級
- `profiles`锛氱洃鎺ч厤缃。
- `usage_records`锛氳仛鍚堟槑缁?- `pull_snapshots`锛氳秼鍔垮揩鐓?- `pull_logs`锛氭媺鍙栨棩蹇?
## 6. Docker Compose 部署

### 本地构建

```powershell
docker compose up -d --build
```

### 使用 GitHub Actions 构建的镜像

推送到 `main` 后会自动构建并推送到 GHCR：

```text
ghcr.io/xkxdym/gpa-monitor:latest
```

生产环境可直接使用：

```powershell
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
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

更完整的说明见 [DEPLOY_DOCKER.md](DEPLOY_DOCKER.md)。