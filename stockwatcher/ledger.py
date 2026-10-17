"""Single portfolio ledger with chronological replay and weighted average cost.

Opening holdings do not imply historical cash flows. Unknown cash remains unknown.
Book cost is for portfolio tracking, not a tax-lot accounting method.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext

from .domain import ZERO, LedgerView, Position, ValidationError, amount, instrument, iso, trade_day
from .storage import Store


class Ledger:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _replay(rows) -> LedgerView:
        view = LedgerView()
        with localcontext() as context:
            context.prec = 40
            for row in rows:
                kind = row["kind"]
                data = json.loads(row["payload"])
                if kind == "cash_set":
                    view.cash = Decimal(data["amount"])
                    continue
                if kind in {"deposit", "withdraw", "income"}:
                    if view.cash is None:
                        raise ValidationError("入金/出金前需先设置当时的现金余额。")
                    delta = Decimal(data["amount"])
                    view.cash += -delta if kind == "withdraw" else delta
                else:
                    symbol = row["instrument"]
                    position = view.positions.setdefault(symbol, Position(symbol))
                    if kind == "split":
                        if position.quantity <= ZERO:
                            raise ValidationError("没有持仓，不能登记拆股。")
                        adjusted = (
                            position.quantity
                            * Decimal(data["numerator"])
                            / Decimal(data["denominator"])
                        )
                        if adjusted != adjusted.quantize(Decimal("0.00000001")):
                            raise ValidationError("拆股后数量超过 8 位小数，需先核对券商碎股处理。")
                        position.quantity = adjusted
                        continue
                    qty, price, fee = (Decimal(data[key]) for key in ("quantity", "price", "fee"))
                    if kind == "seed":
                        if position.quantity or position.has_opening_seed or position.realized_pnl:
                            raise ValidationError("期初持仓只能在该股票的交易记录之前登记。")
                        position.quantity = qty
                        position.cost_basis = qty * price + fee
                        position.has_opening_seed = True
                    elif kind == "buy":
                        position.quantity += qty
                        position.cost_basis += qty * price + fee
                        if view.cash is not None:
                            view.cash -= qty * price + fee
                    elif kind == "sell":
                        if qty > position.quantity:
                            raise ValidationError(f"{symbol} 卖出数量超过该交易日已有持仓。")
                        basis = (
                            position.cost_basis
                            if qty == position.quantity
                            else position.cost_basis * qty / position.quantity
                        )
                        proceeds = qty * price - fee
                        position.quantity -= qty
                        position.cost_basis -= basis
                        position.realized_pnl += proceeds - basis
                        if view.cash is not None:
                            view.cash += proceeds
                    else:
                        raise ValidationError("账本包含不支持的事件类型。")
                if view.cash is not None and view.cash < ZERO:
                    raise ValidationError(
                        "该事件会使历史现金余额为负；请核对入金、余额或成交日期。"
                    )
        return view

    @staticmethod
    def _active(db):
        return db.execute(
            "SELECT * FROM ledger_events WHERE voided_at IS NULL ORDER BY event_date,id"
        ).fetchall()

    def view(self) -> LedgerView:
        return self._replay(
            self.store.rows(
                "SELECT * FROM ledger_events WHERE voided_at IS NULL ORDER BY event_date,id"
            )
        )

    def record(
        self,
        kind: str,
        *,
        symbol: str | None = None,
        day: str | None = None,
        quantity: str = "0",
        price: str = "0",
        fee: str = "0",
        cash: str = "0",
        numerator: str = "1",
        denominator: str = "1",
        note: str = "",
        request_key: str | None = None,
    ) -> tuple[int, bool]:
        day = trade_day(day)
        if len(note) > 1000:
            raise ValidationError("备注最多 1000 字。")
        symbol = instrument(symbol) if symbol else None
        if kind in {"seed", "buy", "sell"}:
            if not symbol:
                raise ValidationError("请提供股票代码。")
            data = {
                "quantity": str(amount(quantity, positive=True)),
                "price": str(amount(price, positive=True)),
                "fee": str(amount(fee)),
                "note": note,
            }
        elif kind == "split":
            if not symbol:
                raise ValidationError("请提供股票代码。")
            data = {
                "numerator": str(amount(numerator, positive=True)),
                "denominator": str(amount(denominator, positive=True)),
                "note": note,
            }
        elif kind in {"cash_set", "deposit", "withdraw", "income"}:
            symbol = None
            data = {"amount": str(amount(cash, positive=kind != "cash_set")), "note": note}
        else:
            raise ValidationError("不支持的账本事件。")
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self.store.transaction() as db:
            if request_key:
                existing = db.execute(
                    "SELECT * FROM ledger_events WHERE request_key=?", (request_key,)
                ).fetchone()
                if existing:
                    if (
                        existing["kind"],
                        existing["instrument"],
                        existing["event_date"],
                        existing["payload"],
                    ) != (kind, symbol, day, payload):
                        raise ValidationError("同一请求标识对应了不同内容，拒绝重复写入。")
                    return existing["id"], False
            if (
                kind == "seed"
                and db.execute(
                    "SELECT 1 FROM ledger_events WHERE instrument=? AND voided_at IS NULL",
                    (symbol,),
                ).fetchone()
            ):
                raise ValidationError("该股票已有记录；如期初数据有误，请先撤销并重新登记。")
            cursor = db.execute(
                "INSERT INTO ledger_events(kind,instrument,event_date,payload,request_key,created_at) VALUES (?,?,?,?,?,?)",
                (kind, symbol, day, payload, request_key, iso(datetime.now(UTC))),
            )
            self._replay(self._active(db))
            return cursor.lastrowid, True

    def void(self, event_id: int, note: str) -> bool:
        if not note.strip() or len(note) > 1000:
            raise ValidationError("撤销需填写 1～1000 字的更正原因。")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM ledger_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValidationError("找不到该记录。")
            if row["voided_at"]:
                return False
            db.execute(
                "UPDATE ledger_events SET voided_at=?,void_note=? WHERE id=?",
                (iso(datetime.now(UTC)), note, event_id),
            )
            # Reject a correction that would make a later sale or cash balance impossible.
            self._replay(self._active(db))
            return True

    def events(self, limit: int = 20) -> list[dict]:
        return [
            dict(row)
            for row in self.store.rows(
                "SELECT * FROM ledger_events ORDER BY event_date DESC,id DESC LIMIT ?", (limit,)
            )
        ]

    def watch(self, symbol: str, reason: str = "") -> None:
        symbol = instrument(symbol)
        if len(reason) > 1000:
            raise ValidationError("关注理由最多 1000 字。")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO watchlist VALUES (?,?,?) ON CONFLICT(instrument) DO UPDATE SET reason=excluded.reason,updated_at=excluded.updated_at",
                (symbol, reason, iso(datetime.now(UTC))),
            )

    def unwatch(self, symbol: str) -> bool:
        with self.store.transaction() as db:
            return bool(
                db.execute(
                    "DELETE FROM watchlist WHERE instrument=?", (instrument(symbol),)
                ).rowcount
            )

    def watchlist(self) -> list[dict]:
        return [dict(row) for row in self.store.rows("SELECT * FROM watchlist ORDER BY instrument")]
