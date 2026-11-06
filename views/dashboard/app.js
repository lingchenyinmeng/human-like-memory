/* Human-like Memory 面板逻辑：记忆 / 人物 / 关系 */
(function () {
  "use strict";

  var PREFIX = "/page";
  var api = null;
  var tab = "memories";
  var mem = { page: 1, size: 30, status: "", type: "", q: "" };
  var ppl = { page: 1, size: 40, status: "", q: "" };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function waitBridge(timeoutMs) {
    return new Promise(function (resolve) {
      var deadline = Date.now() + (timeoutMs || 8000);
      (function poll() {
        if (window.AstrBotPluginView || window.AstrBotPluginPage) return resolve(true);
        if (Date.now() > deadline) return resolve(false);
        setTimeout(poll, 80);
      })();
    });
  }

  async function call(path, params) {
    if (!api || !api.apiGet) throw new Error("桥接不可用");
    return await api.apiGet(PREFIX + path, params || {});
  }

  /* ================= 统计 ================= */
  async function loadStats() {
    try {
      var d = await call("/stats");
      var st = d.by_status_cn || {};
      var cards = [
        ["记忆", d.memories, ""],
        ["事件原子", d.atoms_total, ""],
        ["暂存", st["暂存"] || 0, "等定性"],
        ["正式", st["正式"] || 0, "在用"],
        ["待定", st["待定"] || 0, "最底层"],
        ["遗忘库", st["遗忘库"] || 0, "提起才回"],
        ["平均重要性", d.avg_importance, ""]
      ];
      $("stats").innerHTML = cards.map(function (c) {
        return '<div class="stat"><div class="k">' + c[0] + '</div><div class="v">' + c[1] +
          (c[2] ? '<small>' + c[2] + '</small>' : '') + '</div></div>';
      }).join("");
      $("badge").textContent = d.semantic ? "语义 开" : "关键词";
      $("cntMem").textContent = d.atoms_total || 0;
    } catch (e) {
      $("stats").innerHTML = '<div class="stat"><div class="k">统计失败</div><div class="v" style="font-size:12px">' +
        esc(e.message) + '</div></div>';
    }
  }

  /* ================= 记忆 ================= */
  async function loadMemories() {
    var list = $("list");
    list.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var d = await call("/atoms", {
        page: mem.page, size: mem.size, status: mem.status, type: mem.type, q: mem.q
      });
      var items = d.items || [];
      if (!items.length) {
        list.innerHTML = '<div class="empty">没有符合条件的记忆</div>';
        $("pager").innerHTML = "";
        $("footHint").textContent = "共 0 条";
        return;
      }
      list.innerHTML = items.map(memCard).join("");
      each(list.querySelectorAll(".card"), function (el) {
        el.addEventListener("click", function () { showAtom(el.dataset.id); });
      });
      renderPager($("pager"), d.page, d.pages, function (p) { mem.page = p; loadMemories(); });
      $("footHint").textContent = "共 " + d.total + " 条";
    } catch (e) {
      list.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  function memCard(a) {
    var tags = [
      '<span class="tag">' + esc(a.status_cn) + "</span>",
      '<span class="tag">' + esc(a.type_cn) + (a.subtype_cn ? "·" + esc(a.subtype_cn) : "") + "</span>",
      '<span class="tag">' + esc(a.layer_cn) + "</span>"
    ];
    if (a.nature_cn) {
      tags.push('<span class="tag nature-' + esc(a.nature) + '">' + esc(a.nature_cn) + " " + a.nature_conf + "</span>");
    }
    if (a.privacy === "sensitive") tags.push('<span class="tag privacy">私密</span>');
    if (a.who_name) tags.push('<span class="tag">' + esc(a.who_name) + "</span>");
    tags.push('<span class="tag">重要性 ' + a.importance + "</span>");
    tags.push('<span class="tag time">' + esc(a.create_text) + " · " + esc(a.age) + "</span>");
    return '<div class="card st-' + esc(a.status) + '" data-id="' + a.id + '">' +
      '<div class="content">' + esc(a.content) + "</div>" +
      '<div class="meta">' + tags.join("") + "</div></div>";
  }

  async function showAtom(id) {
    $("dTitle").textContent = "记忆 #" + id;
    $("dBody").innerHTML = '<div class="empty">加载中…</div>';
    $("mask").classList.add("show");
    try {
      var a = await call("/atom", { id: id });
      var rows = [
        ["内容", a.content],
        ["状态", a.status_cn],
        ["谁对谁", a.layer_cn],
        ["类型", a.type_cn + (a.subtype_cn ? " / " + a.subtype_cn : "")],
        ["性质", (a.nature_cn || "未定性") + (a.nature ? "（" + a.nature_conf + "）" : "")],
        ["说话人", (a.who_name || "-") + (a.who_id ? " (" + a.who_id + ")" : "")],
        ["来源", a.source === "group" ? "群聊" : a.source === "private" ? "私聊" : "-"],
        ["隐私", a.privacy === "sensitive" ? "私密（不外带）" : "普通"],
        ["重要性", String(a.importance)],
        ["时间", a.create_text + "（" + a.age + "）"],
        ["分区", a.partition_key || "-"],
        ["所属记忆", "#" + a.memory_id]
      ];
      var html = rows.map(function (r) {
        return '<div class="row"><div class="k">' + r[0] + '</div><div class="v">' + esc(r[1]) + "</div></div>";
      }).join("");
      if (a.context && a.context.length) {
        html += '<div class="row"><div class="k">上文</div><div class="v">';
        a.context.forEach(function (c) {
          html += '<div class="quote"><b>' + esc(c.who || "?") + "：</b>" + esc(c.text || "") + "</div>";
        });
        html += "</div></div>";
      }
      $("dBody").innerHTML = html;
    } catch (e) {
      $("dBody").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  /* ================= 人物 ================= */
  async function loadPeople() {
    var box = $("peopleList");
    box.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var d = await call("/people", {
        page: ppl.page, size: ppl.size, status: ppl.status, q: ppl.q
      });
      var items = d.items || [];
      $("cntPpl").textContent = d.total || 0;
      if (!items.length) {
        box.innerHTML = '<div class="empty">还没有认过人</div>';
        return;
      }
      box.innerHTML = items.map(function (p) {
        var pills = [];
        if (p.rel_count > 0) {
          pills.push('<span class="pill rel">' + p.rel_count + " 条关系</span>");
        } else {
          pills.push('<span class="pill lonely">孤立</span>');
        }
        if (p.is_unknown) pills.push('<span class="pill">待认</span>');
        if (p.status !== "active") pills.push('<span class="pill">' + esc(p.status) + "</span>");
        return '<div class="pcard' + (p.is_unknown ? " unk" : "") + '" data-id="' + esc(p.person_id) + '">' +
          '<div class="name">' + esc(p.names) + "</div>" +
          '<div class="sub">提及 ' + p.mention_count + " 次 · 重要 " + p.importance +
          "<br>最近 " + esc(p.last_seen_text) + "</div>" +
          '<div class="rel">' + pills.join("") + "</div></div>";
      }).join("");
      each(box.querySelectorAll(".pcard"), function (el) {
        el.addEventListener("click", function () { showPerson(el.dataset.id); });
      });
      renderPager($("ppager"), d.page, d.pages, function (p) { ppl.page = p; loadPeople(); });
    } catch (e) {
      box.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  async function showPerson(pid) {
    $("dTitle").textContent = "人物 " + pid;
    $("dBody").innerHTML = '<div class="empty">加载中…</div>';
    $("mask").classList.add("show");
    try {
      var c = await call("/person", { id: pid });
      var html = '<div class="row"><div class="k">身份</div><div class="v">' +
        (c.is_unknown ? "待认（还没确认是谁）" : "已认") + "</div></div>";
      html += '<div class="row"><div class="k">状态</div><div class="v">' +
        esc(c.status || "active") + " · 提及 " + (c.mention_count || 0) +
        " 次 · 重要 " + (c.importance || 0) + "</div></div>";
      if (c.tags && c.tags.length) {
        html += '<div class="row"><div class="k">叫法</div><div class="v">';
        c.tags.forEach(function (t) {
          html += '<div class="quote">' + esc(t.value) + " <span style='color:#8b97ab'>(" +
            esc(t.kind) + " · " + esc(t.source) + " · " + t.confidence + ")</span></div>";
        });
        html += "</div></div>";
      }
      if (c.relations && c.relations.length) {
        html += '<div class="row"><div class="k">关系</div><div class="v">';
        c.relations.forEach(function (r) {
          html += '<div class="quote">跟 <b>' + esc(r.other_name || r.other_id) + "</b> 是" +
            esc(r.rel_type) + " <span style='color:#8b97ab'>(" + esc(r.verified) +
            " " + r.confidence + ")</span></div>";
        });
        html += "</div></div>";
      } else {
        html += '<div class="row"><div class="k">关系</div><div class="v">还没有跟任何人建立关系（孤立原子，但依然在库里）</div></div>';
      }
      $("dBody").innerHTML = html;
    } catch (e) {
      $("dBody").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  /* ================= 关系 + 图谱 ================= */
  var graphData = { nodes: [], edges: [] };

  async function loadRelations() {
    var box = $("relList");
    box.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var g = await call("/graph");
      graphData = { nodes: g.nodes || [], edges: g.edges || [] };
      $("cntRel").textContent = graphData.edges.length;
      drawGraph();
      var d = await call("/relations", { limit: 200 });
      var items = d.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="empty">还没有记过关系。跟她说「鲸宝是我朋友」试试</div>';
        return;
      }
      box.innerHTML = items.map(function (r) {
        var guess = r.source === "inferred" ? '<span class="guess">（猜的）</span>' : "";
        return '<div class="rel-row">' +
          '<span class="pair">' + esc(r.a_name) + "</span>" +
          '<span class="type">—' + esc(r.rel_type) + "—</span>" +
          '<span class="pair">' + esc(r.b_name) + "</span>" + guess +
          '<span class="conf">' + esc(r.verified) + " " + r.confidence +
          " · " + esc(r.from_text) + "</span></div>";
      }).join("");
    } catch (e) {
      box.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  /* 极简力导向：节点少，够看 */
  var layout = { nodes: [], anim: 0 };

  function prepareLayout() {
    var W = 1100, H = 520;
    var n = graphData.nodes.length;
    var pos = {};
    graphData.nodes.forEach(function (node, i) {
      var ang = (i / Math.max(1, n)) * Math.PI * 2;
      var r = Math.min(W, H) * 0.34;
      pos[node.id] = {
        x: W / 2 + Math.cos(ang) * r * (0.6 + 0.4 * Math.random()),
        y: H / 2 + Math.sin(ang) * r * (0.6 + 0.4 * Math.random()),
        vx: 0, vy: 0, node: node
      };
    });
    layout.nodes = Object.keys(pos).map(function (k) { return pos[k]; });
    layout.pos = pos;
    layout.W = W; layout.H = H;
  }

  function stepLayout() {
    var nodes = layout.nodes;
    var W = layout.W, H = layout.H;
    // 斥力
    for (var i = 0; i < nodes.length; i++) {
      for (var j = i + 1; j < nodes.length; j++) {
        var a = nodes[i], b = nodes[j];
        var dx = a.x - b.x, dy = a.y - b.y;
        var d2 = dx * dx + dy * dy + 1;
        var f = 6000 / d2;
        var d = Math.sqrt(d2);
        a.vx += (dx / d) * f; a.vy += (dy / d) * f;
        b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
      }
    }
    // 边拉力
    graphData.edges.forEach(function (e) {
      var a = layout.pos[e.a], b = layout.pos[e.b];
      if (!a || !b) return;
      var dx = b.x - a.x, dy = b.y - a.y;
      var d = Math.sqrt(dx * dx + dy * dy) + 1;
      var f = (d - 150) * 0.012;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    });
    // 向心 + 阻尼
    nodes.forEach(function (n) {
      n.vx += (W / 2 - n.x) * 0.002;
      n.vy += (H / 2 - n.y) * 0.002;
      n.vx *= 0.85; n.vy *= 0.85;
      n.x = Math.max(40, Math.min(W - 40, n.x + n.vx));
      n.y = Math.max(36, Math.min(H - 36, n.y + n.vy));
    });
  }

  function drawGraph() {
    var cv = $("graph");
    if (!cv || !graphData.nodes.length) {
      if (cv) {
        var c0 = cv.getContext("2d");
        c0.clearRect(0, 0, cv.width, cv.height);
        $("graphHint").textContent = "还没有人，聊几句再来";
      }
      return;
    }
    prepareLayout();
    for (var k = 0; k < 220; k++) stepLayout();
    renderGraph();
    $("graphHint").textContent =
      graphData.nodes.length + " 个人 · " + graphData.edges.length + " 条关系（灰色是没有关系的人）";
  }

  function renderGraph() {
    var cv = $("graph");
    var dpr = window.devicePixelRatio || 1;
    var cssW = cv.clientWidth || 1100;
    var cssH = 520;
    cv.width = cssW * dpr;
    cv.height = cssH * dpr;
    cv.style.height = cssH + "px";
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var sx = cssW / layout.W, sy = cssH / layout.H;

    // 边
    graphData.edges.forEach(function (e) {
      var a = layout.pos[e.a], b = layout.pos[e.b];
      if (!a || !b) return;
      ctx.beginPath();
      ctx.moveTo(a.x * sx, a.y * sy);
      ctx.lineTo(b.x * sx, b.y * sy);
      var c = e.confidence || 0.5;
      ctx.strokeStyle = e.source === "inferred"
        ? "rgba(251,191,36," + (0.25 + c * 0.5) + ")"
        : "rgba(91,192,255," + (0.2 + c * 0.6) + ")";
      ctx.lineWidth = 1 + c * 2;
      ctx.stroke();
    });

    // 节点
    layout.nodes.forEach(function (n) {
      var x = n.x * sx, y = n.y * sy;
      var node = n.node;
      var r = 5 + (node.importance || 0.3) * 10;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = node.unknown ? "#fbbf24"
        : node.status === "archived" ? "#6b7280" : "#5bc0ff";
      ctx.fill();
      ctx.fillStyle = "#c8d2e0";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(node.name || "", x, y - r - 5);
    });
  }

  /* ================= 通用 ================= */

  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  function renderPager(box, page, pages, onGo) {
    if (pages <= 1) { box.innerHTML = ""; return; }
    var html = "";
    var start = Math.max(1, page - 2), end = Math.min(pages, start + 4);
    if (start > 1) html += '<button data-p="1">1</button>';
    for (var i = start; i <= end; i++) {
      html += '<button data-p="' + i + '"' + (i === page ? ' class="on"' : "") + ">" + i + "</button>";
    }
    if (end < pages) html += '<button data-p="' + pages + '">' + pages + "</button>";
    box.innerHTML = html;
    each(box.querySelectorAll("button"), function (b) {
      b.addEventListener("click", function () {
        onGo(parseInt(b.dataset.p, 10) || 1);
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
  }

  function switchTab(name) {
    tab = name;
    each($("tabs").querySelectorAll("button"), function (b) {
      b.classList.toggle("on", b.dataset.tab === name);
    });
    $("view-memories").hidden = name !== "memories";
    $("view-people").hidden = name !== "people";
    $("view-relations").hidden = name !== "relations";
    if (name === "memories") loadMemories();
    if (name === "people") loadPeople();
    if (name === "relations") loadRelations();
  }

  function bind() {
    $("refreshBtn").addEventListener("click", function () {
      loadStats();
      switchTab(tab);
    });
    each($("tabs").querySelectorAll("button"), function (b) {
      b.addEventListener("click", function () { switchTab(b.dataset.tab); });
    });
    $("searchBtn").addEventListener("click", function () {
      mem.q = $("fQ").value.trim(); mem.page = 1; loadMemories();
    });
    $("fQ").addEventListener("keydown", function (e) { if (e.key === "Enter") $("searchBtn").click(); });
    $("fStatus").addEventListener("change", function () { mem.status = this.value; mem.page = 1; loadMemories(); });
    $("fType").addEventListener("change", function () { mem.type = this.value; mem.page = 1; loadMemories(); });
    $("pSearchBtn").addEventListener("click", function () {
      ppl.q = $("pQ").value.trim(); ppl.page = 1; loadPeople();
    });
    $("pQ").addEventListener("keydown", function (e) { if (e.key === "Enter") $("pSearchBtn").click(); });
    $("pStatus").addEventListener("change", function () { ppl.status = this.value; ppl.page = 1; loadPeople(); });
    $("closeBtn").addEventListener("click", function () { $("mask").classList.remove("show"); });
    $("mask").addEventListener("click", function (e) { if (e.target === $("mask")) $("mask").classList.remove("show"); });
    window.addEventListener("resize", function () {
      if (tab === "relations" && graphData.nodes.length) renderGraph();
    });
  }

  async function boot() {
    var ok = await waitBridge(8000);
    api = window.AstrBotPluginView || window.AstrBotPluginPage || null;
    if (api && api.ready) { try { await api.ready(); } catch (e) {} }
    bind();
    if (!ok || !api) {
      $("stats").innerHTML = '<div class="stat" style="grid-column:1/-1"><div class="k">页面桥接没加载成功</div>' +
        '<div class="v" style="font-size:13px">确认 AstrBot 4.24.2+，并从「插件 → 视图」入口打开</div></div>';
      $("list").innerHTML = '<div class="empty">桥接不可用时读不到数据</div>';
      return;
    }
    await loadStats();
    await loadMemories();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
