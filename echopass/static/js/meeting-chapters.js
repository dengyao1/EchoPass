/**
 * 中栏「章节」Tab：工具条、列表、POST /api/meeting/chapters、点击章节联动转写滚动。
 * 依赖由 EchoPassChapters.init(ctx) 注入，ctx.getState() 须返回与主脚本共享的 state 对象。
 */
(function (W) {
  "use strict";

  /** @type {null | Record<string, unknown>} */
  let ctx = null;

  function _fmtRelTime(tsMs) {
    if (!tsMs) return "";
    const diff = Math.max(0, Date.now() - tsMs);
    const sec = Math.round(diff / 1000);
    if (sec < 60) return sec + " 秒前";
    const m = Math.floor(sec / 60);
    if (m < 60) return m + " 分钟前";
    const h = Math.floor(m / 60);
    if (h < 24) return h + " 小时前";
    return new Date(tsMs).toLocaleString();
  }

  function renderChapterToolbar() {
    if (!ctx) return;
    const $ = ctx.$;
    const escHtml = ctx.escHtml;
    const state = ctx.getState();
    const meta = $("chapterMeta");
    const btn = $("btnGenChapters");
    const label = $("btnGenChaptersLabel");
    if (!meta || !btn || !label) return;
    const hasTranscript = state.transcriptLines.length > 0;
    const inFlight = state.chaptersInFlight;

    btn.disabled = !hasTranscript || inFlight;
    if (inFlight) {
      label.innerHTML = '<span class="spin"></span> 正在生成…';
    } else if (state.chaptersData && state.chaptersData.length) {
      label.textContent = "重新生成";
    } else {
      label.textContent = "立即生成";
    }

    if (state.chaptersError) {
      meta.innerHTML = '<b style="color:#ff8080">生成失败：</b>' + escHtml(state.chaptersError);
    } else if (state.chaptersGeneratedAt) {
      const cnt = state.chaptersData ? state.chaptersData.length : 0;
      meta.innerHTML =
        "已生成 <b>" + cnt + "</b> 个章节 · 上次更新 <b>" + _fmtRelTime(state.chaptersGeneratedAt) + "</b>";
    } else if (hasTranscript) {
      meta.innerHTML =
        "当前共 <b>" + state.transcriptLines.length + "</b> 条转录，点击右侧按钮交给 AI 切章并起标题";
    } else {
      meta.textContent = "点击右侧按钮，让 AI 按话题切分章节并生成精炼标题";
    }
  }

  function renderChapterList() {
    if (!ctx) return;
    const $ = ctx.$;
    const escHtml = ctx.escHtml;
    const fmtMS = ctx.fmtMS;
    const state = ctx.getState();
    const box = $("chapterList");
    if (!box) return;
    const chs = state.chaptersData || [];
    if (!chs.length) {
      if (state.chaptersInFlight) {
        box.innerHTML =
          '<div class="summary-placeholder"><span class="emoji">⏳</span>AI 正在分析转录、提炼章节标题与摘要…</div>';
      } else if (state.transcriptLines.length === 0) {
        box.innerHTML =
          '<div class="summary-placeholder"><span class="emoji">📚</span>开始录音后点击"立即生成"，AI 会按话题切分章节并配上 2-4 句摘要</div>';
      } else {
        box.innerHTML =
          '<div class="summary-placeholder"><span class="emoji">📚</span>点击右上角"立即生成"，让 AI 按话题切分章节</div>';
      }
      return;
    }
    box.innerHTML = chs
      .map((c) => {
        const startMs = c.start_ms || 0;
        const endMs = c.end_ms || startMs;
        const startSec = startMs / 1000;
        const dur = Math.max(0, (endMs - startMs) / 1000);
        const speakers = (c.speakers || []).filter(Boolean);
        const slRaw = c.start_line;
        const slNum = slRaw == null || slRaw === "" ? NaN : Number(slRaw);
        const sl = Number.isFinite(slNum) && slNum >= 0 ? String(Math.trunc(slNum)) : "";
        return (
          '<div class="chapter-card" data-start="' +
          startMs +
          '" data-start-line="' +
          sl +
          '" title="点击跳转左侧对应转写">' +
          '<div class="head">' +
          '<span class="ts-badge" title="跳转到 ' +
          fmtMS(startSec) +
          '">' +
          '<span class="play-icon"></span>' +
          fmtMS(startSec) +
          "</span>" +
          '<div class="title">' +
          escHtml(c.title || "（无标题）") +
          "</div>" +
          "</div>" +
          (c.summary ? '<div class="summary">' + escHtml(c.summary) + "</div>" : "") +
          '<div class="footer">' +
          (speakers.length
            ? '<span class="speakers">' + escHtml(speakers.join("、")) + '</span><span class="dot"></span>'
            : "") +
          "<span>时长 " +
          fmtMS(dur) +
          "</span>" +
          (c.line_count ? '<span class="dot"></span><span>' + c.line_count + " 段</span>" : "") +
          "</div>" +
          "</div>"
        );
      })
      .join("");
    box.querySelectorAll(".chapter-card").forEach((node) => {
      node.addEventListener("click", () => {
        const startMs = parseInt(node.getAttribute("data-start"), 10) || 0;
        const slAttr = node.getAttribute("data-start-line");
        const startLine = slAttr !== "" && slAttr != null ? parseInt(slAttr, 10) : NaN;
        let idx = state.transcriptLines.findIndex((l) => (l.start_ms || 0) >= startMs);
        if (Number.isFinite(startLine) && startLine >= 0 && startLine < state.transcriptLines.length) {
          idx = startLine;
        } else if (idx < 0) {
          idx = Math.max(0, state.transcriptLines.length - 1);
        }
        if (state.audioUrl && typeof ctx.seekAudioTo === "function") {
          ctx.seekAudioTo(startMs / 1000);
        }
        if (typeof ctx.scrollTranscriptToLineIdx === "function") {
          ctx.scrollTranscriptToLineIdx(idx);
        }
      });
    });
  }

  function renderChapters() {
    renderChapterToolbar();
    renderChapterList();
  }

  async function requestChapters() {
    if (!ctx) return;
    const state = ctx.getState();
    if (state.chaptersInFlight) return;
    if (!state.transcriptLines.length) {
      state.chaptersError = "当前没有转录文本，无法生成章节";
      renderChapterToolbar();
      return;
    }
    state.chaptersInFlight = true;
    state.chaptersError = null;
    renderChapters();
    try {
      const sid = state.liveSessionId || "default";
      const resp = await fetch("/api/meeting/chapters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid }),
      });
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status);
      }
      const data = await resp.json();
      state.chaptersData = Array.isArray(data.chapters) ? data.chapters : [];
      state.chaptersGeneratedAt = Date.now();
    } catch (err) {
      state.chaptersError = (err && err.message) || String(err);
    } finally {
      state.chaptersInFlight = false;
      renderChapters();
    }
  }

  W.EchoPassChapters = {
    /**
     * @param {object} c
     * @param {function(string): HTMLElement|null} c.$
     * @param {function(string): string} c.escHtml
     * @param {function(number): string} c.fmtMS
     * @param {function(): object} c.getState
     * @param {function(number): void} [c.seekAudioTo]
     * @param {function(number): void} c.scrollTranscriptToLineIdx
     */
    init(c) {
      ctx = c;
      const btn = W.document.getElementById("btnGenChapters");
      if (btn) btn.addEventListener("click", () => requestChapters());
    },
    renderChapters,
    requestChapters,
  };
})(typeof window !== "undefined" ? window : globalThis);
