import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import structural  # noqa: E402
from kalshi_engine.relations import Contract  # noqa: E402


def C(ticker, title, rules, venue=None):
    return Contract(venue=venue or ("polymarket" if ticker.startswith("PM-") else "kalshi"), ticker=ticker,
                    event_id="", category=None, title=title, context=rules, close_time=None, yes_bid=None, yes_ask=None)


# Rule wording as the venues publish it (2026-09).
CLOSE = ('This market will resolve according to the final "Close" price of the Binance 1 minute candle for {s}/USDT '
         '12:00 in the ET timezone (noon) on the date specified in the title.')
ABOVE = ('This market will resolve to "Yes" if the Binance 1 minute candle for {s}/USDT 12:00 in the ET timezone (noon) '
         'on the date specified in the title has a final "Close" price higher than the price specified in the title.')
REACH_MONTH = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for {s}/USDT during the month '
               'specified in the title (from 00:00 AM ET on the first day to 11:59 PM ET on the last), has a final High '
               'price equal to or greater than the price specified in the title.')
DIP_RANGE = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for {s}/USDT during the date '
             'range specified in the title (from 12:00 AM ET on the first date to 11:59 PM ET on the last) has a final '
             '"Low" price equal to or lower than the price specified in the title.')
HIT_BY = ('This market will immediately resolve to "Yes" if any Binance 1 minute candle for Solana (SOL/USDT) from the '
          'creation of this market until 11:59 PM ET on the date specified in the title has a final "High" price.')


def sol_range(lo, hi, day=28):
    return C(f"PM-will-the-price-of-solana-be-between-{lo}-{hi}-on-september-{day}-2026",
             f"Will the price of Solana be between ${lo} and ${hi} on September {day}?", CLOSE.format(s="SOL"))


def sol_above(x, day=28):
    return C(f"PM-solana-above-{x}-on-september-{day}-2026", f"Will the price of Solana be above ${x} on September {day}?",
             ABOVE.format(s="SOL"))


def rels(a, b):
    v = structural.judge(a, b)
    return None if v is None else v.relations


def test_crypto_range_vs_above():
    assert rels(sol_range(110, 120), sol_above(120)) == ["mutually_exclusive"]  # [110,120) vs >120
    assert rels(sol_above(110), sol_range(110, 120)) == []  # a close of exactly 110 is in the range, not above
    assert rels(sol_range(120, 130), sol_above(110)) == ["a_implies_b"]
    assert rels(sol_range(110, 120, day=27), sol_above(120, day=28)) == []  # different days
    assert rels(sol_range(110, 120), sol_range(120, 130)) == ["mutually_exclusive"]


def test_crypto_close_vs_path_windows():
    reach = C("PM-will-solana-reach-120-in-september-2026", "Will Solana reach $120 in September?", REACH_MONTH.format(s="SOL"))
    assert rels(sol_above(120), reach) == ["a_implies_b"]  # close > 120 means that candle's high > 120
    assert rels(sol_range(110, 120), reach) == []
    dip = C("PM-will-solana-dip-to-110-september-21-27-2026", "Will Solana dip to $110 September 21-27?", DIP_RANGE.format(s="SOL"))
    assert rels(sol_range(100, 110, day=25), dip) == ["a_implies_b"]
    assert rels(sol_range(100, 110, day=28), dip) == []  # Sep 28 is outside Sep 21-27
    hit = C("PM-will-solana-hit-150-by-september-30-2026", "Will Solana hit $150 by September 30, 2026?", HIT_BY)
    assert structural.parse_crypto(hit) is None  # window starts at market creation: not read


def test_crypto_different_coins_unrelated():
    xrp = C("PM-xrp-above-120-on-september-28-2026", "Will the price of XRP be above $120 on September 28?", ABOVE.format(s="XRP"))
    assert rels(sol_range(110, 120), xrp) == []


YT = "If {n} has above {x} Global daily views on YouTube {w}, then the market resolves to Yes."


