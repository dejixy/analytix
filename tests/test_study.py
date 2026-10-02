import math

from scripts.study import Occurrence, attach_forward_returns, run, summarize


def test_forward_moves_are_signed_by_the_implied_direction():
    mids = [(i * 1000, 3000.0 + i * 0.03) for i in range(4000)]          # a steady rise: +1 bp every 10s
    up, down = Occurrence("a", 0, +1, 3000.0), Occurrence("a", 0, -1, 3000.0)
    attach_forward_returns([up, down], mids, horizons_s=(60, 300, 7200))
    assert abs(up.forward[60] - 6.0) < 1e-6 and abs(down.forward[60] + 6.0) < 1e-6
    assert up.forward[7200] is None                                      # beyond the recording


def test_summary_reports_hit_rate_mean_and_t():
    occ = [Occurrence("x", i, 1, 1.0, {60: v}) for i, v in enumerate([2.0, 4.0, -1.0, 3.0])]
    (row,) = summarize(occ, horizons_s=(60,))
    n, hit, avg, t = row.stats[60]
    assert (n, hit, avg) == (4, 0.75, 2.0) and abs(t - 2.0 / (math.sqrt(14 / 3) / 2)) < 1e-9


def test_study_runs_over_the_synthetic_session(tmp_path):
    from ingestion.synthetic import generate_session
    path = generate_session(tmp_path / "s.jsonl")
    occ, rows = run([str(path)], "ETH")
    signals = {r.signal for r in rows}
    assert "cascade, likely liquidations: fade it" in signals     # the scripted long liquidation
    assert "level broke: follow the break" in signals             # which breaks the absorption-phase bids
    assert all(o.forward for o in occ)
