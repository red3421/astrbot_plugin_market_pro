# -*- coding: utf-8 -*-
"""K 线绘图（可选依赖，缺库时自动降级为文字输出）。"""
import io
import os

_MPL_OK = True
_MPF_OK = True
_PANDAS_OK = True

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
except Exception:  # pragma: no cover
    _MPL_OK = False

try:
    import mplfinance as mpf
except Exception:  # pragma: no cover
    _MPF_OK = False

try:
    import pandas as pd
except Exception:  # pragma: no cover
    _PANDAS_OK = False

_CJK_CANDIDATES = (
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "PingFang SC",
    "Heiti SC",
    "Arial Unicode MS",
)

_WINDOWS_FONT_FILES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

_font_ready = False
_font_name = None


def available():
    return bool(_MPL_OK and _MPF_OK and _PANDAS_OK)


def _setup_font(font_path=""):
    global _font_ready, _font_name
    if _font_ready or not _MPL_OK:
        return _font_name
    _font_ready = True
    try:
        if font_path and os.path.exists(font_path):
            font_manager.fontManager.addfont(font_path)
            _font_name = font_manager.FontProperties(fname=font_path).get_name()
        else:
            for path in _WINDOWS_FONT_FILES:
                if os.path.exists(path):
                    try:
                        font_manager.fontManager.addfont(path)
                    except Exception:
                        pass
            installed = {f.name for f in font_manager.fontManager.ttflist}
            for name in _CJK_CANDIDATES:
                if name in installed:
                    _font_name = name
                    break
        if _font_name:
            plt.rcParams["font.sans-serif"] = [_font_name, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        _font_name = None
    return _font_name


def font_name():
    return _font_name


def plot_kline(history, title="K线", font_path=""):
    """history: 含 timestamp/open/high/low/close/volume 的对象或字典列表。

    返回 BytesIO(PNG)；依赖缺失或数据不足时返回 None。
    """
    if not available() or not history:
        return None
    _setup_font(font_path)

    rows = []
    stamps = []
    for item in history:
        if isinstance(item, dict):
            data = item
        else:
            data = {
                "timestamp": getattr(item, "timestamp", None),
                "open": getattr(item, "open", 0.0),
                "high": getattr(item, "high", 0.0),
                "low": getattr(item, "low", 0.0),
                "close": getattr(item, "close", 0.0),
                "volume": getattr(item, "volume", 0.0) or 0.0,
            }
        ts = data.get("timestamp")
        try:
            ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")
            stamps.append(ts)
        except AttributeError:
            ts_text = str(ts)
        rows.append(
            {
                "Date": ts_text,
                "Open": float(data.get("open") or 0.0),
                "High": float(data.get("high") or 0.0),
                "Low": float(data.get("low") or 0.0),
                "Close": float(data.get("close") or 0.0),
                "Volume": float(data.get("volume") or 0.0),
            }
        )
    if len(rows) < 2:
        return None

    # 时间跨度决定 X 轴标签精度（1 天内显示到秒，跨天显示到分钟）
    fmt = "%m-%d %H:%M"
    if len(stamps) >= 2:
        try:
            span = (stamps[-1] - stamps[0]).total_seconds()
            if span <= 86400:
                fmt = "%H:%M:%S"
        except Exception:
            pass
    has_volume = max(r["Volume"] for r in rows) > 0

    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(df["Date"])

    try:
        mc = mpf.make_marketcolors(
            up="#d64545", down="#2e9e5b", edge="inherit", wick="inherit", volume="in", inherit=True
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle="--",
            gridcolor="#dcdcdc",
            facecolor="#ffffff",
            y_on_right=True,
            rc={"font.sans-serif": [(_font_name or "DejaVu Sans")], "axes.unicode_minus": False},
        )
        buf = io.BytesIO()
        fig, axes = mpf.plot(
            df,
            type="candle",
            style=style,
            title=title,
            volume=has_volume,
            figratio=(16, 9),
            figscale=1.2,
            datetime_format=fmt,
            xrotation=20,
            warn_too_much_data=5000,
            returnfig=True,
        )
        ax = axes[0]
        ax.text(
            0.02,
            0.97,
            "红=涨  绿=跌\nO开盘 H最高\nL最低 C收盘",
            transform=ax.transAxes,
            fontsize=9,
            va="top",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#ffffff", edgecolor="#cccccc", alpha=0.9),
        )
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:  # pragma: no cover
        print("[market_pro] plot error: {}".format(e))
        try:
            plt.close("all")
        except Exception:
            pass
        return None
