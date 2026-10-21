/* Market Pro · 交易端前端 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    token: localStorage.getItem("mp_token") || "",
    uid: localStorage.getItem("mp_uid") || "",
    symbol: localStorage.getItem("mp_symbol") || "",
    side: "LONG",
    spotSide: "buy",
    leverage: 20,
    margin: 100,
    klineLimit: 60,
    kline: [],
    symbols: [],
    config: { max_leverage: 125, fee_rate: 0.0005, mmr: 0.005, color_style: "intl" },
    colors: { up: "#0ecb81", down: "#f6465d" },
    account: null,
    timers: [],
    hover: null,
  };

  /* ---------------- 工具 ---------------- */
  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    const v = Number(n);
    const digits = d === undefined ? Math.abs(v) >= 100 ? 2 : Math.abs(v) >= 1 ? 3 : 5 : d;
    return v.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }
  function price(n) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    const v = Number(n);
    if (v >= 1000) return fmt(v, 2);
    if (v >= 10) return fmt(v, 3);
    if (v >= 1) return fmt(v, 4);
    return fmt(v, 6);
  }
  function money(n) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    return fmt(Number(n), 2);
  }
  function signed(n, d) {
    const v = Number(n);
    const s = v > 0 ? "+" : "";
    return s + fmt(v, d === undefined ? 2 : d);
  }
  function cls(n) {
    const v = Number(n);
    if (v > 0) return "up";
    if (v < 0) return "down";
    return "muted";
  }

  let toastTimer = null;
  function toast(msg, kind) {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast " + (kind || "ok");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (state.token) headers["X-Token"] = state.token;
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const detail = (data && (data.detail || data.error)) || "请求失败(" + res.status + ")";
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ---------------- 免责声明（强制阅读） ---------------- */
  function bindAgreement() {
    const box = $("legalBox");
    const chk = $("agreeChk");
    const btn = $("loginBtn");
    const row = $("agreeRow");
    if (!box || !chk || !btn) return;

    const agreedBefore = localStorage.getItem("mp_agreed") === "1";

    const sync = () => {
      btn.disabled = !chk.checked;
      row.classList.toggle("ready", chk.checked);
    };

    const unlock = () => {
      chk.disabled = false;
      row.classList.add("ready");
    };

    const checkBottom = () => {
      const reached = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
      // 内容不足一屏时直接算读完
      if (reached || box.scrollHeight <= box.clientHeight + 4) unlock();
    };

    if (agreedBefore) {
      unlock();
      chk.checked = true;
      sync();
    } else {
      chk.disabled = true;
      chk.checked = false;
      sync();
      box.addEventListener("scroll", checkBottom);
      // 内容太少无需滚动的情况
      setTimeout(checkBottom, 60);
    }

    chk.addEventListener("change", () => {
      if (chk.checked) localStorage.setItem("mp_agreed", "1");
      sync();
    });
    sync();
  }

  /* ---------------- 登录 ---------------- */
  async function doLogin() {
    const uid = $("loginUser").value.trim();
    const pass = $("loginPass").value;
    $("loginErr").textContent = "";
    if (!uid || !pass) {
      $("loginErr").textContent = "请输入账号和密码";
      return;
    }
    $("loginBtn").disabled = true;
    try {
      const data = await api("/api/login", { method: "POST", body: { user_id: uid, password: pass } });
      state.token = data.token;
      state.uid = data.user_id;
      localStorage.setItem("mp_token", state.token);
      localStorage.setItem("mp_uid", state.uid);
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      boot();
    } catch (e) {
      $("loginErr").textContent = e.message;
    } finally {
      $("loginBtn").disabled = false;
    }
  }

  function logout() {
    api("/api/logout", { method: "POST" }).catch(() => {});
    localStorage.removeItem("mp_token");
    localStorage.removeItem("mp_uid");
    state.token = "";
    state.uid = "";
    stopAll();
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
  }

  function showApp() {
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("uidLabel") && ($("uidLabel").textContent = state.uid);
    boot();
  }

  /* ---------------- 启动 ---------------- */
  function stopAll() {
    state.timers.forEach((t) => clearInterval(t));
    state.timers = [];
  }

  async function boot() {
    stopAll();
    try {
      state.config = await api("/api/config");
    } catch (e) {
      /* 忽略 */
    }
    if (state.config.color_style === "cn") {
      state.colors = { up: "#f6465d", down: "#0ecb81" };
    } else {
      state.colors = { up: "#0ecb81", down: "#f6465d" };
    }
    $("levRange").max = state.config.max_leverage || 125;
    $("leverHint").textContent =
      "逐仓 · 维持保证金率 " + ((state.config.mmr || 0.005) * 100).toFixed(2) + "%";
    await loadSymbols(true);
    await Promise.all([loadKline(), loadAccount(), loadDepth(), loadTrades()]);
    state.timers.push(setInterval(loadSymbols, 1500));
    state.timers.push(setInterval(loadKline, 2000));
    state.timers.push(setInterval(loadAccount, 1800));
    state.timers.push(setInterval(loadDepth, 2000));
    state.timers.push(setInterval(loadTrades, 3500));
  }

  /* ---------------- 行情 ---------------- */
  async function loadSymbols(first) {
    let data;
    try {
      data = await api("/api/symbols");
    } catch (e) {
      return;
    }
    state.symbols = data.symbols || [];
    state.globalMode = data.global_mode || data.mode || "--";
    $("mktDot").className = "dot" + (data.market_open ? "" : " closed");
    $("mktText").textContent = data.market_open ? "开市中" : "休市中";
    $("serverTime").textContent = data.server_time || "";

    if (!state.symbol || !state.symbols.some((s) => s.symbol === state.symbol)) {
      if (state.symbols.length) {
        state.symbol = state.symbols[0].symbol;
        localStorage.setItem("mp_symbol", state.symbol);
      }
    }
    renderMarkets();
    renderHeader();
  }

  function renderMarkets() {
    const kw = ($("search").value || "").trim().toUpperCase();
    const list = state.symbols.filter(
      (s) => !kw || s.symbol.indexOf(kw) >= 0 || (s.name || "").toUpperCase().indexOf(kw) >= 0
    );
    $("marketList").innerHTML = list
      .map((s) => {
        const extra = s.mode && s.mode !== state.globalMode ? " · " + s.mode : "";
        const tip = s.mode
          ? `走势：${s.mode}（${s.mode_source || "全局配置"}）`
          : s.name || s.symbol;
        return `<div class="mkt-row ${s.symbol === state.symbol ? "active" : ""}" data-sym="${s.symbol}"
             title="${tip}">
          <span class="sym">${s.symbol}<small>${s.name && s.name !== s.symbol ? s.name : ""}${extra}</small></span>
          <span class="px ${cls(s.change_pct)}">${price(s.price)}</span>
          <span class="chg ${cls(s.change_pct)}">${signed(s.change_pct)}%</span>
        </div>`;
      })
      .join("");
    Array.from(document.querySelectorAll(".mkt-row")).forEach((row) => {
      row.addEventListener("click", () => selectSymbol(row.dataset.sym));
    });
  }

  function currentSymbol() {
    return state.symbols.find((s) => s.symbol === state.symbol) || null;
  }

  function renderHeader() {
    const s = currentSymbol();
    if (!s) return;
    $("curSym").textContent = s.symbol + "/USDT";
    // 走势只在后端允许（web_show_mode=true）时才出现在币种名后面；默认什么都没有
    const baseName = s.name && s.name !== s.symbol ? s.name : "永续合约";
    $("curName").textContent = s.mode ? baseName + "  ·  走势 " + s.mode : baseName;
    // 兼容旧版残留元素：如果页面上还有 modeTag，直接移除，避免露出「走势 --」
    const legacyTag = document.getElementById("modeTag");
    if (legacyTag && legacyTag.parentNode) legacyTag.parentNode.removeChild(legacyTag);
    $("curPrice").textContent = price(s.price);
    $("curPrice").className = "big-price " + cls(s.change_pct);
    $("curChange").textContent = signed(s.change_pct) + "%";
    $("curChange").className = "chg " + cls(s.change_pct);
    $("statHigh").textContent = price(s.high);
    $("statLow").textContent = price(s.low);
    $("statVol").textContent = fmt(s.volume, 2);
    const btn = $("openBtn");
    btn.textContent = (state.side === "LONG" ? "开多 " : "开空 ") + s.symbol;
    btn.className = "btn-open " + (state.side === "LONG" ? "long" : "short");
    updatePreview();
  }

  function selectSymbol(sym) {
    if (!sym || sym === state.symbol) return;
    state.symbol = sym;
    localStorage.setItem("mp_symbol", sym);
    state.kline = [];
    renderMarkets();
    renderHeader();
    loadKline();
    loadDepth();
    loadTrades();
  }

  /* ---------------- K 线 ---------------- */
  async function loadKline() {
    if (!state.symbol) return;
    try {
      const data = await api("/api/kline/" + state.symbol + "?limit=" + state.klineLimit);
      state.kline = data.data || [];
      drawChart();
    } catch (e) {
      /* 忽略 */
    }
  }

  function drawChart() {
    const cv = $("chart");
    const wrap = cv.parentElement;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(w * dpr);
    cv.height = Math.floor(h * dpr);
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const data = state.kline;
    if (!data.length) {
      ctx.fillStyle = "#848e9c";
      ctx.font = "13px sans-serif";
      ctx.fillText("暂无 K 线数据，等待价格更新…", 20, 34);
      return;
    }

    const padR = 64, padL = 10, padT = 12, padB = 24;
    const plotW = Math.max(40, w - padL - padR);
    const plotH = Math.max(40, h - padT - padB);

    let max = -Infinity, min = Infinity;
    data.forEach((d) => {
      if (d.h > max) max = d.h;
      if (d.l < min) min = d.l;
    });
    if (!isFinite(max) || !isFinite(min)) return;
    const span = max - min || max * 0.02 || 1;
    max += span * 0.08;
    min -= span * 0.08;

    const xOf = (i) => padL + (plotW / data.length) * (i + 0.5);
    const yOf = (p) => padT + plotH * (1 - (p - min) / (max - min));

    // 网格
    ctx.strokeStyle = "#232930";
    ctx.lineWidth = 1;
    ctx.font = "11px monospace";
    ctx.fillStyle = "#848e9c";
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      const p = max - ((max - min) / 4) * i;
      ctx.fillText(price(p), padL + plotW + 6, y + 3);
    }

    // 时间标签（跨度短则显示到秒）
    let labelFn = (s) => (s || "").slice(5, 16);
    try {
      const t0 = new Date((data[0].t || "").replace(" ", "T"));
      const t1 = new Date((data[data.length - 1].t || "").replace(" ", "T"));
      const spanMin = (t1 - t0) / 60000;
      labelFn = spanMin < 40 ? (s) => (s || "").slice(11, 19) : (s) => (s || "").slice(5, 16);
    } catch (e) {
      /* 保持默认 */
    }
    const step = Math.max(1, Math.floor(data.length / 6));
    ctx.textAlign = "center";
    for (let i = 0; i < data.length; i += step) {
      if (xOf(i) < 42) continue; // 避免左边缘标签被裁掉
      ctx.fillText(labelFn(data[i].t), xOf(i), h - 8);
    }
    ctx.textAlign = "left";

    // K 线
    const bw = Math.max(1, (plotW / data.length) * 0.62);
    data.forEach((d, i) => {
      const up = d.c >= d.o;
      const color = up ? state.colors.up : state.colors.down;
      const x = xOf(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, yOf(d.h));
      ctx.lineTo(x, yOf(d.l));
      ctx.stroke();
      const yO = yOf(d.o), yC = yOf(d.c);
      const top = Math.min(yO, yC);
      const hgt = Math.max(1, Math.abs(yC - yO));
      ctx.fillRect(x - bw / 2, top, bw, hgt);
    });

    // 最新价线
    const last = data[data.length - 1];
    const yLast = yOf(last.c);
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "#f0b90b";
    ctx.beginPath();
    ctx.moveTo(padL, yLast);
    ctx.lineTo(padL + plotW, yLast);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#f0b90b";
    ctx.fillRect(padL + plotW, yLast - 8, padR - 2, 16);
    ctx.fillStyle = "#10151a";
    ctx.font = "11px monospace";
    ctx.fillText(price(last.c), padL + plotW + 4, yLast + 4);

    // 十字光标
    if (state.hover) {
      const { mx, my, i } = state.hover;
      ctx.strokeStyle = "#5b636e";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(mx, padT);
      ctx.lineTo(mx, padT + plotH);
      ctx.moveTo(padL, my);
      ctx.lineTo(padL + plotW, my);
      ctx.stroke();
      ctx.setLineDash([]);
      if (data[i]) {
        const tip = $("tip");
        const d = data[i];
        const chg = ((d.c - d.o) / d.o) * 100;
        tip.innerHTML =
          `<div class="muted">${d.t}</div>` +
          `<div>开 <b>${price(d.o)}</b>　高 <b class="up">${price(d.h)}</b></div>` +
          `<div>低 <b class="down">${price(d.l)}</b>　收 <b>${price(d.c)}</b></div>` +
          `<div class="${chg >= 0 ? "up" : "down"}">${signed(chg)}%</div>`;
        tip.classList.remove("hidden");
        const tw = 132;
        tip.style.left = Math.min(mx + 14, w - tw) + "px";
        tip.style.top = Math.max(6, my - 60) + "px";
      }
    } else {
      $("tip").classList.add("hidden");
    }
  }

  function bindChartHover() {
    const cv = $("chart");
    cv.addEventListener("mousemove", (ev) => {
      const rect = cv.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const data = state.kline;
      if (!data.length) return;
      const padL = 10, padR = 64;
      const plotW = Math.max(40, rect.width - padL - padR);
      const idx = Math.min(
        data.length - 1,
        Math.max(0, Math.floor(((mx - padL) / plotW) * data.length))
      );
      state.hover = { mx, my, i: idx };
      drawChart();
    });
    cv.addEventListener("mouseleave", () => {
      state.hover = null;
      drawChart();
    });
    window.addEventListener("resize", drawChart);
  }

  /* ---------------- 盘口 / 成交 ---------------- */
  async function loadDepth() {
    if (!state.symbol) return;
    try {
      const data = await api("/api/depth/" + state.symbol + "?levels=12");
      const maxAcc = Math.max(
        ...data.bids.map((b) => b[2]),
        ...data.asks.map((a) => a[2])
      ) || 1;
      $("asks").innerHTML = data.asks
        .slice()
        .reverse()
        .map(
          (a) =>
            `<div class="book-row"><div class="bg" style="width:${(a[2] / maxAcc) * 100}%"></div>
             <span class="down">${price(a[0])}</span><span>${fmt(a[1], 2)}</span></div>`
        )
        .join("");
      $("bids").innerHTML = data.bids
        .map(
          (b) =>
            `<div class="book-row"><div class="bg" style="width:${(b[2] / maxAcc) * 100}%"></div>
             <span class="up">${price(b[0])}</span><span>${fmt(b[1], 2)}</span></div>`
        )
        .join("");
      $("bookMid").textContent = price(data.price);
    } catch (e) {
      /* 忽略 */
    }
  }

  async function loadTrades() {
    if (!state.symbol) return;
    try {
      const data = await api("/api/trades/" + state.symbol + "?limit=20");
      $("trades").innerHTML = (data.trades || [])
        .map(
          (t) =>
            `<div class="trade-row"><span class="${t.side === "buy" ? "up" : "down"}">${price(
              t.price
            )}</span><span>${fmt(t.qty, 2)}</span></div>`
        )
        .join("");
    } catch (e) {
      /* 忽略 */
    }
  }

  /* ---------------- 账户 ---------------- */
  async function loadAccount() {
    let data;
    try {
      data = await api("/api/account");
    } catch (e) {
      if (e.status === 401) logout();
      return;
    }
    state.account = data;
    $("balTop").textContent = "可用 " + money(data.available);
    $("accBalance").textContent = money(data.balance);
    $("accMargin").textContent = money(data.used_margin);
    $("accPnl").textContent = signed(data.unrealized_pnl);
    $("accPnl").className = cls(data.unrealized_pnl);
    if ($("accRealized")) {
      $("accRealized").textContent = signed(data.realized_pnl);
      $("accRealized").className = cls(data.realized_pnl);
    }
    $("accEquity").textContent = money(data.equity);
    renderPositions(data.positions || []);
    renderAssets(data.holdings || []);
    updatePreview();
    loadHistory();
  }

  function renderPositions(list) {
    $("posCount").textContent = list.length;
    $("posEmpty").classList.toggle("hidden", list.length > 0);
    $("posBody").innerHTML = list
      .map((p) => {
        const dir = p.side === "LONG" ? "多" : "空";
        const badge = p.side === "LONG" ? "badge" : "badge short";
        return `<tr>
          <td>${p.symbol}</td>
          <td><span class="${badge}">${dir}</span></td>
          <td>${p.leverage}x</td>
          <td>${fmt(p.qty, 4)}</td>
          <td>${price(p.entry_price)}</td>
          <td>${price(p.mark_price)}</td>
          <td>${money(p.margin)}</td>
          <td class="${cls(p.pnl)}">${signed(p.pnl)}</td>
          <td class="${cls(p.pnl)}">${signed(p.roe)}%</td>
          <td class="down">${price(p.liq_price)}</td>
          <td>
            <button class="mini-btn" data-close="${p.id}" data-ratio="0.5">半平</button>
            <button class="mini-btn" data-close="${p.id}" data-ratio="1">平仓</button>
          </td>
        </tr>`;
      })
      .join("");
    Array.from(document.querySelectorAll("[data-close]")).forEach((btn) => {
      btn.addEventListener("click", () =>
        closePosition(Number(btn.dataset.close), Number(btn.dataset.ratio))
      );
    });
  }

  function renderAssets(list) {
    $("assetEmpty").classList.toggle("hidden", list.length > 0);
    $("assetBody").innerHTML = list
      .map((h) => {
        const pnl = h.value - h.cost;
        return `<tr>
          <td>${h.symbol}</td><td>${fmt(h.amount, 4)}</td><td>${price(h.price)}</td>
          <td>${money(h.value)}</td><td>${money(h.cost)}</td>
          <td class="${cls(pnl)}">${h.cost > 0 ? signed(pnl) : "--"}</td>
        </tr>`;
      })
      .join("");
  }

  async function loadHistory() {
    let data;
    try {
      data = await api("/api/history");
    } catch (e) {
      return;
    }
    const rows = data.history || [];
    $("histEmpty").classList.toggle("hidden", rows.length > 0);
    $("histBody").innerHTML = rows
      .map((p) => {
        const isSpot = p.kind === "spot";
        let badge;
        let label;
        if (isSpot) {
          const buy = p.side === "buy";
          badge = buy ? "badge" : "badge short";
          label = buy ? "买入" : "卖出";
        } else if (p.status === "liquidated") {
          badge = "badge liq";
          label = "强平";
        } else {
          badge = p.side === "LONG" ? "badge" : "badge short";
          label = p.side === "LONG" ? "多" : "空";
        }
        const pnl =
          p.realized_pnl === null || p.realized_pnl === undefined
            ? "--"
            : signed(p.realized_pnl);
        const statusText = isSpot
          ? "已成交"
          : p.status === "liquidated"
          ? "已强平"
          : "已平仓";
        return `<tr>
          <td>${(p.time || "").slice(5)}</td>
          <td>${isSpot ? "现货" : "合约"}</td>
          <td>${p.symbol}</td>
          <td><span class="${badge}">${label}</span></td>
          <td>${p.leverage ? p.leverage + "x" : "--"}</td>
          <td>${fmt(p.qty, 4)}</td>
          <td>${price(p.entry_price)}</td>
          <td>${p.close_price ? price(p.close_price) : "--"}</td>
          <td class="${p.realized_pnl === null || p.realized_pnl === undefined ? "muted" : cls(p.realized_pnl)}">${pnl}</td>
          <td class="muted">${statusText}</td>
        </tr>`;
      })
      .join("");
  }

  /* ---------------- 下单预览 ---------------- */
  function updatePreview() {
    const s = currentSymbol();
    const priceNow = s ? s.price : 0;
    const lev = state.leverage;
    const margin = Number(state.margin) || 0;
    const notional = margin * lev;
    const qty = priceNow > 0 ? notional / priceNow : 0;
    const fee = notional * (state.config.fee_rate || 0.0005);
    const mmr = state.config.mmr || 0.005;
    let liq = 0;
    if (priceNow > 0) {
      liq =
        state.side === "LONG"
          ? priceNow * (1 + mmr - 1 / lev)
          : priceNow * (1 - mmr + 1 / lev);
    }
    $("pvNotional").textContent = money(notional);
    $("pvQty").textContent = fmt(qty, 4);
    $("pvLiq").textContent = price(Math.max(0, liq));
    $("pvLiq").className = state.side === "LONG" ? "down" : "up";
    $("pvFee").textContent = money(fee);

    const cost = margin + fee;
    const avail = state.account ? state.account.available : 0;
    $("openBtn").disabled = !(margin > 0 && cost <= avail && state.config.market_open !== false);
    if (state.config.market_open === false) {
      $("openBtn").textContent = "休市中";
    }
    if ($("spPrice")) {
      $("spPrice").textContent = price(priceNow);
      const amt = Number($("spotAmount").value) || 0;
      $("spCost").textContent = money(amt * priceNow);
    }
  }

  /* ---------------- 交互 ---------------- */
  function bindLeverage() {
    const range = $("levRange");
    const sync = () => {
      state.leverage = Number(range.value);
      $("levValue").textContent = state.leverage + "x";
      const pct = ((state.leverage - 1) / (Number(range.max) - 1)) * 100;
      range.style.background = `linear-gradient(to right, #f0b90b 0%, #f0b90b ${pct}%, #2b3139 ${pct}%, #2b3139 100%)`;
      Array.from(document.querySelectorAll("#levQuick button")).forEach((b) =>
        b.classList.toggle("active", Number(b.dataset.lev) === state.leverage)
      );
      updatePreview();
    };
    range.addEventListener("input", sync);
    Array.from(document.querySelectorAll("#levQuick button")).forEach((b) => {
      b.addEventListener("click", () => {
        range.value = b.dataset.lev;
        sync();
      });
    });
    sync();
  }

  function bindSide() {
    Array.from(document.querySelectorAll("#sideSwitch button")).forEach((b) => {
      b.addEventListener("click", () => {
        state.side = b.dataset.side;
        Array.from(document.querySelectorAll("#sideSwitch button")).forEach((x) =>
          x.classList.toggle("active", x === b)
        );
        renderHeader();
      });
    });
    Array.from(document.querySelectorAll("#spotSide button")).forEach((b) => {
      b.addEventListener("click", () => {
        state.spotSide = b.dataset.side;
        Array.from(document.querySelectorAll("#spotSide button")).forEach((x) =>
          x.classList.toggle("active", x === b)
        );
        $("spotBtn").textContent = state.spotSide === "buy" ? "买入" : "卖出";
        $("spotBtn").className = "btn-open " + (state.spotSide === "buy" ? "long" : "short");
      });
    });
  }

  function bindMargin() {
    const input = $("marginInput");
    input.addEventListener("input", () => {
      state.margin = Number(input.value) || 0;
      updatePreview();
    });
    Array.from(document.querySelectorAll("#pctQuick button")).forEach((b) => {
      b.addEventListener("click", () => {
        const avail = state.account ? state.account.available : 0;
        const pct = Number(b.dataset.pct);
        const feeRate = state.config.fee_rate || 0.0005;
        const lev = state.leverage || 1;
        // 手续费按「名义价值 = 保证金 × 杠杆」收取，所以：
        //   margin * (1 + lev * feeRate) <= avail  →  margin <= avail / (1 + lev*feeRate)
        const maxMargin = avail / (1 + lev * feeRate);
        let val = maxMargin * pct;
        val = Math.floor(val * 100) / 100; // 向下取整到分，保证点完就能成交
        if (pct >= 1) val = Math.max(0, val - 0.01);
        input.value = val > 0 ? val : 0;
        state.margin = Number(input.value) || 0;
        updatePreview();
      });
    });
  }

  function bindTabs() {
    Array.from(document.querySelectorAll("#orderTabs button")).forEach((b) => {
      b.addEventListener("click", () => {
        Array.from(document.querySelectorAll("#orderTabs button")).forEach((x) =>
          x.classList.toggle("active", x === b)
        );
        const contract = b.dataset.tab === "contract";
        $("contractForm").classList.toggle("hidden", !contract);
        $("spotForm").classList.toggle("hidden", contract);
      });
    });
    Array.from(document.querySelectorAll("#bottomTabs button")).forEach((b) => {
      b.addEventListener("click", () => {
        Array.from(document.querySelectorAll("#bottomTabs button")).forEach((x) =>
          x.classList.toggle("active", x === b)
        );
        $("panePositions").classList.toggle("hidden", b.dataset.tab !== "positions");
        $("paneHistory").classList.toggle("hidden", b.dataset.tab !== "history");
        $("paneAssets").classList.toggle("hidden", b.dataset.tab !== "assets");
      });
    });
    Array.from(document.querySelectorAll(".nav a")).forEach((a) => {
      a.addEventListener("click", () => {
        Array.from(document.querySelectorAll(".nav a")).forEach((x) =>
          x.classList.toggle("active", x === a)
        );
        const contract = a.dataset.nav === "contract";
        document.querySelector('#orderTabs button[data-tab="' + (contract ? "contract" : "spot") + '"]').click();
      });
    });
    Array.from(document.querySelectorAll("#tfBar button")).forEach((b) => {
      b.addEventListener("click", () => {
        Array.from(document.querySelectorAll("#tfBar button")).forEach((x) =>
          x.classList.toggle("active", x === b)
        );
        state.klineLimit = Number(b.dataset.limit);
        loadKline();
      });
    });
  }

  async function openPosition() {
    const s = currentSymbol();
    if (!s) return;
    const btn = $("openBtn");
    btn.disabled = true;
    try {
      const data = await api("/api/contract/open", {
        method: "POST",
        body: {
          symbol: s.symbol,
          side: state.side,
          margin: Number(state.margin),
          leverage: state.leverage,
        },
      });
      toast(data.message || "开仓成功", "ok");
      loadAccount();
    } catch (e) {
      toast(e.message, "err");
    } finally {
      btn.disabled = false;
      updatePreview();
    }
  }

  async function closePosition(id, ratio) {
    try {
      const data = await api("/api/contract/close", {
        method: "POST",
        body: { position_id: id, ratio: ratio },
      });
      toast(data.message || "平仓成功", "ok");
      loadAccount();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  async function spotTrade() {
    const s = currentSymbol();
    if (!s) return;
    const amount = Number($("spotAmount").value);
    if (!(amount > 0)) {
      toast("请输入数量", "err");
      return;
    }
    try {
      const data = await api("/api/spot/trade", {
        method: "POST",
        body: { symbol: s.symbol, side: state.spotSide, amount: amount },
      });
      toast(data.message || "下单成功", "ok");
      loadAccount();
    } catch (e) {
      toast(e.message, "err");
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindAll() {
    $("loginBtn").addEventListener("click", doLogin);
    $("loginPass").addEventListener("keydown", (e) => {
      if (e.key === "Enter") doLogin();
    });
    $("logoutBtn").addEventListener("click", logout);
    $("openBtn").addEventListener("click", openPosition);
    $("spotBtn").addEventListener("click", spotTrade);
    $("spotAmount").addEventListener("input", updatePreview);
    $("search").addEventListener("input", renderMarkets);
    $("closeAllBtn").addEventListener("click", async () => {
      try {
        const data = await api("/api/contract/close_all", { method: "POST" });
        toast("已平仓 " + (data.count || 0) + " 个仓位", "ok");
        loadAccount();
      } catch (e) {
        toast(e.message, "err");
      }
    });
    bindLeverage();
    bindSide();
    bindMargin();
    bindTabs();
    bindChartHover();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindAll();
    bindAgreement();
    if (state.token) {
      showApp();
    }
  });
})();
