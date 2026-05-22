import copy
import fnmatch
import json
import logging
import os
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib import error, parse, request


DEFAULT_PROVIDER_MAPPING = {
    "codex": {"platform": "openai", "type": "oauth"},
    "claude": {"platform": "anthropic", "type": "oauth"},
    "gemini": {"platform": "gemini", "type": "oauth"},
    "antigravity": {"platform": "antigravity", "type": "oauth"},
}


def build_default_raw_config() -> Dict[str, Any]:
    return {
        "interval_seconds": 300,
        "web": {"host": "127.0.0.1", "port": 8990},
        "cpa": {
            "base_url": "http://127.0.0.1:8317/v0/management",
            "management_key": "",
            "timeout_seconds": 20,
            "verify_ssl": True,
            "headers": {},
        },
        "sub2api": {
            "import_url": "http://127.0.0.1:8080/api/v1/admin/accounts/data",
            "timeout_seconds": 30,
            "verify_ssl": True,
            "skip_default_group_bind": True,
            "headers": {"x-api-key": ""},
        },
        "sync": {
            "delete_after_success": False,
            "dry_run": False,
            "max_files_per_cycle": 0,
            "default_concurrency": 1,
            "default_priority": 0,
            "name_template": "{filename}",
            "save_transformed_dir": "",
            "max_record_items": 2000,
            "provider_mapping": copy.deepcopy(DEFAULT_PROVIDER_MAPPING),
            "sync_type": "expiry_policy",
            "expiry_sync_days": 0,
            "sync_without_expiry": True,
            "auth_file_filter": {
                "only_json": True,
                "allow_runtime_only": False,
                "allow_non_file_source": False,
                "require_enabled": False,
                "include_providers": [],
                "exclude_providers": [],
                "include_statuses": [],
                "exclude_statuses": [],
                "include_name_patterns": [],
                "exclude_name_patterns": [],
                "min_size_bytes": None,
                "max_size_bytes": None,
            },
        },
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge_dict(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class CPAConfig:
    base_url: str
    management_key: str
    timeout_seconds: int = 20
    verify_ssl: bool = True
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Sub2APIConfig:
    import_url: str
    timeout_seconds: int = 30
    verify_ssl: bool = True
    headers: Dict[str, str] = field(default_factory=dict)
    skip_default_group_bind: bool = True


@dataclass
class SyncConfig:
    interval_seconds: int
    cpa: CPAConfig
    sub2api: Sub2APIConfig
    delete_after_success: bool = False
    dry_run: bool = False
    max_files_per_cycle: int = 0
    default_concurrency: int = 1
    default_priority: int = 0
    name_template: str = "{filename}"
    provider_mapping: Dict[str, Dict[str, str]] = field(default_factory=dict)
    sync_type: str = "expiry_policy"
    expiry_sync_days: int = 0
    sync_without_expiry: bool = True
    auth_file_filter: Dict[str, Any] = field(default_factory=dict)
    save_transformed_dir: str = ""
    max_record_items: int = 2000
    web_host: str = "127.0.0.1"
    web_port: int = 8990


def parse_config(raw: Dict[str, Any]) -> SyncConfig:
    merged = deep_merge_dict(build_default_raw_config(), raw or {})
    cpa_raw = merged["cpa"]
    sub_raw = merged["sub2api"]
    sync_raw = merged.get("sync", {})
    web_raw = merged.get("web", {})

    provider_mapping = copy.deepcopy(DEFAULT_PROVIDER_MAPPING)
    if isinstance(sync_raw.get("provider_mapping"), dict):
        provider_mapping.update(sync_raw["provider_mapping"])

    sync_type = str(sync_raw.get("sync_type", "expiry_policy")).strip().lower()
    valid_sync_types = {"all", "expiry_policy", "expired_only", "no_expiry_only"}
    if sync_type not in valid_sync_types:
        raise ValueError(f"invalid sync.sync_type: {sync_type}")

    auth_file_filter = sync_raw.get("auth_file_filter", {})
    if not isinstance(auth_file_filter, dict):
        raise ValueError("sync.auth_file_filter must be object")

    return SyncConfig(
        interval_seconds=max(1, int(merged.get("interval_seconds", 300))),
        cpa=CPAConfig(
            base_url=str(cpa_raw.get("base_url", "")).rstrip("/"),
            management_key=str(cpa_raw.get("management_key", "")),
            timeout_seconds=max(1, int(cpa_raw.get("timeout_seconds", 20))),
            verify_ssl=bool(cpa_raw.get("verify_ssl", True)),
            headers=dict(cpa_raw.get("headers", {})),
        ),
        sub2api=Sub2APIConfig(
            import_url=str(sub_raw.get("import_url", "")),
            timeout_seconds=max(1, int(sub_raw.get("timeout_seconds", 30))),
            verify_ssl=bool(sub_raw.get("verify_ssl", True)),
            headers=dict(sub_raw.get("headers", {})),
            skip_default_group_bind=bool(sub_raw.get("skip_default_group_bind", True)),
        ),
        delete_after_success=False,
        dry_run=False,
        max_files_per_cycle=max(0, int(sync_raw.get("max_files_per_cycle", 0))),
        default_concurrency=1,
        default_priority=0,
        name_template="{filename}",
        provider_mapping=provider_mapping,
        sync_type=sync_type,
        expiry_sync_days=int(sync_raw.get("expiry_sync_days", 0)),
        sync_without_expiry=True,
        auth_file_filter=auth_file_filter,
        save_transformed_dir="",
        max_record_items=2000,
        web_host=str(web_raw.get("host", "127.0.0.1")),
        web_port=max(1, int(web_raw.get("port", 8990))),
    )


def ssl_context(verify_ssl: bool) -> Optional[ssl.SSLContext]:
    if verify_ssl:
        return None
    return ssl._create_unverified_context()


def http_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    timeout: int,
    verify_ssl: bool,
    body: Optional[bytes] = None,
) -> Tuple[int, Any]:
    req = request.Request(url=url, method=method, data=body)
    for k, v in headers.items():
        req.add_header(k, v)
    ctx = ssl_context(verify_ssl)
    try:
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.getcode()
            content = resp.read()
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        detail_lower = detail.lower()
        if e.code == 401 and ("invalid_token" in detail_lower or "invalid token" in detail_lower):
            raise RuntimeError(
                f"HTTP {e.code} {method} {url}: INVALID_TOKEN. "
                "sub2 授权失败，请检查“基础地址”是否对应当前环境、x-api-key/Authorization 是否正确、"
                "以及 Authorization 模式下是否需要 Bearer 前缀。"
            ) from e
        if "1010" in detail_lower:
            raise RuntimeError(
                f"HTTP {e.code} {method} {url}: error code 1010 (Cloudflare access denied). "
                "请在 Cloudflare/WAF 放行当前服务 IP 与请求头（x-api-key / Authorization / User-Agent / Accept 等）。"
            ) from e
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"{method} {url} failed: {e}") from e

    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        return status, None
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def deep_remove_refresh_token(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() == "refresh_token":
                continue
            out[k] = deep_remove_refresh_token(v)
        return out
    if isinstance(value, list):
        return [deep_remove_refresh_token(v) for v in value]
    return value


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def parse_datetime_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            try:
                ts = int(s)
                if ts > 1e12:
                    ts = ts / 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                return None
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def extract_last_refresh_at(auth_doc: Dict[str, Any]) -> Optional[datetime]:
    candidates = [
        "last_refresh",
        "last_refresh_at",
        "lastRefresh",
        "lastRefreshAt",
        "refresh_at",
        "refreshAt",
        "refreshed_at",
        "refreshedAt",
        "updated_at",
        "updatedAt",
        "update_at",
    ]
    for key in candidates:
        if key in auth_doc:
            dt = parse_datetime_value(auth_doc.get(key))
            if dt:
                return dt

    token = auth_doc.get("token")
    if isinstance(token, dict):
        for key in candidates:
            if key in token:
                dt = parse_datetime_value(token.get(key))
                if dt:
                    return dt
    return None


def should_sync_by_expiry(
    cfg: SyncConfig,
    auth_doc: Dict[str, Any],
    last_refresh_at_override: Optional[datetime] = None,
) -> Tuple[bool, Optional[datetime], str]:
    if cfg.expiry_sync_days <= 0:
        return True, None, "未启用刷新时间校验（expiry_sync_days<=0），默认允许同步"
    last_refresh_at = last_refresh_at_override if isinstance(last_refresh_at_override, datetime) else extract_last_refresh_at(auth_doc)
    if last_refresh_at is None:
        if cfg.sync_without_expiry:
            return True, None, "未找到最后刷新token时间，但已允许“无刷新时间账号可同步”（sync_without_expiry=true）"
        return False, None, "未找到最后刷新token时间，且策略要求必须存在刷新时间（sync_without_expiry=false）"
    now = datetime.now(timezone.utc)
    elapsed_days = (now - last_refresh_at).total_seconds() / 86400.0
    last_text = last_refresh_at.isoformat(timespec="seconds")
    now_text = now.isoformat(timespec="seconds")
    if elapsed_days <= cfg.expiry_sync_days:
        return (
            True,
            last_refresh_at,
            f"最后刷新token时间在有效期内（最后刷新: {last_text}，当前: {now_text}，已过: {elapsed_days:.2f} 天，阈值: {cfg.expiry_sync_days} 天）",
        )
    return (
        False,
        last_refresh_at,
        f"最后刷新token时间已超过有效期（最后刷新: {last_text}，当前: {now_text}，已过: {elapsed_days:.2f} 天，阈值: {cfg.expiry_sync_days} 天）",
    )


def should_sync_by_type(
    cfg: SyncConfig,
    auth_doc: Dict[str, Any],
    last_refresh_at_override: Optional[datetime] = None,
) -> Tuple[bool, Optional[datetime], str]:
    if cfg.sync_type == "all":
        return True, None, "同步策略为 all，直接允许同步"
    if cfg.sync_type == "expiry_policy":
        return should_sync_by_expiry(cfg, auth_doc, last_refresh_at_override=last_refresh_at_override)

    last_refresh_at = last_refresh_at_override if isinstance(last_refresh_at_override, datetime) else extract_last_refresh_at(auth_doc)
    now = datetime.now(timezone.utc)
    if cfg.sync_type == "expired_only":
        if last_refresh_at is None:
            return False, None, "未找到最后刷新token时间，无法按 expired_only 策略判断"
        elapsed_days = (now - last_refresh_at).total_seconds() / 86400.0
        last_text = last_refresh_at.isoformat(timespec="seconds")
        now_text = now.isoformat(timespec="seconds")
        if elapsed_days <= cfg.expiry_sync_days:
            return (
                True,
                last_refresh_at,
                f"最后刷新token时间在有效期内（最后刷新: {last_text}，当前: {now_text}，已过: {elapsed_days:.2f} 天，阈值: {cfg.expiry_sync_days} 天）",
            )
        return (
            False,
            last_refresh_at,
            f"最后刷新token时间已超过有效期（最后刷新: {last_text}，当前: {now_text}，已过: {elapsed_days:.2f} 天，阈值: {cfg.expiry_sync_days} 天）",
        )
    if cfg.sync_type == "no_expiry_only":
        if last_refresh_at is None:
            return True, None, "未找到最后刷新token时间，符合 no_expiry_only 策略"
        return False, last_refresh_at, "已存在最后刷新token时间，不符合 no_expiry_only 策略"
    return should_sync_by_expiry(cfg, auth_doc, last_refresh_at_override=last_refresh_at_override)


def _to_lower_set(values: Any) -> set:
    if not isinstance(values, list):
        return set()
    out = set()
    for v in values:
        s = str(v).strip().lower()
        if s:
            out.add(s)
    return out


def _to_patterns(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for v in values:
        s = str(v).strip().lower()
        if s:
            out.append(s)
    return out


def _to_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _match_any(name: str, patterns: List[str]) -> bool:
    for p in patterns:
        if fnmatch.fnmatch(name, p):
            return True
    return False


def detect_provider(file_name: str, auth_doc: Dict[str, Any]) -> str:
    t = str(auth_doc.get("type", "")).strip().lower()
    if t:
        return t
    lower = file_name.lower()
    for provider in DEFAULT_PROVIDER_MAPPING.keys():
        if lower.startswith(provider + "-") or lower == provider + ".json":
            return provider
    return ""


def detect_provider_from_entry(entry: Dict[str, Any]) -> str:
    provider = str(entry.get("provider", "")).strip().lower()
    if provider:
        return provider
    t = str(entry.get("type", "")).strip().lower()
    if t:
        return t
    lower = str(entry.get("name", "")).strip().lower()
    for p in DEFAULT_PROVIDER_MAPPING.keys():
        if lower.startswith(p + "-") or lower == p + ".json":
            return p
    return ""


def evaluate_auth_file_filter(cfg: SyncConfig, entry: Dict[str, Any]) -> Tuple[bool, str]:
    f = cfg.auth_file_filter or {}
    name = str(entry.get("name", "")).strip()
    lower_name = name.lower()
    provider = detect_provider_from_entry(entry)
    status = str(entry.get("status", "")).strip().lower()
    source = str(entry.get("source", "file")).strip().lower()
    runtime_only = bool(entry.get("runtime_only", False))
    disabled = bool(entry.get("disabled", False))
    unavailable = bool(entry.get("unavailable", False))
    size = _to_optional_int(entry.get("size"))

    if bool(f.get("only_json", True)) and not lower_name.endswith(".json"):
        return False, "not json file"
    if runtime_only and not bool(f.get("allow_runtime_only", False)):
        return False, "runtime_only filtered"
    if source != "file" and not bool(f.get("allow_non_file_source", False)):
        return False, f"source filtered: {source}"
    if bool(f.get("require_enabled", False)) and (disabled or unavailable):
        return False, "disabled/unavailable filtered"

    include_providers = _to_lower_set(f.get("include_providers"))
    if include_providers and provider not in include_providers:
        return False, f"provider not in include: {provider or '<unknown>'}"
    exclude_providers = _to_lower_set(f.get("exclude_providers"))
    if provider and provider in exclude_providers:
        return False, f"provider excluded: {provider}"

    include_statuses = _to_lower_set(f.get("include_statuses"))
    if include_statuses and status not in include_statuses:
        return False, f"status not in include: {status or '<empty>'}"
    exclude_statuses = _to_lower_set(f.get("exclude_statuses"))
    if status and status in exclude_statuses:
        return False, f"status excluded: {status}"

    include_names = _to_patterns(f.get("include_name_patterns"))
    if include_names and not _match_any(lower_name, include_names):
        return False, "name not matched include patterns"
    exclude_names = _to_patterns(f.get("exclude_name_patterns"))
    if exclude_names and _match_any(lower_name, exclude_names):
        return False, "name matched exclude patterns"

    min_size = _to_optional_int(f.get("min_size_bytes"))
    if min_size is not None and size is not None and size < min_size:
        return False, f"size<{min_size}"
    max_size = _to_optional_int(f.get("max_size_bytes"))
    if max_size is not None and size is not None and size > max_size:
        return False, f"size>{max_size}"
    return True, "pass"


def build_account_name(template: str, file_name: str, provider: str, auth_doc: Dict[str, Any]) -> str:
    filename_no_ext = file_name[:-5] if file_name.lower().endswith(".json") else file_name
    values = {
        "filename": filename_no_ext,
        "provider": provider,
        "email": str(auth_doc.get("email", "")).strip(),
        "account": str(auth_doc.get("account", "")).strip(),
        "id": str(auth_doc.get("id", "")).strip(),
    }
    try:
        name = template.format(**values).strip()
        if name:
            return name
    except Exception:
        pass
    return filename_no_ext


def extract_auth_email(auth_doc: Dict[str, Any]) -> str:
    return extract_account_email(auth_doc)


def extract_account_email(account_doc: Dict[str, Any]) -> str:
    if not isinstance(account_doc, dict):
        return ""
    for key in ("email", "mail", "user_email", "username"):
        v = str(account_doc.get(key, "") or "").strip()
        if v and "@" in v:
            return v.lower()
    extra = account_doc.get("extra")
    if isinstance(extra, dict):
        for key in ("email", "mail", "user_email", "username"):
            v = str(extra.get(key, "") or "").strip()
            if v and "@" in v:
                return v.lower()
    credentials = account_doc.get("credentials")
    if isinstance(credentials, dict):
        for key in ("email", "mail", "user_email", "username"):
            v = str(credentials.get(key, "") or "").strip()
            if v and "@" in v:
                return v.lower()
        token = credentials.get("token")
        if isinstance(token, dict):
            for key in ("email", "mail", "user_email", "username"):
                v = str(token.get(key, "") or "").strip()
                if v and "@" in v:
                    return v.lower()
    name = str(account_doc.get("name", "") or "").strip()
    if name and "@" in name:
        return name.lower()
    return ""


def build_credentials(provider: str, auth_doc: Dict[str, Any]) -> Dict[str, Any]:
    raw = auth_doc.get("credentials") if isinstance(auth_doc.get("credentials"), dict) else auth_doc
    if not isinstance(raw, dict):
        return {}
    credentials = copy.deepcopy(raw)
    if provider == "gemini" and isinstance(credentials.get("token"), dict):
        token = credentials["token"]
        for key in ("access_token", "id_token", "token_type", "expiry", "expires_in"):
            if key in token and key not in credentials:
                credentials[key] = token[key]
    return credentials


def convert_to_sub2api_account(cfg: SyncConfig, file_name: str, auth_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    provider = detect_provider(file_name, auth_doc)
    mapping = cfg.provider_mapping.get(provider)
    if mapping:
        credentials = build_credentials(provider, auth_doc)
        if not isinstance(credentials, dict) or not credentials:
            return None
        platform = mapping["platform"]
        account_type = mapping["type"]
    else:
        # Compatible with sub2api exported account-json style input.
        platform = str(auth_doc.get("platform", "")).strip()
        account_type = str(auth_doc.get("type", "")).strip()
        credentials = build_credentials(provider, auth_doc)
        if not platform or not account_type or not isinstance(credentials, dict) or not credentials:
            return None
    email = extract_auth_email(auth_doc)
    # Replace-by-email strategy:
    # Use stable account name by email so repeated sync with same email overwrites target account.
    account_name = email if email else build_account_name(cfg.name_template, file_name, provider, auth_doc)
    out = {
        "name": account_name,
        "platform": platform,
        "type": account_type,
        "credentials": credentials,
        "concurrency": cfg.default_concurrency,
        "priority": cfg.default_priority,
    }
    extra = auth_doc.get("extra")
    if isinstance(extra, dict):
        out["extra"] = copy.deepcopy(extra)
    elif email:
        out["extra"] = {"email": email}

    if "rate_multiplier" in auth_doc:
        try:
            out["rate_multiplier"] = float(auth_doc.get("rate_multiplier"))
        except Exception:
            out["rate_multiplier"] = 1
    else:
        out["rate_multiplier"] = 1

    if "auto_pause_on_expired" in auth_doc:
        out["auto_pause_on_expired"] = bool(auth_doc.get("auto_pause_on_expired"))
    else:
        out["auto_pause_on_expired"] = True
    return deep_remove_refresh_token(out)


def parse_files_response(payload: Any) -> List[Dict[str, Any]]:
    data = unwrap_data(payload)
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return [f for f in data["files"] if isinstance(f, dict)]
    if isinstance(data, list):
        return [f for f in data if isinstance(f, dict)]
    return []


def is_import_success(result_payload: Any) -> bool:
    data = unwrap_data(result_payload)
    if not isinstance(data, dict):
        return True
    if "account_failed" in data and int(data.get("account_failed", 0)) > 0:
        return False
    created = int(data.get("account_created", 0) or 0) if "account_created" in data else 0
    updated = int(data.get("account_updated", 0) or 0) if "account_updated" in data else 0
    if "account_created" in data or "account_updated" in data:
        return (created + updated) > 0
    return True


def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def cpa_headers(cfg: SyncConfig) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {cfg.cpa.management_key}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    headers.update(cfg.cpa.headers)
    return headers


def sub_headers(cfg: SyncConfig) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    headers.update(cfg.sub2api.headers)
    return headers


def list_cpa_auth_files(cfg: SyncConfig) -> List[Dict[str, Any]]:
    url = f"{cfg.cpa.base_url}/auth-files"
    _, payload = http_json(
        method="GET",
        url=url,
        headers=cpa_headers(cfg),
        timeout=cfg.cpa.timeout_seconds,
        verify_ssl=cfg.cpa.verify_ssl,
    )
    return parse_files_response(payload)


def download_cpa_auth_file(cfg: SyncConfig, file_name: str) -> Dict[str, Any]:
    query = parse.urlencode({"name": file_name})
    url = f"{cfg.cpa.base_url}/auth-files/download?{query}"
    _, payload = http_json(
        method="GET",
        url=url,
        headers=cpa_headers(cfg),
        timeout=cfg.cpa.timeout_seconds,
        verify_ssl=cfg.cpa.verify_ssl,
    )
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"downloaded content is not JSON object: {file_name}")


def delete_cpa_auth_file(cfg: SyncConfig, file_name: str) -> None:
    query = parse.urlencode({"name": file_name})
    url = f"{cfg.cpa.base_url}/auth-files?{query}"
    http_json(
        method="DELETE",
        url=url,
        headers=cpa_headers(cfg),
        timeout=cfg.cpa.timeout_seconds,
        verify_ssl=cfg.cpa.verify_ssl,
    )


def build_sub2_import_request(cfg: SyncConfig, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "proxies": [],
            "accounts": list(accounts or []),
        },
        "skip_default_group_bind": cfg.sub2api.skip_default_group_bind,
    }


def derive_sub2_accounts_url(import_url: str) -> str:
    raw = str(import_url or "").strip()
    if not raw:
        return ""
    parsed = parse.urlsplit(raw)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/data"):
        path = path[:-5]
    out = parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return out.rstrip("/")


def parse_sub2_account_items(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    data = unwrap_data(payload)
    if isinstance(data, dict):
        items = data.get("items")
        total = data.get("total")
        total_int = None
        try:
            if total is not None:
                total_int = int(total)
        except Exception:
            total_int = None
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)], total_int
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], None
    return [], None


