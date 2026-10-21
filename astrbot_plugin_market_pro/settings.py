# -*- coding: utf-8 -*-
"""配置中心：支持从 AstrBot 的 <插件>_config.json 热加载配置。

注意：AstrBot 保存配置时写入的是 **带 UTF-8 BOM** 的文件，直接用 utf-8 解析会报
"Unexpected UTF-8 BOM"，因此这里统一用 BOM 安全的方式读取。
"""
import json
import os
import threading
import time

# 配置项默认值（与 _conf_schema.json 保持一致）
DEFAULTS = {
    "market_mode": "横盘震荡",
    "rise_strength": 0.01,
    "fall_strength": 0.01,
    "sideways_amplitude": 0.03,
    "sideways_period": 20,
    "rally_ticks": 15,
    "rally_step": 0.02,
    "dump_ticks": 15,
    "dump_step": 0.02,
    "after_trend_mode": "横盘震荡",
    "daily_target_pct": 5.0,
    "target_window_minutes": 10.0,
    "random_volatility": 0.02,
    "noise": 0.002,
    "tick_interval": 5,
    "max_tick_change": 0.2,
    "min_price": 0.01,
    "trade_impact": 0.002,
    "initial_balance": 10000.0,
    "fee_rate": 0.001,
    "enforce_trading_hours": False,
    "session_morning": "09:30-11:30",
    "session_afternoon": "13:00-15:00",
    "admin_ids": [],
    "coins": [],
    "history_keep": 2000,
    "config_price_priority": True,
    "enable_plot": True,
    "font_path": "",
    "max_kline_points": 60,
    # ---- Web 交易端 ----
    "web_enabled": True,
    "web_host": "0.0.0.0",
    "web_port": 8666,
    "web_public_url": "",
    "web_show_mode": False,
    # ---- 合约 / 杠杆 ----
    "max_leverage": 125,
    "default_leverage": 20,
    "max_positions_per_user": 5,
    "contract_fee_rate": 0.0005,
    "maintenance_margin_rate": 0.005,
    "auto_close_on_liq": True,
    "chart_color_style": "国际（绿涨红跌）",
    "reset_prices_arm": False,
    "admin_api_key": "",
}

_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "latin-1")


def read_json_file(path):
    """BOM / 编码安全的 JSON 读取。"""
    with open(path, "rb") as f:
        raw = f.read()
    last_err = None
    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError as e:
            last_err = e
            continue
        text = text.lstrip("\ufeff").strip()
        if not text:
            raise ValueError("配置文件为空")
        return json.loads(text)
    raise ValueError("无法解码配置文件: {}".format(last_err))


