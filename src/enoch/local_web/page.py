SHOP_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Enoch shop</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: #fff;
      --ink: #1d1a16;
      --muted: #6b645b;
      --line: #ddd6cb;
      --accent: #0f6b5c;
      --accent-ink: #fff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 16px/1.45 ui-sans-serif, system-ui, sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      padding: 16px 20px 8px;
    }
    h1 { font-size: 1.15rem; margin: 0; }
    .need { color: var(--muted); margin: 4px 0 0; font-size: 0.92rem; }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
      gap: 16px;
      padding: 0 20px 20px;
      min-height: calc(100vh - 72px);
      align-items: stretch;
    }
    @media (max-width: 860px) {
      .layout { grid-template-columns: 1fr; }
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .layout > .panel:first-of-type {
      min-height: calc(100vh - 96px);
    }
    .tabs {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 12px 12px 0;
    }
    .tab {
      flex: 0 0 auto;
      padding: 8px 14px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #faf8f4;
      color: var(--ink);
      cursor: pointer;
      white-space: nowrap;
      font: inherit;
    }
    .tab[aria-selected="true"] {
      background: var(--accent);
      color: var(--accent-ink);
      border-color: var(--accent);
    }
    .product {
      flex: 1;
      min-height: 0;
      overflow: auto;
      padding: 16px 18px 20px;
    }
    .product-message {
      max-width: 28rem;
      background: #f6f3ee;
      border-radius: 16px;
      overflow: hidden;
    }
    .preview img {
      width: 100%;
      max-height: 280px;
      object-fit: cover;
      display: block;
      background: #ece7df;
    }
    .product-copy {
      padding: 12px 14px 14px;
    }
    .product-copy p { margin: 0 0 0.55em; }
    .product-copy p:last-child { margin-bottom: 0; }
    .product-copy ul { margin: 0 0 0.55em; padding-left: 1.2em; }
    .product-copy a { color: var(--accent); }
    .meta, .empty, .notes { color: var(--muted); }
    .chat-log {
      flex: 1;
      overflow: auto;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .bubble { padding: 8px 10px; border-radius: 12px; max-width: 92%; }
    .bubble.plain { white-space: pre-wrap; }
    .you { align-self: flex-end; background: #e7f3f0; }
    .enoch { align-self: flex-start; background: #f6f3ee; }
    .enoch p { margin: 0 0 0.5em; }
    .enoch p:last-child { margin-bottom: 0; }
    .enoch ul { margin: 0 0 0.5em; padding-left: 1.2em; }
    .enoch a { color: var(--accent); }
    .pending { color: var(--muted); font-style: italic; }
    .turn { display: flex; flex-direction: column; gap: 8px; }
    form {
      display: flex;
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    input[type="text"] {
      flex: 1;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
    }
    #composer button {
      background: var(--accent);
      color: var(--accent-ink);
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      font: inherit;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <header>
    <h1>Enoch shop</h1>
    <p class="need" id="need">Loading shortlist…</p>
  </header>
  <div class="layout">
    <section class="panel">
      <div class="tabs" id="tabs"></div>
      <div class="product" id="product"></div>
    </section>
    <section class="panel">
      <form id="composer">
        <input id="message" type="text" maxlength="4000" placeholder="Ask Enoch, or /do another search" autocomplete="off">
        <button type="submit">Send</button>
      </form>
      <div class="chat-log" id="log"></div>
    </section>
  </div>
  <script>
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || sessionStorage.getItem("enochLocalWebToken") || "";
    if (params.get("token")) sessionStorage.setItem("enochLocalWebToken", token);
    const parts = location.pathname.split("/").filter(Boolean);
    let shortlistId = parts[1] || "";
    let focusTab = Number(parts[2] || 0);
    let current = null;
    let renderedKey = "";
    let tabsKey = "";
    let painted = [];

    function headers() {
      const value = { "Accept": "application/json" };
      if (token) value.Authorization = "Bearer " + token;
      return value;
    }

    function shortlistNumber(id) {
      const match = String(id || "").match(/^t(\d+)$/);
      return match ? Number(match[1]) : 0;
    }

    async function loadShortlist() {
      const latestResponse = await fetch("/api/shop", {
        headers: headers(),
        cache: "no-store",
      });
      if (latestResponse.status === 401) {
        document.getElementById("need").textContent =
          "Open the Telegram link, or add ?token= from .enoch/local_web.json";
        return;
      }
      if (latestResponse.ok) {
        const latest = await latestResponse.json();
        if (latest && latest.id && shortlistNumber(latest.id) >= shortlistNumber(shortlistId)) {
          if (latest.id !== shortlistId) {
            shortlistId = latest.id;
            focusTab = 1;
            tabsKey = "";
            renderedKey = "";
            history.replaceState({}, "", "/shop/" + latest.id + "/1" + location.search);
          }
          current = latest;
          if (!focusTab) focusTab = 1;
          renderShortlist();
          return;
        }
      }
      if (!shortlistId) {
        document.getElementById("need").textContent = "No shop shortlist yet. Run a /do product search.";
        return;
      }
      const response = await fetch("/api/shop/" + shortlistId, {
        headers: headers(),
        cache: "no-store",
      });
      if (!response.ok) {
        document.getElementById("need").textContent = "No shop shortlist yet. Run a /do product search.";
        return;
      }
      current = await response.json();
      if (!focusTab) focusTab = 1;
      renderShortlist();
    }

    function thumbUrl(product) {
      const id = (current && current.id) || shortlistId;
      if (!id || !product) return "";
      const query = token ? ("?token=" + encodeURIComponent(token)) : "";
      return "/api/shop/" + id + "/thumb/" + product.index + query;
    }

    function brandName(product) {
      return (product.brand || product.store || product.name || ("Option " + product.index)).trim();
    }

    function appendInline(node, text) {
      const pattern = /(\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:[^)\s]+)\)|(https?:\/\/[^\s<]+))/g;
      let last = 0;
      let match;
      while ((match = pattern.exec(text))) {
        if (match.index > last) {
          node.appendChild(document.createTextNode(text.slice(last, match.index)));
        }
        if (match[2]) {
          const strong = document.createElement("strong");
          strong.textContent = match[2];
          node.appendChild(strong);
        } else if (match[3] && match[4]) {
          const link = document.createElement("a");
          link.href = match[4];
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent = match[3];
          node.appendChild(link);
        } else if (match[5]) {
          const link = document.createElement("a");
          link.href = match[5];
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent = match[5];
          node.appendChild(link);
        }
        last = match.index + match[0].length;
      }
      if (last < text.length) {
        node.appendChild(document.createTextNode(text.slice(last)));
      }
    }

    function appendMarkdown(target, text) {
      let list = null;
      String(text || "").split(/\n/).forEach((line) => {
        const bullet = line.match(/^\s*[-*]\s+(.*)$/);
        if (bullet) {
          if (!list) {
            list = document.createElement("ul");
            target.appendChild(list);
          }
          const item = document.createElement("li");
          appendInline(item, bullet[1]);
          list.appendChild(item);
          return;
        }
        list = null;
        if (!line.trim() || /^<!--.*-->$/.test(line.trim())) return;
        const paragraph = document.createElement("p");
        appendInline(paragraph, line);
        target.appendChild(paragraph);
      });
    }

    function productCardText(product) {
      const brand = brandName(product);
      const lines = ["**" + (product.name || brand) + "**"];
      if (product.price) lines[0] += " — " + product.price;
      if (brand) lines.push("Store: " + brand);
      if (product.variant) lines.push("Variant: " + product.variant);
      if (product.detail) lines.push(product.detail);
      if (product.url) {
        lines.push("[go to " + brand + " product page](" + product.url + ")");
      }
      return lines.join("\n");
    }

    function renderShortlist() {
      document.getElementById("need").textContent = current.title || "Shop shortlist";
      const products = current.products || [];
      const identity = products.map((item) => item.index + ":" + brandName(item) + ":" + item.url).join("|");
      const tabs = document.getElementById("tabs");
      if (identity !== tabsKey) {
        tabsKey = identity;
        tabs.replaceChildren();
        products.forEach((product) => {
          const button = document.createElement("button");
          button.className = "tab";
          button.type = "button";
          button.dataset.index = String(product.index);
          button.textContent = brandName(product);
          button.title = product.name || brandName(product);
          button.setAttribute("aria-label", brandName(product));
          button.addEventListener("click", () => {
            focusTab = product.index;
            history.replaceState({}, "", "/shop/" + current.id + "/" + focusTab + location.search);
            renderShortlist();
          });
          tabs.appendChild(button);
        });
      }
      Array.from(tabs.querySelectorAll(".tab")).forEach((button) => {
        button.setAttribute("aria-selected", String(Number(button.dataset.index) === focusTab));
      });
      const product = products.find((item) => item.index === focusTab) || products[0];
      const panel = document.getElementById("product");
      if (!product) {
        renderedKey = "";
        panel.innerHTML = '<p class="empty">No products in this shortlist.</p>';
        return;
      }
      if (!focusTab) focusTab = product.index;
      const key = (current.id || "") + ":" + product.index + ":" + product.url;
      if (key === renderedKey) return;
      renderedKey = key;
      panel.replaceChildren();
      const card = document.createElement("div");
      card.className = "product-message";
      const preview = document.createElement("div");
      preview.className = "preview";
      const img = document.createElement("img");
      img.alt = product.name || brandName(product);
      img.src = thumbUrl(product);
      img.addEventListener("error", () => preview.remove());
      preview.appendChild(img);
      const copy = document.createElement("div");
      copy.className = "product-copy";
      appendMarkdown(copy, productCardText(product));
      card.appendChild(preview);
      card.appendChild(copy);
      panel.appendChild(card);
    }

    function renderChat(turns) {
      const log = document.getElementById("log");
      log.replaceChildren();
      (turns || []).forEach((turn) => {
        const block = document.createElement("div");
        block.className = "turn";
        if (turn.message) {
          const you = document.createElement("div");
          you.className = "bubble you plain";
          you.textContent = turn.message;
          block.appendChild(you);
        }
        if (turn.reply) {
          const enoch = document.createElement("div");
          enoch.className = "bubble enoch" + (turn.pending ? " pending plain" : "");
          if (turn.pending) enoch.textContent = turn.reply;
          else appendMarkdown(enoch, turn.reply);
          block.appendChild(enoch);
        }
        log.appendChild(block);
      });
      log.scrollTop = 0;
    }

    async function loadChat() {
      const response = await fetch("/api/conversation?ts=" + Date.now(), {
        headers: headers(),
        cache: "no-store",
      });
      if (!response.ok) return;
      const data = await response.json();
      const server = data.turns || [];
      const extras = painted.filter((item) => {
        if (!(item.pending || item.local)) return false;
        return !server.some((turn) => turn.message === item.message);
      });
      painted = extras.concat(server);
      renderChat(painted);
    }

    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function waitForTurn(message) {
      const started = Date.now();
      while (Date.now() - started < 180000) {
        await loadChat();
        const found = painted.find((turn) => (
          turn.message === message && turn.reply && !turn.pending
        ));
        if (found) return;
        await sleep(500);
      }
      const pendingTurn = painted.find((turn) => turn.message === message && turn.pending);
      if (pendingTurn) {
        pendingTurn.pending = false;
        pendingTurn.reply = "Still waiting for Enoch. If Telegram already has the answer, it should show up here on the next refresh.";
        renderChat(painted);
      }
    }

    document.getElementById("composer").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = document.getElementById("message");
      const button = event.target.querySelector("button");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      const pending = { message: text, reply: "Enoch is answering…", pending: true, local: true };
      painted = [pending].concat(painted.filter((item) => !item.pending));
      renderChat(painted);
      if (button) button.disabled = true;
      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { ...headers(), "Content-Type": "application/json" },
          body: JSON.stringify({
            text,
            shortlist_id: (current && current.id) || shortlistId || "",
            tab: focusTab || null,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (data.reply) {
          pending.pending = false;
          pending.reply = data.reply;
          renderChat(painted);
        } else if (!response.ok) {
          pending.pending = false;
          pending.reply = data.error || "Enoch could not answer on this page.";
          renderChat(painted);
        }
        await waitForTurn(text);
      } catch (error) {
        pending.pending = false;
        pending.reply = "Could not reach the local shop page.";
        renderChat(painted);
      } finally {
        if (button) button.disabled = false;
      }
    });

    loadShortlist();
    loadChat();
    setInterval(() => { loadShortlist(); loadChat(); }, 4000);
  </script>
</body>
</html>
"""
