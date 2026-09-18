/* Reroll Studio 前端。
 *
 * 状态由后端统一判定（app/runtime.py），前端只负责显示：
 *   未生成 / 待质检 / 待审 / 需重抽 / 已采纳
 * 任何写操作都返回一份最新的完整状态，前端整块刷新，避免前后端各算一遍。
 */

const STATUS = {
  empty:        { label: "未抽卡", cls: "empty",        hint: "还没有候选，点「抽卡」从资源池取" },
  pending:      { label: "待质检", cls: "pending",      hint: "有候选，但还没质检" },
  review:       { label: "待审",   cls: "review",       hint: "质检完成，等待人工采纳" },
  needs_reroll: { label: "需重抽", cls: "needs_reroll", hint: "所有候选都没通过质检" },
  accepted:     { label: "已采纳", cls: "accepted",     hint: "已经选定了某一条" },
};

// 测试集里注入的缺陷：内部用英文键，界面上给人看中文
const DEFECT_LABELS = {
  blur: "模糊",
  noise: "噪点",
  flicker: "闪烁",
  freeze: "卡帧",
  black: "黑屏",
  spec: "规格不符",
};
const SEVERITY_LABELS = { light: "轻", medium: "中", heavy: "重" };
const PRIORITY_LABELS = { high: "高", medium: "中", low: "低" };

/** 这个分镜抽卡时抽几条候选：分镜表里的「候选数」优先，没填就用默认 4 条。 */
function drawCountFor(shot) {
  const n = Number(shot?.candidates_per_shot);
  return Number.isFinite(n) && n > 0 ? n : 4;
}

/** 测试集标注的缺陷名，例如「噪点（重）」。没有注入缺陷就返回空串。 */
function defectLabel(cand) {
  if (!cand.defects?.length) return "";
  const names = cand.defects.map((d) => DEFECT_LABELS[d] || d).join("、");
  const sev = SEVERITY_LABELS[cand.severity];
  return sev ? `${names}（${sev}）` : names;
}

const state = {
  project: null,       // 含每个分镜的 status
  results: {},         // shot_id -> 质检结果（不含证据图）
  checks: [],
  config: [],          // 已生效的质检配置
  draft: null,         // 弹窗里的草稿（点保存才写回 config）
  passLine: 75,
  softTolerance: 0.5,
  session: { adopted: {}, reroll_count: {} },
  runSummary: null,
  pool: { total: 0, good: 0, bad: 0 },
  activeShotId: null,
  openCandidateId: null,
  drawShotId: null,    // 抽卡弹窗正在编辑哪个分镜
  detailCache: {},     // candidate_id -> 含证据的完整结果
  busy: false,
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let toastTimer = null;
function toast(msg, ms = 2600) {
  const box = $("#toast");
  box.textContent = msg;
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), ms);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

function checkMeta(id) {
  return state.checks.find((c) => c.id === id) || {};
}

function enabledCheckCount() {
  return state.config.filter((c) => c.enabled).length;
}

/** 一项检测项都没勾选时，质检按钮直接禁用——省得点了才发现白跑。 */
const RUN_BUTTON_TITLES = {};

function updateRunAvailability() {
  const none = enabledCheckCount() === 0;
  for (const sel of ["#btn-run", "#btn-rerun"]) {
    const btn = $(sel);
    if (!btn) continue;
    if (RUN_BUTTON_TITLES[sel] === undefined) RUN_BUTTON_TITLES[sel] = btn.title;
    btn.disabled = none || state.busy;
    btn.title = none
      ? "请先在「打开质检配置」里勾选至少一个检测项"
      : RUN_BUTTON_TITLES[sel];
  }
}

function shotById(id) {
  return state.project.shots.find((s) => s.shot_id === id);
}

// ---------------------------------------------------------------------------
// 服务端状态应用
// ---------------------------------------------------------------------------

/** 把后端返回的状态整块应用到前端。 */
function applyState(data) {
  if (data.project) state.project = data.project;
  if (data.session) state.session = data.session;
  if (data.run_summary) state.runSummary = data.run_summary;
  if (data.pool) state.pool = data.pool;

  if (data.shots) {
    // 只更新这次跑过的分镜，其它保持原样
    for (const shotResult of data.shots) {
      state.results[shotResult.shot.shot_id] = shotResult;
    }
  }
  if (data.ran_shot_ids || data.drawn) {
    // 这些分镜的候选变了（抽卡/重抽），清掉旧的证据缓存
    for (const id of [...(data.ran_shot_ids || []), ...(data.drawn || [])]) {
      for (const key of Object.keys(state.detailCache)) {
        if (key.startsWith(id + "::")) delete state.detailCache[key];
      }
    }
  }
  renderAll();
}

