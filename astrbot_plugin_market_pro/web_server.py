# -*- coding: utf-8 -*-
"""Web 交易端（FastAPI + uvicorn，独立线程运行，不占用主事件循环）。"""
import os
import random
import secrets
import socket
import threading
import time

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import json

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

    # 管理页可能来自 AstrBot 面板（不同端口），需要允许跨域调用
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 管理页平时跑在 sandbox iframe 里（origin 为 "null"），所以它访问的接口都不带凭据。
    # GET 版在下面用 JSONP 暴露，POST 版要求 confirm=RESET。

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
    async def index():
        path = os.path.join(static_dir, "index.html")
        if not os.path.exists(path):
            return JSONResponse({"error": "index.html missing"}, status_code=500)
        return FileResponse(path)

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
    async def admin_overview():
        return _overview_payload()

    @app.get("/api/admin/overview_public")
    async def admin_overview_public(callback: Optional[str] = None):
        """供 sandbox iframe 里的管理页用（无 cookie / 跨 origin），支持 JSONP。"""
        data = _overview_payload()
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
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        """GET 版重置（JSONP 用），同样要求 confirm=RESET。"""
        need_key = settings.get_str("admin_api_key", "").strip()
        if need_key and str(x_admin_key or "") != need_key:
            raise HTTPException(status_code=403, detail="管理密钥错误")
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
        x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    ):
        # 二次确认：请求体必须带 confirm=RESET
        if str(data.confirm or "").strip().upper() != "RESET":
            raise HTTPException(
                status_code=400,
                detail="二次确认未通过：请求体需要 confirm=RESET",
            )
        need_key = settings.get_str("admin_api_key", "").strip()
        if need_key and str(x_admin_key or "") != need_key:
            raise HTTPException(status_code=403, detail="管理密钥错误")

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
