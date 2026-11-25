const ZOOMS = [0, 1, 2, 3];
const KINDS = ["regular", "emergency"];
const CN = {
  idle: "空闲", queued: "排队中", running: "渲染中",
  interrupted: "被抢占·待续算", done: "已完成", failed: "失败",
};
const KIND_CN = { regular: "常规底图", emergency: "应急航片" };

const cells = {};

function buildGrid(kind) {
  const host = document.querySelector(`#panel-${kind} .pyramid`);
  const map = {};
  for (const z of ZOOMS) {
    const n = 2 ** z;
    const row = document.createElement("div");
    row.className = "zrow";
    row.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
    row.style.maxWidth = `${Math.min(n, 8) * 64}px`;
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const a = document.createElement("a");
        a.className = "cell empty";
        a.target = "_blank";
        a.title = `${KIND_CN[kind]} z${z} ${x}/${y}（点击预览）`;
        a.setAttribute("role", "button");
        a.tabIndex = 0;
        a.addEventListener("click", (ev) => {
          // 普通左键：打开单格预览（拦截 <a> 默认的新标签打开 PNG）。
          // 带修饰键 / 中键：保留浏览器原生行为（已发布格可直接开原 PNG）。
          if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
          ev.preventDefault();
          openPreview(kind, z, x, y);
        });
        a.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            openPreview(kind, z, x, y);
          }
        });
        row.appendChild(a);
        map[`${z}/${x}/${y}`] = a;
      }
    }
    host.appendChild(row);
  }
  return map;
}

for (const k of KINDS) cells[k] = buildGrid(k);

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const t = await r.text();
    alert(`操作被拒绝：${r.status} ${t}`);
  }
  poll();
}

document.querySelectorAll("button[data-act]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const kind = btn.closest(".panel").id.replace("panel-", "");
    if (btn.dataset.act === "start") {
      let url = `/api/jobs/${kind}/start`;
      if (kind === "emergency" && document.getElementById("simulate-fail").checked) {
        url += "?simulate_fail=true";
      }
      post(url);
    } else if (btn.dataset.act === "reset") {
      post(`/api/jobs/${kind}/reset`);
    }
  });
});

async function poll() {
  let s;
  try {
    const r = await fetch("/api/status");
    s = await r.json();
  } catch (e) {
    return;
  }
  document.getElementById("global").textContent =
    "当前渲染进程：" + (s.running_kind ? KIND_CN[s.running_kind] : "无（空闲）");
  for (const kind of KINDS) {
    const j = s.jobs[kind];
    const panel = document.getElementById(`panel-${kind}`);
    panel.dataset.status = j.status;
    panel.querySelector(".badge").textContent = CN[j.status] || j.status;
    panel.querySelector(".fill").style.width = j.percent + "%";
    let info = `${j.published}/${j.total}（${j.percent}%）`;
    if (j.current_tile) info += ` · 正在渲染 ${j.current_tile.join("/")}`;
    if (j.plan) info += ` · 本轮计划 ${j.plan.remaining}/${j.plan.total} 张`;
    if (j.error) info += ` · 错误：${j.error}`;
    panel.querySelector(".info").textContent = info;
    const pub = new Set(j.tiles);
    for (const key in cells[kind]) {
      const a = cells[kind][key];
      const has = pub.has(key);
      if (has && !a.dataset.on) {
        a.dataset.on = "1";
        a.classList.remove("empty");
        const img = document.createElement("img");
        img.src = `/tiles/${kind}/${key}.png`;
        img.alt = key;
        a.href = img.src;
        a.appendChild(img);
      } else if (!has && a.dataset.on) {
        delete a.dataset.on;
        a.classList.add("empty");
        a.innerHTML = "";
        a.removeAttribute("href");
      }
    }
  }
}

setInterval(poll, 500);
poll();

// ---------------------------------------------------------------------------
// 单格预览：独立地址 + 定时自动重验证（手动带 If-None-Match）
// ---------------------------------------------------------------------------

const pvModal = document.getElementById("preview-modal");
const pvTitle = document.getElementById("preview-title");
const pvImage = document.getElementById("preview-image");
const pvEmpty = document.getElementById("preview-empty");
const pvSource = document.getElementById("preview-source");
const pvUrl = document.getElementById("preview-url");
const pvRev = document.getElementById("preview-rev");
const pvMapRow = document.getElementById("preview-maprow");
const pvMapLink = document.getElementById("preview-context-link");
const pvMapImg = document.getElementById("preview-context-img");
const pvMapCrop = document.getElementById("preview-context-crop");
const pvRawLink = document.getElementById("preview-raw-link");
const pvRawNa = document.getElementById("preview-raw-na");