def test_youtube_day_implies_week_at_same_or_lower_strike():
    day = C("KXYTVIEWSD-YOU26SEP27-12.0M", "Will YoungBoy Never Broke Again have above 12M daily views on Sep 27, 2026? -- Above 12M",
            YT.format(n="YoungBoy Never Broke Again", x="12M", w="on Sep 27, 2026"))
    week = C("KXYTVIEWSW-YOU26SEP27-12.0M", "Will NBA YoungBoy have above 12M daily views at any point during September 21, 2026 - September 27, 2026? -- Above 12M",
             YT.format(n="NBA YoungBoy", x="12M", w="at any point during September 21, 2026 - September 27, 2026"))
    week_hi = C("KXYTVIEWSW-YOU26SEP27-14.0M", week.title, week.context)
    month = C("KXYTVIEWSHIGH-YOU26OCT-10.0M", "Will NBA YoungBoy have above 10M daily views at any point during September 2026? -- Above 10M",
              YT.format(n="NBA YoungBoy", x="10M", w="at any point during September 2026"))
    assert rels(day, week) == ["a_implies_b"]
    assert rels(day, week_hi) == []  # 12M on the day doesn't reach a 14M bar
    assert rels(week, month) == ["a_implies_b"]
    other = C("KXYTVIEWSW-BAD26SEP27-12.0M", "Will Bad Bunny have above 12M daily views ...", week.context.replace("NBA YoungBoy", "Bad Bunny"))
    assert rels(day, other) is None  # different artist: not the rules' call


def test_album_pure_implies_equivalent():
    lum = "resolve based on the value reported by Luminate's API"
    pure = C("KXPUREALBUMS-POP26OCT01-7K", "Will Popstar by Tinashe have above 7000 Pure Album Sales ...", lum)
    equiv = C("KXALBUMEQUIV-POP26OCT01-7K", "Will Popstar by Tinashe have above 7000 Album Equivalent Units ...", lum)
    equiv_hi = C("KXALBUMEQUIV-POP26OCT01-10K", equiv.title, lum)
    assert rels(equiv, pure) == ["b_implies_a"]
    assert rels(pure, equiv_hi) == []


def nf(title, rank, pos_word="#2"):
    return C(f"PM-will-{title.lower().replace(' ', '-')}-be-the-{rank}-global-netflix-movie-this-week-20260929",
             f'Will "{title}" be the {pos_word} global Netflix movie this week?', "Netflix global top 10 movies")


def test_rank_same_title_and_different_titles():
    assert rels(nf("Best of the Best", "top", "top"), nf("Best of the Best", "2")) == ["mutually_exclusive"]
    assert rels(nf("Best of the Best", "top", "top"), nf("Unabomber", "2")) == []  # both can happen
    assert rels(nf("Riot", "top", "top"), nf("Unabomber", "top", "top")) == ["mutually_exclusive"]  # one #1 per chart


def test_restrict_strips_jev_relations_between_different_titles():
    kalshi = C("KXNETFLIXRANKMOVIEGLOBAL-26SEP28-WHY", "Will Why Did I Get Married Again? be Top Global Netflix Movie on Sep 28, 2026? -- Why Did I Get Married Again?",
               "If Why Did I Get Married Again? is #1 on the Netflix Top 10 Global Movie on the chart published on Sep 29, 2026, then")
    pm = nf("Unabomber", "2")
    kept, why = structural.restrict(kalshi, pm, ["mutually_exclusive"])
    assert kept == [] and why


def test_family_pairs_finds_ladder_pairs():
    ms = [sol_range(110, 120), sol_above(120), sol_above(110), sol_range(120, 130)]
    pairs = {(a.ticker, b.ticker) for a, b in structural.family_pairs(ms)}
    assert (sol_range(110, 120).ticker, sol_above(120).ticker) in pairs or (sol_above(120).ticker, sol_range(110, 120).ticker) in pairs
    assert all(a.ticker < b.ticker for a, b in structural.family_pairs(ms))


def sol_less(x, day=28):
    return C(f"PM-will-the-price-of-solana-be-less-than-{x}-on-september-{day}-2026",
             f"Will the price of Solana be less than ${x} on September {day}?", CLOSE.format(s="SOL"))


def sol_greater(x, day=28):
    return C(f"PM-will-the-price-of-solana-be-greater-than-{x}-on-september-{day}-2026",
             f"Will the price of Solana be greater than ${x} on September {day}?", CLOSE.format(s="SOL"))


def test_covers_all_needs_a_gapless_bracket_set():
    full = [sol_less(100), sol_range(100, 110), sol_range(110, 120), sol_greater(120)]
    assert structural.covers_all(full)
    assert not structural.covers_all([sol_less(100), sol_range(110, 120), sol_greater(120)])  # 100-110 missing
    assert not structural.covers_all(full[:-1])  # nothing above 120
    assert not structural.covers_all(full[:-1] + [sol_greater(120, day=27)])  # mixed dates


