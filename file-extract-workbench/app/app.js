/* 文件提取工作台 —— 前端 */
(function () {
"use strict";

// ================================================================ 基础工具
var $ = function (id) { return document.getElementById(id); };
var TOKEN = new URLSearchParams(location.search).get("t") || "";
var LS_PLAN = "few.plan.v2", LS_UI = "few.ui.v2", LS_THEME = "few.theme";

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function fmtBytes(n) { n = Number(n) || 0; var u = ["B", "KB", "MB", "GB", "TB"], i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + " " + u[i]; }
function pad(n) { return (n < 10 ? "0" : "") + n; }
function fmtTs(ts, withSec) {
  if (ts == null) return "";
  var d = new Date(ts * 1000);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + (withSec === false ? "" : ":" + pad(d.getSeconds()));
}
function fmtShort(ts) {
  if (ts == null) return "";
  var d = new Date(ts * 1000);
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
function todStr(secs, withSec) { secs = Math.max(0, Math.min(86399, Math.round(secs))); var h = Math.floor(secs / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60; return pad(h) + ":" + pad(m) + (withSec ? ":" + pad(s) : ""); }
function today() { var d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
var _uid = 0;
function uid(p) { _uid++; return (p || "c") + Date.now().toString(36) + _uid; }
function debounce(fn, ms) { var t; return function () { var a = arguments; clearTimeout(t); t = setTimeout(function () { fn.apply(null, a); }, ms); }; }
function lsGet(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
function h(tag, attrs, kids) {
  var e = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  (kids || []).forEach(function (c) { if (c == null) return; e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
  return e;
}

function api(name, body) {
  return fetch("/api/" + name, {method: "POST", headers: {"Content-Type": "application/json", "X-Token": TOKEN}, body: JSON.stringify(body || {})})
    .then(function (r) {
      return r.json().catch(function () { return {error: "HTTP " + r.status}; }).then(function (j) {
        if (!r.ok || j.error) { var e = new Error(j.error || ("HTTP " + r.status)); throw e; }
        return j;
      });
    }, function () { throw new Error("与后台的连接已断开。请确认启动脚本的窗口仍开着，然后刷新本页。"); });
}

var toastTimer;
function toast(msg, isErr) {
  var t = $("toast"); t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, isErr ? 7000 : 3200);
}

// ================================================================ 状态
var DEFAULT_PLAN = {
  sources: [],
  scan: {maxDepth: 0, skipHidden: true, excludeDirs: []},
  rule: {mode: "all", items: []},
  limit: {enabled: false, per: "dir1", count: 10, order: "newest", by: "exif.time"},
  dest: {root: "", template: "{一级文件夹}/{子目录}", rename: "", action: "copy", conflict: "rename", preserveTimes: true,
         verify: true, dedupe: false, zipName: "导出_{运行日期:yyyyMMdd_HHmmss}.zip", watchInterval: 10},
  timeFallback: "mtime"
};
var S = {
  info: null, reg: null, fields: {}, kinds: {}, plan: clone(DEFAULT_PLAN),
  ui: {engine: "local", facet: "time", histField: "", histMode: "timeline", view: "grid", sort: "path", pick: false},
  idx: null, gen: 0, q: null, items: [], marked: {}, thumbs: {}, job: null, logSeq: 0, lastFocusTpl: "destTpl"
};

function mergePlan(p) {
  var out = clone(DEFAULT_PLAN);
  p = p || {};
  ["sources", "timeFallback"].forEach(function (k) { if (p[k] !== undefined) out[k] = clone(p[k]); });
  ["scan", "limit", "dest"].forEach(function (k) { if (p[k]) Object.keys(p[k]).forEach(function (kk) { out[k][kk] = clone(p[k][kk]); }); });
  if (p.rule && Array.isArray(p.rule.items)) out.rule = clone(p.rule);
  ensureIds(out.rule);
  return out;
}
function ensureIds(node) { (node.items || []).forEach(function (it) { if (!it.id) it.id = uid(it.items ? "g" : "c"); if (it.items) { it.group = true; ensureIds(it); } }); }

var saveLocal = debounce(function () { lsSet(LS_PLAN, S.plan); lsSet(LS_UI, S.ui); }, 300);
function changed(opts) {
  opts = opts || {};
  saveLocal();
  if (opts.reindex) startIndex();
  else scheduleQuery();
  if (opts.rules) renderRules();
}

// ================================================================ 条件辅助
function fieldDef(key) { return S.fields[key] || {key: key, label: key, type: "str"}; }
function defaultValue(type, op) {
  if (["empty", "notEmpty"].indexOf(op) >= 0) return null;
  if (type === "time") {
    if (op === "timeOfDay" || op === "between") return ["", ""];
    if (op === "onDate") return [today()];
    if (op === "datePreset") return "today";
    if (op === "recent") return {amount: 1, unit: "days"};
    return "";
  }
  if (type === "kind" || type === "ext") return [];
  if ((type === "num" || type === "size") && op === "between") return ["", ""];
  if (type === "list") return {mode: "stem", keys: [], rows: [], columns: []};
  return "";
}
function newCond(field, op, value) {
  var f = fieldDef(field);
  op = op || S.reg.ops[f.type][0];
  var c = {id: uid("c"), field: field, op: op, value: value !== undefined ? value : defaultValue(f.type, op)};
  if (f.type === "size") c.unit = "MB";
  return c;
}
function filled(v) { return v != null && String(v).trim() !== ""; }
function isComplete(c) {
  if (c.items) return c.items.some(function (x) { return !x.off && isComplete(x); });
  var f = fieldDef(c.field), op = c.op, v = c.value;
  if (op === "empty" || op === "notEmpty") return true;
  switch (f.type) {
    case "kind": case "ext": return Array.isArray(v) ? v.length > 0 : filled(v);
    case "list": return v && v.keys && v.keys.length > 0;
    case "num": case "size":
      if (op === "between") return Array.isArray(v) && (filled(v[0]) || filled(v[1]));
      return filled(v) && !isNaN(Number(v));
    case "time":
      if (op === "timeOfDay") return Array.isArray(v) && filled(v[0]) && filled(v[1]);
      if (op === "between") return Array.isArray(v) && (filled(v[0]) || filled(v[1]));
      if (op === "onDate") return Array.isArray(v) ? v.some(filled) : filled(v);
      if (op === "recent") return v && Number(v.amount) > 0;
      return filled(v);
    default:
      if (op === "in" || op === "notIn") return Array.isArray(v) ? v.length > 0 : filled(v);
      return filled(v);
  }
}
function cleanNode(node) {
  var out = {id: node.id, mode: node.mode || "all", items: []};
  if (node.group) out.group = true;
  (node.items || []).forEach(function (it) {
    if (it.off) return;
    if (it.items) { var g = cleanNode(it); if (g.items.length) out.items.push(g); }
    else if (isComplete(it)) out.items.push(it);
  });
  return out;
}
function cleanPlan() {
  var p = clone(S.plan);
  p.rule = cleanNode(S.plan.rule);
  if (p.limit && !p.limit.enabled) p.limit = {enabled: false};
  return p;
}
function topItems() { return S.plan.rule.items; }
function findTop(field, ops) { return topItems().filter(function (c) { return !c.items && !c.off && c.field === field && ops.indexOf(c.op) >= 0; })[0]; }

// ================================================================ 索引
var idxPoll = null;
function startIndex() {
  clearTimeout(idxPoll);
  S.items = []; S.q = null; S.marked = {}; clearThumbs();
  if (!S.plan.sources.length) {
    S.idx = null; S.gen = 0;
    renderIdxStatus({state: "idle"}); renderDirs(); renderResults(); renderCounts(); renderFacet(); renderTree();
    return;
  }
  renderIdxStatus({state: "scanning", count: 0});
  api("index", {plan: S.plan}).then(function (r) { S.gen = r.gen; pollIndex(); }).catch(function (e) {
    renderIdxStatus({state: "error", error: e.message});
  });
}
function pollIndex() {
  api("index_status").then(function (st) {
    if (st.gen !== S.gen) return;
    if (st.state === "scanning") { renderIdxStatus(st); idxPoll = setTimeout(pollIndex, 300); return; }
    if (st.state === "error") { renderIdxStatus(st); return; }
    if (st.state === "ready") { S.idx = st.index; renderIdxStatus(st); renderDirs(); runQuery(); }
  }).catch(function (e) { renderIdxStatus({state: "error", error: e.message}); });
}
function renderIdxStatus(st) {
  var box = $("idxStatus");
  if (st.state === "idle") { box.innerHTML = '<span class="faint">添加来源文件夹后自动扫描。</span>'; return; }
  if (st.state === "scanning") { box.innerHTML = '<span class="busy"></span> 正在扫描… 已找到 <b class="num">' + (st.count || 0) + "</b> 个文件"; return; }
  if (st.state === "error") { box.innerHTML = '<span class="err-text">' + esc(st.error || "扫描失败") + "</span>"; return; }
  var ix = st.index;
  box.innerHTML = "";
  box.appendChild(h("span", {}, ["已索引 ", h("b", {class: "num", text: String(ix.total)}), " 个文件，" + fmtBytes(ix.bytes) + "，用时 " + ix.seconds + " 秒"]));
  box.appendChild(h("button", {class: "ghost sm", text: "重新扫描", onclick: startIndex}));
  if (ix.errors && ix.errors.length) box.appendChild(h("div", {class: "err-text", style: "font-size:12px;width:100%", text: ix.errors.slice(0, 3).join("；")}));
}

// ================================================================ 来源
function renderSources() {
  var box = $("srcList"); box.innerHTML = "";
  if (!S.plan.sources.length) box.appendChild(h("p", {class: "hint", text: "例如放各机位文件夹的大文件夹。可以添加多个。"}));
  S.plan.sources.forEach(function (p, i) {
    box.appendChild(h("div", {class: "src-item", title: p}, [
      h("span", {class: "p mono", text: p}),
      h("button", {class: "ghost sm icon", title: "移除", "aria-label": "移除", text: "×", onclick: function () {
        S.plan.sources.splice(i, 1); S.plan.scan.excludeDirs = []; renderSources(); changed({reindex: true});
      }})
    ]));
  });
  $("depthSel").value = String(S.plan.scan.maxDepth || 0);
  $("skipHidden").checked = S.plan.scan.skipHidden !== false;
}
function addSource(p) {
  p = (p || "").trim().replace(/^["']|["']$/g, "");
  if (!p) return;
  if (S.plan.sources.indexOf(p) >= 0) { toast("这个文件夹已经添加过了"); return; }
  S.plan.sources.push(p); renderSources(); changed({reindex: true});
}
$("btnAddSrc").onclick = function () { openDir("选择来源文件夹", S.plan.sources[S.plan.sources.length - 1] || "", addSource); };
$("srcInput").addEventListener("keydown", function (e) { if (e.key === "Enter") { addSource(this.value); this.value = ""; } });
$("depthSel").onchange = function () { S.plan.scan.maxDepth = Number(this.value); changed({reindex: true}); };
$("skipHidden").onchange = function () { S.plan.scan.skipHidden = this.checked; changed({reindex: true}); };

function dirKey(src, name) { return src + "|" + name; }
function renderDirs() {
  var sec = $("dirSec"), box = $("dirList");
  if (!S.idx || !S.idx.dirs.length) { sec.hidden = true; return; }
  sec.hidden = false; box.innerHTML = "";
  var matchMap = {};
  if (S.q && S.q.facets) S.q.facets.dirs.forEach(function (d) { matchMap[dirKey(d[0], d[1])] = d[3]; });
  var multi = S.idx.sources.length > 1;
  var ex = S.plan.scan.excludeDirs || [];
  S.idx.dirs.forEach(function (d) {
    var key = dirKey(d.src, d.name), off = ex.indexOf(key) >= 0;
    var m = matchMap[key] || 0;
    var label = (d.name || "（直接放在来源里的文件）") + (multi ? " · " + S.idx.sources[d.src].name : "");
    var cb = h("input", {type: "checkbox", checked: !off, "aria-label": label, onchange: function () {
      var i = ex.indexOf(key);
      if (this.checked && i >= 0) ex.splice(i, 1); else if (!this.checked && i < 0) ex.push(key);
      S.plan.scan.excludeDirs = ex; renderDirs(); changed();
    }});
    var pct = d.count ? Math.round(m / d.count * 100) : 0;
    box.appendChild(h("label", {class: "dir-row" + (off ? " off" : ""), title: label}, [
      cb, h("span", {class: "n", text: label}),
      h("span", {class: "c", html: "<b>" + (off ? 0 : m) + "</b> / " + d.count}),
      h("span", {class: "meter"}, [h("i", {style: "width:" + (off ? 0 : pct) + "%"})])
    ]));
  });
}
$("btnDirAll").onclick = function () { S.plan.scan.excludeDirs = []; renderDirs(); changed(); };

// ================================================================ 条件积木
function fieldSelect(c, onChange) {
  var sel = h("select", {"aria-label": "字段"});
  var groups = {};
  S.reg.fields.forEach(function (f) {
    if (!groups[f.group]) { groups[f.group] = h("optgroup", {label: f.group}); sel.appendChild(groups[f.group]); }
    groups[f.group].appendChild(h("option", {value: f.key, text: f.label, title: f.hint || ""}));
  });
  sel.value = c.field;
  sel.onchange = function () { onChange(this.value); };
  return sel;
}
function opSelect(c, onChange) {
  var f = fieldDef(c.field);
  var sel = h("select", {"aria-label": "条件", class: "opsel"});
  S.reg.ops[f.type].forEach(function (o) { sel.appendChild(h("option", {value: o, text: S.reg.opLabels[o]})); });
  sel.value = c.op;
  sel.onchange = function () { onChange(this.value); };
  return sel;
}
function datalistFor(field) {
  var id = "dl-" + field.replace(/\W/g, "");
  var dl = $(id);
  if (!dl) { dl = h("datalist", {id: id}); document.body.appendChild(dl); }
  dl.innerHTML = "";
  var vals = [];
  if (S.idx) {
    if (field === "dir1") vals = S.idx.dirs.map(function (d) { return d.name; }).filter(Boolean);
    if (field === "source") vals = S.idx.sources.map(function (s) { return s.name; });
    if (field === "ext") vals = S.idx.exts.map(function (e) { return e[0]; });
  }
  vals.slice(0, 200).forEach(function (v) { dl.appendChild(h("option", {value: v})); });
  return id;
}
function inp(type, value, onInput, attrs) {
  var a = {type: type, value: value == null ? "" : value};
  for (var k in (attrs || {})) a[k] = attrs[k];
  var e = h("input", a);
  e.addEventListener("input", function () { onInput(this.value); });
  e.addEventListener("change", function () { onInput(this.value); });
  return e;
}
function valueEditor(c) {
  var f = fieldDef(c.field), op = c.op, box = h("div", {class: "val"});
  var set = function (v) { c.value = v; changed(); };
  if (op === "empty" || op === "notEmpty") { box.appendChild(h("span", {class: "faint", text: "—"})); return box; }
  if (f.type === "kind") {
    var cur = Array.isArray(c.value) ? c.value : [];
    var wrap = h("div", {class: "kinds"});
    S.reg.kinds.forEach(function (k) {
      wrap.appendChild(h("button", {class: "chip", "aria-pressed": cur.indexOf(k.key) >= 0 ? "true" : "false", text: k.label, title: k.exts.join(", "), onclick: function () {
        var i = cur.indexOf(k.key); if (i >= 0) cur.splice(i, 1); else cur.push(k.key);
        c.value = cur; this.setAttribute("aria-pressed", i >= 0 ? "false" : "true"); changed();
      }}));
    });
    box.appendChild(wrap); return box;
  }
  if (f.type === "ext") {
    var s = Array.isArray(c.value) ? c.value.join(", ") : (c.value || "");
    box.appendChild(inp("text", s, function (v) { c.value = v.split(/[\s,，;；、]+/).map(function (x) { return x.trim().replace(/^\*?\./, "").toLowerCase(); }).filter(Boolean); changed(); },
      {placeholder: "例如 jpg, cr3, nef", list: datalistFor("ext"), "aria-label": "扩展名"}));
    return box;
  }
  if (f.type === "list") {
    var v = c.value || {};
    box.appendChild(h("span", {class: "muted", text: (v.keys || []).length ? "名单 " + v.keys.length + " 项（" + (S.reg.listModes[v.mode] || "") + "）" : "名单还没填写"}));
    box.appendChild(h("button", {class: "sm", text: "编辑名单…", onclick: function () { openList(c); }}));
    return box;
  }
  if (f.type === "num" || f.type === "size") {
    var numAttrs = {step: "any", "aria-label": "数值"};
    if (op === "between") {
      var arr = Array.isArray(c.value) ? c.value : ["", ""];
      box.appendChild(inp("number", arr[0], function (x) { arr[0] = x; c.value = arr; changed(); }, numAttrs));
      box.appendChild(h("span", {class: "sep", text: "到"}));
      box.appendChild(inp("number", arr[1], function (x) { arr[1] = x; c.value = arr; changed(); }, numAttrs));
    } else box.appendChild(inp("number", c.value, set, numAttrs));
    if (f.type === "size") {
      var us = h("select", {"aria-label": "单位", style: "width:72px;flex:none"});
      ["KB", "MB", "GB"].forEach(function (u) { us.appendChild(h("option", {value: u, text: u})); });
      us.value = c.unit || "MB"; us.onchange = function () { c.unit = this.value; changed(); };
      box.appendChild(us);
    }
    return box;
  }
  if (f.type === "time") {
    if (op === "timeOfDay" || op === "between") {
      var t = op === "timeOfDay" ? "time" : "datetime-local";
      var a2 = Array.isArray(c.value) ? c.value : ["", ""];
      var norm = function (x) { return op === "between" && x && x.length === 10 ? x + "T00:00" : (x || "").replace(" ", "T"); };
      box.appendChild(inp(t, norm(a2[0]), function (x) { a2[0] = x; c.value = a2; changed(); }, {step: 1, "aria-label": "开始"}));
      box.appendChild(h("span", {class: "sep", text: "到"}));
      box.appendChild(inp(t, norm(a2[1]), function (x) { a2[1] = x; c.value = a2; changed(); }, {step: 1, "aria-label": "结束"}));
      return box;
    }
    if (op === "onDate") {
      var dv = Array.isArray(c.value) ? c.value[0] : c.value;
      box.appendChild(inp("date", dv, function (x) { c.value = [x]; changed(); }, {"aria-label": "日期"}));
      return box;
    }
    if (op === "datePreset") {
      var ps = h("select", {"aria-label": "日期范围"});
      Object.keys(S.reg.presets).forEach(function (k) { ps.appendChild(h("option", {value: k, text: S.reg.presets[k]})); });
      ps.value = c.value || "today"; ps.onchange = function () { set(this.value); };
      box.appendChild(ps); return box;
    }
    if (op === "recent") {
      var rv = c.value && typeof c.value === "object" ? c.value : {amount: 1, unit: "days"};
      box.appendChild(inp("number", rv.amount, function (x) { rv.amount = x; c.value = rv; changed(); }, {min: 0, step: "any", "aria-label": "数量", style: "max-width:90px"}));
      var un = h("select", {"aria-label": "单位"});
      Object.keys(S.reg.units).forEach(function (k) { un.appendChild(h("option", {value: k, text: S.reg.units[k]})); });
      un.value = rv.unit || "days"; un.onchange = function () { rv.unit = this.value; c.value = rv; changed(); };
      box.appendChild(un); return box;
    }
    box.appendChild(inp("datetime-local", (c.value || "").replace(" ", "T"), set, {step: 1, "aria-label": "时间"}));
    return box;
  }
  // 字符串
  var ph = {contains: "要包含的文字", notContains: "不能包含的文字", eq: "完整内容", ne: "完整内容", startsWith: "开头的文字",
            endsWith: "结尾的文字", in: "多个值用逗号隔开", notIn: "多个值用逗号隔开", wildcard: "例如 IMG_*;DSC_0??", regex: "例如 ^IMG_\\d{4}$", notRegex: "正则表达式"}[op] || "";
  var val = Array.isArray(c.value) ? c.value.join(", ") : c.value;
  var attrs = {placeholder: ph, "aria-label": "值"};
  if (["dir1", "dir2", "source", "folder", "folders"].indexOf(c.field) >= 0) attrs.list = datalistFor(c.field === "folders" || c.field === "folder" || c.field === "dir2" ? "dir1" : c.field);
  if (op === "regex" || op === "notRegex") attrs.class = "mono";
  box.appendChild(inp("text", val, set, attrs));
  return box;
}
function statText(c, top) {
  if (!isComplete(c)) return {text: "未填写，暂不生效", cls: "todo"};
  if (!top || !S.q || !S.q.condStats) return {text: "", cls: ""};
  var st = null;
  S.q.condStats.forEach(function (x) { if (x.id === c.id) st = x; });
  if (!st) return {text: "", cls: ""};
  if (S.q.mode === "all") {
    if (!st.uniqueBlock) return {text: "", cls: ""};
    return {text: "卡掉 " + st.uniqueBlock, cls: "hot", title: "只因为这一条被排除的文件有 " + st.uniqueBlock + " 个；去掉这一条会多出这些文件"};
  }
  return {text: "命中 " + st.hits, cls: "", title: "单独满足这一条的文件数"};
}
function condRow(c, list, top) {
  var f = fieldDef(c.field);
  var st = statText(c, top);
  var row = h("div", {class: "rule" + (c.off ? " off" : ""), "data-id": c.id, "data-top": top ? "1" : ""});
  row.appendChild(h("input", {type: "checkbox", class: "tog", checked: !c.off, title: "暂时停用 / 启用这一条", "aria-label": "启用", onchange: function () { c.off = !this.checked; changed({rules: true}); }}));
  row.appendChild(fieldSelect(c, function (v) {
    var nf = fieldDef(v); c.field = v;
    if (S.reg.ops[nf.type].indexOf(c.op) < 0 || nf.type !== f.type) { c.op = S.reg.ops[nf.type][0]; c.value = defaultValue(nf.type, c.op); }
    if (nf.type === "size" && !c.unit) c.unit = "MB";
    if (nf.type === "list") { changed({rules: true}); openList(c); return; }
    changed({rules: true});
  }));
  row.appendChild(opSelect(c, function (v) {
    var keep = (["in", "notIn"].indexOf(v) >= 0) === (["in", "notIn"].indexOf(c.op) >= 0) && f.type !== "time" && v !== "between" && c.op !== "between";
    c.op = v; if (!keep) c.value = defaultValue(f.type, v);
    changed({rules: true});
  }));
  row.appendChild(valueEditor(c));
  row.appendChild(h("span", {class: "stat " + st.cls, text: st.text, title: st.title || ""}));
  row.appendChild(h("button", {class: "ghost sm icon rm", title: "删除这一条", "aria-label": "删除", text: "×", onclick: function () {
    list.splice(list.indexOf(c), 1); changed({rules: true});
  }}));
  return row;
}
function groupBox(g, list) {
  var box = h("div", {class: "group"});
  var ms = h("select", {style: "height:26px", "aria-label": "组合方式"});
  [["all", "全部满足"], ["any", "满足任一"], ["none", "都不满足（排除）"]].forEach(function (o) { ms.appendChild(h("option", {value: o[0], text: o[1]})); });
  ms.value = g.mode || "any"; ms.onchange = function () { g.mode = this.value; changed({rules: true}); };
  box.appendChild(h("div", {class: "group-h"}, [h("span", {text: "条件组："}), ms, h("span", {style: "flex:1"}),
    h("button", {class: "ghost sm", text: "＋ 条件", onclick: function () { g.items.push(newCond("name", "contains")); changed({rules: true}); }}),
    h("button", {class: "ghost sm danger", text: "删除组", onclick: function () { list.splice(list.indexOf(g), 1); changed({rules: true}); }})]));
  var inner = h("div", {class: "rules"});
  g.items.forEach(function (c) { inner.appendChild(c.items ? groupBox(c, g.items) : condRow(c, g.items, false)); });
  if (!g.items.length) inner.appendChild(h("p", {class: "hint", text: "组里还没有条件。"}));
  box.appendChild(inner);
  return box;
}
function updateStats() {
  document.querySelectorAll("#rules .rule[data-id]").forEach(function (row) {
    var c = findById(S.plan.rule, row.dataset.id); if (!c) return;
    var st = statText(c, !!row.dataset.top), el = row.querySelector(".stat");
    if (el) { el.textContent = st.text; el.className = "stat " + st.cls; el.title = st.title || ""; }
  });
}
function findById(node, id) {
  var found = null;
  (node.items || []).forEach(function (it) { if (found) return; if (it.id === id) found = it; else if (it.items) found = findById(it, id); });
  return found;
}
function renderRules() {
  var box = $("rules"); box.innerHTML = "";
  $("topMode").value = S.plan.rule.mode || "all";
  var items = topItems();
  if (!items.length) box.appendChild(h("p", {class: "hint", text: "还没有条件，现在显示全部文件。可以在上面用一句话描述，或点“＋ 条件”逐条添加。"}));
  items.forEach(function (c) { box.appendChild(c.items ? groupBox(c, items) : condRow(c, items, true)); });
}
$("topMode").onchange = function () { S.plan.rule.mode = this.value; changed({rules: true}); };
$("btnAddCond").onclick = function () { topItems().push(newCond("name", "contains")); changed({rules: true}); };
$("btnAddGroup").onclick = function () { topItems().push({id: uid("g"), group: true, mode: "any", items: [newCond("name", "contains"), newCond("name", "contains")]}); changed({rules: true}); };
$("btnClearRules").onclick = function () { S.plan.rule.items = []; S.plan.limit.enabled = false; changed({rules: true}); };
$("btnList").onclick = function () { openList(null); };

function renderLimit() {
  var L = S.plan.limit, box = $("limitRow"); box.innerHTML = "";
  var timeFields = S.reg.fields.filter(function (f) { return f.type === "time"; });
  box.appendChild(h("label", {class: "check"}, [h("input", {type: "checkbox", checked: L.enabled, onchange: function () { L.enabled = this.checked; changed(); renderLimit(); }}), "数量上限"]));
  var per = h("select", {"aria-label": "分组"});
  [["dir1", "每个一级文件夹"], ["dir2", "每个二级文件夹"], ["folder", "每个所在文件夹"], ["source", "每个来源"], ["none", "总共"]].forEach(function (o) { per.appendChild(h("option", {value: o[0], text: o[1]})); });
  per.value = L.per || "dir1"; per.onchange = function () { L.per = this.value; changed(); };
  var cnt = inp("number", L.count, function (v) { L.count = Number(v) || 0; changed(); }, {min: 1, step: 1, class: "w-num", "aria-label": "数量", style: "width:70px"});
  var ord = h("select", {"aria-label": "顺序"});
  [["newest", "最新的"], ["oldest", "最早的"], ["largest", "最大的"], ["smallest", "最小的"], ["nameAsc", "名称靠前的"], ["nameDesc", "名称靠后的"]].forEach(function (o) { ord.appendChild(h("option", {value: o[0], text: o[1]})); });
  ord.value = L.order || "newest"; ord.onchange = function () { L.order = this.value; changed(); renderLimit(); };
  var by = h("select", {"aria-label": "时间依据"});
  timeFields.forEach(function (f) { by.appendChild(h("option", {value: f.key, text: f.label})); });
  by.value = L.by || "exif.time"; by.onchange = function () { L.by = this.value; changed(); };
  [per, h("span", {text: "只取"}), cnt, h("span", {text: "个"}), ord].forEach(function (e) { if (!L.enabled) e.disabled = true; box.appendChild(e); });
  if (L.order === "newest" || L.order === "oldest") { box.appendChild(h("span", {text: "，按"})); if (!L.enabled) by.disabled = true; box.appendChild(by); }
}

// ================================================================ 查询
var qTimer = null, qBusyTimer = null, qSeq = 0;
function scheduleQuery() { clearTimeout(qTimer); qTimer = setTimeout(runQuery, 260); }
function runQuery(append) {
  if (!S.idx || !S.gen) return;
  var seq = ++qSeq;
  clearTimeout(qBusyTimer);
  qBusyTimer = setTimeout(function () { $("qBusy").innerHTML = ' <span class="busy" title="计算中"></span>'; }, 250);
  var opts = {histField: S.ui.histField || "", histMode: S.ui.histMode, sort: S.ui.sort, offset: append ? S.items.length : 0, limit: 200};
  api("query", {plan: cleanPlan(), gen: S.gen, opts: opts}).then(function (r) {
    if (seq !== qSeq || r.stale) return;
    clearTimeout(qBusyTimer); $("qBusy").innerHTML = "";
    $("queryErr").hidden = true;
    if (append) { S.items = S.items.concat(r.items); S.q.items = S.items; renderResults(true); return; }
    S.q = r; S.items = r.items;
    if (!S.ui.histField) S.ui.histField = "";
    renderCounts(); updateStats(); renderFacet(); renderResults(); renderTree(); renderDirs(); renderRunBtn();
  }).catch(function (e) {
    if (seq !== qSeq) return;
    clearTimeout(qBusyTimer); $("qBusy").innerHTML = "";
    if (/索引已更新/.test(e.message)) { pollIndex(); return; }
    var b = $("queryErr"); b.textContent = e.message; b.hidden = false;
  });
}
function renderCounts() {
  var q = S.q;
  $("cntMatched").textContent = q ? q.matched : 0;
  $("cntTotal").textContent = "/ " + (q ? q.scoped : (S.idx ? S.idx.total : 0)) + " 个文件" + (q && q.matched ? "，" + fmtBytes(q.matchedBytes) : "");
  var sum = $("summary");
  sum.innerHTML = q ? "<b>当前规则：</b>" + esc(q.summary) + (q.beforeLimit !== q.matched ? "（上限前 " + q.beforeLimit + " 个）" : "") : "";
}

// ================================================================ 分布
document.querySelectorAll("#facetTabs button").forEach(function (b) {
  b.onclick = function () { S.ui.facet = b.dataset.v; saveLocal(); renderFacet(); };
});
function renderFacet() {
  document.querySelectorAll("#facetTabs button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.v === S.ui.facet ? "true" : "false"); });
  var body = $("facetBody"), ctl = $("histCtl"); body.innerHTML = ""; ctl.innerHTML = "";
  if (!S.q) { body.appendChild(h("p", {class: "empty", text: S.plan.sources.length ? "扫描完成后显示分布" : "先在左侧添加来源文件夹"})); return; }
  if (S.ui.facet === "time") return renderHist(body, ctl);
  if (S.ui.facet === "kind") return renderKindFacet(body);
  return renderDirFacet(body);
}
function renderHist(body, ctl) {
  var fs = h("select", {style: "height:28px", "aria-label": "时间依据"});
  S.reg.fields.filter(function (f) { return f.type === "time"; }).forEach(function (f) { fs.appendChild(h("option", {value: f.key, text: f.label})); });
  fs.value = S.q.histField; fs.onchange = function () { S.ui.histField = this.value; saveLocal(); runQuery(); };
  var seg = h("div", {class: "seg"});
  [["timeline", "时间线"], ["tod", "一天中的时刻"]].forEach(function (o) {
    seg.appendChild(h("button", {"aria-pressed": S.ui.histMode === o[0] ? "true" : "false", text: o[1], onclick: function () { S.ui.histMode = o[0]; saveLocal(); runQuery(); }}));
  });
  ctl.appendChild(fs); ctl.appendChild(seg);
  var hs = S.q.hist;
  if (!hs || !hs.count) { body.appendChild(h("p", {class: "empty", text: "没有可显示的时间"})); return; }
  var wrap = h("div", {class: "hist-wrap hist"});
  body.appendChild(wrap);
  body.appendChild(h("p", {class: "hint", text: "深色是符合当前条件的文件。按住鼠标在图上拖动，可以框出一个时段并设为条件。"}));
  drawHist(wrap, hs);
}
function histFmt(hs, v, edge) {
  if (hs.mode === "tod") return todStr(v, hs.size < 60);
  var d = new Date(v * 1000);
  var span = hs.max - hs.min;
  if (hs.size < 60) return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  if (span < 86400 * 1.5) return pad(d.getHours()) + ":" + pad(d.getMinutes());
  if (span < 86400 * 400) return (d.getMonth() + 1) + "-" + d.getDate() + (hs.size < 86400 ? " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) : "");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1);
}
function drawHist(wrap, hs) {
  var NS = "http://www.w3.org/2000/svg";
  var W = Math.max(320, wrap.clientWidth || 600), H = 132, top = 8, bottom = 22, plotH = H - top - bottom;
  var n = hs.total.length, bw = W / n, maxT = Math.max.apply(null, hs.total) || 1;
  var svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "时间分布：共 " + hs.count + " 个文件");
  function rect(x, y, w, hh, cls) { var r = document.createElementNS(NS, "rect"); r.setAttribute("x", x); r.setAttribute("y", y); r.setAttribute("width", Math.max(1, w)); r.setAttribute("height", Math.max(0, hh)); r.setAttribute("class", cls); r.setAttribute("rx", Math.min(2, w / 4)); return r; }
  for (var i = 0; i < n; i++) {
    var t = hs.total[i], m = hs.matched[i];
    if (!t) continue;
    var gap = bw > 4 ? 1 : 0;
    var th = Math.max(2, t / maxT * plotH), mh = m ? Math.max(2, m / maxT * plotH) : 0;
    svg.appendChild(rect(i * bw + gap / 2, top + plotH - th, bw - gap, th, "bar"));
    if (m) svg.appendChild(rect(i * bw + gap / 2, top + plotH - mh, bw - gap, mh, "bar hit"));
    var tt = document.createElementNS(NS, "title");
    tt.textContent = histFmt(hs, hs.start + i * hs.size) + "：" + t + " 个，符合 " + m + " 个";
    svg.lastChild.appendChild(tt);
  }
  var axis = document.createElementNS(NS, "g"); axis.setAttribute("class", "axis");
  var base = document.createElementNS(NS, "line"); base.setAttribute("x1", 0); base.setAttribute("x2", W); base.setAttribute("y1", top + plotH + .5); base.setAttribute("y2", top + plotH + .5); axis.appendChild(base);
  var ticks = Math.min(6, n), lastX = -999;
  for (var k = 0; k <= ticks; k++) {
    var bi = Math.round(k / ticks * n), x = bi * bw;
    if (x - lastX < 70) continue;
    lastX = x;
    var tx = document.createElementNS(NS, "text"); tx.setAttribute("x", Math.min(W - 2, Math.max(2, x))); tx.setAttribute("y", H - 6);
    tx.setAttribute("text-anchor", k === 0 ? "start" : (k === ticks ? "end" : "middle"));
    tx.textContent = histFmt(hs, hs.start + bi * hs.size); axis.appendChild(tx);
  }
  svg.appendChild(axis);
  var brush = rect(0, top, 0, plotH, "brush"); brush.style.display = "none"; svg.appendChild(brush);
  wrap.innerHTML = ""; wrap.appendChild(svg);
  var x0 = null;
  function px(e) { var r = svg.getBoundingClientRect(); return Math.max(0, Math.min(W, (e.clientX - r.left) / r.width * W)); }
  svg.addEventListener("pointerdown", function (e) { x0 = px(e); svg.setPointerCapture(e.pointerId); brush.style.display = ""; brush.setAttribute("x", x0); brush.setAttribute("width", 1); var old = wrap.querySelector(".brush-pop"); if (old) old.remove(); });
  svg.addEventListener("pointermove", function (e) { if (x0 == null) return; var x = px(e); brush.setAttribute("x", Math.min(x0, x)); brush.setAttribute("width", Math.max(1, Math.abs(x - x0))); });
  svg.addEventListener("pointerup", function (e) {
    if (x0 == null) return;
    var x1 = px(e), a = Math.min(x0, x1), b = Math.max(x0, x1); x0 = null;
    if (b - a < 4) { var bi2 = Math.floor(a / bw); a = bi2 * bw; b = (bi2 + 1) * bw; brush.setAttribute("x", a); brush.setAttribute("width", b - a); }
    var v0 = hs.start + a / W * n * hs.size, v1 = hs.start + b / W * n * hs.size;
    showBrushPop(wrap, hs, v0, v1, brush);
  });
}
function showBrushPop(wrap, hs, v0, v1, brush) {
  var fine = hs.size < 60, label, value, op;
  var fl = fieldDef(S.q.histField).label;
  if (hs.mode === "tod") {
    var s0 = fine ? Math.floor(v0) : Math.floor(v0 / 60) * 60;
    var s1 = fine ? Math.ceil(v1) - 1 : Math.ceil(v1 / 60) * 60 - 60;
    if (s1 < s0) s1 = s0;
    value = [todStr(s0, fine), todStr(s1, fine)]; op = "timeOfDay";
    label = fl + " 每天 " + value[0] + "–" + value[1];
  } else {
    var d0 = fine ? Math.floor(v0) : Math.floor(v0 / 60) * 60, d1 = fine ? Math.ceil(v1) - 1 : Math.ceil(v1 / 60) * 60 - 60;
    if (d1 < d0) d1 = d0;
    value = [fmtTs(d0, fine).replace(" ", "T"), fmtTs(d1, fine).replace(" ", "T")]; op = "between";
    label = fl + " " + fmtTs(d0, fine) + " 到 " + fmtTs(d1, fine);
  }
  var pop = h("div", {class: "brush-pop"}, [h("span", {}, ["框选：", h("b", {text: label.replace(fl + " ", "")})]),
    h("button", {class: "primary sm", text: "设为条件", onclick: function () {
      var field = S.q.histField, c = findTop(field, [op]);
      if (c) c.value = value; else topItems().push(newCond(field, op, value));
      if (!S.ui.histField) S.ui.histField = field;
      pop.remove(); changed({rules: true}); toast("已设为条件：" + label);
    }}),
    h("button", {class: "ghost sm", text: "取消", onclick: function () { pop.remove(); brush.style.display = "none"; }})]);
  wrap.appendChild(pop);
}
function toggleIn(field, value) {
  var c = findTop(field, ["in"]);
  if (!c) { topItems().push(newCond(field, "in", [value])); }
  else {
    var arr = Array.isArray(c.value) ? c.value : String(c.value).split(/[,，]/).map(function (x) { return x.trim(); }).filter(Boolean);
    var i = arr.indexOf(value); if (i >= 0) arr.splice(i, 1); else arr.push(value);
    if (!arr.length) topItems().splice(topItems().indexOf(c), 1); else c.value = arr;
  }
  changed({rules: true});
}
function bars(body, rows, onClick, inCond) {
  var g = h("div", {class: "facet-bars"});
  var max = Math.max.apply(null, rows.map(function (r) { return r.total; }).concat([1]));
  rows.forEach(function (r) {
    var sel = inCond && inCond.indexOf(r.key) >= 0;
    g.appendChild(h("button", {class: "lbl", text: (sel ? "✓ " : "") + r.label, title: "点击" + (sel ? "从条件中去掉" : "加入条件"), onclick: function () { onClick(r.key); }}));
    g.appendChild(h("div", {class: "track"}, [h("s", {style: "width:" + (r.total / max * 100) + "%"}), h("i", {style: "width:" + (r.matched / max * 100) + "%"})]));
    g.appendChild(h("span", {class: "n", html: "<b>" + r.matched + "</b> / " + r.total}));
  });
  body.appendChild(g);
}
function condValues(field) { var c = findTop(field, ["in"]); return c ? (Array.isArray(c.value) ? c.value : []) : []; }
function renderKindFacet(body) {
  bars(body, S.q.facets.kinds.map(function (k) { return {key: k[0], label: k[1], total: k[2], matched: k[3]}; }), function (k) { toggleIn("kind", k); }, condValues("kind"));
  body.appendChild(h("p", {class: "hint", style: "margin:12px 0 6px", text: "常见扩展名（点击加入或去掉扩展名条件）："}));
  var ex = h("div", {class: "kinds"}), cur = condValues("ext");
  S.q.facets.exts.slice(0, 30).forEach(function (e) {
    ex.appendChild(h("button", {class: "chip", "aria-pressed": cur.indexOf(e[0]) >= 0 ? "true" : "false", html: esc(e[0] || "无扩展名") + ' <span class="num faint">' + e[2] + "/" + e[1] + "</span>", onclick: function () { toggleIn("ext", e[0]); }}));
  });
  body.appendChild(ex);
}
function renderDirFacet(body) {
  var multi = S.idx && S.idx.sources.length > 1;
  bars(body, S.q.facets.dirs.map(function (d) { return {key: d[1], label: (d[1] || "（来源根目录）") + (multi ? " · " + S.idx.sources[d[0]].name : ""), total: d[2], matched: d[3]}; }),
       function (k) { if (k) toggleIn("dir1", k); }, condValues("dir1"));
}

// ================================================================ 看片台
var IMG_KINDS = {photo: 1, raw: 1};
var thumbQueue = [], thumbActive = 0, observer = null;
function clearThumbs() { Object.keys(S.thumbs).forEach(function (k) { try { URL.revokeObjectURL(S.thumbs[k].url); } catch (e) {} }); S.thumbs = {}; thumbQueue = []; }
function loadThumb(img) {
  var id = img.dataset.id, gen = S.gen;
  if (S.thumbs[id]) { applyThumb(img, S.thumbs[id]); return; }
  thumbQueue.push(img); pumpThumbs();
}
function pumpThumbs() {
  while (thumbActive < 6 && thumbQueue.length) {
    var img = thumbQueue.shift();
    if (!img.isConnected) continue;
    thumbActive++;
    (function (img) {
      var id = img.dataset.id, gen = S.gen;
      fetch("/thumb?gen=" + gen + "&id=" + id + "&t=" + encodeURIComponent(TOKEN)).then(function (r) {
        if (!r.ok) throw new Error("no");
        var o = r.headers.get("X-Orient") || "1";
        return r.blob().then(function (b) { return {url: URL.createObjectURL(b), orient: o}; });
      }).then(function (t) { if (gen !== S.gen) return; S.thumbs[id] = t; applyThumb(img, t); }).catch(function () {
        var p = img.parentNode; if (p) { p.innerHTML = ""; p.appendChild(h("span", {class: "glyph", text: (img.dataset.ext || "?").toUpperCase()})); }
      }).then(function () { thumbActive--; pumpThumbs(); });
    })(img);
  }
}
function applyThumb(img, t) { img.src = t.url; img.className = {"3": "r3", "6": "r6", "8": "r8"}[t.orient] || ""; }
function getObserver() {
  if (observer) return observer;
  observer = new IntersectionObserver(function (ents) { ents.forEach(function (e) { if (e.isIntersecting) { observer.unobserve(e.target); loadThumb(e.target); } }); }, {root: null, rootMargin: "300px"});
  return observer;
}
var STATUS = {same: ["已有相同", ""], rename: ["将改名", "warn"], skip: ["将跳过", ""], overwrite: ["将覆盖", "err"], newer: ["将覆盖", "warn"], older: ["将跳过", ""]};
function visibleItems() {
  var f = ($("resFilter").value || "").trim().toLowerCase();
  if (!f) return S.items;
  return S.items.filter(function (it) { return (it.rel + " " + (it.dest || "")).toLowerCase().indexOf(f) >= 0; });
}
function renderResults() {
  var body = $("resBody"); body.innerHTML = "";
  $("moreBox").hidden = true;
  renderPickBar(); renderListReport();
  document.querySelectorAll("#viewSeg button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.v === S.ui.view ? "true" : "false"); });
  $("sortSel").value = S.ui.sort;
  if (!S.plan.sources.length) { body.appendChild(h("p", {class: "empty", text: "先在左侧添加来源文件夹，这里会显示符合条件的文件。"})); $("resInfo").textContent = ""; return; }
  if (!S.q) { body.appendChild(h("p", {class: "empty", html: '<span class="busy"></span> 正在准备…'})); return; }
  var items = visibleItems();
  $("resInfo").textContent = "共 " + S.q.matched + " 个" + (S.items.length < S.q.matched ? "，已显示 " + S.items.length : "");
  if (!S.q.matched) {
    body.appendChild(h("p", {class: "empty", text: "没有符合条件的文件。看看条件右侧“卡掉”的数字最大的那一条，往往就是太严的条件；也可以在时间分布图上看看文件实际集中在什么时段。"}));
    return;
  }
  if (S.ui.view === "list") {
    var tb = h("tbody");
    items.forEach(function (it) {
      var st = it.status && STATUS[it.status];
      tb.appendChild(h("tr", {class: S.marked[it.id] ? "marked" : "", "data-id": it.id, onclick: function () { itemClick(it); }}, [
        h("td", {class: "p", text: it.name, title: it.rel}), h("td", {class: "p", text: it.dir1 || "—"}),
        h("td", {class: "num", text: fmtTs(it.time)}), h("td", {class: "num", text: fmtBytes(it.size)}),
        h("td", {class: "p mono", text: it.dest || "", title: it.dest || ""}),
        h("td", {}, [st ? h("span", {class: "tag " + st[1], text: st[0]}) : ""])
      ]));
    });
    body.appendChild(h("div", {style: "overflow:auto"}, [h("table", {class: "list"}, [h("thead", {}, [h("tr", {}, ["名称", "一级文件夹", "时间", "大小", "目标位置", ""].map(function (t) { return h("th", {text: t}); }))]), tb])]));
  } else {
    var grid = h("div", {class: "grid"}), ob = getObserver();
    items.forEach(function (it) {
      var pic;
      if (IMG_KINDS[it.kind] || it.ext === "heic") {
        var img = h("img", {alt: "", "data-id": it.id, "data-ext": it.ext, loading: "lazy"});
        pic = h("div", {class: "pic"}, [img]);
        if (S.thumbs[it.id]) applyThumb(img, S.thumbs[it.id]); else ob.observe(img);
      } else pic = h("div", {class: "pic"}, [h("span", {class: "glyph", text: (it.ext || "?").toUpperCase()})]);
      var st = it.status && STATUS[it.status];
      grid.appendChild(h("button", {class: "frame" + (S.marked[it.id] ? " marked" : ""), "data-id": it.id, title: it.rel + (it.dest ? "\n→ " + it.dest : ""), onclick: function () { itemClick(it); }}, [
        pic, st ? h("span", {class: "st", text: st[0]}) : null, h("span", {class: "tick", text: "✓"}),
        h("span", {class: "nm", text: it.name}),
        h("span", {class: "meta"}, [h("span", {text: it.time ? fmtShort(it.time) : (it.dir1 || "")}), h("span", {text: fmtBytes(it.size)})])
      ]));
    });
    body.appendChild(grid);
  }
  if (S.items.length < S.q.matched) { $("moreBox").hidden = false; $("btnMore").textContent = "加载更多（还有 " + (S.q.matched - S.items.length) + " 个）"; }
}
$("btnMore").onclick = function () { runQuery(true); };
$("resFilter").addEventListener("input", debounce(function () { renderResults(); }, 200));
$("sortSel").onchange = function () { S.ui.sort = this.value; saveLocal(); runQuery(); };
document.querySelectorAll("#viewSeg button").forEach(function (b) { b.onclick = function () { S.ui.view = b.dataset.v; saveLocal(); renderResults(); }; });
function itemClick(it) {
  if (S.ui.pick) { toggleMark(it.id); return; }
  openLightbox(S.items.indexOf(it));
}
function toggleMark(id) {
  if (S.marked[id]) delete S.marked[id]; else S.marked[id] = true;
  document.querySelectorAll('#resBody .frame[data-id="' + id + '"], #resBody tr[data-id="' + id + '"]').forEach(function (el) { el.classList.toggle("marked", !!S.marked[id]); });
  renderPickBar();
}
$("btnPick").onclick = function () { S.ui.pick = !S.ui.pick; renderPickBar(); };
function renderPickBar() {
  var bar = $("pickBar"), n = Object.keys(S.marked).length;
  $("btnPick").setAttribute("aria-pressed", S.ui.pick ? "true" : "false");
  $("btnPick").textContent = S.ui.pick ? "完成挑选" : "挑样例";
  if (!S.ui.pick && !n) { bar.hidden = true; return; }
  bar.hidden = false; bar.innerHTML = "";
  bar.appendChild(h("span", {html: S.ui.pick ? "点击结果中你想要的文件，用红笔圈出来。已圈 <b>" + n + "</b> 个。" : "已圈出 <b>" + n + "</b> 个样例。"}));
  bar.appendChild(h("button", {class: "primary sm", text: "根据样例推断规则", disabled: n < 1, onclick: doInfer}));
  if (n) bar.appendChild(h("button", {class: "ghost sm", text: "清除圈选", onclick: function () { S.marked = {}; renderResults(); }}));
  bar.appendChild(h("span", {class: "faint", text: "建议圈 3–5 个，覆盖不同文件夹效果更好。"}));
}
function renderListReport() {
  var box = $("listReport"); box.innerHTML = "";
  var L = S.q && S.q.list;
  if (!L) return;
  var row = h("div", {class: "list-report"}, [
    h("span", {html: "名单 <b class='num'>" + L.total + "</b> 项："}),
    h("span", {class: "tag ok", text: "找到 " + L.found}),
    L.multiple ? h("span", {class: "tag warn", text: "找到多个 " + L.multiple, title: "例如同名的 JPG 和 RAW，或不同文件夹里的同名文件"}) : null,
    h("span", {class: "tag " + (L.missing ? "err" : ""), text: "未找到 " + L.missing})
  ]);
  if (L.missing) {
    row.appendChild(h("button", {class: "sm", text: "复制未找到的", onclick: function () {
      var t = L.missingKeys.join("\n");
      (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(function () { toast("已复制 " + L.missingKeys.length + " 项"); }, function () { download("未找到.txt", t, "text/plain"); });
    }}));
    row.appendChild(h("span", {class: "faint", text: L.missingKeys.slice(0, 8).join("、") + (L.missing > 8 ? "……" : "")}));
  }
  box.appendChild(row);
}
function download(name, text, type) {
  var a = h("a", {href: URL.createObjectURL(new Blob(["﻿" + text], {type: type || "text/plain"})), download: name});
  document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}

// ================================================================ 以例选文
function doInfer() {
  var ids = Object.keys(S.marked).map(Number);
  api("infer", {gen: S.gen, plan: cleanPlan(), ids: ids}).then(function (r) {
    var body = $("inferBody"); body.innerHTML = "";
    body.appendChild(h("p", {class: "muted", style: "margin-bottom:8px", text: "根据 " + r.examples.length + " 个样例（" + r.examples.map(function (e) { return e.name; }).slice(0, 4).join("、") + (r.examples.length > 4 ? "……" : "") + "）找到这些共同点。勾选要用的："}));
    r.suggestions.forEach(function (s) {
      var cb = h("input", {type: "checkbox", checked: s.selected, onchange: function () { s.selected = this.checked; }});
      body.appendChild(h("label", {class: "sug"}, [cb, h("span", {class: "lab", text: s.label}),
        h("span", {class: "cnt", text: s.count == null ? "" : "单独满足 " + s.count + " 个"}), s.note ? h("span", {class: "note", text: s.note}) : null]));
    });
    if (r.dirCount > 1) body.appendChild(h("p", {class: "hint", style: "margin-top:8px", text: "样例来自 " + r.dirCount + " 个一级文件夹；目标里的 {一级文件夹} 会按原文件夹分开存放。"}));
    $("inferInfo").textContent = r.combined != null ? "默认勾选的组合会选中 " + r.combined + " 个文件" : "";
    var apply = function (replace) {
      var conds = r.suggestions.filter(function (s) { return s.selected; }).map(function (s) { var c = clone(s.cond); c.id = uid("c"); if (fieldDef(c.field).type === "size" && !c.unit) c.unit = "MB"; return c; });
      if (!conds.length) { toast("请至少勾选一项"); return; }
      if (replace) { S.plan.rule = {mode: "all", items: conds}; S.plan.limit.enabled = false; }
      else conds.forEach(function (c) { topItems().push(c); });
      $("inferDlg").close(); S.ui.pick = false; S.marked = {};
      changed({rules: true}); toast("已根据样例" + (replace ? "替换" : "追加") + " " + conds.length + " 个条件");
    };
    $("inferReplace").onclick = function () { apply(true); };
    $("inferAppend").onclick = function () { apply(false); };
    $("inferDlg").showModal();
  }).catch(function (e) { toast(e.message, true); });
}

// ================================================================ 一句话
var EXAMPLES = ["每个机位今天 11:38 到 11:41 拍的照片和 RAW", "编号 0120 到 0350 的 CR3", "最近 3 天修改的 Word 和 PDF，大于 5MB", "每个文件夹最新的 10 张照片", "文件名包含“婚礼”的照片，按月份归档"];
function renderExamples() {
  var box = $("askExamples"); box.innerHTML = "";
  EXAMPLES.forEach(function (t) { box.appendChild(h("button", {text: t, onclick: function () { $("askInput").value = t; autoGrow(); doAsk(); }})); });
}
function autoGrow() { var t = $("askInput"); t.style.height = "44px"; t.style.height = Math.min(140, t.scrollHeight + 2) + "px"; }
$("askInput").addEventListener("input", autoGrow);
$("askInput").addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); doAsk(); } });
$("btnAsk").onclick = doAsk;
document.querySelectorAll("#engineSeg button").forEach(function (b) {
  b.onclick = function () {
    if (b.dataset.v === "llm" && !(S.info.settings.llm.hasKey || S.info.settings.llm.provider === "openai")) { toast("请先在“设置”里填写大模型接口"); openSettings(); return; }
    S.ui.engine = b.dataset.v; saveLocal(); renderEngine();
  };
});
function renderEngine() { document.querySelectorAll("#engineSeg button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.v === S.ui.engine ? "true" : "false"); }); }
function doAsk() {
  var text = $("askInput").value.trim();
  if (!text) { $("askInput").focus(); return; }
  var box = $("interp"); box.hidden = false; box.innerHTML = '<span class="busy"></span> ' + (S.ui.engine === "llm" ? "正在请大模型理解…" : "正在理解…");
  $("btnAsk").disabled = true;
  api("nl", {text: text, engine: S.ui.engine}).then(function (r) { showInterp(r); }).catch(function (e) {
    box.innerHTML = ""; box.appendChild(h("p", {class: "err-text", text: e.message}));
  }).then(function () { $("btnAsk").disabled = false; });
}
function showInterp(r) {
  var box = $("interp"); box.innerHTML = "";
  if (!r.items.length && !r.limit && !Object.keys(r.dest || {}).length) {
    box.appendChild(h("p", {class: "muted", text: "没能从这句话里找到可用的条件。可以换个说法，比如加上时间、文件类型或文件名里的字。"}));
    if (r.unknown && r.unknown.length) box.appendChild(h("div", {class: "bits"}, r.unknown.map(function (u) { return h("span", {class: "bit unk", text: "没听懂：" + u}); })));
    return;
  }
  box.appendChild(h("p", {class: "sum", html: "<b>我的理解：</b>" + esc(r.summary || "（只设置了去向或动作）")}));
  var bits = h("div", {class: "bits"});
  (r.understood || []).forEach(function (u) { bits.appendChild(h("span", {class: "bit"}, [h("q", {text: u.text}), u.meaning])); });
  (r.unknown || []).forEach(function (u) { bits.appendChild(h("span", {class: "bit unk", text: "没听懂：" + u, title: "这部分没有对应的条件，可以手动补一条"})); });
  box.appendChild(bits);
  var extra = [];
  if (r.dest && r.dest.template !== undefined) extra.push("子文件夹：" + (r.dest.template || "不建子文件夹"));
  if (r.dest && r.dest.action) extra.push("动作：" + {copy: "拷贝", move: "移动", link: "链接", zip: "打包", list: "只出清单"}[r.dest.action]);
  if (r.dest && r.dest.root) extra.push("目标：" + r.dest.root);
  if (r.dedupe) extra.push("跳过重复文件");
  if (extra.length) box.appendChild(h("p", {class: "muted", style: "font-size:12.5px;margin-bottom:6px", text: extra.join("；")}));
  (r.notes || []).forEach(function (n) { box.appendChild(h("p", {class: "hint", text: n})); });
  var apply = function (replace) {
    var items = clone(r.items); items.forEach(function (it) { it.id = uid("c"); if (it.items) ensureIds(it); });
    if (replace) { S.plan.rule = {mode: "all", items: items}; S.plan.limit.enabled = false; }
    else items.forEach(function (it) { topItems().push(it); });
    if (r.limit) { S.plan.limit = Object.assign({}, S.plan.limit, r.limit, {enabled: true}); }
    if (r.dest) Object.keys(r.dest).forEach(function (k) { S.plan.dest[k] = r.dest[k]; });
    if (r.dedupe != null) S.plan.dest.dedupe = r.dedupe;
    var tf = items.filter(function (it) { return fieldDef(it.field).type === "time"; })[0];
    if (tf) { S.ui.histField = tf.field; if (tf.op === "timeOfDay") S.ui.histMode = "tod"; }
    box.hidden = true; renderDest(); changed({rules: true});
    toast(replace ? "已替换为新的条件" : "已追加到条件");
  };
  box.appendChild(h("div", {class: "row"}, [
    h("button", {class: "primary sm", text: "用这些条件替换", onclick: function () { apply(true); }}),
    h("button", {class: "sm", text: "追加到现有条件", onclick: function () { apply(false); }}),
    h("button", {class: "ghost sm", text: "收起", onclick: function () { box.hidden = true; }})
  ]));
}

// ================================================================ 名单
var listState = null, listTarget = null;
function openList(cond) {
  listTarget = cond; listState = null;
  $("listText").value = cond && cond.value && cond.value.keys ? cond.value.keys.join("\n") : "";
  $("listPreview").innerHTML = ""; $("listInfo").textContent = ""; $("listApply").disabled = true;
  $("listDlg").showModal();
  if ($("listText").value) parseList({text: $("listText").value});
}
function parseList(payload) {
  $("listInfo").innerHTML = '<span class="busy"></span>';
  api("list_parse", payload).then(function (r) { listState = {r: r, payload: payload}; renderListPreview(); }).catch(function (e) { $("listInfo").textContent = e.message; });
}
function renderListPreview() {
  var r = listState.r, box = $("listPreview"); box.innerHTML = "";
  $("listInfo").textContent = "识别到 " + r.count + " 项";
  var keySel = h("select", {"aria-label": "按哪一列查找"});
  r.columns.forEach(function (c, i) { keySel.appendChild(h("option", {value: i, text: c})); });
  keySel.value = r.keyCol; keySel.onchange = function () { r.keyCol = Number(this.value); renderListPreview(); };
  var modeSel = h("select", {"aria-label": "匹配方式"});
  Object.keys(S.reg.listModes).forEach(function (k) { modeSel.appendChild(h("option", {value: k, text: S.reg.listModes[k]})); });
  modeSel.value = r.mode; modeSel.onchange = function () { r.mode = this.value; };
  var hdr = h("input", {type: "checkbox", checked: r.hasHeader, onchange: function () { var p = Object.assign({}, listState.payload, {header: this.checked}); parseList(p); }});
  box.appendChild(h("div", {class: "row", style: "margin-top:12px"}, [h("span", {text: "按这一列查找"}), keySel, h("span", {text: "匹配方式"}), modeSel, r.rows.length > 0 ? h("label", {class: "check"}, [hdr, "第一行是标题"]) : null]));
  box.appendChild(h("p", {class: "hint", style: "margin-top:6px", text: "主文件名相同：IMG_0123 找到 IMG_0123.JPG 和 IMG_0123.CR3；编号相同：0123、123、IMG_0123 都算同一个；文件名包含：名单里的文字出现在文件名中即可。"}));
  var tbl = h("table", {class: "preview-table"});
  tbl.appendChild(h("tr", {}, r.columns.map(function (c, i) { return h("th", {class: i === r.keyCol ? "key" : "", text: c}); })));
  r.rows.slice(0, 8).forEach(function (row) { tbl.appendChild(h("tr", {}, row.map(function (c, i) { return h("td", {class: i === r.keyCol ? "key" : "", text: c}); }))); });
  box.appendChild(tbl);
  if (r.rows.length > 8) box.appendChild(h("p", {class: "hint", text: "……共 " + r.rows.length + " 行"}));
  if (r.columns.length > 1) {
    var colSel = h("select", {"aria-label": "按列分文件夹"});
    colSel.appendChild(h("option", {value: "", text: "不按名单分文件夹"}));
    r.columns.forEach(function (c, i) { if (i !== r.keyCol) colSel.appendChild(h("option", {value: c, text: "按「" + c + "」列分文件夹"})); });
    listState.groupSel = colSel;
    box.appendChild(h("div", {class: "row", style: "margin-top:10px"}, [colSel]));
  }
  $("listApply").disabled = false;
}
$("listParse").onclick = function () { parseList({text: $("listText").value}); };
$("listFileBtn").onclick = function () { $("listFile").click(); };
$("listFile").onchange = function () {
  var f = this.files[0]; if (!f) return;
  var rd = new FileReader();
  rd.onload = function () { var b64 = String(rd.result).split(",")[1]; parseList({b64: b64, filename: f.name}); $("listText").value = ""; };
  rd.readAsDataURL(f); this.value = "";
};
$("listApply").onclick = function () {
  var r = listState.r;
  var keys = r.rows.map(function (row) { return row[r.keyCol]; }).filter(function (k) { return k && String(k).trim(); });
  if (!keys.length) { toast("名单里没有可用的值", true); return; }
  var value = {mode: r.mode, keys: keys, rows: r.rows.filter(function (row) { return row[r.keyCol] && String(row[r.keyCol]).trim(); }), columns: r.columns, keyCol: r.keyCol};
  if (listTarget) { listTarget.field = "list"; listTarget.op = "match"; listTarget.value = value; }
  else {
    var exist = findTop("list", ["match"]);
    if (exist) exist.value = value; else topItems().push(newCond("list", "match", value));
  }
  if (listState.groupSel && listState.groupSel.value) S.plan.dest.template = "{名单." + listState.groupSel.value + "}";
  $("listDlg").close(); renderDest(); changed({rules: true});
  toast("名单已加入条件：" + keys.length + " 项");
};

// ================================================================ 去向
function renderDest() {
  var d = S.plan.dest;
  $("destRoot").value = d.root || ""; $("destTpl").value = d.template == null ? "" : d.template; $("destRename").value = d.rename || "";
  $("zipName").value = d.zipName || "";
  $("conflictSel").value = d.conflict || "rename";
  $("optTimes").checked = d.preserveTimes !== false; $("optVerify").checked = d.verify !== false; $("optDedupe").checked = !!d.dedupe;
  $("watchSec").value = d.watchInterval || 10;
  document.querySelectorAll("#actionSeg button").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.v === (d.action || "copy") ? "true" : "false"); });
  var hints = {copy: "保留原文件，在目标文件夹里放一份副本。", move: "原文件会被移走。可以在“执行记录”里撤销。", link: "建立硬链接，不额外占用空间，要求在同一个磁盘；不支持时自动改为拷贝。",
               zip: "把结果打成一个 zip 压缩包，放在目标文件夹里。", list: "不复制文件，只在目标文件夹生成一份 CSV 清单。"};
  $("actionHint").textContent = hints[d.action || "copy"];
  $("zipBox").hidden = d.action !== "zip";
  $("conflictSel").disabled = d.action === "zip" || d.action === "list";
  var vb = $("tplVars"); vb.innerHTML = "";
  (S.info.vars || []).forEach(function (v) {
    vb.appendChild(h("button", {text: v[0], title: v[1] || "", onclick: function () { insertVar(v[0]); }}));
  });
  renderRunBtn();
}
function insertVar(v) {
  var inpEl = $(S.lastFocusTpl === "destRename" ? "destRename" : "destTpl");
  var s = inpEl.selectionStart == null ? inpEl.value.length : inpEl.selectionStart, e2 = inpEl.selectionEnd == null ? s : inpEl.selectionEnd;
  inpEl.value = inpEl.value.slice(0, s) + v + inpEl.value.slice(e2);
  inpEl.dispatchEvent(new Event("input")); inpEl.focus(); inpEl.setSelectionRange(s + v.length, s + v.length);
}
["destTpl", "destRename"].forEach(function (id) { $(id).addEventListener("focus", function () { S.lastFocusTpl = id; }); });
$("destRoot").addEventListener("input", function () { S.plan.dest.root = this.value.trim(); changed(); });
$("destTpl").addEventListener("input", function () { S.plan.dest.template = this.value; changed(); });
$("destRename").addEventListener("input", function () { S.plan.dest.rename = this.value; changed(); });
$("zipName").addEventListener("input", function () { S.plan.dest.zipName = this.value; changed(); });
$("conflictSel").onchange = function () { S.plan.dest.conflict = this.value; changed(); };
$("optTimes").onchange = function () { S.plan.dest.preserveTimes = this.checked; saveLocal(); };
$("optVerify").onchange = function () { S.plan.dest.verify = this.checked; saveLocal(); };
$("optDedupe").onchange = function () { S.plan.dest.dedupe = this.checked; saveLocal(); };
$("watchSec").onchange = function () { S.plan.dest.watchInterval = Math.max(3, Number(this.value) || 10); saveLocal(); };
$("optWatch").onchange = renderRunBtn;
$("btnDest").onclick = function () { openDir("选择目标文件夹", S.plan.dest.root || "", function (p) { S.plan.dest.root = p; renderDest(); changed(); }); };
document.querySelectorAll("#actionSeg button").forEach(function (b) { b.onclick = function () { S.plan.dest.action = b.dataset.v; renderDest(); changed(); }; });

function renderTree() {
  var box = $("destTree"), q = S.q, sep = (S.info && S.info.sep) || "\\";
  $("destErr").hidden = true; $("statusLine").innerHTML = ""; $("treeInfo").textContent = ""; $("destExample").textContent = "";
  if (q && q.destError) { $("destErr").textContent = q.destError; $("destErr").hidden = false; }
  if (!q || !q.destTree) { box.innerHTML = '<div class="faint">' + (q && q.matched === 0 ? "没有要处理的文件" : "选择目标文件夹后显示") + "</div>"; return; }
  var root = q.destRoot || "（目标文件夹）";
  box.innerHTML = "";
  if (S.plan.dest.action === "zip") {
    box.appendChild(h("div", {}, [h("span", {class: "mono", text: q.zipPath || root})]));
    box.appendChild(h("div", {}, [h("span", {class: "faint", text: "压缩包内的结构："})]));
  } else box.appendChild(h("div", {}, [h("span", {class: "mono", text: root}), h("span", {class: "c", text: ""})]));
  q.destTree.dirs.forEach(function (d) {
    var depth = d[0] ? d[0].split("/").length : 0;
    box.appendChild(h("div", {title: d[0]}, [h("span", {style: "padding-left:" + (depth * 14) + "px", text: (d[0] ? d[0].split("/").pop() : "（直接放在目标文件夹里）")}), h("span", {class: "c", text: d[1] + " 个 · " + fmtBytes(d[2])})]));
  });
  if (q.destTree.dirCount > q.destTree.dirs.length) box.appendChild(h("div", {class: "faint", text: "……共 " + q.destTree.dirCount + " 个文件夹"}));
  $("treeInfo").textContent = q.destTree.dirCount + " 个文件夹";
  var labels = {new: ["将新建", "ok"], same: ["已有相同，跳过", ""], rename: ["重名改名", "warn"], skip: ["重名跳过", ""], overwrite: ["将覆盖", "err"], newer: ["较新覆盖", "warn"], older: ["目标更新，跳过", ""]};
  Object.keys(q.destTree.status).forEach(function (k) { var l = labels[k] || [k, ""]; $("statusLine").appendChild(h("span", {class: "tag " + l[1], text: l[0] + " " + q.destTree.status[k]})); });
  var first = S.items[0];
  if (first && first.dest && q.destRoot) $("destExample").textContent = "例：" + q.destRoot.replace(/[\\\/]+$/, "") + sep + first.dest.split("/").join(sep);
}

// ================================================================ 执行
var ACTION_VERB = {copy: "拷贝", move: "移动", link: "链接", zip: "打包", list: "导出清单"};
function jobRunning() { return S.job && S.job.state === "running"; }
function renderRunBtn() {
  var b = $("btnRun"), n = S.q ? S.q.matched : 0, a = S.plan.dest.action || "copy";
  if (jobRunning()) { b.textContent = S.job.kind === "watch" ? "停止监视" : "停止"; b.disabled = false; return; }
  if ($("optWatch").checked) { b.textContent = "开始监视"; b.disabled = !S.plan.dest.root || a === "zip" || a === "list"; return; }
  b.textContent = n ? ACTION_VERB[a] + " " + n + " 个文件" : "没有要处理的文件";
  b.disabled = !n || !S.plan.dest.root || !!(S.q && S.q.destError);
}
$("btnRun").onclick = function () {
  if (jobRunning()) { api("job_cancel").catch(function () {}); return; }
  var watch = $("optWatch").checked, d = S.plan.dest, q = S.q;
  var st = q && q.destTree ? q.destTree.status : {};
  var body = h("div");
  if (watch) {
    body.appendChild(h("p", {text: "每 " + (d.watchInterval || 10) + " 秒检查一次来源文件夹，新出现且已写入完成的文件按当前规则" + ACTION_VERB[d.action || "copy"] + "到："}));
  } else {
    body.appendChild(h("p", {html: "将<b>" + ACTION_VERB[d.action || "copy"] + " " + q.matched + " 个文件</b>（" + fmtBytes(q.matchedBytes) + "）到："}));
  }
  body.appendChild(h("p", {class: "mono", style: "margin:6px 0 10px;word-break:break-all", text: d.action === "zip" ? (q.zipPath || d.root) : d.root}));
  body.appendChild(h("p", {class: "muted", style: "font-size:12.5px", text: "规则：" + (q ? q.summary : "")}));
  var notes = [];
  if (st.rename) notes.push(st.rename + " 个重名文件会自动改名");
  if (st.same) notes.push(st.same + " 个目标里已有相同文件，会跳过");
  if (st.overwrite) notes.push(st.overwrite + " 个会覆盖目标里的文件（原文件先备份，可撤销）");
  if (d.dedupe) notes.push("内容相同的文件只保留一个");
  if (notes.length) body.appendChild(h("p", {style: "margin-top:8px", text: notes.join("；") + "。"}));
  if (d.action === "move") body.appendChild(h("p", {class: "banner warn", style: "margin-top:10px", text: "移动后原文件夹里就没有这些文件了。需要时可在“执行记录”里撤销。"}));
  confirmDlg(watch ? "开始监视？" : "确认执行", body, watch ? "开始监视" : "开始" + ACTION_VERB[d.action || "copy"]).then(function (ok) {
    if (!ok) return;
    S.logSeq = 0; $("log").innerHTML = ""; $("afterBtns").innerHTML = "";
    api("run", {plan: cleanPlan(), mode: watch ? "watch" : "run"}).then(function () { $("prog").hidden = false; $("prog").scrollIntoView({block: "nearest", behavior: "smooth"}); pollJob(); }).catch(function (e) { toast(e.message, true); });
  });
};
var jobTimer = null;
function pollJob() {
  clearTimeout(jobTimer);
  api("job_status", {since: S.logSeq}).then(function (j) {
    var wasRunning = jobRunning();
    S.job = j; renderJob(j);
    if (j.state === "running") jobTimer = setTimeout(pollJob, 500);
    else if (wasRunning || j.state !== "idle") { renderRunBtn(); if (wasRunning) { toast(j.message, j.state === "error"); runQuery(); } }
  }).catch(function () { jobTimer = setTimeout(pollJob, 1500); });
}
function renderJob(j) {
  if (j.state === "idle") return;
  $("prog").hidden = false;
  $("progPhase").textContent = j.state === "running" ? j.phase : ({done: "已完成", cancelled: "已停止", error: "出错"}[j.state] || j.state);
  $("progTime").textContent = j.elapsed + " 秒";
  var pct = j.bytesTotal ? j.bytesDone / j.bytesTotal : (j.total ? j.processed / j.total : 0);
  if (j.state === "done") pct = 1;
  $("progBar").style.width = Math.min(100, pct * 100).toFixed(1) + "%";
  $("sDone").textContent = j.done; $("sSkip").textContent = j.skipped; $("sFail").textContent = j.failed;
  var m = $("progMsg"); m.textContent = j.message || ""; m.className = j.state === "error" ? "err-text" : "";
  var log = $("log");
  (j.logs || []).forEach(function (l) { log.appendChild(h("div", {class: l.level, text: l.t + "  " + l.msg})); });
  while (log.childNodes.length > 1500) log.removeChild(log.firstChild);
  if (j.logs && j.logs.length) log.scrollTop = log.scrollHeight;
  if (j.logSeq) S.logSeq = j.logSeq;
  renderRunBtn();
  var ab = $("afterBtns"); ab.innerHTML = "";
  if (j.state !== "running") {
    if (j.output) ab.appendChild(h("button", {class: "sm", text: j.action === "zip" ? "打开压缩包位置" : "打开清单", onclick: function () { openPath(j.output, true); }}));
    if (j.root && j.action !== "zip" && j.action !== "list") ab.appendChild(h("button", {class: "sm", text: "打开目标文件夹", onclick: function () { openPath(j.root); }}));
    if (j.logFile) ab.appendChild(h("button", {class: "sm", text: "打开明细（CSV）", onclick: function () { openPath(j.logFile); }}));
    if (j.done) ab.appendChild(h("button", {class: "sm danger", text: "撤销这次", onclick: function () { undoRun(j.runId); }}));
  }
}
function openPath(p, reveal) { api("open_path", {path: p, reveal: !!reveal}).catch(function (e) { toast(e.message, true); }); }
function undoRun(runId) {
  confirmDlg("撤销这次执行？", h("p", {text: "拷贝出的文件会被删除（已被改动过的会保留）；移动的文件会移回原处；被覆盖的文件会从备份恢复。"}), "撤销").then(function (ok) {
    if (!ok) return;
    api("undo", {runId: runId}).then(function (r) { toast(r.message); if (r.notes && r.notes.length) console.warn(r.notes); runQuery(); if ($("histDlg").open) openHistory(); }).catch(function (e) { toast(e.message, true); });
  });
}

// ================================================================ 方案与模板
function refreshPresets() {
  api("presets_list").then(function (r) {
    var tl = $("tplList"); tl.innerHTML = "";
    r.templates.forEach(function (t) {
      tl.appendChild(h("button", {class: "tpl", onclick: function () { applyPreset(t.file, true); }}, [h("b", {text: t.name}), t.description ? h("span", {text: t.description}) : null]));
    });
    var pl = $("presetList"); pl.innerHTML = "";
    if (!r.presets.length) pl.appendChild(h("p", {class: "hint", text: "把常用的设置保存下来，下次一键载入。"}));
    r.presets.forEach(function (p) {
      var row = h("div", {class: "tpl", style: "display:flex;align-items:center;gap:4px;cursor:default"}, [
        h("button", {class: "ghost", style: "flex:1;text-align:left;height:auto;padding:2px 0", onclick: function () { applyPreset(p.file, false); }}, [h("b", {text: p.name}), h("span", {text: p.savedAt ? "保存于 " + p.savedAt.slice(0, 16) : ""})]),
        h("button", {class: "ghost sm icon", title: "删除", "aria-label": "删除方案", text: "×", onclick: function () {
          confirmDlg("删除方案", h("p", {text: "删除“" + p.name + "”？"}), "删除").then(function (ok) { if (ok) api("presets_delete", {file: p.file}).then(refreshPresets); });
        }})
      ]);
      pl.appendChild(row);
    });
  }).catch(function () {});
}
function applyPreset(file, isTemplate) {
  var go = function () {
    api("presets_load", {file: file, template: isTemplate}).then(function (r) {
      var p = r.plan || {};
      if (isTemplate) {
        var keep = {sources: S.plan.sources, scan: S.plan.scan, root: S.plan.dest.root};
        var m = mergePlan(p);
        m.sources = keep.sources; m.scan = keep.scan; m.dest.root = keep.root;
        S.plan = m;
      } else S.plan = mergePlan(p);
      var tf = topItems().filter(function (it) { return !it.items && fieldDef(it.field).type === "time"; })[0];
      if (tf) { S.ui.histField = tf.field; S.ui.histMode = tf.op === "timeOfDay" ? "tod" : "timeline"; }
      renderAll(); changed({reindex: !isTemplate, rules: true});
      if (isTemplate) {
        startIndexIfNeeded();
        var lc = findTop("list", ["match"]);
        if (lc && !isComplete(lc)) openList(lc);
      }
      toast((isTemplate ? "已套用：" : "已载入方案：") + r.name);
    }).catch(function (e) { toast(e.message, true); });
  };
  if (topItems().length) confirmDlg(isTemplate ? "套用“常用场景”" : "载入方案", h("p", {text: isTemplate ? "会替换当前的条件和去向设置（来源和目标文件夹保留）。" : "会替换当前的全部设置。"}), "继续").then(function (ok) { if (ok) go(); });
  else go();
}
function startIndexIfNeeded() { if (S.plan.sources.length && !S.idx) startIndex(); }
$("btnSavePreset").onclick = function () {
  promptDlg("保存当前方案", "方案名称", "").then(function (name) {
    if (!name) return;
    api("presets_save", {name: name, plan: S.plan}).then(function () { toast("已保存方案：" + name); refreshPresets(); }).catch(function (e) { toast(e.message, true); });
  });
};
$("btnExport").onclick = function () { download("方案-" + today() + ".json", JSON.stringify({name: "导出的方案", tool: "file-extract-workbench", version: 2, plan: S.plan}, null, 2), "application/json"); };
$("btnImport").onclick = function () { $("importFile").click(); };
$("importFile").onchange = function () {
  var f = this.files[0]; if (!f) return;
  f.text().then(function (t) { var d = JSON.parse(t); S.plan = mergePlan(d.plan || d.config || d); renderAll(); changed({reindex: true, rules: true}); toast("已导入：" + f.name); })
    .catch(function (e) { toast("导入失败：" + e.message, true); });
  this.value = "";
};

// ================================================================ 弹窗：通用
document.querySelectorAll("dialog [data-close]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });
function confirmDlg(title, bodyEl, okText) {
  return new Promise(function (resolve) {
    $("cfTitle").textContent = title; var b = $("cfBody"); b.innerHTML = ""; b.appendChild(bodyEl);
    $("cfOk").textContent = okText || "确定";
    var dlg = $("confirmDlg"), done = false;
    $("cfOk").onclick = function () { done = true; dlg.close(); resolve(true); };
    dlg.onclose = function () { if (!done) resolve(false); };
    dlg.showModal();
  });
}
function promptDlg(title, placeholder, value) {
  return new Promise(function (resolve) {
    $("pmTitle").textContent = title; var i = $("pmInput"); i.placeholder = placeholder || ""; i.value = value || "";
    var dlg = $("promptDlg"), done = false;
    var ok = function () { done = true; dlg.close(); resolve(i.value.trim()); };
    $("pmOk").onclick = ok; i.onkeydown = function (e) { if (e.key === "Enter") ok(); };
    dlg.onclose = function () { if (!done) resolve(null); };
    dlg.showModal(); i.focus();
  });
}

// ---------------------------------------------------------------- 选择文件夹
var dirCb = null, dirCur = "";
function openDir(title, initial, cb) {
  dirCb = cb; $("dirTitle").textContent = title; $("dirErr").textContent = "";
  $("dirDlg").showModal();
  loadDir(initial || "").catch(function () { loadDir(""); });
}
function loadDir(p) {
  return api("list_dir", {path: p}).then(function (r) {
    dirCur = r.path; $("dirPath").value = r.path; $("dirErr").textContent = "";
    $("dirUp").disabled = !r.path; $("dirUp").dataset.p = r.parent || "";
    var roots = $("dirRoots"); roots.innerHTML = "";
    r.roots.forEach(function (x) { roots.appendChild(h("button", {class: "sm", text: x.name, title: x.path, onclick: function () { loadDir(x.path); }})); });
    var list = $("dirEntries"); list.innerHTML = "";
    if (!r.path) list.appendChild(h("p", {class: "empty", text: "请在上方选择磁盘或常用位置"}));
    else if (!r.dirs.length) list.appendChild(h("p", {class: "empty", text: "（没有子文件夹）"}));
    r.dirs.forEach(function (d) { list.appendChild(h("button", {text: d.name, title: d.path, onclick: function () { loadDir(d.path); }})); });
    $("dirOk").disabled = !r.path;
  }).catch(function (e) { $("dirErr").textContent = e.message; throw e; });
}
$("dirUp").onclick = function () { loadDir(this.dataset.p || "").catch(function () {}); };
$("dirPath").addEventListener("keydown", function (e) { if (e.key === "Enter") loadDir(this.value).catch(function () {}); });
$("dirOk").onclick = function () { if (dirCur && dirCb) dirCb(dirCur); $("dirDlg").close(); };
$("dirNative").onclick = function () {
  var b = this; b.disabled = true; b.textContent = "请在弹出的系统窗口中选择…";
  api("pick_dir", {initial: dirCur, title: $("dirTitle").textContent}).then(function (r) {
    if (r.path) { if (dirCb) dirCb(r.path); $("dirDlg").close(); }
    else if (r.unsupported) $("dirErr").textContent = "这台电脑不支持系统选择窗口，请在本窗口中选择。";
  }).catch(function (e) { $("dirErr").textContent = e.message; }).then(function () { b.disabled = false; b.textContent = "用系统窗口选择…"; });
};

// ---------------------------------------------------------------- 看大图
var lbIndex = -1, lbUrl = null;
function openLightbox(i) {
  if (i < 0 || i >= S.items.length) return;
  lbIndex = i; var it = S.items[i];
  $("lbTitle").textContent = it.name;
  $("lbMark").textContent = S.marked[it.id] ? "取消样例" : "标为样例";
  var pic = $("lbPic"); pic.innerHTML = "";
  if (lbUrl) { URL.revokeObjectURL(lbUrl); lbUrl = null; }
  var q = "?gen=" + S.gen + "&id=" + it.id + "&t=" + encodeURIComponent(TOKEN);
  var vid = {mp4: 1, m4v: 1, mov: 1, webm: 1}, aud = {mp3: 1, wav: 1, m4a: 1, aac: 1, flac: 1, ogg: 1};
  if (IMG_KINDS[it.kind] || it.ext === "heic") {
    pic.appendChild(h("span", {class: "busy"}));
    fetch("/view" + q).then(function (r) { if (!r.ok) throw 0; var o = r.headers.get("X-Orient") || "1"; return r.blob().then(function (b) { return {b: b, o: o}; }); }).then(function (x) {
      lbUrl = URL.createObjectURL(x.b); pic.innerHTML = "";
      var img = h("img", {src: lbUrl, alt: it.name}); var rot = {"3": 180, "6": 90, "8": -90}[x.o];
      if (rot) img.style.transform = "rotate(" + rot + "deg)";
      pic.appendChild(img); addNav(pic);
    }).catch(function () { pic.innerHTML = ""; pic.appendChild(h("span", {class: "glyph", text: it.ext.toUpperCase()})); addNav(pic); });
  } else if (vid[it.ext]) { pic.appendChild(h("video", {src: "/raw" + q, controls: true, preload: "metadata"})); addNav(pic); }
  else if (aud[it.ext]) { pic.appendChild(h("audio", {src: "/raw" + q, controls: true})); addNav(pic); }
  else if (it.ext === "pdf") { pic.appendChild(h("iframe", {src: "/raw" + q, title: it.name})); addNav(pic); }
  else { pic.appendChild(h("span", {class: "glyph", text: (it.ext || "?").toUpperCase()})); addNav(pic); }
  var info = $("lbInfo"); info.innerHTML = '<span class="busy"></span>';
  api("file_info", {gen: S.gen, id: it.id}).then(function (r) {
    info.innerHTML = "";
    info.appendChild(h("p", {class: "mono", style: "word-break:break-all", text: r.path}));
    if (it.dest) info.appendChild(h("p", {style: "margin-top:6px"}, ["将放到：", h("span", {class: "mono", text: it.dest})]));
    var dl = h("dl");
    r.rows.forEach(function (row) { dl.appendChild(h("dt", {text: row[0]})); dl.appendChild(h("dd", {text: row[1]})); });
    info.appendChild(dl);
    $("lbReveal").onclick = function () { openPath(r.path, true); };
  }).catch(function (e) { info.textContent = e.message; });
  $("lbMark").onclick = function () { toggleMark(it.id); $("lbMark").textContent = S.marked[it.id] ? "取消样例" : "标为样例"; };
  if (!$("lbDlg").open) $("lbDlg").showModal();
}
function addNav(pic) {
  if (lbIndex > 0) pic.appendChild(h("button", {class: "nav prev", "aria-label": "上一个", text: "‹", onclick: function () { openLightbox(lbIndex - 1); }}));
  if (lbIndex < S.items.length - 1) pic.appendChild(h("button", {class: "nav next", "aria-label": "下一个", text: "›", onclick: function () { openLightbox(lbIndex + 1); }}));
}
$("lbDlg").addEventListener("keydown", function (e) { if (e.key === "ArrowLeft") openLightbox(lbIndex - 1); if (e.key === "ArrowRight") openLightbox(lbIndex + 1); });
$("lbDlg").addEventListener("close", function () { var v = $("lbPic").querySelector("video,audio"); if (v) v.pause(); });

// ---------------------------------------------------------------- 设置
function openSettings() {
  var l = S.info.settings.llm || {};
  $("setFallback").value = S.plan.timeFallback || "mtime";
  $("setProvider").value = l.provider || "anthropic";
  var def = S.info.llmDefaults[$("setProvider").value] || {};
  $("setBase").value = l.baseUrl || def.baseUrl || ""; $("setModel").value = l.model || def.model || "";
  $("setKey").value = ""; $("setKey").placeholder = l.hasKey ? "已保存（" + l.keyHint + "），留空表示不修改" : "粘贴 API Key";
  $("setTestMsg").textContent = "";
  $("settingsDlg").showModal();
}
$("setProvider").onchange = function () { var def = S.info.llmDefaults[this.value] || {}; $("setBase").value = def.baseUrl || ""; $("setModel").value = def.model || ""; };
function settingsPayload(extra) {
  return Object.assign({llm: {provider: $("setProvider").value, baseUrl: $("setBase").value.trim(), model: $("setModel").value.trim(), apiKey: $("setKey").value.trim(), enabled: true}}, extra || {});
}
$("setSave").onclick = function () {
  S.plan.timeFallback = $("setFallback").value;
  api("settings_save", settingsPayload()).then(function (s) { S.info.settings = s; $("settingsDlg").close(); toast("设置已保存"); changed(); }).catch(function (e) { toast(e.message, true); });
};
$("setClearKey").onclick = function () { api("settings_save", {llm: {clearKey: true}}).then(function (s) { S.info.settings = s; $("setKey").placeholder = "粘贴 API Key"; toast("已清除"); }); };
$("setTest").onclick = function () {
  var m = $("setTestMsg"); m.innerHTML = '<span class="busy"></span>';
  api("settings_save", settingsPayload()).then(function (s) { S.info.settings = s; return api("llm_test"); })
    .then(function () { m.textContent = "连接成功"; m.className = "tag ok"; }).catch(function (e) { m.textContent = e.message; m.className = "err-text"; });
};
$("btnSettings").onclick = openSettings;

// ---------------------------------------------------------------- 执行记录
function openHistory() {
  var body = $("histBody"); body.innerHTML = '<span class="busy"></span>';
  if (!$("histDlg").open) $("histDlg").showModal();
  api("history").then(function (r) {
    body.innerHTML = "";
    if (!r.runs.length) { body.appendChild(h("p", {class: "empty", text: "还没有执行记录"})); return; }
    var tb = h("tbody");
    r.runs.forEach(function (x) {
      var s = x.summary || {};
      tb.appendChild(h("tr", {style: "cursor:default"}, [
        h("td", {class: "num", text: x.startedAt}), h("td", {text: (x.kind === "watch" ? "监视 · " : "") + (ACTION_VERB[x.action] || x.action)}),
        h("td", {class: "num", text: (s.done || 0) + " / 跳过 " + (s.skipped || 0) + (s.failed ? " / 失败 " + s.failed : "")}),
        h("td", {class: "p", text: x.description || "", title: x.description || ""}),
        h("td", {class: "p mono", text: x.output || x.root || "", title: x.output || x.root || ""}),
        h("td", {}, [x.undone ? h("span", {class: "tag", text: "已撤销"}) : h("button", {class: "sm danger", text: "撤销", onclick: function () { undoRun(x.runId); }}),
                     (x.output || x.root) ? h("button", {class: "ghost sm", text: "打开", onclick: function () { openPath(x.output || x.root, !!x.output); }}) : null])
      ]));
    });
    body.appendChild(h("div", {style: "overflow:auto"}, [h("table", {class: "list"}, [h("thead", {}, [h("tr", {}, ["时间", "动作", "结果", "规则", "位置", ""].map(function (t) { return h("th", {text: t}); }))]), tb])]));
  }).catch(function (e) { body.textContent = e.message; });
}
$("btnHistory").onclick = openHistory;

// ---------------------------------------------------------------- 主题与退出
function applyTheme() { var t = lsGet(LS_THEME); if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); }
$("btnTheme").onclick = function () {
  var cur = lsGet(LS_THEME), dark = matchMedia("(prefers-color-scheme: dark)").matches;
  var next = cur ? (cur === "dark" ? "light" : "dark") : (dark ? "light" : "dark");
  lsSet(LS_THEME, next); applyTheme(); if (S.q) renderFacet();
};
$("btnQuit").onclick = function () {
  confirmDlg("退出工作台？", h("p", {text: (jobRunning() ? "正在运行的任务会被停止。" : "") + "退出后需要重新双击启动脚本才能再次使用。"}), "退出").then(function (ok) {
    if (!ok) return;
    api("shutdown").then(function () { document.body.innerHTML = '<div style="max-width:520px;margin:22vh auto;text-align:center;font-size:16px;line-height:1.9">工作台已退出，可以关闭这个页面了。<br><span style="color:var(--ink-3);font-size:14px">下次使用请再次双击启动脚本。</span></div>'; });
  });
};
window.addEventListener("resize", debounce(function () { if (S.q && S.ui.facet === "time") renderFacet(); }, 200));

// ================================================================ 启动
function renderAll() { renderSources(); renderRules(); renderLimit(); renderDest(); renderEngine(); renderExamples(); renderFacet(); renderResults(); renderTree(); }
function init() {
  applyTheme();
  if (location.protocol === "file:") { $("fileBanner").hidden = false; $("shell").hidden = true; return; }
  api("info").then(function (info) {
    S.info = info; S.reg = info.registry;
    S.reg.fields.forEach(function (f) { S.fields[f.key] = f; });
    $("ver").textContent = "v" + info.toolVersion;
    $("sysLabel").innerHTML = '<i class="dot ok"></i>' + esc(info.osLabel) + " · Python " + esc(info.engineVersion) + (info.bundled ? "（内置）" : "");
    var saved = lsGet(LS_PLAN), ui = lsGet(LS_UI);
    S.plan = mergePlan(saved || {});
    if (ui) Object.keys(ui).forEach(function (k) { if (k !== "pick") S.ui[k] = ui[k]; });
    renderAll(); refreshPresets();
    if (S.plan.sources.length) startIndex(); else renderIdxStatus({state: "idle"});
    api("job_status", {since: 0}).then(function (j) { if (j.state !== "idle") { S.job = j; renderJob(j); if (j.state === "running") pollJob(); } });
  }).catch(function (e) {
    $("sysLabel").innerHTML = '<i class="dot bad"></i>未连接';
    var b = $("queryErr"); b.hidden = false; b.textContent = "无法连接后台（" + e.message + "）。请确认启动脚本的窗口仍开着；必要时关闭本页，重新双击启动脚本。";
  });
}
init();
})();