def list_sub2_accounts(cfg: SyncConfig, search: str, page: int = 1, page_size: int = 200) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    base_url = derive_sub2_accounts_url(cfg.sub2api.import_url)
    if not base_url:
        raise RuntimeError("sub2 account url is empty")
    q = {"page": max(1, int(page)), "page_size": max(1, int(page_size))}
    s = str(search or "").strip()
    if s:
        q["search"] = s
    url = f"{base_url}?{parse.urlencode(q)}"
    _, payload = http_json(
        method="GET",
        url=url,
        headers=sub_headers(cfg),
        timeout=cfg.sub2api.timeout_seconds,
        verify_ssl=cfg.sub2api.verify_ssl,
    )
    return parse_sub2_account_items(payload)


def derive_sub2_groups_url(import_url: str) -> str:
    accounts_url = derive_sub2_accounts_url(import_url)
    if not accounts_url:
        return ""
    if accounts_url.endswith("/accounts"):
        return f"{accounts_url[:-len('/accounts')]}/groups/all"
    return f"{accounts_url.rstrip('/')}/groups/all"


def list_sub2_groups(cfg: SyncConfig, platform: str = "") -> List[Dict[str, Any]]:
    groups_url = derive_sub2_groups_url(cfg.sub2api.import_url)
    if not groups_url:
        raise RuntimeError("sub2 groups url is empty")
    q = {}
    p = str(platform or "").strip()
    if p:
        q["platform"] = p
    url = f"{groups_url}?{parse.urlencode(q)}" if q else groups_url
    _, payload = http_json(
        method="GET",
        url=url,
        headers=sub_headers(cfg),
        timeout=cfg.sub2api.timeout_seconds,
        verify_ssl=cfg.sub2api.verify_ssl,
    )
    data = unwrap_data(payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def delete_sub2_account(cfg: SyncConfig, account_id: int) -> None:
    base_url = derive_sub2_accounts_url(cfg.sub2api.import_url)
    if not base_url:
        raise RuntimeError("sub2 account url is empty")
    url = f"{base_url}/{int(account_id)}"
    http_json(
        method="DELETE",
        url=url,
        headers=sub_headers(cfg),
        timeout=cfg.sub2api.timeout_seconds,
        verify_ssl=cfg.sub2api.verify_ssl,
    )


def bind_sub2_account_groups(cfg: SyncConfig, account_id: int, group_ids: List[int]) -> None:
    base_url = derive_sub2_accounts_url(cfg.sub2api.import_url)
    if not base_url:
        raise RuntimeError("sub2 account url is empty")
    gid_list = []
    for v in group_ids or []:
        try:
            iv = int(v)
        except Exception:
            continue
        if iv > 0 and iv not in gid_list:
            gid_list.append(iv)
    if not gid_list:
        return
    body = json.dumps({"group_ids": gid_list}, ensure_ascii=False).encode("utf-8")
    http_json(
        method="PUT",
        url=f"{base_url}/{int(account_id)}",
        headers=sub_headers(cfg),
        timeout=cfg.sub2api.timeout_seconds,
        verify_ssl=cfg.sub2api.verify_ssl,
        body=body,
    )


def extract_sub2_account_group_ids(account_item: Dict[str, Any]) -> List[int]:
    if not isinstance(account_item, dict):
        return []
    out: List[int] = []

    def add_gid(v: Any) -> None:
        try:
            iv = int(v)
        except Exception:
            return
        if iv > 0 and iv not in out:
            out.append(iv)

    for key in ("group_ids", "groupIds"):
        val = account_item.get(key)
        if isinstance(val, list):
            for x in val:
                add_gid(x)
        elif val is not None:
            add_gid(val)

    for key in ("groups", "group_list", "groupList"):
        val = account_item.get(key)
        if not isinstance(val, list):
            continue
        for x in val:
            if isinstance(x, dict):
                add_gid(x.get("id"))
                add_gid(x.get("group_id"))
                add_gid(x.get("groupId"))
            else:
                add_gid(x)

    add_gid(account_item.get("group_id"))
    add_gid(account_item.get("groupId"))
    return out


def find_sub2_account_for_binding(cfg: SyncConfig, account_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    target_name = str((account_payload or {}).get("name", "") or "").strip()
    if not target_name:
        return None
    target_platform = str((account_payload or {}).get("platform", "") or "").strip().lower()
    target_type = str((account_payload or {}).get("type", "") or "").strip().lower()
    page = 1
    page_size = 200
    while page <= 5:
        items, total = list_sub2_accounts(cfg, search=target_name, page=page, page_size=page_size)
        matched = []
        for item in items:
            if str(item.get("name", "") or "").strip() != target_name:
                continue
            if target_platform and str(item.get("platform", "") or "").strip().lower() != target_platform:
                continue
            if target_type and str(item.get("type", "") or "").strip().lower() != target_type:
                continue
            matched.append(item)
        for item in matched:
            try:
                aid = int(item.get("id"))
            except Exception:
                continue
            if aid > 0:
                return item
        if total is not None and page * page_size >= int(total):
            break
        if not items:
            break
        page += 1
    return None


def find_sub2_account_id_for_binding(cfg: SyncConfig, account_payload: Dict[str, Any]) -> Optional[int]:
    item = find_sub2_account_for_binding(cfg, account_payload)
    if not isinstance(item, dict):
        return None
    try:
        aid = int(item.get("id"))
    except Exception:
        return None
    return aid if aid > 0 else None


def bind_sub2_groups_for_account_payload(cfg: SyncConfig, account_payload: Dict[str, Any], group_ids: List[int]) -> None:
    aid = find_sub2_account_id_for_binding(cfg, account_payload)
    if not aid:
        raise RuntimeError(f"找不到刚同步的 sub2 账号用于分组绑定: {str((account_payload or {}).get('name', '') or '')}")
    bind_sub2_account_groups(cfg, aid, group_ids)


def replace_sub2_accounts_by_email(cfg: SyncConfig, account_payload: Dict[str, Any]) -> int:
    target_email = extract_account_email(account_payload)
    target_name = str(account_payload.get("name", "") or "").strip().lower()
    target_platform = str(account_payload.get("platform", "") or "").strip().lower()
    target_type = str(account_payload.get("type", "") or "").strip().lower()

    if not target_email and not target_name:
        return 0

    search_key = target_email or target_name
    page = 1
    page_size = 200
    deleted = 0
    visited_ids = set()
    max_pages = 20
    while page <= max_pages:
        items, total = list_sub2_accounts(cfg, search=search_key, page=page, page_size=page_size)
        if not items:
            break
        for item in items:
            try:
                acc_id = int(item.get("id"))
            except Exception:
                continue
            if acc_id in visited_ids:
                continue
            visited_ids.add(acc_id)

            item_platform = str(item.get("platform", "") or "").strip().lower()
            item_type = str(item.get("type", "") or "").strip().lower()
            if target_platform and item_platform and item_platform != target_platform:
                continue
            if target_type and item_type and item_type != target_type:
                continue

            item_email = extract_account_email(item)
            item_name = str(item.get("name", "") or "").strip().lower()
            matched = False
            if target_email and item_email == target_email:
                matched = True
            elif target_name and item_name == target_name:
                matched = True

            if matched:
                delete_sub2_account(cfg, acc_id)
                deleted += 1

        if total is not None and page * page_size >= total:
            break
        if len(items) < page_size:
            break
        page += 1
    return deleted


def push_to_sub2api(cfg: SyncConfig, account_payload: Dict[str, Any]) -> Any:
    deleted = replace_sub2_accounts_by_email(cfg, account_payload)
    if deleted > 0:
        logging.info(
            "sub2 replace-by-email removed existing accounts: name=%s email=%s deleted=%d",
            str(account_payload.get("name", "") or "").strip(),
            extract_account_email(account_payload),
            deleted,
        )
    body = build_sub2_import_request(cfg, [account_payload])
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    _, payload = http_json(
        method="POST",
        url=cfg.sub2api.import_url,
        headers=sub_headers(cfg),
        timeout=cfg.sub2api.timeout_seconds,
        verify_ssl=cfg.sub2api.verify_ssl,
        body=body_bytes,
    )
    return payload


def validate_sub2api_auth(cfg: SyncConfig) -> Dict[str, Any]:
    import_url = str(cfg.sub2api.import_url or "").strip()
    headers = sub_headers(cfg)
    auth_header = str(headers.get("Authorization", "") or "").strip()
    auth_key = str(headers.get("x-api-key", "") or headers.get("X-API-KEY", "") or "").strip()
    if not import_url:
        raise RuntimeError("sub2 基础地址为空，无法校验授权。")
    if not auth_header and not auth_key:
        raise RuntimeError("sub2 授权为空，无法校验授权。")

    probe = build_sub2_import_request(cfg, [])
    body_bytes = json.dumps(probe, ensure_ascii=False).encode("utf-8")
    try:
        status, payload = http_json(
            method="POST",
            url=cfg.sub2api.import_url,
            headers=sub_headers(cfg),
            timeout=cfg.sub2api.timeout_seconds,
            verify_ssl=cfg.sub2api.verify_ssl,
            body=body_bytes,
        )
    except Exception as e:
        msg = str(e)
        msg_lower = msg.lower()
        if "invalid_token" in msg_lower or "invalid token" in msg_lower or "http 401" in msg_lower:
            return {
                "ok": False,
                "status": 401,
                "message": "sub2 授权无效（INVALID_TOKEN），请检查授权 Key、鉴权方式（是否 Bearer）以及目标环境。",
            }
        return {"ok": False, "status": None, "message": msg}

    if is_import_success(payload):
        return {"ok": True, "status": status, "message": "sub2 授权验证通过。", "result": payload}

    code = ""
    message = ""
    if isinstance(payload, dict):
        code = str(payload.get("code", "") or "").strip()
        message = str(payload.get("message", "") or "").strip()
    token_failed = "invalid_token" in code.lower() or "invalid token" in message.lower()
    if token_failed:
        return {
            "ok": False,
            "status": status,
            "message": "sub2 授权无效（INVALID_TOKEN），请检查授权 Key、鉴权方式（是否 Bearer）以及目标环境。",
            "result": payload,
        }
    tip = "sub2 授权验证通过（接口返回业务提示，可忽略）。"
    if message:
        tip = f"{tip} {message}"
    return {"ok": True, "status": status, "message": tip, "result": payload}


class SyncRecordStore:
    def __init__(self, db_path: str, max_items: int, retention_days: int = 7) -> None:
        self.db_path = db_path
        self.max_items = max_items
        self.retention_days = max(1, int(retention_days or 7))
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sync_records (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sync_records_ts ON auth_sync_records(ts DESC)")
            conn.commit()
        finally:
            conn.close()

    def _prune(self, conn) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM auth_sync_records WHERE ts < ?", (cutoff,))

    def add(self, item: Dict[str, Any]) -> None:
        payload = dict(item)
        if "ts" not in payload:
            payload["ts"] = now_iso()
        with self._lock:
            conn = self._conn()
            try:
                self._prune(conn)
                conn.execute(
                    "INSERT INTO auth_sync_records (ts, payload_json) VALUES (?, ?)",
                    (str(payload.get("ts") or now_iso()), json.dumps(payload, ensure_ascii=False)),
                )
                if self.max_items > 0:
                    conn.execute(
                        """
                        DELETE FROM auth_sync_records
                        WHERE id NOT IN (
                          SELECT id FROM auth_sync_records ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (int(self.max_items),),
                    )
                conn.commit()
            finally:
                conn.close()

    def list(self, limit: int = 200) -> List[Dict[str, Any]]:
        page_data = self.list_page(page=1, page_size=max(1, int(limit or 200)))
        return page_data.get("items", [])

    def list_page(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        p = max(1, int(page or 1))
        ps = max(1, min(2000, int(page_size or 50)))
        offset = (p - 1) * ps
        with self._lock:
            conn = self._conn()
            try:
                self._prune(conn)
                total = conn.execute("SELECT COUNT(1) AS c FROM auth_sync_records").fetchone()["c"]
                rows = conn.execute(
                    "SELECT payload_json FROM auth_sync_records ORDER BY id DESC LIMIT ? OFFSET ?",
                    (ps, max(0, offset)),
                ).fetchall()
                conn.commit()
            finally:
                conn.close()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                item = json.loads(row["payload_json"] or "{}")
            except Exception:
                item = {}
            if isinstance(item, dict):
                out.append(item)
        total_pages = max(1, (int(total) + ps - 1) // ps) if int(total) > 0 else 1
        if p > total_pages:
            p = total_pages
        return {
            "items": out,
            "page": p,
            "page_size": ps,
            "total": int(total),
            "total_pages": int(total_pages),
        }


class SyncManager:
    def __init__(
        self,
        root_dir: Path,
        raw_config: Dict[str, Any],
        save_raw_config: Callable[[Dict[str, Any]], None],
        cpa_source_getter: Optional[Callable[[], Dict[str, Any]]] = None,
        file_audit_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        cached_enabled_name_getter: Optional[Callable[[], set]] = None,
        cached_entry_getter: Optional[Callable[[List[str]], Dict[str, Dict[str, Any]]]] = None,
        record_db_path: Optional[str] = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self._save_raw_config = save_raw_config
        self._cpa_source_getter = cpa_source_getter
        self._file_audit_hook = file_audit_hook
        self._cached_enabled_name_getter = cached_enabled_name_getter
        self._cached_entry_getter = cached_entry_getter
        self._record_db_path = str(record_db_path or (self.root_dir / "stats.db"))
        self._cfg_lock = threading.Lock()
        self._raw_config = deep_merge_dict(build_default_raw_config(), raw_config or {})
        self._cfg = parse_config(self._raw_config)
        self._record_store = SyncRecordStore(self._record_db_path, self._cfg.max_record_items, retention_days=7)
        self._cycle_lock = threading.Lock()
        self._manual_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._running = False
        self._last_cycle: Dict[str, Any] = {}
        self._next_run_at = time.time() + max(1, self._cfg.interval_seconds)

    def _resolve_path(self, path_value: str) -> str:
        path_value = str(path_value or "").strip()
        if not path_value:
            return ""
        p = Path(path_value)
        if not p.is_absolute():
            p = self.root_dir / p
        return str(p.resolve())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._manual_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def get_config(self) -> Dict[str, Any]:
        with self._cfg_lock:
            return copy.deepcopy(self._raw_config)

    def update_config(self, new_raw: Dict[str, Any]) -> None:
        merged = deep_merge_dict(build_default_raw_config(), new_raw or {})
        new_cfg = parse_config(merged)
        if new_cfg.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        persisted_raw = self._save_raw_config(merged)
        effective_raw = persisted_raw if isinstance(persisted_raw, dict) else merged
        effective_cfg = parse_config(effective_raw)
        with self._cfg_lock:
            self._raw_config = copy.deepcopy(effective_raw)
            self._cfg = effective_cfg
            self._record_store = SyncRecordStore(self._record_db_path, effective_cfg.max_record_items, retention_days=7)
            self._next_run_at = time.time() + max(1, effective_cfg.interval_seconds)
        self._record_store.add({"kind": "config", "status": "success", "message": "config updated"})

    def request_run(self, trigger: str = "manual") -> None:
        self._record_store.add({"kind": "control", "status": "accepted", "trigger": trigger, "message": "sync queued"})
        self._manual_event.set()

    def get_records(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self._record_store.list(limit=limit)

    def get_records_page(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        return self._record_store.list_page(page=page, page_size=page_size)

    def get_status(self) -> Dict[str, Any]:
        with self._cfg_lock:
            cfg = self._cfg
            next_run_at = self._next_run_at
        cpa_source = None
        if self._cpa_source_getter:
            try:
                src = self._cpa_source_getter() or {}
                cpa_source = {
                    "profile_id": src.get("profile_id"),
                    "profile_name": src.get("profile_name"),
                    "base_url": src.get("base_url"),
                    "has_token": bool(str(src.get("management_key", "")).strip()),
                }
            except Exception as e:
                cpa_source = {"error": str(e)}
        return {
            "running": self._running,
            "last_cycle": self._last_cycle,
            "next_run_at": datetime.fromtimestamp(next_run_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "interval_seconds": cfg.interval_seconds,
            "sync_type": cfg.sync_type,
            "web": {"host": cfg.web_host, "port": cfg.web_port},
            "cpa_source": cpa_source,
        }

    def _cfg_snapshot(self) -> SyncConfig:
        with self._cfg_lock:
            return copy.deepcopy(self._cfg)

    def _apply_cpa_source(self, cfg: SyncConfig) -> SyncConfig:
        if not self._cpa_source_getter:
            return cfg
        src = self._cpa_source_getter() or {}
        base_url = str(src.get("base_url", "") or "").strip().rstrip("/")
        token = str(src.get("management_key", "") or "").strip()
        if base_url:
            cfg.cpa.base_url = base_url
        if token:
            cfg.cpa.management_key = token
        return cfg

    def _runtime_cfg(self) -> SyncConfig:
        cfg = self._cfg_snapshot()
        cfg = self._apply_cpa_source(cfg)
        if not str(cfg.cpa.base_url or "").strip():
            raise RuntimeError("cpa base_url is empty (active profile)")
        if not str(cfg.cpa.management_key or "").strip():
            raise RuntimeError("cpa management_key is empty (active profile token)")
        return cfg

    def _runtime_cfg_with_override(self, raw_override: Optional[Dict[str, Any]] = None) -> SyncConfig:
        override = raw_override if isinstance(raw_override, dict) else {}
        if not override:
            return self._runtime_cfg()
        with self._cfg_lock:
            current_raw = copy.deepcopy(self._raw_config)
        merged = deep_merge_dict(current_raw, override)
        cfg = parse_config(merged)
        cfg = self._apply_cpa_source(cfg)
        if not str(cfg.cpa.base_url or "").strip():
            raise RuntimeError("cpa base_url is empty (active profile)")
        if not str(cfg.cpa.management_key or "").strip():
            raise RuntimeError("cpa management_key is empty (active profile token)")
        return cfg

    def _audit_file(self, payload: Dict[str, Any]) -> None:
        if not self._file_audit_hook:
            return
        try:
            self._file_audit_hook(dict(payload or {}))
        except Exception:
            pass

    def fetch_auth_files(self, apply_filter: bool = False) -> List[Dict[str, Any]]:
        cfg = self._runtime_cfg()
        files = list_cpa_auth_files(cfg)
        out = []
        for item in files:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["name"] = str(row.get("name", "")).strip()
            row["provider_detected"] = detect_provider_from_entry(row)
            if apply_filter:
                passed, reason = evaluate_auth_file_filter(cfg, row)
                row["filter_passed"] = bool(passed)
                row["filter_reason"] = reason
            out.append(row)
        out.sort(key=lambda x: str(x.get("name", "")).lower())
        return out

    def _normalize_selected_files(self, file_names: List[str]) -> List[str]:
        if not isinstance(file_names, list):
            raise RuntimeError("files must be list")
        selected = []
        seen = set()
        for v in file_names:
            name = str(v or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            selected.append(name)
        if not selected:
            raise RuntimeError("no files selected")
        if len(selected) > 1000:
            raise RuntimeError("too many selected files (max 1000)")
        return selected

    def preview_selected_files(self, file_names: List[str], raw_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        selected = self._normalize_selected_files(file_names)
        cfg = self._runtime_cfg_with_override(raw_override)
        items: List[Dict[str, Any]] = []
        accounts: List[Dict[str, Any]] = []
        converted = skipped = failed = 0
        for file_name in selected:
            try:
                auth_doc = download_cpa_auth_file(cfg, file_name)
                account = convert_to_sub2api_account(cfg, file_name, auth_doc)
                if not account:
                    skipped += 1
                    items.append(
                        {
                            "file": file_name,
                            "status": "skipped",
                            "message": "unsupported provider or invalid credentials",
                        }
                    )
                    continue

                converted += 1
                accounts.append(copy.deepcopy(account))
                items.append({"file": file_name, "status": "converted", "account": account})
            except Exception as e:
                failed += 1
                items.append({"file": file_name, "status": "failed", "message": str(e)})

        return {
            "total": len(selected),
            "converted": converted,
            "skipped": skipped,
            "failed": failed,
            "items": items,
            "accounts": accounts,
            "sub2_import_request": build_sub2_import_request(cfg, accounts),
        }

    def export_selected_auth_json(self, file_names: List[str], raw_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        selected = self._normalize_selected_files(file_names)
        cfg = self._runtime_cfg_with_override(raw_override)
        items: List[Dict[str, Any]] = []
        exported = failed = 0
        for file_name in selected:
            try:
                auth_doc = download_cpa_auth_file(cfg, file_name)
                items.append({"file": file_name, "auth_doc": auth_doc})
                exported += 1
            except Exception as e:
                failed += 1
                items.append({"file": file_name, "error": str(e)})
        return {
            "total": len(selected),
            "exported": exported,
            "failed": failed,
            "items": items,
        }

    def validate_sub2_auth(self, raw_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._cfg_lock:
            current_raw = copy.deepcopy(self._raw_config)
        override = raw_override if isinstance(raw_override, dict) else {}
        merged = deep_merge_dict(current_raw, override)
        cfg = parse_config(merged)
        return validate_sub2api_auth(cfg)

    def list_sub2_groups(self, raw_override: Optional[Dict[str, Any]] = None, platform: str = "") -> List[Dict[str, Any]]:
        with self._cfg_lock:
            current_raw = copy.deepcopy(self._raw_config)
        override = raw_override if isinstance(raw_override, dict) else {}
        merged = deep_merge_dict(current_raw, override)
        cfg = parse_config(merged)
        return list_sub2_groups(cfg, platform=platform)

    def pull_sub2_groups_for_files(self, file_names: List[str], raw_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        selected = self._normalize_selected_files(file_names)
        cfg = self._runtime_cfg_with_override(raw_override)
        items: List[Dict[str, Any]] = []
        mapping: Dict[str, List[int]] = {}
        ok = failed = skipped = 0
        for file_name in selected:
            try:
                auth_doc = download_cpa_auth_file(cfg, file_name)
                account = convert_to_sub2api_account(cfg, file_name, auth_doc)
                if not account:
                    skipped += 1
                    items.append({"file": file_name, "status": "skipped", "message": "无法识别账号映射"})
                    continue
                matched = find_sub2_account_for_binding(cfg, account)
                if not isinstance(matched, dict):
                    skipped += 1
                    items.append({"file": file_name, "status": "skipped", "message": "sub2 中未找到对应账号"})
                    continue
                gids = extract_sub2_account_group_ids(matched)
                mapping[file_name] = list(gids)
                ok += 1
                items.append({"file": file_name, "status": "success", "target_group_ids": gids})
            except Exception as e:
                failed += 1
                items.append({"file": file_name, "status": "failed", "message": str(e)})
        return {
            "total": len(selected),
            "ok": ok,
            "failed": failed,
            "skipped": skipped,
            "group_ids_by_file": mapping,
            "items": items,
        }

    def sync_selected_files(
        self,
        file_names: List[str],
        trigger: str = "manual_selected",
        raw_override: Optional[Dict[str, Any]] = None,
        group_ids_by_file: Optional[Dict[str, List[int]]] = None,
    ) -> Dict[str, Any]:
        selected = self._normalize_selected_files(file_names)

        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("another sync cycle is running")

        started = time.time()
        self._running = True
        cfg = self._runtime_cfg_with_override(raw_override)
        ok = fail = skipped = 0
        try:
            for file_name in selected:
                entry = {"name": file_name}
                try:
                    if isinstance(group_ids_by_file, dict):
                        gids = group_ids_by_file.get(file_name)
                        if isinstance(gids, list):
                            entry["target_group_ids"] = gids
                    result = self._process_one_file(cfg, entry, trigger, skip_auth_filter=True)
                    if result == "ok":
                        ok += 1
                    elif result == "skip":
                        skipped += 1
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    logging.exception("auth sync selected file failed: file=%s err=%s", file_name, e)
                    self._record_store.add(
                        {
                            "kind": "file",
                            "trigger": trigger,
                            "file": file_name,
                            "status": "failed",
                            "message": str(e),
                        }
                    )
        finally:
            duration_ms = int((time.time() - started) * 1000)
            status = "success"
            if fail > 0 and ok > 0:
                status = "partial"
            elif fail > 0 and ok == 0:
                status = "failed"
            summary = {
                "kind": "cycle",
                "trigger": trigger,
                "sync_type": cfg.sync_type,
                "status": status,
                "total": len(selected),
                "ok": ok,
                "fail": fail,
                "skipped": skipped,
                "duration_ms": duration_ms,
            }
            self._last_cycle = summary
            self._record_store.add(summary)
            self._running = False
            self._cycle_lock.release()
        return summary

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = self._cfg_snapshot()
            timeout = max(1, int(self._next_run_at - time.time()))
            manual = self._manual_event.wait(timeout=timeout)
            if self._stop_event.is_set():
                return
            trigger = "manual" if manual else "scheduled"
            if manual:
                self._manual_event.clear()
            self._run_cycle(trigger=trigger)
            cfg = self._cfg_snapshot()
            self._next_run_at = time.time() + max(1, cfg.interval_seconds)

    def _run_cycle(self, trigger: str) -> None:
        if not self._cycle_lock.acquire(blocking=False):
            self._record_store.add(
                {"kind": "cycle", "trigger": trigger, "status": "skipped", "message": "previous cycle still running"}
            )
            return
        started = time.time()
        self._running = True
        cfg = self._runtime_cfg()
        ok = fail = skipped = total = 0
        try:
            files = list_cpa_auth_files(cfg)
            if self._cached_enabled_name_getter:
                enabled_names = self._cached_enabled_name_getter() or set()
                files = [item for item in files if str((item or {}).get("name", "")).strip() in enabled_names]
            file_entry_map: Dict[str, Dict[str, Any]] = {}
            if self._cached_entry_getter:
                file_names = [str((item or {}).get("name", "")).strip() for item in files if isinstance(item, dict)]
                file_names = [x for x in file_names if x]
                try:
                    file_entry_map = self._cached_entry_getter(file_names) or {}
                except Exception:
                    file_entry_map = {}
            if cfg.max_files_per_cycle > 0:
                files = files[: cfg.max_files_per_cycle]
            total = len(files)
            for item in files:
                file_name = str(item.get("name", "")).strip()
                if not file_name:
                    continue
                cached = file_entry_map.get(file_name, {})
                entry_json = cached.get("entry_json") if isinstance(cached, dict) else {}
                if isinstance(entry_json, dict) and isinstance(entry_json.get("target_group_ids"), list):
                    item = dict(item)
                    item["target_group_ids"] = entry_json.get("target_group_ids")
                try:
                    result = self._process_one_file(cfg, item, trigger)
                    if result == "ok":
                        ok += 1
                    elif result == "skip":
                        skipped += 1
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    logging.exception("auth sync file failed: file=%s err=%s", file_name, e)
                    self._record_store.add(
                        {"kind": "file", "trigger": trigger, "file": file_name, "status": "failed", "message": str(e)}
                    )
        except Exception as e:
            fail += 1
            logging.exception("auth sync cycle failed: %s", e)
            self._record_store.add({"kind": "cycle", "trigger": trigger, "status": "failed", "message": str(e)})
        finally:
            duration_ms = int((time.time() - started) * 1000)
            status = "success"
            if fail > 0 and ok > 0:
                status = "partial"
            elif fail > 0 and ok == 0:
                status = "failed"
            summary = {
                "kind": "cycle",
                "trigger": trigger,
                "sync_type": cfg.sync_type,
                "status": status,
                "total": total,
                "ok": ok,
                "fail": fail,
                "skipped": skipped,
                "duration_ms": duration_ms,
            }
            self._last_cycle = summary
            self._record_store.add(summary)
            self._running = False
            self._cycle_lock.release()

    def _process_one_file(self, cfg: SyncConfig, entry: Dict[str, Any], trigger: str, skip_auth_filter: bool = False) -> str:
        file_name = str(entry.get("name", "")).strip()
        if not file_name:
            return "skip"

        auth_doc = download_cpa_auth_file(cfg, file_name)
        # Sync policy checks use the same refresh timestamp shown in file list first.
        # Fallback to downloaded auth_doc when list entry has no usable refresh timestamp.
        last_refresh_at_for_check = extract_last_refresh_at(entry) or extract_last_refresh_at(auth_doc)
        can_sync, last_refresh_at, reason = should_sync_by_type(cfg, auth_doc, last_refresh_at_override=last_refresh_at_for_check)
        if not can_sync:
            self._audit_file(
                {
                    "ts": now_iso(),
                    "file_name": file_name,
                    "trigger": trigger,
                    "status": "skipped",
                    "synced_to_sub2": False,
                    "sync_time": None,
                    "message": f"按同步策略跳过（{cfg.sync_type}）：{reason}",
                    "auth_doc": auth_doc,
                    "account": None,
                }
            )
            self._record_store.add(
                {
                    "kind": "file",
                    "trigger": trigger,
                    "file": file_name,
                    "status": "skipped",
                    "last_refresh_at": last_refresh_at.isoformat(timespec="seconds") if last_refresh_at else None,
                    "message": f"按同步策略跳过（{cfg.sync_type}）：{reason}",
                }
            )
            return "skip"

        account = convert_to_sub2api_account(cfg, file_name, auth_doc)
        if not account:
            self._audit_file(
                {
                    "ts": now_iso(),
                    "file_name": file_name,
                    "trigger": trigger,
                    "status": "skipped",
                    "synced_to_sub2": False,
                    "sync_time": None,
                    "message": "unsupported provider or invalid credentials",
                    "auth_doc": auth_doc,
                    "account": None,
                }
            )
            self._record_store.add(
                {
                    "kind": "file",
                    "trigger": trigger,
                    "file": file_name,
                    "status": "skipped",
                    "message": "unsupported provider or invalid credentials",
                }
            )
            return "skip"

        target_group_ids: List[int] = []
        for v in entry.get("target_group_ids", []) if isinstance(entry.get("target_group_ids"), list) else []:
            try:
                iv = int(v)
            except Exception:
                continue
            if iv > 0 and iv not in target_group_ids:
                target_group_ids.append(iv)
        if target_group_ids:
            account["group_ids"] = list(target_group_ids)

        if cfg.save_transformed_dir:
            out_dir = self._resolve_path(cfg.save_transformed_dir)
            ensure_dir(out_dir)
            out_path = os.path.join(out_dir, file_name)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(build_sub2_import_request(cfg, [account]), f, ensure_ascii=False, indent=2)

        if cfg.dry_run:
            self._audit_file(
                {
                    "ts": now_iso(),
                    "file_name": file_name,
                    "trigger": trigger,
                    "status": "success",
                    "synced_to_sub2": False,
                    "sync_time": None,
                    "message": "dry-run converted",
                    "auth_doc": auth_doc,
                    "account": account,
                }
            )
            self._record_store.add(
                {
                    "kind": "file",
                    "trigger": trigger,
                    "file": file_name,
                    "account_name": account["name"],
                    "status": "success",
                    "message": "dry-run converted",
                }
            )
            return "ok"

        result = push_to_sub2api(cfg, account)
        if not is_import_success(result):
            self._audit_file(
                {
                    "ts": now_iso(),
                    "file_name": file_name,
                    "trigger": trigger,
                    "status": "failed",
                    "synced_to_sub2": False,
                    "sync_time": None,
                    "message": f"push failed: {json.dumps(result, ensure_ascii=False)}",
                    "auth_doc": auth_doc,
                    "account": account,
                }
            )
            self._record_store.add(
                {
                    "kind": "file",
                    "trigger": trigger,
                    "file": file_name,
                    "account_name": account["name"],
                    "status": "failed",
                    "message": f"push failed: {json.dumps(result, ensure_ascii=False)}",
                }
            )
            return "fail"

        if target_group_ids:
            try:
                bind_sub2_groups_for_account_payload(cfg, account, target_group_ids)
            except Exception as e:
                self._audit_file(
                    {
                        "ts": now_iso(),
                        "file_name": file_name,
                        "trigger": trigger,
                        "status": "failed",
                        "synced_to_sub2": True,
                        "sync_time": now_iso(),
                        "message": f"synced but group bind failed: {e}",
                        "auth_doc": auth_doc,
                        "account": account,
                    }
                )
                self._record_store.add(
                    {
                        "kind": "file",
                        "trigger": trigger,
                        "file": file_name,
                        "account_name": account["name"],
                        "status": "failed",
                        "message": f"synced but group bind failed: {e}",
                    }
                )
                return "fail"

        msg = "synced"
        if target_group_ids:
            msg = f"synced; groups={','.join([str(x) for x in target_group_ids])}"
        synced_at = now_iso()
        self._audit_file(
            {
                "ts": synced_at,
                "file_name": file_name,
                "trigger": trigger,
                "status": "success",
                "synced_to_sub2": True,
                "sync_time": synced_at,
                "message": msg,
                "auth_doc": auth_doc,
                "account": account,
            }
        )

        self._record_store.add(
            {
                "kind": "file",
                "trigger": trigger,
                "file": file_name,
                "account_name": account["name"],
                "status": "success",
                "message": msg,
            }
        )
        return "ok"

