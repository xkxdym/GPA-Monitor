
    const $ = (id) => document.getElementById(id);
    const THEME_KEY = "cpa_theme_mode_v1";
    let authFiles = [];
    let sub2Groups = [];
    let sub2GroupsLoadedAt = null;
    const selectedAuthFiles = new Set();
    let lastAuthFilesFetchedAt = null;
    let recordPage = 1;
    let recordPageSize = 50;
    let fileRecordPage = 1;
    let fileRecordPageSize = 50;

    function systemTheme() {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(mode = "auto") {
      const effective = mode === "auto" ? systemTheme() : mode;
      document.documentElement.setAttribute("data-theme", effective);
      const sel = $("themeMode");
      if (sel && sel.value !== mode) sel.value = mode;
      ["themeLightBtn", "themeDarkBtn", "themeAutoBtn"].forEach((id) => {
        const el = $(id);
        if (!el) return;
        el.classList.remove("active");
      });
      if (mode === "light") $("themeLightBtn") && $("themeLightBtn").classList.add("active");
      else if (mode === "dark") $("themeDarkBtn") && $("themeDarkBtn").classList.add("active");
      else $("themeAutoBtn") && $("themeAutoBtn").classList.add("active");
      try { localStorage.setItem(THEME_KEY, mode); } catch (_) {}
    }

    function setStatus(msg, level = "info") {
      const el = $("status");
      el.textContent = msg;
      el.style.color = level === "error" ? "var(--bad)" : level === "warn" ? "var(--warn)" : "var(--muted)";
    }

    function esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;" }[c]));
    }

    function num(v, d = 0) {
      const n = Number(v);
      return Number.isFinite(n) ? n : d;
    }

    function fmtTime(v) {
      if (!v) return "-";
      const d = new Date(v);
      if (!Number.isFinite(d.getTime())) return String(v);
      return d.toLocaleString("zh-CN", { hour12: false });
    }

    function pickTokenRefreshAt(row) {
      const item = (row && typeof row === "object") ? row : {};
      const keys = [
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
      ];
      for (const k of keys) {
        const v = item[k];
        if (v !== undefined && v !== null && String(v).trim()) return v;
      }
      const token = item.token;
      if (token && typeof token === "object") {
        for (const k of keys) {
          const v = token[k];
          if (v !== undefined && v !== null && String(v).trim()) return v;
        }
      }
      return null;
    }

    function statusText(v) {
      const raw = String(v || "").trim();
      if (!raw) return "-";
      const key = raw.toLowerCase();
      const map = {
        success: "成功",
        succeeded: "成功",
        ok: "成功",
        active: "可用",
        enabled: "已启用",
        inactive: "未激活",
        disabled: "已禁用",
        expired: "已过期",
        invalid: "无效",
        unavailable: "不可用",
        paused: "已暂停",
        revoked: "已撤销",
        failed: "失败",
        fail: "失败",
        error: "失败",
        partial: "部分成功",
        running: "运行中",
        pending: "等待中",
        queued: "排队中",
        skipped: "已跳过",
        skip: "已跳过",
        idle: "空闲",
        unknown: "未知",
      };
      return map[key] || raw;
    }

    function statusClass(v) {
      const key = String(v || "").trim().toLowerCase();
      if (["success", "succeeded", "ok", "active", "enabled"].includes(key)) return "good";
      if (["failed", "fail", "error", "invalid", "unavailable", "revoked"].includes(key)) return "bad";
      if (["partial", "running", "pending", "queued", "skipped", "skip", "inactive", "disabled", "expired", "paused"].includes(key)) return "warn";
      return "";
    }

    async function api(path, opts = {}) {
      const r = await fetch(path, opts);
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(d.message || `${r.status} ${r.statusText}`);
      return d;
    }

    function renderStatus(d) {
      $("syncRunning").textContent = d.running ? "运行中" : "空闲";
      $("syncRunning").className = d.running ? "good" : "";
      $("syncType").textContent = d.sync_type || "-";
      $("nextRunAt").textContent = fmtTime(d.next_run_at);
      const lc = d.last_cycle || {};
      $("lastCycleStatus").textContent = statusText(lc.status);
      $("lastCycleStatus").className = statusClass(lc.status);
      $("lastCycleSummary").textContent = `ok=${lc.ok || 0} fail=${lc.fail || 0} skip=${lc.skipped || 0}`;
      $("lastCycleDuration").textContent = `${lc.duration_ms || 0} ms`;
    }

    function renderRecords(rows, pageData = {}) {
      const tb = $("recordsTbody");
      const list = Array.isArray(rows) ? rows : [];
      const page = Number(pageData.page || recordPage || 1);
      const totalPages = Number(pageData.total_pages || 1);
      const total = Number(pageData.total || list.length || 0);
      $("recordPageInfo").textContent = `第 ${page} / ${totalPages} 页，共 ${total} 条`;
      $("recordPrevBtn").disabled = page <= 1;
      $("recordNextBtn").disabled = page >= totalPages;
      if (!list.length) {
        tb.innerHTML = `<tr><td colspan="5" style="color:var(--muted);">暂无记录</td></tr>`;
        return;
      }
      tb.innerHTML = list.map((r) => {
        const summary = r.file || (r.kind === "cycle"
          ? `total=${r.total || 0}, ok=${r.ok || 0}, fail=${r.fail || 0}, skip=${r.skipped || 0}`
          : (r.account_name || "-"));
        const sClass = statusClass(r.status);
        return `<tr>
          <td class="mono">${esc(fmtTime(r.ts))}</td>
          <td>${esc(r.kind || "-")}</td>
          <td class="${sClass}">${esc(statusText(r.status))}</td>
          <td>${esc(summary)}</td>
          <td>${esc(r.message || "-")}</td>
        </tr>`;
      }).join("");
    }

    function renderFileRecords(rows, pageData = {}) {
      const tb = $("fileRecordsTbody");
      const list = Array.isArray(rows) ? rows : [];
      const page = Number(pageData.page || fileRecordPage || 1);
      const totalPages = Number(pageData.total_pages || 1);
      const total = Number(pageData.total || list.length || 0);
      $("fileRecordPageInfo").textContent = `第 ${page} / ${totalPages} 页，共 ${total} 条`;
      $("fileRecordPrevBtn").disabled = page <= 1;
      $("fileRecordNextBtn").disabled = page >= totalPages;
      if (!list.length) {
        tb.innerHTML = `<tr><td colspan="7" style="color:var(--muted);">暂无文件同步记录</td></tr>`;
        return;
      }
      tb.innerHTML = list.map((r) => {
        const sClass = statusClass(r.status);
        return `<tr>
          <td class="mono">${esc(fmtTime(r.ts))}</td>
          <td class="mono">${esc(r.file_name || "-")}</td>
          <td>${esc(r.trigger || "-")}</td>
          <td class="${sClass}">${esc(statusText(r.status))}</td>
          <td class="${r.synced_to_sub2 ? "good" : "warn"}">${r.synced_to_sub2 ? "是" : "否"}</td>
          <td class="mono">${esc(fmtTime(r.sync_time))}</td>
          <td>${esc(r.message || "-")}</td>
        </tr>`;
      }).join("");
    }

    function renderAuthFiles() {
      const tb = $("authFilesTbody");
      if (!Array.isArray(authFiles) || !authFiles.length) {
        tb.innerHTML = `<tr><td colspan="10" style="color:var(--muted);">暂无认证文件，请先手动获取。</td></tr>`;
        const tsText = lastAuthFilesFetchedAt ? `上次获取认证文件列表时间：${fmtTime(lastAuthFilesFetchedAt)}。` : "";
        $("authFilesHint").textContent = `当前列表为空。${tsText}`;
        return;
      }
      function buildGroupOptions(selectedIds) {
        const selectedSet = new Set((selectedIds || []).map((x) => Number(x || 0)).filter((x) => x > 0));
        return (sub2Groups || []).map((g) => {
          const gid = Number(g.id || 0);
          const label = String(g.name || `group-${gid}`);
          const platform = String(g.platform || "").trim();
          const status = String(g.status || "").trim();
          const suffix = [platform, status].filter(Boolean).join("/");
          const selected = selectedSet.has(gid) ? " selected" : "";
          return `<option value="${gid}"${selected}>${esc(`${label}${suffix ? ` (${suffix})` : ""}`)}</option>`;
        }).join("");
      }
      tb.innerHTML = authFiles.map((row) => {
        const name = String(row.name || "");
        const checked = selectedAuthFiles.has(name) ? "checked" : "";
        const statusLabel = statusText(row.status);
        const rowStatusClass = statusClass(row.status);
        const provider = row.provider_detected || row.provider || row.type || "-";
        const syncEnabled = !!row.sync_enabled;
        const syncedText = row.synced_to_sub2 ? "是" : "否";
        const syncedClass = row.synced_to_sub2 ? "good" : "warn";
        const tokenRefreshTimeRaw = pickTokenRefreshAt(row) || row.cached_fetched_at || lastAuthFilesFetchedAt;
        const fetchedTime = tokenRefreshTimeRaw ? fmtTime(tokenRefreshTimeRaw) : "-";
        const syncTime = row.sync_time ? fmtTime(row.sync_time) : "-";
        const gids = Array.isArray(row.target_group_ids) ? row.target_group_ids.map((x) => Number(x || 0)).filter((x) => x > 0) : [];
        return `<tr>
          <td><input type="checkbox" data-file-name="${esc(name)}" ${checked} /></td>
          <td class="mono">${esc(name)}</td>
          <td>${esc(provider)}</td>
          <td class="mono">${esc(fetchedTime)}</td>
          <td>
            <select data-target-group-name="${esc(name)}" multiple size="4" style="min-width:200px; height:auto; min-height:96px;">
              ${buildGroupOptions(gids)}
            </select>
          </td>
          <td class="${syncedClass}">${syncedText}</td>
          <td class="mono">${esc(syncTime)}</td>
          <td class="${rowStatusClass}">${esc(statusLabel)}</td>
          <td><input type="checkbox" data-sync-enabled-name="${esc(name)}" ${syncEnabled ? "checked" : ""} /></td>
          <td><button class="btn-primary" style="height:28px;padding:0 8px;font-size:12px;" data-file-act="sync" data-file-name="${esc(name)}">立即同步</button></td>
        </tr>`;
      }).join("");
      const tsText = lastAuthFilesFetchedAt ? `，上次获取认证文件列表时间：${fmtTime(lastAuthFilesFetchedAt)}` : "";
      const gText = sub2GroupsLoadedAt ? `，分组加载时间：${fmtTime(sub2GroupsLoadedAt)}` : "";
      $("authFilesHint").textContent = `共 ${authFiles.length} 个文件，已选择 ${selectedAuthFiles.size} 个${tsText}${gText}。`;
    }

    async function reloadStatus() {
      const d = await api("/api/auth-sync/status");
      renderStatus(d.data || {});
    }

    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function waitManualSyncDone(timeoutMs = 120000, intervalMs = 1500) {
      const started = Date.now();
      let seenRunning = false;
      while (Date.now() - started < timeoutMs) {
        const d = await api("/api/auth-sync/status");
        const data = d.data || {};
        renderStatus(data);
        if (data.running) {
          seenRunning = true;
        } else {
          const lc = data.last_cycle || {};
          if (seenRunning || lc.trigger === "manual") return lc;
        }
        await sleep(intervalMs);
      }
      return null;
    }

    async function runSync() {
      await api("/api/auth-sync/run", { method: "POST" });
      setStatus("已触发手动同步，完成后将自动刷新列表。");
      await reloadStatus();
      const lastCycle = await waitManualSyncDone();
      await Promise.all([reloadStatus(), reloadRecords(), reloadFileRecords(), fetchAuthFiles(true)]);
      if (lastCycle && lastCycle.status === "success") {
        setStatus("手动同步成功，列表已自动刷新。");
      }
    }

    async function reloadRecords(resetPage = false) {
      const nextSize = Math.max(1, Math.min(2000, num($("recordPageSize").value, 50)));
      if (resetPage || nextSize !== recordPageSize) recordPage = 1;
      recordPageSize = nextSize;
      const d = await api(`/api/auth-sync/records?page=${encodeURIComponent(recordPage)}&page_size=${encodeURIComponent(recordPageSize)}`);
      const pageData = (d.data && typeof d.data === "object") ? d.data : {};
      const totalPages = Math.max(1, Number(pageData.total_pages || 1));
      if (recordPage > totalPages) {
        recordPage = totalPages;
        const d2 = await api(`/api/auth-sync/records?page=${encodeURIComponent(recordPage)}&page_size=${encodeURIComponent(recordPageSize)}`);
        const pageData2 = (d2.data && typeof d2.data === "object") ? d2.data : {};
        renderRecords(Array.isArray(pageData2.items) ? pageData2.items : [], pageData2);
        return;
      }
      renderRecords(Array.isArray(pageData.items) ? pageData.items : [], pageData);
    }

    async function reloadFileRecords(resetPage = false) {
      const nextSize = Math.max(1, Math.min(2000, num($("fileRecordPageSize").value, 50)));
      if (resetPage || nextSize !== fileRecordPageSize) fileRecordPage = 1;
      fileRecordPageSize = nextSize;
      const d = await api(`/api/auth-sync/file-records?page=${encodeURIComponent(fileRecordPage)}&page_size=${encodeURIComponent(fileRecordPageSize)}`);
      const pageData = (d.data && typeof d.data === "object") ? d.data : {};
      const totalPages = Math.max(1, Number(pageData.total_pages || 1));
      if (fileRecordPage > totalPages) {
        fileRecordPage = totalPages;
        const d2 = await api(`/api/auth-sync/file-records?page=${encodeURIComponent(fileRecordPage)}&page_size=${encodeURIComponent(fileRecordPageSize)}`);
        const pageData2 = (d2.data && typeof d2.data === "object") ? d2.data : {};
        renderFileRecords(Array.isArray(pageData2.items) ? pageData2.items : [], pageData2);
        return;
      }
      renderFileRecords(Array.isArray(pageData.items) ? pageData.items : [], pageData);
    }

    async function fetchAuthFiles(silent = false) {
      const d = await api("/api/auth-sync/files");
      authFiles = Array.isArray(d.data) ? d.data : [];
      lastAuthFilesFetchedAt = d.last_fetched_at || new Date().toISOString();
      const validNames = new Set(authFiles.map((x) => String(x.name || "")).filter(Boolean));
      [...selectedAuthFiles].forEach((name) => { if (!validNames.has(name)) selectedAuthFiles.delete(name); });
      renderAuthFiles();
      if (!silent) {
        setStatus(`认证文件列表已刷新，共 ${authFiles.length} 个。`);
      }
    }

    async function setFilesSyncEnabled(files, enabled) {
      const names = Array.isArray(files) ? files.map((x) => String(x || "").trim()).filter(Boolean) : [];
      if (!names.length) return;
      await api("/api/auth-sync/files-enabled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: names, enabled: !!enabled }),
      });
      const nameSet = new Set(names);
      authFiles = authFiles.map((row) => {
        const name = String((row && row.name) || "").trim();
        if (!nameSet.has(name)) return row;
        return { ...row, sync_enabled: !!enabled };
      });
      renderAuthFiles();
    }

    async function loadLastAuthFiles() {
      const d = await api("/api/auth-sync/files-last");
      authFiles = Array.isArray(d.data) ? d.data : [];
      lastAuthFilesFetchedAt = d.last_fetched_at || null;
      const validNames = new Set(authFiles.map((x) => String(x.name || "")).filter(Boolean));
      [...selectedAuthFiles].forEach((name) => { if (!validNames.has(name)) selectedAuthFiles.delete(name); });
      renderAuthFiles();
    }

    function renderSub2GroupOptions() {
      const sel = $("bulkTargetGroupSelect");
      if (!sel) return;
      const options = (sub2Groups || []).map((g) => {
        const gid = Number(g.id || 0);
        const label = String(g.name || `group-${gid}`);
        const platform = String(g.platform || "").trim();
        const status = String(g.status || "").trim();
        const suffix = [platform, status].filter(Boolean).join("/");
        return `<option value="${gid}">${esc(`${label}${suffix ? ` (${suffix})` : ""}`)}</option>`;
      });
      sel.innerHTML = options.join("");
    }

    async function fetchSub2Groups(silent = false) {
      if (!silent) setStatus("正在获取 sub2 分组列表...");
      const d = await api("/api/auth-sync/sub2-groups");
      sub2Groups = Array.isArray(d.data) ? d.data : [];
      sub2GroupsLoadedAt = new Date().toISOString();
      renderSub2GroupOptions();
      renderAuthFiles();
      if (!silent) setStatus(`sub2 分组已加载：${sub2Groups.length} 个。`);
    }

    async function setFilesTargetGroups(files, targetGroupIds) {
      const names = Array.isArray(files) ? files.map((x) => String(x || "").trim()).filter(Boolean) : [];
      if (!names.length) return;
      const gids = Array.isArray(targetGroupIds) ? targetGroupIds.map((x) => Number(x || 0)).filter((x) => x > 0) : [];
      await api("/api/auth-sync/files-groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: names, target_group_ids: gids }),
      });
      const nameSet = new Set(names);
      authFiles = authFiles.map((row) => {
        const name = String((row && row.name) || "").trim();
        if (!nameSet.has(name)) return row;
        return { ...row, target_group_ids: gids };
      });
      renderAuthFiles();
    }

    async function applySelectedGroupToCheckedFiles() {
      const selected = [...$("bulkTargetGroupSelect").selectedOptions].map((x) => Number(x.value || 0)).filter((x) => x > 0);
      if (!selectedAuthFiles.size) {
        setStatus("请先勾选至少一个认证文件。", "warn");
        return;
      }
      const files = [...selectedAuthFiles];
      await setFilesTargetGroups(files, selected);
      setStatus(`已为 ${files.length} 个文件${selected.length ? `设置 ${selected.length} 个分组` : "清空分组"}。`);
    }

    function selectAllFiles() {
      selectedAuthFiles.clear();
      for (const row of authFiles) {
        const name = String((row && row.name) || "").trim();
        if (name) selectedAuthFiles.add(name);
      }
      renderAuthFiles();
    }

    function clearSelectedFiles() {
      selectedAuthFiles.clear();
      renderAuthFiles();
    }

    async function syncSelectedFiles(filesInput = null) {
      const files = Array.isArray(filesInput) ? filesInput : [...selectedAuthFiles];
      if (!files.length) {
        setStatus("请先勾选至少一个认证文件。", "warn");
        return;
      }
      const nameSet = new Set(files.map((x) => String(x || "").trim()).filter(Boolean));
      const groupIdsByFile = {};
      for (const row of authFiles) {
        const name = String((row && row.name) || "").trim();
        if (!name || !nameSet.has(name)) continue;
        const gids = Array.isArray(row.target_group_ids) ? row.target_group_ids.map((x) => Number(x || 0)).filter((x) => x > 0) : [];
        if (gids.length) groupIdsByFile[name] = gids;
      }
      const d = await api("/api/auth-sync/sync-selected", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files, group_ids_by_file: groupIdsByFile }),
      });
      const s = d.data || {};
      setStatus(`选中文件同步完成：total=${s.total || 0}, ok=${s.ok || 0}, fail=${s.fail || 0}, skip=${s.skipped || 0}`);
      await Promise.all([reloadStatus(), reloadRecords(), reloadFileRecords(), fetchAuthFiles()]);
    }

    $("runSyncBtn").addEventListener("click", () => runSync().catch((e) => setStatus(`触发失败: ${e.message}`, "error")));
    $("reloadStatusBtn").addEventListener("click", () => reloadStatus().catch((e) => setStatus(`状态刷新失败: ${e.message}`, "error")));
    $("reloadCycleRecordsBtn").addEventListener("click", () => reloadRecords().catch((e) => setStatus(`周期记录刷新失败: ${e.message}`, "error")));
    $("fetchAuthFilesBtn").addEventListener("click", () => fetchAuthFiles().catch((e) => setStatus(`获取认证文件失败: ${e.message}`, "error")));
    $("fetchSub2GroupsBtn").addEventListener("click", () => fetchSub2Groups().catch((e) => setStatus(`获取 sub2 分组失败: ${e.message}`, "error")));
    $("selectAllFilesBtn").addEventListener("click", selectAllFiles);
    $("clearSelectedFilesBtn").addEventListener("click", clearSelectedFiles);
    $("applySelectedGroupBtn").addEventListener("click", () => applySelectedGroupToCheckedFiles().catch((e) => setStatus(`应用分组失败: ${e.message}`, "error")));
    $("recordPageSize").addEventListener("change", () => reloadRecords(true).catch((e) => setStatus(`记录刷新失败: ${e.message}`, "error")));
    $("recordPrevBtn").addEventListener("click", () => {
      if (recordPage <= 1) return;
      recordPage -= 1;
      reloadRecords().catch((e) => setStatus(`记录刷新失败: ${e.message}`, "error"));
    });
    $("recordNextBtn").addEventListener("click", () => {
      recordPage += 1;
      reloadRecords().catch((e) => setStatus(`记录刷新失败: ${e.message}`, "error"));
    });
    $("reloadFileRecordsBtn").addEventListener("click", () => reloadFileRecords().catch((e) => setStatus(`文件记录刷新失败: ${e.message}`, "error")));
    $("fileRecordPageSize").addEventListener("change", () => reloadFileRecords(true).catch((e) => setStatus(`文件记录刷新失败: ${e.message}`, "error")));
    $("fileRecordPrevBtn").addEventListener("click", () => {
      if (fileRecordPage <= 1) return;
      fileRecordPage -= 1;
      reloadFileRecords().catch((e) => setStatus(`文件记录刷新失败: ${e.message}`, "error"));
    });
    $("fileRecordNextBtn").addEventListener("click", () => {
      fileRecordPage += 1;
      reloadFileRecords().catch((e) => setStatus(`文件记录刷新失败: ${e.message}`, "error"));
    });
    $("syncSelectedFilesBtn").addEventListener("click", () => syncSelectedFiles().catch((e) => setStatus(`同步选中文件失败: ${e.message}`, "error")));
    $("themeMode").addEventListener("change", () => applyTheme($("themeMode").value));
    document.addEventListener("mousedown", (e) => {
      const opt = e.target && e.target.closest ? e.target.closest("option") : null;
      if (!opt) return;
      const sel = opt.parentElement;
      if (!sel || sel.tagName !== "SELECT" || !sel.multiple) return;
      const isTarget = sel.id === "bulkTargetGroupSelect" || !!sel.getAttribute("data-target-group-name");
      if (!isTarget) return;
      e.preventDefault();
      opt.selected = !opt.selected;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    $("authFilesTbody").addEventListener("change", (e) => {
      const el = e.target;
      if (!el || el.tagName !== "INPUT" || el.type !== "checkbox") return;
      const syncName = String(el.getAttribute("data-sync-enabled-name") || "").trim();
      if (syncName) {
        setFilesSyncEnabled([syncName], el.checked).catch((er) => {
          el.checked = !el.checked;
          setStatus(`更新运行同步失败: ${er.message}`, "error");
        });
        return;
      }
      const name = String(el.getAttribute("data-file-name") || "").trim();
      if (!name) return;
      if (el.checked) selectedAuthFiles.add(name);
      else selectedAuthFiles.delete(name);
      $("authFilesHint").textContent = `共 ${authFiles.length} 个文件，已选择 ${selectedAuthFiles.size} 个。`;
    });
    $("authFilesTbody").addEventListener("change", (e) => {
      const el = e.target;
      if (!el || el.tagName !== "SELECT") return;
      const name = String(el.getAttribute("data-target-group-name") || "").trim();
      if (!name) return;
      const gids = [...el.selectedOptions].map((x) => Number(x.value || 0)).filter((x) => x > 0);
      setFilesTargetGroups([name], gids).catch((er) => setStatus(`更新目标分组失败: ${er.message}`, "error"));
    });
    $("authFilesTbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-file-act]");
      if (!btn) return;
      const act = String(btn.getAttribute("data-file-act") || "");
      const name = String(btn.getAttribute("data-file-name") || "").trim();
      if (!name) return;
      if (act === "sync") {
        syncSelectedFiles([name]).catch((er) => setStatus(`同步选中文件失败: ${er.message}`, "error"));
      }
    });
    $("themeLightBtn").addEventListener("click", () => {
      $("themeMode").value = "light";
      applyTheme("light");
    });
    $("themeDarkBtn").addEventListener("click", () => {
      $("themeMode").value = "dark";
      applyTheme("dark");
    });
    $("themeAutoBtn").addEventListener("click", () => {
      $("themeMode").value = "auto";
      applyTheme("auto");
    });

    if (window.matchMedia) {
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      const onSystemTheme = () => {
        const mode = (() => { try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (_) { return "auto"; } })();
        if (mode === "auto") applyTheme("auto");
      };
      if (media.addEventListener) media.addEventListener("change", onSystemTheme);
      else if (media.addListener) media.addListener(onSystemTheme);
    }

    (async function init() {
      try {
        const mode = (() => { try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (_) { return "auto"; } })();
        applyTheme(mode);
        await Promise.all([reloadStatus(), reloadRecords(), reloadFileRecords()]);
        await fetchSub2Groups(true);
        await loadLastAuthFiles();
        setInterval(() => reloadStatus().catch(() => {}), 5000);
        setInterval(() => reloadRecords().catch(() => {}), 7000);
        setInterval(() => reloadFileRecords().catch(() => {}), 9000);
      } catch (e) {
        setStatus(`初始化失败: ${e.message}`, "error");
      }
    })();
  