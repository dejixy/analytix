from engine.engineFlow import WARMUP_MS, EngineFlow, is_forced
from models.orderModel import AggressiveOrder, is_sweep, is_twap_slice
from models.tradeModel import TradeSide
from tests.helpers import T0

S = 1000
SELL, BUY = TradeSide.SELL, TradeSide.BUY


def _eng(ts, wallet, usd=20_000, side=SELL, engine=True, levels=1):
    return AggressiveOrder(ts, side, usd, usd / 3000, levels, levels, 3000.0, 3000.0, taker=wallet, engine=engine)


def _warm(ef):
    """Run the classifier past its warm-up with an unrelated engine fill."""
    ef.observe(_eng(T0 - WARMUP_MS - S, "0xearly", usd=1_000, side=BUY))


def test_a_wallet_slicing_at_a_fixed_interval_and_size_is_a_twap():
    ef = EngineFlow()
    for i, usd in enumerate((20_000, 21_500)):
        ef.observe(_eng(T0 + i * 30 * S + (150 if i else 0), "0xtwap", usd))
    assert ("0xtwap", SELL) not in ef.twaps                      # two slices: too early to say
    ef.observe(_eng(T0 + 60 * S - 200, "0xtwap", 18_500))        # third, same cadence, similar size
    twaps, _ = ef.view()
    assert twaps[("0xtwap", SELL)][1] == 20_000
    assert is_twap_slice(_eng(T0, "0xtwap", 24_000), twaps)
    assert not is_twap_slice(_eng(T0, "0xtwap", 24_000, side=BUY), twaps)        # other side: not the TWAP


def test_a_twap_wallet_that_gets_liquidated_is_not_excused():
    ef = EngineFlow()
    for i in range(3):
        ef.observe(_eng(T0 + i * 30 * S, "0xtwap", 20_000))
    twaps, _ = ef.view()
    big = _eng(T0 + 75 * S, "0xtwap", 2_000_000, levels=12)
    assert not is_twap_slice(big, twaps) and is_sweep(big, 100_000, twaps)
    catch_up = _eng(T0 + 90 * S, "0xtwap", 60_000, levels=12)   # a 3× catch-up slice walking the book
    assert is_sweep(catch_up, 100_000) and not is_sweep(catch_up, 100_000, twaps)    # …is excused as a slice


def test_a_liquidation_in_uneven_pieces_is_not_mistaken_for_a_twap():
    ef = EngineFlow()
    for t, usd in ((0, 200_000), (30, 700_000), (60, 100_000)):
        ef.observe(_eng(T0 + t * S, "0xliq", usd))
    assert ("0xliq", SELL) not in ef.twaps
    _, forced = ef.view()
    assert {(T0, "0xliq"), (T0 + 30 * S, "0xliq")} <= forced     # the 20% chunk, then the rest


def test_irregular_engine_fills_are_not_a_twap():
    ef = EngineFlow()
    for t in (0, 30, 95):
        ef.observe(_eng(T0 + t * S, "0xodd"))
    assert ("0xodd", SELL) not in ef.twaps


def test_many_accounts_closed_in_the_same_instant_are_forced_after_warmup():
    cold = EngineFlow()
    for i, w in enumerate(["0xa", "0xb", "0xc"]):
        cold.observe(_eng(T0 + i * 800, w, usd=80_000))
    assert not cold.view()[1]                                    # at start-up these could be running TWAPs
    ef = EngineFlow()
    _warm(ef)
    for i, w in enumerate(["0xa", "0xb", "0xc"]):
        ef.observe(_eng(T0 + i * 800, w, usd=80_000))
    _, forced = ef.view()
    assert {(T0, "0xa"), (T0 + 800, "0xb"), (T0 + 1600, "0xc")} <= forced
    assert is_forced(_eng(T0, "0xa"), forced) and not is_forced(_eng(T0, "0xa", engine=False), forced)


def test_wallets_with_recent_engine_history_are_left_out_of_clusters():
    ef = EngineFlow()
    _warm(ef)
    for w in ("0xa", "0xb", "0xc"):                              # each sliced 30s ago: TWAPs in the making
        ef.observe(_eng(T0 - 30 * S, w, usd=10_000))
    for i, w in enumerate(("0xa", "0xb", "0xc")):
        ef.observe(_eng(T0 + i * 500, w, usd=10_000))
    assert not ef.view()[1]


def test_a_partial_liquidation_needs_a_real_first_chunk_and_a_lone_fill_is_not_guessed():
    ef = EngineFlow()
    ef.observe(_eng(T0, "0xbig", usd=200_000))                  # 20% chunk of a $1M position
    assert not ef.view()[1]                                      # alone, it could be a TWAP's first slice
    ef.observe(_eng(T0 + 12 * S, "0xbig", usd=800_000))          # the remaining 80% inside the cooldown
    assert {(T0, "0xbig"), (T0 + 12 * S, "0xbig")} <= ef.view()[1]
    small = EngineFlow()                                         # a TWAP catch-up after a tiny under-fill
    small.observe(_eng(T0, "0xt", usd=4_000))
    small.observe(_eng(T0 + 30 * S, "0xt", usd=36_000))
    assert not small.view()[1]


def test_two_twaps_from_one_wallet_are_not_called_liquidations_for_long():
    ef = EngineFlow()
    for i in range(6):                                           # $25K and $100K TWAPs interleaved, 15s apart
        usd = 25_000 if i % 2 == 0 else 100_000
        ef.observe(_eng(T0 + i * 15 * S, "0xtwo", usd))
    assert not ef.view()[1]


def test_a_liquidation_of_a_known_twap_does_not_drag_its_last_slice_in():
    ef = EngineFlow()
    for i in range(3):
        ef.observe(_eng(T0 + i * 30 * S, "0xtwap", 30_000))
    ef.observe(_eng(T0 + 70 * S, "0xtwap", 500_000))             # far beyond a slice: not excused…
    assert (T0 + 60 * S, "0xtwap") not in ef.view()[1]           # …but the previous slice isn't "liquidated"


def test_the_largest_legitimate_catch_up_is_still_a_slice():
    ef = EngineFlow()
    for i in range(3):
        ef.observe(_eng(T0 + i * 30 * S, "0xtwap", 30_000))
    twaps, _ = ef.view()
    assert is_twap_slice(_eng(T0 + 90 * S, "0xtwap", 108_000), twaps)   # 1.2 × 3 catch-up


def test_who_survives_a_wallet_whose_orders_share_one_timestamp():
    from dataclasses import replace
    from engine.summary import who_row
    from tests.helpers import make_slice
    orders = [_eng(T0, "0xw", 40_000)] + [_eng(T0, "0xw", 5_000, engine=False) for _ in range(3)]
    orders += [_eng(T0 + i * S, f"0xo{i}", 1_000, side=BUY, engine=False) for i in range(6)]
    m = who_row(replace(make_slice(seconds=60), orders=orders, twaps={("0xw", SELL): (T0, 40_000.0, 30_000)}))
    assert m.value.startswith("1 TWAP =")