def write_json_file(path, data):
    """写入不带 BOM 的 UTF-8 JSON（AstrBot 之后可能重写成 BOM，读取端已兼容）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Settings:
    """线程安全的配置读取器，文件 mtime 变化时自动重载。"""

    def __init__(self, path, defaults=None, logger=None):
        self.path = path
        self.defaults = dict(defaults or DEFAULTS)
        self.logger = logger
        self._lock = threading.RLock()
        self._file_data = {}
        self._overrides = {}  # 运行时覆盖（管理命令），不落盘
        self._mtime = None
        self._reload_hooks = []
        self._err_msg = ""
        self._err_ts = 0.0
        self.reload(force=True)

    # ---------- 日志（同样的错误 60 秒内只打一次，避免刷屏） ----------
    def _log(self, msg, level="warning"):
        now = time.time()
        if msg == self._err_msg and now - self._err_ts < 60:
            return
        self._err_msg = msg
        self._err_ts = now
        if self.logger:
            try:
                getattr(self.logger, level, self.logger.warning)(msg)
                return
            except Exception:
                pass
        print(msg)

    # ---------- 文件 ----------
    def bootstrap(self, initial_config):
        """首次运行时，用 AstrBot 传入的 config 生成配置文件。"""
        try:
            if os.path.exists(self.path):
                return
            data = dict(self.defaults)
            if isinstance(initial_config, dict):
                for k, v in initial_config.items():
                    data[k] = v
            folder = os.path.dirname(self.path)
            if folder and not os.path.isdir(folder):
                return
            write_json_file(self.path, data)
            self._mtime = None
            self.reload(force=True)
        except Exception as e:
            self._log("[settings] bootstrap failed: {}".format(e))

    def reload(self, force=False):
        """读取配置文件；文件有变化时返回 True。"""
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except OSError:
                return False
            if not force and mtime == self._mtime:
                return False
            try:
                data = read_json_file(self.path)
            except Exception as e:
                self._log("[settings] 配置解析失败 {}: {}".format(os.path.basename(self.path), e))
                # 记录 mtime，避免同一个坏文件每 0.5 秒重试一次
                self._mtime = mtime
                return False
            if not isinstance(data, dict):
                self._log("[settings] 配置格式错误：顶层不是对象")
                self._mtime = mtime
                return False
            self._file_data = data
            self._mtime = mtime
            self._err_msg = ""
        self._fire_hooks()
        return True

    # ---------- 钩子 ----------
    def add_reload_hook(self, fn):
        if callable(fn):
            self._reload_hooks.append(fn)

    def _fire_hooks(self):
        for fn in list(self._reload_hooks):
            try:
                fn()
            except Exception as e:
                self._log("[settings] hook error: {}".format(e))

    # ---------- 取值 ----------
    def raw(self, key, default=None):
        with self._lock:
            if key in self._overrides:
                return self._overrides[key]
            if key in self._file_data:
                return self._file_data[key]
            if key in self.defaults:
                return self.defaults[key]
            return default

    def set_override(self, key, value):
        with self._lock:
            self._overrides[key] = value

    def clear_override(self, key):
        with self._lock:
            self._overrides.pop(key, None)

    def overrides(self):
        with self._lock:
            return dict(self._overrides)

    def as_dict(self):
        data = dict(self.defaults)
        with self._lock:
            data.update(self._file_data)
            data.update(self._overrides)
        return data

    def get_str(self, key, default=""):
        value = self.raw(key, default)
        if value is None:
            return default
        return str(value)

    def get_float(self, key, default=0.0):
        try:
            return float(self.raw(key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_int(self, key, default=0):
        try:
            return int(float(self.raw(key, default)))
        except (TypeError, ValueError):
            return int(default)

    def get_bool(self, key, default=False):
        value = self.raw(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        try:
            return bool(value)
        except Exception:
            return bool(default)

    def get_list(self, key, default=None):
        value = self.raw(key, default if default is not None else [])
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return []

    def get_coins(self):
        """把配置页的 coins（template_list）规整为标准结构。"""
        result = []
        for item in self.get_list("coins", []):
            if isinstance(item, str):
                sym = item.strip().upper()
                if sym:
                    result.append(
                        {"symbol": sym, "name": sym, "price": 100.0, "desc": "",
                         "enabled": True, "mode": ""}
                    )
                continue
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            try:
                price = float(item.get("price") or 100.0)
            except (TypeError, ValueError):
                price = 100.0
            result.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("name") or symbol),
                    "price": price,
                    "desc": str(item.get("desc") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "mode": str(item.get("mode") or "").strip(),
                }
            )
        return result

    def admin_ids(self):
        ids = set()
        for item in self.get_list("admin_ids", []):
            text = str(item).strip()
            if text:
                ids.add(text)
        return ids

    # ---------- 写回配置页 ----------
    def _mutate_coins(self, mutator):
        with self._lock:
            try:
                data = read_json_file(self.path)
            except Exception as e:
                self._log("[settings] 写回配置失败（读取）: {}".format(e))
                return False
            if not isinstance(data, dict):
                return False
            coins = data.get("coins")
            if not isinstance(coins, list):
                coins = []
            changed = mutator(coins)
            if not changed:
                return False
            data["coins"] = coins
            try:
                write_json_file(self.path, data)
            except Exception as e:
                self._log("[settings] 写回配置失败（写入）: {}".format(e))
                return False
            self._mtime = None
        return True

    def append_coin_to_file(self, symbol, price, name=None, desc=None, mode=""):
        """把新币种写回配置文件，使其出现在配置页的币种列表里。"""
        symbol = str(symbol).strip().upper()
        if not symbol:
            return False

        def mutator(coins):
            for item in coins:
                if isinstance(item, dict) and str(item.get("symbol", "")).strip().upper() == symbol:
                    return False
            coins.append(
                {
                    "__template_key": "coin",
                    "symbol": symbol,
                    "name": name or symbol,
                    "price": float(price),
                    "desc": desc or "",
                    "enabled": True,
                    "mode": mode or "",
                }
            )
            return True

        return self._mutate_coins(mutator)

    def remove_coin_from_file(self, symbol):
        symbol = str(symbol).strip().upper()

        def mutator(coins):
            before = len(coins)
            kept = [
                item
                for item in coins
                if not (
                    isinstance(item, dict)
                    and str(item.get("symbol", "")).strip().upper() == symbol
                )
            ]
            coins[:] = kept
            return len(coins) != before

        return self._mutate_coins(mutator)

    def set_value_in_file(self, key, value):
        """把单个配置项写回配置文件（用于开关类字段的自动复位）。"""
        with self._lock:
            try:
                data = read_json_file(self.path)
            except Exception as e:
                self._log("[settings] 写回配置失败（读取）: {}".format(e))
                return False
            if not isinstance(data, dict):
                return False
            if data.get(key) == value:
                return True
            data[key] = value
            try:
                write_json_file(self.path, data)
            except Exception as e:
                self._log("[settings] 写回配置失败（写入）: {}".format(e))
                return False
            self._mtime = None
        return True

    def set_coin_mode_in_file(self, symbol, mode):
        symbol = str(symbol).strip().upper()

        def mutator(coins):
            found = False
            for item in coins:
                if isinstance(item, dict) and str(item.get("symbol", "")).strip().upper() == symbol:
                    item["mode"] = mode or ""
                    found = True
            return found

        return self._mutate_coins(mutator)
