# -*- coding: utf-8 -*-
"""逐仓杠杆合约引擎（1-125 倍）。

核心概念
--------
- 保证金 margin：开仓时从余额划走，锁定在该仓位里
- 名义价值 notional = margin * leverage
- 数量 qty = notional / 开仓价
- 维持保证金率 mmr：仓位权益低于 notional*mmr 时强平
- 强平价（逐仓近似）：
    多单 liq = entry * (1 + mmr - 1/leverage)
    空单 liq = entry * (1 - mmr + 1/leverage)
- 盈亏 pnl = (标记价 - 开仓价) * qty * (多单 +1 / 空单 -1)
- 手续费：开仓、平仓各收 notional * fee_rate
"""
import math

try:
    from .database import (
        Position,
        PositionStatus,
        User,
        now_cn,
        normalize_symbol,
    )
except ImportError:  # 兼容直接运行
    from database import (
        Position,
        PositionStatus,
        User,
        now_cn,
        normalize_symbol,
    )

MIN_LEVERAGE = 1
MAX_LEVERAGE = 125

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

SIDE_ALIASES = {
    "long": SIDE_LONG,
    "多": SIDE_LONG,
    "做多": SIDE_LONG,
    "买多": SIDE_LONG,
    "buy": SIDE_LONG,
    "short": SIDE_SHORT,
    "空": SIDE_SHORT,
    "做空": SIDE_SHORT,
    "卖空": SIDE_SHORT,
    "sell": SIDE_SHORT,
}


