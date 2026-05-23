import json
import hashlib
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request
from sub_sync import SyncManager as AuthSyncManager
from sub_sync import build_default_raw_config as build_auth_sync_default_raw_config
from sub_sync import deep_merge_dict as deep_merge_auth_sync_raw


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "stats.db")))

HOST = os.getenv("HOST", "0.0.0.0")
try:
    PORT = int(os.getenv("PORT", "8088"))
except Exception:
    PORT = 8088

refresh_lock = threading.Lock()
scheduler_state = {
    "last_run_at": None,
    "last_ok": None,
    "last_message": "",
    "next_run_at": None,
}
auth_sync_manager = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pick_text(*vals, default="-"):
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def pick_int(*vals, default=0):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        try:
            return int(v)
        except Exception:
            continue
    return default


def normalize_key_user_map(value):
    if isinstance(value, dict):
        items = value.items()
    else:
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            items = parsed.items() if isinstance(parsed, dict) else []
        except Exception:
            pairs = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                sep = "=" if "=" in line else "," if "," in line else ":" if ":" in line else None
                if sep:
                    k, name = line.split(sep, 1)
                    pairs.append((k, name))
            items = pairs

    out = {}
    for k, name in items:
        key = str(k or "").strip()
        user = str(name or "").strip()
        if key and user:
            out[key] = user
    return out


def key_user_map_json(value):
    return json.dumps(normalize_key_user_map(value), ensure_ascii=False, sort_keys=True)


def key_user_entry_id(external_key):
    return hashlib.sha256(str(external_key or "").encode("utf-8")).hexdigest()


def redacted_key_user_entries(value):
    entries = []
    for idx, (key, user) in enumerate(normalize_key_user_map(value).items(), start=1):
        entries.append({
            "id": key_user_entry_id(key),
            "label": f"已保存配置 #{idx}",
            "user_name": user,
        })
    return entries


def apply_key_user_changes(current_value, payload):
    mapping = normalize_key_user_map(current_value)
    delete_ids = set()
    for v in payload.get("key_user_delete_ids", []) if isinstance(payload, dict) else []:
        s = str(v or "").strip()
        if s:
            delete_ids.add(s)
    if delete_ids:
        mapping = {k: v for k, v in mapping.items() if key_user_entry_id(k) not in delete_ids}

    additions = payload.get("key_user_additions", []) if isinstance(payload, dict) else []
    if isinstance(additions, list):
        for item in additions:
            if not isinstance(item, dict):
                continue
            key = str(item.get("external_key") or "").strip()
            user = str(item.get("user_name") or "").strip()
            if key and user:
                mapping[key] = user

    return json.dumps(mapping, ensure_ascii=False, sort_keys=True)


def user_name_for_key(profile, external_key):
    mapping = normalize_key_user_map(profile.get("key_user_map_json", "")) if profile else {}
    key = str(external_key or "").strip()
    return mapping.get(key, "-") if key else "-"


