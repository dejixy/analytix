from engine.engineFlow import EngineFlow, is_forced, is_twap
from models.orderModel import AggressiveOrder
from models.tradeModel import TradeSide
from tests.helpers import T0

S = 1000
SELL, BUY = TradeSide.SELL, TradeSide.BUY


def _eng(ts, wallet, usd=20_000, side=SELL, engine=True):
    return AggressiveOrder(ts, side, usd, usd / 3000, 1, 1, 3000.0, 3000.0, taker=wallet, engine=engine)


def test_a_wallet_slicing_at_a_fixed_interval_is_a_twap():
    ef = EngineFlow()
    for i in range(2):
        ef.observe(_eng(T0 + i * 30 * S + (150 if i else 0), "0xtwap"))
    assert "0xtwap" not in ef.twap_wallets                      # two slices: too early to say
    ef.observe(_eng(T0 + 60 * S - 200, "0xtwap"))               # third, at the same ~30s cadence
    tw, _ = ef.view()
    assert "0xtwap" in tw and is_twap(_eng(T0, "0xtwap"), tw)


def test_irregular_engine_fills_are_not_a_twap():
    ef = EngineFlow()
    for t in (0, 30, 95):
        ef.observe(_eng(T0 + t * S, "0xodd"))
    assert "0xodd" not in ef.twap_wallets


def test_many_accounts_closed_in_the_same_instant_are_forced():
    ef = EngineFlow()
    for i, w in enumerate(["0xa", "0xb", "0xc"]):
        ef.observe(_eng(T0 + i * 800, w, usd=80_000))
    _, forced = ef.view()
    assert {(T0, "0xa"), (T0 + 800, "0xb"), (T0 + 1600, "0xc")} <= forced
    assert is_forced(_eng(T0, "0xa"), forced)
    assert not is_forced(_eng(T0, "0xa", engine=False), forced)   # only engine-executed orders count


def test_a_partial_liquidation_then_the_rest_is_forced_but_a_lone_fill_is_not_guessed():
    ef = EngineFlow()
    ef.observe(_eng(T0, "0xbig", usd=200_000))                  # 20% chunk of a $1M position
    _, forced = ef.view()
    assert not forced                                            # alone, it could be a TWAP's first slice
    ef.observe(_eng(T0 + 12 * S, "0xbig", usd=800_000))          # the remaining 80% inside the 30s cooldown
    _, forced = ef.view()
    assert {(T0, "0xbig"), (T0 + 12 * S, "0xbig")} <= forced


def test_twap_slices_never_count_as_forced_even_if_they_land_together():
    ef = EngineFlow()
    for k in range(3):                                           # three TWAPs established first
        for i in range(3):
            ef.observe(_eng(T0 + i * 30 * S + k * 100, f"0xt{k}"))
    for k in range(3):                                           # their next slices coincide
        ef.observe(_eng(T0 + 90 * S + k * 100, f"0xt{k}"))
    _, forced = ef.view()
    assert not forced
