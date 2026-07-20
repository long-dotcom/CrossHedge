"""品种映射交易所规格同步测试。"""

from decimal import Decimal

from app.api import settings_api
from app.db.models import SymbolMapping
from app.venues.domain.models import Instrument


class _Connector:
    def __init__(self, instrument: Instrument):
        self.instrument = instrument

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        assert symbol == self.instrument.symbol
        assert refresh is True
        return self.instrument


def test_sync_mapping_reads_crypto_and_mt5_specs(monkeypatch) -> None:
    binance = Instrument(
        venue="binance",
        symbol="XAUUSDT",
        base_asset="XAU",
        quote_asset="USDT",
        settlement_asset="USDT",
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        price_tick=Decimal("0.01"),
        minimum_notional=Decimal("5"),
    )
    mt5 = Instrument(
        venue="mt5",
        symbol="GOLD",
        base_asset="XAU",
        quote_asset="USD",
        settlement_asset="USD",
        quantity_step=Decimal("0.01"),
        minimum_quantity=Decimal("0.01"),
        price_tick=Decimal("0.001"),
        contract_size=Decimal("100"),
        raw={"digits": 3, "trade_calc_mode": 1},
    )
    connectors = {"binance": _Connector(binance), "mt5": _Connector(mt5)}
    monkeypatch.setattr(
        settings_api.native_venue_manager,
        "connector_for",
        lambda venue, environment: connectors[venue],
    )
    mapping = SymbolMapping(
        symbol="GOLD-1",
        leg_a_venue="binance",
        leg_a_symbol="XAUUSDT",
        leg_a_venue_symbol="XAUUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="GOLD",
        mt5_symbol="GOLD",
        price_precision=3,
        min_tick=0.001,
    )

    synced = settings_api._sync_mapping_instruments(mapping)

    assert [item["venue"] for item in synced] == ["binance", "mt5"]
    assert mapping.quantity_precision == 3
    assert mapping.price_precision == 2
    assert mapping.min_tick == 0.01
    assert mapping.leg_a_min_base_size == 0.001
    assert mapping.leg_a_min_notional == 5
    assert mapping.mt5_min_lot == 0.01
    assert mapping.mt5_volume_step == 0.01
    assert mapping.mt5_contract_size == 100
    assert mapping.min_order_size == 1