function renderAll() {
  renderShotList();
  renderWorkbench();
  renderStats();
  updateRunAvailability();
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

async function boot() {
  const data = await api("/api/bootstrap");
  state.project = data.project;
  state.checks = data.checks;
  state.session = data.session;
  state.passLine = data.pass_line ?? 75;
  state.softTolerance = data.soft_tolerance ?? 0.5;
  state.runSummary = data.run_summary;
  state.pool = data.pool || state.pool;

  for (const shotResult of data.results || []) {
    state.results[shotResult.shot.shot_id] = shotResult;
  }

  state.config = (data.config && data.config.length)
    ? mergeConfig(data.config)
    : state.checks.map((c, i) => ({
        check_id: c.id,
        enabled: c.default_enabled !== false,
        order: i,
        threshold: c.default_threshold,
        weight: c.default_weight,
        params: {},
      }));

  state.activeShotId = state.project.shots[0]?.shot_id ?? null;

  renderAll();

  if (state.project.warnings?.length) toast(state.project.warnings[0], 6000);
}

function mergeConfig(saved) {
  const byId = new Map(saved.map((c) => [c.check_id, c]));
  return state.checks.map((c, i) => {
    const prev = byId.get(c.id);
    return {
      check_id: c.id,
      enabled: prev ? prev.enabled : c.default_enabled !== false,
      order: prev && Number.isFinite(prev.order) ? prev.order : i,
      threshold: prev?.threshold ?? c.default_threshold,
      weight: prev?.weight ?? c.default_weight,
      params: prev?.params ?? {},
    };
  }).sort((a, b) => a.order - b.order).map((c, i) => ({ ...c, order: i }));
}

// ---------------------------------------------------------------------------
// 分镜列表
// ---------------------------------------------------------------------------

function renderShotList() {
  const list = $("#shot-list");
  list.innerHTML = "";

  const shots = state.project.shots;
  $("#shot-count").textContent = `${shots.length} 个`;

  // 状态图例
  const counts = { accepted: 0, review: 0, needs_reroll: 0, pending: 0, empty: 0 };
  shots.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });
  $("#status-legend").innerHTML = Object.entries(STATUS)
    .filter(([key]) => counts[key])
    .map(([key, meta]) =>
      `<span class="item ${key}"><span class="dot"></span>${meta.label} ${counts[key]}</span>`)
    .join("");

  for (const shot of shots) {
    const meta = STATUS[shot.status] || STATUS.pending;
    const li = el("li", `shot-item ${shot.status}`);
    if (shot.shot_id === state.activeShotId) li.classList.add("active");

    li.innerHTML = `
      <div class="row1">
        <span class="sid">${esc(shot.shot_id)}</span>
        <span class="badge ${meta.cls}" title="${esc(meta.hint)}">${meta.label}</span>
      </div>
      <div class="prompt" title="${esc(shot.prompt)}">${esc(shot.prompt)}</div>`;
    li.addEventListener("click", () => {
      state.activeShotId = shot.shot_id;
      state.openCandidateId = null;
      renderShotList();
      renderWorkbench();
    });
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// 工作区
// ---------------------------------------------------------------------------

function renderWorkbench() {
  const shot = shotById(state.activeShotId);
  const header = $("#shot-header");
  const grid = $("#candidate-grid");
  const detail = $("#detail-panel");

  grid.innerHTML = "";
  detail.classList.add("hidden");
  detail.innerHTML = "";

  if (!shot) {
    header.innerHTML = `<div class="placeholder"><h3>还没有分镜</h3>
      <p>点右上角「导入分镜表」上传一份分镜表（可以先「下载分镜模板」）。</p></div>`;
    return;
  }

  const result = (shot.status === "review"
    || shot.status === "needs_reroll"
    || shot.status === "accepted")
    ? state.results[shot.shot_id]
    : null;   // 待质检 / 未生成：主界面也要还原成"还没质检"，不留旧分数
  const adoptedId = state.session.adopted[shot.shot_id];
  const candidates = result ? result.candidates : shot.candidates;
  const meta = STATUS[shot.status] || STATUS.pending;
  const hasCandidates = candidates.length > 0;
  const noChecks = enabledCheckCount() === 0;

  header.innerHTML = `
    <div class="muted" style="font-size:12.5px">${esc(shot.shot_id)} · ${esc(shot.scene || "")}</div>
    <div class="prompt">${esc(shot.prompt)}</div>
    <div class="meta">
      <span>候选 ${candidates.length} 条</span>
      ${shot.priority ? `<span>优先级 ${esc(PRIORITY_LABELS[shot.priority] || shot.priority)}</span>` : ""}
      ${shot.expected_faces !== null && shot.expected_faces !== undefined
        ? `<span>期望人脸 ${shot.expected_faces}</span>` : ""}
      ${shot.expected_duration_s ? `<span>期望时长 ${shot.expected_duration_s}s</span>` : ""}
      <span>状态：<b class="status-${meta.cls}">${meta.label}</b></span>
    </div>
    <div class="shot-actions">
      <button class="btn tiny ${hasCandidates ? "" : "primary"}" id="btn-draw-shot"
              title="从视频资源池随机抽 ${drawCountFor(shot)} 个片段，相当于调用云端生成 API">
        ${hasCandidates ? "重新抽卡" : "抽卡"}
      </button>
      <button class="btn tiny ${shot.status === "pending" && hasCandidates ? "primary" : ""}" id="btn-run-shot"
              ${(!hasCandidates || noChecks) ? "disabled" : ""}
              title="${!hasCandidates ? "先抽卡才有候选可质检" : noChecks ? "请先在「打开质检配置」里勾选至少一个检测项" : ""}">
        ${result ? "重新质检这个分镜" : "质检这个分镜"}
      </button>
      ${adoptedId ? '<button class="btn tiny" id="btn-clear-adopt">取消采纳</button>' : ""}
    </div>`;

  header.querySelector("#btn-draw-shot").addEventListener("click", () => openDrawModal(shot.shot_id));
  header.querySelector("#btn-run-shot").addEventListener("click", () => runQC([shot.shot_id], true));
  const clearBtn = header.querySelector("#btn-clear-adopt");
  if (clearBtn) clearBtn.addEventListener("click", () => clearAdopt(shot.shot_id));

  if (!hasCandidates) {
    const box = el("div", "placeholder");
    box.innerHTML = `<h3>这个分镜还没抽卡</h3>
      <p>点「抽卡」从视频资源池里随机取 ${drawCountFor(shot)} 个片段——相当于调用云端生成 API。<br>
         也可以点右上角「全部抽卡」一次性给所有分镜抽。</p>`;
    grid.appendChild(box);
    return;
  }

  if (result?.diagnosis) grid.appendChild(renderDiagnosis(result));

  const ordered = [...candidates].sort((a, b) => {
    if (!result) return 0;
    if (a.passed !== b.passed) return a.passed ? -1 : 1;
    return (b.total_score ?? 0) - (a.total_score ?? 0);
  });

  for (const cand of ordered) grid.appendChild(renderCandidateCard(shot, cand, result));

  if (state.openCandidateId && result) {
    const cand = result.candidates.find((c) => c.candidate_id === state.openCandidateId);
    if (cand) renderDetail(cand);
  }
}

function renderCandidateCard(shot, cand, result) {
  const scored = Boolean(result && cand.checks);
  const passed = scored && cand.passed;
  const card = el("div", "cand-card " + (scored ? (passed ? "pass" : "fail") : "unscored"));

  const adopted = state.session.adopted[shot.shot_id] === cand.candidate_id;
  if (adopted) card.classList.add("adopted");

  const medal = cand.rank === 1 ? " 🥇" : cand.rank ? ` #${cand.rank}` : "";

  // 卡片上只说质检结论。测试集里"注入了什么缺陷"是给校验算法用的元数据，
  // 混在这里会让用户以为是质检项报出来的结果，所以挪到详情里单独说明。
  let note;
  if (cand.error) note = cand.error;
  else if (!scored) note = "还没质检";
  else note = passed ? "质检通过" : "未通过";

  const scoreHtml = scored
    ? `<span class="score ${passed ? "pass" : "fail"}">${cand.total_score?.toFixed(1)}${medal}</span>`
    : `<span class="score none">未质检</span>`;

  card.innerHTML = `
    <video src="/media/${encodeURI(cand.file)}" controls preload="metadata" muted></video>
    <div class="cand-body">
      <div class="cand-top">
        <span class="cand-id">${esc(cand.candidate_id)}</span>
        ${scoreHtml}
      </div>
      <div class="cand-note">${esc(note)}</div>
      <div class="cand-actions">
        <button class="btn adopt ${adopted ? "adopted" : ""}">${adopted ? "✓ 已采纳" : "采纳"}</button>
        ${scored ? '<button class="btn detail-btn">详情</button>' : ""}
      </div>
    </div>`;

  card.querySelector(".adopt").addEventListener("click", (e) => {
    e.stopPropagation();
    adopt(shot.shot_id, cand.candidate_id);
  });
  const detailBtn = card.querySelector(".detail-btn");
  if (detailBtn) {
    detailBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      state.openCandidateId = state.openCandidateId === cand.candidate_id ? null : cand.candidate_id;
      renderWorkbench();
    });
  }

  return card;
}

