/* Human-like Memory 面板逻辑 */
(function () {
  "use strict";

  var PREFIX = "/astrbot_plugin_human_like_memory/page";
  var api = window.AstrBotPluginView;

  var state = { page: 1, size: 30, status: "", type: "", q: "" };

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  async function call(path, params) {
    if (!api || !api.apiGet) throw new Error("插件页面桥接不可用");
    var res = await api.apiGet(PREFIX + path, params || {});
    return res;
  }

  /* ---------- 统计 ---------- */
  async function loadStats() {
    try {
      var d = await call("/stats");
      var box = $("stats");
      var st = d.by_status_cn || {};
      var cards = [
        ["记忆条数", d.memories, ""],
        ["原子总数", d.atoms_total, ""],
        ["暂存", st["暂存"] || 0, "还在等定性"],
        ["正式", st["正式"] || 0, "正在用"],
        ["待定", st["待定"] || 0, "压最底层"],
        ["遗忘库", st["遗忘库"] || 0, "被提起才回来"],
        ["平均重要性", d.avg_importance, ""]
      ];
      box.innerHTML = cards.map(function (c) {
        return '<div class="stat"><div class="k">' + c[0] + '</div>' +
          '<div class="v">' + c[1] + (c[2] ? '<small>' + c[2] + '</small>' : '') + '</div></div>';
      }).join("");
      var badge = $("semanticBadge");
      badge.textContent = d.semantic ? "语义检索 开" : "关键词";
    } catch (e) {
      $("stats").innerHTML = '<div class="stat"><div class="k">统计加载失败</div><div class="v" style="font-size:13px">' + esc(e.message) + "</div></div>";
    }
  }

  /* ---------- 列表 ---------- */
  async function loadList() {
    var list = $("list");
    list.innerHTML = '<div class="empty">加载中…</div>';
    try {
      var d = await call("/atoms", {
        page: state.page, size: state.size,
        status: state.status, type: state.type, q: state.q
      });
      var items = d.items || [];
      if (!items.length) {
        list.innerHTML = '<div class="empty">没有符合条件的记忆</div>';
        renderPager(1, 1);
        $("footHint").textContent = "共 0 条";
        return;
      }
      list.innerHTML = items.map(card).join("");
      Array.prototype.forEach.call(list.querySelectorAll(".card"), function (el) {
        el.addEventListener("click", function () { showDetail(el.dataset.id); });
      });
      renderPager(d.page, d.pages);
      $("footHint").textContent = "共 " + d.total + " 条 · 第 " + d.page + "/" + d.pages + " 页";
    } catch (e) {
      list.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
    }
  }

  function card(a) {
    var tags = [];
    tags.push('<span class="tag status">' + esc(a.status_cn) + "</span>");
    tags.push('<span class="tag">' + esc(a.type_cn) + (a.subtype_cn ? "·" + esc(a.subtype_cn) : "") + "</span>");
    tags.push('<span class="tag">' + esc(a.layer_cn) + "</span>");
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

  function renderPager(page, pages) {
    var box = $("pager");
    if (pages <= 1) { box.innerHTML = ""; return; }
    var html = "";
    var start = Math.max(1, page - 2), end = Math.min(pages, start + 4);
    if (start > 1) html += '<button data-p="1">1</button>';
    for (var i = start; i <= end; i++) {
      html += '<button data-p="' + i + '"' + (i === page ? ' class="on"' : "") + ">" + i + "</button>";
    }
    if (end < pages) html += '<button data-p="' + pages + '">' + pages + "</button>";
    box.innerHTML = html;
    Array.prototype.forEach.call(box.querySelectorAll("button"), function (b) {
      b.addEventListener("click", function () {
        state.page = parseInt(b.dataset.p, 10) || 1;
        loadList();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
  }

  /* ---------- 详情 ---------- */
  async function showDetail(id) {
    var mask = $("mask");
    $("dTitle").textContent = "记忆 #" + id;
    $("dBody").innerHTML = '<div class="empty">加载中…</div>';
    mask.classList.add("show");
    try {
      var a = await call("/atom", { id: id });
      var rows = [
        ["内容", a.content],
        ["状态", a.status_cn + (a.expires_text && a.expires_text !== "-" ? "（到期 " + a.expires_text + "）" : "")],
        ["谁对谁", a.layer_cn],
        ["类型", a.type_cn + (a.subtype_cn ? " / " + a.subtype_cn : "")],
        ["性质", (a.nature_cn || "未定性") + (a.nature ? "（置信 " + a.nature_conf + "）" : "")],
        ["说话人", (a.who_name || "-") + (a.who_id ? " (" + a.who_id + ")" : "")],
        ["来源", a.source === "group" ? "群聊" : a.source === "private" ? "私聊" : "-"],
        ["隐私", a.privacy === "sensitive" ? "私密（不外带）" : "普通"],
        ["重要性", String(a.importance)],
        ["写入时间", a.create_text + "（" + a.age + "）"],
        ["存活", a.ttl_days + " 天"],
        ["强化次数", String(a.reinforce)],
        ["分区", a.partition_key || "-"],
        ["话题", a.topic || "-"],
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

  /* ---------- 事件 ---------- */
  function bind() {
    $("refreshBtn").addEventListener("click", function () {
      loadStats(); loadList();
    });
    $("searchBtn").addEventListener("click", function () {
      state.q = $("fQ").value.trim();
      state.page = 1;
      loadList();
    });
    $("fQ").addEventListener("keydown", function (e) {
      if (e.key === "Enter") $("searchBtn").click();
    });
    $("fStatus").addEventListener("change", function () {
      state.status = this.value; state.page = 1; loadList();
    });
    $("fType").addEventListener("change", function () {
      state.type = this.value; state.page = 1; loadList();
    });
    $("closeBtn").addEventListener("click", function () {
      $("mask").classList.remove("show");
    });
    $("mask").addEventListener("click", function (e) {
      if (e.target === $("mask")) $("mask").classList.remove("show");
    });
  }

  async function boot() {
    if (api && api.ready) {
      try { await api.ready(); } catch (e) { /* 桥接慢也继续 */ }
    }
    bind();
    await loadStats();
    await loadList();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
