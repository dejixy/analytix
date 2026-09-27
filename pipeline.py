"""
Wires the pipeline stages together: raw message → parse → state → engine.

Used by the API runtime, the replay report script and the tests, so there is
exactly one definition of "process a message".
"""
from typing import Any

from engine.analyzer import Analyzer
from ingestion.parsers import parse_message
from state import MarketState


class Pipeline:
    def __init__(self, coin: str, analysis_interval_ms: int | None = None):
        self.coin = coin
        self.state = MarketState(coin)
        self.analyzer = (Analyzer(self.state, interval_ms=analysis_interval_ms)
                         if analysis_interval_ms else Analyzer(self.state))

    def on_message(self, msg: dict[str, Any]) -> None:
        self.state.apply_many(parse_message(msg, self.coin))
        self.analyzer.maybe_run()

    def reset(self) -> None:
        self.state.reset()
        self.analyzer.reset()
