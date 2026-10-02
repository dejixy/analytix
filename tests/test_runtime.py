from api.runtime import Runtime
from tests.test_parsers import BOOK, TRADES


def _as(coin, msg):
    data = msg["data"]
    data = [{**t, "coin": coin} for t in data] if isinstance(data, list) else {**data, "coin": coin}
    return {**msg, "data": data}


def test_messages_are_routed_to_their_coin():
    rt = Runtime("live", coins=["ETH", "BTC"])
    rt.on_message(_as("BTC", TRADES))
    rt.on_message(_as("BTC", BOOK))
    assert len(rt.pipeline("BTC").state.trades) == 2 and rt.pipeline("BTC").state.book
    assert len(rt.pipeline("ETH").state.trades) == 0 and rt.pipeline("ETH").state.book is None


def test_replay_watches_the_coins_in_the_recording(tmp_path):
    import json
    path = tmp_path / "rec.jsonl"
    path.write_text("\n".join(json.dumps({"recv_ms": i, "msg": _as(c, TRADES)}) for i, c in enumerate(["SOL", "ETH", "SOL"])))
    rt = Runtime("replay", coins=["BTC"], default_coin="ETH", replay_file=path)
    assert rt.coins == ["SOL", "ETH"] and rt.default_coin == "ETH"


def test_live_mode_keeps_the_outlook_track_record_and_replay_never_does(tmp_path):
    rt = Runtime("live", coins=["ETH", "BTC"], bars_db=None, outlook_db=tmp_path / "outlook.sqlite")
    rt._load_track_records()
    assert rt.track_store and all(p.analyzer.tracker._store is rt.track_store for p in rt.pipelines.values())
    rt.track_store.close()
    replay = Runtime("replay", coins=["ETH"], replay_file=tmp_path / "none.jsonl", outlook_db=tmp_path / "x.sqlite")
    assert replay.outlook_db is None
