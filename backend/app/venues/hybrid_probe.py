"""混合 Paper 执行连接器。

加密交易所腿使用真实最小单采样成交价，采样后立即反向回平；返回给 Paper
账本的成交数量仍是策略数量。真实探针状态持久化到 Redis 和 ProbeRun，进程
重启后只按稳定 ClientOrderId 查询恢复，绝不盲目重复下单。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now
from app.db.models import ProbeRun
from app.db.session import SessionLocal
from app.execution.runtime_settings import paper_live_probe_enabled_for_venue, paper_probe_limits
from app.venues.domain.models import (
    Fill,
    OrderRequest,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from app.venues.order_event_waiter import OrderEventWaiter


TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
}
ACTIVE_STATUSES = {
    OrderStatus.CREATED,
    OrderStatus.SUBMITTING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_CANCEL,
    OrderStatus.UNKNOWN,
}


class HybridPaperProbeConnector:
    """把加密交易所真实最小单投影为策略数量的 Paper 成交。"""

    def __init__(self, live_connector: Any, *, redis: Any | None = None) -> None:
        self.live = live_connector
        self.redis = redis or redis_client()
        self.venue = str(live_connector.venue)
        self.environment = str(getattr(live_connector, "environment", "live"))
        self.read_only = False
        self.capabilities = live_connector.capabilities
        self._event_waiter = OrderEventWaiter()
        # 探针子订单不进入策略订单投影，但必须直接消费同一条账户私有流。
        self.live.subscribe_private_events(self._event_waiter.on_event)

    def start(self) -> None:
        self.live.start()

    def stop(self) -> None:
        # live Connector 由 NativeVenueManager 统一管理生命周期。
        return None

    def health(self) -> dict[str, Any]:
        return {**self.live.health(), "paper_execution": "real_minimum_probe"}

    def get_account(self):
        return self.live.get_account()

    def get_positions(self):
        return self.live.get_positions()

    def get_open_orders(self, symbol: str | None = None):
        return self.live.get_open_orders(symbol)

    def get_instruments(self, symbols=None):
        return self.live.get_instruments(symbols)

    def get_instrument(self, symbol: str, *, refresh: bool = False):
        return self.live.get_instrument(symbol, refresh=refresh)

    def get_ticker(self, symbol: str):
        return self.live.get_ticker(symbol)

    def get_order_book(self, symbol: str, depth: int = 20):
        return self.live.get_order_book(symbol, depth)

    def subscribe_market_data(self, symbols, handler=None) -> None:
        self.live.subscribe_market_data(symbols, handler)

    def unsubscribe_market_data(self, symbols) -> None:
        self.live.unsubscribe_market_data(symbols)

    def subscribe_private_events(self, handler) -> None:
        # 探针的真实子订单不直接投影到策略腿；策略腿使用同步返回的合成成交。
        return None

    def validate_credentials(self):
        return self.live.validate_credentials()

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        state = self._load_state(request.client_order_id)
        if state:
            return self._advance(state)

        with SessionLocal() as db:
            if not paper_live_probe_enabled_for_venue(db, _settings(), self.venue):
                return self._rejected(request, "Paper 真实最小单探针未开启")
            limits = paper_probe_limits(db)

        account = self.live.get_account()
        account_hash = hashlib.sha256(str(account.account_id).encode("utf-8")).hexdigest()[:16]
        lock_key = redis_key("paper-probe", "lock", self.venue, account_hash, request.symbol)
        lock_ttl = max(
            120,
            int(float(limits["paper_probe_flatten_timeout_seconds"]) + float(limits["paper_probe_maker_timeout_seconds"]) + 30),
        )
        if not self.redis.set(lock_key, request.client_order_id, nx=True, ex=lock_ttl):
            return self._rejected(request, "同一账户和品种已有真实探针正在执行")

        try:
            baseline = self._position_quantity(request.symbol)
            if baseline > Decimal("1e-12"):
                return self._fail_before_exposure(
                    request,
                    lock_key,
                    f"探针账户存在同品种仓位 {baseline}，必须使用空仓专用账户",
                )
            instrument = self.live.get_instrument(request.symbol)
            ticker = self.live.get_ticker(request.symbol)
            reference = request.price or (ticker.ask if request.side == Side.BUY else ticker.bid)
            probe_quantity = _probe_quantity(instrument, reference)
            probe_notional = probe_quantity * reference
            if probe_notional > Decimal(str(limits["paper_probe_max_notional"])):
                return self._fail_before_exposure(
                    request,
                    lock_key,
                    f"交易所最小探针名义金额 {probe_notional} 超过单次上限",
                )
            depth_adjustment = (
                self._market_depth_adjustment(request)
                if request.order_type == OrderType.MARKET else Decimal("0")
            )
            limit_error = self._reserve_daily_limits(probe_notional, limits)
            if limit_error:
                return self._fail_before_exposure(request, lock_key, limit_error)
            cooldown_key = redis_key("paper-probe", "cooldown", self.venue, account_hash, request.symbol)
            cooldown_ms = int(limits["paper_probe_cooldown_ms"])
            if cooldown_ms and not self.redis.set(cooldown_key, "1", nx=True, px=cooldown_ms):
                return self._fail_before_exposure(request, lock_key, "探针冷却时间尚未结束")

            state = {
                "version": 1,
                "stage": "PREPARED",
                "venue": self.venue,
                "symbol": request.symbol,
                "client_order_id": request.client_order_id,
                "entry_client_order_id": f"{request.client_order_id}-P",
                "flatten_client_order_id": f"{request.client_order_id}-F",
                "side": request.side.value,
                "strategy_position_side": request.position_side.value,
                "probe_position_side": (
                    PositionSide.LONG.value if request.side == Side.BUY else PositionSide.SHORT.value
                ) if self.venue == "binance" else PositionSide.NET.value,
                "order_type": request.order_type.value,
                "price": str(request.price) if request.price is not None else "",
                "target_quantity": str(request.quantity),
                "probe_quantity": str(probe_quantity),
                "probe_notional": str(probe_notional),
                "depth_adjustment": str(depth_adjustment),
                "baseline_quantity": str(baseline),
                "hedge_group_id": int(request.metadata.get("hedge_group_id") or 0),
                "intent_id": int(request.metadata.get("intent_id") or 0),
                "action": str(request.metadata.get("action") or "OPEN"),
                "lock_key": lock_key,
                "flatten_timeout_seconds": float(limits["paper_probe_flatten_timeout_seconds"]),
                "maker_timeout_seconds": float(limits["paper_probe_maker_timeout_seconds"]),
                "updated_at": utc_now().isoformat(),
            }
            self._save_state(state)
            self._upsert_probe_run(state, "OPENING")
            return self._advance(state)
        except Exception:
            # PREPARED 之后的异常可能已有真实成交，保留锁和状态给恢复循环处理。
            if not self._load_state(request.client_order_id):
                self._release_lock(lock_key, request.client_order_id)
            raise

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        state = self._load_state(client_order_id)
        if not state:
            raise LookupError(f"Paper 探针状态不存在: {client_order_id or venue_order_id}")
        return self._advance(state)

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        return self.get_order(symbol, client_order_id=client_order_id, venue_order_id=venue_order_id)

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        return []

    def _advance(self, state: dict[str, Any]) -> OrderSnapshot:
        stage = str(state.get("stage") or "PREPARED")
        if stage == "FLAT":
            return self._synthetic_snapshot(state, OrderStatus.FILLED)
        if stage in {"FAILED_NO_EXPOSURE", "FAILED_SAMPLE_INVALID"}:
            return self._synthetic_snapshot(state, OrderStatus.REJECTED)

        try:
            entry = self._entry_order(state)
            entry = self._wait_for_entry(state, entry)
            filled, average = self._filled_details(state, entry)
            if filled <= 0:
                state.update(stage="FAILED_NO_EXPOSURE", error="真实探针未成交")
                self._save_state(state)
                self._upsert_probe_run(state, "FAILED_NO_EXPOSURE")
                self._release_lock(str(state["lock_key"]), str(state["client_order_id"]))
                return self._synthetic_snapshot(state, OrderStatus.REJECTED)

            state.update(
                stage="FLATTENING",
                actual_filled_quantity=str(filled),
                probe_average_price=str(average),
                projected_average_price=str(average + Decimal(str(state.get("depth_adjustment") or 0))),
                entry_venue_order_id=str(entry.venue_order_id or state.get("entry_venue_order_id") or ""),
                residual_quantity=str(filled),
            )
            self._save_state(state)
            self._upsert_probe_run(state, "FLATTENING")
            flatten = self._flatten_order(state, filled)
            flatten = self._wait_for_terminal(
                state,
                flatten,
                str(state["flatten_client_order_id"]),
                float(state["flatten_timeout_seconds"]),
            )
            flattened, flatten_average = self._filled_details(state, flatten, flatten=True)
            if flattened + Decimal("1e-12") < filled:
                raise RuntimeError(f"真实探针回平不完整: filled={filled}, flattened={flattened}")
            final_quantity = self._position_quantity(str(state["symbol"]))
            if final_quantity > Decimal("1e-12"):
                raise RuntimeError(f"真实探针账户未恢复空仓基线，残余数量 {final_quantity}")

            if average <= 0:
                state.update(
                    stage="FAILED_SAMPLE_INVALID",
                    residual_quantity="0",
                    final_position_quantity=str(final_quantity),
                    flat_confirmed_at=utc_now().isoformat(),
                    error="真实探针已安全回平，但没有取得有效成交均价",
                )
                self._save_state(state)
                self._upsert_probe_run(state, "FAILED_SAMPLE_INVALID")
                self._release_lock(str(state["lock_key"]), str(state["client_order_id"]))
                return self._synthetic_snapshot(state, OrderStatus.REJECTED)

            state.update(
                stage="FLAT",
                flatten_venue_order_id=str(flatten.venue_order_id or state.get("flatten_venue_order_id") or ""),
                flatten_average_price=str(flatten_average),
                residual_quantity="0",
                final_position_quantity=str(final_quantity),
                flat_confirmed_at=utc_now().isoformat(),
                error="",
            )
            self._save_state(state)
            self._upsert_probe_run(state, "FLAT")
            self._release_lock(str(state["lock_key"]), str(state["client_order_id"]))
            return self._synthetic_snapshot(state, OrderStatus.FILLED)
        except Exception as exc:
            state.update(stage="RECOVERY_REQUIRED", error=f"{type(exc).__name__}: {exc}")
            self._save_state(state)
            self._upsert_probe_run(state, "RECOVERY_REQUIRED")
            raise

    def _entry_order(self, state: dict[str, Any]) -> OrderSnapshot:
        # 只有中断恢复时才单次查单；首次提交禁止以 REST 查询充当成交确认。
        if str(state.get("stage") or "PREPARED") != "PREPARED":
            existing = self._query_actual(state, flatten=False)
            if existing is not None:
                self._event_waiter.seed(existing)
                return existing
        request = OrderRequest(
            venue=self.venue,
            symbol=str(state["symbol"]),
            side=Side(str(state["side"])),
            quantity=Decimal(str(state["probe_quantity"])),
            client_order_id=str(state["entry_client_order_id"]),
            order_type=OrderType(str(state["order_type"])),
            price=Decimal(str(state["price"])) if state.get("price") else None,
            time_in_force=TimeInForce.GTC,
            post_only=str(state["order_type"]) == OrderType.LIMIT.value,
            reduce_only=False,
            position_side=PositionSide(str(state["probe_position_side"])),
        )
        result = self.live.submit_order(request)
        self._event_waiter.seed(result)
        state.update(stage="ENTRY_SUBMITTED", entry_venue_order_id=result.venue_order_id)
        self._save_state(state)
        return result

    def _wait_for_entry(self, state: dict[str, Any], snapshot: OrderSnapshot) -> OrderSnapshot:
        if str(state["order_type"]) != OrderType.LIMIT.value:
            return self._wait_for_terminal(
                state,
                snapshot,
                str(state["entry_client_order_id"]),
                float(state["flatten_timeout_seconds"]),
            )
        current = self._event_waiter.wait_until(
            client_order_id=str(state["entry_client_order_id"]),
            venue_order_id=str(snapshot.venue_order_id or state.get("entry_venue_order_id") or ""),
            timeout_seconds=float(state["maker_timeout_seconds"]),
            predicate=lambda item: item.status in TERMINAL_STATUSES,
        ) or snapshot
        if current.status in ACTIVE_STATUSES:
            current = self.live.cancel_order(
                str(state["symbol"]),
                client_order_id=str(state["entry_client_order_id"]),
                venue_order_id=str(current.venue_order_id or state.get("entry_venue_order_id") or ""),
            )
            self._event_waiter.seed(current)
            # 必须确认 Maker 剩余量已经撤销，再按最终成交量反向回平；否则撤单在途
            # 期间继续成交会留下未被回平的真实仓位。
            current = self._wait_for_terminal(
                state,
                current,
                str(state["entry_client_order_id"]),
                float(state["flatten_timeout_seconds"]),
            )
        return current

    def _flatten_order(self, state: dict[str, Any], quantity: Decimal) -> OrderSnapshot:
        # 已有回平单 ID 表示进程可能在提交后中断，此时只做一次恢复查询。
        if state.get("flatten_venue_order_id"):
            existing = self._query_actual(state, flatten=True)
            if existing is not None:
                self._event_waiter.seed(existing)
                return existing
        side = Side.SELL if Side(str(state["side"])) == Side.BUY else Side.BUY
        request = OrderRequest(
            venue=self.venue,
            symbol=str(state["symbol"]),
            side=side,
            quantity=quantity,
            client_order_id=str(state["flatten_client_order_id"]),
            order_type=OrderType.MARKET,
            reduce_only=self.venue != "binance",
            position_side=PositionSide(str(state["probe_position_side"])),
        )
        result = self.live.submit_order(request)
        self._event_waiter.seed(result)
        state["flatten_venue_order_id"] = result.venue_order_id
        self._save_state(state)
        return result

    def _wait_for_terminal(
        self,
        state: dict[str, Any],
        snapshot: OrderSnapshot,
        client_order_id: str,
        timeout_seconds: float,
    ) -> OrderSnapshot:
        if snapshot.status not in ACTIVE_STATUSES:
            return snapshot
        return self._event_waiter.wait_for_terminal(
            client_order_id=client_order_id,
            venue_order_id=str(snapshot.venue_order_id or ""),
            timeout_seconds=timeout_seconds,
        )

    def _query_actual(self, state: dict[str, Any], *, flatten: bool) -> OrderSnapshot | None:
        client_key = "flatten_client_order_id" if flatten else "entry_client_order_id"
        venue_key = "flatten_venue_order_id" if flatten else "entry_venue_order_id"
        try:
            return self.live.get_order(
                str(state["symbol"]),
                client_order_id=str(state[client_key]),
                venue_order_id=str(state.get(venue_key) or ""),
            )
        except (LookupError, ValueError):
            return None

    def _filled_details(
        self,
        state: dict[str, Any],
        snapshot: OrderSnapshot,
        *,
        flatten: bool = False,
    ) -> tuple[Decimal, Decimal]:
        client_key = "flatten_client_order_id" if flatten else "entry_client_order_id"
        quantity = Decimal(str(snapshot.filled_quantity or 0))
        average = Decimal(str(snapshot.average_price or 0))
        details_kwargs = {
            "client_order_id": str(state[client_key]),
            "venue_order_id": str(snapshot.venue_order_id or ""),
        }
        if average > 0:
            fill_quantity, fill_average = self._event_waiter.fill_details(**details_kwargs)
        else:
            fill_quantity, fill_average = self._event_waiter.wait_for_fill_details(
                **details_kwargs,
                minimum_quantity=quantity,
                timeout_seconds=min(float(state.get("flatten_timeout_seconds") or 2), 2.0),
            )
        if fill_quantity > 0:
            quantity = max(quantity, fill_quantity)
            average = fill_average
        return quantity, average

    def _position_quantity(self, symbol: str) -> Decimal:
        normalized = symbol.upper().replace("/", "").replace("-", "")
        total = Decimal("0")
        for position in self.live.get_positions() or []:
            candidate = str(position.symbol or "").upper().replace("/", "").replace("-", "")
            if normalized != candidate and normalized not in candidate and candidate not in normalized:
                continue
            total += abs(Decimal(str(position.quantity or 0)))
        return total

    def _market_depth_adjustment(self, request: OrderRequest) -> Decimal:
        """用实时 L2 深度补偿最小探针与策略目标数量之间的非线性滑点。"""
        book = self.live.get_order_book(request.symbol, 50)
        levels = book.asks if request.side == Side.BUY else book.bids
        remaining = Decimal(str(request.quantity))
        if remaining <= 0 or not levels:
            raise ValueError("策略目标数量或实时订单簿无效")
        top = Decimal(str(levels[0][0]))
        notional = Decimal("0")
        for raw_price, raw_size in levels:
            price = Decimal(str(raw_price))
            size = Decimal(str(raw_size))
            taken = min(max(size, Decimal("0")), remaining)
            notional += taken * price
            remaining -= taken
            if remaining <= Decimal("1e-12"):
                break
        if remaining > Decimal("1e-12"):
            raise ValueError(f"实时 L2 深度不足以覆盖策略目标数量，缺口 {remaining}")
        vwap = notional / Decimal(str(request.quantity))
        return vwap - top

    def _reserve_daily_limits(self, probe_notional: Decimal, limits: dict[str, float | int]) -> str:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        runs_key = redis_key("paper-probe", "daily", day, self.venue, "runs")
        notional_key = redis_key("paper-probe", "daily", day, self.venue, "notional")
        runs = int(self.redis.incr(runs_key))
        notional = float(self.redis.incrbyfloat(notional_key, float(probe_notional * 2)))
        self.redis.expire(runs_key, 172800)
        self.redis.expire(notional_key, 172800)
        if runs > int(limits["paper_probe_daily_max_runs"]):
            return "已超过当日真实探针次数上限"
        if notional > float(limits["paper_probe_daily_max_notional"]):
            return "已超过当日真实探针成交名义金额上限"
        return ""

    def _synthetic_snapshot(self, state: dict[str, Any], status: OrderStatus) -> OrderSnapshot:
        target = Decimal(str(state.get("target_quantity") or 0))
        average = Decimal(str(state.get("projected_average_price") or 0)) or None
        return OrderSnapshot(
            venue=self.venue,
            symbol=str(state["symbol"]),
            client_order_id=str(state["client_order_id"]),
            venue_order_id=f"probe:{state.get('entry_venue_order_id') or ''}",
            status=status,
            side=Side(str(state["side"])),
            order_type=OrderType(str(state["order_type"])),
            requested_quantity=target,
            filled_quantity=target if status == OrderStatus.FILLED else Decimal("0"),
            remaining_quantity=Decimal("0") if status == OrderStatus.FILLED else target,
            average_price=average,
            price=Decimal(str(state["price"])) if state.get("price") else None,
            commission=Decimal("0"),
            position_side=PositionSide(str(state["strategy_position_side"])),
            error_message=str(state.get("error") or "") if status == OrderStatus.REJECTED else "",
            raw={"paper_execution": "real_minimum_probe", "probe_status": state.get("stage")},
        )

    def _rejected(self, request: OrderRequest, message: str) -> OrderSnapshot:
        return OrderSnapshot(
            venue=self.venue,
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            venue_order_id="",
            status=OrderStatus.REJECTED,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.quantity,
            remaining_quantity=request.quantity,
            price=request.price,
            position_side=request.position_side,
            error_message=message,
            raw={"error_message": message, "paper_execution": "real_minimum_probe"},
        )

    def _fail_before_exposure(self, request: OrderRequest, lock_key: str, message: str) -> OrderSnapshot:
        self._release_lock(lock_key, request.client_order_id)
        return self._rejected(request, message)

    def _state_key(self, client_order_id: str) -> str:
        return redis_key("paper-probe", "state", client_order_id)

    def _load_state(self, client_order_id: str) -> dict[str, Any] | None:
        raw = self.redis.get(self._state_key(client_order_id))
        return json.loads(raw) if raw else None

    def _save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now().isoformat()
        self.redis.set(self._state_key(str(state["client_order_id"])), json.dumps(state), ex=604800)

    def _release_lock(self, key: str, owner: str) -> None:
        if self.redis.get(key) == owner:
            self.redis.delete(key)

    def _upsert_probe_run(self, state: dict[str, Any], status: str) -> None:
        with SessionLocal() as db:
            row = db.query(ProbeRun).filter(ProbeRun.idempotency_key == str(state["client_order_id"])).one_or_none()
            if row is None:
                row = ProbeRun(
                    idempotency_key=str(state["client_order_id"]),
                    hedge_group_id=int(state.get("hedge_group_id") or 0) or None,
                    entry_intent_id=int(state.get("intent_id") or 0) or None,
                    purpose=f"PAPER_{str(state.get('action') or 'OPEN').upper()}",
                    venue=self.venue,
                    instrument_id=str(state["symbol"]),
                    position_side=str(state["probe_position_side"]),
                    entry_side=str(state["side"]).upper(),
                    probe_quantity=float(state["probe_quantity"]),
                    baseline_position_quantity=float(state.get("baseline_quantity") or 0),
                )
            row.open_fill_price = _optional_float(state.get("probe_average_price"))
            row.close_fill_price = _optional_float(state.get("flatten_average_price"))
            row.residual_quantity = float(state.get("residual_quantity") or 0)
            row.final_position_quantity = _optional_float(state.get("final_position_quantity"))
            row.flat_confirmed_at = utc_now() if status == "FLAT" else row.flat_confirmed_at
            row.status = status
            row.error_message = str(state.get("error") or "")
            db.add(row)
            db.commit()


def _probe_quantity(instrument: Any, reference_price: Decimal) -> Decimal:
    step = Decimal(str(instrument.quantity_step or 0))
    minimum = Decimal(str(instrument.minimum_quantity or 0))
    min_notional = Decimal(str(instrument.minimum_notional or 0))
    if reference_price <= 0:
        raise ValueError("探针参考价格无效")
    if min_notional > 0:
        minimum = max(minimum, min_notional / reference_price)
    if step > 0:
        minimum = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    if minimum <= 0:
        raise ValueError("交易所最小探针数量无效")
    return minimum


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _settings():
    from app.config.settings import get_settings

    return get_settings()
