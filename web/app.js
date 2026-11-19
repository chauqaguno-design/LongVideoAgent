"use strict";

const $ = (id) => document.getElementById(id);
const PART_SIZE = 5 * 1024 * 1024;
const PAGE_SIZE = 50;
const JOBS = {
  uploading: "上传中", queued: "等待处理", processing: "处理中",
  ready: "已就绪", partial: "部分就绪", failed: "处理失败"
};
const TRACKS = { speech: "语音", subtitles: "字幕", visual: "动作" };
const TRACK_STATUS = {
  pending: "待处理", processing: "处理中", ready: "已完成", empty: "无内容",
  unavailable: "不可用", failed: "失败"
};
const VIEWS = { scenes: "场景", chunks: "检索块", evidence: "证据" };
const player = $("player");
const state = {
  videos: [], id: null, video: null, upload: null, view: "scenes",
  scene: null, chunk: null, track: "", limit: PAGE_SIZE
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function duration(ms, precise = false) {
  const value = Math.max(0, Math.floor(Number(ms) || 0));
  const seconds = Math.floor(value / 1000);
  const hours = Math.floor(seconds / 3600);
  const pad = (n) => String(n).padStart(2, "0");
  const clock = (hours ? pad(hours) + ":" : "") + pad(Math.floor(seconds / 60) % 60) + ":" + pad(seconds % 60);
  return precise && value % 1000 ? clock + "." + String(value % 1000).padStart(3, "0") : clock;
}

function sizeText(bytes) {
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + " GiB";
  return (bytes / 1024 ** 2).toFixed(1) + " MiB";
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const message = data.detail || data.error || data.message || ("HTTP " + response.status);
    const error = new Error(typeof message === "string" ? message : JSON.stringify(message));
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

// The first pass keeps only a 32-byte digest for each 5 MiB part, even for long films.
async function fingerprint(file, signal) {
  const count = Math.ceil(file.size / PART_SIZE);
  const digests = new Uint8Array(count * 32);
  for (let i = 0; i < count; i++) {
    signal.throwIfAborted();
    const part = file.slice(i * PART_SIZE, (i + 1) * PART_SIZE);
    const hash = await crypto.subtle.digest("SHA-256", await part.arrayBuffer());
    digests.set(new Uint8Array(hash), i * 32);
    showUpload("正在校验文件", (i + 1) / count);
  }
  return { digests, count, id: hex(new Uint8Array(await crypto.subtle.digest("SHA-256", digests))) };
}

function hex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function showUpload(label, fraction) {
  const percent = Math.round(Math.min(1, Math.max(0, fraction)) * 100);
  $("upload-message").textContent = label;
  $("upload-value").textContent = percent + "%";
  $("upload-bar").value = percent;
}

async function sendPart(path, body, hash, signal) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await api(path, {
        method: "PUT", body, signal,
        headers: { "X-Part-SHA256": hash, "Content-Type": "application/octet-stream" }
      });
    } catch (error) {
      if (signal.aborted || attempt >= 2 || (error.status && error.status < 500 && error.status !== 429)) throw error;
      await new Promise((resolve) => setTimeout(resolve, 600 * (attempt + 1)));
    }
  }
}

