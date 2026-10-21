# -*- coding: utf-8 -*-
"""价格引擎：由配置页的“走势模式”驱动。

支持模式：
    上涨        每 tick 稳定上涨（幅度 = rise_strength）
    下跌        每 tick 稳定下跌（幅度 = fall_strength）
    横盘震荡    围绕基准价正弦摆动（幅度 = sideways_amplitude，周期 = sideways_period）
    连续上涨    连续 rally_ticks 个 tick，每个 tick 涨 rally_step，结束后切到 after_trend_mode
    连续下跌    连续 dump_ticks 个 tick，每个 tick 跌 dump_step，结束后切到 after_trend_mode
    自由随机    高斯随机游走（波动率 = random_volatility）
    目标涨跌    自动对齐到“今日目标涨跌幅”（daily_target_pct）
"""
import math
import random
import threading
import time
from datetime import datetime, timedelta

try:
    from .database import (
        Coin,
        MarketHistory,
        Order,
        OrderStatus,
        OrderType,
        User,
        now_cn,
        normalize_symbol,
    )
except ImportError:  # 兼容直接运行
    from database import (
        Coin,
        MarketHistory,
        Order,
        OrderStatus,
        OrderType,
        User,
        now_cn,
        normalize_symbol,
    )

MODES = ("上涨", "下跌", "横盘震荡", "连续上涨", "连续下跌", "自由随机", "目标涨跌")

MODE_ALIASES = {
    "上涨": "上涨", "涨": "上涨", "up": "上涨", "rise": "上涨", "bull": "上涨",
    "下跌": "下跌", "跌": "下跌", "down": "下跌", "fall": "下跌", "bear": "下跌",
    "横盘震荡": "横盘震荡", "横盘": "横盘震荡", "震荡": "横盘震荡",
    "sideways": "横盘震荡", "flat": "横盘震荡", "range": "横盘震荡",
    "连续上涨": "连续上涨", "连涨": "连续上涨", "rally": "连续上涨",
    "连续下跌": "连续下跌", "连跌": "连续下跌", "dump": "连续下跌",
    "自由随机": "自由随机", "随机": "自由随机", "random": "自由随机",
    "目标涨跌": "目标涨跌", "目标": "目标涨跌", "target": "目标涨跌",
}


