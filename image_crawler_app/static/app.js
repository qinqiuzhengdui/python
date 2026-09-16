/**
 * 前端交互逻辑：
 * 1. 提交网址 -> 调用 /api/crawl -> 渲染图片网格
 * 2. 点击“生成 PPT” -> 调用 /api/ppt -> 输出 PPTX 下载与在线预览
 */
(function () {
  "use strict";

  const form = document.getElementById("crawl-form");
  const urlInput = document.getElementById("url-input");
  const maxInput = document.getElementById("max-input");
  const btn = document.getElementById("crawl-btn");
  const status = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const errorBox = document.getElementById("error");
  const result = document.getElementById("result");
  const summary = document.getElementById("summary");
  const grid = document.getElementById("grid");

  // PPT 面板元素
  const pptPanel = document.getElementById("ppt-panel");
  const pptBtn = document.getElementById("ppt-btn");
  const pptTitle = document.getElementById("ppt-title");
  const pptGoal = document.getElementById("ppt-goal");
  const pptTheme = document.getElementById("ppt-theme");
  const pptStatus = document.getElementById("ppt-status");
  const pptStatusText = document.getElementById("ppt-status-text");
  const pptResult = document.getElementById("ppt-result");
  const pptPptxLink = document.getElementById("ppt-pptx-link");
  const pptPreviewLink = document.getElementById("ppt-preview-link");
  const pptMeta = document.getElementById("ppt-meta");
  const pptError = document.getElementById("ppt-error");

  // 最近一次爬取来源网址（用于推导 PPT 默认标题）
  let lastPageUrl = "";

  /** 格式化文件大小（字节 -> KB/MB）。 */
  function formatSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
    if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " KB";
    return bytes + " B";
  }

  /** 拼接本地图片访问地址。 */
  function localUrl(name) {
    return "crawled/" + encodeURIComponent(name);
  }

  /** 渲染图片网格。 */
  function render(images) {
    grid.innerHTML = "";
    for (const img of images) {
      const item = document.createElement("div");
      item.className = "grid-item";

      const link = document.createElement("a");
      link.href = localUrl(img.name);
      link.target = "_blank";
      link.rel = "noopener";
      link.title = img.url;

      const pic = document.createElement("img");
      pic.src = localUrl(img.name);
      pic.loading = "lazy";
      pic.alt = img.url;
      link.appendChild(pic);

      const meta = document.createElement("div");
      meta.className = "meta";
      const name = document.createElement("span");
      name.textContent = img.name;
      name.title = img.url;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = formatSize(img.size);
      meta.append(name, size);

      item.append(link, meta);
      grid.appendChild(item);
    }
  }

  /** 展示错误信息。 */
  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  /** 展示 PPT 面板错误信息。 */
  function showPptError(message) {
    pptError.textContent = message;
    pptError.hidden = false;
  }

  /** 重置 PPT 面板为待生成状态。 */
  function resetPptPanel() {
    pptError.hidden = true;
    pptResult.hidden = true;
    pptStatus.hidden = true;
  }

  /** 一键生成 PPT：调用 /api/ppt 并展示下载/预览入口。 */
  async function onGeneratePpt() {
    pptError.hidden = true;
    pptResult.hidden = true;
    pptBtn.disabled = true;
    pptStatus.hidden = false;
    pptStatusText.textContent = "正在生成 PPT，需渲染并导出，请耐心等待 1-3 分钟...";

    try {
      const resp = await fetch("/api/ppt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: pptTitle.value.trim(),
          goal: pptGoal.value.trim(),
          theme: pptTheme.value,
          page_url: lastPageUrl,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));

      pptPptxLink.href = data.pptx_url;
      pptPreviewLink.href = data.html_url;
      pptMeta.textContent =
        "　|　共 " + data.page_count + " 页（含 " + data.image_count + " 张图片）";
      pptResult.hidden = false;
    } catch (err) {
      showPptError("PPT 生成失败: " + err.message);
    } finally {
      pptBtn.disabled = false;
      pptStatus.hidden = true;
    }
  }

  /** 表单提交处理。 */
  async function onSubmit(e) {
    e.preventDefault();
    errorBox.hidden = true;
    result.hidden = true;
    grid.innerHTML = "";
    resetPptPanel();

    const url = urlInput.value.trim();
    const maxImages = Number(maxInput.value) || 50;

    btn.disabled = true;
    status.hidden = false;
    statusText.textContent = "正在打开页面并爬取图片，请稍候...";
    const start = Date.now();

    try {
      const resp = await fetch("/api/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, max_images: maxImages }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));

      const seconds = ((Date.now() - start) / 1000).toFixed(1);
      summary.textContent =
        "来源: " + data.page_url + "　|　发现 " + data.total +
        " 张，成功 " + data.ok + " 张，失败 " + data.fail +
        " 张　|　耗时 " + seconds + " 秒";

      lastPageUrl = data.page_url;

      if (data.images.length > 0) {
        render(data.images);
        result.hidden = false;
        pptPanel.hidden = false;
      } else {
        pptPanel.hidden = true;
        showError("没有抓到图片。可能原因：页面需要登录、被反爬拦截，或图片为 JS 动态注入。");
      }
    } catch (err) {
      pptPanel.hidden = true;
      showError("爬取失败: " + err.message);
    } finally {
      btn.disabled = false;
      status.hidden = true;
    }
  }

  form.addEventListener("submit", onSubmit);
  pptBtn.addEventListener("click", onGeneratePpt);
})();