async function uploadFile(file) {
  if (state.upload) return notice("已有文件正在上传。可以先暂停，再选择其他文件。");
  if (!file || !file.size || !/\.mp4$/i.test(file.name)) return notice("请选择一份非空的 MP4 视频。");
  if (!window.crypto?.subtle) return notice("浏览器需要通过 localhost 或 HTTPS 打开工作台，才能校验上传分片。");
  const controller = new AbortController();
  const signal = controller.signal;
  state.upload = controller;
  notice();
  $("upload-progress").hidden = false;
  $("pause-upload").hidden = false;
  $("upload-name").textContent = file.name;
  showUpload("正在校验文件", 0);
  try {
    const fileHash = await fingerprint(file, signal);
    const session = await api("/api/uploads", {
      method: "POST", signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name, size: file.size, fingerprint: fileHash.id })
    });
    if (session.part_size !== PART_SIZE) throw new Error("客户端与服务端的分片大小不一致。");
    const id = String(session.id);
    const path = "/api/uploads/" + encodeURIComponent(id);
    const received = new Set(session.received.map(Number));
    await openVideo(id);
    showUpload("正在上传分片", received.size / fileHash.count);
    for (let i = 0; i < fileHash.count; i++) {
      signal.throwIfAborted();
      if (received.has(i)) continue;
      const part = file.slice(i * PART_SIZE, (i + 1) * PART_SIZE);
      const hash = hex(fileHash.digests.subarray(i * 32, (i + 1) * 32));
      await sendPart(path + "/parts/" + i, part, hash, signal);
      received.add(i);
      showUpload("已接收 " + received.size + " / " + fileHash.count + " 片", received.size / fileHash.count);
    }
    showUpload("分片已齐，正在验证原片", 1);
    await api(path + "/complete", { method: "POST", signal });
    $("upload-progress").hidden = true;
    await refreshLibrary();
    await openVideo(id);
  } catch (error) {
    $("upload-message").textContent = signal.aborted ? "已暂停。重新选择同一文件可继续上传。" : "重新选择同一文件可重试。";
    if (!signal.aborted) notice("上传未完成。" + error.message);
  } finally {
    state.upload = null;
    $("pause-upload").hidden = true;
    $("file").value = "";
  }
}

function renderLibrary() {
  const history = $("history");
  history.replaceChildren();
  $("video-count").textContent = state.videos.length;
  if (!state.videos.length) history.append(element("p", "muted", "暂无素材"));
  for (const video of state.videos) {
    const active = state.id === String(video.id);
    const button = element("button", "history-item" + (active ? " active" : ""));
    button.type = "button";
    button.setAttribute("aria-current", active ? "true" : "false");
    button.title = video.name;
    const label = element("span", "history-text");
    label.append(element("strong", null, video.name), element("small", null, [JOBS[video.status] || video.status, sizeText(video.size)].join(" · ")));
    button.append(element("span", "clip-icon", "▶"), label);
    button.onclick = () => openVideo(String(video.id));
    history.append(button);
  }
}

async function refreshLibrary() {
  const videos = await api("/api/videos");
  state.videos = videos;
  renderLibrary();
}

async function openVideo(id) {
  if (state.id !== id) {
    state.id = id;
    state.video = null;
    state.scene = state.chunk = null;
    state.view = "scenes";
    state.track = "";
    state.limit = PAGE_SIZE;
    try { localStorage.setItem("cinetrace.video", id); } catch { /* Browser storage is optional. */ }
    renderLibrary();
    renderDetail();
  }
  try {
    const video = await api("/api/videos/" + encodeURIComponent(id));
    if (state.id !== id) return;
    state.video = video;
    renderDetail();
  } catch (error) {
    if (state.id === id) notice("读取素材失败。" + error.message);
  }
}

function renderDetail() {
  const video = state.video;
  $("title").textContent = video?.name || "原片工作区";
  $("metadata").textContent = video
    ? [sizeText(video.size), video.duration_ms > 0 ? "原片时长 " + duration(video.duration_ms) : ""].filter(Boolean).join(" · ")
    : "选择素材或上传一份视频";
  const status = $("job-status");
  status.hidden = !video;
  status.textContent = JOBS[video?.status] || video?.status || "";
  status.className = "badge " + (video?.status || "");
  const mediaId = video && video.status !== "uploading" ? String(video.id) : "";
  if ((player.dataset.id || "") !== mediaId) {
    player.pause();
    player.dataset.id = mediaId;
    if (mediaId) player.src = "/api/videos/" + encodeURIComponent(mediaId) + "/media";
    else player.removeAttribute("src");
    player.load();
    $("play-time").textContent = "00:00";
  }
  player.hidden = !mediaId;
  $("empty-player").hidden = !!mediaId;
  renderTracks();
  renderResults();
}