def normalize_mode(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in MODE_ALIASES:
        return MODE_ALIASES[text]
    return MODE_ALIASES.get(text.lower())


def fmt_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    if value >= 100:
        return "{:.2f}".format(value)
    if value >= 1:
        return "{:.3f}".format(value)
    return "{:.4f}".format(value)


class MarketEngine:
    def __init__(self, db, settings, logger=None):
        self.db = db
        self.s = settings
        self.logger = logger
        self.lock = threading.RLock()
        self.prices = {}
        self.candles = {}
        self.state = {}
        self.day_open = {}
        self.running = False
        self._thread = None
        self.is_open = True
        self.open_override = None  # 管理命令强制开/关市
        self.mode_override = None  # 全局走势临时覆盖
        self.mode_overrides = {}  # 单个币种的走势覆盖 {symbol: mode}
        self.coin_mode_cfg = {}  # 配置页/数据库里每个币种自己的模式
        self._cfg_price = {}  # 上一次生效的「配置页价格」，用于检测价格是否被改动
        self._applied_price = self.db.get_applied_prices()  # 跨重启记录：配置价是否已应用过
        self._forced = set()  # 本次被配置价格强制改价的币种
        self._today = None
        self._last_tick = time.time()
        self._last_prune = 0.0
        self._last_error = ""
        self.margin_engine = None  # 由 main.py 注入，用于每 tick 强平检查
        self.liquidation_events = []  # 供命令层/Web 端取用

        self.sync_coins()
        self._reset_daily(force=True)
        self.s.add_reload_hook(self._on_config_reload)

    # ------------------------------------------------------------------ 日志
    def _log(self, msg):
        self._last_error = msg
        if self.logger:
            try:
                self.logger.warning(msg)
                return
            except Exception:
                pass
        print(msg)

    # ------------------------------------------------------------------ 币种
    def sync_coins(self):
        """把配置页 / 数据库里的币种同步进内存价格表。"""
        with self.lock:
            session = self.db.get_session()
            try:
                # 0) 本次启动时内存里还没有的币种 = 冷启动新加载
                was_in_memory = set(self.prices.keys())

                # 1) 配置页新增的币种落库（已存在的不会被重置价格）
                for item in self.s.get_coins():
                    coin = session.query(Coin).filter_by(symbol=item["symbol"]).first()
                    if coin is None:
                        coin = Coin(
                            symbol=item["symbol"],
                            name=item["name"],
                            desc=item["desc"],
                            base_price=float(item["price"]),
                            enabled=bool(item["enabled"]),
                            mode=item.get("mode") or "",
                        )
                        session.add(coin)
                    else:
                        if item.get("name"):
                            coin.name = item["name"]
                        if item.get("desc"):
                            coin.desc = item["desc"]
                        if item.get("mode"):
                            coin.mode = item["mode"]
                        coin.enabled = bool(item["enabled"])
                session.commit()

                # 2) 首次运行且没有任何币种时，给两个示例币
                if session.query(Coin).count() == 0:
                    session.add(
                        Coin(symbol="ZRB", name="孜然币", desc="本插件的基础货币，烤肉的灵魂。", base_price=100.0)
                    )
                    session.add(
                        Coin(symbol="STAR", name="星星币", desc="来自遥远星系的神秘货币。", base_price=50.0)
                    )
                    session.commit()

                # 3) 构建价格表
                coins = session.query(Coin).filter(Coin.enabled.is_(True)).all()
                min_price = max(0.0001, self.s.get_float("min_price", 0.01))
                cfg_prices = {item["symbol"]: item["price"] for item in self.s.get_coins()}
                use_cfg_price = self.s.get_bool("config_price_priority", True)
                new_prices = {}
                new_modes = {}
                forced = set()
                for coin in coins:
                    sym = coin.symbol
                    last = self.db.last_close(session, sym)
                    cfg_price = cfg_prices.get(sym)
                    target = None
                    has_history = last is not None and last > 0
                    applied = getattr(coin, "applied_price", None)
                    applied = float(applied) if applied is not None else None
                    first_seen = sym not in self._applied_price
                    in_memory = sym in self.prices
                    cold_start = sym not in was_in_memory  # 本次启动新加载的币种

                    if cfg_price and use_cfg_price:
                        prev = self._cfg_price.get(sym)
                        changed_in_session = (
                            prev is not None and abs(float(prev) - float(cfg_price)) > 1e-9
                        )
                        changed_since_apply = (
                            applied is not None and abs(applied - float(cfg_price)) > 1e-9
                        )
                        # 判定是否要用「配置价」覆盖当前价：
                        #   1) 全新币种（没历史也没应用记录）
                        #   2) 本次运行期间配置价被改过
                        #   3) 配置价与上次已应用的值不同
                        #   4) 冷启动且历史里没有「接近配置价」的记录 —— 说明改价后没跑 tick，
                        #      历史里只有旧价，需要重建到新配置价
                        near_cfg = self.db.has_close_near(session, sym, float(cfg_price), 0.02)
                        hit = (
                            (not has_history and applied is None)
                            or changed_in_session
                            or changed_since_apply
                            # 冷启动：历史里没有「配置价附近」的 K 线（改价后没跑过 tick），
                            # 需要把价格重建到配置价
                            or (
                                cold_start
                                and not in_memory
                                and applied is not None
                                and abs(applied - float(cfg_price)) < 1e-9
                                and has_history
                                and not near_cfg
                            )
                        )
                        if hit:
                            target = float(cfg_price)
                            forced.add(sym)

                    if cfg_price:
                        self._cfg_price[sym] = float(cfg_price)

                    if target is None:
                        target = last if last and last > 0 else (cfg_price or coin.base_price or 100.0)
                    new_prices[sym] = max(min_price, float(target))
                    new_modes[sym] = normalize_mode(coin.mode) or ""
                    if sym not in self.candles:
                        self.candles[sym] = self._new_candle(self.prices.get(sym, target))
                    if sym not in self.state:
                        self.state[sym] = self._new_state(sym, target)

                self.coin_mode_cfg = new_modes
                # 删除已禁用/不存在的币种
                for sym in list(self.prices.keys()):
                    if sym not in new_prices:
                        self.prices.pop(sym, None)
                        self.candles.pop(sym, None)
                        self.state.pop(sym, None)
                        self.mode_overrides.pop(sym, None)
                        self.day_open.pop(sym, None)
                self._forced |= forced
                for sym, price in new_prices.items():
                    if sym in forced:
                        # 配置价格生效：同步 K 线与今日基准，避免曲线断崖
                        self.prices[sym] = price
                        self.candles[sym] = self._new_candle(price)
                        self.state[sym] = self._new_state(sym, price)
                        self.day_open[sym] = price
                    else:
                        # 已有该币种时不要用数据库收盘价覆盖内存现价
                        if sym not in self.prices:
                            self.prices[sym] = price
                            self.candles[sym] = self._new_candle(price)
                            self.state[sym] = self._new_state(sym, price)
                            self.day_open[sym] = price
                # 记录「已应用过的配置价格」，跨重启持久化到 DB，用来判断配置价格是否变过
                for sym in forced:
                    cfg_price = cfg_prices.get(sym)
                    if cfg_price is not None:
                        self._applied_price[sym] = float(cfg_price)
                        try:
                            self.db.set_applied_price(sym, float(cfg_price))
                        except Exception as e:
                            self._log("[market] save applied price failed: {}".format(e))
                return sorted(new_prices.keys())
            finally:
                session.close()

    def _new_candle(self, price):
        price = float(price)
        return {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0.0,
            "start_time": now_cn(),
        }

    def _new_state(self, sym, price):
        return {
            "mode": None,
            "base_mode": None,
            "finished": False,
            "phase": 0.0,
            "anchor": float(price),
            "remain": None,
        }

    def coins(self):
        session = self.db.get_session()
        try:
            return self.db.list_coins(session, only_enabled=True)
        finally:
            session.close()

    def coin_info(self, symbol):
        session = self.db.get_session()
        try:
            return self.db.get_coin(session, symbol)
        finally:
            session.close()

    # ------------------------------------------------------------------ 每日
    def _reset_daily(self, force=False):
        today = now_cn().date()
        if not force and self._today == today:
            return
        self._today = today
        self.mode_override = None
        session = self.db.get_session()
        try:
            day_start = datetime.combine(today, datetime.min.time())
            for sym in list(self.prices.keys()):
                if sym in self._forced:
                    # 价格由配置页强制指定，今日基准就用它
                    self.day_open[sym] = float(self.prices[sym])
                else:
                    base = self.db.day_open_price(session, sym, day_start)
                    if base and base > 0:
                        self.day_open[sym] = float(base)
                    else:
                        self.day_open[sym] = float(self.prices[sym])
                self.state[sym] = self._new_state(sym, self.prices[sym])
        finally:
            session.close()
            self._forced.clear()

    def daily_change_pct(self, symbol):
        sym = normalize_symbol(symbol)
        price = self.prices.get(sym)
        base = self.day_open.get(sym)
        if not price or not base:
            return 0.0
        return (price - base) / base * 100.0

    # ------------------------------------------------------------------ 模式
    def global_mode(self):
        return normalize_mode(self.s.get_str("market_mode", "横盘震荡")) or "横盘震荡"

    def current_mode(self, symbol=None):
        """解析某个币种当前实际生效的走势。

        优先级：单币种管理命令覆盖 > 单币种配置页设置 > 全局管理命令覆盖 > 全局配置
        """
        sym = normalize_symbol(symbol) if symbol else ""
        if sym:
            if sym in self.mode_overrides:
                return self.mode_overrides[sym]
            configured = self.coin_mode_cfg.get(sym) or ""
            if configured:
                return configured
        if self.mode_override:
            return self.mode_override
        return self.global_mode()

    def coin_mode_source(self, symbol):
        """返回该币种模式的来源，用于界面显示。"""
        sym = normalize_symbol(symbol)
        if sym in self.mode_overrides:
            return "单独指定"
        if self.coin_mode_cfg.get(sym):
            return "配置页"
        if self.mode_override:
            return "全局命令"
        return "全局配置"

    def set_mode(self, mode, symbol=None, persist=True):
        """设置走势。symbol=None 表示全局临时覆盖；指定币种则持久化到数据库。"""
        target = normalize_mode(mode)
        if not target:
            return None
        sym = normalize_symbol(symbol) if symbol else ""
        if sym:
            if sym not in self.prices:
                return None
            self.mode_overrides[sym] = target
            self.coin_mode_cfg[sym] = target
            if persist:
                self.db.set_coin_mode(sym, target)
                self.s.set_coin_mode_in_file(sym, target)
            with self.lock:
                self.state[sym] = self._new_state(sym, self.prices.get(sym, 1.0))
            return target
        self.mode_override = target
        with self.lock:
            for s in self.state:
                self.state[s] = self._new_state(s, self.prices.get(s, 1.0))
        return target

    def clear_mode_override(self, symbol=None, persist=True):
        """清除覆盖。指定币种则回到配置页设置，不指定则清除全局临时覆盖。"""
        sym = normalize_symbol(symbol) if symbol else ""
        if sym:
            self.mode_overrides.pop(sym, None)
            self.coin_mode_cfg.pop(sym, None)
            if persist:
                self.db.set_coin_mode(sym, "")
                self.s.set_coin_mode_in_file(sym, "")
            with self.lock:
                if sym in self.prices:
                    self.state[sym] = self._new_state(sym, self.prices.get(sym, 1.0))
            return True
        self.mode_override = None
        with self.lock:
            for s in self.state:
                self.state[s] = self._new_state(s, self.prices.get(s, 1.0))
        return True

    def mode_status(self, symbol=None):
        """返回 (当前生效模式, 该币种/全局的基准模式)。"""
        sym = normalize_symbol(symbol) if symbol else ""
        if sym:
            base = self.coin_mode_cfg.get(sym) or self.global_mode()
        else:
            base = self.global_mode()
        return self.current_mode(symbol), base

    def mode_map(self):
        """所有币种当前生效的走势，供 Web 端展示。"""
        return {sym: self.current_mode(sym) for sym in self.prices}

    def _trend_ticks(self, mode):
        if mode == "连续上涨":
            ticks = self.s.get_int("rally_ticks", 15)
            return ticks if ticks > 0 else None
        if mode == "连续下跌":
            ticks = self.s.get_int("dump_ticks", 15)
            return ticks if ticks > 0 else None
        return None

    def _effective_mode(self, sym, st):
        """根据「基础模式 + 连涨/连跌是否跑完」决定本 tick 的实际模式。"""
        base_mode = self.current_mode(sym)
        if st.get("base_mode") != base_mode:
            # 配置/管理命令切换了模式：重置该币种的走势状态
            st["base_mode"] = base_mode
            st["finished"] = False
            st["remain"] = self._trend_ticks(base_mode)
            st["phase"] = 0.0
            st["anchor"] = float(self.prices[sym])
            st["mode"] = None

        mode = base_mode
        if base_mode in ("连续上涨", "连续下跌") and st.get("finished"):
            mode = normalize_mode(self.s.get_str("after_trend_mode", "横盘震荡")) or "横盘震荡"
            if mode in ("连续上涨", "连续下跌"):
                mode = "横盘震荡"
        return mode

    # ------------------------------------------------------------------ 价格
    def _change_pct(self, sym, mode, st):
        noise = abs(self.s.get_float("noise", 0.002))

        if mode == "上涨":
            base = abs(self.s.get_float("rise_strength", 0.01))
            return base * random.uniform(0.6, 1.4)

        if mode == "下跌":
            base = abs(self.s.get_float("fall_strength", 0.01))
            return -base * random.uniform(0.6, 1.4)

        if mode == "横盘震荡":
            amp = abs(self.s.get_float("sideways_amplitude", 0.03))
            period = max(2, self.s.get_int("sideways_period", 20))
            st["phase"] = (float(st.get("phase", 0.0)) + 1.0) % period
            wave = math.sin(2.0 * math.pi * st["phase"] / period)
            anchor = float(st.get("anchor") or self.prices[sym])
            target = anchor * (1.0 + amp * wave)
            price = self.prices[sym]
            if price <= 0:
                return 0.0
            return (target - price) / price + random.gauss(0.0, noise)

        if mode == "连续上涨":
            step = abs(self.s.get_float("rally_step", 0.02))
            return step * random.uniform(0.7, 1.3)

        if mode == "连续下跌":
            step = abs(self.s.get_float("dump_step", 0.02))
            return -step * random.uniform(0.7, 1.3)

        if mode == "目标涨跌":
            return self._target_change(sym) + random.gauss(0.0, noise)

        vol = abs(self.s.get_float("random_volatility", 0.02))
        return random.gauss(0.0, vol)

    def _target_change(self, sym):
        """把价格朝「今日目标涨跌幅」平滑推进，target_window_minutes 内基本到位。"""
        target = self.s.get_float("daily_target_pct", 5.0) / 100.0
        base = self.day_open.get(sym) or self.prices[sym]
        if base <= 0:
            return 0.0
        current = (self.prices[sym] - base) / base
        interval = max(1, self.s.get_int("tick_interval", 5))
        window_ticks = max(1, int(self.s.get_float("target_window_minutes", 30.0) * 60 / interval))
        return (target - current) / window_ticks

    def _step(self, sym):
        st = self.state.setdefault(sym, self._new_state(sym, self.prices[sym]))
        mode = self._effective_mode(sym, st)
        if st.get("mode") != mode:
            st["mode"] = mode
            st["phase"] = 0.0
            st["anchor"] = float(self.prices[sym])
            st["remain"] = self._trend_ticks(mode)

        pct = self._change_pct(sym, mode, st)
        limit = abs(self.s.get_float("max_tick_change", 0.2))
        pct = max(-limit, min(limit, pct))
        min_price = max(0.0001, self.s.get_float("min_price", 0.01))
        price = max(min_price, self.prices[sym] * (1.0 + pct))
        self.prices[sym] = price

        candle = self.candles.setdefault(sym, self._new_candle(price))
        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)
        candle["close"] = price

        if st.get("remain") is not None:
            if st["remain"] > 0:
                st["remain"] -= 1
            if st["remain"] <= 0:
                st["finished"] = True

    # ------------------------------------------------------------------ 时段
    def _in_session(self, now=None):
        now = now or now_cn()
        if now.weekday() >= 5:
            return False
        current = now.strftime("%H:%M")
        for key in ("session_morning", "session_afternoon"):
            text = self.s.get_str(key, "")
            if "-" not in text:
                continue
            start, _, end = text.partition("-")
            start, end = start.strip(), end.strip()
            if start and end and start <= current <= end:
                return True
        return False

    def _refresh_open_state(self):
        if self.open_override is not None:
            self.is_open = bool(self.open_override)
            return
        if not self.s.get_bool("enforce_trading_hours", False):
            self.is_open = True
            return
        self.is_open = self._in_session()

    def set_open(self, flag):
        self.open_override = bool(flag)
        self.is_open = bool(flag)
        return self.is_open

    # ------------------------------------------------------------------ 币种增删（运行时）
    def add_coin(self, symbol, price, name=None, desc=None, enabled=True):
        sym = self.db.upsert_coin(symbol, price=price, name=name, desc=desc, enabled=enabled)
        if not sym:
            return None
        with self.lock:
            price = max(0.0001, float(price))
            self.prices[sym] = price
            self.candles[sym] = self._new_candle(price)
            self.state[sym] = self._new_state(sym, price)
            self.day_open[sym] = price
        return sym

    def remove_coin(self, symbol):
        sym = normalize_symbol(symbol)
        if self.db.delete_coin(sym):
            with self.lock:
                self.prices.pop(sym, None)
                self.candles.pop(sym, None)
                self.state.pop(sym, None)
                self.day_open.pop(sym, None)
            return True
        return False

    def set_price(self, symbol, price):
        sym = normalize_symbol(symbol)
        if sym not in self.prices:
            return False
        price = max(0.0001, float(price))
        with self.lock:
            self.prices[sym] = price
            candle = self.candles.get(sym)
            if candle is not None:
                candle["close"] = price
                candle["high"] = max(candle["high"], price)
                candle["low"] = min(candle["low"], price)
            st = self.state.get(sym)
            if st is not None:
                st["anchor"] = price
        return True

    # ------------------------------------------------------------------ 一键重置股价
    def reset_targets(self):
        """列出每个币种「重置后应该变成多少」。"""
        cfg_prices = {item["symbol"]: item["price"] for item in self.s.get_coins()}
        session = self.db.get_session()
        try:
            coins = {c.symbol: c for c in self.db.list_coins(session, only_enabled=True)}
        finally:
            session.close()
        min_price = max(0.0001, self.s.get_float("min_price", 0.01))
        out = []
        for sym in sorted(self.prices.keys()):
            target = cfg_prices.get(sym)
            if target is None and sym in coins:
                target = coins[sym].base_price
            try:
                target = max(min_price, float(target))
            except (TypeError, ValueError):
                target = None
            out.append(
                {
                    "symbol": sym,
                    "current": float(self.prices[sym]),
                    "target": target,
                    "actionable": target is not None,
                }
            )
        return out

    def reset_all_prices(self):
        """把所有币种价格重置为配置页价格（或数据库基准价），K 线与今日基准一起重置。"""
        cfg_prices = {item["symbol"]: item["price"] for item in self.s.get_coins()}
        session = self.db.get_session()
        try:
            coins = {c.symbol: c for c in self.db.list_coins(session, only_enabled=True)}
        finally:
            session.close()
        min_price = max(0.0001, self.s.get_float("min_price", 0.01))
        changed = []
        with self.lock:
            for sym in sorted(self.prices.keys()):
                target = cfg_prices.get(sym)
                if target is None and sym in coins:
                    target = coins[sym].base_price
                try:
                    target = max(min_price, float(target))
                except (TypeError, ValueError):
                    continue
                old = float(self.prices.get(sym, 0.0))
                self.prices[sym] = target
                self.candles[sym] = self._new_candle(target)
                self.state[sym] = self._new_state(sym, target)
                self.day_open[sym] = target
                self._cfg_price[sym] = target
                changed.append({"symbol": sym, "old": old, "new": target})
        return changed

    # ------------------------------------------------------------------ 管理员持仓操作
    def grant_holding(self, user_id, symbol, amount):
        """凭空加股：按当前价记录成本，让盈亏显示正常。返回 (新持仓, ok)。"""
        sym = normalize_symbol(symbol)
        if sym not in self.prices:
            return None, False
        price = float(self.prices[sym])
        session = self.db.get_session()
        try:
            holding, ok = self.db.adjust_holding(session, user_id, sym, amount)
            if not ok:
                session.rollback()
                return None, False
            holding.cost = (holding.cost or 0.0) + float(amount) * price
            session.commit()
            return holding.amount, True
        finally:
            session.close()

    def revoke_holding(self, user_id, symbol, amount):
        """收回股份：成本按比例扣减。返回 (新持仓, ok)。"""
        sym = normalize_symbol(symbol)
        session = self.db.get_session()
        try:
            holding = self.db.get_holding(session, user_id, sym)
            before = holding.amount if holding else 0.0
            holding, ok = self.db.adjust_holding(session, user_id, sym, -amount)
            if not ok:
                session.rollback()
                return before, False
            if before > 0:
                rate = max(0.0, 1.0 - float(amount) / before)
                holding.cost = (holding.cost or 0.0) * rate
            session.commit()
            return holding.amount, True
        finally:
            session.close()

    def move_holding(self, src, dst, symbol, amount):
        """账户间转股：成本一并结转。返回 (源持仓, 目标持仓) 或 None。"""
        sym = normalize_symbol(symbol)
        if sym not in self.prices:
            return None
        session = self.db.get_session()
        try:
            src_holding = self.db.get_holding(session, src, sym)
            if src_holding is None or src_holding.amount + 1e-9 < float(amount):
                session.rollback()
                return None
            before = float(src_holding.amount)
            src_holding, ok = self.db.adjust_holding(session, src, sym, -float(amount))
            if not ok:
                session.rollback()
                return None
            moved_cost = (src_holding.cost or 0.0) * (float(amount) / before) if before > 0 else 0.0
            src_holding.cost = max(0.0, (src_holding.cost or 0.0) - moved_cost)
            dst_holding, _ = self.db.adjust_holding(session, dst, sym, float(amount))
            dst_holding.cost = (dst_holding.cost or 0.0) + moved_cost
            session.commit()
            return src_holding.amount, dst_holding.amount
        finally:
            session.close()

    # ------------------------------------------------------------------ 循环
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="market_pro")
        self._thread.start()

    def stop(self):
        self.running = False
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _on_config_reload(self):
        try:
            self.sync_coins()
        except Exception as e:
            self._log("[market] sync on reload failed: {}".format(e))

    def _loop(self):
        while self.running:
            try:
                self.s.reload()
                self._reset_daily()
                self._refresh_open_state()
                interval = max(1, self.s.get_int("tick_interval", 5))
                now_ts = time.time()
                if now_ts - self._last_tick >= interval:
                    self._last_tick = now_ts
                    self.tick()
                time.sleep(0.5)
            except Exception as e:
                self._log("[market] loop error: {}".format(e))
                time.sleep(2)

    def tick(self):
        """手动推进一个 tick（也用于测试）。"""
        with self.lock:
            if not self.is_open:
                return False
            session = self.db.get_session()
            try:
                for sym in list(self.prices.keys()):
                    self._step(sym)
                self._save_candles(session)
                self.match_orders(session)
                session.commit()
                self._check_liquidations()
                if time.time() - self._last_prune > 600:
                    self._last_prune = time.time()
                    self.db.prune_history(session, max(50, self.s.get_int("history_keep", 2000)))
                    session.commit()
                return True
            except Exception as e:
                session.rollback()
                self._log("[market] tick error: {}".format(e))
                return False
            finally:
                session.close()

    def _check_liquidations(self):
        """每个 tick 之后检查合约仓位是否触及强平价。"""
        if self.margin_engine is None:
            return
        try:
            events = self.margin_engine.check_liquidations()
        except Exception as e:
            self._log("[market] liquidation check failed: {}".format(e))
            return
        if events:
            self.liquidation_events.extend(events)
            if len(self.liquidation_events) > 200:
                self.liquidation_events = self.liquidation_events[-200:]
            for ev in events:
                self._log(
                    "[market] 强平: {} {} {}x 用户 {} 强平价 {} 标记价 {}".format(
                        ev["symbol"], ev["side"], ev.get("leverage", "-"), ev["user_id"],
                        round(ev["liq_price"], 6), round(ev["mark"], 6),
                    )
                )

    def pop_liquidation_events(self):
        with self.lock:
            events = list(self.liquidation_events)
            self.liquidation_events = []
            return events

    def _save_candles(self, session):
        now = now_cn()
        for sym in list(self.prices.keys()):
            candle = self.candles.get(sym)
            if candle is None:
                continue
            session.add(
                MarketHistory(
                    symbol=sym,
                    timestamp=candle["start_time"],
                    open=candle["open"],
                    high=candle["high"],
                    low=candle["low"],
                    close=candle["close"],
                    volume=candle["volume"],
                )
            )
            self.candles[sym] = {
                "open": candle["close"],
                "high": candle["close"],
                "low": candle["close"],
                "close": candle["close"],
                "volume": 0.0,
                "start_time": now,
            }

    # ------------------------------------------------------------------ 交易
    def _fee_rate(self):
        return max(0.0, self.s.get_float("fee_rate", 0.001))

    def place_order(self, user_id, symbol, amount, price, action):
        """下单并尝试成交。返回 (ok, message, order_status)。"""
        sym = normalize_symbol(symbol)
        action = str(action or "").strip().lower()
        if action not in ("buy", "sell"):
            return False, "无效的操作（buy / sell）", None
        if sym not in self.prices:
            return False, "不支持的币种: {}".format(sym or symbol), None
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False, "数量必须是数字", None
        if amount <= 0:
            return False, "数量必须大于 0", None
        limit_price = None
        if price is not None and str(price).strip() != "":
            try:
                limit_price = float(price)
            except (TypeError, ValueError):
                return False, "价格必须是数字", None
            if limit_price <= 0:
                return False, "价格必须大于 0", None

        session = self.db.get_session()
        try:
            initial = self.s.get_float("initial_balance", 10000.0)
            user, _ = self.db.adjust_balance(session, user_id, 0.0, initial)

            # 预校验
            if action == "buy":
                estimate = (limit_price or self.prices[sym]) * amount
                need = estimate * (1.0 + self._fee_rate())
                if user.balance < need:
                    session.rollback()
                    return (
                        False,
                        "余额不足。预估需要 {:.2f}，当前余额 {:.2f}".format(need, user.balance),
                        None,
                    )
            else:
                holding = self.db.get_holding(session, user_id, sym)
                if holding is None or holding.amount + 1e-9 < amount:
                    session.rollback()
                    have = holding.amount if holding else 0.0
                    return False, "持仓不足。当前持有 {} {}".format(round(have, 6), sym), None

            order = Order(
                user_id=str(user_id),
                symbol=sym,
                order_type=OrderType.BUY if action == "buy" else OrderType.SELL,
                price=limit_price,
                amount=amount,
                status=OrderStatus.PENDING,
            )
            session.add(order)
            session.commit()
            order_id = order.id

            self.match_orders(session)
            session.commit()

            session.refresh(order)
            status = order.status
            fill_price = order.filled_price
            if status == OrderStatus.FILLED:
                return (
                    True,
                    "已成交（订单 #{}, 成交价 {}，数量 {}）".format(
                        order_id, fmt_price(fill_price), round(amount, 6)
                    ),
                    status,
                )
            if status == OrderStatus.CANCELLED:
                return True, "订单 #{} 已撤销（资金/持仓不足）".format(order_id), status
            if not self.is_open:
                return True, "已挂单（休市中，开市后自动撮合），订单 #{}".format(order_id), status
            return True, "已挂单，等待价格触及 {}，订单 #{}".format(
                fmt_price(limit_price) if limit_price else "市价", order_id
            ), status
        except Exception as e:
            session.rollback()
            return False, "下单失败: {}".format(e), None
        finally:
            session.close()

    def _should_fill(self, order, current_price):
        if order.price is None:
            return True
        if order.order_type == OrderType.BUY:
            return current_price <= order.price
        return current_price >= order.price

    def match_orders(self, session):
        pending = (
            session.query(Order)
            .filter(Order.status == OrderStatus.PENDING)
            .order_by(Order.id.asc())
            .all()
        )
        for order in pending:
            self._try_fill(session, order)

    def match_single_order(self, order_id):
        session = self.db.get_session()
        try:
            order = session.query(Order).filter_by(id=order_id).first()
            if order is not None:
                self._try_fill(session, order)
                session.commit()
        finally:
            session.close()

    def _try_fill(self, session, order):
        if not self.is_open:
            return
        sym = normalize_symbol(order.symbol)
        price = self.prices.get(sym)
        if not price:
            return
        if not self._should_fill(order, price):
            return
        self._execute(session, order, price)

    def _execute(self, session, order, exec_price):
        sym = normalize_symbol(order.symbol)
        fee_rate = self._fee_rate()
        total = exec_price * order.amount
        fee = total * fee_rate

        if order.order_type == OrderType.BUY:
            need = total + fee
            user, ok = self.db.adjust_balance(session, order.user_id, -need)
            if not ok:
                order.status = OrderStatus.CANCELLED
                return
            holding, ok = self.db.adjust_holding(session, order.user_id, sym, order.amount)
            if not ok:
                order.status = OrderStatus.CANCELLED
                return
            holding.cost = (holding.cost or 0.0) + need
        else:
            holding, ok = self.db.adjust_holding(session, order.user_id, sym, -order.amount)
            if not ok:
                order.status = OrderStatus.CANCELLED
                return
            revenue = total - fee
            _, ok = self.db.adjust_balance(session, order.user_id, revenue)
            if not ok:
                order.status = OrderStatus.CANCELLED
                return
            if holding.amount <= 1e-9:
                holding.cost = 0.0
            else:
                # 按比例摊低成本
                cost_rate = 1.0 - min(1.0, order.amount / (holding.amount + order.amount))
                holding.cost = (holding.cost or 0.0) * cost_rate

        order.status = OrderStatus.FILLED
        order.filled_price = float(exec_price)
        candle = self.candles.get(sym)
        if candle is not None:
            candle["volume"] += order.amount

        # 交易冲击：大单会推动价格
        impact = abs(self.s.get_float("trade_impact", 0.002))
        if impact > 0:
            sign = 1.0 if order.order_type == OrderType.BUY else -1.0
            factor = min(5.0, 1.0 + math.log10(1.0 + max(0.0, order.amount)))
            min_price = max(0.0001, self.s.get_float("min_price", 0.01))
            new_price = self.prices[sym] * (1.0 + sign * impact * factor)
            self.prices[sym] = max(min_price, new_price)
            if candle is not None:
                candle["close"] = self.prices[sym]
                candle["high"] = max(candle["high"], self.prices[sym])
                candle["low"] = min(candle["low"], self.prices[sym])

    # ------------------------------------------------------------------ 查询
    def portfolio(self, user_id):
        """返回 (balance, holdings 列表, 总资产)。"""
        session = self.db.get_session()
        try:
            user = session.query(User).filter_by(user_id=str(user_id)).first()
            balance = float(user.balance) if user else 0.0
            rows = self.db.list_holdings(session, user_id)
            items = []
            total = balance
            for h in rows:
                price = self.prices.get(h.symbol, 0.0)
                value = h.amount * price
                total += value
                cost = h.cost or 0.0
                profit = value - cost
                items.append(
                    {
                        "symbol": h.symbol,
                        "amount": h.amount,
                        "price": price,
                        "value": value,
                        "cost": cost,
                        "profit": profit,
                        "profit_pct": (profit / cost * 100.0) if cost > 0 else 0.0,
                    }
                )
            items.sort(key=lambda x: x["value"], reverse=True)
            return balance, items, total
        finally:
            session.close()

