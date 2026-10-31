# -*- coding: utf-8 -*-
"""Web 交易端（FastAPI + uvicorn，独立线程运行，不占用主事件循环）。"""
import os
import random
import secrets
import socket
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import json
import re

# 手机端 UA 特征（iPad 也走手机版：屏幕窄，桌面四栏布局挤不下）
MOBILE_UA_RE = re.compile(
    r"Android|iPhone|iPod|iPad|Mobile|Windows Phone|IEMobile|Opera Mini|BlackBerry|webOS|HarmonyOS",
    re.I,
)

try:
    from .database import (
        Order,
        OrderStatus,
        Position,
        PositionStatus,
        User,
        normalize_symbol,
    )
    from .leverage import MarginEngine, SIDE_LONG, SIDE_SHORT, normalize_side
except ImportError:  # 兼容直接运行
    from database import (
        Order,
        OrderStatus,
        Position,
        PositionStatus,
        User,
        normalize_symbol,
    )
    from leverage import MarginEngine, SIDE_LONG, SIDE_SHORT, normalize_side

try:
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
except Exception:  # pragma: no cover
    pwd_context = None


# ---------------------------------------------------------------- 请求模型
class LoginModel(BaseModel):
    user_id: str
    password: str


class ContractOpenModel(BaseModel):
    symbol: str
    side: str
    margin: float
    leverage: int = 1


class ContractCloseModel(BaseModel):
    position_id: int
    ratio: float = 1.0


class SpotTradeModel(BaseModel):
    symbol: str
    side: str
    amount: float
    price: Optional[float] = None


class ResetPricesModel(BaseModel):
    confirm: str = ""


