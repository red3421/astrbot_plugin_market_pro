# -*- coding: utf-8 -*-
"""数据层：SQLite + SQLAlchemy。

所有币种代码统一大写存储与比较，避免大小写不一致引发的查找失败。
"""
import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    """当前中国时间（naive，便于与 SQLite 中的 naive 时间比较）。"""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def normalize_symbol(symbol):
    """币种代码统一大写。"""
    if symbol is None:
        return ""
    return str(symbol).strip().upper()


class OrderType(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(enum.Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    user_id = Column(String, primary_key=True)
    balance = Column(Float, default=10000.0)
    password_hash = Column(String, nullable=True)
    created_at = Column(DateTime, default=now_cn)


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True)
    symbol = Column(String, index=True)
    amount = Column(Float, default=0.0)
    cost = Column(Float, default=0.0)  # 累计买入成本，用于盈亏估算
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_holding"),)


class Coin(Base):
    __tablename__ = "coins"

    symbol = Column(String, primary_key=True)
    name = Column(String, default="")
    desc = Column(Text, default="")
    base_price = Column(Float, default=100.0)
    enabled = Column(Boolean, default=True)
    mode = Column(String, default="")  # 该币种单独的走势模式，空=跟随全局
    applied_price = Column(Float, nullable=True)  # 上次强制应用过的配置页价格
    created_at = Column(DateTime, default=now_cn)


class MarketHistory(Base):
    __tablename__ = "market_history"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    timestamp = Column(DateTime, default=now_cn)
    open = Column(Float, default=0.0)
    high = Column(Float, default=0.0)
    low = Column(Float, default=0.0)
    close = Column(Float, default=0.0)
    volume = Column(Float, default=0.0)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True)
    symbol = Column(String)
    order_type = Column(Enum(OrderType))
    price = Column(Float, nullable=True)
    amount = Column(Float, default=0.0)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    filled_price = Column(Float, nullable=True)
    created_at = Column(DateTime, default=now_cn)


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True)
    operator = Column(String)
    action = Column(String)
    target = Column(String)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=now_cn)


class PositionStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    LIQUIDATED = "liquidated"


class Position(Base):
    """逐仓合约仓位：一个仓位一份保证金，独立强平。"""

    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True)
    symbol = Column(String, index=True)
    side = Column(String)  # LONG / SHORT
    qty = Column(Float, default=0.0)  # 币数量
    closed_qty = Column(Float, default=0.0)  # 累计已平仓数量（用于历史成交展示）
    entry_price = Column(Float, default=0.0)
    leverage = Column(Integer, default=1)
    margin = Column(Float, default=0.0)  # 该仓位占用保证金
    liq_price = Column(Float, default=0.0)
    open_fee = Column(Float, default=0.0)
    close_fee = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN)
    opened_at = Column(DateTime, default=now_cn)
    closed_at = Column(DateTime, nullable=True)
    close_price = Column(Float, nullable=True)