function renderTracks() {
  const video = state.video;
  const panel = $("tracks");
  panel.replaceChildren();
  for (const [key, label] of Object.entries(TRACKS)) {
    const track = video?.tracks?.[key] || { status: "pending" };
    const count = (video?.evidence || []).filter((item) => item.track === key).length;
    const button = element("button", "track");
    button.type = "button";
    button.title = "查看" + label + "证据";
    const top = element("span", "track-top");
    const name = element("span", "track-name");
    name.append(element("i", "dot " + key), document.createTextNode(label));
    const status = track.status === "processing" && Number.isFinite(track.progress)
      ? "处理中 " + track.progress + "%" : TRACK_STATUS[track.status] || track.status;
    top.append(name, element("span", "track-status " + track.status, status));
    const message = track.status === "ready" ? "" : track.message;
    button.append(top, element("small", null, count + " 条证据" + (message ? " · " + message : "")));
    button.onclick = () => { changeView("evidence", key); $("records").scrollIntoView({ block: "nearest" }); };
    panel.append(button);
  }
  const percent = Math.round(Math.max(0, Math.min(100, Number(video?.progress) || 0)));
  $("stage-label").textContent = video?.stage || "等待视频";
  $("stage-value").textContent = video ? percent + "%" : "";
  $("stage-bar").value = percent;
  $("index-status").hidden = !video?.index?.message;
  $("index-status").textContent = video?.index?.message || "";
  $("task-error").hidden = !video?.error;
  $("task-error").textContent = video?.error || "";
  $("retry").hidden = !["partial", "failed"].includes(video?.status);
}

function seekTo(ms) {
  const targetId = player.dataset.id;
  if (!targetId) return;
  const seek = () => {
    if (player.dataset.id !== targetId) return;
    const seconds = Math.max(0, Number(ms) || 0) / 1000;
    player.currentTime = Number.isFinite(player.duration) ? Math.min(seconds, player.duration) : seconds;
    player.play().catch(() => {});
  };
  if (player.readyState >= 1) seek();
  else player.addEventListener("loadedmetadata", seek, { once: true });
}

function changeView(view, track = "") {
  state.view = view;
  state.track = track;
  state.scene = state.chunk = null;
  state.limit = PAGE_SIZE;
  renderResults();
}

function renderResults() {
  const { video, view, scene, chunk } = state;
  const parent = view === "chunks" ? scene : view === "evidence" ? chunk : null;
  let items = video?.[view] || [];
  if (parent) {
    const ids = new Set(view === "chunks" ? parent.chunk_ids : parent.evidence_ids);
    items = items.filter((item) => ids.has(item.id));
  }
  if (view === "evidence" && state.track) items = items.filter((item) => item.track === state.track);
  items = [...items].sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms);

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  });
  const scope = $("scope");
  scope.hidden = !parent;
  scope.replaceChildren();
  if (parent) {
    const back = element("button", "quiet", "‹ " + (view === "evidence" ? "返回检索块" : "返回场景"));
    back.onclick = () => {
      if (view === "evidence") { state.view = "chunks"; state.chunk = null; }
      else { state.view = "scenes"; state.scene = null; }
      state.track = ""; state.limit = PAGE_SIZE; renderResults();
    };
    scope.append(back, document.createTextNode((parent.title || VIEWS[view === "chunks" ? "scenes" : "chunks"]) + " · " + duration(parent.start_ms) + "–" + duration(parent.end_ms)));
  }
  const filters = $("filters");
  filters.hidden = view !== "evidence";
  filters.replaceChildren();
  if (!filters.hidden) {
    for (const [key, label] of Object.entries({ "": "全部", ...TRACKS })) {
      const button = element("button", null, label);
      button.setAttribute("aria-pressed", String(state.track === key));
      button.onclick = () => { state.track = key; state.limit = PAGE_SIZE; renderResults(); };
      filters.append(button);
    }
  }
  const list = $("records");
  list.replaceChildren();
  if (!items.length) list.append(element("p", "empty-text", video ? "当前范围内暂无" + VIEWS[view] : "上传视频后，可在此查看场景、检索块和证据"));
  for (const [i, row] of items.slice(0, state.limit).entries()) {
    const title = view === "scenes" ? (row.title || "场景 " + (i + 1)) : view === "chunks" ? "检索块 " + (i + 1) : row.text;
    const detail = view === "scenes" ? row.summary : view === "chunks" ? (row.evidence_ids || []).length + " 条关联证据" : [TRACKS[row.track], row.source].filter(Boolean).join(" · ");
    const button = element("button", "record");
    button.title = view === "evidence" ? "从证据起点播放原片" : "定位原片并展开" + (view === "scenes" ? "检索块" : "证据");
    const text = element("span", "record-text");
    text.append(element("strong", null, title));
    if (detail) text.append(element("small", null, detail));
    if (row.translation_error) text.append(element("small", null, row.translation_error));
    const time = element("span", "record-time", duration(row.start_ms, true) + " – " + duration(row.end_ms, true));
    button.append(time, text, element("span", "record-arrow", view === "evidence" ? "▶" : "›"));
    button.onclick = () => {
      seekTo(row.start_ms);
      if (view === "evidence") return;
      if (view === "scenes") { state.scene = row; state.chunk = null; state.view = "chunks"; }
      else { state.chunk = row; state.view = "evidence"; }
      state.track = ""; state.limit = PAGE_SIZE; renderResults();
    };
    list.append(button);
    if (view === "evidence" && row.original_text) {
      const original = element("details", "action-original");
      original.append(element("summary", null, "查看英文原文"), element("p", null, row.original_text));
      list.append(original);
    }
  }
  $("more").hidden = items.length <= state.limit;
}

