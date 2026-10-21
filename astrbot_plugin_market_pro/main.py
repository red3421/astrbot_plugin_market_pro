# -*- coding: utf-8 -*-
"""多币种模拟交易插件（走势可控 + 管理命令）。

命令入口：/mkt
"""
import os
import tempfile

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    from .database import DB, User, AdminLog, now_cn, normalize_symbol
    from .market import MarketEngine, fmt_price, MODES
    from .leverage import MarginEngine, SIDE_LONG, SIDE_SHORT
    from .settings import Settings, DEFAULTS
    from .web_server import WebServer
    from . import plotter
except ImportError:  # 兼容直接运行/调试
    from database import DB, User, AdminLog, now_cn, normalize_symbol
    from market import MarketEngine, fmt_price, MODES
    from leverage import MarginEngine, SIDE_LONG, SIDE_SHORT
    from settings import Settings, DEFAULTS
    from web_server import WebServer
    import plotter

try:
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
except Exception:  # pragma: no cover
    pwd_context = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = os.path.basename(PLUGIN_DIR)


def _resolve_config_path():
    """定位 AstrBot 的 data/config 目录；失败时退回插件目录。"""
    candidates = []
    try:
        candidates.append(os.path.dirname(os.path.dirname(PLUGIN_DIR)))
    except Exception:
        pass
    candidates.append(PLUGIN_DIR)
    for base in candidates:
        config_dir = os.path.join(base, "config")
        if os.path.isdir(config_dir):
            return os.path.join(config_dir, "{}_config.json".format(PLUGIN_NAME))
    return os.path.join(PLUGIN_DIR, "{}_config.json".format(PLUGIN_NAME))


HELP_TEXT = """📈 模拟交易系统（Web 交易端）
━━━━━ 行情 ━━━━━
/mkt price [币]      实时价格
/mkt change          今日涨跌
/mkt kline <币>      K线图
/mkt info [币]       币种资料
/mkt coins           币种列表
/mkt time            市场状态

━━━━━ 现货 ━━━━━
/mkt buy <币> <数量> [价格]
/mkt sell <币> <数量> [价格]
/mkt orders          我的挂单
/mkt cancel <ID>     撤单
/mkt assets          我的现货资产

━━━━━ 合约 (1-125x) ━━━━━
/mkt long <币> <保证金> [杠杆]    开多
/mkt short <币> <保证金> [杠杆]   开空
/mkt pos                          我的仓位
/mkt close <仓位ID> [比例]         平仓
/mkt closeall                     一键全平
/mkt lev                          杠杆规则说明

━━━━━ 账户 / Web ━━━━━
/mkt register <密码>  开通 Web 交易端
/mkt web              Web 地址
/mkt rank              资产排行

━━━━━ 市场 ━━━━━
/mkt mopen            开市
/mkt mclose           收市"""


