/**
 * pcbackup 前端 — PC 目录增量备份
 *
 * 流程：选目录（webkitdirectory / 拖拽）→ POST scan 生成增量计划
 *      → 先批量标记 .del → 逐文件分块 PUT 上传（断点续传）→ commit → finish
 */
(function () {
  "use strict";

  var CHUNK_SIZE = 8 * 1024 * 1024;   // 8 MiB / 块
  var BRIDGE_MAX_FILE = 64 * 1024 * 1024;
  var RETRY_MAX = 3;
  var LOG_KEEP = 400;
  var DEFAULT_EXCLUDES = [
    "Thumbs.db", "desktop.ini", ".DS_Store", "~$*",
    "node_modules/", ".pnpm-store/", ".venv/", "venv/", ".gradle/",
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    ".tox/", ".nox/", ".next/", ".nuxt/", ".turbo/",
    "coverage/", "htmlcov/", "test-results/", "playwright-report/",
    "*.pyc", "*.pyo", ".coverage", ".coverage.*", "*.tsbuildinfo"
  ];

  var state = {
    jobs: [],
    config: null,       // {data_root, ...} 来自 /api/config
    job: null,          // 当前任务（备份/历史/文件 共用）
    entries: null,      // [{rel, file}] 本次挑选的文件
    folder: "",         // 挑选的根目录名
    plan: null,
    fileMap: {},        // rel -> File
    sourcePath: "",      // 原生读取的 PC 根目录；经典 FileList 模式为空
    resumeRunId: null,
    resumeInfo: null,
    isResuming: false,
    discardResumeOnRun: false,
    resumeNoticeShown: false,
    cancel: false,
    running: false,
    editJobId: null,    // 正在编辑的任务 id（null=新建）
    browseSub: ""
  };

  /* ── 工具 ─────────────────────────────── */

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function fmtBytes(n) {
    if (!n || n < 0) return "0 B";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
  }

  function fmtTime(sec) {
    if (!sec) return "从未";
    var d = new Date(sec * 1000);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString();
  }

  // 桌面端 pathname 可能带盘符前缀（/D:/plugin/...），必须正则提取 /plugin/<uid>/<plugin>
  function apiUrl(ep) {
    var path = window.location.pathname;
    var m = path.match(/(\/plugin\/[^\/]+\/[^\/]+)/);
    var base = m ? m[1] : path.replace(/\/index\.html$/, "").replace(/\/$/, "");
    return base + "/" + ep;
  }

  function api(method, ep, body) {
    var opt = { method: method, cache: "no-store" };
    if (body !== undefined) {
      opt.headers = { "Content-Type": "application/json" };
      opt.body = JSON.stringify(body);
    }
    return fetch(apiUrl(ep), opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || (j.code !== undefined && j.code !== 0)) {
          var err = new Error(j.message || ("HTTP " + r.status));
          err.code = j.code; err.data = j.data;
          throw err;
        }
        return j.data;
      });
    });
  }

  function toast(msg, isErr) {
    var t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    $("toasts").appendChild(t);
    setTimeout(function () { t.remove(); }, isErr ? 5000 : 2500);
  }

  // 任务目录显示：优先「我的数据/子目录」，旧任务回退绝对路径
  function destLabel(j) {
    return j.dest_sub ? "我的数据/" + j.dest_sub : j.destination;
  }

  // 选目录/比对过程的状态提示（常驻，直到清除）
  function setPickStatus(text) {
    var box = $("pickStatus");
    if (!text) { box.hidden = true; return; }
    box.hidden = false;
    $("pickStatusText").textContent = text;
  }

  function ask(title, text, options) {
    return new Promise(function (resolve) {
      options = options || {};
      $("askTitle").textContent = title;
      $("askText").textContent = text;
      $("askOk").textContent = options.okText || "确认";
      $("askOk").className = options.danger ? "btn danger" : "btn primary";
      $("askModal").hidden = false;
      $("askOk").onclick = function () { $("askModal").hidden = true; resolve(true); };
      $("askNo").onclick = function () { $("askModal").hidden = true; resolve(false); };
    });
  }

  // 彻底删除警告：勾选且有待删文件时红字常显，数量随计划实时同步
  // 彻底删除警告：勾选且有待删文件时红字常显，数量随计划实时同步
  // 无待删文件时：复选框禁用、强制取消勾选、警告隐藏
  function syncPruneWarn() {
    var n = state.plan ? (state.plan.excluded_kept || 0) : 0;
    var chk = $("chkPruneExcluded");
    var w = $("pruneWarn");
    if (n <= 0) {
      chk.checked = false;
      chk.disabled = true;
      w.hidden = true;
      return;
    }
    chk.disabled = false;
    if (chk.checked) {
      w.textContent = "⚠ NAS 上这 " + n + " 个文件将被永久删除（不可恢复），开始备份时一并执行";
      w.hidden = false;
    } else {
      w.hidden = true;
    }
  }

  function show(view) {
    ["view-jobs", "view-backup", "view-history", "view-browse"].forEach(function (v) {
      $(v).hidden = (v !== view);
    });
  }

  /* ── 任务列表 ──────────────────────────── */

  function loadJobs() {
    return api("GET", "api/jobs").then(function (list) {
      state.jobs = list || [];
      renderJobs();
      $("connText").textContent = "已连接";
    }).catch(function (e) {
      $("connText").textContent = "连接失败";
      $("connTag").querySelector(".dot").className = "dot bad";
      $("jobList").innerHTML = '<p class="empty">无法连接后端服务：' + esc(e.message) + "</p>";
    });
  }

  function renderJobs() {
    var box = $("jobList");
    if (!state.jobs.length) {
      box.innerHTML = '<p class="empty">还没有任务，点击右上角「新建任务」开始</p>';
      return;
    }
    box.innerHTML = state.jobs.map(function (j) {
      var last = j.last_stats || {};
      return (
        '<div class="card pad job-card">' +
          '<div class="job-top"><h3>' + esc(j.name) + '</h3>' +
            '<span class="pill">' + esc(j.folder || "未绑定目录") + "</span></div>" +
          '<p class="meta muted ellipsis" title="' + esc(j.destination) + '">📁 ' + esc(destLabel(j)) + "</p>" +
          '<div class="job-stats">' +
            (j.resume_available ? '<span class="pill warn">上次备份未完成</span>' : '') +
            '<span class="pill ok">' + j.files + " 个文件</span>" +
            '<span class="pill">' + fmtBytes(j.bytes) + "</span>" +
            '<span class="pill warn">上次：' + (j.last_run ? fmtTime(j.last_run) : "从未") + "</span>" +
          "</div>" +
          '<p class="meta muted">上次结果：上传 ' + (last.uploaded || 0) + " · 跳过 " + (last.skipped || 0) +
            " · 标记删除 " + (last.deleted || 0) + "</p>" +
          '<div class="job-actions">' +
            '<button class="btn primary" data-act="backup" data-id="' + j.id + '">' +
              (j.resume_available ? '继续备份' : '开始备份') + '</button>' +
            '<button class="btn" data-act="files" data-id="' + j.id + '">文件</button>' +
            '<button class="btn" data-act="history" data-id="' + j.id + '">历史</button>' +
            '<button class="btn" data-act="edit" data-id="' + j.id + '">编辑</button>' +
            '<button class="btn danger-ghost" data-act="delete" data-id="' + j.id + '">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("");

    box.querySelectorAll("button[data-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        var job = state.jobs.find(function (j) { return j.id === b.dataset.id; });
        if (!job) return;
        var act = b.dataset.act;
        if (act === "backup") openBackup(job);
        else if (act === "files") openBrowse(job);
        else if (act === "history") openHistory(job);
        else if (act === "edit") openJobModal(job);
        else if (act === "delete") deleteJob(job, b);
      });
    });
    if (!state.resumeNoticeShown && state.jobs.some(function (j) { return j.resume_available; })) {
      state.resumeNoticeShown = true;
      toast("检测到未完成备份，可在任务卡片继续");
    }
  }

  function deleteJob(job, button) {
    if (button.disabled) return;
    button.disabled = true;
    ask("删除任务？", "确认删除「" + job.name + "」？任务配置和备份清单会移除，NAS 上已备份的文件会保留。",
        { okText: "删除任务", danger: true })
      .then(function (yes) {
        if (!yes) { button.disabled = false; return; }
        api("DELETE", "api/jobs/" + encodeURIComponent(job.id))
          .then(function () {
            toast("任务已删除，NAS 上的备份文件仍保留");
            loadJobs();
          })
          .catch(function (e) {
            button.disabled = false;
            toast("删除任务失败：" + e.message, true);
          });
      });
  }

  /* ── 任务编辑弹窗 ──────────────────────── */

  function openJobModal(job) {
    state.editJobId = job ? job.id : null;
    $("jobModalTitle").textContent = job ? "编辑任务" : "新建任务";
    $("fJobName").value = job ? job.name : "";
    $("fJobSub").value = (job && job.dest_sub) || "pcbackup/";
    $("fJobExclude").value = ((job && job.exclude) || DEFAULT_EXCLUDES).join("\n");
    updateDestPreview();
    $("jobModal").hidden = false;
  }

  function updateDestPreview() {
    var root = (state.config && state.config.data_root) || "…";
    var sub = $("fJobSub").value.trim().replace(/^\/+|\/+$/g, "");
    $("fJobDestPreview").textContent = sub ? root + "/" + sub : root + "/";
  }

  function saveJobModal() {
    var name = $("fJobName").value.trim();
    var sub = $("fJobSub").value.trim().replace(/^\/+|\/+$/g, "");
    var exclude = $("fJobExclude").value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    if (!name) { toast("请填写任务名称", true); return; }
    if (!sub) { toast("请填写「我的数据」下的子目录", true); return; }
    var body = { name: name, dest_sub: sub, exclude: exclude };
    // 兼容：老任务的绝对路径仅在不填子目录时保留（正常场景不会发生）
    var p = state.editJobId
      ? api("PATCH", "api/jobs/" + state.editJobId, body)
      : api("POST", "api/jobs", body);
    p.then(function () {
      $("jobModal").hidden = true;
      toast("已保存");
      loadJobs();
    }).catch(function (e) { toast(e.message, true); });
  }

  /* ── 备份流程 ──────────────────────────── */

  function openBackup(job) {
    state.job = job;
    state.entries = null; state.plan = null; state.folder = "";
    state.sourcePath = ""; state.resumeRunId = null;
    state.resumeInfo = null; state.isResuming = false;
    state.discardResumeOnRun = false;
    $("bkJobName").textContent = job.name;
    $("bkJobDest").textContent = "📁 " + destLabel(job);
    $("pickCount").textContent = "未选择";
    $("pickInfo").hidden = true;
    $("pickExclude").textContent = "排除规则：" + ((job.exclude || []).join("、") || "无");
    // 记住的 PC 路径：只预填 + 提示，不自动开始（用户可能只是进来看看/要换目录），
    // 点「确认」或明确选好目录才动
    if ($("npPickPath")) {
      $("npPickPath").value = job.pc_path || "";
      $("npPickHint").textContent = job.pc_path
        ? "上次备份目录：" + job.pc_path + " —— 点「确认」继续，或换目录/重新选择"
        : (getBridge() ? "选择文件夹即开始比对；或输入绝对路径后点「确认」" : "点击上方选择文件夹");
    }
    resetSteps();
    show("view-backup");
    if (job.resume_available) {
      $("stepPick").hidden = true;
      $("stepResume").hidden = false;
      $("resumeSummary").textContent = "正在读取上次备份进度…";
      api("GET", "api/jobs/" + job.id + "/resume").then(function (info) {
        if (state.job !== job) return;
        if (!info || !info.run_id) { $("stepResume").hidden = true; $("stepPick").hidden = false; return; }
        state.resumeInfo = info;
        $("resumeSummary").textContent = "已完成 " + info.plan.previous_uploaded +
          " 个文件，待上传 " + info.files.length + " 个，待处理删除 " +
          info.plan.deleted.length + " 个；可从 NAS 上的未完成分块继续。";
        $("resumePath").textContent = "PC 目录：" + info.source_path;
        $("btnResume").disabled = !getBridge();
        var warning = (info.del_mode === "delete" && info.plan.deleted.length)
          ? "上次选择了直接删除 PC 上已删除的文件。" : "";
        if (info.prune_excluded) warning += "上次选择了清理排除文件。";
        $("resumeHint").textContent = getBridge()
          ? warning + "继续时只处理上次计划；期间新增或删除的文件需下次重新扫描。"
          : "请在小米桌面 App 中打开插件，才能按保存的 PC 路径继续。";
      }).catch(function (e) {
        if (state.job !== job) return;
        state.discardResumeOnRun = true;
        $("stepResume").hidden = true; $("stepPick").hidden = false;
        toast("读取续传记录失败：" + e.message, true);
      });
    }
  }

  function resumeBackup() {
    var info = state.resumeInfo;
    if (!info || !getBridge() || state.running) return;
    state.sourcePath = info.source_path;
    state.resumeRunId = info.run_id;
    state.isResuming = true;
    state.folder = info.plan.folder;
    state.fileMap = {};
    var root = info.source_path.replace(/[\\/]+$/, "");
    info.files.forEach(function (item) {
      state.fileMap[item[0]] = {
        rel: item[0], abs: root + "\\" + item[0].replace(/\//g, "\\"),
        size: item[1], mtime: item[2]
      };
    });
    state.plan = info.plan;
    $("stepResume").hidden = true;
    renderPlan(info.plan);
    $("stepPick").hidden = true;
    var mode = document.querySelector('input[name="delMode"][value="' + info.del_mode + '"]');
    if (mode) mode.checked = true;
    document.querySelectorAll('input[name="delMode"]').forEach(function (r) { r.disabled = true; });
    $("chkPruneExcluded").checked = !!info.prune_excluded;
    syncPruneWarn();
    $("chkPruneExcluded").disabled = true;
    $("btnRun").textContent = "继续备份";
    runUpload();
  }

  function resetSteps() {
    $("stepResume").hidden = true;
    $("stepPlan").hidden = true;
    $("stepRun").hidden = true;
    $("stepDone").hidden = true;
    $("stepPick").hidden = false;
    $("btnRun").textContent = "开始备份";
    $("chkPruneExcluded").checked = false;
    $("chkPruneExcluded").disabled = false;
    document.querySelectorAll('input[name="delMode"]').forEach(function (r) { r.disabled = false; });
    var safeMode = document.querySelector('input[name="delMode"][value="del"]');
    if (safeMode) safeMode.checked = true;
    setPickStatus(null);
  }

  // -- 选择目录 --

  // 大目录逐文件过滤必须分块让出主线程，否则 UI 冻死
  // 选择结果收口（经典 FileList 与原生读取共用）
  // 条目统一形状：{rel, file?|abs?, size, mtime}
  function finishPick(entries, folderName, sourceLabel, excludedCount) {
    if (state.running) { toast("正在传输中，等完成或取消后再重新选择", true); return; }
    if (!entries.length) {
      setPickStatus(null);
      toast("该目录下没有可备份的文件（可能全被排除规则过滤了）", true);
      return;
    }
    entries.sort(function (a, b) { return a.rel < b.rel ? -1 : 1; });
    state.entries = entries;
    state.folder = folderName || "目录";

    var totalBytes = 0;
    state.fileMap = {};
    entries.forEach(function (e) { totalBytes += e.size; state.fileMap[e.rel] = e; });
    $("pickFolder").textContent = state.folder;
    $("pickMeta").textContent = entries.length + " 个文件 · " + fmtBytes(totalBytes) +
      (excludedCount != null ? " · 排除 " + excludedCount + " 项" : "") +
      (sourceLabel ? " · " + sourceLabel : "");
    $("pickInfo").hidden = false;
    $("pickCount").textContent = entries.length + " 个文件";
    resetSteps();
    doScan(false, false);
  }

  function onPicked(files, folderName) {
    state.sourcePath = "";
    var job = state.job;
    var n = files.length;
    var entries = [];
    var seen = {};
    var idx = 0;
    var CHUNK = 3000;

    function step() {
      var end = Math.min(idx + CHUNK, n);
      for (; idx < end; idx++) {
        var f = files[idx];
        var rel = f._relPath || (f.webkitRelativePath || f.name);
        rel = rel.split("/").slice(1).join("/") || rel; // 去掉根目录名
        if (!rel || rel.endsWith("/")) continue;
        if (isExcluded(rel, job.exclude || [])) continue;
        if (seen[rel]) continue;
        seen[rel] = 1;
        entries.push({ rel: rel, file: f, size: f.size, mtime: f.lastModified });
      }
      if (idx < n) {
        setPickStatus("正在筛选文件… " + idx + " / " + n);
        setTimeout(step, 0);
        return;
      }
      finishPick(entries, folderName, "", n - entries.length);
    }

    setPickStatus("正在筛选文件…");
    step();
  }

  function globToRe(pat) {
    var s = pat.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
    return new RegExp("^" + s + "$");
  }

  // 排除规则预编译（缓存）：大目录逐文件匹配不能每次重新编译正则，否则主线程冻死
  var _exclCache = { src: null, dirPats: [], rePats: [] };
  function compileExcludes(pats) {
    var key = JSON.stringify(pats || []);
    if (_exclCache.src === key) return _exclCache;
    var dirPats = [], rePats = [];
    (pats || []).forEach(function (p) {
      p = String(p || "").trim();
      if (!p) return;
      if (p.slice(-1) === "/") dirPats.push(p.slice(0, -1));
      else rePats.push(globToRe(p));
    });
    _exclCache = { src: key, dirPats: dirPats, rePats: rePats };
    return _exclCache;
  }

  function isExcluded(rel, pats) {
    var ex = compileExcludes(pats);
    var segs = rel.split("/");
    var base = segs[segs.length - 1];
    var dirs = segs.slice(0, -1);
    for (var i = 0; i < ex.dirPats.length; i++) {
      if (dirs.indexOf(ex.dirPats[i]) !== -1) return true;
    }
    for (var j = 0; j < ex.rePats.length; j++) {
      if (ex.rePats[j].test(base) || ex.rePats[j].test(rel)) return true;
    }
    return false;
  }

  // -- 增量比对 --

  function doScan(confirm, force) {
    var job = state.job;
    var meta = state.entries.map(function (e) {
      return { p: e.rel, s: e.size, m: e.mtime };
    });
    $("stepPlan").hidden = true;
    state.scanToken = (state.scanToken || 0) + 1;
    setPickStatus("正在发送文件清单（" + meta.length + " 个文件）…");
    api("POST", "api/jobs/" + job.id + "/scan",
        { folder: state.folder, files: meta, confirm: !!confirm, force: !!force,
          replace_resume: state.discardResumeOnRun })
      .then(function () { pollScan(job.id, state.scanToken); })
      .catch(function (e) {
        setPickStatus(null);
        if (e.data && e.data.reason === "folder_mismatch") {
          ask("切换备份目录？", e.data.message).then(function (yes) {
            if (yes) doScan(true, force);
          });
        } else if (e.data && e.data.reason === "deleted_ratio") {
          state.plan = null;
          $("stepPlan").hidden = false;
          $("planPills").innerHTML =
            '<span class="pill danger">' + esc(e.data.message) + "</span>" +
            '<span class="pill">勾选强制并重新比对后，可在下方选择「改名 .del」或「直接删除」</span>';
          $("planUploadList").hidden = true;
          $("planDelList").hidden = true;
          $("excludedRow").hidden = true;
          var dr = $("delConfirmRow");
          dr.hidden = false;
          dr.querySelectorAll('input[name="delMode"]').forEach(function (r) { r.disabled = true; });
          dr.querySelector(".label").textContent = "重新比对出结果后，这里可选择删除处理方式";
          $("forceRow").hidden = false;
          $("btnRun").disabled = true;
          $("chkForce").onchange = function () {
            $("btnRun").disabled = !$("chkForce").checked;
          };
          $("btnRun").onclick = function () {
            if ($("chkForce").checked) doScan(confirm, true);
          };
        } else {
          toast(e.message || "比对失败", true);
        }
      });
  }

  // 轮询异步比对进度（大目录几十秒，必须让用户看到它在动）
  function pollScan(jid, token) {
    if (token !== state.scanToken) return;   // 已重选目录/重开流程，停止旧轮询
    api("GET", "api/jobs/" + jid + "/scan-status").then(function (s) {
      if (token !== state.scanToken) return;
      if (s.phase === "done") {
        state.plan = s.plan;
        renderPlan(s.plan);
        return;
      }
      if (s.phase === "error") {
        setPickStatus(null);
        toast("比对失败：" + (s.error || "未知错误"), true);
        return;
      }
      var txt = {
        queued: "排队中…",
        snapshot: "正在扫描 NAS 备份目录… 已发现 " + s.seen + " 个文件（首次或大目录可能需要一两分钟，请耐心等待）",
        matching: "正在比对 " + s.total + " 个文件…",
        saving: "正在写入清单…"
      }[s.phase] || ("比对中（" + s.phase + "）…");
      setPickStatus(txt);
      setTimeout(function () { pollScan(jid, token); }, 800);
    }).catch(function (e) {
      if (token !== state.scanToken) return;
      setPickStatus(null);
      toast(e.message || "获取比对进度失败", true);
    });
  }

  function renderPlan(plan) {
    setPickStatus(null);
    $("stepPlan").hidden = false;
    $("stepPick").hidden = false;
    $("planTotal").textContent = "共 " + plan.total_files + " 个文件";
    var changed = plan.upload.length - (plan.repaired || 0);
    $("planPills").innerHTML =
      '<span class="pill ok">将上传 ' + plan.upload.length + " 个（" + fmtBytes(plan.upload_bytes) + "）</span>" +
      (plan.adopted ? '<span class="pill ok">已存在认领 ' + plan.adopted + " 个（NAS 上已有且一致，不重传）</span>" : "") +
      '<span class="pill">跳过 ' + plan.skipped + " 个（未变更）</span>" +
      (plan.deleted.length ? '<span class="pill danger">PC 已删除 ' + plan.deleted.length + " 个</span>"
                           : '<span class="pill">无删除</span>') +
      (plan.repaired ? '<span class="pill warn">补传 ' + plan.repaired + " 个（NAS 缺失）</span>" : "");

    fillList($("planUploadList").querySelector(".list-body"), plan.upload.map(function (u) {
      return [u[0], fmtBytes(u[1])];
    }));
    fillList($("planDelList").querySelector(".list-body"), plan.deleted.map(function (p) {
      return [p, ".del"];
    }));
    $("planUploadList").hidden = !plan.upload.length;
    $("planDelList").hidden = !plan.deleted.length;

    var exRow = $("excludedRow");
    if ((plan.excluded_kept || 0) > 0) {
      exRow.hidden = false;
      $("excludedCount").textContent = plan.excluded_kept;
    } else {
      exRow.hidden = true;
    }
    syncPruneWarn();

    // 删除处理选项：常显（无删除项时置灰说明），避免"找不到选项"
    var delRow = $("delConfirmRow");
    var hasDel = plan.deleted.length > 0;
    delRow.hidden = false;
    delRow.querySelectorAll('input[name="delMode"]').forEach(function (r) { r.disabled = !hasDel; });
    delRow.querySelector(".label").textContent = hasDel
      ? "PC 上已删除的 " + plan.deleted.length + " 个文件，在 NAS 端如何处理："
      : "本次没有 PC 上已删除的文件（无需选择处理方式）";
    $("forceRow").hidden = true;
    $("btnRun").disabled = state.running;
    $("btnRun").onclick = runUpload;
  }

  function fillList(box, rows) {
    box.innerHTML = rows.slice(0, 300).map(function (r) {
      return "<div><span class=\"ellipsis\">" + esc(r[0]) + "</span><span>" + esc(r[1]) + "</span></div>";
    }).join("") + (rows.length > 300 ? "<div><span>… 仅显示前 300 项</span><span></span></div>" : "");
  }

  // -- 上传执行 --

  function xhrPut(url, blob, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", apiUrl(url));
      xhr.timeout = 0;
      xhr.upload.onprogress = function (ev) { if (onProgress) onProgress(ev.loaded); };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else {
          var msg = "HTTP " + xhr.status;
          try { msg = JSON.parse(xhr.responseText).message || msg; } catch (e) {}
          var err = new Error(msg); err.status = xhr.status; err.resp = xhr.responseText;
          reject(err);
        }
      };
      xhr.onerror = function () { reject(new Error("网络错误")); };
      xhr.ontimeout = function () { reject(new Error("上传超时")); };
      xhr.send(blob);
    });
  }

  function withRetry(fn, label) {
    var attempt = 0;
    function tryOnce() {
      return fn().catch(function (e) {
        attempt++;
        if (state.cancel || attempt >= RETRY_MAX) throw e;
        log("第 " + attempt + " 次重试 " + label + "：" + e.message, "lg-err");
        return new Promise(function (r) { setTimeout(r, 1000 * Math.pow(2, attempt)); }).then(tryOnce);
      });
    }
    return tryOnce();
  }

  var logBox = null;
  function log(msg, cls) {
    if (!logBox) return;
    var div = document.createElement("div");
    if (cls) div.className = cls;
    div.textContent = msg;
    logBox.appendChild(div);
    while (logBox.childNodes.length > LOG_KEEP) logBox.removeChild(logBox.firstChild);
    logBox.scrollTop = logBox.scrollHeight;
  }

  function runUpload() {
    if (state.running || !state.plan) return;
    if (state.sourcePath && !getPcFs()) {
      var bridge = getBridge();
      if (!bridge || typeof bridge.getPcFileStream !== "function") {
        toast("桌面端无法读取 PC 文件，请点「选择文件夹」重新扫描", true);
        return;
      }
      if (state.plan.upload.some(function (item) { return item[1] > BRIDGE_MAX_FILE; })) {
        toast("原生桥接无法安全读取大文件，请点「选择文件夹」重新扫描", true);
        return;
      }
    }
    if (state.sourcePath && !state.resumeRunId) {
      state.running = true;
      $("btnRun").disabled = true;
      var selectedMode = document.querySelector('input[name="delMode"]:checked');
      api("POST", "api/jobs/" + state.job.id + "/resume", {
        scan_id: state.plan.scan_id,
        source_path: state.sourcePath,
        del_mode: selectedMode ? selectedMode.value : "del",
        prune_excluded: $("chkPruneExcluded").checked,
        replace: state.discardResumeOnRun
      }).then(function (info) {
        state.resumeRunId = info.run_id;
        state.discardResumeOnRun = false;
        state.running = false;
        runUploadActive();
      }).catch(function (e) {
        state.running = false;
        $("btnRun").disabled = false;
        toast("保存续传进度失败：" + e.message, true);
      });
      return;
    }
    runUploadActive();
  }

  function runUploadActive() {
    if (state.running || !state.plan) return;
    var job = state.job, plan = state.plan;
    var delModeEl = document.querySelector('input[name="delMode"]:checked');
    var delMode = delModeEl ? delModeEl.value : "del";       // del=改名保留 / delete=直接删除
    var applyDel = plan.deleted.length > 0 && delMode;
    state.running = true;
    state.cancel = false;
    $("btnRun").disabled = true;
    $("btnCancel").disabled = false;
    // 各步骤顺序向下追加展示，不互相隐藏（与 1→2 的展示逻辑统一）
    $("stepRun").hidden = false;
    $("stepDone").hidden = true;
    $("stepRun").scrollIntoView({ behavior: "smooth", block: "nearest" });
    logBox = $("runLog");
    logBox.innerHTML = "";

    var t0 = Date.now();
    var doneBytes = plan.previous_bytes || 0;
    var okCount = plan.previous_uploaded || 0;
    var totalBytes = (plan.upload_bytes || 0) + doneBytes || 1;
    var totalCount = plan.upload.length + okCount;
    var failed = [], delCount = plan.previous_deleted || 0;
    var speed = { last: t0, bytes: 0, val: 0 };

    function setProgress(phase, curFile) {
      var pct = Math.min(100, doneBytes * 100 / totalBytes);
      $("runBar").style.width = pct.toFixed(1) + "%";
      var elapsed = (Date.now() - t0) / 1000;
      $("runPhase").textContent = phase;
      $("runStats").textContent =
        okCount + " / " + totalCount + " 个文件 · " + fmtBytes(doneBytes) + " / " + fmtBytes(totalBytes) +
        " · " + (speed.val ? fmtBytes(speed.val) + "/s" : "…") +
        (speed.val > 0 ? " · 剩余约 " + Math.max(1, Math.round((totalBytes - doneBytes) / speed.val)) + " 秒" : "");
      if (curFile !== undefined) $("runFile").textContent = curFile || "";
    }
    setProgress("准备…", "");

    function tickSpeed(loadedNow) {
      var now = Date.now();
      speed.bytes += loadedNow;
      if (now - speed.last >= 1000) {
        var v = speed.bytes * 1000 / (now - speed.last);
        speed.val = speed.val ? speed.val * 0.4 + v * 0.6 : v;
        speed.last = now; speed.bytes = 0;
      }
    }

    function uploadOne(item) {
      var rel = item[0];
      var ent = state.fileMap[rel];
      var size = ent.size, mtime = ent.mtime;
      var url = "api/jobs/" + job.id + "/data?path=" + encodeURIComponent(rel) + "&offset=";
      var checkSource = state.isResuming
        ? nativeStat(getBridge(), ent.abs).then(function (st) {
            if (st.size !== size || Math.abs(Math.floor(st.mtimeMs) - mtime) > 1) {
              throw new Error("PC 文件已变化，请重新扫描目录");
            }
          })
        : Promise.resolve();
      return checkSource.then(function () {
        if (state.sourcePath && !state.isResuming) return 0; // 新计划不接旧计划的 .part
        return api("GET", "api/jobs/" + job.id + "/part?path=" + encodeURIComponent(rel))
          .then(function (d) { return (d && d.size && d.size <= size) ? d.size : 0; })
          .catch(function () { return 0; });
      })
        .then(function (offset) {
          if (offset > 0) log("续传 @" + fmtBytes(offset) + " " + rel, "lg-skip");
          doneBytes += offset;
          // 0 字节文件也要 PUT 一个空 .part，否则 commit 会报「分块数据不存在」
          if (size === 0) {
            return withRetry(function () {
              return xhrPut(url + 0, new Blob([]), null);
            }, rel);
          }
          function chunkLoop(off) {
            if (state.cancel) return Promise.reject({ cancelled: true });
            if (off >= size) return Promise.resolve();
            var end = Math.min(off + CHUNK_SIZE, size);
            var base = off;
            return readEntryChunk(ent, base, end).then(function (blob) {
              if (blob.size !== end - base) throw new Error("读取的文件分块长度不符，请重新扫描目录");
              return withRetry(function () {
                return xhrPut(url + base, blob, function (loaded) {
                  tickSpeed(loaded);
                  setProgress("上传中", rel + "（" + fmtBytes(blob.size) + "）");
                });
              }, rel);
            }).then(function () {
              doneBytes += end - base;
              setProgress("上传中", rel);
              return chunkLoop(end);
            });
          }
          return chunkLoop(offset);
        })
        .then(function () {
          return api("POST", "api/jobs/" + job.id + "/commit",
                     { p: rel, s: size, m: mtime });
        })
        .then(function () {
          okCount++;
          log("✓ " + rel, "lg-ok");
        });
    }

    function tombstoneAll() {
      var paths = plan.deleted.slice();
      function batch() {
        if (state.cancel || !paths.length) return Promise.resolve();
        var cur = paths.splice(0, 200);
        return withRetry(function () {
          return api("POST", "api/jobs/" + job.id + "/deleted", { paths: cur, mode: delMode });
        }, "处理删除").then(function (res) {
          (res || []).forEach(function (r) {
            if (r.status === "renamed") { delCount++; log("⟲ " + r.p + " → .del", "lg-del"); }
            else if (r.status === "deleted") { delCount++; log("✂ 已删除 " + r.p, "lg-err"); }
          });
          return batch();
        });
      }
      return batch();
    }

    var chain = Promise.resolve();
    if (applyDel) {
      chain = chain.then(function () {
        setProgress("标记删除…", plan.deleted.length + " 个文件");
        return tombstoneAll();
      });
    }
    chain = chain.then(function () {
      // 文件间并发（4 路，避开浏览器同域连接数上限），单文件内部仍按块串行
      var CONCURRENCY = 4;
      var queue = plan.upload.slice();
      function worker() {
        if (state.cancel || !queue.length) return Promise.resolve();
        var item = queue.shift();
        setProgress("上传中", item[0]);
        return uploadOne(item).catch(function (e) {
          if (e && e.cancelled) return;
          failed.push(item[0]);
          log("✗ " + item[0] + " — " + (e.message || e), "lg-err");
        }).then(worker);
      }
      var workers = [];
      // 不要用 queue.length 当循环条件：worker() 同步 shift 会让队列中途变短，
      // 小批量（≤4 个文件）只能起 1~2 个 worker，退化成串行
      for (var w = 0; w < CONCURRENCY; w++) workers.push(worker());
      return Promise.all(workers);
    });

    chain.then(function () {
      var elapsed = Date.now() - t0;
      setProgress(state.cancel ? "已取消" : "完成", "");
      return api("POST", "api/jobs/" + job.id + "/finish", {
        run_id: state.resumeRunId,
        uploaded: okCount, uploaded_bytes: doneBytes, skipped: plan.skipped,
        adopted: plan.adopted || 0,
        deleted: delCount, failed: failed.length, elapsed_ms: elapsed,
        cancelled: state.cancel,
        prune_excluded: $("chkPruneExcluded").checked && (plan.excluded_kept || 0) > 0
      }).then(function (entry) {
        if (entry.status === "partial" && !failed.length) throw new Error("仍有未完成文件，可再次继续备份");
      });
    }).then(function () {
      state.running = false;
      renderDone(okCount, delCount, failed, plan, state.cancel, Date.now() - t0);
      loadJobs();
    }).catch(function (e) {
      state.running = false;
      toast("备份异常中断：" + (e && e.message || e), true);
    });
  }

  function renderDone(okCount, delCount, failed, plan, cancelled, elapsed) {
    $("stepDone").hidden = false;
    $("stepDone").scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("doneTag").textContent = cancelled ? "已取消" : (failed.length ? "部分失败" : "成功");
    $("doneTag").className = "pill " + (cancelled ? "warn" : failed.length ? "danger" : "ok");
    $("donePills").innerHTML =
      '<span class="pill ok">上传 ' + okCount + " 个</span>" +
      (plan.adopted ? '<span class="pill ok">认领 ' + plan.adopted + " 个</span>" : "") +
      '<span class="pill">跳过 ' + plan.skipped + " 个</span>" +
      '<span class="pill danger">标记删除 ' + delCount + " 个</span>" +
      '<span class="pill warn">耗时 ' + Math.round(elapsed / 1000) + " 秒</span>";
    if (failed.length) {
      $("doneFailed").hidden = false;
      $("doneFailed").textContent = "失败 " + failed.length + " 个：" + failed.slice(0, 10).join("；") +
        (failed.length > 10 ? " 等" : "");
    } else {
      $("doneFailed").hidden = true;
    }
  }

  /* ── 历史与文件浏览 ────────────────────── */

  function openHistory(job) {
    state.job = job;
    $("histJobName").textContent = job.name + " · " + job.destination;
    show("view-history");
    api("GET", "api/jobs/" + job.id + "/history").then(function (list) {
      if (!list || !list.length) {
        $("histBody").innerHTML = '<p class="empty">还没有备份记录</p>';
        return;
      }
      var rows = list.slice().reverse().map(function (h) {
        return "<tr>" +
          "<td>" + fmtTime(h.ts) + "</td>" +
          "<td>上传 " + h.uploaded + "</td>" +
          "<td>跳过 " + h.skipped + "</td>" +
          "<td>删除标记 " + h.deleted + "</td>" +
          "<td>" + fmtBytes(h.uploaded_bytes) + "</td>" +
          "<td>" + Math.round(h.elapsed_ms / 1000) + "s</td>" +
          '<td><span class="pill ' + (h.status === "ok" ? "ok" : "warn") + '">' +
            (h.status === "ok" ? "成功" : h.status === "partial" ? "未完成" : "取消") + "</span></td>" +
          "</tr>";
      }).join("");
      $("histBody").innerHTML =
        '<table class="tbl"><thead><tr><th>时间</th><th>上传</th><th>跳过</th><th>删除标记</th>' +
        "<th>流量</th><th>耗时</th><th>状态</th></tr></thead><tbody>" + rows + "</tbody></table>";
    }).catch(function (e) {
      $("histBody").innerHTML = '<p class="empty">' + esc(e.message) + "</p>";
    });
  }

  function openBrowse(job, sub) {
    state.job = job;
    state.browseSub = sub || "";
    $("brJobName").textContent = job.name + " · " + job.destination;
    show("view-browse");
    api("GET", "api/jobs/" + job.id + "/browse" + (state.browseSub ? "?sub=" + encodeURIComponent(state.browseSub) : ""))
      .then(function (list) {
        renderCrumbs(job, state.browseSub);
        if (!list || !list.length) {
          $("brBody").innerHTML = '<p class="empty">空目录（还没有备份数据）</p>';
          return;
        }
        $("brBody").innerHTML = list.map(function (e) {
          if (e.dir) {
            var sub2 = (state.browseSub ? state.browseSub + "/" : "") + e.name;
            return '<div class="br-row"><span class="name dir" data-sub="' + esc(sub2) + '">📁 ' +
              esc(e.name) + '</span><span class="size">—</span><span></span></div>';
          }
          var p = (state.browseSub ? state.browseSub + "/" : "") + e.name;
          return '<div class="br-row"><span class="name">' + esc(e.name) + "</span>" +
            (e.del ? '<span class="tag-del">.del</span>' : "<span></span>") +
            '<span class="size">' + fmtBytes(e.size) + " · " + new Date(e.mtime * 1000).toLocaleDateString() + "</span>" +
            '<a class="dl" href="' + esc(apiUrl("api/jobs/" + job.id + "/download?path=" + encodeURIComponent(p))) +
            '">下载</a></div>';
        }).join("");
        $("brBody").querySelectorAll(".name.dir").forEach(function (el) {
          el.addEventListener("click", function () { openBrowse(job, el.dataset.sub); });
        });
      })
      .catch(function (e) {
        $("brBody").innerHTML = '<p class="empty">' + esc(e.message) + "</p>";
      });
  }

  function renderCrumbs(job, sub) {
    var parts = sub ? sub.split("/") : [];
    var html = '<a data-sub="">根目录</a>';
    var acc = "";
    parts.forEach(function (p) {
      acc = acc ? acc + "/" + p : p;
      html += '<span class="sep">/</span><a data-sub="' + esc(acc) + '">' + esc(p) + "</a>";
    });
    $("brCrumbs").innerHTML = html;
    $("brCrumbs").querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () { openBrowse(job, a.dataset.sub); });
    });
  }

  /* ── 原生目录读取（桌面 App electronAPI 桥接） ────────────── */
  // 合同（探测台 v3-v5 真机实测）：
  //   getPcFileList({pcPath,tag}, prog, done, fail) → done({folders:[],files:[{fullPath,size,...}]})
  //     一次调用递归返回整树；files[] 无 mtime
  //   getPcFileStats({filepath,tag}, cb) → cb(fs.Stats{mtimeMs,...})   ← 字段是 filepath 不是 path！
  //   getDropFilePaths([...File]) → [绝对路径]                          ← 必须喂真数组
  //   getPcFileStream(path, start, onData, onEnd, onError) → 从 start 流到 EOF
  // 所有调用按 invoke 语义防御：promise 可能先回 undefined、结果稍后走回调。

  function getBridge() {
    try {
      var g = window.microApp ? window.microApp.getGlobalData() : null;
      if (g && g.electronAPI) return g.electronAPI;
    } catch (e) {}
    try {
      var rw = window.__MICRO_APP_WINDOW__ && window.__MICRO_APP_WINDOW__.rawWindow;
      if (rw) {
        for (var k in rw) {
          try { if (rw[k] && rw[k].electronAPI) return rw[k].electronAPI; } catch (e) {}
        }
      }
    } catch (e) {}
    try {
      for (var i = 0; i < window.frames.length; i++) {
        if (window.frames[i] && window.frames[i].electronAPI) return window.frames[i].electronAPI;
      }
    } catch (e) {}
    try { if (window.electronAPI) return window.electronAPI; } catch (e) {}
    return null;
  }

  function nativeListDir(api, pcPath) {
    return new Promise(function (resolve, reject) {
      var done = false;
      function ok(v) { if (!done) { done = true; resolve(v); } }
      function bad(e) { if (!done) { done = true; reject(e instanceof Error ? e : new Error("目录读取失败")); } }
      try {
        var r = api.getPcFileList({ pcPath: pcPath, tag: "lst_" + Date.now() },
          function () {}, ok, bad);
        if (r && typeof r.then === "function") {
          r.then(function (v) { if (v !== undefined) ok(v); }).catch(function () {});
        }
      } catch (e) { bad(e); }
      setTimeout(function () { if (!done) bad(new Error("目录读取超时")); }, 600000);
    });
  }

  function nativeStat(api, absPath) {
    return new Promise(function (resolve, reject) {
      var done = false;
      function fin(v) {
        if (done) return;
        done = true;
        if (v && typeof v.mtimeMs === "number") resolve(v);
        else reject(new Error("stat 无结果"));
      }
      try {
        var r = api.getPcFileStats({ filepath: absPath, tag: "st_" + Math.random().toString(36).slice(2, 8) }, fin);
        if (r && typeof r.then === "function") {
          r.then(function (v) { if (v !== undefined) fin(v); }).catch(function () {});
        }
      } catch (e) { /* 走超时 */ }
      setTimeout(function () { if (!done) { done = true; reject(new Error("stat 超时")); } }, 30000);
    });
  }

  // 有限并发映射（stat 补 mtime 用）；fn 返回 Promise，失败位记 null
  function mapLimit(items, limit, fn) {
    var ret = new Array(items.length);
    var idx = 0, active = 0, left = items.length;
    return new Promise(function (resolve) {
      function next() {
        if (left === 0) { resolve(ret); return; }
        while (active < limit && idx < items.length) {
          (function (i) {
            active++;
            Promise.resolve().then(function () { return fn(items[i], i); })
              .then(function (v) { ret[i] = v; }, function () { ret[i] = null; })
              .then(function () { active--; left--; next(); });
          })(idx++);
        }
      }
      if (!items.length) resolve(ret);
      else next();
    });
  }

  function toBytes(v) {
    if (!v) return null;
    if (v instanceof ArrayBuffer) return new Uint8Array(v);
    if (ArrayBuffer.isView(v)) return new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
    if (v.type === "Buffer" && v.data) return new Uint8Array(v.data);
    return null;
  }

  var _pcFs;
  function getPcFs() {
    if (_pcFs !== undefined) return _pcFs;
    var loaders = [];
    try { if (typeof require === "function") loaders.push(require); } catch (e) {}
    try { if (typeof window.require === "function") loaders.push(window.require); } catch (e) {}
    try {
      var raw = window.__MICRO_APP_WINDOW__ && window.__MICRO_APP_WINDOW__.rawWindow;
      if (raw && typeof raw.require === "function") loaders.push(raw.require);
    } catch (e) {}
    for (var i = 0; i < loaders.length; i++) {
      try {
        var fs = loaders[i]("fs");
        if (fs && fs.promises && typeof fs.promises.open === "function") return (_pcFs = fs);
      } catch (e) {}
    }
    return (_pcFs = null);
  }

  async function readNodeChunk(fs, abs, off, len) {
    var handle = await fs.promises.open(abs, "r");
    try {
      var bytes = new Uint8Array(len), got = 0;
      while (got < len) {
        var result = await handle.read(bytes, got, len - got, off + got);
        if (!result.bytesRead) break;
        got += result.bytesRead;
      }
      return new Blob([bytes.subarray(0, got)]);
    } finally {
      await handle.close();
    }
  }

  // 桥接接口实际为 (path, start, onData, onEnd, onError)，会一直读到 EOF。
  // 它没有定长读取或暂停能力，故只作小文件回退，并串行调用以免全局 IPC 事件串流。
  var _bridgeReadQueue = Promise.resolve();
  function readBridgeChunk(api, abs, off, len, size) {
    if (!api || typeof api.getPcFileStream !== "function") {
      return Promise.reject(new Error("桌面端无法读取 PC 文件，请用「选择文件夹」重新扫描"));
    }
    if (size > BRIDGE_MAX_FILE) {
      return Promise.reject(new Error("原生桥接读取大文件不安全，请用「选择文件夹」重新扫描"));
    }
    return new Promise(function (resolve, reject) {
      var parts = [], got = 0, done = false, timer;
      function finish(error) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (error) reject(error);
        else if (got !== len) reject(new Error("读取的文件分块长度不符，请重新扫描目录"));
        else resolve(new Blob(parts));
      }
      function armTimer() {
        if (done) return;
        clearTimeout(timer);
        timer = setTimeout(function () { finish(new Error("读取 PC 文件超时")); }, 30000);
      }
      try {
        api.getPcFileStream(abs, off, function (value) {
          if (done) return;
          var bytes = toBytes(value);
          if (!bytes) { finish(new Error("桌面端返回了无法识别的文件数据")); return; }
          var keep = Math.min(bytes.length, len - got);
          if (keep > 0) { parts.push(bytes.subarray(0, keep)); got += keep; }
          armTimer();
        }, function () { finish(); }, function (error) {
          finish(new Error("读取 PC 文件失败：" + (error && error.message || error)));
        });
        armTimer();
      } catch (error) { finish(error); }
    });
  }

  function readPcChunk(api, abs, off, len, size) {
    var fs = getPcFs();
    if (fs) return readNodeChunk(fs, abs, off, len);
    var task = _bridgeReadQueue.then(function () {
      return readBridgeChunk(api, abs, off, len, size);
    });
    _bridgeReadQueue = task.catch(function () {});
    return task;
  }

  function readEntryChunk(ent, off, end) {
    if (ent.file) return Promise.resolve(ent.file.slice(off, end));
    return readPcChunk(getBridge(), ent.abs, off, end - off, ent.size);
  }

  // 原生读取入口：整树 → 排除过滤 → 并发补 mtime → 走既有比对流水
  function nativePick(pcPath) {
    var job = state.job;
    var bridge = getBridge();       // 注意：不能叫 api——会遮蔽同名 HTTP 助手（踩过）
    if (!bridge) { toast("该功能需在桌面 App 内使用（当前浏览器请点上方「选择文件夹」）", true); return; }
    if (!pcPath) { toast("先填 PC 目录绝对路径，或把文件夹拖进框里取路径", true); return; }
    setPickStatus("正在读取目录：" + pcPath + " …");
    nativeListDir(bridge, pcPath).then(function (data) {
      var files = (data && data.files) || [];
      var rootNorm = pcPath.replace(/[\\/]+$/, "");
      var entries = [], seen = {}, skippedOut = 0;
      for (var i = 0; i < files.length; i++) {
        var full = files[i].fullPath || "";
        if (!full) continue;
        var norm = full.replace(/\\/g, "/");
        var rootN = rootNorm.replace(/\\/g, "/");
        if (norm.indexOf(rootN + "/") !== 0) { skippedOut++; continue; }  // junction 逃逸出根的不收
        var rel = norm.slice(rootN.length + 1);
        if (!rel || rel.slice(-1) === "/") continue;
        if (isExcluded(rel, job.exclude || [])) continue;
        if (seen[rel]) continue;
        seen[rel] = 1;
        entries.push({ rel: rel, abs: full, size: files[i].size || 0, mtime: 0 });
      }
      if (!entries.length) {
        setPickStatus(null);
        toast("该目录下没有可备份的文件（排除后为空）" + (skippedOut ? "；另有 " + skippedOut + " 个越界路径被跳过" : ""), true);
        return;
      }
      entries.sort(function (a, b) { return a.rel < b.rel ? -1 : 1; });
      setPickStatus("正在读取文件时间戳… 0 / " + entries.length);
      var t0 = Date.now(), done = 0, statFail = 0, failSamples = [];
      return mapLimit(entries, 64, function (e) {
        return nativeStat(bridge, e.abs).then(function (st) {
          // 必须截断到毫秒（与浏览器 File.lastModified 同口径）——
          // 四舍五入会与账本差 1ms，把约 1/4 的文件误判为变更（踩过：12711 个假变更）
          e.mtime = Math.floor(st.mtimeMs);
        }, function () {
          e.mtime = 0;   // stat 失败按 0：比对会当变更重传，方向安全
          statFail++;
          if (failSamples.length < 5) failSamples.push(e.abs);
        }).then(function () {
          done++;
          if (done % 2000 === 0 || done === entries.length) {
            setPickStatus("正在读取文件时间戳… " + done + " / " + entries.length +
              "（" + ((Date.now() - t0) / 1000).toFixed(0) + "s）");
          }
        });
      }).then(function () {
        setPickStatus(null);
        if (statFail) {
          log("[目录读取] ⚠ " + statFail + " 个文件取不到 mtime（会被判变更重传）：" + failSamples.join(" | "), "lg-err");
        }
        // 根目录名取末段：Windows 路径是反斜杠，必须先归一化，
        // 否则 "D:\work" 整串被当目录名，触发假的 folder_mismatch（踩过）
        var leaf = rootNorm.replace(/\\/g, "/").split("/").pop() || "目录";
        state.sourcePath = pcPath;
        finishPick(entries, leaf, "读取目录" + (statFail ? "（mtime 取不到 " + statFail + " 个）" : ""),
          files.length - entries.length - skippedOut);
        // 记住路径：下次打开任务直接用
        api("PATCH", "api/jobs/" + job.id, { pc_path: pcPath }).catch(function () {});
      });
    }).catch(function (e) {
      setPickStatus(null);
      toast("读取目录失败：" + (e && e.message), true);
    });
  }

  // 原生目录选择框 v2——真机实测规律：
  //   带函数回调 → IPC clone 必炸；带字符串占位 → 被门面校验拦下（不弹框）；
  //   纯 task 零回调 → 弹框成功 ✓，但结果被门面丢弃。
  // 结果捕获假说：对话框结果走 ipc 事件 → 用 listenToMain 订阅（名字逐个试，
  // fn.length 先探签名），兜底翻"最近选择"痕迹。
  var _browseSeq = 0;
  function npLogLine(msg) {
    var box = $("npLog");
    if (box) {
      var div = document.createElement("div");
      div.textContent = msg;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    }
    try { console.log("[browse] " + msg); } catch (e) {}
  }

  function nativeBrowse() {
    var bridge = getBridge();
    if (!bridge) { toast("「浏览…」需在桌面 App 内使用", true); return; }
    var tag = "br" + (++_browseSeq) + "_" + Date.now();
    var got = false;

    function onResult(v) {
      if (v === undefined || v === null) return;
      npLogLine("收到结果：" + JSON.stringify(v).slice(0, 200));
      if (got) return;
      var p = extractWinPath(v);
      if (p) {
        got = true;
        $("npPickPath").value = p;
        toast("✓ 选目录成功：" + p);
        nativePick(p);   // 显式选好目录＝确认，直接开始
      }
    }

    // 探签名 + 多名字武装监听
    try {
      npLogLine("listenToMain.length = " + (bridge.listenToMain ? bridge.listenToMain.length : "无此函数"));
    } catch (e) {}
    var names = ["openFileDialog", "openFileDialog_" + tag, tag, "fileDialog", "file-dialog",
                 "dialog", "openDirectory", "selectFolder", "selectedPath", "folderSelected"];
    names.forEach(function (n) {
      try {
        var r = bridge.listenToMain(n, onResult);
        npLogLine("listenToMain('" + n + "') → " + String(r).slice(0, 40));
        if (r && typeof r.then === "function") {
          r.then(function (v) { if (v !== undefined) onResult(v); }).catch(function () {});
        }
      } catch (e) {
        npLogLine("listenToMain('" + n + "') 抛：" + (e && e.message));
      }
    });
    // 签名若只有 1 参，再试纯回调订阅
    try {
      if (bridge.listenToMain && bridge.listenToMain.length <= 1) {
        var r0 = bridge.listenToMain(onResult);
        npLogLine("listenToMain(纯回调) → " + String(r0).slice(0, 40));
      }
    } catch (e) { npLogLine("listenToMain(纯回调) 抛：" + (e && e.message)); }

    // 弹框：零回调姿势（唯一实测能弹框的）
    try {
      var r2 = bridge.openFileDialog({ properties: ["openDirectory", "createDirectory"], tag: tag });
      npLogLine("openFileDialog(task) 同步返回：" + String(r2).slice(0, 40));
      if (r2 && typeof r2.then === "function") {
        r2.then(function (v) { if (v !== undefined) onResult(v); }).catch(function () {});
      }
    } catch (e) {
      toast("openFileDialog 抛异常：" + (e && e.message), true);
      return;
    }
    toast("已打开目录选择框——选好后自动开始读取");

    // 兜底：30 秒没等到就翻"最近选择"痕迹
    setTimeout(function () {
      if (got) return;
      try {
        var d = bridge.getDropFilePaths([]);
        npLogLine("兜底 getDropFilePaths([]) → " + JSON.stringify(d).slice(0, 120));
        if (Array.isArray(d) && d.length) onResult(d);
      } catch (e) { npLogLine("兜底 getDropFilePaths 抛：" + (e && e.message)); }
      try {
        var l = bridge.localFileList({ tag: tag }, onResult, function () {}, function () {});
        npLogLine("兜底 localFileList 同步返回：" + String(l).slice(0, 40));
      } catch (e) {}
      if (!got) toast("没捕获到选择结果——把探测台/控制台日志贴回来", true);
    }, 30000);
  }

  // 从各种返回形状里挖 Windows 绝对路径
  function extractWinPath(v) {
    if (typeof v === "string" && /^[a-zA-Z]:\\/.test(v)) return v;
    if (Array.isArray(v)) {
      for (var i = 0; i < v.length; i++) {
        var p = extractWinPath(v[i]);
        if (p) return p;
      }
      return "";
    }
    if (v && typeof v === "object") {
      var keys = ["filePaths", "selections", "path", "sPath", "filePath", "dirPath", "folderPath", "paths"];
      for (var j = 0; j < keys.length; j++) {
        var p2 = extractWinPath(v[keys[j]]);
        if (p2) return p2;
      }
    }
    return "";
  }

  // 拖拽取绝对路径：必须喂真数组（FileList 本体无效）
  function nativeDropPath(e) {
    var bridge = getBridge();
    if (!bridge) return Promise.resolve("");
    var files = e.dataTransfer && e.dataTransfer.files;
    var arr = files ? Array.prototype.slice.call(files) : [];
    try {
      var r = bridge.getDropFilePaths(arr);
      return Promise.resolve(Array.isArray(r) && r.length ? r[0] : "");
    } catch (err) {
      return Promise.resolve("");
    }
  }

  /* ── 目录选择（input + 拖拽） ──────────── */

  function traverseEntry(entry, onFile) {
    // 递归读取拖入的目录项，返回 Promise<[{rel, file}]>
    return new Promise(function (resolve) {
      if (entry.isFile) {
        entry.file(function (f) {
          f._relPath = entry.fullPath.replace(/^\//, "");
          if (onFile) onFile();
          resolve([{ rel: f._relPath, file: f }]);
        }, function () { resolve([]); });
      } else if (entry.isDirectory) {
        var reader = entry.createReader();
        var all = [];
        function readBatch() {
          reader.readEntries(function (batch) {
            if (!batch.length) {
              Promise.all(all).then(function (xs) { resolve([].concat.apply([], xs)); });
              return;
            }
            batch.forEach(function (e) { all.push(traverseEntry(e, onFile)); });
            readBatch();
          }, function () { resolve([]); });
        }
        readBatch();
      } else {
        resolve([]);
      }
    });
  }

  function initDropzone() {
    var dz = $("dropzone");
    var input = $("dirInput");

    dz.addEventListener("click", function (e) {
      // 输入框/按钮区域的点击不触发选择框
      if (e.target.closest && e.target.closest(".np-entry")) return;
      if (getBridge()) { nativeBrowse(); return; }
      setPickStatus("正在打开文件夹选择器——选择目录后，浏览器需要时间读取文件（大目录可能几十秒），期间界面可能短暂无响应，请耐心等待，不是死机…");
      input.click();
    });
    input.addEventListener("change", function () {
      if (!input.files || !input.files.length) { setPickStatus(null); return; }
      // input.files 是活的 FileList：必须先快照成数组再清空 value，
      // 否则分块筛选从第二块起读到 undefined（TypeError 链条中断）
      var picked = Array.prototype.slice.call(input.files);
      input.value = "";
      var root = "";
      for (var i = 0; i < picked.length; i++) {
        var rp = picked[i].webkitRelativePath || picked[i].name;
        if (rp.indexOf("/") > 0) { root = rp.split("/")[0]; break; }
      }
      setPickStatus("正在读取目录（" + picked.length + " 个文件）…");
      setTimeout(function () {
        onPicked(picked, root || "目录");
      }, 30);
    });

    // 经典拖拽：读 File 对象走 webkitdirectory 同款流水
    function classicDrop(e) {
      var items = e.dataTransfer && e.dataTransfer.items;
      if (!items || !items.length) return;
      var entries = [];
      var rootName = "";
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (entry) { entries.push(entry); if (!rootName) rootName = entry.name; }
      }
      if (!entries.length) return;
      var found = 0, lastUpd = 0;
      var onFile = function () {
        found++;
        var now = Date.now();
        if (now - lastUpd > 200) {
          lastUpd = now;
          setPickStatus("正在读取拖入的目录…已发现 " + found + " 个文件");
        }
      };
      setPickStatus("正在读取拖入的目录…");
      Promise.all(entries.map(function (en) { return traverseEntry(en, onFile); })).then(function (xs) {
        var flat = [].concat.apply([], xs);
        if (!flat.length) { setPickStatus(null); toast("没有读到文件", true); return; }
        var files = flat.map(function (x) { return x.file; });
        // 多个拖入项时用第一项名字当根目录
        var fake = { length: files.length };
        for (var j = 0; j < files.length; j++) fake[j] = files[j];
        onPicked(fake, rootName);
      });
    }

    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); dz.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); dz.classList.remove("drag"); });
    });
    dz.addEventListener("drop", function (e) {
      // 桌面 App 内优先取绝对路径走原生读取（显式拖放＝确认）；取不到回退经典读取
      if (getBridge()) {
        nativeDropPath(e).then(function (p) {
          if (p) {
            $("npPickPath").value = p;
            nativePick(p);
          } else {
            classicDrop(e);
          }
        });
        return;
      }
      classicDrop(e);
    });
  }

  /* ── 绑定与启动 ────────────────────────── */

  function bind() {
    $("btnNewJob").addEventListener("click", function () { openJobModal(null); });
    $("btnResume").addEventListener("click", resumeBackup);
    $("btnResumeFresh").addEventListener("click", function () {
      state.resumeInfo = null;
      state.discardResumeOnRun = true;
      $("stepResume").hidden = true;
      $("stepPick").hidden = false;
    });
    $("btnJobSave").addEventListener("click", saveJobModal);
    $("btnJobCancel").addEventListener("click", function () { $("jobModal").hidden = true; });
    $("fJobSub").addEventListener("input", updateDestPreview);
    $("chkPruneExcluded").addEventListener("change", syncPruneWarn);
    $("btnBackJobs").addEventListener("click", function () { if (!state.running) { show("view-jobs"); loadJobs(); } });
    $("btnBackFromHistory").addEventListener("click", function () { show("view-jobs"); });
    $("btnBackFromBrowse").addEventListener("click", function () { show("view-jobs"); });
    $("btnRepick").addEventListener("click", function () {
      if (state.running) { toast("正在传输，先取消再重新选目录", true); return; }
      resetSteps();
    });
    $("btnCancel").addEventListener("click", function () {
      state.cancel = true;
      $("btnCancel").disabled = true;
      log("正在取消…（当前文件传输完成后停止）", "lg-err");
    });
    $("btnDoneBack").addEventListener("click", function () { show("view-jobs"); loadJobs(); });
    $("btnDoneAgain").addEventListener("click", function () {
      var jid = state.job.id;
      loadJobs().then(function () {
        openBackup(state.jobs.find(function (j) { return j.id === jid; }) || state.job);
      });
    });
    $("btnNativeConfirm").addEventListener("click", function () {
      nativePick($("npPickPath").value.trim());
    });
    // 「浏览…」按钮：openFileDialog 姿势实验（字符串占位回调避开 clone 限制）
    if ($("btnNativeBrowse")) {
      $("btnNativeBrowse").addEventListener("click", nativeBrowse);
    }
    if (!getBridge()) {
      $("npPickHint").textContent = "路径读取需桌面 App；当前浏览器请用上方「选择文件夹」";
    }
    initDropzone();
  }

  bind();
  api("GET", "api/config").then(function (c) { state.config = c || {}; })
    .catch(function () { state.config = {}; });
  loadJobs();

  // 调试/测试钩子（生产无影响）
  window.__pcb = { state: state, apiUrl: apiUrl, isExcluded: isExcluded, onPicked: onPicked, doScan: doScan };
})();
