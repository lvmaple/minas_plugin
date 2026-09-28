/**
 * sysmon 前端 — 轮询 /api/info 并渲染仪表盘（小米橙主题）
 */
(function () {
  "use strict";

  var REFRESH_MS = 5000;
  var RING_C = 326.7;

  function $(id) { return document.getElementById(id); }

  // 桌面端 pathname 可能带盘符前缀（/D:/plugin/...），必须正则提取 /plugin/<uid>/<plugin> 段
  function apiUrl(ep) {
    var path = window.location.pathname;
    var m = path.match(/(\/plugin\/[^\/]+\/[^\/]+)/);
    var base = m ? m[1] : path.replace(/\/index\.html$/, "").replace(/\/$/, "");
    return base + "/api/" + ep;
  }

  function fmtBytes(n) {
    if (!n || n < 0) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + units[i];
  }

  function fmtUptime(sec) {
    if (!sec || sec < 0) return "—";
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + " 天 " + h + " 小时";
    if (h > 0) return h + " 小时 " + m + " 分";
    return m + " 分钟";
  }

  function setBar(el, percent) {
    el.style.width = Math.min(Math.max(percent, 0), 100) + "%";
  }

  function setRing(percent) {
    var arc = $("cpuArc");
    if (!arc) return;
    var p = Math.min(Math.max(percent, 0), 100);
    arc.style.strokeDashoffset = RING_C * (1 - p / 100);
    // 小米橙：低负载白、高负载仍保持白（hero 为橙底）
    arc.style.stroke = p > 85 ? "#ffe1cc" : "#ffffff";
    $("cpuVal").textContent = p.toFixed(0) + "%";
  }

  /* ── 温度 / 风扇 ─────────────────────── */

  function renderTemps(list) {
    var box = $("tempList");
    var count = $("tempCount");
    if (!list || !list.length) {
      box.innerHTML = '<p class="empty">未检测到温度传感器（Windows 本地无 hwmon）</p>';
      count.textContent = "0 个";
      return;
    }
    count.textContent = list.length + " 个";
    box.innerHTML = list.map(function (t) {
      var hot = t.celsius >= 70;
      return (
        '<div class="chip' + (hot ? " hot" : "") + '">' +
          '<div class="chip-name">' + esc(t.label || t.chip) + "</div>" +
          '<div class="chip-val">' + t.celsius + '<span class="unit">°C</span></div>' +
        "</div>"
      );
    }).join("");
  }

  function renderFans(list) {
    var box = $("fanList");
    var count = $("fanCount");
    if (!list || !list.length) {
      box.innerHTML = '<p class="empty">未检测到风扇转速传感器</p>';
      count.textContent = "0 个";
      return;
    }
    count.textContent = list.length + " 个";
    box.innerHTML = list.map(function (f) {
      return (
        '<div class="chip">' +
          '<div class="chip-name">' + esc(f.label || f.chip) + "</div>" +
          '<div class="chip-val">' + f.rpm + '<span class="unit">RPM</span></div>' +
        "</div>"
      );
    }).join("");
  }

  /* ── 断电保护电源 ────────────────────── */

  function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var min = Math.floor(sec / 60);
    var rem = sec % 60;
    return min + " 分 " + rem + " 秒";
  }

  function renderUps(ups) {
    var badge = $("upsBadge");
    var state = $("upsState");
    var health = $("upsHealth");
    var duration = $("upsDuration");
    var countdown = $("upsCountdown");
    badge.className = "pill";
    state.className = "ups-value";
    health.className = "ups-value";

    if (!ups || !ups.available) {
      badge.textContent = "无数据";
      state.textContent = "无法读取";
      health.textContent = "—";
      duration.textContent = "—";
      countdown.textContent = "供电状态不可用";
      return;
    }

    state.textContent = ups.state === "ADAPTER" ? "适配器供电" :
      ups.state === "UPS" ? "UPS 后备供电" : "未确认";
    health.textContent = ups.health === "HEALTH" ? "未报告异常" :
      ups.health === "UNHEALTH" ? "异常" : "未确认";
    if (ups.state === "UPS" && ups.timer && ups.timer.active) {
      duration.textContent = fmtDuration(ups.timer.elapsed_sec);
      countdown.textContent = "约 " + fmtDuration(ups.timer.remaining_sec) + "后自动关机";
    } else {
      duration.textContent = "0 分 0 秒";
      countdown.textContent = ups.state === "UPS" ? "计时器启动中…" :
        "连续后备供电 5 分钟后自动关机";
    }

    if (ups.health === "UNHEALTH") {
      badge.textContent = "异常";
      badge.className = "pill alert";
      health.className = "ups-value alert";
    } else if (ups.state === "UPS") {
      badge.textContent = "后备供电";
      badge.className = "pill warn";
    } else {
      badge.textContent = "固件记录";
    }
  }

  /* ── Docker ─────────────────────────── */

  function renderDocker(d) {
    var box = $("dockerList");
    var runEl = $("dockerRunning");
    var exitEl = $("dockerExited");

    if (!d || !d.available) {
      runEl.textContent = "运行 —";
      exitEl.textContent = "停止 —";
      box.innerHTML = '<p class="empty">Docker 不可用' +
        (d && d.error ? "（" + esc(d.error) + "）" : "（未安装或无权限）") + "</p>";
      return;
    }

    var c = d.counts || {};
    runEl.textContent = "运行 " + (c.running || 0);
    exitEl.textContent = "停止 " + (c.exited || 0);

    if (!d.containers || !d.containers.length) {
      box.innerHTML = '<p class="empty">暂无容器</p>';
      return;
    }

    box.innerHTML = d.containers.map(function (k) {
      var cls = k.state === "running" ? "running" : (k.state === "exited" ? "exited" : "other");
      return (
        '<div class="docker-row">' +
          '<span class="dot ' + cls + '"></span>' +
          '<div class="docker-main">' +
            '<div class="docker-name">' + esc(k.name) + "</div>" +
            '<div class="docker-sub">' + esc(k.image) + (k.ports ? " · " + esc(k.ports) : "") + "</div>" +
          "</div>" +
          '<div class="docker-status">' + esc(k.status) + "</div>" +
        "</div>"
      );
    }).join("");
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ── 主渲染 ─────────────────────────── */

  function render(data) {
    $("hostname").textContent = data.hostname || "—";
    $("platform").textContent = data.platform || "";
    $("uptime").textContent = fmtUptime(data.uptime_sec);
    $("cores").textContent = data.cpu ? data.cpu.cores : "—";

    setRing((data.cpu && data.cpu.percent) || 0);

    if (data.cpu && data.cpu.load) {
      $("load1").textContent = data.cpu.load.load1;
      $("load5").textContent = data.cpu.load.load5;
      $("load15").textContent = data.cpu.load.load15;
    }

    if (data.memory) {
      var memP = data.memory.percent || 0;
      $("memPercent").textContent = memP.toFixed(1) + "%";
      setBar($("memBar"), memP);
      $("memUsed").textContent = fmtBytes(data.memory.used);
      $("memTotal").textContent = fmtBytes(data.memory.total);
    }

    if (data.disk) {
      var diskP = data.disk.percent || 0;
      $("diskPercent").textContent = diskP.toFixed(1) + "%";
      setBar($("diskBar"), diskP);
      $("diskUsed").textContent = fmtBytes(data.disk.used);
      $("diskTotal").textContent = fmtBytes(data.disk.total);
    }

    renderTemps(data.temps);
    renderFans(data.fans);
    renderUps(data.ups);
    renderDocker(data.docker);

    $("updatedAt").textContent =
      "更新于 " + new Date((data.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
  }

  function load() {
    fetch(apiUrl("info"), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (json) {
        if (json && json.code === 0 && json.data) {
          render(json.data);
        } else {
          $("updatedAt").textContent = "接口返回异常";
        }
      })
      .catch(function () {
        $("updatedAt").textContent = "无法连接本地服务（请确认服务已启动）";
      });
  }

  $("refreshBtn").addEventListener("click", load);
  load();
  setInterval(load, REFRESH_MS);
})();