def normalize_side(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    return SIDE_ALIASES.get(text)


def liquidation_price(entry, side, leverage, mmr):
    """逐仓强平价。"""
    lev = max(MIN_LEVERAGE, min(MAX_LEVERAGE, int(leverage)))
    entry = float(entry)
    mmr = float(mmr)
    if side == SIDE_LONG:
        price = entry * (1.0 + mmr - 1.0 / lev)
    else:
        price = entry * (1.0 - mmr + 1.0 / lev)
    return max(0.0, price)


def unrealized_pnl(entry, mark, qty, side):
    sign = 1.0 if side == SIDE_LONG else -1.0
    return (float(mark) - float(entry)) * float(qty) * sign


def margin_ratio(entry, mark, qty, side, margin):
    """保证金率 = (保证金 + 未实现盈亏) / 名义价值。"""
    notional = float(entry) * float(qty)
    if notional <= 0:
        return 0.0
    equity = float(margin) + unrealized_pnl(entry, mark, qty, side)
    return equity / notional


class MarginEngine:
    """合约交易的全部业务逻辑，供命令层与 Web API 共用。"""

    def __init__(self, db, settings, market, logger=None):
        self.db = db
        self.s = settings
        self.market = market
        self.logger = logger

    # ---------------- 配置 ----------------
    def fee_rate(self):
        return max(0.0, self.s.get_float("contract_fee_rate", 0.0005))

    def mmr(self):
        return max(0.0, self.s.get_float("maintenance_margin_rate", 0.005))

    def max_leverage(self):
        return max(MIN_LEVERAGE, min(MAX_LEVERAGE, self.s.get_int("max_leverage", 125)))

    def max_positions(self):
        return max(1, self.s.get_int("max_positions_per_user", 5))

    def clamp_leverage(self, leverage):
        try:
            lev = int(float(leverage))
        except (TypeError, ValueError):
            lev = 1
        return max(MIN_LEVERAGE, min(self.max_leverage(), lev))

    def price_of(self, symbol):
        return float(self.market.prices.get(normalize_symbol(symbol), 0.0) or 0.0)

    # ---------------- 开仓 ----------------
    def open_position(self, user_id, symbol, side, margin, leverage):
        """返回 (ok, message, position_dict)。"""
        sym = normalize_symbol(symbol)
        side = normalize_side(side)
        if side is None:
            return False, "方向无效：long / short", None
        if sym not in self.market.prices:
            return False, "不支持的币种: {}".format(sym or symbol), None
        if not self.market.is_open:
            return False, "市场休市中，无法开仓", None
        try:
            margin = float(margin)
        except (TypeError, ValueError):
            return False, "保证金必须是数字", None
        if margin <= 0:
            return False, "保证金必须大于 0", None
        lev = self.clamp_leverage(leverage)
        price = self.price_of(sym)
        if price <= 0:
            return False, "当前价格无效", None

        notional = margin * lev
        fee = notional * self.fee_rate()
        need = margin + fee

        session = self.db.get_session()
        try:
            open_count = len(self.db.open_positions(session, user_id))
            if open_count >= self.max_positions():
                session.close()
                return False, "持仓数量已达上限（{} 个），请先平仓".format(self.max_positions()), None

            user = session.query(User).filter_by(user_id=str(user_id)).first()
            if user is None:
                user = User(user_id=str(user_id), balance=self.s.get_float("initial_balance", 10000.0))
                session.add(user)
                session.flush()
            if user.balance < need:
                session.rollback()
                return False, "余额不足。需要 {}（保证金 {} + 手续费 {}），当前余额 {}".format(
                    round(need, 4), round(margin, 4), round(fee, 6), round(user.balance, 4)
                ), None

            user.balance -= need
            qty = notional / price
            liq = liquidation_price(price, side, lev, self.mmr())
            pos = Position(
                user_id=str(user_id),
                symbol=sym,
                side=side,
                qty=qty,
                entry_price=price,
                leverage=lev,
                margin=margin,
                liq_price=liq,
                open_fee=fee,
                status=PositionStatus.OPEN,
            )
            session.add(pos)
            session.commit()
            data = self.position_dict(pos, price)
            return True, "开仓成功：{} {} {}x，数量 {}，开仓价 {}".format(
                sym, "做多" if side == SIDE_LONG else "做空", lev,
                round(qty, 6), round(price, 6)
            ), data
        finally:
            session.close()

    # ---------------- 平仓 ----------------
    def close_position(self, user_id, position_id, ratio=1.0, reason="manual"):
        """按比例平仓。ratio=1 全平。返回 (ok, message, dict)。"""
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = 1.0
        ratio = max(0.0001, min(1.0, ratio))

        session = self.db.get_session()
        try:
            pos = self.db.get_position(session, position_id)
            if pos is None or pos.status != PositionStatus.OPEN:
                return False, "仓位不存在或已平仓", None
            if str(pos.user_id) != str(user_id):
                return False, "这不是你的仓位", None

            mark = self.price_of(pos.symbol)
            if mark <= 0:
                return False, "当前价格无效", None

            close_qty = pos.qty * ratio
            notional = mark * close_qty
            close_fee = notional * self.fee_rate()
            pnl = unrealized_pnl(pos.entry_price, mark, close_qty, pos.side)
            released_margin = pos.margin * ratio

            realized = pnl - close_fee
            # 亏损超过释放的保证金时，最多亏掉保证金（逐仓）
            if realized < -released_margin:
                realized = -released_margin

            user = session.query(User).filter_by(user_id=str(pos.user_id)).first()
            if user is not None:
                user.balance += released_margin + realized

            pos.close_fee = (pos.close_fee or 0.0) + close_fee
            pos.realized_pnl = (pos.realized_pnl or 0.0) + realized - (pos.open_fee or 0.0) * ratio
            pos.closed_qty = (pos.closed_qty or 0.0) + close_qty

            if ratio >= 0.999999:
                pos.status = PositionStatus.CLOSED
                pos.qty = 0.0
                pos.close_price = mark
                pos.closed_at = now_cn()
                pos.margin = 0.0
            else:
                pos.qty -= close_qty
                pos.margin -= released_margin
                pos.liq_price = liquidation_price(pos.entry_price, pos.side, pos.leverage, self.mmr())

            session.commit()
            return True, "平仓成功：{} {} {}，平仓价 {}，盈亏 {}".format(
                pos.symbol,
                "多" if pos.side == SIDE_LONG else "空",
                round(close_qty, 6),
                round(mark, 6),
                round(realized, 4),
            ), {
                "symbol": pos.symbol,
                "qty": close_qty,
                "exit_price": mark,
                "realized_pnl": realized,
                "fee": close_fee,
                "reason": reason,
            }
        finally:
            session.close()

    def close_all(self, user_id):
        session = self.db.get_session()
        try:
            ids = [p.id for p in self.db.open_positions(session, user_id)]
        finally:
            session.close()
        results = []
        for pid in ids:
            results.append(self.close_position(user_id, pid, 1.0, reason="close_all"))
        return results

    # ---------------- 强平 ----------------
    def check_liquidations(self):
        """每个 tick 之后扫描一次，触发强平。返回被强平的仓位列表。"""
        liquidated = []
        session = self.db.get_session()
        try:
            positions = self.db.open_positions(session)
            for pos in positions:
                mark = self.price_of(pos.symbol)
                if mark <= 0:
                    continue
                hit = (
                    pos.side == SIDE_LONG and mark <= pos.liq_price
                ) or (
                    pos.side == SIDE_SHORT and mark >= pos.liq_price
                )
                if hit:
                    liquidated.append(self._liquidate(pos, mark))
            return liquidated
        finally:
            session.close()

    def _liquidate(self, pos, mark):
        session = self.db.get_session()
        try:
            fresh = self.db.get_position(session, pos.id)
            if fresh is None or fresh.status != PositionStatus.OPEN:
                return None
            # 逐仓强平：亏掉全部保证金，不追加负债
            fresh.status = PositionStatus.LIQUIDATED
            fresh.close_price = mark
            fresh.closed_at = now_cn()
            fresh.realized_pnl = -(fresh.margin or 0.0) - (fresh.open_fee or 0.0)
            fresh.closed_qty = fresh.qty
            session.commit()
            info = {
                "position_id": fresh.id,
                "user_id": fresh.user_id,
                "symbol": fresh.symbol,
                "side": fresh.side,
                "leverage": fresh.leverage,
                "qty": fresh.qty,
                "liq_price": fresh.liq_price,
                "mark": mark,
                "margin": fresh.margin,
            }
            fresh.qty = 0.0
            fresh.margin = 0.0
            return info
        finally:
            session.close()

    # ---------------- 查询 ----------------
    def position_dict(self, pos, mark=None):
        mark = float(mark if mark is not None else self.price_of(pos.symbol))
        pnl = unrealized_pnl(pos.entry_price, mark, pos.qty, pos.side)
        notional = pos.entry_price * pos.qty
        ratio = margin_ratio(pos.entry_price, mark, pos.qty, pos.side, pos.margin)
        return {
            "id": pos.id,
            "user_id": pos.user_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "mark_price": mark,
            "leverage": pos.leverage,
            "margin": pos.margin,
            "notional": notional,
            "liq_price": pos.liq_price,
            "pnl": pnl,
            "roe": (pnl / pos.margin * 100.0) if pos.margin > 0 else 0.0,
            "margin_ratio": ratio * 100.0,
            "open_fee": pos.open_fee,
            "opened_at": pos.opened_at.strftime("%Y-%m-%d %H:%M:%S") if pos.opened_at else "",
        }

    def account(self, user_id):
        session = self.db.get_session()
        try:
            user = session.query(User).filter_by(user_id=str(user_id)).first()
            balance = float(user.balance) if user else 0.0
            positions = [self.position_dict(p) for p in self.db.open_positions(session, user_id)]
            realized = (
                session.query(Position)
                .filter(Position.user_id == str(user_id))
                .filter(Position.status != PositionStatus.OPEN)
                .all()
            )
            total_realized = sum(p.realized_pnl or 0.0 for p in realized)
        finally:
            session.close()

        unrealized = sum(p["pnl"] for p in positions)
        used_margin = sum(p["margin"] for p in positions)
        return {
            "user_id": str(user_id),
            "balance": balance,
            "used_margin": used_margin,
            "available": balance,
            "unrealized_pnl": unrealized,
            "realized_pnl": total_realized,
            "equity": balance + used_margin + unrealized,
            "positions": positions,
        }

    def preview(self, symbol, side, margin, leverage):
        """下单预览：数量、手续费、强平价，用于 Web 前端实时显示。"""
        sym = normalize_symbol(symbol)
        price = self.price_of(sym)
        lev = self.clamp_leverage(leverage)
        try:
            margin = float(margin)
        except (TypeError, ValueError):
            margin = 0.0
        side_n = normalize_side(side) or SIDE_LONG
        notional = margin * lev
        qty = notional / price if price > 0 else 0.0
        fee = notional * self.fee_rate()
        return {
            "symbol": sym,
            "side": side_n,
            "leverage": lev,
            "price": price,
            "margin": margin,
            "notional": notional,
            "qty": qty,
            "fee": fee,
            "liq_price": liquidation_price(price, side_n, lev, self.mmr()) if price > 0 else 0.0,
            "mmr": self.mmr(),
            "max_leverage": self.max_leverage(),
        }


def safe_div(a, b, default=0.0):
    try:
        if not b:
            return default
        return a / b
    except Exception:
        return default