class DB:
    def __init__(self, path):
        if not str(path).startswith("sqlite"):
            path = "sqlite:///{}".format(path)
        self.engine = create_engine(
            path,
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        Base.metadata.create_all(self.engine)
        self._migrate()
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def _migrate(self):
        """给老库补字段（create_all 不会修改已存在的表）。"""
        with self.engine.connect() as conn:
            def has_column(table, column):
                try:
                    for row in conn.execute(text("PRAGMA table_info({})".format(table))):
                        if row[1] == column:
                            return True
                except Exception:
                    pass
                return False

            patches = (
                ("coins", "mode", "ALTER TABLE coins ADD COLUMN mode VARCHAR DEFAULT ''"),
                ("coins", "applied_price", "ALTER TABLE coins ADD COLUMN applied_price FLOAT"),
                ("users", "password_hash", "ALTER TABLE users ADD COLUMN password_hash VARCHAR"),
                ("orders", "filled_price", "ALTER TABLE orders ADD COLUMN filled_price FLOAT"),
                ("positions", "closed_qty", "ALTER TABLE positions ADD COLUMN closed_qty FLOAT DEFAULT 0"),
            )
            for table, column, ddl in patches:
                if not has_column(table, column):
                    try:
                        conn.execute(text(ddl))
                        conn.commit()
                    except Exception as e:
                        print("[market_pro] migration {}: {}".format(column, e))
            try:
                if conn.in_transaction():
                    conn.commit()
            except Exception:
                pass

    # ---------- session ----------
    def get_session(self):
        return self.Session()

    # ---------- user ----------
    def get_or_create_user(self, user_id, initial_balance=10000.0):
        uid = str(user_id)
        session = self.Session()
        user = session.query(User).filter_by(user_id=uid).first()
        if user is None:
            user = User(user_id=uid, balance=float(initial_balance))
            session.add(user)
            session.commit()
        return user, session

    def get_user(self, user_id):
        session = self.Session()
        user = session.query(User).filter_by(user_id=str(user_id)).first()
        return user, session

    def ensure_user(self, user_id, initial_balance=10000.0):
        """确保用户存在，返回当前余额；内部自带 session 关闭。"""
        session = self.Session()
        try:
            user = session.query(User).filter_by(user_id=str(user_id)).first()
            if user is None:
                user = User(user_id=str(user_id), balance=float(initial_balance))
                session.add(user)
                session.commit()
            return float(user.balance)
        finally:
            session.close()

    @staticmethod
    def adjust_balance(session, user_id, delta, initial_balance=10000.0):
        """增减余额，返回 (user, ok)。余额不足时 ok=False。"""
        uid = str(user_id)
        user = session.query(User).filter_by(user_id=uid).first()
        if user is None:
            user = User(user_id=uid, balance=float(initial_balance))
            session.add(user)
            session.flush()
        new_balance = (user.balance or 0.0) + float(delta)
        if new_balance < 0:
            return user, False
        user.balance = new_balance
        return user, True

    # ---------- holding ----------
    @staticmethod
    def get_holding(session, user_id, symbol):
        return (
            session.query(Holding)
            .filter_by(user_id=str(user_id), symbol=normalize_symbol(symbol))
            .first()
        )

    @staticmethod
    def adjust_holding(session, user_id, symbol, delta):
        """增减持仓，返回 (holding, ok)。持仓不足时 ok=False。"""
        sym = normalize_symbol(symbol)
        holding = (
            session.query(Holding).filter_by(user_id=str(user_id), symbol=sym).first()
        )
        if holding is None:
            holding = Holding(user_id=str(user_id), symbol=sym, amount=0.0, cost=0.0)
            session.add(holding)
            session.flush()
        new_amount = (holding.amount or 0.0) + float(delta)
        if new_amount < -1e-9:
            return holding, False
        holding.amount = max(0.0, new_amount)
        return holding, True

    @staticmethod
    def list_holdings(session, user_id):
        return (
            session.query(Holding)
            .filter_by(user_id=str(user_id))
            .filter(Holding.amount > 1e-9)
            .all()
        )

    # ---------- coin ----------
    @staticmethod
    def list_coins(session, only_enabled=False):
        q = session.query(Coin)
        if only_enabled:
            q = q.filter(Coin.enabled.is_(True))
        return q.order_by(Coin.symbol).all()

    @staticmethod
    def get_coin(session, symbol):
        return session.query(Coin).filter_by(symbol=normalize_symbol(symbol)).first()

    def upsert_coin(self, symbol, price=None, name=None, desc=None, enabled=True, mode=None):
        sym = normalize_symbol(symbol)
        if not sym:
            return None
        session = self.Session()
        try:
            coin = session.query(Coin).filter_by(symbol=sym).first()
            if coin is None:
                coin = Coin(
                    symbol=sym,
                    name=name or sym,
                    desc=desc or "",
                    base_price=float(price) if price else 100.0,
                    enabled=bool(enabled),
                    mode=mode or "",
                )
                session.add(coin)
            else:
                if price is not None:
                    coin.base_price = float(price)
                if name:
                    coin.name = name
                if desc:
                    coin.desc = desc
                if mode is not None:
                    coin.mode = mode or ""
                coin.enabled = bool(enabled)
            session.commit()
            return sym
        finally:
            session.close()

    def set_coin_mode(self, symbol, mode):
        sym = normalize_symbol(symbol)
        session = self.Session()
        try:
            coin = session.query(Coin).filter_by(symbol=sym).first()
            if coin is None:
                return False
            coin.mode = mode or ""
            session.commit()
            return True
        finally:
            session.close()

    def set_applied_price(self, symbol, price):
        """记录「配置页价格已被强制应用」，用于跨重启判断价格是否变过。"""
        sym = normalize_symbol(symbol)
        session = self.Session()
        try:
            coin = session.query(Coin).filter_by(symbol=sym).first()
            if coin is None:
                return False
            coin.applied_price = float(price)
            session.commit()
            return True
        finally:
            session.close()

    def get_applied_prices(self):
        session = self.Session()
        try:
            out = {}
            for coin in session.query(Coin).all():
                if coin.applied_price is not None:
                    out[coin.symbol] = float(coin.applied_price)
            return out
        finally:
            session.close()

    def delete_coin(self, symbol):
        sym = normalize_symbol(symbol)
        session = self.Session()
        try:
            coin = session.query(Coin).filter_by(symbol=sym).first()
            if coin is None:
                return False
            session.delete(coin)
            session.commit()
            return True
        finally:
            session.close()

    # ---------- history ----------
    @staticmethod
    def last_close(session, symbol):
        row = (
            session.query(MarketHistory)
            .filter_by(symbol=normalize_symbol(symbol))
            .order_by(MarketHistory.timestamp.desc(), MarketHistory.id.desc())
            .first()
        )
        return row.close if row else None

    @staticmethod
    def day_open_price(session, symbol, day_start):
        row = (
            session.query(MarketHistory)
            .filter_by(symbol=normalize_symbol(symbol))
            .filter(MarketHistory.timestamp >= day_start)
            .order_by(MarketHistory.timestamp.asc(), MarketHistory.id.asc())
            .first()
        )
        return row.open if row else None

    @staticmethod
    def has_close_near(session, symbol, price, tol=0.02):
        """历史里是否存在接近该价格的收盘价（用于判断改价后是否已跑出过 K 线）。"""
        sym = normalize_symbol(symbol)
        if not price:
            return False
        lo, hi = price * (1 - tol), price * (1 + tol)
        row = (
            session.query(MarketHistory.id)
            .filter(MarketHistory.symbol == sym)
            .filter(MarketHistory.close >= lo)
            .filter(MarketHistory.close <= hi)
            .first()
        )
        return row is not None

    @staticmethod
    def recent_history(session, symbol, limit=60):
        rows = (
            session.query(MarketHistory)
            .filter_by(symbol=normalize_symbol(symbol))
            .order_by(MarketHistory.timestamp.desc(), MarketHistory.id.desc())
            .limit(limit)
            .all()
        )
        return rows[::-1]

    @staticmethod
    def prune_history(session, keep=2000):
        """每个币种只保留最近 keep 条。"""
        symbols = [r[0] for r in session.query(MarketHistory.symbol).distinct().all()]
        removed = 0
        for sym in symbols:
            total = (
                session.query(func.count(MarketHistory.id))
                .filter(MarketHistory.symbol == sym)
                .scalar()
                or 0
            )
            if total <= keep:
                continue
            cutoff_row = (
                session.query(MarketHistory.id)
                .filter(MarketHistory.symbol == sym)
                .order_by(MarketHistory.timestamp.desc(), MarketHistory.id.desc())
                .offset(keep - 1)
                .first()
            )
            if not cutoff_row:
                continue
            removed += (
                session.query(MarketHistory)
                .filter(MarketHistory.symbol == sym)
                .filter(MarketHistory.id < cutoff_row[0])
                .delete(synchronize_session=False)
            )
        return removed

    # ---------- admin log ----------
    def write_log(self, operator, action, target, detail=""):
        session = self.Session()
        try:
            session.add(
                AdminLog(
                    operator=str(operator),
                    action=str(action),
                    target=str(target),
                    detail=str(detail),
                )
            )
            session.commit()
        finally:
            session.close()

    # ---------- password ----------
    def set_password(self, user_id, password_hash):
        session = self.Session()
        try:
            user = session.query(User).filter_by(user_id=str(user_id)).first()
            if user is None:
                user = User(user_id=str(user_id), balance=10000.0)
                session.add(user)
                session.flush()
            user.password_hash = password_hash
            session.commit()
            return True
        finally:
            session.close()

    def get_password_hash(self, user_id):
        session = self.Session()
        try:
            user = session.query(User).filter_by(user_id=str(user_id)).first()
            return user.password_hash if user else None
        finally:
            session.close()

    # ---------- positions ----------
    @staticmethod
    def open_positions(session, user_id=None):
        q = session.query(Position).filter(Position.status == PositionStatus.OPEN)
        if user_id is not None:
            q = q.filter(Position.user_id == str(user_id))
        return q.order_by(Position.id.desc()).all()

    @staticmethod
    def get_position(session, position_id):
        return session.query(Position).filter_by(id=int(position_id)).first()

    @staticmethod
    def closed_positions(session, user_id, limit=30):
        return (
            session.query(Position)
            .filter(Position.user_id == str(user_id))
            .filter(Position.status != PositionStatus.OPEN)
            .order_by(Position.id.desc())
            .limit(limit)
            .all()
        )
