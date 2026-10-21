"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    userId: getDemoId(), conversationId: null, latest: null, draft: null, task: null,
    busy: false, actionBusy: false, version: 0, queryController: null,
    timer: null, pollTimer: null, traceRequest: 0, taskRequest: 0, taskReused: false,
  };
  const TERMINAL_TASKS = new Set(["SUCCEEDED", "MANUAL_REVIEW", "CANCELLED", "FAILED"]);
  const stateLabels = {
    SUCCEEDED: "已完成", FAILED: "失败", RUNNING: "处理中", PENDING: "待处理",
    RETRY: "等待重试", MANUAL_REVIEW: "待人工处理", CANCELLED: "已终止",
  };
  const agentLabels = { policy_check: "政策核查", preparation_plan: "准备规划", safety_review: "安全审查" };
  const eventLabels = { TASK_CLAIMED: "领取任务", ARTIFACT_CREATED: "保存成果", TASK_COMPLETED: "完成任务", TASK_FAILED: "任务失败" };

  function getDemoId() {
    const key = "campus-copilot-demo-user";
    try {
      const saved = localStorage.getItem(key);
      if (saved && /^demo-[a-zA-Z0-9-]{6,55}$/.test(saved)) return saved;
    } catch (_) { /* Storage can be unavailable in a private browser context. */ }
    const random = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const id = `demo-${random}`.slice(0, 64);
    try { localStorage.setItem(key, id); } catch (_) { /* The in-memory identifier remains usable. */ }
    return id;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function safeList(value) { return Array.isArray(value) ? value : []; }
  function printable(value, fallback = "未提供") { return value === null || value === undefined || value === "" ? fallback : String(value); }
  function millis(value) { return typeof value === "number" && Number.isFinite(value) ? `${value.toLocaleString()} ms` : "耗时未提供"; }
  function tokenText(value) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "未提供"; }
  function serverDate(value) {
    if (typeof value !== "string") return new Date(NaN);
    return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`);
  }
  function statusPill(status) {
    const pill = node("span", "state-pill", stateLabels[status] || printable(status));
    pill.dataset.tone = status === "SUCCEEDED" ? "success" : ["FAILED", "MANUAL_REVIEW", "CANCELLED"].includes(status) ? "error" : "neutral";
    return pill;
  }

  async function requestJSON(url, options = {}) {
    let response;
    try { response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } }); }
    catch (error) {
      if (error.name === "AbortError") throw error;
      const connectionError = new Error("连接中断，未能收到服务结果。请确认本机服务仍在运行。");
      connectionError.resultUncertain = true;
      throw connectionError;
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务没有返回可读取的数据，请确认当前访问的是项目页面。"); }
    if (!response.ok) {
      let message = typeof data.detail === "string" ? data.detail : "请求暂时未完成，请稍后重试。";
      if (Array.isArray(data.detail)) message = data.detail.map((item) => `${safeList(item.loc).filter((part) => part !== "body").join(" · ")}：${item.msg || "格式不正确"}`).join("；");
      throw new Error(message);
    }
    return data;
  }

  async function refreshHealth() {
    const badge = $("service-status");
    const label = $("service-label");
    badge.disabled = true;
    try {
      const data = await requestJSON("/health/ready");
      const degraded = data.checks?.redis === "degraded";
      badge.dataset.state = degraded ? "degraded" : "ready";
      label.textContent = degraded ? "基础可用 · 缓存降级" : "基础服务可用";
      badge.title = "检查数据库、缓存与索引目录；不代表模型调用已验证。点击刷新。";
    } catch (_) {
      badge.dataset.state = "unavailable";
      label.textContent = "服务待启动";
      badge.title = "尚未确认基础服务就绪。点击重新检查。";
    } finally { badge.disabled = false; }
  }

  function profileSnapshot() {
    return { grade: $("grade").value.trim(), college: $("college").value.trim(), targetMajor: $("target-major").value.trim() };
  }

  function setBusy(busy) {
    state.busy = busy;
    $("submit-query").disabled = busy;
    $("submit-label").textContent = busy ? "正在整理" : "发送问题";
    $("question").disabled = busy;
    $("new-conversation").disabled = busy;
    for (const id of ["grade", "college", "target-major"]) $(id).disabled = busy;
    $("query-form").setAttribute("aria-busy", String(busy));
    document.querySelectorAll(".example-button").forEach((button) => { button.disabled = busy; });
    $("loading-state").hidden = !busy;
    clearInterval(state.timer);
    if (busy) {
      const started = Date.now();
      $("elapsed-time").textContent = "00:00";
      state.timer = setInterval(() => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        $("elapsed-time").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      }, 1000);
    }
  }

  function clearAction() {
    clearTimeout(state.pollTimer);
    state.taskRequest++;
    state.draft = null;
    state.task = null;
    state.taskReused = false;
    state.actionBusy = false;
    $("action-section").hidden = true;
    $("action-feedback").hidden = true;
    $("task-result").replaceChildren();
    $("task-result").hidden = true;
    $("action-controls").hidden = false;
    $("create-draft").disabled = false;
    $("create-draft").textContent = "查看准备清单草稿 →";
    if ($("confirmation-dialog").open) $("confirmation-dialog").close();
  }

  async function submitQuery(event) {
    event?.preventDefault();
    if (state.busy) return;
    const message = $("question").value.trim();
    if (!message) { $("question").focus(); return; }
    if (message.length > 500) return;
    const version = ++state.version;
    state.traceRequest++;
    clearAction();
    state.latest = null;
    $("query-error").hidden = true;
    $("welcome-state").hidden = true;
    $("refresh-trace").disabled = true;
    $("trace-content").replaceChildren(node("p", "trace-loading", "本次请求完成后显示运行记录"));
    const profile = profileSnapshot();
    const submittedAt = Date.now();
    const body = { user_id: state.userId, message, grade: profile.grade || null, college: profile.college || null, major: profile.targetMajor || null };
    if (state.conversationId !== null) body.conversation_id = state.conversationId;
    state.queryController = new AbortController();
    setBusy(true);
    try {
      const result = await requestJSON("/agent/query", { method: "POST", body: JSON.stringify(body), signal: state.queryController.signal });
      if (version !== state.version) return;
      state.conversationId = result.conversation_id;
      state.latest = { ...result, message, profile, version, submittedAt };
      $("conversation-label").textContent = "继续当前对话";
      const card = renderAnswer(state.latest);
      $("answers").prepend(card);
      prepareAction(state.latest);
      $("refresh-trace").disabled = false;
      void refreshTrace();
    } catch (error) {
      if (version !== state.version || error.name === "AbortError") return;
      $("query-error-message").textContent = error.resultUncertain ? `${error.message} 后台可能仍在处理；重新发送会发起新请求，请稍候再重试。` : error.message;
      $("query-error").hidden = false;
      $("trace-content").replaceChildren(node("p", "trace-list-empty", "本次请求未返回运行标识，暂时无法读取记录。"));
    } finally {
      if (version === state.version) { setBusy(false); state.queryController = null; }
    }
  }

  function addInline(parent, value) {
    // Only create text and a small set of DOM elements. Model text never becomes HTML.
    const parts = String(value).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.append(node("strong", "", part.slice(2, -2)));
      else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) parent.append(node("code", "", part.slice(1, -1)));
      else parent.append(document.createTextNode(part));
    }
  }

  function renderAnswerText(value) {
    const container = node("div", "answer-text");
    const lines = printable(value, "本次没有返回回答内容。").split(/\r?\n/);
    let paragraph = null;
    let list = null;
    for (const line of lines) {
      if (!line.trim()) { paragraph = null; list = null; continue; }
      const heading = line.match(/^#{1,4}\s+(.+)$/);
      const item = line.match(/^\s*(?:[-*]\s+|\d+[.、]\s*)(.+)$/);
      if (heading) { const h = node("h3"); addInline(h, heading[1]); container.append(h); paragraph = null; list = null; }
      else if (item) {
        if (!list) { list = node("ul"); container.append(list); }
        const li = node("li"); addInline(li, item[1]); list.append(li); paragraph = null;
      } else {
        list = null;
        if (!paragraph) { paragraph = node("p"); container.append(paragraph); }
        else paragraph.append(node("br"));
        addInline(paragraph, line);
      }
    }
    return container;
  }

  function safeSourceURL(value) {
    try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch (_) { return null; }
  }

  function renderAnswer(result) {
    const card = node("article", "answer-card");
    card.append(node("div", "answer-label", "CAMPUSCOPILOT 的回答"), node("h2", "question-echo", result.message), renderAnswerText(result.answer));
    const notices = [...safeList(result.warnings), ...safeList(result.unknowns)].filter((item) => typeof item === "string" && item.trim());
    if (result.conflict_detected && !notices.some((item) => item.includes("冲突") || item.includes("差异"))) notices.unshift("检索资料中存在差异，请结合来源核对，勿直接认定某一版本现行有效。");
    if (notices.length) {
      const box = node("div", "answer-notices");
      for (const message of [...new Set(notices)]) { const p = node("p"); p.append(node("span", "notice-bullet", "ⓘ"), node("span", "", message)); box.append(p); }
      card.append(box);
    }
    const citations = safeList(result.citations);
    const sources = node("section", "citations");
    const title = node("h3", "citations-heading", "参考来源 "); title.append(node("span", "", citations.length)); sources.append(title);
    if (!citations.length) sources.append(node("p", "no-citations", "本次没有可展示的有效引用，请留意回答中的证据限制。"));
    else {
      const list = node("div", "citation-list");
      citations.forEach((source, index) => {
        const url = safeSourceURL(source.url);
        const link = node(url ? "a" : "div", url ? "citation-link" : "citation-static");
        if (url) { link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; }
        const heading = node("div", "citation-title"); heading.append(node("span", "", String(index + 1).padStart(2, "0")), node("span", "", source.title || "公开资料"), node("span", "", url ? "↗" : ""));
        const metadata = [source.published_at, source.version_label, source.collection_status === "STALE" ? "历史资料" : "收录资料"].filter(Boolean).join(" · ");
        link.append(heading, node("p", "citation-meta", metadata));
        if (source.scope) link.append(node("p", "citation-meta", source.scope));
        if (source.validity) link.append(node("p", "citation-warning", source.validity));
        list.append(link);
      });
      sources.append(list);
    }
    card.append(sources);
    const footer = node("div", "answer-footer"); footer.append(node("span", "", "公开资料辅助查询，请核对当年通知"), node("span", "", new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }))); card.append(footer);
    return card;
  }

  function traceSection(title, right) {
    const section = node("section", "trace-section");
    const heading = node("h3", "trace-section-title", title);
    if (right) heading.append(node("span", "", right));
    section.append(heading);
    return section;
  }

  function renderTrace(data) {
    const container = node("div");
    const header = node("div", "trace-run-header");
    const id = node("span", "trace-run-id", `RUN ${data.run_id}`); id.title = data.run_id; header.append(id, statusPill(data.status)); container.append(header);
    const retrievals = safeList(data.retrievals);
    const candidates = retrievals.reduce((sum, record) => sum + safeList(record.results).length, 0);
    const retrieval = traceSection("01 / 政策检索");
    const stat = node("div", "trace-stat"); stat.append(node("strong", "", candidates), node("span", "", "条原始候选"), node("small", "", `${retrievals.length} 次记录`)); retrieval.append(stat, node("p", "trace-footnote", "候选不一定成为有效引用，回答中的来源以左侧为准。")); container.append(retrieval);
    const attempts = safeList(data.model_attempts);
    const models = traceSection("02 / 实际模型调用", `${attempts.length} 次`);
    if (!attempts.length) models.append(node("p", "trace-list-empty", "没有模型调用记录，可能由证据检查直接安全答复。"));
    attempts.forEach((attempt) => {
      const row = node("div", "model-row");
      const heading = node("div", "model-row-header");
      const operation = { classification: "意图与风险分类", answer: "生成回答" }[attempt.operation] || printable(attempt.operation);
      heading.append(node("strong", "", `${attempt.attempt_no}. ${operation}`), statusPill(attempt.status));
      row.append(heading, node("p", "model-row-meta", `${printable(attempt.provider)} · ${millis(attempt.latency_ms)}`));
      const tokens = node("div", "model-tokens"); tokens.append(node("span", "", `输入 ${tokenText(attempt.prompt_tokens)}`), node("span", "", `输出 ${tokenText(attempt.completion_tokens)}`), node("span", "", `总计 ${tokenText(attempt.total_tokens)}`)); row.append(tokens);
      if (attempt.error_code) row.append(node("p", "model-row-meta", `错误：${attempt.error_code}`));
      models.append(row);
    });
    models.append(node("p", "trace-footnote", "Token 为服务商返回的实际用量；缺失字段显示“未提供”。")); container.append(models);
    const events = safeList(data.blackboard_events);
    const workflow = traceSection("03 / Agent 执行步骤", `${events.length} 条事件`);
    if (!events.length) workflow.append(node("p", "trace-list-empty", "本次没有 Agent 工作流事件。"));
    else {
      const timeline = node("ol", "trace-timeline");
      events.forEach((event) => { const li = node("li", "", `${agentLabels[event.agent] || printable(event.agent)} · ${eventLabels[event.event_type] || printable(event.event_type)}`); li.append(node("small", "", `事件 ${event.sequence}`)); timeline.append(li); });
      workflow.append(timeline);
    }
    container.append(workflow);
    const tasks = safeList(data.tasks);
    const taskSection = traceSection("04 / 准备任务", `${tasks.length} 个`);
    if (!tasks.length) taskSection.append(node("p", "trace-list-empty", "尚无关联任务，创建仍需你明确确认。"));
    tasks.forEach((task) => { const row = node("div", "trace-task"); const label = node("span", "", `${String(task.task_id).slice(0, 8)}… · 已领取 ${task.attempts} 次`); label.title = task.task_id; row.append(label, statusPill(task.status)); taskSection.append(row); });
    container.append(taskSection);
    if (data.error_code) container.append(node("p", "trace-error", `运行错误：${data.error_code}`));
    return container;
  }

  async function refreshTrace() {
    const latest = state.latest;
    if (!latest?.run_id) return;
    const request = ++state.traceRequest;
    const version = state.version;
    $("refresh-trace").disabled = true;
    $("refresh-trace").textContent = "正在读取…";
    try {
      const data = await requestJSON(`/runs/${encodeURIComponent(latest.run_id)}?user_id=${encodeURIComponent(state.userId)}`);
      if (version !== state.version || request !== state.traceRequest) return;
      $("trace-content").replaceChildren(renderTrace(data));
    } catch (error) {
      if (version !== state.version || request !== state.traceRequest) return;
      const box = node("div", "trace-error", error.message);
      const retry = node("button", "text-button", "重新读取记录 ↻"); retry.type = "button"; retry.addEventListener("click", refreshTrace); box.append(retry); $("trace-content").replaceChildren(box);
    } finally {
      if (version === state.version && request === state.traceRequest) { $("refresh-trace").disabled = false; $("refresh-trace").textContent = "刷新记录 ↻"; }
    }
  }

  function scholarshipType(message) {
    for (const name of ["国家励志奖学金", "国家奖学金", "国家助学金", "校级奖学金"]) if (message.includes(name)) return name;
    return "奖助学金申请";
  }

  function prepareAction(latest) {
    if (!latest.can_create_task || !["scholarship", "major_transfer"].includes(latest.intent)) return;
    $("action-section").hidden = false;
    const isScholarship = latest.intent === "scholarship";
    $("action-description").textContent = isScholarship ? "根据本次提问的年级、学院和申请类别，创建一份内部准备清单。" : "根据本次提问时填写的年级和目标专业，创建转专业准备清单。";
    const missing = isScholarship ? !latest.profile.grade || !latest.profile.college : !latest.profile.grade || !latest.profile.targetMajor;
    if (missing) {
      $("create-draft").disabled = true;
      $("action-feedback").textContent = isScholarship ? "请补充年级和学院后重新提问，再创建准备清单。" : "请在适用范围补充年级和目标专业后重新提问，再创建准备清单。";
      $("action-feedback").hidden = false;
    }
  }

  function showDraftDialog(draft) {
    $("draft-summary").textContent = draft.summary;
    $("draft-warning").textContent = draft.warning || "只创建项目内部准备任务，不会向学校提交申请。";
    const date = serverDate(draft.expires_at);
    $("draft-expiry").textContent = Number.isNaN(date.getTime()) ? "草稿确认有时效限制，请及时确认。" : `确认有效期至 ${date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" })}（北京时间）`;
    $("confirmation-error").hidden = true;
    $("confirm-action").disabled = false;
    $("confirm-action").textContent = "确认创建任务";
    $("cancel-confirmation").disabled = false;
    $("close-confirmation").disabled = false;
    $("confirmation-dialog").showModal();
  }

  async function createDraft() {
    if (state.actionBusy || !state.latest) return;
    if (state.draft) {
      const expiry = serverDate(state.draft.expires_at).getTime();
      if (!Number.isFinite(expiry) || expiry > Date.now()) { showDraftDialog(state.draft); return; }
      state.draft = null;
    }
    const latest = state.latest;
    const version = state.version;
    const isScholarship = latest.intent === "scholarship";
    const details = isScholarship ? { grade: latest.profile.grade, college: latest.profile.college, scholarship_type: scholarshipType(latest.message) } : { grade: latest.profile.grade, target_major: latest.profile.targetMajor };
    const body = { user_id: state.userId, conversation_id: latest.conversation_id, run_id: latest.run_id, action_type: isScholarship ? "scholarship_preparation" : "major_transfer_preparation", details };
    state.actionBusy = true;
    $("create-draft").disabled = true;
    $("create-draft").textContent = "正在生成草稿…";
    $("action-feedback").hidden = true;
    try {
      const draft = await requestJSON("/actions/drafts", { method: "POST", body: JSON.stringify(body) });
      if (version !== state.version) return;
      state.draft = draft; // Confirmation tokens stay only in this in-memory object.
      showDraftDialog(draft);
    } catch (error) {
      if (version !== state.version) return;
      $("action-feedback").textContent = error.message;
      $("action-feedback").hidden = false;
    } finally {
      if (version === state.version) { state.actionBusy = false; $("create-draft").disabled = false; $("create-draft").textContent = state.draft ? "再次查看草稿 →" : "重试生成准备清单草稿 →"; }
    }
  }

  async function confirmAction() {
    if (state.actionBusy || !state.draft) return;
    const draft = state.draft;
    const version = state.version;
    state.actionBusy = true;
    $("confirm-action").disabled = true;
    $("confirm-action").textContent = "正在确认…";
    $("cancel-confirmation").disabled = true;
    $("close-confirmation").disabled = true;
    $("confirmation-error").hidden = true;
    try {
      const task = await requestJSON("/actions/confirm", { method: "POST", body: JSON.stringify({ user_id: state.userId, draft_id: draft.draft_id, confirmation_token: draft.confirmation_token }) });
      if (version !== state.version) return;
      state.task = task;
      const createdAt = serverDate(task.created_at).getTime();
      state.taskReused = Number.isFinite(createdAt) && createdAt < state.latest.submittedAt - 1000;
      state.draft = null;
      $("confirmation-dialog").close();
      $("action-controls").hidden = true;
      renderTask(task);
      void refreshTrace();
      if (!TERMINAL_TASKS.has(task.status)) scheduleTaskPoll(version, 0);
    } catch (error) {
      if (version !== state.version) return;
      if (error.message.includes("过期")) {
        state.draft = null;
        $("confirmation-dialog").close();
        $("action-feedback").textContent = "确认已过期，请重新生成草稿并核对内容。";
        $("action-feedback").hidden = false;
        $("create-draft").textContent = "重新生成准备清单草稿 →";
      } else {
        $("confirmation-error").textContent = `${error.message} 原草稿已保留，可以重试确认。`;
        $("confirmation-error").hidden = false;
      }
    } finally {
      if (version === state.version) {
        state.actionBusy = false;
        $("confirm-action").disabled = false;
        $("confirm-action").textContent = "确认创建任务";
        $("cancel-confirmation").disabled = false;
        $("close-confirmation").disabled = false;
      }
    }
  }

  function renderTask(task) {
    const container = $("task-result");
    container.replaceChildren(); container.hidden = false;
    const heading = node("div", "task-heading"); heading.append(node("strong", "", task.result?.title || "内部准备任务"), statusPill(task.status)); container.append(heading);
    if (task.result?.items?.length) { const list = node("ol", "task-items"); safeList(task.result.items).forEach((item) => list.append(node("li", "", item))); container.append(list); }
    else container.append(node("p", "task-disclaimer", task.status === "MANUAL_REVIEW" ? "任务需要人工处理，请查看运行记录。" : "任务已保存，等待后台执行器处理。可以稍后刷新状态。"));
    container.append(node("p", "task-disclaimer", task.result?.disclaimer || "这是项目内部准备任务，不代表已向学校提交申请。"));
    if (state.taskReused) container.append(node("p", "task-disclaimer", "已复用此前创建的同内容任务，它可能关联先前的运行记录。"));
    if (task.error_message) container.append(node("p", "inline-feedback", task.error_message));
    const meta = node("div", "task-meta");
    const id = node("span", "", `任务 ${String(task.task_id).slice(0, 8)}… · 已领取 ${task.attempts} 次`); id.title = task.task_id;
    const refresh = node("button", "text-button", "刷新状态 ↻"); refresh.type = "button";
    refresh.addEventListener("click", async () => { refresh.disabled = true; await refreshTask(); if (refresh.isConnected) refresh.disabled = false; });
    meta.append(id, refresh); container.append(meta);
  }

  async function refreshTask() {
    if (!state.task) return;
    const version = state.version;
    const request = ++state.taskRequest;
    const taskId = state.task.task_id;
    try {
      const task = await requestJSON(`/actions/tasks/${encodeURIComponent(taskId)}?user_id=${encodeURIComponent(state.userId)}`);
      if (version !== state.version || request !== state.taskRequest || state.task?.task_id !== taskId) return;
      const changed = state.task.status !== task.status;
      state.task = task;
      renderTask(task);
      $("action-feedback").hidden = true;
      if (changed) void refreshTrace();
      if (TERMINAL_TASKS.has(task.status)) clearTimeout(state.pollTimer);
    } catch (error) {
      if (version !== state.version || request !== state.taskRequest) return;
      $("action-feedback").textContent = `状态读取未完成：${error.message}`;
      $("action-feedback").hidden = false;
    }
  }

  function scheduleTaskPoll(version, count) {
    clearTimeout(state.pollTimer);
    if (count >= 20 || !state.task || TERMINAL_TASKS.has(state.task.status) || version !== state.version) return;
    state.pollTimer = setTimeout(async () => { await refreshTask(); scheduleTaskPoll(version, count + 1); }, 3000);
  }

  function newConversation() {
    state.version++;
    state.traceRequest++;
    state.queryController?.abort();
    state.queryController = null;
    state.conversationId = null;
    state.latest = null;
    setBusy(false);
    clearAction();
    $("answers").replaceChildren();
    $("question").value = "";
    $("input-hint").textContent = "Ctrl + Enter 发送 · 最多 500 字";
    $("conversation-label").textContent = "新对话";
    $("welcome-state").hidden = false;
    $("query-error").hidden = true;
    $("refresh-trace").disabled = true;
    $("refresh-trace").textContent = "刷新记录 ↻";
    const empty = node("div", "trace-empty"); empty.append(node("span", "", "⌁"), node("strong", "", "等待你的第一个问题"), node("p", "", "提问后，这里会显示检索、模型调用与执行步骤。")); $("trace-content").replaceChildren(empty);
    $("question").focus();
  }

  $("demo-user-id").value = state.userId;
  $("query-form").addEventListener("submit", submitQuery);
  $("retry-query").addEventListener("click", submitQuery);
  $("new-conversation").addEventListener("click", newConversation);
  $("refresh-trace").addEventListener("click", refreshTrace);
  $("service-status").addEventListener("click", refreshHealth);
  $("create-draft").addEventListener("click", createDraft);
  $("confirm-action").addEventListener("click", confirmAction);
  for (const id of ["close-confirmation", "cancel-confirmation"]) $(id).addEventListener("click", () => { if (!state.actionBusy) $("confirmation-dialog").close(); });
  $("confirmation-dialog").addEventListener("cancel", (event) => { if (state.actionBusy) event.preventDefault(); });
  $("question").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); void submitQuery(); } });
  function updateInputHint() { $("input-hint").textContent = $("question").value.length ? `Ctrl + Enter 发送 · ${$("question").value.length} / 500 字` : "Ctrl + Enter 发送 · 最多 500 字"; }
  $("question").addEventListener("input", updateInputHint);
  for (const id of ["grade", "college", "target-major"]) $(id).addEventListener("input", () => { $("scope-summary").textContent = [$("grade").value.trim(), $("college").value.trim(), $("target-major").value.trim()].filter(Boolean).join(" · ") || "请补充你的适用范围"; });
  document.querySelectorAll(".example-button").forEach((button) => button.addEventListener("click", () => { $("question").value = button.dataset.question; updateInputHint(); $("question").focus(); }));
  void refreshHealth();
})();