document.querySelectorAll("[data-choose]").forEach((button) => button.onclick = () => $("file").click());
document.querySelectorAll("[data-view]").forEach((button) => button.onclick = () => changeView(button.dataset.view));
$("file").onchange = () => { if ($("file").files[0]) uploadFile($("file").files[0]); };
$("pause-upload").onclick = () => state.upload?.abort();
$("more").onclick = () => { state.limit += PAGE_SIZE; renderResults(); };
$("retry").onclick = async () => {
  if (!state.id) return;
  const id = state.id;
  $("retry").disabled = true;
  try {
    await api("/api/videos/" + encodeURIComponent(id) + "/retry", { method: "POST" });
    if (id === state.id) await openVideo(id);
    await refreshLibrary();
  } catch (error) { notice("重试失败。" + error.message); }
  finally { $("retry").disabled = false; }
};
player.ontimeupdate = () => { $("play-time").textContent = duration(player.currentTime * 1000); };
const zone = $("drop-zone");
for (const event of ["dragover", "drop"]) {
  window.addEventListener(event, (e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); });
}
zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("dragging"); };
zone.ondragleave = (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("dragging"); };
zone.ondrop = (e) => {
  e.preventDefault();
  zone.classList.remove("dragging");
  if (e.dataTransfer.files.length !== 1) return notice("每次请选择一份 MP4 视频。");
  uploadFile(e.dataTransfer.files[0]);
};

async function start() {
  renderDetail();
  try {
    await refreshLibrary();
    let saved;
    try { saved = localStorage.getItem("cinetrace.video"); } catch { /* Optional preference. */ }
    const selected = state.videos.find((video) => String(video.id) === saved) || state.videos[0];
    if (selected) await openVideo(String(selected.id));
  } catch (error) { notice("无法读取素材库。" + error.message); }
  async function poll() {
    try {
      if (!document.hidden) {
        await refreshLibrary();
        if ($("notice").textContent.startsWith("连接中断")) notice();
        const active = state.videos.find((v) => String(v.id) === state.id);
        if (state.id && (!state.video || ["queued", "processing", "uploading"].includes(state.video.status) || active?.status !== state.video.status)) {
          await openVideo(state.id);
        }
      }
    } catch (error) { notice("连接中断，正在重新连接。" + error.message); }
    setTimeout(poll, 3000);
  }
  setTimeout(poll, 3000);
}

start();