const pv = {
  open: false, kind: null, z: null, x: null, y: null,
  etag: null, timer: null, objectUrl: null, seq: 0,
  revCount: 0, lastStatus: null,
};

function previewPath() {
  return `/preview/${pv.kind}/${pv.z}/${pv.x}/${pv.y}.png`;
}

function openPreview(kind, z, x, y) {
  pv.open = true;
  pv.seq += 1; // 使上一格在途请求失效，避免旧响应覆盖新格
  pv.kind = kind; pv.z = z; pv.x = x; pv.y = y;
  pv.etag = null;
  pv.revCount = 0;
  pv.lastStatus = null;
  if (pv.objectUrl) { URL.revokeObjectURL(pv.objectUrl); pv.objectUrl = null; }

  pvModal.hidden = false;
  pvModal.dataset.kind = kind;
  pvModal.dataset.z = String(z);
  pvModal.dataset.x = String(x);
  pvModal.dataset.y = String(y);
  pvTitle.textContent = `单格预览 · ${KIND_CN[kind] || kind} z${z}/${x}/${y}`;
  pvUrl.textContent = previewPath();
  pvModal.dataset.previewUrl = previewPath();
  pvImage.dataset.previewUrl = previewPath();
  pvSource.textContent = "正在取图…";
  pvSource.dataset.available = "0";
  pvImage.hidden = true;
  pvEmpty.hidden = false;
  pvEmpty.textContent = "加载中…";
  pvMapRow.hidden = true;
  pvRawLink.hidden = true;
  pvRawNa.hidden = true;

  schedulePreview(0); // 立即拉一次
}

function closePreview() {
  pv.open = false;
  pv.seq += 1; // 使在途响应与定时器链全部作废
  if (pv.timer) { clearTimeout(pv.timer); pv.timer = null; }
  if (pv.objectUrl) { URL.revokeObjectURL(pv.objectUrl); pv.objectUrl = null; }
  pvModal.hidden = true;
}

document.getElementById("preview-close").addEventListener("click", closePreview);
pvModal.addEventListener("click", (ev) => { if (ev.target === pvModal) closePreview(); });
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && pv.open) closePreview();
});

function schedulePreview(delay) {
  if (pv.timer) clearTimeout(pv.timer);
  pv.timer = setTimeout(pollPreview, delay);
}

function headerNum(r, name) {
  const v = r.headers.get(name);
  return v === null ? null : Number(v);
}

function applySource(r) {
  // 优先用聚合 JSON 头，缺失时退回离散头
  let meta = null;
  const metaRaw = r.headers.get("X-Preview-Meta");
  if (metaRaw) { try { meta = JSON.parse(metaRaw); } catch (e) { meta = null; } }

  const type = r.headers.get("X-Source-Type") || (meta && meta.type) || "";
  const sz = headerNum(r, "X-Source-Z") ?? (meta && meta.z);
  const sx = headerNum(r, "X-Source-X") ?? (meta && meta.x);
  const sy = headerNum(r, "X-Source-Y") ?? (meta && meta.y);
  const scale = headerNum(r, "X-Scale") ?? (meta && meta.scale) ?? 1;
  const cropRaw = r.headers.get("X-Crop-Box") || (meta && meta.crop_box) || "";
  const crop = cropRaw.split(",").map(Number);
  const orect = r.headers.get("X-Original-Rect") ||
    (meta && meta.original_rect ? meta.original_rect.join(",") : "");
  const isOfficial = type === "official" ||
    (sz === pv.z && sx === pv.x && sy === pv.y && scale === 1);

  pvSource.dataset.available = "1";
  pvSource.dataset.sourceType = isOfficial ? "official" : "ancestor";
  pvSource.dataset.sourceZ = String(sz);
  pvSource.dataset.sourceX = String(sx);
  pvSource.dataset.sourceY = String(sy);
  pvSource.dataset.scale = String(scale);
  if (crop.length === 4) pvSource.dataset.crop = crop.join(",");
  if (orect) pvSource.dataset.originalRect = orect;
  if (pv.etag) pvSource.dataset.etag = pv.etag;

  const cropText = crop.length === 4
    ? `瓦内裁剪 (${crop[0]}, ${crop[1]})–(${crop[2]}, ${crop[3]})` : "";
  const origText = orect ? `；对应 2048 源图坐标 [${orect}]` : "";
  if (isOfficial) {
    pvSource.innerHTML =
      `来源：<b>正式瓦片</b> z${sz}/${sx}/${sy}（整格 256×256，1:1）${origText}`;
  } else {
    pvSource.innerHTML =
      `来源：<b>最近已发布上级瓦片</b> z${sz}/${sx}/${sy}；${cropText}；` +
      `放大 ${scale}× 到 256×256${origText}`;
  }

  // 取图位置示意：来源瓦片原 PNG + 红框标出裁剪区
  if (sz !== null && sx !== null && sy !== null) {
    const tileUrl = `/tiles/${pv.kind}/${sz}/${sx}/${sy}.png`;
    pvMapRow.hidden = false;
    pvMapLink.href = tileUrl;
    pvMapImg.src = tileUrl;
    if (crop.length === 4) {
      const [l, t, rr, b] = crop;
      pvMapCrop.style.left = `${(l / 256) * 100}%`;
      pvMapCrop.style.top = `${(t / 256) * 100}%`;
      pvMapCrop.style.width = `${((rr - l) / 256) * 100}%`;
      pvMapCrop.style.height = `${((b - t) / 256) * 100}%`;
    }
  }

  // “本格原 PNG”链接只在正式瓦片已发布时给出（/tiles 地址仍只供正式瓦片）
  if (isOfficial) {
    const raw = `/tiles/${pv.kind}/${pv.z}/${pv.x}/${pv.y}.png`;
    pvRawLink.href = raw;
    pvRawLink.hidden = false;
    pvRawNa.hidden = true;
  } else {
    pvRawLink.hidden = true;
    pvRawNa.hidden = false;
  }
}