def row_matches_keyword(row, keyword, keys):
    kw = str(keyword or "").strip().lower()
    if not kw:
        return True
    return any(kw in str(row.get(k, "") or "").lower() for k in keys)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_has_column(conn, table_name, col_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(r["name"] == col_name for r in rows)


def ensure_column(conn, table_name, col_name, col_def):
    if not table_has_column(conn, table_name, col_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def migrate_auth_sync_settings_drop_record_file(conn):
    if not table_has_column(conn, "auth_sync_settings", "record_file"):
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS auth_sync_settings_v2 (
          id INTEGER PRIMARY KEY CHECK (id=1),
          interval_seconds INTEGER NOT NULL DEFAULT 300,
          sync_type TEXT NOT NULL DEFAULT 'expiry_policy',
          expiry_sync_days INTEGER NOT NULL DEFAULT 0,
          sync_without_expiry INTEGER NOT NULL DEFAULT 1,
          max_files_per_cycle INTEGER NOT NULL DEFAULT 0,
          sub_import_url TEXT NOT NULL DEFAULT '',
          sub_auth_header TEXT NOT NULL DEFAULT 'x-api-key',
          sub_authorization TEXT NOT NULL DEFAULT '',
          sub_timeout_seconds INTEGER NOT NULL DEFAULT 30,
          sub_verify_ssl INTEGER NOT NULL DEFAULT 1,
          sub_skip_default_group_bind INTEGER NOT NULL DEFAULT 1,
          delete_after_success INTEGER NOT NULL DEFAULT 1,
          dry_run INTEGER NOT NULL DEFAULT 0,
          default_concurrency INTEGER NOT NULL DEFAULT 1,
          default_priority INTEGER NOT NULL DEFAULT 0,
          name_template TEXT NOT NULL DEFAULT '{filename}',
          max_record_items INTEGER NOT NULL DEFAULT 2000,
          save_transformed_dir TEXT NOT NULL DEFAULT '',
          f_only_json INTEGER NOT NULL DEFAULT 1,
          f_allow_runtime_only INTEGER NOT NULL DEFAULT 0,
          f_allow_non_file_source INTEGER NOT NULL DEFAULT 0,
          f_require_enabled INTEGER NOT NULL DEFAULT 0,
          f_include_providers TEXT NOT NULL DEFAULT '',
          f_exclude_providers TEXT NOT NULL DEFAULT '',
          f_include_statuses TEXT NOT NULL DEFAULT '',
          f_exclude_statuses TEXT NOT NULL DEFAULT '',
          f_include_name_patterns TEXT NOT NULL DEFAULT '',
          f_exclude_name_patterns TEXT NOT NULL DEFAULT '',
          f_min_size_bytes INTEGER,
          f_max_size_bytes INTEGER
        );
        INSERT OR REPLACE INTO auth_sync_settings_v2 (
          id, interval_seconds, sync_type, expiry_sync_days, sync_without_expiry, max_files_per_cycle,
          sub_import_url, sub_auth_header, sub_authorization, sub_timeout_seconds, sub_verify_ssl, sub_skip_default_group_bind,
          delete_after_success, dry_run, default_concurrency, default_priority, name_template,
          max_record_items, save_transformed_dir,
          f_only_json, f_allow_runtime_only, f_allow_non_file_source, f_require_enabled,
          f_include_providers, f_exclude_providers, f_include_statuses, f_exclude_statuses,
          f_include_name_patterns, f_exclude_name_patterns, f_min_size_bytes, f_max_size_bytes
        )
        SELECT
          id, interval_seconds, sync_type, expiry_sync_days, sync_without_expiry, max_files_per_cycle,
          sub_import_url, sub_auth_header, sub_authorization, sub_timeout_seconds, sub_verify_ssl, sub_skip_default_group_bind,
          delete_after_success, dry_run, default_concurrency, default_priority, name_template,
          max_record_items, save_transformed_dir,
          f_only_json, f_allow_runtime_only, f_allow_non_file_source, f_require_enabled,
          f_include_providers, f_exclude_providers, f_include_statuses, f_exclude_statuses,
          f_include_name_patterns, f_exclude_name_patterns, f_min_size_bytes, f_max_size_bytes
        FROM auth_sync_settings;
        DROP TABLE auth_sync_settings;
        ALTER TABLE auth_sync_settings_v2 RENAME TO auth_sync_settings;
        INSERT OR IGNORE INTO auth_sync_settings (id) VALUES (1);
        """
    )


def init_db():
    conn = get_conn()
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS app_config (
              id INTEGER PRIMARY KEY CHECK (id=1),
              active_profile_id INTEGER,
              refresh_interval_sec INTEGER NOT NULL DEFAULT 60,
              auto_refresh_enabled INTEGER NOT NULL DEFAULT 0,
              lookback_hours INTEGER NOT NULL DEFAULT 24,
              record_limit INTEGER NOT NULL DEFAULT 300,
              retention_days INTEGER NOT NULL DEFAULT 30,
              auth_sync_config_json TEXT NOT NULL DEFAULT '{}'
            );
            INSERT OR IGNORE INTO app_config (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS auth_sync_settings (
              id INTEGER PRIMARY KEY CHECK (id=1),
              interval_seconds INTEGER NOT NULL DEFAULT 300,
              sync_type TEXT NOT NULL DEFAULT 'expiry_policy',
              expiry_sync_days INTEGER NOT NULL DEFAULT 0,
              sync_without_expiry INTEGER NOT NULL DEFAULT 1,
              max_files_per_cycle INTEGER NOT NULL DEFAULT 0,
              sub_import_url TEXT NOT NULL DEFAULT '',
              sub_auth_header TEXT NOT NULL DEFAULT 'x-api-key',
              sub_authorization TEXT NOT NULL DEFAULT '',
              sub_timeout_seconds INTEGER NOT NULL DEFAULT 30,
              sub_verify_ssl INTEGER NOT NULL DEFAULT 1,
              sub_skip_default_group_bind INTEGER NOT NULL DEFAULT 1,
              delete_after_success INTEGER NOT NULL DEFAULT 1,
              dry_run INTEGER NOT NULL DEFAULT 0,
              default_concurrency INTEGER NOT NULL DEFAULT 1,
              default_priority INTEGER NOT NULL DEFAULT 0,
              name_template TEXT NOT NULL DEFAULT '{filename}',
              max_record_items INTEGER NOT NULL DEFAULT 2000,
              save_transformed_dir TEXT NOT NULL DEFAULT '',
              f_only_json INTEGER NOT NULL DEFAULT 1,
              f_allow_runtime_only INTEGER NOT NULL DEFAULT 0,
              f_allow_non_file_source INTEGER NOT NULL DEFAULT 0,
              f_require_enabled INTEGER NOT NULL DEFAULT 0,
              f_include_providers TEXT NOT NULL DEFAULT '',
              f_exclude_providers TEXT NOT NULL DEFAULT '',
              f_include_statuses TEXT NOT NULL DEFAULT '',
              f_exclude_statuses TEXT NOT NULL DEFAULT '',
              f_include_name_patterns TEXT NOT NULL DEFAULT '',
              f_exclude_name_patterns TEXT NOT NULL DEFAULT '',
              f_min_size_bytes INTEGER,
              f_max_size_bytes INTEGER
            );
            INSERT OR IGNORE INTO auth_sync_settings (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              base_url TEXT NOT NULL DEFAULT '',
              token TEXT NOT NULL DEFAULT '',
              key_user_map_json TEXT NOT NULL DEFAULT '{}',
              endpoint_mode TEXT NOT NULL DEFAULT 'auto',
              queue_count INTEGER NOT NULL DEFAULT 300,
              is_enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              alias TEXT NOT NULL,
              source TEXT NOT NULL,
              auth_account TEXT NOT NULL DEFAULT '',
              external_key TEXT NOT NULL DEFAULT '',
              requests INTEGER NOT NULL,
              success INTEGER NOT NULL,
              failed INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL,
              output_tokens INTEGER NOT NULL,
              reasoning_tokens INTEGER NOT NULL,
              cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL,
              avg_latency_ms REAL NOT NULL,
              min_latency_ms REAL NOT NULL,
              max_latency_ms REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_fetched_at ON usage_records(fetched_at);

            CREATE TABLE IF NOT EXISTS pull_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              source_endpoint TEXT NOT NULL,
              model_groups INTEGER NOT NULL,
              total_requests INTEGER NOT NULL,
              total_success INTEGER NOT NULL,
              total_failed INTEGER NOT NULL,
              total_input_tokens INTEGER NOT NULL,
              total_output_tokens INTEGER NOT NULL,
              total_reasoning_tokens INTEGER NOT NULL,
              total_tokens INTEGER NOT NULL,
              avg_latency_ms REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_fetched_at ON pull_snapshots(fetched_at);

            CREATE TABLE IF NOT EXISTS pull_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              profile_id INTEGER NOT NULL DEFAULT 0,
              profile_name TEXT NOT NULL DEFAULT '',
              ok INTEGER NOT NULL,
              source_endpoint TEXT NOT NULL,
              message TEXT NOT NULL,
              trace_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_sync_file_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              file_name TEXT NOT NULL,
              trigger TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT '',
              synced_to_sub2 INTEGER NOT NULL DEFAULT 0,
              sync_time TEXT,
              message TEXT NOT NULL DEFAULT '',
              auth_json TEXT NOT NULL DEFAULT '',
              account_json TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sync_file_records_name_ts ON auth_sync_file_records(file_name, ts DESC);

            CREATE TABLE IF NOT EXISTS auth_sync_cached_files (
              file_name TEXT PRIMARY KEY,
              fetched_at TEXT NOT NULL,
              entry_json TEXT NOT NULL DEFAULT '{}',
              sync_enabled INTEGER NOT NULL DEFAULT 0,
              target_group_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sync_cached_files_fetched_at ON auth_sync_cached_files(fetched_at DESC);
            """
        )

        # Backward-compatible migration from old schema if old columns still exist.
        ensure_column(conn, "app_config", "active_profile_id", "INTEGER")
        ensure_column(conn, "app_config", "retention_days", "INTEGER NOT NULL DEFAULT 30")
        ensure_column(conn, "app_config", "refresh_interval_sec", "INTEGER NOT NULL DEFAULT 60")
        ensure_column(conn, "app_config", "auto_refresh_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "app_config", "lookback_hours", "INTEGER NOT NULL DEFAULT 24")
        ensure_column(conn, "app_config", "record_limit", "INTEGER NOT NULL DEFAULT 300")
        ensure_column(conn, "app_config", "auth_sync_config_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "profiles", "key_user_map_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "auth_sync_settings", "sub_auth_header", "TEXT NOT NULL DEFAULT 'x-api-key'")
        ensure_column(conn, "auth_sync_cached_files", "sync_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "auth_sync_cached_files", "target_group_ids", "TEXT NOT NULL DEFAULT '[]'")
        migrate_auth_sync_settings_drop_record_file(conn)
        conn.execute(
            """
            UPDATE app_config SET
              refresh_interval_sec = COALESCE(refresh_interval_sec, 60),
              auto_refresh_enabled = COALESCE(auto_refresh_enabled, 0),
              lookback_hours = COALESCE(lookback_hours, 24),
              record_limit = COALESCE(record_limit, 300),
              retention_days = COALESCE(retention_days, 30),
              auth_sync_config_json = COALESCE(auth_sync_config_json, '{}')
            WHERE id=1
            """
        )

        ensure_column(conn, "usage_records", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "usage_records", "profile_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_records", "auth_account", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_records", "external_key", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "usage_records", "cache_hit_tokens", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pull_snapshots", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pull_snapshots", "profile_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "pull_logs", "profile_id", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pull_logs", "profile_name", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_profile_model_source ON usage_records(profile_id, model, source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_profile_model_auth_key ON usage_records(profile_id, model, auth_account, external_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_profile_time ON pull_snapshots(profile_id, fetched_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_profile_time ON pull_logs(profile_id, fetched_at)")

        # Migrate legacy auth_sync_config_json -> auth_sync_settings (best-effort).
        try:
            legacy_raw = conn.execute("SELECT auth_sync_config_json FROM app_config WHERE id=1").fetchone()["auth_sync_config_json"]
            legacy_obj = json.loads(legacy_raw) if legacy_raw else {}
            if isinstance(legacy_obj, dict) and legacy_obj:
                current = conn.execute("SELECT * FROM auth_sync_settings WHERE id=1").fetchone()
                current = dict(current) if current else {}
                sync_legacy = legacy_obj.get("sync", {}) if isinstance(legacy_obj.get("sync"), dict) else {}
                sub_legacy = legacy_obj.get("sub2api", {}) if isinstance(legacy_obj.get("sub2api"), dict) else {}
                sub_legacy_headers = sub_legacy.get("headers", {}) if isinstance(sub_legacy.get("headers"), dict) else {}
                legacy_auth_header, legacy_auth_value = _extract_sub_auth_header_pair(sub_legacy_headers)
                f_legacy = sync_legacy.get("auth_file_filter", {}) if isinstance(sync_legacy.get("auth_file_filter"), dict) else {}
                upd = {
                    "interval_seconds": to_int(legacy_obj.get("interval_seconds"), current.get("interval_seconds", 300)),
                    "sync_type": str(sync_legacy.get("sync_type", current.get("sync_type", "expiry_policy"))),
                    "expiry_sync_days": to_int(sync_legacy.get("expiry_sync_days"), current.get("expiry_sync_days", 0)),
                    "sync_without_expiry": 1 if bool(sync_legacy.get("sync_without_expiry", current.get("sync_without_expiry", 1))) else 0,
                    "max_files_per_cycle": to_int(sync_legacy.get("max_files_per_cycle"), current.get("max_files_per_cycle", 0)),
                    "sub_import_url": str(sub_legacy.get("import_url", current.get("sub_import_url", "")) or ""),
                    "sub_auth_header": str(legacy_auth_header or current.get("sub_auth_header", "x-api-key") or "x-api-key"),
                    "sub_authorization": str(legacy_auth_value or current.get("sub_authorization", "") or ""),
                    "sub_timeout_seconds": to_int(sub_legacy.get("timeout_seconds"), current.get("sub_timeout_seconds", 30)),
                    "sub_verify_ssl": 1 if bool(sub_legacy.get("verify_ssl", current.get("sub_verify_ssl", 1))) else 0,
                    "sub_skip_default_group_bind": 1 if bool(sub_legacy.get("skip_default_group_bind", current.get("sub_skip_default_group_bind", 1))) else 0,
                    "delete_after_success": 1 if bool(sync_legacy.get("delete_after_success", current.get("delete_after_success", 1))) else 0,
                    "dry_run": 1 if bool(sync_legacy.get("dry_run", current.get("dry_run", 0))) else 0,
                    "default_concurrency": to_int(sync_legacy.get("default_concurrency"), current.get("default_concurrency", 1)),
                    "default_priority": to_int(sync_legacy.get("default_priority"), current.get("default_priority", 0)),
                    "name_template": str(sync_legacy.get("name_template", current.get("name_template", "{filename}")) or "{filename}"),
                    "max_record_items": to_int(sync_legacy.get("max_record_items"), current.get("max_record_items", 2000)),
                    "save_transformed_dir": str(sync_legacy.get("save_transformed_dir", current.get("save_transformed_dir", "")) or ""),
                    "f_only_json": 1 if bool(f_legacy.get("only_json", current.get("f_only_json", 1))) else 0,
                    "f_allow_runtime_only": 1 if bool(f_legacy.get("allow_runtime_only", current.get("f_allow_runtime_only", 0))) else 0,
                    "f_allow_non_file_source": 1 if bool(f_legacy.get("allow_non_file_source", current.get("f_allow_non_file_source", 0))) else 0,
                    "f_require_enabled": 1 if bool(f_legacy.get("require_enabled", current.get("f_require_enabled", 0))) else 0,
                    "f_include_providers": ",".join([str(x).strip() for x in f_legacy.get("include_providers", []) if str(x).strip()]),
                    "f_exclude_providers": ",".join([str(x).strip() for x in f_legacy.get("exclude_providers", []) if str(x).strip()]),
                    "f_include_statuses": ",".join([str(x).strip() for x in f_legacy.get("include_statuses", []) if str(x).strip()]),
                    "f_exclude_statuses": ",".join([str(x).strip() for x in f_legacy.get("exclude_statuses", []) if str(x).strip()]),
                    "f_include_name_patterns": ",".join([str(x).strip() for x in f_legacy.get("include_name_patterns", []) if str(x).strip()]),
                    "f_exclude_name_patterns": ",".join([str(x).strip() for x in f_legacy.get("exclude_name_patterns", []) if str(x).strip()]),
                    "f_min_size_bytes": f_legacy.get("min_size_bytes"),
                    "f_max_size_bytes": f_legacy.get("max_size_bytes"),
                }
                conn.execute(
                    """
                    UPDATE auth_sync_settings SET
                      interval_seconds=?, sync_type=?, expiry_sync_days=?, sync_without_expiry=?, max_files_per_cycle=?,
                      sub_import_url=?, sub_auth_header=?, sub_authorization=?, sub_timeout_seconds=?, sub_verify_ssl=?, sub_skip_default_group_bind=?,
                      delete_after_success=?, dry_run=?, default_concurrency=?, default_priority=?, name_template=?,
                      max_record_items=?, save_transformed_dir=?,
                      f_only_json=?, f_allow_runtime_only=?, f_allow_non_file_source=?, f_require_enabled=?,
                      f_include_providers=?, f_exclude_providers=?, f_include_statuses=?, f_exclude_statuses=?,
                      f_include_name_patterns=?, f_exclude_name_patterns=?, f_min_size_bytes=?, f_max_size_bytes=?
                    WHERE id=1
                    """,
                    (
                        upd["interval_seconds"], upd["sync_type"], upd["expiry_sync_days"], upd["sync_without_expiry"], upd["max_files_per_cycle"],
                        upd["sub_import_url"], upd["sub_auth_header"], upd["sub_authorization"], upd["sub_timeout_seconds"], upd["sub_verify_ssl"], upd["sub_skip_default_group_bind"],
                        upd["delete_after_success"], upd["dry_run"], upd["default_concurrency"], upd["default_priority"], upd["name_template"],
                        upd["max_record_items"], upd["save_transformed_dir"],
                        upd["f_only_json"], upd["f_allow_runtime_only"], upd["f_allow_non_file_source"], upd["f_require_enabled"],
                        upd["f_include_providers"], upd["f_exclude_providers"], upd["f_include_statuses"], upd["f_exclude_statuses"],
                        upd["f_include_name_patterns"], upd["f_exclude_name_patterns"], upd["f_min_size_bytes"], upd["f_max_size_bytes"],
                    ),
                )
        except Exception:
            pass

        # Bootstrap profile from legacy app_config fields if profiles empty.
        profile_count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
        if profile_count == 0:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(app_config)").fetchall()}
            base_url = ""
            token = ""
            endpoint_mode = "auto"
            queue_count = 300
            if "base_url" in cols:
                row = conn.execute("SELECT base_url, token, endpoint_mode, queue_count FROM app_config WHERE id=1").fetchone()
                if row:
                    base_url = str(row["base_url"] or "").strip()
                    token = str(row["token"] or "").strip()
                    mode = str(row["endpoint_mode"] or "auto").strip().lower()
                    endpoint_mode = mode if mode in ("auto", "queue", "legacy") else "auto"
                    queue_count = max(1, min(10000, to_int(row["queue_count"], 300)))
            now = now_iso()
            conn.execute(
                """
                INSERT INTO profiles (name, base_url, token, key_user_map_json, endpoint_mode, queue_count, is_enabled, created_at, updated_at)
                VALUES (?, ?, ?, '{}', ?, ?, 1, ?, ?)
                """,
                ("default", base_url, token, endpoint_mode, queue_count, now, now),
            )

        # Ensure active profile exists.
        active = conn.execute("SELECT active_profile_id FROM app_config WHERE id=1").fetchone()["active_profile_id"]
        if not active:
            first = conn.execute("SELECT id FROM profiles ORDER BY id ASC LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (first["id"],))

        conn.commit()
    finally:
        conn.close()


def read_config():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM app_config WHERE id=1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def write_config(payload):
    cfg = read_config()
    merged = dict(cfg)
    for k in ("active_profile_id", "refresh_interval_sec", "auto_refresh_enabled", "lookback_hours", "record_limit", "retention_days"):
        if k in payload:
            merged[k] = payload[k]

    merged["active_profile_id"] = to_int(merged.get("active_profile_id"), 0) or None
    merged["refresh_interval_sec"] = max(5, min(86400, to_int(merged.get("refresh_interval_sec", 60), 60)))
    merged["auto_refresh_enabled"] = 1 if to_int(merged.get("auto_refresh_enabled", 0), 0) else 0
    merged["lookback_hours"] = max(1, min(24 * 90, to_int(merged.get("lookback_hours", 24), 24)))
    merged["record_limit"] = max(10, min(5000, to_int(merged.get("record_limit", 300), 300)))
    merged["retention_days"] = max(1, min(3650, to_int(merged.get("retention_days", 30), 30)))

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE app_config SET
              active_profile_id=?,
              refresh_interval_sec=?,
              auto_refresh_enabled=?,
              lookback_hours=?,
              record_limit=?,
              retention_days=?
            WHERE id=1
            """,
            (
                merged["active_profile_id"],
                merged["refresh_interval_sec"],
                merged["auto_refresh_enabled"],
                merged["lookback_hours"],
                merged["record_limit"],
                merged["retention_days"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return merged


def _csv_to_list(text):
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _list_to_csv(value):
    if isinstance(value, list):
        return ",".join([str(x).strip() for x in value if str(x).strip()])
    return ""


def _to_opt_int(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _sanitize_sync_type(v):
    s = str(v or "").strip().lower()
    return s if s in {"all", "expiry_policy", "expired_only", "no_expiry_only"} else "expiry_policy"


def _extract_sub_auth_header_pair(headers_obj):
    if not isinstance(headers_obj, dict):
        return "x-api-key", ""
    for k, v in headers_obj.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if key.lower() == "x-api-key" and val:
            return "x-api-key", val
    for k, v in headers_obj.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if key.lower() == "authorization" and val:
            return "Authorization", val
    for k, v in headers_obj.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if key and val:
            return key, val
    return "x-api-key", ""


def _build_sub_auth_headers_from_row(row_dict):
    header_name = str((row_dict or {}).get("sub_auth_header", "") or "").strip() or "x-api-key"
    header_value = str((row_dict or {}).get("sub_authorization", "") or "").strip()
    if not header_value:
        return {}
    return {header_name: header_value}


def _mask_secret_text(secret: str) -> str:
    s = str(secret or "")
    n = len(s)
    if n <= 0:
        return ""
    if n <= 4:
        return "*" * n
    return f"{s[:2]}{'*' * (n - 4)}{s[-2:]}"


def _build_auth_sync_public_config(raw_cfg: dict) -> dict:
    cfg = deep_merge_auth_sync_raw(build_auth_sync_default_raw_config(), raw_cfg or {})
    sub = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
    headers = sub.get("headers") if isinstance(sub.get("headers"), dict) else {}
    auth_header, auth_value = _extract_sub_auth_header_pair(headers)
    auth_set = bool(str(auth_value or "").strip())
    mode = "raw"
    if auth_header.lower() == "x-api-key":
        mode = "x_api_key"
    elif auth_header.lower() == "authorization":
        mode = "bearer" if str(auth_value or "").strip().lower().startswith("bearer ") else "authorization_raw"
    sub["auth_key_set"] = auth_set
    sub["auth_header"] = auth_header or "x-api-key"
    sub["auth_mode"] = mode
    sub["auth_key_masked"] = _mask_secret_text(auth_value) if auth_set else ""
    sub["headers"] = {sub["auth_header"]: sub["auth_key_masked"]} if auth_set else {}
    cfg["sub2api"] = sub
    return cfg


def _row_to_auth_sync_raw(row):
    r = dict(row or {})
    out = deep_merge_auth_sync_raw(
        build_auth_sync_default_raw_config(),
        {
            "interval_seconds": max(1, to_int(r.get("interval_seconds"), 300)),
            "sub2api": {
                "import_url": str(r.get("sub_import_url", "") or ""),
                "timeout_seconds": max(1, to_int(r.get("sub_timeout_seconds"), 30)),
                "verify_ssl": bool(to_int(r.get("sub_verify_ssl", 1), 1)),
                "skip_default_group_bind": bool(to_int(r.get("sub_skip_default_group_bind", 1), 1)),
                "headers": _build_sub_auth_headers_from_row(r),
            },
            "sync": {
                "delete_after_success": False,
                "dry_run": False,
                "max_files_per_cycle": max(0, to_int(r.get("max_files_per_cycle", 0), 0)),
                "default_concurrency": 1,
                "default_priority": 0,
                "name_template": "{filename}",
                "save_transformed_dir": "",
                "max_record_items": 2000,
                "sync_type": _sanitize_sync_type(r.get("sync_type")),
                "expiry_sync_days": to_int(r.get("expiry_sync_days", 0), 0),
                "sync_without_expiry": True,
                "auth_file_filter": {
                    "only_json": bool(to_int(r.get("f_only_json", 1), 1)),
                    "allow_runtime_only": bool(to_int(r.get("f_allow_runtime_only", 0), 0)),
                    "allow_non_file_source": bool(to_int(r.get("f_allow_non_file_source", 0), 0)),
                    "require_enabled": bool(to_int(r.get("f_require_enabled", 0), 0)),
                    "include_providers": _csv_to_list(r.get("f_include_providers")),
                    "exclude_providers": _csv_to_list(r.get("f_exclude_providers")),
                    "include_statuses": _csv_to_list(r.get("f_include_statuses")),
                    "exclude_statuses": _csv_to_list(r.get("f_exclude_statuses")),
                    "include_name_patterns": _csv_to_list(r.get("f_include_name_patterns")),
                    "exclude_name_patterns": _csv_to_list(r.get("f_exclude_name_patterns")),
                    "min_size_bytes": _to_opt_int(r.get("f_min_size_bytes")),
                    "max_size_bytes": _to_opt_int(r.get("f_max_size_bytes")),
                },
            },
        },
    )
    sub = out.get("sub2api", {}) if isinstance(out.get("sub2api"), dict) else {}
    sub["headers"] = _build_sub_auth_headers_from_row(r)
    out["sub2api"] = sub
    return out


def get_auth_sync_raw_config():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM auth_sync_settings WHERE id=1").fetchone()
        return _row_to_auth_sync_raw(row)
    finally:
        conn.close()


def save_auth_sync_raw_config(raw):
    merged = deep_merge_auth_sync_raw(build_auth_sync_default_raw_config(), raw or {})
    sync = merged.get("sync", {}) if isinstance(merged.get("sync"), dict) else {}
    sub = merged.get("sub2api", {}) if isinstance(merged.get("sub2api"), dict) else {}
    sub_headers = sub.get("headers", {}) if isinstance(sub.get("headers"), dict) else {}
    sub_auth_header, sub_auth_value = _extract_sub_auth_header_pair(sub_headers)
    clear_auth = bool(sub.get("clear_auth", False))
    f = sync.get("auth_file_filter", {}) if isinstance(sync.get("auth_file_filter"), dict) else {}
    conn = get_conn()
    try:
        current = conn.execute("SELECT sub_auth_header, sub_authorization FROM auth_sync_settings WHERE id=1").fetchone()
        current_header = str((dict(current or {}) if current else {}).get("sub_auth_header", "x-api-key") or "x-api-key").strip() or "x-api-key"
        current_value = str((dict(current or {}) if current else {}).get("sub_authorization", "") or "").strip()
        if clear_auth:
            sub_auth_header = sub_auth_header or current_header
            sub_auth_value = ""
        elif not str(sub_auth_value or "").strip():
            sub_auth_header = current_header
            sub_auth_value = current_value
        conn.execute(
            """
            UPDATE auth_sync_settings SET
              interval_seconds=?, sync_type=?, expiry_sync_days=?, sync_without_expiry=?, max_files_per_cycle=?,
              sub_import_url=?, sub_auth_header=?, sub_authorization=?, sub_timeout_seconds=?, sub_verify_ssl=?, sub_skip_default_group_bind=?,
              delete_after_success=?, dry_run=?, default_concurrency=?, default_priority=?, name_template=?,
              max_record_items=?, save_transformed_dir=?,
              f_only_json=?, f_allow_runtime_only=?, f_allow_non_file_source=?, f_require_enabled=?,
              f_include_providers=?, f_exclude_providers=?, f_include_statuses=?, f_exclude_statuses=?,
              f_include_name_patterns=?, f_exclude_name_patterns=?, f_min_size_bytes=?, f_max_size_bytes=?
            WHERE id=1
            """,
            (
                max(1, to_int(merged.get("interval_seconds", 300), 300)),
                _sanitize_sync_type(sync.get("sync_type", "expiry_policy")),
                to_int(sync.get("expiry_sync_days", 0), 0),
                1,
                max(0, to_int(sync.get("max_files_per_cycle", 0), 0)),
                str(sub.get("import_url", "") or ""),
                str(sub_auth_header or "x-api-key"),
                str(sub_auth_value or ""),
                max(1, to_int(sub.get("timeout_seconds", 30), 30)),
                1 if bool(sub.get("verify_ssl", True)) else 0,
                1 if bool(sub.get("skip_default_group_bind", True)) else 0,
                0,
                0,
                1,
                0,
                "{filename}",
                2000,
                "",
                1 if bool(f.get("only_json", True)) else 0,
                1 if bool(f.get("allow_runtime_only", False)) else 0,
                1 if bool(f.get("allow_non_file_source", False)) else 0,
                1 if bool(f.get("require_enabled", False)) else 0,
                _list_to_csv(f.get("include_providers")),
                _list_to_csv(f.get("exclude_providers")),
                _list_to_csv(f.get("include_statuses")),
                _list_to_csv(f.get("exclude_statuses")),
                _list_to_csv(f.get("include_name_patterns")),
                _list_to_csv(f.get("exclude_name_patterns")),
                _to_opt_int(f.get("min_size_bytes")),
                _to_opt_int(f.get("max_size_bytes")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_auth_sync_raw_config()


def apply_auth_sync_override_preserving_auth(override):
    if not isinstance(override, dict):
        return {}
    out = dict(override)
    sub = out.get("sub2api")
    if not isinstance(sub, dict):
        return out
    if bool(sub.get("clear_auth", False)):
        return out
    headers = sub.get("headers") if isinstance(sub.get("headers"), dict) else {}
    _, auth_value = _extract_sub_auth_header_pair(headers)
    if str(auth_value or "").strip():
        return out
    current = get_auth_sync_raw_config()
    current_sub = current.get("sub2api") if isinstance(current.get("sub2api"), dict) else {}
    current_headers = current_sub.get("headers") if isinstance(current_sub.get("headers"), dict) else {}
    patched_sub = dict(sub)
    patched_sub["headers"] = dict(current_headers)
    out["sub2api"] = patched_sub
    return out


def get_active_cpa_sync_source():
    profile = get_active_profile()
    if not profile:
        raise RuntimeError("active profile not found")
    raw_base = str(profile.get("base_url", "") or "").strip().rstrip("/")
    token = str(profile.get("token", "") or "").strip()
    if not raw_base:
        raise RuntimeError("active profile base_url is empty")
    if raw_base.endswith("/v0/management"):
        mgmt_base = raw_base
    else:
        mgmt_base = f"{raw_base}/v0/management"
    return {
        "profile_id": profile.get("id"),
        "profile_name": profile.get("name"),
        "base_url": mgmt_base,
        "management_key": token,
    }


def persist_auth_sync_file_record(item):
    payload = dict(item or {})
    file_name = str(payload.get("file_name", "") or "").strip()
    if not file_name:
        return
    ts = str(payload.get("ts", "") or "").strip() or now_iso()
    trigger = str(payload.get("trigger", "") or "").strip()
    status = str(payload.get("status", "") or "").strip()
    synced_to_sub2 = 1 if payload.get("synced_to_sub2") else 0
    sync_time = payload.get("sync_time")
    sync_time = str(sync_time).strip() if sync_time else None
    message = str(payload.get("message", "") or "")
    auth_json = json.dumps(payload.get("auth_doc"), ensure_ascii=False) if isinstance(payload.get("auth_doc"), dict) else ""
    account_json = json.dumps(payload.get("account"), ensure_ascii=False) if isinstance(payload.get("account"), dict) else ""
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO auth_sync_file_records (
              ts, file_name, trigger, status, synced_to_sub2, sync_time, message, auth_json, account_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, file_name, trigger, status, synced_to_sub2, sync_time, message, auth_json, account_json),
        )
        conn.commit()
    finally:
        conn.close()


def get_auth_sync_file_latest_map(file_names):
    names = [str(x or "").strip() for x in (file_names or []) if str(x or "").strip()]
    if not names:
        return {}
    placeholders = ",".join(["?"] * len(names))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT r.*
            FROM auth_sync_file_records r
            INNER JOIN (
              SELECT file_name, MAX(id) AS max_id
              FROM auth_sync_file_records
              WHERE file_name IN ({placeholders})
              GROUP BY file_name
            ) t ON r.id = t.max_id
            """,
            names,
        ).fetchall()
        out = {}
        for row in rows:
            d = dict(row)
            out[d["file_name"]] = {
                "synced_to_sub2": bool(d.get("synced_to_sub2")),
                "sync_time": d.get("sync_time"),
                "last_status": d.get("status"),
                "last_message": d.get("message"),
                "record_ts": d.get("ts"),
                "has_saved_auth_json": 1 if str(d.get("auth_json") or "").strip() else 0,
                "has_saved_account_json": 1 if str(d.get("account_json") or "").strip() else 0,
            }
        return out
    finally:
        conn.close()


def get_auth_sync_file_records_page(page=1, page_size=50):
    p = max(1, to_int(page, 1))
    ps = max(1, min(2000, to_int(page_size, 50)))
    offset = (p - 1) * ps
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(1) AS c FROM auth_sync_file_records").fetchone()["c"]
        rows = conn.execute(
            """
            SELECT id, ts, file_name, trigger, status, synced_to_sub2, sync_time, message
            FROM auth_sync_file_records
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (ps, max(0, offset)),
        ).fetchall()
        items = []
        for row in rows:
            d = dict(row)
            d["synced_to_sub2"] = bool(d.get("synced_to_sub2"))
            items.append(d)
        total_pages = max(1, (int(total) + ps - 1) // ps) if int(total) > 0 else 1
        if p > total_pages:
            p = total_pages
        return {
            "items": items,
            "page": int(p),
            "page_size": int(ps),
            "total": int(total),
            "total_pages": int(total_pages),
        }
    finally:
        conn.close()


def save_auth_sync_cached_files(rows):
    fetched_at = now_iso()
    clean_rows = []
    conn = get_conn()
    try:
        old_rows = conn.execute("SELECT file_name, sync_enabled, target_group_ids FROM auth_sync_cached_files").fetchall()
        enabled_map = {str(r["file_name"] or "").strip(): bool(to_int(r["sync_enabled"], 0)) for r in old_rows}
        group_ids_map = {}
        for r in old_rows:
            name = str(r["file_name"] or "").strip()
            if not name:
                continue
            try:
                arr = json.loads(str(r["target_group_ids"] or "[]"))
            except Exception:
                arr = []
            clean = []
            if isinstance(arr, list):
                for v in arr:
                    iv = to_int(v, 0)
                    if iv > 0 and iv not in clean:
                        clean.append(iv)
            group_ids_map[name] = clean
    finally:
        conn.close()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "") or "").strip()
        if not name:
            continue
        payload = dict(row)
        payload["name"] = name
        payload["sync_enabled"] = bool(enabled_map.get(name, False))
        payload["target_group_ids"] = group_ids_map.get(name, [])
        clean_rows.append(payload)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM auth_sync_cached_files")
        if clean_rows:
            conn.executemany(
                "INSERT INTO auth_sync_cached_files (file_name, fetched_at, entry_json, sync_enabled, target_group_ids) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        r["name"],
                        fetched_at,
                        json.dumps(r, ensure_ascii=False),
                        1 if r.get("sync_enabled") else 0,
                        json.dumps(r.get("target_group_ids", []), ensure_ascii=False),
                    )
                    for r in clean_rows
                ],
            )
        conn.commit()
    finally:
        conn.close()
    return fetched_at


def get_auth_sync_cached_files():
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT file_name, fetched_at, entry_json, sync_enabled, target_group_ids
            FROM auth_sync_cached_files
            ORDER BY LOWER(file_name) ASC
            """
        ).fetchall()
        out = []
        last_fetched_at = None
        for row in rows:
            fetched_at = str(row["fetched_at"] or "").strip()
            if fetched_at and (not last_fetched_at or fetched_at > last_fetched_at):
                last_fetched_at = fetched_at
            try:
                item = json.loads(row["entry_json"] or "{}")
            except Exception:
                item = {}
            if not isinstance(item, dict):
                item = {}
            item["name"] = str(row["file_name"] or "").strip()
            item["cached_fetched_at"] = fetched_at
            item["sync_enabled"] = bool(to_int(row["sync_enabled"], 0))
            try:
                gid_arr = json.loads(str(row["target_group_ids"] or "[]"))
            except Exception:
                gid_arr = []
            clean_gids = []
            if isinstance(gid_arr, list):
                for v in gid_arr:
                    iv = to_int(v, 0)
                    if iv > 0 and iv not in clean_gids:
                        clean_gids.append(iv)
            item["target_group_ids"] = clean_gids
            out.append(item)
        return {"last_fetched_at": last_fetched_at, "rows": out}
    finally:
        conn.close()


def set_auth_sync_cached_files_enabled(file_names, enabled):
    names = [str(x or "").strip() for x in (file_names or []) if str(x or "").strip()]
    if not names:
        return 0
    placeholders = ",".join(["?"] * len(names))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT file_name, entry_json, target_group_ids FROM auth_sync_cached_files WHERE file_name IN ({placeholders})",
            names,
        ).fetchall()
        count = 0
        for row in rows:
            file_name = str(row["file_name"] or "").strip()
            try:
                item = json.loads(row["entry_json"] or "{}")
            except Exception:
                item = {}
            if not isinstance(item, dict):
                item = {}
            item["name"] = file_name
            item["sync_enabled"] = bool(enabled)
            try:
                gid_arr = json.loads(str(row["target_group_ids"] or "[]"))
            except Exception:
                gid_arr = []
            clean_gids = []
            if isinstance(gid_arr, list):
                for v in gid_arr:
                    iv = to_int(v, 0)
                    if iv > 0 and iv not in clean_gids:
                        clean_gids.append(iv)
            item["target_group_ids"] = clean_gids
            conn.execute(
                "UPDATE auth_sync_cached_files SET entry_json=?, sync_enabled=? WHERE file_name=?",
                (json.dumps(item, ensure_ascii=False), 1 if enabled else 0, file_name),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def get_auth_sync_cached_enabled_name_set():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT file_name FROM auth_sync_cached_files WHERE sync_enabled=1").fetchall()
        return {str(r["file_name"] or "").strip() for r in rows if str(r["file_name"] or "").strip()}
    finally:
        conn.close()


def get_auth_sync_cached_entry_json_map(file_names):
    names = [str(x or "").strip() for x in (file_names or []) if str(x or "").strip()]
    if not names:
        return {}
    placeholders = ",".join(["?"] * len(names))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT file_name, fetched_at, entry_json, target_group_ids
            FROM auth_sync_cached_files
            WHERE file_name IN ({placeholders})
            ORDER BY LOWER(file_name) ASC
            """,
            names,
        ).fetchall()
        out = {}
        for row in rows:
            file_name = str(row["file_name"] or "").strip()
            if not file_name:
                continue
            raw = row["entry_json"] or "{}"
            try:
                entry_json = json.loads(raw)
            except Exception:
                entry_json = raw
            if not isinstance(entry_json, dict):
                entry_json = {}
            try:
                gid_arr = json.loads(str(row["target_group_ids"] or "[]"))
            except Exception:
                gid_arr = []
            clean_gids = []
            if isinstance(gid_arr, list):
                for v in gid_arr:
                    iv = to_int(v, 0)
                    if iv > 0 and iv not in clean_gids:
                        clean_gids.append(iv)
            entry_json["target_group_ids"] = clean_gids
            out[file_name] = {
                "file": file_name,
                "fetched_at": str(row["fetched_at"] or "").strip() or None,
                "entry_json": entry_json,
            }
        return out
    finally:
        conn.close()


def _normalize_group_ids(value):
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        iv = to_int(v, 0)
        if iv > 0 and iv not in out:
            out.append(iv)
    return out


def set_auth_sync_cached_files_groups(file_names, target_group_ids):
    names = [str(x or "").strip() for x in (file_names or []) if str(x or "").strip()]
    if not names:
        return 0
    gids = _normalize_group_ids(target_group_ids)
    placeholders = ",".join(["?"] * len(names))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT file_name, entry_json, sync_enabled FROM auth_sync_cached_files WHERE file_name IN ({placeholders})",
            names,
        ).fetchall()
        count = 0
        for row in rows:
            file_name = str(row["file_name"] or "").strip()
            try:
                item = json.loads(row["entry_json"] or "{}")
            except Exception:
                item = {}
            if not isinstance(item, dict):
                item = {}
            item["name"] = file_name
            item["sync_enabled"] = bool(to_int(row["sync_enabled"], 0))
            item["target_group_ids"] = list(gids)
            conn.execute(
                "UPDATE auth_sync_cached_files SET entry_json=?, target_group_ids=? WHERE file_name=?",
                (json.dumps(item, ensure_ascii=False), json.dumps(gids, ensure_ascii=False), file_name),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def set_auth_sync_cached_files_groups_by_map(group_ids_by_file):
    if not isinstance(group_ids_by_file, dict) or not group_ids_by_file:
        return {"updated": 0, "files": []}
    normalized = {}
    for k, v in group_ids_by_file.items():
        name = str(k or "").strip()
        if not name:
            continue
        normalized[name] = _normalize_group_ids(v if isinstance(v, list) else [])
    if not normalized:
        return {"updated": 0, "files": []}
    names = list(normalized.keys())
    placeholders = ",".join(["?"] * len(names))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT file_name, entry_json, sync_enabled FROM auth_sync_cached_files WHERE file_name IN ({placeholders})",
            names,
        ).fetchall()
        count = 0
        updated_files = []
        for row in rows:
            file_name = str(row["file_name"] or "").strip()
            if not file_name:
                continue
            gids = normalized.get(file_name, [])
            try:
                item = json.loads(row["entry_json"] or "{}")
            except Exception:
                item = {}
            if not isinstance(item, dict):
                item = {}
            item["name"] = file_name
            item["sync_enabled"] = bool(to_int(row["sync_enabled"], 0))
            item["target_group_ids"] = list(gids)
            conn.execute(
                "UPDATE auth_sync_cached_files SET entry_json=?, target_group_ids=? WHERE file_name=?",
                (json.dumps(item, ensure_ascii=False), json.dumps(gids, ensure_ascii=False), file_name),
            )
            count += 1
            updated_files.append(file_name)
        conn.commit()
        return {"updated": count, "files": updated_files}
    finally:
        conn.close()


def merge_auth_sync_rows_with_latest(rows):
    out = [dict(x) for x in (rows or []) if isinstance(x, dict)]
    latest = get_auth_sync_file_latest_map([x.get("name") for x in out])
    for row in out:
        info = latest.get(str(row.get("name", "")).strip())
        if info:
            row.update(info)
    return out


def get_auth_sync_manager():
    if auth_sync_manager is None:
        raise RuntimeError("auth sync manager not initialized")
    return auth_sync_manager


def init_auth_sync_manager():
    global auth_sync_manager
    raw_cfg = get_auth_sync_raw_config()
    auth_sync_manager = AuthSyncManager(
        root_dir=ROOT,
        raw_config=raw_cfg,
        save_raw_config=save_auth_sync_raw_config,
        cpa_source_getter=get_active_cpa_sync_source,
        file_audit_hook=persist_auth_sync_file_record,
        cached_enabled_name_getter=get_auth_sync_cached_enabled_name_set,
        cached_entry_getter=get_auth_sync_cached_entry_json_map,
        record_db_path=str(DB_PATH),
    )
    auth_sync_manager.start()
    return auth_sync_manager


def sanitize_profile_payload(payload):
    p = dict(payload or {})
    p["name"] = str(p.get("name", "")).strip()
    p["base_url"] = str(p.get("base_url", "")).strip().rstrip("/")
    if "token" in p:
        p["token"] = str(p.get("token", "")).strip()
    p["key_user_map_json"] = key_user_map_json(p.get("key_user_map_json", p.get("key_user_map", "")))
    mode = str(p.get("endpoint_mode", "auto")).strip().lower()
    p["endpoint_mode"] = mode if mode in ("auto", "queue", "legacy") else "auto"
    p["queue_count"] = max(1, min(10000, to_int(p.get("queue_count", 300), 300)))
    p["is_enabled"] = 1 if to_int(p.get("is_enabled", 1), 1) else 0
    return p


def list_profiles():
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, name, base_url, key_user_map_json, endpoint_mode, queue_count, is_enabled, created_at, updated_at,
                   CASE WHEN token <> '' THEN 1 ELSE 0 END AS has_token
            FROM profiles
            ORDER BY id ASC
            """
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            key_user_map = item.pop("key_user_map_json", "")
            item["key_user_entries"] = redacted_key_user_entries(key_user_map)
            item["key_user_count"] = len(item["key_user_entries"])
            item["has_key_user_map"] = 1 if item["key_user_count"] > 0 else 0
            out.append(item)
        return out
    finally:
        conn.close()


def get_profile(profile_id):
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT id, name, base_url, token, key_user_map_json, endpoint_mode, queue_count, is_enabled, created_at, updated_at
            FROM profiles WHERE id=?
            """,
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_profile():
    cfg = read_config()
    active_id = cfg.get("active_profile_id")
    profile = get_profile(active_id) if active_id else None
    if profile:
        return profile
    profiles = list_profiles()
    if profiles:
        set_active_profile(profiles[0]["id"])
        return get_profile(profiles[0]["id"])
    return None


def upsert_profile(payload):
    p = sanitize_profile_payload(payload)
    if not p["name"]:
        raise RuntimeError("profile name is required")

    pid = to_int(payload.get("id"), 0) if isinstance(payload, dict) else 0
    now = now_iso()
    conn = get_conn()
    try:
        if pid > 0:
            old = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            if not old:
                raise RuntimeError("profile not found")
            token_to_save = old["token"]
            if "token" in payload and p.get("token", ""):
                token_to_save = p["token"]
            elif payload.get("force_clear_token"):
                token_to_save = ""
            key_user_map_to_save = old["key_user_map_json"]
            if (("key_user_map_json" in payload and str(payload.get("key_user_map_json") or "").strip()) or
                    ("key_user_map" in payload and str(payload.get("key_user_map") or "").strip())):
                key_user_map_to_save = p["key_user_map_json"]
            if "key_user_additions" in payload or "key_user_delete_ids" in payload:
                key_user_map_to_save = apply_key_user_changes(key_user_map_to_save, payload)

            conn.execute(
                """
                UPDATE profiles SET
                  name=?, base_url=?, token=?, key_user_map_json=?, endpoint_mode=?, queue_count=?, is_enabled=?, updated_at=?
                WHERE id=?
                """,
                (p["name"], p["base_url"], token_to_save, key_user_map_to_save, p["endpoint_mode"], p["queue_count"], p["is_enabled"], now, pid),
            )
            conn.commit()
            return pid

        token = p.get("token", "")
        key_user_map = p["key_user_map_json"]
        if "key_user_additions" in payload or "key_user_delete_ids" in payload:
            key_user_map = apply_key_user_changes("{}", payload)
        conn.execute(
            """
            INSERT INTO profiles (name, base_url, token, key_user_map_json, endpoint_mode, queue_count, is_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (p["name"], p["base_url"], token, key_user_map, p["endpoint_mode"], p["queue_count"], p["is_enabled"], now, now),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return new_id
    except sqlite3.IntegrityError:
        raise RuntimeError("profile name already exists")
    finally:
        conn.close()


def set_active_profile(profile_id):
    pid = to_int(profile_id, 0)
    if pid <= 0:
        raise RuntimeError("invalid profile id")
    p = get_profile(pid)
    if not p:
        raise RuntimeError("profile not found")
    conn = get_conn()
    try:
        conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (pid,))
        conn.commit()
    finally:
        conn.close()
    return pid


def delete_profile(profile_id):
    pid = to_int(profile_id, 0)
    if pid <= 0:
        raise RuntimeError("invalid profile id")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            raise RuntimeError("profile not found")
        count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
        if count <= 1:
            raise RuntimeError("cannot delete last profile")

        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
        active_id = conn.execute("SELECT active_profile_id FROM app_config WHERE id=1").fetchone()["active_profile_id"]
        if active_id == pid:
            first = conn.execute("SELECT id FROM profiles ORDER BY id ASC LIMIT 1").fetchone()
            conn.execute("UPDATE app_config SET active_profile_id=? WHERE id=1", (first["id"],))
        conn.commit()
    finally:
        conn.close()


def request_json(url, token, label, traces):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, headers=headers, method="GET")
    t0 = time.time()
    try:
        with request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        status = e.code
        ms = int((time.time() - t0) * 1000)
        traces.append({"label": label, "url": url, "ok": False, "status": status, "ms": ms, "preview": " ".join(body[:220].split())})
        raise RuntimeError(f"{label} {status}: {body[:200]}")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        traces.append({"label": label, "url": url, "ok": False, "status": "FETCH_FAIL", "ms": ms, "preview": str(e)})
        raise RuntimeError(f"{label} request failed: {e}")

    ms = int((time.time() - t0) * 1000)
    traces.append({"label": label, "url": url, "ok": 200 <= status < 300, "status": status, "ms": ms, "preview": " ".join(body[:220].split())})
    try:
        return json.loads(body) if body else None
    except Exception:
        raise RuntimeError(f"{label} returned non-JSON: {body[:200]}")


def unwrap_array(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def normalize_queue(item):
    tokens = item.get("tokens") if isinstance(item.get("tokens"), dict) else {}
    input_tokens = to_int(tokens.get("input_tokens", tokens.get("prompt", item.get("input_tokens", item.get("prompt_tokens", item.get("prompt", 0))))), 0)
    output_tokens = to_int(
        tokens.get("output_tokens", tokens.get("completion", item.get("output_tokens", item.get("completion_tokens", item.get("completion", 0))))), 0
    )
    reasoning_tokens = to_int(tokens.get("reasoning_tokens", tokens.get("reasoning", item.get("reasoning_tokens", 0))), 0)
    token_details = tokens.get("details") if isinstance(tokens.get("details"), dict) else {}
    prompt_details = item.get("prompt_tokens_details") if isinstance(item.get("prompt_tokens_details"), dict) else {}
    cache_hit_tokens = pick_int(
        tokens.get("cache_hit_tokens"),
        tokens.get("cached_tokens"),
        token_details.get("cache_hit_tokens"),
        token_details.get("cached_tokens"),
        prompt_details.get("cached_tokens"),
        item.get("cache_hit_tokens"),
        item.get("cached_tokens"),
        item.get("cache_hits"),
        item.get("cache_hit"),
        default=0,
    )
    total_tokens = to_int(tokens.get("total_tokens", tokens.get("total", item.get("total_tokens", input_tokens + output_tokens + reasoning_tokens))), 0)
    failed = 1 if item.get("failed", item.get("is_failed", False)) else 0
    lat = to_float(item.get("latency_ms", item.get("latency", 0)), 0.0)
    auth_account = pick_text(
        item.get("auth_account"),
        item.get("authAccount"),
        item.get("authorized_account"),
        item.get("authorizedAccount"),
        item.get("account"),
        item.get("auth_name"),
        item.get("source_account"),
        item.get("source"),
        default="-",
    )
    external_key = pick_text(
        item.get("external_key"),
        item.get("externalKey"),
        item.get("api_key"),
        item.get("apiKey"),
        item.get("key"),
        item.get("client_key"),
        item.get("consumer_key"),
        item.get("x_api_key"),
        default="-",
    )
    return {
        "provider": pick_text(item.get("provider"), item.get("provider_name"), default="-"),
        "model": pick_text(item.get("model"), item.get("model_name"), default="-"),
        "alias": pick_text(item.get("alias"), item.get("model_alias"), default="-"),
        "source": pick_text(item.get("source"), auth_account, default="-"),
        "auth_account": auth_account,
        "external_key": external_key,
        "requests": 1,
        "success": 0 if failed else 1,
        "failed": failed,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "reasoning_tokens": max(0, reasoning_tokens),
        "cache_hit_tokens": max(0, cache_hit_tokens),
        "total_tokens": max(0, total_tokens),
        "avg_latency_ms": max(0.0, lat),
        "min_latency_ms": max(0.0, lat),
        "max_latency_ms": max(0.0, lat),
    }


def normalize_usage(item):
    requests_count = max(1, to_int(item.get("requests", item.get("request_count", 1)), 1))
    failed = max(0, to_int(item.get("failed", item.get("fail_count", 0)), 0))
    success = max(0, requests_count - failed)
    input_tokens = max(0, to_int(item.get("input_tokens", item.get("prompt_tokens", item.get("prompt", 0))), 0))
    output_tokens = max(0, to_int(item.get("output_tokens", item.get("completion_tokens", item.get("completion", 0))), 0))
    reasoning_tokens = max(0, to_int(item.get("reasoning_tokens", 0), 0))
    prompt_details = item.get("prompt_tokens_details") if isinstance(item.get("prompt_tokens_details"), dict) else {}
    cache_hit_tokens = max(
        0,
        pick_int(
            item.get("cache_hit_tokens"),
            item.get("cached_tokens"),
            prompt_details.get("cached_tokens"),
            item.get("cache_hits"),
            item.get("cache_hit"),
            default=0,
        ),
    )
    total_tokens = max(0, to_int(item.get("total_tokens", item.get("total", input_tokens + output_tokens + reasoning_tokens)), 0))
    avg_latency = max(0.0, to_float(item.get("avg_latency_ms", item.get("latency_ms", item.get("latency", 0))), 0.0))
    auth_account = pick_text(
        item.get("auth_account"),
        item.get("authAccount"),
        item.get("authorized_account"),
        item.get("authorizedAccount"),
        item.get("account"),
        item.get("auth_name"),
        item.get("source_account"),
        item.get("source"),
        default="-",
    )
    external_key = pick_text(
        item.get("external_key"),
        item.get("externalKey"),
        item.get("api_key"),
        item.get("apiKey"),
        item.get("key"),
        item.get("client_key"),
        item.get("consumer_key"),
        item.get("x_api_key"),
        default="-",
    )
    return {
        "provider": pick_text(item.get("provider"), item.get("provider_name"), default="-"),
        "model": pick_text(item.get("model"), item.get("model_name"), default="-"),
        "alias": pick_text(item.get("alias"), item.get("model_alias"), default="-"),
        "source": pick_text(item.get("source"), auth_account, default="-"),
        "auth_account": auth_account,
        "external_key": external_key,
        "requests": requests_count,
        "success": success,
        "failed": failed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "total_tokens": total_tokens,
        "avg_latency_ms": avg_latency,
        "min_latency_ms": max(0.0, to_float(item.get("min_latency_ms", avg_latency), avg_latency)),
        "max_latency_ms": max(0.0, to_float(item.get("max_latency_ms", avg_latency), avg_latency)),
    }


def normalize_legacy_payload(payload):
    direct = unwrap_array(payload)
    if direct:
        return [normalize_usage(x) for x in direct if isinstance(x, dict)]

    out = []
    if isinstance(payload, dict):
        apis = payload.get("usage", {}).get("apis") if isinstance(payload.get("usage"), dict) else payload.get("apis")
        if isinstance(apis, dict):
            for name, val in apis.items():
                if not isinstance(val, dict):
                    continue
                out.append(
                    normalize_usage(
                        {
                            "provider": val.get("provider", "-"),
                            "model": val.get("model", name),
                            "alias": val.get("alias", "-"),
                            "source": val.get("source", val.get("account", val.get("auth_name", "-"))),
                            "auth_account": val.get("auth_account", val.get("account", val.get("auth_name", val.get("source", "-")))),
                            "external_key": val.get("external_key", val.get("api_key", val.get("key", "-"))),
                            "requests": val.get("requests", val.get("request_count", val.get("count", 1))),
                            "failed": val.get("failed", val.get("fail_count", 0)),
                            "input_tokens": val.get("input_tokens", val.get("prompt_tokens", val.get("input", 0))),
                            "output_tokens": val.get("output_tokens", val.get("completion_tokens", val.get("output", 0))),
                            "reasoning_tokens": val.get("reasoning_tokens", 0),
                            "cache_hit_tokens": val.get("cache_hit_tokens", val.get("cached_tokens", val.get("cache_hits", val.get("cache_hit", 0)))),
                            "total_tokens": val.get("total_tokens", val.get("total", 0)),
                            "avg_latency_ms": val.get("avg_latency_ms", val.get("latency_ms", 0)),
                            "min_latency_ms": val.get("min_latency_ms", val.get("latency_ms", 0)),
                            "max_latency_ms": val.get("max_latency_ms", val.get("latency_ms", 0)),
                        }
                    )
                )
    return out


def aggregate_rows(rows):
    grouped = {}
    for r in rows:
        key = (r["provider"], r["model"], r["alias"], r["source"], r.get("auth_account", "-"), r.get("external_key", "-"))
        if key not in grouped:
            grouped[key] = {
                "provider": r["provider"],
                "model": r["model"],
                "alias": r["alias"],
                "source": r["source"],
                "auth_account": pick_text(r.get("auth_account"), r.get("source"), default="-"),
                "external_key": pick_text(r.get("external_key"), default="-"),
                "requests": 0,
                "success": 0,
                "failed": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_hit_tokens": 0,
                "total_tokens": 0,
                "lat_sum": 0.0,
                "lat_weight": 0,
                "min_latency_ms": 0.0,
                "max_latency_ms": 0.0,
            }
        g = grouped[key]
        req = max(1, to_int(r["requests"], 1))
        g["requests"] += req
        g["success"] += max(0, to_int(r["success"], 0))
        g["failed"] += max(0, to_int(r["failed"], 0))
        g["input_tokens"] += max(0, to_int(r["input_tokens"], 0))
        g["output_tokens"] += max(0, to_int(r["output_tokens"], 0))
        g["reasoning_tokens"] += max(0, to_int(r["reasoning_tokens"], 0))
        g["cache_hit_tokens"] += max(0, to_int(r.get("cache_hit_tokens", 0), 0))
        g["total_tokens"] += max(0, to_int(r["total_tokens"], 0))
        lat = max(0.0, to_float(r["avg_latency_ms"], 0.0))
        if lat > 0:
            g["lat_sum"] += lat * req
            g["lat_weight"] += req
            min_lat = max(0.0, to_float(r.get("min_latency_ms", lat), lat))
            max_lat = max(0.0, to_float(r.get("max_latency_ms", lat), lat))
            g["min_latency_ms"] = min_lat if g["min_latency_ms"] == 0 else min(g["min_latency_ms"], min_lat)
            g["max_latency_ms"] = max(g["max_latency_ms"], max_lat)

    out = []
    for g in grouped.values():
        avg_latency = g["lat_sum"] / g["lat_weight"] if g["lat_weight"] > 0 else 0.0
        out.append(
            {
                "provider": g["provider"],
                "model": g["model"],
                "alias": g["alias"],
                "source": g["source"],
                "auth_account": g["auth_account"],
                "external_key": g["external_key"],
                "requests": g["requests"],
                "success": g["success"],
                "failed": g["failed"],
                "input_tokens": g["input_tokens"],
                "output_tokens": g["output_tokens"],
                "reasoning_tokens": g["reasoning_tokens"],
                "cache_hit_tokens": g["cache_hit_tokens"],
                "total_tokens": g["total_tokens"],
                "avg_latency_ms": avg_latency,
                "min_latency_ms": g["min_latency_ms"],
                "max_latency_ms": g["max_latency_ms"],
            }
        )
    out.sort(key=lambda x: (x["total_tokens"], x["requests"]), reverse=True)
    return out


def persist_pull(fetched_at, profile, endpoint, traces, aggregated_rows, ok, message):
    profile_id = to_int(profile.get("id", 0), 0) if profile else 0
    profile_name = profile.get("name", "") if profile else ""
    conn = get_conn()
    try:
        if ok and aggregated_rows:
            conn.executemany(
                """
                INSERT INTO usage_records (
                  fetched_at, profile_id, profile_name, provider, model, alias, source, auth_account, external_key,
                  requests, success, failed,
                  input_tokens, output_tokens, reasoning_tokens, cache_hit_tokens, total_tokens,
                  avg_latency_ms, min_latency_ms, max_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        fetched_at,
                        profile_id,
                        profile_name,
                        r["provider"],
                        r["model"],
                        r["alias"],
                        r["source"],
                        r.get("auth_account", "-"),
                        r.get("external_key", "-"),
                        r["requests"],
                        r["success"],
                        r["failed"],
                        r["input_tokens"],
                        r["output_tokens"],
                        r["reasoning_tokens"],
                        r.get("cache_hit_tokens", 0),
                        r["total_tokens"],
                        r["avg_latency_ms"],
                        r["min_latency_ms"],
                        r["max_latency_ms"],
                    )
                    for r in aggregated_rows
                ],
            )

            total_requests = sum(x["requests"] for x in aggregated_rows)
            total_success = sum(x["success"] for x in aggregated_rows)
            total_failed = sum(x["failed"] for x in aggregated_rows)
            total_input = sum(x["input_tokens"] for x in aggregated_rows)
            total_output = sum(x["output_tokens"] for x in aggregated_rows)
            total_reasoning = sum(x["reasoning_tokens"] for x in aggregated_rows)
            total_tokens = sum(x["total_tokens"] for x in aggregated_rows)
            lat_weight = sum(x["requests"] for x in aggregated_rows if x["avg_latency_ms"] > 0)
            lat_sum = sum(x["avg_latency_ms"] * x["requests"] for x in aggregated_rows if x["avg_latency_ms"] > 0)
            avg_latency = lat_sum / lat_weight if lat_weight > 0 else 0.0

            conn.execute(
                """
                INSERT INTO pull_snapshots (
                  fetched_at, profile_id, profile_name, source_endpoint, model_groups,
                  total_requests, total_success, total_failed,
                  total_input_tokens, total_output_tokens, total_reasoning_tokens,
                  total_tokens, avg_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetched_at,
                    profile_id,
                    profile_name,
                    endpoint,
                    len(aggregated_rows),
                    total_requests,
                    total_success,
                    total_failed,
                    total_input,
                    total_output,
                    total_reasoning,
                    total_tokens,
                    avg_latency,
                ),
            )

        conn.execute(
            """
            INSERT INTO pull_logs (fetched_at, profile_id, profile_name, ok, source_endpoint, message, trace_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fetched_at, profile_id, profile_name, 1 if ok else 0, endpoint, message, json.dumps(traces, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_data(retention_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat(timespec="seconds")
    conn = get_conn()
    try:
        c1 = conn.execute("DELETE FROM usage_records WHERE fetched_at < ?", (cutoff,)).rowcount
        c2 = conn.execute("DELETE FROM pull_snapshots WHERE fetched_at < ?", (cutoff,)).rowcount
        c3 = conn.execute("DELETE FROM pull_logs WHERE fetched_at < ?", (cutoff,)).rowcount
        conn.commit()
        return {"cutoff": cutoff, "usage_records": c1, "pull_snapshots": c2, "pull_logs": c3}
    finally:
        conn.close()


def perform_refresh(trigger="manual"):
    if not refresh_lock.acquire(blocking=False):
        return {"ok": False, "message": "refresh already running"}

    fetched_at = now_iso()
    traces = []
    endpoint_used = "none"
    profile = get_active_profile()
    try:
        if not profile:
            msg = "no active profile"
            persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
            scheduler_state["last_run_at"] = fetched_at
            scheduler_state["last_ok"] = False
            scheduler_state["last_message"] = msg
            return {"ok": False, "message": msg}

        base = str(profile.get("base_url", "")).rstrip("/")
        token = profile.get("token", "")
        mode = profile.get("endpoint_mode", "auto")
        count = max(1, to_int(profile.get("queue_count", 300), 300))
        if not base:
            msg = "active profile base_url is empty"
            persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
            scheduler_state["last_run_at"] = fetched_at
            scheduler_state["last_ok"] = False
            scheduler_state["last_message"] = msg
            return {"ok": False, "message": msg}

        def load_queue():
            url = f"{base}/v0/management/usage-queue?count={count}"
            payload = request_json(url, token, "usage-queue", traces)
            return [normalize_queue(x) for x in unwrap_array(payload) if isinstance(x, dict)]

        def load_legacy():
            url = f"{base}/v0/management/usage"
            payload = request_json(url, token, "legacy-usage", traces)
            return normalize_legacy_payload(payload)

        rows = []
        if mode == "queue":
            endpoint_used = "usage-queue"
            rows = load_queue()
        elif mode == "legacy":
            endpoint_used = "legacy-usage"
            rows = load_legacy()
        else:
            queue_error = None
            try:
                endpoint_used = "usage-queue"
                rows = load_queue()
                if not rows:
                    try:
                        fallback_rows = load_legacy()
                        if fallback_rows:
                            endpoint_used = "legacy-usage-fallback"
                            rows = fallback_rows
                    except Exception:
                        pass
            except Exception as e_queue:
                queue_error = e_queue
                try:
                    endpoint_used = "legacy-usage"
                    rows = load_legacy()
                except Exception as e_legacy:
                    raise RuntimeError(f"usage-queue failed: {e_queue}; legacy-usage failed: {e_legacy}")

        aggregated = aggregate_rows(rows)
        msg = f"{trigger}: profile={profile['name']} source={endpoint_used}, raw={len(rows)}, groups={len(aggregated)}"
        persist_pull(fetched_at, profile, endpoint_used, traces, aggregated, True, msg)

        cfg = read_config()
        cleanup_old_data(max(1, to_int(cfg.get("retention_days", 30), 30)))

        scheduler_state["last_run_at"] = fetched_at
        scheduler_state["last_ok"] = True
        scheduler_state["last_message"] = msg
        return {
            "ok": True,
            "message": msg,
            "profile": {"id": profile["id"], "name": profile["name"]},
            "source": endpoint_used,
            "raw_count": len(rows),
            "group_count": len(aggregated),
            "traces": traces,
        }
    except Exception as e:
        msg = str(e)
        if "1010" in msg:
            msg += " | Cloudflare 1010: 站点基于客户端签名拦截，请优先使用 endpoint_mode=queue，并确认该域名允许当前服务器 IP/UA 访问。"
        persist_pull(fetched_at, profile, endpoint_used, traces, [], False, msg)
        scheduler_state["last_run_at"] = fetched_at
        scheduler_state["last_ok"] = False
        scheduler_state["last_message"] = msg
        return {"ok": False, "message": msg, "traces": traces}
    finally:
        refresh_lock.release()


def query_stats(hours, keyword, profile_id):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    profile = get_profile(profile_id) if profile_id else None
    conn = get_conn()
    try:
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        rows = conn.execute(
            f"""
            SELECT
              provider, model, alias, source, auth_account, external_key,
              SUM(requests) AS requests,
              SUM(success) AS success,
              SUM(failed) AS failed,
              SUM(input_tokens) AS input_tokens,
              SUM(output_tokens) AS output_tokens,
              SUM(reasoning_tokens) AS reasoning_tokens,
              SUM(cache_hit_tokens) AS cache_hit_tokens,
              SUM(total_tokens) AS total_tokens,
              SUM(CASE WHEN avg_latency_ms > 0 THEN avg_latency_ms * requests ELSE 0 END) AS lat_sum,
              SUM(CASE WHEN avg_latency_ms > 0 THEN requests ELSE 0 END) AS lat_weight,
              MIN(CASE WHEN min_latency_ms > 0 THEN min_latency_ms ELSE NULL END) AS min_latency_ms,
              MAX(max_latency_ms) AS max_latency_ms
            FROM usage_records
            {where}
            GROUP BY provider, model, alias, source, auth_account, external_key
            ORDER BY total_tokens DESC, requests DESC
            """,
            params,
        ).fetchall()

        grouped = {}
        for r in rows:
            req = to_int(r["requests"], 0)
            succ = to_int(r["success"], 0)
            lat_weight = to_int(r["lat_weight"], 0)
            avg_latency = to_float(r["lat_sum"], 0.0) / lat_weight if lat_weight > 0 else 0.0
            user_name = user_name_for_key(profile, r["external_key"])
            item = {
                "provider": r["provider"],
                "model": r["model"],
                "alias": r["alias"],
                "source": r["source"],
                "auth_account": r["auth_account"],
                "external_key": r["external_key"],
                "user_name": user_name,
                "requests": req,
                "success": succ,
                "failed": to_int(r["failed"], 0),
                "input_tokens": to_int(r["input_tokens"], 0),
                "output_tokens": to_int(r["output_tokens"], 0),
                "reasoning_tokens": to_int(r["reasoning_tokens"], 0),
                "cache_hit_tokens": to_int(r["cache_hit_tokens"], 0),
                "total_tokens": to_int(r["total_tokens"], 0),
                "lat_sum": avg_latency * req if avg_latency > 0 else 0.0,
                "lat_weight": req if avg_latency > 0 else 0,
                "min_latency_ms": to_float(r["min_latency_ms"], 0.0),
                "max_latency_ms": to_float(r["max_latency_ms"], 0.0),
            }
            if row_matches_keyword(item, keyword, ("provider", "model", "alias", "source", "auth_account", "external_key", "user_name")):
                key = (item["provider"], item["model"], item["source"], item["user_name"])
                if key not in grouped:
                    grouped[key] = {
                        "provider": item["provider"],
                        "model": item["model"],
                        "alias": item["alias"],
                        "source": item["source"],
                        "auth_account": item["auth_account"],
                        "external_key": "",
                        "user_name": item["user_name"],
                        "requests": 0,
                        "success": 0,
                        "failed": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_hit_tokens": 0,
                        "total_tokens": 0,
                        "lat_sum": 0.0,
                        "lat_weight": 0,
                        "min_latency_ms": 0.0,
                        "max_latency_ms": 0.0,
                    }
                g = grouped[key]
                for k in ("requests", "success", "failed", "input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "total_tokens", "lat_weight"):
                    g[k] += item[k]
                g["lat_sum"] += item["lat_sum"]
                min_lat = to_float(item["min_latency_ms"], 0.0)
                if min_lat > 0 and (g["min_latency_ms"] <= 0 or min_lat < g["min_latency_ms"]):
                    g["min_latency_ms"] = min_lat
                g["max_latency_ms"] = max(g["max_latency_ms"], to_float(item["max_latency_ms"], 0.0))

        out = []
        for g in grouped.values():
            req = to_int(g["requests"], 0)
            succ = to_int(g["success"], 0)
            lat_weight = to_int(g.pop("lat_weight"), 0)
            lat_sum = to_float(g.pop("lat_sum"), 0.0)
            g["success_rate"] = (succ / req) if req else 0.0
            g["avg_latency_ms"] = lat_sum / lat_weight if lat_weight > 0 else 0.0
            out.append(g)
        out.sort(key=lambda x: (x["total_tokens"], x["requests"]), reverse=True)

        summary = {
            "models": len(out),
            "requests": sum(x["requests"] for x in out),
            "success": sum(x["success"] for x in out),
            "failed": sum(x["failed"] for x in out),
            "input_tokens": sum(x["input_tokens"] for x in out),
            "output_tokens": sum(x["output_tokens"] for x in out),
            "reasoning_tokens": sum(x["reasoning_tokens"] for x in out),
            "cache_hit_tokens": sum(x["cache_hit_tokens"] for x in out),
            "total_tokens": sum(x["total_tokens"] for x in out),
            "avg_latency_ms": 0.0,
            "success_rate": 0.0,
        }
        summary["success_rate"] = (summary["success"] / summary["requests"]) if summary["requests"] else 0.0
        lat_weight = sum(x["requests"] for x in out if x["avg_latency_ms"] > 0)
        lat_sum = sum(x["avg_latency_ms"] * x["requests"] for x in out if x["avg_latency_ms"] > 0)
        summary["avg_latency_ms"] = lat_sum / lat_weight if lat_weight > 0 else 0.0
        return {"hours": hours, "since": since_iso, "summary": summary, "rows": out}
    finally:
        conn.close()


def query_trend(hours, limit, profile_id):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    conn = get_conn()
    try:
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT fetched_at, source_endpoint, total_requests, total_success, total_failed, total_tokens, avg_latency_ms
            FROM pull_snapshots
            {where}
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        items = []
        for r in reversed(rows):
            req = to_int(r["total_requests"], 0)
            succ = to_int(r["total_success"], 0)
            items.append(
                {
                    "fetched_at": r["fetched_at"],
                    "source": r["source_endpoint"],
                    "requests": req,
                    "tokens": to_int(r["total_tokens"], 0),
                    "success_rate": (succ / req) if req else 0.0,
                    "avg_latency_ms": to_float(r["avg_latency_ms"], 0.0),
                }
            )
        return {"hours": hours, "points": items}
    finally:
        conn.close()


def query_logs(limit, profile_id):
    conn = get_conn()
    try:
        if profile_id:
            rows = conn.execute(
                """
                SELECT id, fetched_at, ok, source_endpoint, message, trace_json
                FROM pull_logs
                WHERE profile_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (profile_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, fetched_at, ok, source_endpoint, message, trace_json
                FROM pull_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        out = []
        for r in rows:
            try:
                traces = json.loads(r["trace_json"]) if r["trace_json"] else []
            except Exception:
                traces = []
            out.append(
                {
                    "id": r["id"],
                    "fetched_at": r["fetched_at"],
                    "ok": bool(r["ok"]),
                    "source": r["source_endpoint"],
                    "message": r["message"],
                    "traces": traces,
                }
            )
        return out
    finally:
        conn.close()


def query_records(hours, profile_id, keyword, limit):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat(timespec="seconds")
    conn = get_conn()
    try:
        profiles = {}
        if profile_id:
            profile = get_profile(profile_id)
            if profile:
                profiles[profile_id] = profile
        where = "WHERE fetched_at >= ?"
        params = [since_iso]
        if profile_id:
            where += " AND profile_id = ?"
            params.append(profile_id)
        limit_sql = "" if keyword else "LIMIT ?"
        if not keyword:
            params.append(limit)

        rows = conn.execute(
            f"""
            SELECT
              id, fetched_at, profile_id, profile_name,
              provider, model, alias, source, auth_account, external_key,
              requests, success, failed,
              input_tokens, output_tokens, reasoning_tokens, cache_hit_tokens, total_tokens,
              avg_latency_ms, min_latency_ms, max_latency_ms
            FROM usage_records
            {where}
            ORDER BY fetched_at DESC, id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            pid = to_int(item.get("profile_id"), 0)
            if pid not in profiles:
                profile = get_profile(pid) if pid else None
                if profile:
                    profiles[pid] = profile
            item["user_name"] = user_name_for_key(profiles.get(pid), item.get("external_key"))
            if row_matches_keyword(item, keyword, ("profile_name", "provider", "model", "alias", "source", "auth_account", "external_key", "user_name")):
                out.append(item)
                if keyword and len(out) >= limit:
                    break
        return out
    finally:
        conn.close()


def clear_cache():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM usage_records")
        conn.execute("DELETE FROM pull_snapshots")
        conn.execute("DELETE FROM pull_logs")
        conn.commit()
    finally:
        conn.close()


class RefreshScheduler(threading.Thread):
    daemon = True

    def run(self):
        while True:
            try:
                cfg = read_config()
                enabled = bool(to_int(cfg.get("auto_refresh_enabled", 0), 0))
                interval_sec = max(5, to_int(cfg.get("refresh_interval_sec", 60), 60))
                now = time.time()
                nxt = scheduler_state.get("next_run_at")
                if not enabled:
                    scheduler_state["next_run_at"] = None
                else:
                    if nxt is None:
                        scheduler_state["next_run_at"] = now + 1
                    elif now >= nxt:
                        perform_refresh("auto")
                        scheduler_state["next_run_at"] = time.time() + interval_sec
            except Exception as e:
                scheduler_state["last_ok"] = False
                scheduler_state["last_message"] = str(e)
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "RouterStatsSQLite/2.0"

    def _json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = to_int(self.headers.get("Content-Length", "0"), 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(body) if body else {}

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        u = parse.urlparse(self.path)
        if u.path.startswith("/api/"):
            self.handle_api_get(u)
            return
        self.serve_static(u.path)

    def do_POST(self):
        u = parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            self._json(404, {"ok": False, "message": "not found"})
            return
        self.handle_api_post(u)

    def handle_api_get(self, u):
        q = parse.parse_qs(u.query or "")
        if u.path == "/api/health":
            cfg = read_config()
            ap = get_active_profile()
            self._json(
                200,
                {
                    "ok": True,
                    "now": now_iso(),
                    "scheduler": scheduler_state,
                    "active_profile": {"id": ap["id"], "name": ap["name"]} if ap else None,
                    "config_brief": {
                        "refresh_interval_sec": cfg.get("refresh_interval_sec", 60),
                        "auto_refresh_enabled": bool(cfg.get("auto_refresh_enabled", 0)),
                        "lookback_hours": cfg.get("lookback_hours", 24),
                        "record_limit": cfg.get("record_limit", 300),
                        "retention_days": cfg.get("retention_days", 30),
                    },
                },
            )
            return

        if u.path == "/api/config":
            cfg = read_config()
            ap = get_active_profile()
            self._json(200, {"ok": True, "config": cfg, "active_profile": {"id": ap["id"], "name": ap["name"]} if ap else None})
            return

        if u.path == "/api/profiles":
            cfg = read_config()
            self._json(200, {"ok": True, "active_profile_id": cfg.get("active_profile_id"), "profiles": list_profiles()})
            return

        if u.path == "/api/stats":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            keyword = (q.get("keyword") or [""])[0].strip()
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_stats(hours, keyword, profile_id)})
            return

        if u.path == "/api/trend":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            limit = max(10, min(5000, to_int((q.get("limit") or ["200"])[0], 200)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_trend(hours, limit, profile_id)})
            return

        if u.path == "/api/logs":
            cfg = read_config()
            limit = max(1, min(200, to_int((q.get("limit") or ["20"])[0], 20)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            self._json(200, {"ok": True, "data": query_logs(limit, profile_id)})
            return

        if u.path == "/api/records":
            cfg = read_config()
            hours = max(1, min(24 * 90, to_int((q.get("hours") or [str(cfg.get("lookback_hours", 24))])[0], 24)))
            profile_id = to_int((q.get("profile_id") or [str(cfg.get("active_profile_id") or 0)])[0], 0)
            keyword = (q.get("keyword") or [""])[0].strip()
            limit = max(10, min(5000, to_int((q.get("limit") or [str(cfg.get("record_limit", 300))])[0], 300)))
            self._json(200, {"ok": True, "data": query_records(hours, profile_id, keyword, limit)})
            return

        if u.path == "/api/auth-sync/status":
            manager = get_auth_sync_manager()
            self._json(200, {"ok": True, "data": manager.get_status()})
            return

        if u.path == "/api/auth-sync/config":
            manager = get_auth_sync_manager()
            self._json(200, {"ok": True, "data": _build_auth_sync_public_config(manager.get_config())})
            return

        if u.path == "/api/auth-sync/records":
            manager = get_auth_sync_manager()
            page = max(1, to_int((q.get("page") or ["1"])[0], 1))
            page_size = max(1, min(2000, to_int((q.get("page_size") or ["50"])[0], 50)))
            self._json(200, {"ok": True, "data": manager.get_records_page(page=page, page_size=page_size)})
            return

        if u.path == "/api/auth-sync/file-records":
            page = max(1, to_int((q.get("page") or ["1"])[0], 1))
            page_size = max(1, min(2000, to_int((q.get("page_size") or ["50"])[0], 50)))
            self._json(200, {"ok": True, "data": get_auth_sync_file_records_page(page=page, page_size=page_size)})
            return

        if u.path == "/api/auth-sync/files":
            manager = get_auth_sync_manager()
            apply_filter = bool(to_int((q.get("apply_filter") or ["0"])[0], 0))
            data = manager.fetch_auth_files(apply_filter=apply_filter)
            save_auth_sync_cached_files(data)
            cached = get_auth_sync_cached_files()
            merged = merge_auth_sync_rows_with_latest(cached.get("rows") or [])
            self._json(200, {"ok": True, "last_fetched_at": cached.get("last_fetched_at"), "data": merged})
            return

        if u.path == "/api/auth-sync/files-last":
            cached = get_auth_sync_cached_files()
            merged = merge_auth_sync_rows_with_latest(cached.get("rows") or [])
            self._json(200, {"ok": True, "last_fetched_at": cached.get("last_fetched_at"), "data": merged})
            return

        if u.path == "/api/auth-sync/sub2-groups":
            manager = get_auth_sync_manager()
            platform = (q.get("platform") or [""])[0].strip()
            try:
                groups = manager.list_sub2_groups(platform=platform)
                self._json(200, {"ok": True, "data": groups, "platform": platform})
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        self._json(404, {"ok": False, "message": "not found"})

    def handle_api_post(self, u):
        if u.path == "/api/config":
            payload = self._read_json()
            cfg = write_config(payload)
            scheduler_state["next_run_at"] = None
            self._json(200, {"ok": True, "config": cfg})
            return

        if u.path == "/api/profiles/upsert":
            payload = self._read_json()
            pid = upsert_profile(payload)
            if payload.get("set_active"):
                set_active_profile(pid)
            self._json(200, {"ok": True, "profile_id": pid})
            return

        if u.path == "/api/profiles/select":
            payload = self._read_json()
            pid = set_active_profile(payload.get("id"))
            scheduler_state["next_run_at"] = None
            self._json(200, {"ok": True, "active_profile_id": pid})
            return

        if u.path == "/api/profiles/delete":
            payload = self._read_json()
            delete_profile(payload.get("id"))
            self._json(200, {"ok": True})
            return

        if u.path == "/api/refresh":
            result = perform_refresh("manual")
            self._json(200 if result.get("ok") else 500, result)
            return

        if u.path == "/api/cache/clear":
            clear_cache()
            self._json(200, {"ok": True, "message": "cache cleared"})
            return

        if u.path == "/api/cache/prune":
            payload = self._read_json()
            days = max(1, min(3650, to_int(payload.get("retention_days"), to_int(read_config().get("retention_days", 30), 30))))
            stats = cleanup_old_data(days)
            self._json(200, {"ok": True, "message": "pruned", "data": stats})
            return

        if u.path == "/api/auth-sync/config":
            payload = self._read_json()
            try:
                manager = get_auth_sync_manager()
                manager.update_config(payload)
                self._json(200, {"ok": True, "data": _build_auth_sync_public_config(manager.get_config()), "message": "auth sync config saved"})
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        if u.path == "/api/auth-sync/run":
            manager = get_auth_sync_manager()
            manager.request_run(trigger="manual")
            self._json(200, {"ok": True, "message": "auth sync queued"})
            return

        if u.path == "/api/auth-sync/sync-selected":
            payload = self._read_json()
            manager = get_auth_sync_manager()
            try:
                override = payload.get("config") if isinstance(payload, dict) else {}
                override = apply_auth_sync_override_preserving_auth(override)
                files = payload.get("files") or []
                mapping_raw = payload.get("group_ids_by_file") if isinstance(payload, dict) else {}
                group_ids_by_file = {}
                if isinstance(mapping_raw, dict):
                    for k, v in mapping_raw.items():
                        name = str(k or "").strip()
                        if not name:
                            continue
                        group_ids_by_file[name] = _normalize_group_ids(v if isinstance(v, list) else [])
                if not group_ids_by_file:
                    cached_map = get_auth_sync_cached_entry_json_map(files)
                    for name in files:
                        n = str(name or "").strip()
                        if not n:
                            continue
                        entry = cached_map.get(n, {}).get("entry_json") if isinstance(cached_map.get(n), dict) else {}
                        gids = _normalize_group_ids(entry.get("target_group_ids") if isinstance(entry, dict) else [])
                        if gids:
                            group_ids_by_file[n] = gids
                summary = manager.sync_selected_files(
                    files,
                    trigger="manual_selected",
                    raw_override=override,
                    group_ids_by_file=group_ids_by_file,
                )
                self._json(200, {"ok": True, "message": "selected files synced", "data": summary})
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        if u.path == "/api/auth-sync/preview-selected":
            self._json(403, {"ok": False, "message": "preview feature disabled for security"})
            return

        if u.path == "/api/auth-sync/db-json-selected":
            self._json(403, {"ok": False, "message": "auth json query disabled for security"})
            return

        if u.path == "/api/auth-sync/files-enabled":
            payload = self._read_json()
            try:
                files = payload.get("files") if isinstance(payload, dict) else []
                enabled = bool(payload.get("enabled")) if isinstance(payload, dict) else False
                updated = set_auth_sync_cached_files_enabled(files, enabled)
                self._json(200, {"ok": True, "message": "sync enabled updated", "data": {"updated": updated, "enabled": enabled}})
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        if u.path == "/api/auth-sync/files-groups":
            payload = self._read_json()
            try:
                files = payload.get("files") if isinstance(payload, dict) else []
                target_group_ids = payload.get("target_group_ids") if isinstance(payload, dict) else []
                updated = set_auth_sync_cached_files_groups(files, target_group_ids)
                self._json(
                    200,
                    {
                        "ok": True,
                        "message": "target groups updated",
                        "data": {"updated": updated, "target_group_ids": _normalize_group_ids(target_group_ids)},
                    },
                )
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        if u.path == "/api/auth-sync/files-groups-sync-from-sub2":
            payload = self._read_json()
            manager = get_auth_sync_manager()
            try:
                files = payload.get("files") if isinstance(payload, dict) else []
                override = payload.get("config") if isinstance(payload, dict) else {}
                override = apply_auth_sync_override_preserving_auth(override)
                summary = manager.pull_sub2_groups_for_files(files, raw_override=override)
                updated_result = set_auth_sync_cached_files_groups_by_map(summary.get("group_ids_by_file") or {})
                self._json(
                    200,
                    {
                        "ok": True,
                        "message": "target groups synced from sub2",
                        "data": {
                            "sync_summary": summary,
                            "updated": int(updated_result.get("updated", 0) or 0),
                            "updated_files": updated_result.get("files") or [],
                        },
                    },
                )
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        if u.path == "/api/auth-sync/sub2-validate":
            payload = self._read_json()
            manager = get_auth_sync_manager()
            try:
                override = payload.get("config") if isinstance(payload, dict) else {}
                override = apply_auth_sync_override_preserving_auth(override)
                data = manager.validate_sub2_auth(override)
                self._json(200, {"ok": True, "message": data.get("message", ""), "data": data})
            except Exception as e:
                self._json(400, {"ok": False, "message": str(e)})
            return

        self._json(404, {"ok": False, "message": "not found"})

    def serve_static(self, path):
        req_path = "/" if path in ("", "/") else path
        if req_path == "/":
            target = WEB_ROOT / "index.html"
        else:
            safe = Path(req_path.lstrip("/"))
            target = (WEB_ROOT / safe).resolve()
            if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
                self.send_error(403)
                return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return

        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Avoid stale cached HTML/JS/CSS that may carry previous garbled text.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    sync_manager = init_auth_sync_manager()
    scheduler = RefreshScheduler()
    scheduler.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server: http://{HOST}:{PORT}")
    print(f"DB: {DB_PATH}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sync_manager.stop()


if __name__ == "__main__":
    main()