def create_app(db, market, margin: MarginEngine, settings, static_dir, logger=None):
    app = FastAPI(title="Market Pro Trading", docs_url=None, redoc_url=None)

    tokens = {}  # token -> user_id
    tokens_lock = threading.Lock()

    def show_mode():
        """是否在 Web 端暴露走势模式（默认关闭，避免"露馅"）。"""
        return settings.get_bool("web_show_mode", False)

    def _overview_payload():
        rows = []
        for item in market.reset_targets():
            coin = market.coin_info(item["symbol"])
            rows.append(
                {
                    "symbol": item["symbol"],
                    "name": (coin.name if coin else item["symbol"]) or item["symbol"],
                    "current": item["current"],
                    "target": item["target"],
                    "actionable": item["actionable"],
                }
            )
        return {
            "status": "ok",
            "coins": rows,
            "market_open": bool(market.is_open),
            "mode": market.global_mode(),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # 管理页可能来自 AstrBot 面板（不同端口），需要允许跨域调用。
    # 注意：管理接口不再依赖 CORS 保护，而是必须带 X-Admin-Key，
    # 因此这里只放开交易端自身需要的公开端点。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # 管理页平时跑在 sandbox iframe 里（origin 为 "null"），所以它访问的接口都不带 cookie。
    # 因此所有管理接口都必须带 X-Admin-Key（或 JSONP 的 key 参数）才能生效。

    def _admin_key_required():
        """是否强制校验管理密钥：默认强制。"""
        return settings.get_bool("admin_api_key_required", True)

    def _admin_authorized(provided):
        """管理鉴权：admin_api_key 为空且允许无密钥时才放行（默认不放行）。"""
        need_key = settings.get_str("admin_api_key", "").strip()
        if not need_key:
            # 未配置密钥：默认拒绝一切管理操作，避免默认配置裸奔
            return not _admin_key_required()
        return secrets.compare_digest(str(provided or ""), need_key)

    def _admin_forbidden(detail="管理接口未授权：请在插件配置中设置 admin_api_key"):
        if logger:
            try:
                logger.warning("[market_pro] 管理接口拒绝了未授权请求")
            except Exception:
                pass
        return JSONResponse({"status": "error", "message": detail}, status_code=403)

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        """页面与前端资源禁止缓存，避免升级后浏览器还在用旧的 app.js。"""
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ---------- 鉴权 ----------
    def auth(token: Optional[str] = Header(default=None, alias="X-Token")):
        if not token:
            raise HTTPException(status_code=401, detail="未登录")
        with tokens_lock:
            uid = tokens.get(token)
        if not uid:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
        return uid

    # ---------- 基础 ----------
    @app.get("/")
    async def index(request: Request):
        """按 UA 分发两套 UI：桌面 /web/index.html，手机 /web/mobile/index.html。

        优先级：?ui=mobile|desktop（并记住，写 cookie）> cookie > UA 识别。
        """
        override = (request.query_params.get("ui") or "").strip().lower()
        cookie = (request.cookies.get("mkt_ui") or "").strip().lower()

        ua = request.headers.get("user-agent", "") or ""
        ch_mobile = (request.headers.get("sec-ch-ua-mobile", "") or "").strip()
        # 以 UA 为准；Sec-CH-UA-Mobile: ?1 只做「加分」信号。
        # 不能因为 ?0 就判定桌面——很多浏览器在自定义 UA / 移动端内核下仍会发 ?0。
        is_mobile = bool(MOBILE_UA_RE.search(ua))
        if ch_mobile in ("?1", "1"):
            is_mobile = True

        if override in ("mobile", "m", "phone"):
            is_mobile = True
        elif override in ("desktop", "pc", "web"):
            is_mobile = False
        elif cookie in ("mobile", "desktop"):
            is_mobile = cookie == "mobile"

        rel = "mobile/index.html" if is_mobile else "index.html"
        path = os.path.join(static_dir, *rel.split("/"))
        if not os.path.exists(path):
            path = os.path.join(static_dir, "index.html")
        if not os.path.exists(path):
            return JSONResponse({"error": "index.html missing"}, status_code=500)

        resp = FileResponse(path)
        resp.headers["Vary"] = "User-Agent, Sec-CH-UA-Mobile, Cookie"
        resp.headers["Cache-Control"] = "no-store"
        if override in ("mobile", "m", "phone"):
            resp.set_cookie("mkt_ui", "mobile", max_age=2592000, path="/", samesite="lax")
        elif override in ("desktop", "pc", "web"):
            resp.set_cookie("mkt_ui", "desktop", max_age=2592000, path="/", samesite="lax")
        return resp

    @app.get("/api/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    @app.get("/api/config")
    async def get_config():
        data = {
            "max_leverage": margin.max_leverage(),
            "min_leverage": 1,
            "fee_rate": margin.fee_rate(),
            "mmr": margin.mmr(),
            "initial_balance": settings.get_float("initial_balance", 10000.0),
            "max_positions": margin.max_positions(),
            "market_open": bool(market.is_open),
            "tick_interval": settings.get_int("tick_interval", 5),
            "color_style": "cn" if "中国" in settings.get_str("chart_color_style", "") else "intl",
        }
        if show_mode():
            data["mode"] = market.global_mode()
            data["global_mode"] = market.global_mode()
        return data

    # ---------- 登录 ----------
    @app.post("/api/login")
    async def login(data: LoginModel):
        if pwd_context is None:
            raise HTTPException(status_code=500, detail="缺少 passlib 依赖")
        uid = str(data.user_id).strip()
        if not uid:
            raise HTTPException(status_code=400, detail="账号不能为空")
        password_hash = db.get_password_hash(uid)
        if not password_hash:
            raise HTTPException(
                status_code=400,
                detail="该账号未设置密码，请先在群里发送 /mkt register <密码>",
            )
        if not pwd_context.verify(data.password, password_hash):
            raise HTTPException(status_code=400, detail="密码错误")
        token = secrets.token_urlsafe(24)
        with tokens_lock:
            tokens[token] = uid
        return {"token": token, "user_id": uid}

    @app.post("/api/logout")
    async def logout(token: Optional[str] = Header(default=None, alias="X-Token")):
        if token:
            with tokens_lock:
                tokens.pop(token, None)
        return {"ok": True}

    # ---------- 行情 ----------
    @app.get("/api/symbols")
    async def symbols():
        expose_mode = show_mode()
        out = []
        for sym, price in sorted(market.prices.items()):
            coin = market.coin_info(sym)
            change = market.daily_change_pct(sym)
            candle = market.candles.get(sym) or {}
            item = {
                "symbol": sym,
                "name": (coin.name if coin else sym) or sym,
                "price": price,
                "change_pct": change,
                "high": candle.get("high", price),
                "low": candle.get("low", price),
                "volume": candle.get("volume", 0.0),
            }
            if expose_mode:
                item["mode"] = market.current_mode(sym)
                item["mode_source"] = market.coin_mode_source(sym)
            out.append(item)
        data = {
            "symbols": out,
            "market_open": bool(market.is_open),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if expose_mode:
            data["mode"] = market.global_mode()
            data["global_mode"] = market.global_mode()
            data["global_overridden"] = bool(market.mode_override)
        return data

    @app.get("/api/kline/{symbol}")
    async def kline(symbol: str, limit: int = Query(default=90, ge=10, le=500)):
        sym = normalize_symbol(symbol)
        if sym not in market.prices:
            raise HTTPException(status_code=404, detail="币种不存在")
        session = db.get_session()
        try:
            rows = db.recent_history(session, sym, limit)
        finally:
            session.close()
        data = [
            {
                "t": r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "o": r.open,
                "h": r.high,
                "l": r.low,
                "c": r.close,
                "v": r.volume or 0.0,
            }
            for r in rows
        ]
        return {"symbol": sym, "data": data, "price": market.prices.get(sym, 0.0)}

    @app.get("/api/depth/{symbol}")
    async def depth(symbol: str, levels: int = Query(default=14, ge=5, le=40)):
        sym = normalize_symbol(symbol)
        price = market.prices.get(sym)
        if not price:
            raise HTTPException(status_code=404, detail="币种不存在")
        rnd = random.Random(int(price * 10000) % 1000003)
        step = max(price * 0.0004, 0.0001)
        bids, asks = [], []
        acc = 0.0
        for i in range(levels):
            p = price - step * (i + 1) * (1 + rnd.random() * 0.4)
            q = abs(rnd.gauss(0, 1)) * 40 + 5
            acc += q
            bids.append([round(max(0.0, p), 8), round(q, 4), round(acc, 4)])
        acc = 0.0
        for i in range(levels):
            p = price + step * (i + 1) * (1 + rnd.random() * 0.4)
            q = abs(rnd.gauss(0, 1)) * 40 + 5
            acc += q
            asks.append([round(p, 8), round(q, 4), round(acc, 4)])
        return {"symbol": sym, "price": price, "bids": bids, "asks": asks}

    @app.get("/api/trades/{symbol}")
    async def recent_trades(symbol: str, limit: int = Query(default=24, ge=5, le=60)):
        sym = normalize_symbol(symbol)
        price = market.prices.get(sym)
        if not price:
            raise HTTPException(status_code=404, detail="币种不存在")
        rnd = random.Random(int(time.time() / 3) ^ int(price * 1000))
        out = []
        p = price
        now = time.time()
        for i in range(limit):
            p = max(0.0001, p * (1 + rnd.gauss(0, 0.0008)))
            out.append(
                {
                    "price": p,
                    "qty": round(abs(rnd.gauss(0, 1)) * 20 + 0.5, 4),
                    "side": "buy" if rnd.random() > 0.5 else "sell",
                    "t": time.strftime("%H:%M:%S", time.localtime(now - i * 4)),
                }
            )
        return {"symbol": sym, "trades": out}

    # ---------- 账户 ----------
    @app.get("/api/account")
    async def account(user_id: str = Depends(auth)):
        data = margin.account(user_id)
        session = db.get_session()
        try:
            holdings = []
            for h in db.list_holdings(session, user_id):
                price = market.prices.get(h.symbol, 0.0)
                holdings.append(
                    {
                        "symbol": h.symbol,
                        "amount": h.amount,
                        "price": price,
                        "value": h.amount * price,
                        "cost": h.cost or 0.0,
                    }
                )
        finally:
            session.close()
        data["holdings"] = holdings
        return data

    @app.get("/api/history")
    async def history(user_id: str = Depends(auth)):
        """统一历史成交：合约平仓 + 现货买卖成交。"""
        items = []
        session = db.get_session()
        try:
            for p in db.closed_positions(session, user_id, 40):
                items.append(
                    {
                        "kind": "contract",
                        "id": p.id,
                        "symbol": p.symbol,
                        "side": p.side,
                        "qty": (p.closed_qty or p.qty or 0.0),
                        "entry_price": p.entry_price,
                        "close_price": p.close_price,
                        "leverage": p.leverage,
                        "margin": p.margin,
                        "realized_pnl": p.realized_pnl,
                        "status": p.status.value,
                        "time": (p.closed_at or p.opened_at).strftime("%Y-%m-%d %H:%M:%S")
                        if (p.closed_at or p.opened_at)
                        else "",
                    }
                )
            orders = (
                session.query(Order)
                .filter(Order.user_id == str(user_id))
                .filter(Order.status == OrderStatus.FILLED)
                .order_by(Order.id.desc())
                .limit(40)
                .all()
            )
            for o in orders:
                side = o.order_type.value if o.order_type else ""
                items.append(
                    {
                        "kind": "spot",
                        "id": o.id,
                        "symbol": o.symbol,
                        "side": side,
                        "qty": o.amount,
                        "entry_price": o.filled_price if o.filled_price else (o.price or 0.0),
                        "close_price": None,
                        "leverage": None,
                        "margin": None,
                        "realized_pnl": None,
                        "status": "filled",
                        "time": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
                    }
                )
        finally:
            session.close()
        items.sort(key=lambda x: x.get("time") or "", reverse=True)
        return {"history": items[:40]}

    # ---------- 合约 ----------
    @app.get("/api/preview")
    async def preview(symbol: str, side: str, margin: float, leverage: int):
        return margin.preview(symbol, side, margin, leverage)

    @app.post("/api/contract/open")
    async def contract_open(data: ContractOpenModel, user_id: str = Depends(auth)):
        ok, message, payload = margin.open_position(
            user_id, data.symbol, data.side, data.margin, data.leverage
        )
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message, "position": payload}

    @app.post("/api/contract/close")
    async def contract_close(data: ContractCloseModel, user_id: str = Depends(auth)):
        ok, message, payload = margin.close_position(user_id, data.position_id, data.ratio)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message, "result": payload}

    @app.post("/api/contract/close_all")
    async def contract_close_all(user_id: str = Depends(auth)):
        results = margin.close_all(user_id)
        return {"ok": True, "count": len(results)}

    # ---------- 现货 ----------
    @app.post("/api/spot/trade")
    async def spot_trade(data: SpotTradeModel, user_id: str = Depends(auth)):
        side = "buy" if str(data.side).lower() in ("buy", "long", "买", "买入") else "sell"
        ok, message, _status = market.place_order(
            user_id, data.symbol, data.amount, data.price, side
        )
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True, "message": message}

    # ---------- 管理：一键重置股价 ----------
    @app.get("/api/admin/overview")
    async def admin_overview(
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        if not _admin_authorized(x_admin_key):
            raise HTTPException(status_code=403, detail="管理接口未授权：请配置 admin_api_key")
        return _overview_payload()

    @app.get("/api/admin/overview_public")
    async def admin_overview_public(
        callback: Optional[str] = None,
        key: Optional[str] = None,
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        """供 sandbox iframe 里的管理页用（无 cookie / 跨 origin），支持 JSONP。

        仍必须带管理密钥（请求头 X-Admin-Key 或查询参数 key）。
        """
        provided = x_admin_key or key
        if not _admin_authorized(provided):
            data = {
                "status": "error",
                "message": "管理接口未授权：请配置并在管理页填入 admin_api_key",
            }
            if logger:
                try:
                    logger.warning("[market_pro] 管理接口拒绝了未授权请求")
                except Exception:
                    pass
            # JSONP 也要带 403，避免“看起来成功”的响应被当成数据
            if callback:
                safe = "".join(ch for ch in str(callback) if ch.isalnum() or ch in "_$.")
                if safe:
                    return Response(
                        content="{}({});".format(safe, json.dumps(data, ensure_ascii=False)),
                        media_type="application/javascript",
                        status_code=403,
                    )
            return JSONResponse(data, status_code=403)
        data = _overview_payload()
        if callback:
            safe = "".join(ch for ch in str(callback) if ch.isalnum() or ch in "_$.")
            if safe:
                body = "{}({});".format(safe, json.dumps(data, ensure_ascii=False))
                return Response(content=body, media_type="application/javascript")
        return JSONResponse(data)

    @app.get("/api/admin/nudge_public")
    async def admin_nudge_public(
        callback: Optional[str] = None,
        symbol: str = "",
        pct: float = 0.0,
        scope: str = "one",
        key: Optional[str] = None,
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        """实时调控：按百分比立刻推价（JSONP，供 sandbox iframe 里的管理页用）。

        scope=one  只推 symbol 这一个币种
        scope=all  全市场同比例推
        """
        if not _admin_authorized(x_admin_key or key):
            raise HTTPException(status_code=403, detail="管理接口未授权")

        pct = max(-50.0, min(50.0, float(pct or 0.0)))
        if scope == "all":
            results = market.nudge_all(pct)
        else:
            r = market.nudge(symbol, pct)
            if r is None:
                data = {"status": "error", "message": "未找到币种: {}".format(symbol)}
                if callback:
                    safe = "".join(c for c in str(callback) if c.isalnum() or c in "_$.")
                    if safe:
                        return Response(
                            content="{}({});".format(safe, json.dumps(data, ensure_ascii=False)),
                            media_type="application/javascript",
                        )
                return JSONResponse(data)
            results = [r]

        if logger:
            try:
                logger.info(
                    "[market_pro] 管理页实时调控: {} {}（{} 个币种）".format(
                        pct, "%", len(results)
                    )
                )
            except Exception:
                pass

        data = {
            "status": "ok",
            "ok": True,
            "pct": pct,
            "scope": scope,
            "count": len(results),
            "results": results,
            "prices": {s: float(p) for s, p in market.prices.items()},
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if callback:
            safe = "".join(ch for ch in str(callback) if ch.isalnum() or ch in "_$.")
            if safe:
                body = "{}({});".format(safe, json.dumps(data, ensure_ascii=False))
                return Response(content=body, media_type="application/javascript")
        return JSONResponse(data)

    @app.get("/api/admin/reset_prices_public")
    async def admin_reset_prices_public_get(
        callback: Optional[str] = None,
        confirm: str = "",
        key: Optional[str] = None,
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        """GET 版重置（JSONP 用），要求管理密钥 + confirm=RESET。"""
        if not _admin_authorized(x_admin_key or key):
            raise HTTPException(status_code=403, detail="管理接口未授权")
        if str(confirm or "").strip().upper() != "RESET":
            data = {"status": "error", "message": "二次确认未通过：需要 confirm=RESET"}
        else:
            changed = market.reset_all_prices()
            if logger:
                try:
                    logger.warning(
                        "[market_pro] 管理页重置了 {} 个币种的价格".format(len(changed))
                    )
                except Exception:
                    pass
            data = {
                "status": "ok",
                "ok": True,
                "count": len(changed),
                "changed": changed,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        if callback:
            safe = "".join(ch for ch in str(callback) if ch.isalnum() or ch in "_$.")
            if safe:
                body = "{}({});".format(safe, json.dumps(data, ensure_ascii=False))
                return Response(content=body, media_type="application/javascript")
        return JSONResponse(data)

    @app.post("/api/admin/reset_prices")
    async def admin_reset_prices(
        data: ResetPricesModel,
        key: Optional[str] = None,
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        # 鉴权优先：未授权直接 403，不再依赖 confirm 兜底
        if not _admin_authorized(x_admin_key or key):
            raise HTTPException(status_code=403, detail="管理接口未授权：请配置 admin_api_key")
        # 二次确认：请求体必须带 confirm=RESET
        if str(data.confirm or "").strip().upper() != "RESET":
            raise HTTPException(
                status_code=400,
                detail="二次确认未通过：请求体需要 confirm=RESET",
            )

        changed = market.reset_all_prices()
        if logger:
            try:
                logger.warning(
                    "[market_pro] 通过接口重置了 {} 个币种的价格".format(len(changed))
                )
            except Exception:
                pass
        return {
            "ok": True,
            "count": len(changed),
            "changed": changed,
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    return app


class WebServer:
    """把 FastAPI 应用跑在独立守护线程里。"""

    def __init__(self, db, market, margin, settings, static_dir, logger=None):
        self.db = db
        self.market = market
        self.margin = margin
        self.settings = settings
        self.static_dir = static_dir
        self.logger = logger
        self.host = settings.get_str("web_host", "0.0.0.0") or "0.0.0.0"
        self.port = settings.get_int("web_port", 8666)
        self.app = create_app(db, market, margin, settings, static_dir, logger)
        self.server = None
        self.thread = None
        self._error = ""

    def log(self, msg, level="info"):
        if self.logger:
            try:
                getattr(self.logger, level)(msg)
                return
            except Exception:
                pass
        print(msg)

    def port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self):
        if not self.settings.get_bool("web_enabled", True):
            self.log("[market_pro] Web 端已在配置中关闭")
            return False
        if self.port_in_use():
            self._error = "端口 {} 已被占用".format(self.port)
            self.log("[market_pro] Web 启动失败：{}，请改 web_port".format(self._error), "error")
            return False
        try:
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
            )
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(
                target=self.server.run, daemon=True, name="market_pro_web"
            )
            self.thread.start()
            for _ in range(50):
                time.sleep(0.1)
                if self.port_in_use():
                    break
            url = "http://{}:{}".format("127.0.0.1", self.port)
            public = self.settings.get_str("web_public_url", "")
            self.log(
                "[market_pro] Web 交易端已启动: {}（对外: {}）".format(url, public or "未配置")
            )
            return True
        except Exception as e:
            self._error = str(e)
            self.log("[market_pro] Web 启动异常: {}".format(e), "error")
            return False

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