@register("market_pro", "market_pro", "多币种模拟交易（走势可配置 + 管理员加钱/转账/转股）", "1.6.2", "")
class MarketPro(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        global PLUGIN_DIR, PLUGIN_NAME
        PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
        PLUGIN_NAME = os.path.basename(PLUGIN_DIR)
        self.config_path = _resolve_config_path()
        self.settings = Settings(self.config_path, DEFAULTS, logger)
        self.settings.bootstrap(self.config)

        self.db = DB(os.path.join(PLUGIN_DIR, "market_pro.db"))
        self.market = MarketEngine(self.db, self.settings, logger)
        self.margin = MarginEngine(self.db, self.settings, self.market, logger)
        self.market.margin_engine = self.margin
        self.market.start()

        # 注册同源插件 Web API（供 pages/console 管理页调用，无需跨端口）
        self._register_web_apis()

        # 配置页备用开关：勾选后执行一次并自动取消勾选
        self._reset_arm_last = self.settings.get_bool("reset_prices_arm", False)
        self._last_reset_ts = 0.0
        self.settings.add_reload_hook(self._on_settings_reload)

        self.web_server = WebServer(
            self.db,
            self.market,
            self.margin,
            self.settings,
            os.path.join(PLUGIN_DIR, "web"),
            logger,
        )
        self.web_server.start()

        logger.info(
            "[market_pro] 已启动 | 配置: {} | 币种: {} | Web: {}:{}".format(
                self.config_path,
                ", ".join(sorted(self.market.prices.keys())),
                self.web_server.host,
                self.web_server.port,
            )
        )

    async def terminate(self):
        try:
            if getattr(self, "web_server", None) is not None:
                self.web_server.stop()
            self.market.stop()
        finally:
            logger.info("[market_pro] 已停止")

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _save_temp_image(buf):
        try:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="mkt_")
            with os.fdopen(fd, "wb") as f:
                f.write(buf.getvalue())
            return path
        except Exception as e:
            logger.error("[market_pro] save image failed: {}".format(e))
            return None

    def _is_admin(self, user_id):
        return str(user_id) in self.settings.admin_ids()

    # ------------------------------------------------------------ 配置页备用开关
    def _on_settings_reload(self):
        """配置页「⚠ 一键重置股价」勾选后执行一次，并自动把开关改回未勾选。"""
        import time as _time

        arm = self.settings.get_bool("reset_prices_arm", False)
        rising = arm and not self._reset_arm_last
        self._reset_arm_last = arm
        if not rising:
            return
        if _time.time() - self._last_reset_ts < 60:
            # 60 秒内重复触发（多半是面板把旧值又写回来了），忽略
            logger.warning("[market_pro] 距上次重置不足 60 秒，本次忽略")
            return
        self._last_reset_ts = _time.time()
        try:
            changed = self.market.reset_all_prices()
            self.db.write_log("config_page", "reset_prices", "all", "{} coins".format(len(changed)))
            logger.warning(
                "[market_pro] 配置页开关触发，已重置 {} 个币种价格（开关已自动取消）".format(len(changed))
            )
        except Exception as e:
            logger.error("[market_pro] 配置页重置股价失败: {}".format(e))
        # 自动复位开关，避免下次保存配置时又触发
        if self.settings.set_value_in_file("reset_prices_arm", False):
            self.settings.reload()
            self._reset_arm_last = False

    # ------------------------------------------------------------ 插件 Web API（同源，供管理页用）
    def _register_web_apis(self):
        ctx = getattr(self, "context", None)
        register = getattr(ctx, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[market_pro] 当前 AstrBot 不支持插件 Web API，管理页将回退到端口直连"
            )
            return
        # 面板调用时的路径前缀是「插件名」，不同 AstrBot 版本可能用目录名或注册名，
        # 这里把两种都注册上，页面就不用关心具体是哪个。
        prefixes = []
        for name in (PLUGIN_NAME, "market_pro", getattr(self, "name", None)):
            text = str(name or "").strip()
            if text and text not in prefixes:
                prefixes.append(text)
        try:
            for prefix in prefixes:
                register(
                    "{}/overview".format(prefix),
                    self.web_overview,
                    ["GET"],
                    "模拟股市概览",
                )
                register(
                    "{}/reset_prices".format(prefix),
                    self.web_reset_prices,
                    ["POST"],
                    "一键重置全部股价",
                )
            logger.info(
                "[market_pro] 已注册插件 Web API 前缀: {}".format(", ".join(prefixes))
            )
        except Exception as e:
            logger.warning("[market_pro] 注册插件 Web API 失败: {}".format(e))

    def _overview_payload(self):
        rows = []
        for item in self.market.reset_targets():
            coin = self.market.coin_info(item["symbol"])
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
            "market_open": bool(self.market.is_open),
            "mode": self.market.global_mode(),
            "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def web_overview(self):
        """GET /api/v1/plugins/extensions/market_pro/overview"""
        return self._overview_payload()

    async def web_reset_prices(self):
        """POST /api/v1/plugins/extensions/market_pro/reset_prices"""
        body = {}
        try:
            from astrbot.api.web import request as web_request

            body = await web_request.json(default={}) or {}
        except Exception:
            body = {}
        if str((body or {}).get("confirm", "")).strip().upper() != "RESET":
            return {
                "status": "error",
                "message": "二次确认未通过：需要 confirm=RESET",
                "data": {},
            }
        changed = self.market.reset_all_prices()
        self.db.write_log("dashboard", "reset_prices", "all", "{} coins".format(len(changed)))
        logger.warning("[market_pro] 插件页一键重置：{} 个币种".format(len(changed)))
        return {
            "status": "ok",
            "count": len(changed),
            "changed": changed,
            "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _num(value, digits=2):
        try:
            return ("{:." + str(digits) + "f}").format(float(value))
        except (TypeError, ValueError):
            return "-"

    def _coin_rows(self):
        return self.market.coins()

    def _web_url(self):
        public = self.settings.get_str("web_public_url", "").strip()
        if public:
            return public
        port = self.settings.get_int("web_port", 8666)
        return "http://<机器人IP>:{}".format(port)

    def _mode_hint(self):
        """解释当前走势大概会怎么走，避免「怎么涨到一半就停了」的困惑。"""
        mode = self.market.global_mode()
        after = self.settings.get_str("after_trend_mode", "横盘震荡")
        if mode == "连续上涨":
            ticks = self.settings.get_int("rally_ticks", 15)
            step = abs(self.settings.get_float("rally_step", 0.02))
            if ticks > 0:
                total = (1.0 + step) ** ticks - 1.0
                return "连涨 {} 个 tick × {:.2%} ≈ 累计 {:+.1f}%，跑完后转「{}」".format(
                    ticks, step, total * 100, after
                )
            return "连涨无限持续（rally_ticks=0，不会停）"
        if mode == "连续下跌":
            ticks = self.settings.get_int("dump_ticks", 15)
            step = abs(self.settings.get_float("dump_step", 0.02))
            if ticks > 0:
                total = 1.0 - (1.0 - step) ** ticks
                return "连跌 {} 个 tick × {:.2%} ≈ 累计 -{:.1f}%，跑完后转「{}」".format(
                    ticks, step, total * 100, after
                )
            return "连跌无限持续（dump_ticks=0，不会停）"
        if mode == "上涨":
            return "每个 tick 涨 {:.2%}，不会自动停（想停请改用「连续上涨」）".format(
                abs(self.settings.get_float("rise_strength", 0.01))
            )
        if mode == "下跌":
            return "每个 tick 跌 {:.2%}，不会自动停".format(
                abs(self.settings.get_float("fall_strength", 0.01))
            )
        if mode == "横盘震荡":
            return "围绕基准价 ±{:.1%} 摆动，周期 {} 个 tick".format(
                abs(self.settings.get_float("sideways_amplitude", 0.03)),
                self.settings.get_int("sideways_period", 20),
            )
        if mode == "目标涨跌":
            return "{} 分钟内推向今日目标 {:+.1f}%".format(
                self.settings.get_float("target_window_minutes", 10.0),
                self.settings.get_float("daily_target_pct", 5.0),
            )
        return "高斯随机游走，波动率 {:.2%}".format(
            abs(self.settings.get_float("random_volatility", 0.02))
        )

    # ------------------------------------------------------------ 命令
    @filter.command("mkt")
    async def mkt(self, event: AstrMessageEvent):
        args = event.message_str.split()
        user_id = str(event.get_sender_id())

        if len(args) < 2:
            yield event.plain_result(HELP_TEXT)
            return

        cmd = args[1].lower()

        # ---------------- 行情 ----------------
        if cmd in ("help", "?", "帮助"):
            yield event.plain_result(HELP_TEXT)
            return

        if cmd == "price":
            if len(args) > 2:
                sym = normalize_symbol(args[2])
                if sym in self.market.prices:
                    msg = "【{}】{}".format(sym, fmt_price(self.market.prices[sym]))
                    pct = self.market.daily_change_pct(sym)
                    msg += "\n今日 {:+.2f}%".format(pct)
                    yield event.plain_result(msg)
                else:
                    yield event.plain_result("未找到币种: {}".format(args[2]))
            else:
                lines = ["【实时价格】"]
                for sym in sorted(self.market.prices.keys()):
                    pct = self.market.daily_change_pct(sym)
                    lines.append("{}: {}  ({:+.2f}%)".format(sym, fmt_price(self.market.prices[sym]), pct))
                yield event.plain_result("\n".join(lines))
            return

        if cmd == "change":
            lines = ["【今日涨跌】基准 = 今日首个 K 线开盘价"]
            rows = sorted(self.market.prices.keys())
            if not rows:
                lines.append("暂无币种")
            for sym in rows:
                pct = self.market.daily_change_pct(sym)
                base = self.market.day_open.get(sym, 0.0)
                icon = "📈" if pct > 0 else ("📉" if pct < 0 else "➖")
                lines.append(
                    "{} {}: {}  基准 {}  ({:+.2f}%)  走势 {}".format(
                        icon, sym, fmt_price(self.market.prices[sym]), fmt_price(base), pct,
                        self.market.current_mode(sym),
                    )
                )
            lines.append("")
            lines.append("全局走势: {}（配置页: {}）".format(
                self.market.global_mode(),
                "已被命令覆盖" if self.market.mode_override else "生效中",
            ))
            yield event.plain_result("\n".join(lines))
            return

        if cmd == "coins":
            rows = self._coin_rows()
            if not rows:
                yield event.plain_result("暂无币种，用 /mkt add <币> <价格> [描述] 添加")
                return
            lines = ["🪙 币种列表"]
            for coin in rows:
                price = self.market.prices.get(coin.symbol, 0.0)
                lines.append("{} ({})  价格 {}".format(coin.symbol, coin.name or coin.symbol, fmt_price(price)))
            yield event.plain_result("\n".join(lines))
            return

        if cmd == "info":
            if len(args) > 2:
                coin = self.market.coin_info(normalize_symbol(args[2]))
                if coin is None:
                    yield event.plain_result("未找到币种: {}".format(args[2]))
                    return
                price = self.market.prices.get(coin.symbol, 0.0)
                msg = "【{} ({})】\n价格: {}\n今日: {:+.2f}%\n\n{}".format(
                    coin.symbol,
                    coin.name or coin.symbol,
                    fmt_price(price),
                    self.market.daily_change_pct(coin.symbol),
                    coin.desc or "（暂无介绍）",
                )
                yield event.plain_result(msg)
            else:
                lines = ["【币种资料】"]
                for coin in self._coin_rows():
                    lines.append("{} ({})\n  {}".format(coin.symbol, coin.name or coin.symbol, coin.desc or "（暂无介绍）"))
                yield event.plain_result("\n".join(lines) if len(lines) > 1 else "暂无币种")
            return

        if cmd == "time":
            msg = "【市场状态】\n当前: {}\n北京时间: {}".format(
                "🟢 开市中" if self.market.is_open else "🔴 休市中",
                now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if self.settings.get_bool("enforce_trading_hours", False):
                msg += "\n时段: {} / {}".format(
                    self.settings.get_str("session_morning", "09:30-11:30"),
                    self.settings.get_str("session_afternoon", "13:00-15:00"),
                )
            else:
                msg += "\n时段限制: 未启用（全天可交易）"
            msg += "\n全局走势: {}（{}）".format(
                self.market.global_mode(),
                "命令覆盖" if self.market.mode_override else "配置页",
            )
            msg += "\n走法: " + self._mode_hint()
            individual = [
                "{}={}".format(s, self.market.current_mode(s))
                for s in sorted(self.market.prices.keys())
                if self.market.coin_mode_source(s) != "全局配置"
                and self.market.coin_mode_source(s) != "全局命令"
            ]
            if individual:
                msg += "\n单独设置: " + ", ".join(individual)
            msg += "\n更新间隔: {} 秒".format(self.settings.get_int("tick_interval", 5))
            yield event.plain_result(msg)
            return

        if cmd == "kline":
            if len(args) < 3:
                yield event.plain_result("请输入币种，例如: /mkt kline ZRB")
                return
            sym = normalize_symbol(args[2])
            if sym not in self.market.prices:
                yield event.plain_result("未找到币种: {}".format(args[2]))
                return
            limit = max(10, min(300, self.settings.get_int("max_kline_points", 60)))
            session = self.db.get_session()
            try:
                rows = self.db.recent_history(session, sym, limit)
            finally:
                session.close()
            if not rows:
                yield event.plain_result("{} 暂无历史数据，等价格更新一波再试".format(sym))
                return
            if not self.settings.get_bool("enable_plot", True):
                lines = ["【{} 最近 {} 根收盘价】".format(sym, len(rows))]
                lines.append(", ".join(fmt_price(r.close) for r in rows[-30:]))
                yield event.plain_result("\n".join(lines))
                return
            buf = plotter.plot_kline(
                rows,
                title="{} 走势 ({})".format(sym, self.market.current_mode(sym)),
                font_path=self.settings.get_str("font_path", ""),
            )
            if buf is None:
                lines = ["【{} 最近收盘价】".format(sym)]
                lines.append(", ".join(fmt_price(r.close) for r in rows[-30:]))
                lines.append("（绘图依赖缺失，已降级为文字输出）")
                yield event.plain_result("\n".join(lines))
                return
            path = self._save_temp_image(buf)
            if path:
                yield event.image_result(path)
            else:
                yield event.plain_result("绘图保存失败")
            return

        # ---------------- 交易 ----------------
        if cmd in ("buy", "sell"):
            if len(args) < 4:
                yield event.plain_result("格式: /mkt {} <币> <数量> [价格]".format(cmd))
                return
            ok, message, _status = self.market.place_order(
                user_id, args[2], args[3], args[4] if len(args) > 4 else None, cmd
            )
            yield event.plain_result(("✅ " if ok else "❌ ") + message)
            return

        if cmd == "assets":
            balance, items, total = self.market.portfolio(user_id)
            lines = ["【我的资产】", "可用资金: {}".format(self._num(balance))]
            if items:
                lines.append("持仓:")
                for item in items:
                    lines.append(
                        "- {}: {} 枚  现价 {}  市值 {}".format(
                            item["symbol"],
                            self._num(item["amount"], 4),
                            fmt_price(item["price"]),
                            self._num(item["value"]),
                        )
                    )
                    if item["cost"] > 0:
                        lines.append(
                            "  盈亏: {}{} ({:+.2f}%)".format(
                                "+" if item["profit"] >= 0 else "",
                                self._num(item["profit"]),
                                item["profit_pct"],
                            )
                        )
                    else:
                        lines.append("  盈亏: -（无成本记录，可能是管理员赠股）")
            else:
                lines.append("持仓: 无")
            lines.append("总资产: {}".format(self._num(total)))
            yield event.plain_result("\n".join(lines))
            return

        if cmd == "orders":
            from .database import Order, OrderStatus
            session = self.db.get_session()
            try:
                rows = (
                    session.query(Order)
                    .filter_by(user_id=user_id, status=OrderStatus.PENDING)
                    .order_by(Order.id.desc())
                    .limit(20)
                    .all()
                )
                if not rows:
                    yield event.plain_result("暂无挂单")
                    return
                lines = ["【我的挂单】"]
                for o in rows:
                    lines.append(
                        "#{} {} {} {} @ {}".format(
                            o.id,
                            o.order_type.value.upper(),
                            o.symbol,
                            self._num(o.amount, 4),
                            fmt_price(o.price) if o.price else "市价",
                        )
                    )
                yield event.plain_result("\n".join(lines))
            finally:
                session.close()
            return

        if cmd == "cancel":
            from .database import Order, OrderStatus
            if len(args) < 3:
                yield event.plain_result("请输入订单ID，例如: /mkt cancel 3")
                return
            try:
                oid = int(args[2])
            except ValueError:
                yield event.plain_result("订单ID必须是数字")
                return
            session = self.db.get_session()
            try:
                order = (
                    session.query(Order)
                    .filter_by(id=oid, user_id=user_id, status=OrderStatus.PENDING)
                    .first()
                )
                if order is None:
                    yield event.plain_result("订单不存在或无法撤销")
                    return
                order.status = OrderStatus.CANCELLED
                session.commit()
                yield event.plain_result("✅ 订单 #{} 已撤销".format(oid))
            finally:
                session.close()
            return

        if cmd == "rank":
            session = self.db.get_session()
            try:
                users = session.query(User).all()
                rows = []
                for u in users:
                    total = float(u.balance or 0.0)
                    for h in self.db.list_holdings(session, u.user_id):
                        total += h.amount * self.market.prices.get(h.symbol, 0.0)
                    rows.append((u.user_id, total))
            finally:
                session.close()
            rows.sort(key=lambda x: x[1], reverse=True)
            lines = ["🏆 资产排行"]
            for idx, (uid, total) in enumerate(rows[:10], 1):
                lines.append("{}. {}  {}".format(idx, uid, self._num(total)))
            yield event.plain_result("\n".join(lines) if rows else "暂无数据")
            return

        # ---------------- 账户 / Web ----------------
        if cmd == "register":
            if len(args) < 3:
                yield event.plain_result("请设置密码: /mkt register <您的密码>（至少 6 位）")
                return
            password = args[2]
            if len(password) < 6:
                yield event.plain_result("密码长度至少需要 6 位")
                return
            if pwd_context is None:
                yield event.plain_result("服务端缺少 passlib 依赖，无法设置密码")
                return
            self.db.ensure_user(user_id, self.settings.get_float("initial_balance", 10000.0))
            self.db.set_password(user_id, pwd_context.hash(password))
            yield event.plain_result(
                "✅ Web 交易端账号已开通\n账号: {}\n密码: {}（请自行保管）\n登录地址: {}".format(
                    user_id, password, self._web_url()
                )
            )
            return

        if cmd == "web":
            yield event.plain_result(
                "🌐 Web 交易端地址: {}\n账号: 你的 QQ / 群号\n首次使用请先 /mkt register <密码>".format(
                    self._web_url()
                )
            )
            return

        if cmd == "lev":
            yield event.plain_result(
                "【合约规则】\n"
                "杠杆范围: 1 - {}x（逐仓）\n"
                "开仓手续费: {:.3%}\n"
                "维持保证金率: {:.2%}\n"
                "强平价（多）: 开仓价 × (1 + 维持率 - 1/杠杆)\n"
                "强平价（空）: 开仓价 × (1 - 维持率 + 1/杠杆)\n"
                "最大持仓数: {} 个\n"
                "用法: /mkt long <币> <保证金> [杠杆]".format(
                    self.margin.max_leverage(),
                    self.margin.fee_rate(),
                    self.margin.mmr(),
                    self.margin.max_positions(),
                )
            )
            return

        # ---------------- 合约 ----------------
        if cmd in ("long", "short"):
            if len(args) < 4:
                yield event.plain_result(
                    "格式: /mkt {} <币> <保证金> [杠杆1-125]".format(cmd)
                )
                return
            side = SIDE_LONG if cmd == "long" else SIDE_SHORT
            leverage = args[4] if len(args) > 4 else self.settings.get_int("default_leverage", 20)
            ok, message, _pos = self.margin.open_position(user_id, args[2], side, args[3], leverage)
            yield event.plain_result(("✅ " if ok else "❌ ") + message)
            return

        if cmd == "pos":
            acc = self.margin.account(user_id)
            if not acc["positions"]:
                yield event.plain_result("暂无合约持仓")
                return
            lines = ["【我的合约持仓】"]
            for p in acc["positions"]:
                lines.append(
                    "#{} {} {} {}x  数量 {}  开仓 {}  标记 {}".format(
                        p["id"],
                        p["symbol"],
                        "做多" if p["side"] == SIDE_LONG else "做空",
                        p["leverage"],
                        self._num(p["qty"], 4),
                        fmt_price(p["entry_price"]),
                        fmt_price(p["mark_price"]),
                    )
                )
                lines.append(
                    "   保证金 {}  盈亏 {}{} ({:+.2f}%)  强平 {}".format(
                        self._num(p["margin"]),
                        "+" if p["pnl"] >= 0 else "",
                        self._num(p["pnl"]),
                        p["roe"],
                        fmt_price(p["liq_price"]),
                    )
                )
            lines.append("")
            lines.append(
                "可用 {}  占用保证金 {}  未实现 {}  权益 {}".format(
                    self._num(acc["available"]),
                    self._num(acc["used_margin"]),
                    self._num(acc["unrealized_pnl"]),
                    self._num(acc["equity"]),
                )
            )
            yield event.plain_result("\n".join(lines))
            return

        if cmd == "close":
            if len(args) < 3:
                yield event.plain_result("格式: /mkt close <仓位ID> [比例0-1]")
                return
            try:
                pid = int(args[2])
            except ValueError:
                yield event.plain_result("仓位ID必须是数字（可用 /mkt pos 查看）")
                return
            ratio = 1.0
            if len(args) > 3:
                try:
                    ratio = float(args[3])
                except ValueError:
                    ratio = 1.0
            ok, message, _r = self.margin.close_position(user_id, pid, ratio)
            yield event.plain_result(("✅ " if ok else "❌ ") + message)
            return

        if cmd == "closeall":
            results = self.margin.close_all(user_id)
            if not results:
                yield event.plain_result("暂无持仓")
                return
            okn = sum(1 for r in results if r[0])
            pnl = sum((r[2] or {}).get("realized_pnl", 0.0) for r in results if r[0])
            yield event.plain_result(
                "✅ 已平仓 {} 个仓位，合计实现盈亏 {}{}".format(
                    okn, "+" if pnl >= 0 else "", self._num(pnl)
                )
            )
            return

        # ---------------- 管理 ----------------
        admin_cmds = {
            "give", "take", "pay", "transfer", "grant", "revoke", "sendstock",
            "add", "del", "setprice", "mode", "mopen", "mclose", "reset", "log",
            "resetprice",
        }
        if cmd in admin_cmds and not self._is_admin(user_id):
            yield event.plain_result("权限不足：该命令仅限管理员（配置页 admin_ids）")
            return

        initial = self.settings.get_float("initial_balance", 10000.0)

        if cmd == "give":
            if len(args) < 4:
                yield event.plain_result("格式: /mkt give <用户ID> <金额>")
                return
            target, amount = args[2], args[3]
            try:
                amount = float(amount)
            except ValueError:
                yield event.plain_result("金额必须是数字")
                return
            if amount <= 0:
                yield event.plain_result("金额必须大于 0")
                return
            session = self.db.get_session()
            try:
                user, ok = self.db.adjust_balance(session, target, amount, initial)
                session.commit()
                balance = user.balance
            finally:
                session.close()
            self.db.write_log(user_id, "give", target, "+{}".format(amount))
            yield event.plain_result(
                "✅ 已给 {} 增加 {}，当前余额 {}".format(target, self._num(amount), self._num(balance))
            )
            return

        if cmd == "take":
            if len(args) < 4:
                yield event.plain_result("格式: /mkt take <用户ID> <金额>")
                return
            target = args[2]
            try:
                amount = float(args[3])
            except ValueError:
                yield event.plain_result("金额必须是数字")
                return
            if amount <= 0:
                yield event.plain_result("金额必须大于 0")
                return
            session = self.db.get_session()
            try:
                user, ok = self.db.adjust_balance(session, target, -amount, initial)
                if not ok:
                    session.rollback()
                    yield event.plain_result("余额不足，扣款失败")
                    return
                session.commit()
                balance = user.balance
            finally:
                session.close()
            self.db.write_log(user_id, "take", target, "-{}".format(amount))
            yield event.plain_result(
                "✅ 已从 {} 扣除 {}，当前余额 {}".format(target, self._num(amount), self._num(balance))
            )
            return

        if cmd in ("pay", "transfer"):
            # pay: 自己 -> 目标；transfer: 源 -> 目标
            if cmd == "pay":
                if len(args) < 4:
                    yield event.plain_result("格式: /mkt pay <用户ID> <金额>")
                    return
                src, target, amount_text = user_id, args[2], args[3]
            else:
                if len(args) < 5:
                    yield event.plain_result("格式: /mkt transfer <源用户> <目标用户> <金额>")
                    return
                src, target, amount_text = args[2], args[3], args[4]
            try:
                amount = float(amount_text)
            except ValueError:
                yield event.plain_result("金额必须是数字")
                return
            if amount <= 0:
                yield event.plain_result("金额必须大于 0")
                return
            if str(src) == str(target):
                yield event.plain_result("源账户和目标账户不能相同")
                return
            session = self.db.get_session()
            try:
                _, ok = self.db.adjust_balance(session, src, -amount, initial)
                if not ok:
                    session.rollback()
                    yield event.plain_result("源账户余额不足，转账失败")
                    return
                self.db.adjust_balance(session, target, amount, initial)
                session.commit()
                src_balance = session.query(User).filter_by(user_id=str(src)).first().balance
                dst_balance = session.query(User).filter_by(user_id=str(target)).first().balance
            finally:
                session.close()
            self.db.write_log(
                user_id,
                "pay" if cmd == "pay" else "transfer",
                src,
                "-> {} : {}".format(target, amount),
            )
            yield event.plain_result(
                "✅ 转账完成\n{} -> {}  {}\n源余额 {} | 目标余额 {}".format(
                    src, target, self._num(amount), self._num(src_balance), self._num(dst_balance)
                )
            )
            return

        if cmd in ("grant", "revoke"):
            if len(args) < 5:
                yield event.plain_result("格式: /mkt {} <用户ID> <币> <数量>".format(cmd))
                return
            target = args[2]
            sym = normalize_symbol(args[3])
            if sym not in self.market.prices:
                yield event.plain_result("未找到币种: {}".format(args[3]))
                return
            try:
                amount = float(args[4])
            except ValueError:
                yield event.plain_result("数量必须是数字")
                return
            if amount <= 0:
                yield event.plain_result("数量必须大于 0")
                return
            delta = amount if cmd == "grant" else -amount
            if cmd == "grant":
                new_amount, ok = self.market.grant_holding(target, sym, delta)
            else:
                new_amount, ok = self.market.revoke_holding(target, sym, -delta)
            if not ok:
                yield event.plain_result("目标账户持仓不足，操作失败")
                return
            self.db.write_log(user_id, cmd, target, "{} {}".format(delta, sym))
            verb = "增加" if cmd == "grant" else "扣除"
            yield event.plain_result(
                "✅ 已给 {} {} {} {}，现持有 {} 枚".format(
                    target, verb, self._num(amount, 4), sym, self._num(new_amount, 4)
                )
            )
            return

        if cmd == "sendstock":
            if len(args) < 6:
                yield event.plain_result("格式: /mkt sendstock <源用户> <目标用户> <币> <数量>")
                return
            src, target = args[2], args[3]
            sym = normalize_symbol(args[4])
            if sym not in self.market.prices:
                yield event.plain_result("未找到币种: {}".format(args[4]))
                return
            try:
                amount = float(args[5])
            except ValueError:
                yield event.plain_result("数量必须是数字")
                return
            if amount <= 0:
                yield event.plain_result("数量必须大于 0")
                return
            if str(src) == str(target):
                yield event.plain_result("源账户和目标账户不能相同")
                return
            result = self.market.move_holding(src, target, sym, amount)
            if result is None:
                yield event.plain_result("源账户持仓不足，转股失败")
                return
            src_amount, dst_amount = result
            self.db.write_log(
                user_id, "sendstock", src, "-> {} : {} {}".format(target, amount, sym)
            )
            yield event.plain_result(
                "✅ 转股完成\n{} -> {}  {} {} 枚\n源持有 {} | 目标持有 {}".format(
                    src, target, self._num(amount, 4), sym, self._num(src_amount, 4), self._num(dst_amount, 4)
                )
            )
            return

        if cmd == "add":
            if len(args) < 4:
                yield event.plain_result("格式: /mkt add <币> <价格> [描述]")
                return
            sym = normalize_symbol(args[2])
            if not sym:
                yield event.plain_result("币种代码不能为空")
                return
            if sym in self.market.prices:
                yield event.plain_result("币种已存在: {}（用 /mkt setprice 改价）".format(sym))
                return
            try:
                price = float(args[3])
            except ValueError:
                yield event.plain_result("价格必须是数字")
                return
            if price <= 0:
                yield event.plain_result("价格必须大于 0")
                return
            desc = " ".join(args[4:]) if len(args) > 4 else ""
            self.market.add_coin(sym, price, name=sym, desc=desc, enabled=True)
            wrote = self.settings.append_coin_to_file(sym, price, name=sym, desc=desc)
            self.db.write_log(user_id, "add_coin", sym, "price={}".format(price))
            yield event.plain_result(
                "✅ 已添加币种 {}，初始价格 {}{}\n{}".format(
                    sym,
                    fmt_price(price),
                    "（已同步到配置页）" if wrote else "",
                    "描述: {}".format(desc) if desc else "（无描述，可用 /mkt add 重新补充或改配置页）",
                )
            )
            return

        if cmd == "del":
            if len(args) < 3:
                yield event.plain_result("格式: /mkt del <币>")
                return
            sym = normalize_symbol(args[2])
            if sym not in self.market.prices:
                yield event.plain_result("未找到币种: {}".format(args[2]))
                return
            self.db.delete_coin(sym)
            self.settings.remove_coin_from_file(sym)
            self.market.remove_coin(sym)
            self.db.write_log(user_id, "del_coin", sym, "")
            yield event.plain_result("✅ 已删除币种 {}（相关持仓与历史保留在数据库中）".format(sym))
            return

        if cmd == "setprice":
            if len(args) < 4:
                yield event.plain_result("格式: /mkt setprice <币> <价格>")
                return
            sym = normalize_symbol(args[2])
            if sym not in self.market.prices:
                yield event.plain_result("未找到币种: {}".format(args[2]))
                return
            try:
                price = float(args[3])
            except ValueError:
                yield event.plain_result("价格必须是数字")
                return
            if price <= 0:
                yield event.plain_result("价格必须大于 0")
                return
            self.market.set_price(sym, price)
            self.db.write_log(user_id, "setprice", sym, str(price))
            yield event.plain_result("✅ {} 价格已设为 {}".format(sym, fmt_price(price)))
            return

        if cmd == "mode":
            # /mkt mode                查看
            # /mkt mode <模式>          全局临时覆盖
            # /mkt mode <模式> <币>      只改这一个币种（持久化，写回配置页）
            # /mkt mode auto [币]       清除覆盖
            if len(args) < 3:
                lines = ["【当前走势】全局: {}（来源: {}）".format(
                    self.market.global_mode(),
                    "全局命令覆盖" if self.market.mode_override else "配置页",
                )]
                for sym in sorted(self.market.prices.keys()):
                    src = self.market.coin_mode_source(sym)
                    lines.append("  {}: {}  [{}]".format(sym, self.market.current_mode(sym), src))
                lines.append("")
                lines.append("走法: " + self._mode_hint())
                lines.append("可选: {}".format(" / ".join(MODES)))
                lines.append("用法:")
                lines.append("  /mkt mode <模式>          全部一起变")
                lines.append("  /mkt mode <模式> <币>      只改单个币种")
                lines.append("  /mkt mode auto [币]       恢复配置")
                yield event.plain_result("\n".join(lines))
                return

            text = args[2].strip()
            target_sym = args[3] if len(args) > 3 else None
            if target_sym:
                sym = normalize_symbol(target_sym)
                if sym not in self.market.prices:
                    yield event.plain_result("未找到币种: {}".format(target_sym))
                    return

            if text.lower() in ("auto", "reset", "default", "自动"):
                self.market.clear_mode_override(target_sym)
                if target_sym:
                    yield event.plain_result(
                        "✅ {} 已恢复为配置页设置：{}".format(
                            normalize_symbol(target_sym), self.market.current_mode(target_sym)
                        )
                    )
                else:
                    yield event.plain_result("✅ 已恢复全局配置（实时生效）")
                return

            target = self.market.set_mode(text, symbol=target_sym)
            if not target:
                yield event.plain_result("未知模式。可选: {}".format(" / ".join(MODES)))
                return
            if target_sym:
                self.db.write_log(user_id, "set_mode_coin", normalize_symbol(target_sym), target)
                yield event.plain_result(
                    "✅ {} 的走势已单独设为: {}（已写回配置页，其他币种不受影响）".format(
                        normalize_symbol(target_sym), target
                    )
                )
            else:
                self.db.write_log(user_id, "set_mode", target, "global")
                yield event.plain_result(
                    "✅ 全局走势已切换为: {}（所有未单独设置的币种跟随；重启或跨日回到配置值）".format(target)
                )
            return

        if cmd == "resetprice":
            changed = self.market.reset_all_prices()
            self.db.write_log(user_id, "resetprice", "all", "{} coins".format(len(changed)))
            if not changed:
                yield event.plain_result("没有可重置的币种")
                return
            lines = ["✅ 已重置 {} 个币种价格:".format(len(changed))]
            for item in changed:
                lines.append(
                    "  {}: {} -> {}".format(
                        item["symbol"], fmt_price(item["old"]), fmt_price(item["new"])
                    )
                )
            lines.append("（K 线与今日涨跌基准已同步重置）")
            yield event.plain_result("\n".join(lines))
            return

        if cmd in ("mopen", "mclose"):
            self.market.set_open(cmd == "mopen")
            self.db.write_log(user_id, cmd, "market", "")
            yield event.plain_result("✅ 市场已{}".format("开市" if cmd == "mopen" else "休市"))
            return

        if cmd == "reset":
            if len(args) < 3:
                yield event.plain_result("格式: /mkt reset <用户ID>")
                return
            target = args[2]
            session = self.db.get_session()
            try:
                from .database import Holding, Order as OrderModel
                user, _ = self.db.adjust_balance(session, target, 0.0, initial)
                user.balance = initial
                session.query(Holding).filter_by(user_id=str(target)).delete(synchronize_session=False)
                session.query(OrderModel).filter_by(user_id=str(target)).delete(synchronize_session=False)
                session.commit()
            finally:
                session.close()
            self.db.write_log(user_id, "reset", target, "balance={}".format(initial))
            yield event.plain_result("✅ 已重置 {} 的账户（余额 {}）".format(target, self._num(initial)))
            return

        if cmd == "log":
            session = self.db.get_session()
            try:
                rows = session.query(AdminLog).order_by(AdminLog.id.desc()).limit(10).all()
                if not rows:
                    yield event.plain_result("暂无管理日志")
                    return
                lines = ["【最近管理操作】"]
                for r in rows:
                    lines.append(
                        "{} {} {} -> {} {}".format(
                            r.created_at.strftime("%m-%d %H:%M") if r.created_at else "-",
                            r.operator,
                            r.action,
                            r.target,
                            r.detail or "",
                        )
                    )
                yield event.plain_result("\n".join(lines))
            finally:
                session.close()
            return

        yield event.plain_result("未知命令: {}（/mkt help 查看帮助）".format(cmd))
