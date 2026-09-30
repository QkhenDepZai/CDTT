/*!
 * Python Master 2026 - Chat Widget (Module D1)
 *
 * Nhúng vào website bất kỳ bằng 1 dòng:
 *   <script src="https://<backend>/widget.js" data-site-key="site_demo_001" defer></script>
 *
 * Tuỳ chọn (data-*):
 *   data-api-base       URL backend (mặc định = nơi phục vụ widget.js)
 *   data-title          tiêu đề khung chat
 *   data-position       "right" (mặc định) | "left"
 *   data-open           "true" -> tự mở khung chat khi tải trang (tiện cho QC)
 *
 * API cho trang chủ / kiểm thử: window.PythonMasterChat.open() | close() | toggle() | newChat()
 *
 * Toàn bộ giao diện nằm trong Shadow DOM: CSS của website chủ không làm vỡ
 * widget và CSS của widget không ảnh hưởng website chủ.
 */
(function () {
  "use strict";

  if (window.PythonMasterChat) return; // chống nhúng 2 lần

  // ---------------------------------------------------------------------------
  // 1. Cấu hình
  // ---------------------------------------------------------------------------
  var script = document.currentScript || document.querySelector("script[data-site-key]");
  if (!script || !script.getAttribute("data-site-key")) {
    console.error("[PythonMasterChat] Thiếu thuộc tính data-site-key trên thẻ <script>.");
    return;
  }

  var CONFIG = {
    siteKey: script.getAttribute("data-site-key"),
    apiBase: (script.getAttribute("data-api-base") || new URL(script.src, location.href).origin).replace(/\/+$/, ""),
    title: script.getAttribute("data-title") || "Trợ lý Python Master",
    position: script.getAttribute("data-position") === "left" ? "left" : "right",
    autoOpen: script.getAttribute("data-open") === "true",
  };

  var MAX_IMAGE_BYTES = 5 * 1024 * 1024;
  var ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
  var MAX_MESSAGE_CHARS = 2000;
  var POLL_INTERVAL_MS = 3000;
  var STORAGE_KEY = "pm-chat:" + CONFIG.siteKey;
  var STATUS_TEXT = {
    bot: "Trợ lý AI · trực tuyến 24/7",
    waiting_agent: "Đang chờ tư vấn viên tiếp nhận…",
    agent: "Tư vấn viên đang hỗ trợ bạn",
    closed: "Phiên đã kết thúc · nhắn để tiếp tục",
  };
  var MSG = {
    network: "Không kết nối được máy chủ. Vui lòng kiểm tra mạng và thử lại.",
    badType: "Định dạng file không được hỗ trợ. Chỉ nhận ảnh JPG, PNG, WEBP.",
    tooLarge: "Dung lượng ảnh vượt quá giới hạn 5MB.",
    tooLong: "Tin nhắn tối đa " + MAX_MESSAGE_CHARS + " ký tự.",
    noToken: "Máy chủ chưa bật user_token (APP_SECRET_KEY) nên chưa xem được lịch sử.",
  };

  // ---------------------------------------------------------------------------
  // 2. Trạng thái & lưu trữ (localStorage chỉ giữ id phiên, không giữ nội dung chat)
  // ---------------------------------------------------------------------------
  var state = {
    userId: null,
    userToken: null,
    conversationId: null,
    status: "bot",
    lastMessageId: 0,
    faqSuggestions: [],
    support: null,
    open: false,
    busy: false,
    ready: null, // Promise: phiên đã khởi tạo/khôi phục xong
    pendingImage: null,
    unread: 0,
    pollTimer: null,
    lastUserText: "",
    lastAgentNotice: "",
  };

  function loadSession() {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && saved.userId) {
        state.userId = saved.userId;
        state.userToken = saved.userToken || null;
        state.conversationId = saved.conversationId || null;
        state.faqSuggestions = saved.faqSuggestions || [];
      }
    } catch (e) { /* trình duyệt chặn storage: chạy như phiên mới */ }
  }

  function saveSession() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        userId: state.userId,
        userToken: state.userToken,
        conversationId: state.conversationId,
        faqSuggestions: state.faqSuggestions,
      }));
    } catch (e) { /* bỏ qua */ }
  }

  // ---------------------------------------------------------------------------
  // 3. Gọi API
  // ---------------------------------------------------------------------------
  function ApiError(message, status) {
    this.message = message;
    this.status = status;
  }

  function api(path, options) {
    options = options || {};
    var headers = { "X-Site-Key": CONFIG.siteKey };
    if (state.userToken) headers["X-User-Token"] = state.userToken;
    var body;
    if (options.form) {
      body = options.form;
    } else if (options.json) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.json);
    }
    return fetch(CONFIG.apiBase + path, { method: options.method || "GET", headers: headers, body: body })
      .catch(function () { throw new ApiError(MSG.network, 0); })
      .then(function (response) {
        if (options.raw && response.ok) return response;
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new ApiError(data.error || "Có lỗi xảy ra (" + response.status + ").", response.status);
          return data;
        });
      });
  }

  /** Ghi nhận user_id / token / conversation_id / mốc tin nhắn từ mọi response. */
  function absorb(data) {
    if (data.user_id) state.userId = data.user_id;
    if (data.user_token) state.userToken = data.user_token;
    if (data.conversation_id) state.conversationId = data.conversation_id;
    if (data.last_message_id) state.lastMessageId = Math.max(state.lastMessageId, data.last_message_id);
    if (data.support) state.support = data.support;
    saveSession();
  }

  // ---------------------------------------------------------------------------
  // 4. Markdown tối giản & AN TOÀN (escape HTML trước, chỉ sinh thẻ đã biết)
  // ---------------------------------------------------------------------------
  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderInline(text) {
    var codes = [];
    var html = escapeHtml(text).replace(/`([^`\n]+)`/g, function (_, code) {
      codes.push("<code>" + code + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    html = html
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,!?:;])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return html.replace(/\u0000(\d+)\u0000/g, function (_, i) { return codes[+i]; });
  }

  function renderTextBlock(text) {
    var out = [];
    var list = null; // "ul" | "ol"
    function closeList() { if (list) { out.push("</" + list + ">"); list = null; } }
    text.split("\n").forEach(function (line) {
      var bullet = line.match(/^\s*[-*•]\s+(.*)$/);
      var numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      var heading = line.match(/^\s*#{1,6}\s+(.*)$/);
      if (bullet || numbered) {
        var type = bullet ? "ul" : "ol";
        if (list !== type) { closeList(); out.push("<" + type + ">"); list = type; }
        out.push("<li>" + renderInline((bullet || numbered)[1]) + "</li>");
      } else if (!line.trim()) {
        closeList();
        out.push('<div class="gap"></div>');
      } else {
        closeList();
        out.push(heading ? "<p><strong>" + renderInline(heading[1]) + "</strong></p>" : "<p>" + renderInline(line) + "</p>");
      }
    });
    closeList();
    return out.join("");
  }

  function renderMarkdown(text) {
    var parts = String(text || "").split(/```([\w+#.-]*)[ \t]*\n?([\s\S]*?)```/g);
    var html = "";
    for (var i = 0; i < parts.length; i += 3) {
      html += renderTextBlock(parts[i].replace(/^\n+|\n+$/g, ""));
      if (i + 2 < parts.length) {
        var lang = parts[i + 1] || "code";
        html += '<div class="code"><div class="code-head"><span>' + escapeHtml(lang) +
          '</span><button type="button" class="copy" aria-label="Sao chép mã">Sao chép</button></div>' +
          "<pre><code>" + escapeHtml(parts[i + 2].replace(/\n$/, "")) + "</code></pre></div>";
      }
    }
    return html.replace(/(<div class="gap"><\/div>)+$/, "");
  }

  // ---------------------------------------------------------------------------
  // 5. Giao diện
  // ---------------------------------------------------------------------------
  var ICON = {
    history: '<path d="M12 8v4l3 2m6-2a9 9 0 1 1-3-6.7M21 4v4h-4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    download: '<path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    clip: '<path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/>',
    send: '<path d="M4 12 20 4l-6 16-3-7-7-1z"/>',
    agent: '<path d="M4 14v-2a8 8 0 0 1 16 0v2M4 14h3v5H5a1 1 0 0 1-1-1v-4Zm16 0h-3v5h2a1 1 0 0 0 1-1v-4ZM17 19c0 1.5-2 2-5 2"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    chat: '<path d="M4 5h16v11H9l-5 4V5z"/>',
  };

  function icon(name) {
    return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICON[name] + "</svg>";
  }

  var CSS = [
    ":host{all:initial}",
    "*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}",
    ".root{--blue:#0b59c8;--blue-dark:#0847a3;--navy:#032259;--ink:#16202e;--muted:#5b6678;--line:#e3e8f0;--soft:#f3f6fb;--danger:#c62828;",
    "position:fixed;bottom:20px;z-index:2147483000;color:var(--ink);font-size:14px;line-height:1.5}",
    ".root.right{right:20px}.root.left{left:20px}",
    "button{font:inherit;cursor:pointer}",
    "button:focus-visible,textarea:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--blue);outline-offset:2px}",
    ".launcher{width:60px;height:60px;border-radius:50%;border:0;background:#fff;box-shadow:0 6px 20px rgba(3,34,89,.28);display:grid;place-items:center;position:relative;transition:transform .15s}",
    ".launcher:hover{transform:scale(1.06)}.launcher img{width:38px;height:38px}",
    ".badge{position:absolute;top:-2px;right:-2px;min-width:20px;height:20px;padding:0 5px;border-radius:10px;background:var(--danger);color:#fff;font-size:12px;font-weight:700;display:grid;place-items:center}",
    ".panel{position:absolute;bottom:76px;width:390px;height:min(640px,calc(100vh - 110px));background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(3,34,89,.25);display:flex;flex-direction:column;overflow:hidden}",
    ".right .panel{right:0}.left .panel{left:0}",
    ".panel[hidden],[hidden]{display:none!important}",
    "header{background:linear-gradient(135deg,var(--navy),var(--blue));color:#fff;padding:12px 8px 12px 12px;display:flex;align-items:center;gap:8px}",
    "header .avatar{width:38px;height:38px;border-radius:50%;background:#fff;display:grid;place-items:center;flex:none}",
    "header .avatar img{width:26px;height:26px}",
    "header .titles{flex:1;min-width:0}",
    "header h2{margin:0;font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    "header p{margin:0;font-size:12px;opacity:.9;display:flex;align-items:center;gap:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    "header p::before{content:'';width:8px;height:8px;border-radius:50%;background:#4ade80;flex:none}",
    "header p.waiting_agent::before{background:#fbbf24}header p.closed::before{background:#cbd5e1}",
    ".icon-btn{width:30px;height:30px;border-radius:8px;border:0;background:transparent;color:inherit;display:grid;place-items:center;flex:none;padding:0}",
    "header .icon-btn svg{width:18px;height:18px}",
    ".icon-btn:hover{background:rgba(255,255,255,.16)}",
    ".menu{position:absolute;top:56px;right:10px;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.12);padding:4px;z-index:2;min-width:180px}",
    ".menu button{display:block;width:100%;text-align:left;border:0;background:none;padding:9px 12px;border-radius:6px;color:var(--ink)}",
    ".menu button:hover{background:var(--soft)}",
    ".banner{background:#fff7e6;color:#7a4b00;font-size:13px;padding:8px 14px;border-bottom:1px solid #f5deb0}",
    ".banner.agent{background:#e8f7ee;color:#14532d;border-color:#bfe5cc}",
    ".body{flex:1;overflow-y:auto;background:var(--soft);padding:14px 12px;scroll-behavior:smooth}",
    ".msg{display:flex;flex-direction:column;margin:0 0 12px;max-width:86%}",
    ".msg.user{margin-left:auto;align-items:flex-end}",
    ".who{font-size:11px;color:var(--muted);margin:0 4px 2px}",
    ".bubble{padding:9px 12px;border-radius:14px;background:#fff;border:1px solid var(--line);word-wrap:break-word;overflow-wrap:anywhere}",
    ".msg.user .bubble{background:var(--blue);color:#fff;border-color:var(--blue);border-bottom-right-radius:4px}",
    ".msg.bot .bubble{border-bottom-left-radius:4px}",
    ".msg.staff .bubble{border-color:#86c79d;background:#f1fbf4;border-bottom-left-radius:4px}",
    ".bubble p{margin:0}.bubble .gap{height:8px}.bubble ul,.bubble ol{margin:4px 0;padding-left:20px}.bubble li{margin:2px 0}",
    ".bubble a{color:var(--blue);text-decoration:underline}.msg.user .bubble a{color:#fff}",
    ".bubble code{background:#eef2f8;border-radius:4px;padding:1px 4px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}",
    ".msg.user .bubble code{background:rgba(255,255,255,.2)}",
    ".code{margin:6px 0;border-radius:8px;overflow:hidden;background:#0f172a}",
    ".code-head{display:flex;justify-content:space-between;align-items:center;padding:4px 8px;background:#1e293b;color:#cbd5e1;font-size:11px}",
    ".code-head .copy{border:0;background:transparent;color:#93c5fd;font-size:11px}",
    ".code pre{margin:0;padding:10px;overflow-x:auto}",
    ".code pre code{background:none;color:#e2e8f0;padding:0;font-size:12.5px;white-space:pre}",
    ".bubble img{display:block;max-width:220px;max-height:220px;border-radius:8px;margin-bottom:4px;cursor:zoom-in}",
    ".meta{font-size:11px;color:var(--muted);margin:3px 4px 0}",
    ".notice{text-align:center;font-size:12px;color:var(--muted);margin:8px auto 12px;max-width:90%;background:#e9eef6;border-radius:12px;padding:5px 10px}",
    ".chips{display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 12px}",
    ".chip{border:1px solid var(--blue);color:var(--blue);background:#fff;border-radius:16px;padding:5px 11px;font-size:13px;text-align:left}",
    ".chip:hover{background:var(--blue);color:#fff}",
    ".typing{display:inline-flex;gap:4px;padding:12px 14px}",
    ".typing i{width:7px;height:7px;border-radius:50%;background:#9aa7ba;animation:blink 1.2s infinite both}",
    ".typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}",
    "@keyframes blink{0%,80%,100%{opacity:.3}40%{opacity:1}}",
    ".card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;margin:0 0 12px}",
    ".card h3{margin:0 0 8px;font-size:14px;color:var(--navy)}",
    ".card label{display:block;font-size:12px;color:var(--muted);margin:8px 0 3px}",
    ".card input[type=text],.card input[type=email],.card input[type=tel],.card textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:7px 9px;font:inherit;font-size:13px;color:var(--ink)}",
    ".card textarea{min-height:64px;resize:vertical}",
    ".card .consent{display:flex;gap:8px;align-items:flex-start;color:var(--ink);font-size:12px;margin-top:10px}",
    ".card .form-error{color:var(--danger);font-size:12px;margin-top:6px}",
    ".primary{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;margin-top:10px}",
    ".primary:hover{background:var(--blue-dark)}.primary:disabled{opacity:.6;cursor:default}",
    ".history-item{display:block;width:100%;text-align:left;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}",
    ".history-item:hover{border-color:var(--blue)}",
    ".history-item strong{display:block;font-size:13px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".history-item span{font-size:12px;color:var(--muted)}",
    ".history-head{display:flex;align-items:center;gap:6px;margin-bottom:10px;color:var(--navy);font-weight:700}",
    ".history-head .icon-btn{color:var(--navy)}.history-head .icon-btn:hover{background:#e3e9f3}",
    ".alert{margin:0 12px 6px;padding:7px 10px;border-radius:8px;background:#fdecec;color:var(--danger);font-size:12.5px}",
    ".preview{display:flex;align-items:center;gap:8px;margin:0 12px 6px;padding:6px;border:1px solid var(--line);border-radius:10px;background:#fff}",
    ".preview img{width:48px;height:48px;object-fit:cover;border-radius:6px}",
    ".preview span{flex:1;font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".preview .icon-btn{color:var(--muted)}.preview .icon-btn:hover{background:var(--soft)}",
    ".composer{display:flex;align-items:flex-end;gap:6px;padding:8px 10px;border-top:1px solid var(--line);background:#fff}",
    ".composer .icon-btn{color:var(--muted)}.composer .icon-btn:hover{background:var(--soft);color:var(--blue)}",
    ".composer textarea{flex:1;border:1px solid var(--line);border-radius:18px;padding:8px 12px;font:inherit;resize:none;max-height:110px;min-height:38px;color:var(--ink)}",
    ".composer textarea:focus-visible{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(11,89,200,.15)}",
    ".send{width:38px;height:38px;border-radius:50%;border:0;background:var(--blue);color:#fff;display:grid;place-items:center;flex:none}",
    ".send:disabled{background:#b6c6de;cursor:default}",
    ".foot{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:0 10px 8px;background:#fff;font-size:11px;color:var(--muted)}",
    ".agent-btn{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);background:#fff;color:var(--navy);border-radius:16px;padding:4px 10px;font-size:12px;font-weight:600}",
    ".agent-btn svg{width:15px;height:15px}.agent-btn:hover{border-color:var(--blue);color:var(--blue)}",
    ".agent-btn:disabled{opacity:.5;cursor:default}",
    ".sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}",
    "@media (max-width:480px){.root{bottom:14px}.root.right{right:14px}.root.left{left:14px}",
    ".panel{position:fixed;inset:0;width:100%;height:100%;border-radius:0}.msg{max-width:92%}",
    ".root.is-open .launcher{display:none}}",
  ].join("\n");

  var host = document.createElement("div");
  host.id = "python-master-chat";
  var shadow = host.attachShadow({ mode: "open" });
  var mark = CONFIG.apiBase + "/assets/sotatek-mark.png";

  shadow.innerHTML =
    "<style>" + CSS + "</style>" +
    '<div class="root ' + CONFIG.position + '">' +
    '<section class="panel" role="dialog" aria-modal="false" aria-label="' + escapeHtml(CONFIG.title) + '" hidden>' +
    "<header>" +
    '<div class="avatar"><img src="' + mark + '" alt=""></div>' +
    '<div class="titles"><h2>' + escapeHtml(CONFIG.title) + '</h2><p class="status bot">' + STATUS_TEXT.bot + "</p></div>" +
    '<button type="button" class="icon-btn" data-action="history" title="Lịch sử trò chuyện" aria-label="Lịch sử trò chuyện">' + icon("history") + "</button>" +
    '<button type="button" class="icon-btn" data-action="menu" title="Tải xuống" aria-label="Tải xuống cuộc trò chuyện" aria-haspopup="true">' + icon("download") + "</button>" +
    '<button type="button" class="icon-btn" data-action="new" title="Cuộc trò chuyện mới" aria-label="Cuộc trò chuyện mới">' + icon("plus") + "</button>" +
    '<button type="button" class="icon-btn" data-action="close" title="Thu nhỏ" aria-label="Đóng khung chat">' + icon("close") + "</button>" +
    "</header>" +
    '<div class="menu" role="menu" hidden>' +
    '<button type="button" role="menuitem" data-action="download-txt">Tải file văn bản (.txt)</button>' +
    '<button type="button" role="menuitem" data-action="download-pdf">Lưu thành PDF</button>' +
    "</div>" +
    '<div class="banner" role="status" hidden></div>' +
    '<div class="body" data-view="chat"><div class="messages" role="log" aria-live="polite" aria-label="Nội dung trò chuyện"></div></div>' +
    '<div class="body" data-view="history" hidden></div>' +
    '<div class="alert" role="alert" hidden></div>' +
    '<div class="preview" hidden><img alt="Ảnh sắp gửi"><span></span>' +
    '<button type="button" class="icon-btn" data-action="remove-image" title="Bỏ ảnh" aria-label="Bỏ ảnh đính kèm">' + icon("close") + "</button></div>" +
    '<form class="composer" autocomplete="off">' +
    '<button type="button" class="icon-btn" data-action="attach" title="Đính kèm ảnh (JPG, PNG, WEBP ≤ 5MB)" aria-label="Đính kèm ảnh">' + icon("clip") + "</button>" +
    '<input type="file" accept="image/jpeg,image/png,image/webp" hidden>' +
    '<label class="sr" for="pm-input">Nhập câu hỏi</label>' +
    '<textarea id="pm-input" rows="1" maxlength="' + MAX_MESSAGE_CHARS + '" placeholder="Nhập câu hỏi của bạn…"></textarea>' +
    '<button type="submit" class="send" title="Gửi" aria-label="Gửi tin nhắn">' + icon("send") + "</button>" +
    "</form>" +
    '<div class="foot"><button type="button" class="agent-btn" data-action="agent">' + icon("agent") + "Gặp tư vấn viên</button>" +
    "<span>AI có thể nhầm lẫn · thể lệ BTC là chuẩn</span></div>" +
    "</section>" +
    '<button type="button" class="launcher" aria-label="Mở trợ lý Python Master" aria-expanded="false">' +
    '<img src="' + mark + '" alt=""><span class="badge" hidden></span></button>' +
    "</div>";

  function $(selector) { return shadow.querySelector(selector); }

  var el = {
    root: $(".root"),
    panel: $(".panel"),
    launcher: $(".launcher"),
    badge: $(".badge"),
    status: $(".status"),
    menu: $(".menu"),
    banner: $(".banner"),
    chatView: $('[data-view="chat"]'),
    historyView: $('[data-view="history"]'),
    messages: $(".messages"),
    alert: $(".alert"),
    preview: $(".preview"),
    previewImg: $(".preview img"),
    previewName: $(".preview span"),
    form: $(".composer"),
    file: $('input[type="file"]'),
    input: $("textarea"),
    send: $(".send"),
    agentBtn: $(".agent-btn"),
  };

  // ---- dựng tin nhắn ----------------------------------------------------------
  function scrollToBottom() {
    el.chatView.scrollTop = el.chatView.scrollHeight;
  }

  function formatTime(value) {
    var date = value ? new Date(String(value).replace(" ", "T")) : new Date();
    if (isNaN(date.getTime())) date = new Date();
    return ("0" + date.getHours()).slice(-2) + ":" + ("0" + date.getMinutes()).slice(-2);
  }

  function absoluteUrl(url) {
    return /^https?:\/\//.test(url) ? url : CONFIG.apiBase + url;
  }

  /** role: user | bot | staff. options: {imageUrl, time, sources} */
  function addBubble(role, text, options) {
    options = options || {};
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var html = "";
    if (role === "staff") html += '<div class="who">Tư vấn viên</div>';
    html += '<div class="bubble">';
    if (options.imageUrl) html += '<img src="' + escapeHtml(absoluteUrl(options.imageUrl)) + '" alt="Ảnh đính kèm">';
    if (text) html += role === "user" ? renderTextBlock(text) : renderMarkdown(text);
    html += "</div>";
    var meta = formatTime(options.time);
    if (options.sources && options.sources.length) {
      var titles = options.sources.map(function (s) { return s.title; })
        .filter(function (t, i, all) { return t && all.indexOf(t) === i; });
      if (titles.length) meta += " · Nguồn: " + titles.map(escapeHtml).join(", ");
    }
    html += '<div class="meta">' + meta + "</div>";
    wrap.innerHTML = html;
    el.messages.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function addNotice(text) {
    var notice = document.createElement("div");
    notice.className = "notice";
    notice.textContent = text;
    el.messages.appendChild(notice);
    scrollToBottom();
  }

  function clearChips() {
    Array.prototype.forEach.call(el.messages.querySelectorAll(".chips"), function (node) { node.remove(); });
  }

  /** items: [{label, onClick}] */
  function addChips(items) {
    clearChips();
    if (!items.length) return;
    var box = document.createElement("div");
    box.className = "chips";
    items.forEach(function (item) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = item.label;
      chip.addEventListener("click", function () {
        if (state.busy) return;
        clearChips();
        item.onClick();
      });
      box.appendChild(chip);
    });
    el.messages.appendChild(box);
    scrollToBottom();
  }

  function faqLabel(faq) {
    var first = String(faq.cau_hoi_mau || "").split("\n").map(function (line) {
      return line.replace(/^\s*[-•]\s*/, "").trim();
    }).filter(Boolean)[0];
    return first || faq.intent;
  }

  function showFaqChips() {
    addChips(state.faqSuggestions.map(function (faq) {
      return { label: faqLabel(faq), onClick: function () { sendFaq(faq); } };
    }));
  }

  var typingNode = null;
  function setTyping(on) {
    if (on && !typingNode) {
      typingNode = document.createElement("div");
      typingNode.className = "msg bot";
      typingNode.innerHTML = '<div class="bubble typing" aria-label="Trợ lý đang trả lời"><i></i><i></i><i></i></div>';
      el.messages.appendChild(typingNode);
      scrollToBottom();
    } else if (!on && typingNode) {
      typingNode.remove();
      typingNode = null;
    }
  }

  var alertTimer = null;
  function showError(message) {
    el.alert.textContent = message;
    el.alert.hidden = false;
    clearTimeout(alertTimer);
    alertTimer = setTimeout(function () { el.alert.hidden = true; }, 7000);
  }

  function setBusy(busy) {
    state.busy = busy;
    el.send.disabled = busy;
    setTyping(busy);
  }

  /** Dựng 1 bản ghi tin nhắn lấy từ server (khôi phục phiên / polling). */
  function renderRecord(record, isFirst) {
    var content = record.content || "";
    var options = { time: record.created_at, imageUrl: record.image_url };
    if (record.sender_type === "user") {
      addBubble("user", content.replace(/^\[Chọn FAQ\]\s*/, ""), options);
    } else if (record.sender_type === "staff") {
      addBubble("staff", content, options);
    } else if (record.sender_type === "system" && !isFirst && record.answer_status !== "blocked") {
      addNotice(content);
    } else {
      addBubble("bot", content, options); // model/bot, lời chào đầu phiên, câu từ chối
    }
  }

  // ---- trạng thái phiên & polling tin tư vấn viên ---------------------------------------
  function setStatus(status) {
    state.status = status || "bot";
    el.status.textContent = STATUS_TEXT[state.status] || STATUS_TEXT.bot;
    el.status.className = "status " + state.status;
    var withAgent = state.status === "waiting_agent" || state.status === "agent";
    el.agentBtn.disabled = withAgent;
    el.banner.hidden = !withAgent;
    el.banner.className = "banner" + (state.status === "agent" ? " agent" : "");
    el.banner.textContent = state.status === "agent"
      ? "Bạn đang trò chuyện với tư vấn viên. Trợ lý AI tạm dừng trả lời."
      : "Đã chuyển tới tư vấn viên. Bạn có thể nhắn thêm thông tin trong lúc chờ.";
    if (withAgent) startPolling(); else stopPolling();
  }

  function startPolling() {
    if (state.pollTimer || !state.userToken) return;
    state.pollTimer = setInterval(poll, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function poll() {
    if (!state.conversationId || !state.userToken) return;
    api("/api/chat/conversations/" + state.conversationId + "/messages?user_id=" + state.userId +
        "&after_id=" + state.lastMessageId)
      .then(function (data) {
        data.messages.forEach(function (record) {
          if (record.id <= state.lastMessageId) return;
          state.lastMessageId = record.id;
          renderRecord(record, false);
          if (!state.open && record.sender_type === "staff") setUnread(state.unread + 1);
        });
        if (data.conversation.status !== state.status) setStatus(data.conversation.status);
      })
      .catch(function () { /* mạng chập chờn: lần sau hỏi lại */ });
  }

  function setUnread(count) {
    state.unread = count;
    el.badge.hidden = count === 0;
    el.badge.textContent = count > 9 ? "9+" : String(count);
  }

  // ---------------------------------------------------------------------------
  // 6. Nghiệp vụ
  // ---------------------------------------------------------------------------
  function startConversation() {
    setBusy(true);
    return api("/api/chat/init", { method: "POST", json: { site_key: CONFIG.siteKey, user_id: state.userId } })
      .then(function (data) {
        state.lastMessageId = 0;
        state.faqSuggestions = data.faq_suggestions || [];
        absorb(data);
        el.messages.innerHTML = "";
        setBusy(false);
        setStatus("bot");
        addBubble("bot", data.greeting);
        showFaqChips();
      })
      .catch(function (error) {
        setBusy(false);
        showError(error.message);
      });
  }

  function restoreConversation() {
    return api("/api/chat/conversations/" + state.conversationId + "/messages?user_id=" + state.userId)
      .then(function (data) {
        el.messages.innerHTML = "";
        state.lastMessageId = 0;
        data.messages.forEach(function (record, index) {
          renderRecord(record, index === 0);
          state.lastMessageId = Math.max(state.lastMessageId, record.id);
        });
        setStatus(data.conversation.status);
        var asked = data.messages.some(function (m) { return m.sender_type === "user"; });
        if (!asked && data.conversation.status === "bot") showFaqChips();
      });
  }

  function ensureStarted() {
    if (!state.ready) {
      state.ready = state.conversationId && state.userToken
        ? restoreConversation().catch(startConversation)
        : startConversation();
    }
    return state.ready;
  }

  function handleReply(data) {
    absorb(data);
    if (data.handled_by === "agent") {
      // Chỉ nhắc 1 lần, không lặp lại sau mỗi tin nhắn gửi cho tư vấn viên.
      if (state.lastAgentNotice !== data.reply) addNotice(data.reply);
      state.lastAgentNotice = data.reply;
      setStatus(data.conversation_status || state.status);
      return;
    }
    var clarify = data.suggestions && data.suggestions.length;
    addBubble("bot", clarify ? data.clarification_question : data.reply, { sources: data.sources });
    if (clarify) {
      addChips(data.suggestions.map(function (option) {
        return { label: option, onClick: function () { sendText(option); } };
      }));
    }
    if (data.after_hours) {
      showTicketForm();
    } else if (data.escalated) {
      setStatus("waiting_agent");
    } else if (state.status === "closed") {
      setStatus("bot"); // server mở lại phiên đã đóng khi thí sinh nhắn tiếp
    }
  }

  function sendText(text) {
    text = (text || "").trim();
    var image = state.pendingImage;
    if ((!text && !image) || state.busy) return;
    if (text.length > MAX_MESSAGE_CHARS) { showError(MSG.tooLong); return; }

    clearChips();
    state.lastUserText = text || state.lastUserText;
    addBubble("user", text, { imageUrl: image ? image.previewUrl : null });
    el.input.value = "";
    autoGrow();
    clearPendingImage(false);
    setBusy(true);

    var request;
    if (image) {
      var form = new FormData();
      form.append("site_key", CONFIG.siteKey);
      if (state.userId) form.append("user_id", state.userId);
      if (state.conversationId) form.append("conversation_id", state.conversationId);
      form.append("message", text);
      form.append("image", image.file, image.file.name);
      request = api("/api/chat/image", { method: "POST", form: form });
    } else {
      request = api("/api/chat", {
        method: "POST",
        json: { site_key: CONFIG.siteKey, user_id: state.userId, conversation_id: state.conversationId, message: text },
      });
    }
    request
      .then(function (data) { setBusy(false); handleReply(data); })
      .catch(function (error) { setBusy(false); showError(error.message); })
      .then(function () { el.input.focus(); });
  }

  function sendFaq(faq) {
    addBubble("user", faqLabel(faq));
    setBusy(true);
    api("/api/chat/faq/" + faq.id, {
      method: "POST",
      json: { site_key: CONFIG.siteKey, user_id: state.userId, conversation_id: state.conversationId },
    })
      .then(function (data) { setBusy(false); handleReply(data); })
      .catch(function (error) { setBusy(false); showError(error.message); });
  }

  function requestAgent() {
    if (state.busy || !state.conversationId) return;
    el.agentBtn.disabled = true;
    api("/api/chat/request-agent", {
      method: "POST",
      json: { site_key: CONFIG.siteKey, user_id: state.userId, conversation_id: state.conversationId },
    })
      .then(function (data) {
        absorb(data);
        addNotice(data.message);
        setStatus(data.conversation_status);
        if (data.after_hours) showTicketForm();
      })
      .catch(function (error) {
        el.agentBtn.disabled = false;
        showError(error.message);
      });
  }

  function showTicketForm() {
    if (el.messages.querySelector(".ticket")) return;
    var card = document.createElement("form");
    card.className = "card ticket";
    card.noValidate = true;
    card.innerHTML =
      "<h3>Để lại thông tin để tư vấn viên liên hệ</h3>" +
      '<label for="pm-t-name">Họ và tên *</label><input id="pm-t-name" name="full_name" type="text" maxlength="100" required>' +
      '<label for="pm-t-email">Email</label><input id="pm-t-email" name="email" type="email" maxlength="255">' +
      '<label for="pm-t-phone">Số điện thoại</label><input id="pm-t-phone" name="phone" type="tel" maxlength="15" placeholder="0912345678">' +
      '<label for="pm-t-content">Nội dung cần hỗ trợ *</label><textarea id="pm-t-content" name="content" maxlength="2000" required></textarea>' +
      '<label class="consent"><input type="checkbox" name="consent"> <span>Tôi đồng ý để Ban tổ chức lưu họ tên, email/số điện thoại này để liên hệ hỗ trợ.</span></label>' +
      '<div class="form-error" role="alert" hidden></div>' +
      '<button type="submit" class="primary">Gửi thông tin</button>';
    card.elements.content.value = state.lastUserText;
    card.addEventListener("submit", function (event) {
      event.preventDefault();
      submitTicket(card);
    });
    card.addEventListener("input", function () { card.querySelector(".form-error").hidden = true; });
    el.messages.appendChild(card);
    scrollToBottom();
  }

  function submitTicket(card) {
    var errorBox = card.querySelector(".form-error");
    var button = card.querySelector("button[type=submit]");
    var fields = card.elements;
    var payload = {
      site_key: CONFIG.siteKey,
      user_id: state.userId,
      conversation_id: state.conversationId,
      full_name: fields.full_name.value.trim(),
      email: fields.email.value.trim(),
      phone: fields.phone.value.trim(),
      content: fields.content.value.trim(),
      consent: fields.consent.checked,
    };
    var problem = !payload.full_name ? "Vui lòng nhập họ tên."
      : !payload.email && !payload.phone ? "Vui lòng nhập email hoặc số điện thoại."
      : !payload.content ? "Vui lòng nhập nội dung cần hỗ trợ."
      : !payload.consent ? "Vui lòng tích đồng ý để chúng tôi lưu thông tin liên hệ." : "";
    if (problem) {
      errorBox.textContent = problem;
      errorBox.hidden = false;
      return;
    }
    errorBox.hidden = true;
    button.disabled = true;
    api("/api/chat/tickets", { method: "POST", json: payload })
      .then(function (data) {
        absorb(data);
        card.remove();
        addNotice(data.message + " (Mã yêu cầu #" + data.ticket_id + ")");
      })
      .catch(function (error) {
        button.disabled = false;
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      });
  }

  // ---- ảnh đính kèm -----------------------------------------------------------------
  function pickImage(file) {
    if (!file) return;
    if (ALLOWED_IMAGE_TYPES.indexOf(file.type) === -1) { showError(MSG.badType); return; }
    if (file.size > MAX_IMAGE_BYTES) { showError(MSG.tooLarge); return; }
    clearPendingImage(true);
    state.pendingImage = { file: file, previewUrl: URL.createObjectURL(file) };
    el.previewImg.src = state.pendingImage.previewUrl;
    el.previewName.textContent = (file.name || "ảnh dán") + " · " + (file.size / 1024 / 1024).toFixed(2) + " MB";
    el.preview.hidden = false;
    el.input.focus();
  }

  /** revoke=false khi ảnh vừa được gửi: object URL vẫn đang hiển thị trong bong bóng chat. */
  function clearPendingImage(revoke) {
    if (state.pendingImage && revoke) URL.revokeObjectURL(state.pendingImage.previewUrl);
    state.pendingImage = null;
    el.preview.hidden = true;
    el.previewImg.removeAttribute("src");
    el.file.value = "";
  }

  // ---- lịch sử & tải xuống ---------------------------------------------------------
  function showView(view) {
    el.chatView.hidden = view !== "chat";
    el.historyView.hidden = view !== "history";
  }

  function openHistory() {
    if (!state.userToken) { showError(MSG.noToken); return; }
    showView("history");
    el.historyView.innerHTML = '<div class="history-head"><button type="button" class="icon-btn" data-action="back" aria-label="Quay lại">' +
      icon("back") + "</button>Lịch sử trò chuyện</div><p>Đang tải…</p>";
    api("/api/history/" + state.userId + "/conversations")
      .then(function (data) {
        var list = el.historyView.querySelector("p");
        if (!data.conversations.length) { list.textContent = "Chưa có cuộc trò chuyện nào."; return; }
        list.remove();
        data.conversations.forEach(function (conversation) {
          var item = document.createElement("button");
          item.type = "button";
          item.className = "history-item";
          var current = conversation.id === state.conversationId ? " · đang mở" : "";
          item.innerHTML = "<strong>" + escapeHtml(conversation.title) + "</strong><span>#" + conversation.id + " · " +
            escapeHtml(conversation.created_at) + current + "</span>";
          item.addEventListener("click", function () { openConversation(conversation.id); });
          el.historyView.appendChild(item);
        });
      })
      .catch(function (error) { showError(error.message); showView("chat"); });
  }

  function openConversation(conversationId) {
    stopPolling();
    state.conversationId = conversationId;
    saveSession();
    showView("chat");
    restoreConversation().catch(function (error) { showError(error.message); });
  }

  function downloadTxt() {
    if (!state.userToken) { showError(MSG.noToken); return; }
    api("/api/history/" + state.userId + "/export?conversation_id=" + state.conversationId, { raw: true })
      .then(function (response) { return response.blob(); })
      .then(function (blob) {
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = "lich_su_chat_" + state.conversationId + ".txt";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
      })
      .catch(function (error) { showError(error.message); });
  }

  /** PDF: mở bản in của cuộc trò chuyện, người dùng chọn "Lưu thành PDF" trong hộp thoại in. */
  function downloadPdf() {
    if (!state.userToken) { showError(MSG.noToken); return; }
    var win = window.open("", "_blank"); // mở ngay trong sự kiện click để không bị chặn popup
    if (!win) { showError("Trình duyệt đã chặn cửa sổ mới, hãy cho phép popup để lưu PDF."); return; }
    win.document.write("<p style='font-family:sans-serif'>Đang chuẩn bị bản in…</p>");
    api("/api/chat/conversations/" + state.conversationId + "/messages?user_id=" + state.userId)
      .then(function (data) {
        var labels = { user: "Bạn", model: "Trợ lý AI", bot: "Trợ lý AI", staff: "Tư vấn viên", system: "Hệ thống" };
        var rows = data.messages.map(function (m) {
          var image = m.image_url ? '<p><img src="' + escapeHtml(absoluteUrl(m.image_url)) + '" style="max-width:260px"></p>' : "";
          return '<div class="m ' + m.sender_type + '"><div class="h">' + labels[m.sender_type] + " · " +
            escapeHtml(m.created_at) + "</div>" + image + renderMarkdown(m.content) + "</div>";
        }).join("");
        win.document.open();
        win.document.write(
          '<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>Lịch sử chat #' + state.conversationId + "</title>" +
          "<style>body{font-family:Arial,sans-serif;color:#16202e;max-width:760px;margin:24px auto;font-size:13px}" +
          "h1{font-size:18px;color:#032259}.m{border-left:3px solid #cbd5e1;padding:6px 10px;margin:10px 0}" +
          ".m.user{border-color:#0b59c8}.m.staff{border-color:#16a34a}.h{font-size:11px;color:#5b6678;font-weight:bold}" +
          "p{margin:2px 0}pre{background:#f1f5f9;padding:8px;white-space:pre-wrap}.code-head{display:none}</style></head><body>" +
          "<h1>Lịch sử tư vấn · Trợ lý AI Python Master 2026</h1><p>Phiên #" + state.conversationId + " · " +
          escapeHtml(data.conversation.title) + "</p>" + rows + "</body></html>");
        win.document.close();
        win.focus();
        setTimeout(function () { win.print(); }, 300);
      })
      .catch(function (error) { win.close(); showError(error.message); });
  }

  // ---------------------------------------------------------------------------
  // 7. Sự kiện
  // ---------------------------------------------------------------------------
  function openPanel() {
    state.open = true;
    el.panel.hidden = false;
    el.root.classList.add("is-open");
    el.launcher.setAttribute("aria-expanded", "true");
    el.launcher.setAttribute("aria-label", "Đóng trợ lý Python Master");
    setUnread(0);
    ensureStarted();
    scrollToBottom();
    setTimeout(function () { el.input.focus(); }, 50);
  }

  function closePanel() {
    state.open = false;
    el.panel.hidden = true;
    el.root.classList.remove("is-open");
    el.menu.hidden = true;
    el.launcher.setAttribute("aria-expanded", "false");
    el.launcher.setAttribute("aria-label", "Mở trợ lý Python Master");
    el.launcher.focus();
  }

  function autoGrow() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 110) + "px";
  }

  var actions = {
    close: closePanel,
    history: openHistory,
    back: function () { showView("chat"); },
    new: function () { stopPolling(); showView("chat"); state.ready = startConversation(); },
    menu: function () { el.menu.hidden = !el.menu.hidden; },
    "download-txt": function () { el.menu.hidden = true; downloadTxt(); },
    "download-pdf": function () { el.menu.hidden = true; downloadPdf(); },
    attach: function () { el.file.click(); },
    "remove-image": function () { clearPendingImage(true); },
    agent: requestAgent,
  };

  el.launcher.addEventListener("click", function () { state.open ? closePanel() : openPanel(); });

  el.panel.addEventListener("click", function (event) {
    var copy = event.target.closest(".copy");
    if (copy) {
      var code = copy.closest(".code").querySelector("code").textContent;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(code).then(function () {
          copy.textContent = "Đã chép";
          setTimeout(function () { copy.textContent = "Sao chép"; }, 1500);
        });
      }
      return;
    }
    var image = event.target.closest(".bubble img");
    if (image) { window.open(image.src, "_blank", "noopener"); return; }
    var trigger = event.target.closest("[data-action]");
    if (trigger && actions[trigger.getAttribute("data-action")]) {
      actions[trigger.getAttribute("data-action")]();
    }
    if (!event.target.closest('[data-action="menu"]') && !event.target.closest(".menu")) el.menu.hidden = true;
  });

  el.panel.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closePanel();
  });

  el.form.addEventListener("submit", function (event) {
    event.preventDefault();
    sendText(el.input.value);
  });

  el.input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendText(el.input.value);
    }
  });
  el.input.addEventListener("input", autoGrow);
  el.input.addEventListener("paste", function (event) {
    var items = (event.clipboardData && event.clipboardData.files) || [];
    if (items.length && items[0].type.indexOf("image/") === 0) {
      event.preventDefault();
      pickImage(items[0]);
    }
  });
  el.file.addEventListener("change", function () { pickImage(el.file.files[0]); });

  // ---------------------------------------------------------------------------
  // 8. Khởi động
  // ---------------------------------------------------------------------------
  loadSession();
  (document.body || document.documentElement).appendChild(host);

  // Có phiên cũ -> khôi phục ngay (khung chat vẫn đóng) để nếu phiên đang chờ/đang
  // gặp tư vấn viên thì vẫn nhận tin mới và hiện số tin chưa đọc trên nút chat.
  if (state.conversationId && state.userToken) ensureStarted();

  window.PythonMasterChat = {
    open: openPanel,
    close: closePanel,
    toggle: function () { state.open ? closePanel() : openPanel(); },
    newChat: function () { openPanel(); actions.new(); },
    /** Gửi 1 câu hỏi như người dùng gõ (dùng cho trang demo/kiểm thử). */
    send: function (text) {
      openPanel();
      return state.ready.then(function () { sendText(text); });
    },
  };

  if (CONFIG.autoOpen) openPanel();
})();