function showEmpty() {
  if (pv.objectUrl) { URL.revokeObjectURL(pv.objectUrl); pv.objectUrl = null; }
  pvImage.removeAttribute("src");
  pvImage.hidden = true;
  pvEmpty.hidden = false;
  pvEmpty.textContent = "暂无图";
  pvSource.dataset.available = "0";
  pvSource.removeAttribute("data-source-type");
  pvSource.textContent = "暂无可用取图来源：该格正式瓦片尚未发布，也没有已发布的上级瓦片。";
  pvMapRow.hidden = true;
  pvRawLink.hidden = true;
  pvRawNa.hidden = false;
}

async function pollPreview() {
  if (!pv.open) return;
  const seq = pv.seq;
  const current = () => pv.open && seq === pv.seq;
  const url = previewPath();
  const headers = {};
  if (pv.etag) headers["If-None-Match"] = pv.etag;
  let r;
  try {
    r = await fetch(url, { headers, cache: "no-store" });
  } catch (e) {
    // 网络抖动：保留当前图，下一轮继续重验证
    if (current()) schedulePreview(1000);
    return;
  }
  if (!current()) return; // 已关闭或切换到别的格子，丢弃本次旧响应

  pv.revCount += 1;
  pv.lastStatus = r.status;
  const srvEtag = r.headers.get("ETag");
  if (srvEtag) pv.etag = srvEtag;

  if (r.status === 304) {
    // 来源坐标 + 原 PNG 内容未变：继续显示本次已打开的图，仅同步来源头
    applySource(r);
    pvRev.textContent = `自动重验证 #${pv.revCount}：304 未变化（If-None-Match 命中，跳过裁剪）`;
    document.dispatchEvent(new CustomEvent("preview-revalidated", { detail: { status: 304 } }));
  } else if (r.status === 404) {
    pv.etag = null;
    showEmpty();
    pvRev.textContent = `自动重验证 #${pv.revCount}：404 暂无图，继续等待发布…`;
    document.dispatchEvent(new CustomEvent("preview-revalidated", { detail: { status: 404 } }));
  } else if (r.ok) {
    const blob = await r.blob();
    if (!current()) return; // blob 读取期间可能已关闭/换格
    if (pv.objectUrl) URL.revokeObjectURL(pv.objectUrl);
    pv.objectUrl = URL.createObjectURL(blob);
    pvImage.src = pv.objectUrl;
    pvImage.hidden = false;
    pvEmpty.hidden = true;
    applySource(r);
    pvRev.textContent = `自动重验证 #${pv.revCount}：200 已取图（ETag ${pv.etag ? pv.etag.slice(0, 18) + "…" : "无"}）`;
    document.dispatchEvent(new CustomEvent("preview-revalidated", { detail: { status: 200 } }));
  } else {
    pvRev.textContent = `自动重验证 #${pv.revCount}：${r.status} 异常，下轮重试`;
  }

  if (current()) schedulePreview(700);
}