// ---------------------------------------------------------------------------
// 候选详情（按需拉取证据图）
// ---------------------------------------------------------------------------

async function renderDetail(cand) {
  const panel = $("#detail-panel");
  panel.classList.remove("hidden");

  let full = state.detailCache[`${cand.shot_id}::${cand.candidate_id}`];
  if (!full) {
    panel.innerHTML = `<div class="muted" style="padding:8px 0">正在加载证据图…</div>`;
    try {
      full = await api(`/api/candidate/${cand.shot_id}/${cand.candidate_id}`);
      state.detailCache[`${cand.shot_id}::${cand.candidate_id}`] = full;
    } catch (err) {
      panel.innerHTML = `<div class="muted">加载失败：${esc(err.message)}</div>`;
      return;
    }
  }
  // 等待期间用户可能已经切走
  if (state.openCandidateId !== cand.candidate_id) return;

  const rows = full.checks.map((c) => {
    const meta = checkMeta(c.check_id);
    const flag = c.passed ? '<span class="flag ok">✓</span>' : '<span class="flag no">✗</span>';
    const hard = meta.hard ? '<span class="hard-mark">硬</span> ' : "";
    const th = c.detail?.threshold;
    return `<div class="check-row ${c.passed ? "" : "fail"}">
      ${flag}
      <span>${hard}${esc(meta.name || c.check_id)}</span>
      <span class="num">${c.score.toFixed(1)}${th !== undefined ? ` / ${th}` : ""}</span>
      <span class="note">${esc(c.notes || "")}</span>
    </div>`;
  }).join("");

  const evidence = full.checks.flatMap((c) => (c.evidence || []));
  const evidenceHtml = evidence.map((e) => {
    if (e.data?.image_b64) {
      return `<figure><img src="data:image/jpeg;base64,${e.data.image_b64}" alt="${esc(e.label)}">
        <figcaption>${esc(e.label)}${e.frame_index !== null && e.frame_index !== undefined ? ` · 第 ${e.frame_index} 帧` : ""}</figcaption></figure>`;
    }
    if (e.data?.series) {
      return `<figure class="chart-box">${sparkline(e.data.series)}
        <figcaption>${esc(e.label)}</figcaption></figure>`;
    }
    return `<figure><figcaption>${esc(e.label)}${e.frame_index !== null && e.frame_index !== undefined ? ` · 第 ${e.frame_index} 帧` : ""}</figcaption></figure>`;
  }).join("");

  const injected = defectLabel(full);
  const annotationHtml = injected
    ? `<div class="annotation">
         📌 <b>测试集标注</b>：这条视频在制作时被刻意注入了「${esc(injected)}」。
         这个标签只用来校验算法判得准不准，<b>不是质检项的输出</b>。
       </div>`
    : "";

  panel.innerHTML = `
    <h3>${esc(full.candidate_id)} 的逐项结果
      <span class="muted" style="font-weight:400;font-size:12px">
        ${full.hard_failed?.length ? `硬性未过：${full.hard_failed.map((x) => checkMeta(x).name || x).join("、")} · ` : ""}
        ${full.soft_failed?.length ? `软性未过：${full.soft_failed.map((x) => checkMeta(x).name || x).join("、")} · ` : ""}
        扣分系数 ${(full.penalty_ratio * 100).toFixed(0)}%
      </span>
    </h3>
    ${annotationHtml}
    <div class="check-rows">${rows}</div>
    ${evidenceHtml ? `<div class="evidence">${evidenceHtml}</div>` : ""}`;
}