def test_hit_by_windows_start_at_market_creation():
    def hit(x, created):
        c = C(f"PM-will-solana-hit-{x}-by-september-30-2026", f"Will Solana hit ${x} by September 30, 2026?", HIT_BY)
        c.opened_at = created
        return c
    early, late = hit(150, "2026-08-20T19:30:49Z"), hit(300, "2026-08-20T19:31:11Z")
    assert rels(early, late) == ["b_implies_a"]  # $300 within a window inside $150's implies $150
    assert structural.parse_crypto(hit(150, None)) is None  # creation time unknown: not read
    reach_day = C("PM-will-solana-reach-160-on-august-20-2026", "Will Solana reach $160 on August 20?",
                  REACH_MONTH.format(s="SOL").replace("during the month specified in the title (from 00:00 AM ET on the first day to 11:59 PM ET on the last)", "on the date specified in the title, between 12:00 AM ET and 11:59 PM ET"))
    assert rels(reach_day, early) == []  # Aug 20 starts before creation that afternoon


def test_econ_above_vs_exactly_same_release():
    bls = "as reported by the Bureau of Labor Statistics"
    above = C("KXCPIYOY-26SEP-T3.0", "Will the rate of CPI inflation be above 3.0% ...", f"increases by more than 3.0% ({bls})")
    exact = C("KXECONSTATCPIYOY-26SEP-T3.0", "CPI year-over-year in Sep 2026? -- Exactly 3.0%", "exactly 3.0%")
    exact_hi = C("KXECONSTATCPIYOY-26SEP-T3.2", "CPI year-over-year in Sep 2026? -- Exactly 3.2%", "exactly 3.2%")
    other_month = C("KXECONSTATCPIYOY-26OCT-T3.2", "CPI year-over-year in Oct 2026? -- Exactly 3.2%", "exactly 3.2%")
    assert rels(above, exact) == ["mutually_exclusive"]  # exactly 3.0 is not above 3.0
    assert rels(exact_hi, above) == ["a_implies_b"]
    assert rels(above, other_month) is None  # different month: not the rules' call


def test_ism_kalshi_vs_polymarket_brackets():
    k = C("KXISMPMI-26SEP-51", "ISM Manufacturing PMI in Sep 2026? -- At least 51", "is at least 51, then the market resolves to Yes.")
    pm = C("PM-will-ism-manufacturing-pmi-be-between-55pt0-and-55pt9-in-september", "Will ISM Manufacturing PMI be between 55.0 and 55.9 in September?",
           "ISM Manufacturing PMI for September 2026 ... resolve to the bracket containing ... https://www.ismworld.org/")
    low = C("PM-will-ism-manufacturing-pmi-be-between-49pt0-and-49pt9-in-september", "Will ISM Manufacturing PMI be between 49.0 and 49.9 in September?", pm.context)
    serv = C("PM-will-ism-services-pmi-be-between-52pt0-and-52pt9-in-september", "Will ISM Services PMI be between 52.0 and 52.9 in September?", pm.context)
    assert rels(k, pm) == ["b_implies_a"]
    assert rels(k, low) == ["mutually_exclusive"]
    assert rels(k, serv) is None  # manufacturing vs services


def test_treasury_daily_inside_window():
    window = C("KX30YRDIRHM-26SEP30H-T5.45", "Will the 30Y U.S. Treasury yield be above 5.45% by Sep 30, 2026?",
               "If the daily published par yield for the 30Y U.S. Treasury is above 5.45% on any business day between Sep 9, 2026 and Sep 30, 2026 ... Daily Treasury Par Yield Curve Rate")
    low = C("KX30YRDIRLM-26SEP30L-T5.25", "Will the 30Y U.S. Treasury yield be below 5.25% by Sep 30, 2026?",
            "If the daily published par yield for the 30Y U.S. Treasury is below 5.25% on any business day between Sep 9, 2026 and Sep 30, 2026 ... Daily Treasury Par Yield Curve Rate")
    def day(x, d=25, tenor=30):
        return C(f"KXUST{tenor}A-26SEP{d}-T{x}", f"Will the {tenor}Y U.S. Treasury yield be above {x}% on Sep {d}, 2026?",
                 f"If the par yield for the {tenor}Y U.S. Treasury is above {x}% on Sep {d}, 2026, then the market resolves to Yes.")
    assert rels(window, day("5.45")) == ["b_implies_a"]
    assert rels(window, day("5.40")) == []  # the day's bar is lower than the window's
    assert rels(low, day("5.24", d=30)) == ["exhaustive"]
    assert rels(window, day("5.45", tenor=10)) is None or rels(window, day("5.45", tenor=10)) == []