function sparkline(series) {
  if (!series?.length) return "";
  const w = 300, h = 68, pad = 4;
  const min = Math.min(...series), max = Math.max(...series);
  const span = max - min || 1;
  const pts = series.map((v, i) => {
    const x = pad + (i / Math.max(1, series.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((v - min) / span) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="#2563eb" stroke-width="1.5"/></svg>`;
}

// ---------------------------------------------------------------------------
// 诊断卡 + 模拟重抽
// ---------------------------------------------------------------------------

function renderDiagnosis(result) {
  const d = result.diagnosis;
  const box = el("div", "diagnosis");
  const kindLabel = d.kind === "systematic" ? "系统性缺陷" : "疑似运气问题";

  const bars = Object.entries(d.failure_distribution || {})
    .sort((a, b) => b[1] - a[1])
    .map(([cid, ratio]) => {
      const meta = checkMeta(cid);
      return `<div class="diag-bar">
        <span class="label">${esc(meta.name || cid)}</span>
        <span class="track"><span class="fill" style="width:${(ratio * 100).toFixed(0)}%"></span></span>
        <span>${(ratio * 100).toFixed(0)}%</span>
      </div>`;
    }).join("");

  const suggestions = [];
  if (d.negative_additions?.length) {
    suggestions.push(`建议往负面词里加：<code>${esc(d.negative_additions.join("、"))}</code>`);
  }
  if (d.positive_additions?.length) {
    suggestions.push(`建议往提示词里加：<code>${esc(d.positive_additions.join("、"))}</code>`);
  }
  if (d.param_hint) suggestions.push(esc(d.param_hint));

  box.innerHTML = `
    <h3>⚠️ 这个分镜的候选全部不通过 · 诊断：${kindLabel}</h3>
    <div class="msg">${esc(d.message)}</div>
    <div class="diag-chart">${bars}</div>
    ${suggestions.length ? `<div class="sugg">${suggestions.join("<br>")}</div>` : ""}
    <div class="prompt-edit">
      <input type="text" id="reroll-prompt" value="${esc(result.shot.prompt)}"
             placeholder="可以在这里直接改提示词">
      <input type="text" id="reroll-negative" value="${esc(d.suggested_negative || result.shot.negative_prompt || "")}"
             placeholder="负面词（已按建议填好，可编辑）">
      <button class="btn primary" id="btn-reroll">按修改后的提示词重抽</button>
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:7px">
      说明：demo 阶段的「重抽」是模拟的（没有接真实生成 API），会从备用素材里换一批候选进来，走的是和真实 provider 相同的接口。
    </div>`;

  box.querySelector("#btn-reroll").addEventListener("click", () => {
    reroll(
      result.shot.shot_id,
      box.querySelector("#reroll-prompt").value,
      box.querySelector("#reroll-negative").value,
    );
  });

  return box;
}

// ---------------------------------------------------------------------------
// 质检配置（弹窗：改动先落在草稿，点保存才统一生效）
// ---------------------------------------------------------------------------

function openConfigModal() {
  state.draft = {
    config: state.config.map((c) => ({ ...c, params: { ...c.params } })),
    passLine: state.passLine,
    softTolerance: state.softTolerance,
  };
  $("#config-modal").classList.remove("hidden");
  renderConfig();
}

function closeConfigModal() {
  state.draft = null;
  $("#config-modal").classList.add("hidden");
}

function draftDirty() {
  if (!state.draft) return false;
  const saved = JSON.stringify({
    config: state.config,
    passLine: state.passLine,
    softTolerance: state.softTolerance,
  });
  return JSON.stringify(state.draft) !== saved;
}

function updateDirtyHint() {
  const node = $("#config-dirty");
  if (!node) return;
  const n = state.draft ? state.draft.config.filter((c) => c.enabled).length : 0;
  const parts = [`已勾选 ${n} 项`];
  if (draftDirty()) parts.push("有未保存的修改");
  node.textContent = parts.join(" · ");
  node.classList.toggle("warn", n === 0);
}

/** 保存：生效 + 通知服务端 + 整块刷新（过期结果会从主界面消失）。 */
async function saveConfigModal() {
  if (!state.draft) return;
  state.config = state.draft.config;
  state.passLine = state.draft.passLine;
  state.softTolerance = state.draft.softTolerance;
  closeConfigModal();

  try {
    const data = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        configs: state.config,
        pass_line: state.passLine,
        soft_tolerance: state.softTolerance,
      }),
    });
    if (data.project) state.project = data.project;
    if (data.run_summary) state.runSummary = data.run_summary;
    renderAll();
    toast("质检配置已保存，相关分镜已回到「待质检」");
  } catch (err) {
    toast(`配置保存失败：${err.message}`, 5000);
  }
}

function renderConfig() {
  const draft = state.draft;
  if (!draft) return;

  $("#pass-line").value = draft.passLine;
  $("#pass-line-val").textContent = draft.passLine;
  $("#soft-tol").value = draft.softTolerance;
  $("#soft-tol-val").textContent = draft.softTolerance.toFixed(1);

  const list = $("#check-list");
  list.innerHTML = "";

  const ordered = [...draft.config].sort((a, b) => a.order - b.order);

  ordered.forEach((cfg, index) => {
    const meta = checkMeta(cfg.check_id);
    if (!meta) return;

    const li = el("li", "check-item" + (cfg.enabled ? "" : " disabled"));
    li.draggable = true;
    li.dataset.index = index;

    const tags = [];
    tags.push(meta.hard
      ? '<span class="tag">硬性</span>'
      : '<span class="tag soft">软性</span>');
    if (meta.requires_models) tags.push('<span class="tag model">慢 · 未校准</span>');

    li.innerHTML = `
      <div class="check-head">
        <span class="grip">⋮⋮</span>
        <input type="checkbox" ${cfg.enabled ? "checked" : ""}>
        <span class="name">${esc(meta.name)}</span>
        ${tags.join("")}
      </div>
      <div class="check-desc">${esc(meta.description)}</div>
      <div class="slider-row">
        <label>阈值</label>
        <input type="range" class="thr" min="0" max="100" step="1" value="${cfg.threshold}">
        <span class="val">${cfg.threshold}</span>
      </div>
      <div class="slider-row">
        <label>权重</label>
        <input type="range" class="wgt" min="0" max="2" step="0.1" value="${cfg.weight}">
        <span class="val">${cfg.weight}</span>
      </div>`;

    li.querySelector("input[type=checkbox]").addEventListener("change", (e) => {
      cfg.enabled = e.target.checked;
      li.classList.toggle("disabled", !cfg.enabled);
      updateDirtyHint();
    });
    li.querySelector(".thr").addEventListener("input", (e) => {
      cfg.threshold = Number(e.target.value);
      e.target.nextElementSibling.textContent = cfg.threshold;
      updateDirtyHint();
    });
    li.querySelector(".wgt").addEventListener("input", (e) => {
      cfg.weight = Number(e.target.value);
      e.target.nextElementSibling.textContent = cfg.weight.toFixed(1);
      updateDirtyHint();
    });

    li.addEventListener("dragstart", (e) => {
      li.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(index));
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("drop-target"); });
    li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
    li.addEventListener("drop", (e) => {
      e.preventDefault();
      li.classList.remove("drop-target");
      reorder(Number(e.dataTransfer.getData("text/plain")), index);
    });

    list.appendChild(li);
  });

  updateDirtyHint();
}

function reorder(from, to) {
  if (!state.draft || from === to) return;
  const ordered = [...state.draft.config].sort((a, b) => a.order - b.order);
  const [moved] = ordered.splice(from, 1);
  ordered.splice(to, 0, moved);
  ordered.forEach((c, i) => { c.order = i; });
  state.draft.config = ordered;
  renderConfig();
  updateDirtyHint();
}

// ---------------------------------------------------------------------------
// 抽卡弹窗：先确认 / 修改分镜信息，再抽
// ---------------------------------------------------------------------------

function openDrawModal(shotId) {
  const shot = shotById(shotId);
  if (!shot) return;

  state.drawShotId = shotId;
  $("#dm-shot-id").textContent = shot.shot_id;
  $("#dm-scene").textContent = shot.scene ? `· ${shot.scene}` : "";
  $("#dm-prompt").value = shot.prompt || "";
  $("#dm-negative").value = shot.negative_prompt || "";
  $("#dm-count").value = drawCountFor(shot);
  $("#dm-duration").value = shot.expected_duration_s ?? 5;
  $("#dm-aspect").value = shot.aspect_ratio || "16:9";
  $("#dm-faces").value = shot.expected_faces ?? "";
  $("#dm-hands").value = shot.expected_hands ?? "";

  const n = shot.candidates.length;
  $("#dm-hint").textContent = n
    ? `当前有 ${n} 条候选，确认后会被换掉（旧质检结果作废）`
    : "";

  $("#draw-modal").classList.remove("hidden");
  $("#dm-prompt").focus();
}

function closeDrawModal() {
  state.drawShotId = null;
  $("#draw-modal").classList.add("hidden");
}

async function confirmDraw() {
  const shotId = state.drawShotId;
  if (!shotId) return;

  const prompt = $("#dm-prompt").value.trim();
  if (!prompt) {
    toast("提示词不能为空");
    $("#dm-prompt").focus();
    return;
  }

  const faces = $("#dm-faces").value;
  const hands = $("#dm-hands").value;
  const payload = {
    shot_id: shotId,
    prompt,
    negative_prompt: $("#dm-negative").value,
    candidates_per_shot: Number($("#dm-count").value) || 4,
    expected_duration_s: Number($("#dm-duration").value) || 5,
    aspect_ratio: $("#dm-aspect").value.trim() || "16:9",
    expected_faces: faces === "" ? null : Number(faces),
    expected_hands: hands === "" ? null : Number(hands),
  };

  closeDrawModal();
  try {
    await api("/api/shot", { method: "POST", body: JSON.stringify(payload) });
  } catch (err) {
    toast(`保存分镜信息失败：${err.message}`, 5000);
    return;
  }
  await drawCards([shotId]);
}

// ---------------------------------------------------------------------------
// 状态栏
// ---------------------------------------------------------------------------

function renderStats() {
  const box = $("#stats");
  const counts = { accepted: 0, review: 0, needs_reroll: 0, pending: 0, empty: 0 };
  state.project.shots.forEach((s) => { counts[s.status] = (counts[s.status] || 0) + 1; });

  const s = state.runSummary;
  const stat = (label, value, cls = "", title = "") =>
    `<span class="stat ${cls}" title="${esc(title)}"><b>${value}</b><span class="muted">${label}</span></span>`;

  const parts = [
    stat("已采纳", counts.accepted, counts.accepted ? "pass" : ""),
    stat("待审", counts.review),
    stat("待质检", counts.pending),
    stat("未抽卡", counts.empty),
    stat("需重抽", counts.needs_reroll, counts.needs_reroll ? "fail" : ""),
  ];

  if (state.pool?.total) {
    parts.push('<span class="sep"></span>');
    parts.push(
      `<span class="stat" title="视频资源池：抽卡时从这里随机取片段。文件名里标了它是好片还是什么缺陷。">
         <b>${state.pool.total}</b><span class="muted">资源池片段</span></span>`,
      `<span class="stat" title="资源池里标注为好片的数量">
         <b>${state.pool.good}</b><span class="muted">其中好片</span></span>`,
    );
  }

  if (s && s.candidates) {
    parts.push('<span class="sep"></span>');
    parts.push(stat("已质检候选", s.candidates));
    parts.push(stat("通过", s.passed, "pass"));
    parts.push(stat("不通过", s.failed, "fail"));
    parts.push('<span class="sep"></span>');
    parts.push(
      `<span class="stat" title="对照内置测试集自带的缺陷标签：好片被判废的比例。这是算法成绩，不是人工复核结果。">
         <b>${s.false_reject_rate ?? "-"}%</b><span class="muted">误杀率(对标注)</span></span>`,
      `<span class="stat" title="对照内置测试集自带的缺陷标签：废片被判通过的比例。">
         <b>${s.false_accept_rate ?? "-"}%</b><span class="muted">漏检率(对标注)</span></span>`,
    );
  }

  box.innerHTML = parts.join("");
}

// ---------------------------------------------------------------------------
// 进度条
// ---------------------------------------------------------------------------

let progressTimer = null;

function renderProgress(p) {
  const pct = Math.max(0, Math.min(100, p.percent ?? 0));
  $("#progress-fill").style.width = `${pct}%`;
  $("#progress-text").textContent = p.running
    ? `质检中 ${p.done}/${p.total}（${pct}%）· 已用时 ${p.elapsed}s · 当前 ${p.candidate_id || "…"}`
    : `完成 ${p.done}/${p.total} · 用时 ${p.elapsed}s`;
}

function startProgressPolling() {
  $("#progress-strip").classList.remove("hidden");
  renderProgress({ running: true, done: 0, total: 0, percent: 0, elapsed: 0 });
  progressTimer = setInterval(async () => {
    try { renderProgress(await api("/api/progress")); } catch (e) { /* 忽略 */ }
  }, 500);
}

function hideProgress() {
  $("#progress-strip").classList.add("hidden");
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
}

// ---------------------------------------------------------------------------
// 动作
// ---------------------------------------------------------------------------

async function runQC(shotIds, force) {
  if (state.busy) return;
  state.busy = true;

  const btn = $("#btn-run");
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin dark"></span> 质检中…';

  const scope = shotIds?.length ? `${shotIds.length} 个分镜` : "所有待质检的分镜";
  toast(`开始质检：${scope}`, 3000);
  startProgressPolling();

  try {
    const data = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        configs: state.config,
        pass_line: state.passLine,
        soft_tolerance: state.softTolerance,
        shot_ids: shotIds || null,
        force: Boolean(force),
      }),
    });

    if (data.skipped) {
      toast(data.message, 3500);
    } else {
      applyState(data);
      const s = data.run_summary;
      toast(`质检完成 ${data.ran} 个分镜：通过 ${s.passed} 条，不通过 ${s.failed} 条`);
      return;
    }
    applyState(data);
  } catch (err) {
    toast(`质检失败：${err.message}`, 5000);
  } finally {
    state.busy = false;
    btn.textContent = original;
    updateRunAvailability();
    hideProgress();
  }
}

async function drawCards(shotIds) {
  if (state.busy) return;
  state.busy = true;
  toast(shotIds?.length ? "正在抽卡…" : "正在给还没抽过卡的分镜抽卡…");
  try {
    const data = await api("/api/draw", {
      // count 不传：让后端按每个分镜自己的「候选数」决定抽几条
      method: "POST",
      body: JSON.stringify({ shot_ids: shotIds || null, count: null }),
    });
    // 抽卡后候选变了，旧质检结果作废
    for (const id of data.drawn || []) delete state.results[id];
    applyState(data);
    const n = (data.drawn || []).length;
    const counts = Object.values(data.counts || {});
    const uniform = counts.length > 0 && counts.every((c) => c === counts[0]);
    toast(n
      ? `已为 ${n} 个分镜抽卡${uniform ? `（每个 ${counts[0]} 条候选）` : ""}`
      : "所有分镜都已经抽过卡了");
  } catch (err) {
    toast(`抽卡失败：${err.message}`, 6000);
  } finally {
    state.busy = false;
  }
}

async function adopt(shotId, candidateId) {  try {
    applyState(await api("/api/adopt", {
      method: "POST",
      body: JSON.stringify({ shot_id: shotId, candidate_id: candidateId }),
    }));
    toast(`已采纳 ${candidateId}`);
  } catch (err) {
    toast(`采纳失败：${err.message}`);
  }
}

async function clearAdopt(shotId) {
  try {
    applyState(await api("/api/clear-adopt", {
      method: "POST",
      body: JSON.stringify({ shot_id: shotId }),
    }));
    toast(`${shotId} 已取消采纳`);
  } catch (err) {
    toast(`操作失败：${err.message}`);
  }
}

async function adoptTop() {
  try {
    const data = await api("/api/adopt-top", { method: "POST", body: JSON.stringify({}) });
    applyState(data);
    const n = Object.keys(data.adopted || {}).length;
    const skipped = data.skipped_shots?.length || 0;
    if (n === 0) {
      toast(skipped ? `没有可采纳的候选：${skipped} 个分镜还没质检或全部未通过` : "没有可采纳的候选");
    } else {
      toast(skipped
        ? `已采纳 ${n} 个分镜的第一名；跳过 ${skipped} 个（未质检或全部未通过）`
        : `已采纳 ${n} 个分镜的第一名`);
    }
  } catch (err) {
    toast(`操作失败：${err.message}`);
  }
}

async function reroll(shotId, prompt, negativePrompt) {
  if (state.busy) return;
  state.busy = true;
  toast("正在模拟重抽…");
  startProgressPolling();
  try {
    const data = await api("/api/reroll", {
      method: "POST",
      body: JSON.stringify({
        shot_id: shotId,
        prompt,
        negative_prompt: negativePrompt,
        configs: state.config,
        pass_line: state.passLine,
        soft_tolerance: state.softTolerance,
      }),
    });
    // 候选变了，清掉该分镜的旧结果与证据缓存
    delete state.results[shotId];
    for (const key of Object.keys(state.detailCache)) {
      if (key.startsWith(shotId)) delete state.detailCache[key];
    }
    if (data.shot) state.results[shotId] = data.shot;
    applyState(data);
    state.openCandidateId = null;
    renderWorkbench();

    const passed = data.shot.candidates.filter((c) => c.passed).length;
    toast(`已重抽第 ${data.reroll_round} 轮：${passed}/${data.shot.candidates.length} 条通过`);
  } catch (err) {
    toast(`重抽失败：${err.message}`, 5000);
  } finally {
    state.busy = false;
    hideProgress();
  }
}

/** 按钮上转圈：导出 / 拼接这类要等几秒的操作，得让人看出在跑。 */
async function withBusyButton(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin dark"></span> ${label}`;
  try {
    return await fn();
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
}

async function exportSelected() {
  const ok = confirm(
    "把已采纳的片段导出到 data/selected/？\n\n"
    + "· 按分镜顺序改名 01_S001.mp4…\n"
    + "· 统一分辨率 / 帧率 / 编码（剪辑软件要求一致）\n"
    + "· 生成 selected.csv 清单（分镜号 / 提示词 / 采纳的候选 / 得分）",
  );
  if (!ok) return;

  await withBusyButton($("#btn-export"), "导出中…", async () => {
    try {
      const d = await api("/api/export", { method: "POST" });
      const miss = d.missing?.length ? `；还有 ${d.missing.length} 个分镜没采纳` : "";
      toast(
        `已导出 ${d.count} 个片段到 data/selected/（${d.spec}，共 ${d.total_duration_s}s）${miss}`,
        8000,
      );
    } catch (err) {
      toast(`导出失败：${err.message}`, 6000);
    }
  });
}

async function buildPreview() {
  const ok = confirm(
    "把所有采纳的片段拼成一条预览片？\n\n"
    + "会先导出（统一规格），再用 ffmpeg 顺序拼接成 data/selected/preview.mp4。\n"
    + "拼接要转码，会花几秒到几十秒。",
  );
  if (!ok) return;

  await withBusyButton($("#btn-preview"), "拼接中…", async () => {
    try {
      const d = await api("/api/preview", { method: "POST" });
      const miss = d.missing?.length ? `；还有 ${d.missing.length} 个分镜没采纳` : "";
      toast(
        `预览片已生成：data/selected/${d.preview_name}（${d.count} 个镜头 / `
        + `${d.preview_duration_s}s / ${d.preview_mb} MB）${miss}`,
        9000,
      );
    } catch (err) {
      toast(`合成失败：${err.message}`, 6000);
    }
  });
}

async function importCSV(file) {
  const form = new FormData();
  form.append("file", file);
  toast("正在导入分镜表…", 6000);
  try {
    const data = await api("/api/import", { method: "POST", body: form });
    await reloadFromServer(
      `导入成功：${data.project.shots.length} 个分镜。接下来点「全部抽卡」生成候选。`,
    );
  } catch (err) {
    toast(`导入失败：${err.message}`, 6000);
  }
}

/** 项目整体换掉之后（导入 / 初始化），前端整块重建。 */
async function reloadFromServer(message) {
  state.results = {};
  state.detailCache = {};
  state.openCandidateId = null;
  state.activeShotId = null;
  await boot();
  if (message) toast(message, 5000);
}

// ---------------------------------------------------------------------------
// 事件绑定
// ---------------------------------------------------------------------------

$("#btn-run").addEventListener("click", () => runQC(null, false));
$("#btn-rerun").addEventListener("click", () => {
  const n = state.project?.shots?.length || 0;
  const ok = confirm(
    `这会忽略已有结果，把所有 ${n} 个分镜重新质检一遍。\n\n`
    + "分镜多的时候比较慢，继续？",
  );
  if (ok) runQC(null, true);
});
$("#btn-draw-all").addEventListener("click", () => {
  const waiting = (state.project?.shots || []).filter((s) => !s.candidates.length);
  if (!waiting.length) {
    toast("所有分镜都已经抽过卡了");
    return;
  }
  const ok = confirm(
    `给还没抽过卡的 ${waiting.length} 个分镜抽卡？\n\n`
    + "每个分镜抽几条看分镜表里的「候选数」（默认 4 条）。\n"
    + "已经抽过卡的分镜不受影响。",
  );
  if (ok) drawCards(null);
});
$("#btn-adopt-all").addEventListener("click", adoptTop);
$("#btn-reset").addEventListener("click", async () => {
  const ok = confirm(
    "确定清空所有分镜的采纳记录吗？\n\n"
    + "候选和质检结果都会保留，只是「已采纳」全部取消。",
  );
  if (!ok) return;
  try {
    applyState(await api("/api/reset", { method: "POST" }));
    toast("已清空采纳记录");
  } catch (err) {
    toast(`操作失败：${err.message}`);
  }
});
$("#btn-init").addEventListener("click", async () => {
  const ok = confirm(
    "确定要初始化吗？\n\n"
    + "· 清空当前的分镜、候选、质检结果和采纳记录\n"
    + "· 质检配置恢复默认\n"
    + "· 视频资源池不受影响\n\n"
    + "初始化后工作台是空的，需要重新导入分镜表"
    + "（可以先用「下载分镜模板」拿到内置示例）。",
  );
  if (!ok) return;
  try {
    await api("/api/init", { method: "POST" });
    await reloadFromServer("已初始化，工作台已清空");
  } catch (err) {
    toast(`初始化失败：${err.message}`);
  }
});
$("#btn-export").addEventListener("click", exportSelected);
$("#btn-preview").addEventListener("click", buildPreview);
$("#csv-input").addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (file) importCSV(file);
  e.target.value = "";
});

// 抽卡弹窗
$("#btn-close-draw").addEventListener("click", closeDrawModal);
$("#btn-cancel-draw").addEventListener("click", closeDrawModal);
$("#btn-confirm-draw").addEventListener("click", confirmDraw);
$("#draw-modal").addEventListener("click", (e) => {
  if (e.target.id === "draw-modal") closeDrawModal();
});

$("#pass-line").addEventListener("input", (e) => {
  if (!state.draft) return;
  state.draft.passLine = Number(e.target.value);
  $("#pass-line-val").textContent = state.draft.passLine;
  updateDirtyHint();
});
$("#soft-tol").addEventListener("input", (e) => {
  if (!state.draft) return;
  state.draft.softTolerance = Number(e.target.value);
  $("#soft-tol-val").textContent = state.draft.softTolerance.toFixed(1);
  updateDirtyHint();
});

$("#btn-open-config").addEventListener("click", openConfigModal);
$("#btn-close-config").addEventListener("click", closeConfigModal);
$("#btn-cancel-config").addEventListener("click", closeConfigModal);
$("#btn-save-config").addEventListener("click", saveConfigModal);
$("#config-modal").addEventListener("click", (e) => {
  if (e.target.id === "config-modal") closeConfigModal();   // 点遮罩也关闭
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (state.draft) closeConfigModal();
  if (state.drawShotId) closeDrawModal();
});

function setDraftEnabled(fn) {
  if (!state.draft) return;
  state.draft.config.forEach(fn);
  renderConfig();
  updateDirtyHint();
}
$("#btn-select-all").addEventListener("click", () =>
  setDraftEnabled((c) => { c.enabled = true; }));
$("#btn-select-fast").addEventListener("click", () =>
  setDraftEnabled((c) => { c.enabled = !checkMeta(c.check_id).requires_models; }));
$("#btn-select-none").addEventListener("click", () =>
  setDraftEnabled((c) => { c.enabled = false; }));

boot().catch((err) => {
  toast(`启动失败：${err.message}`, 8000);
  console.error(err);
});
