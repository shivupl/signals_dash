import { useEffect, useMemo, useState } from "react";
import { fetchWatchlist, type WatchlistEntry } from "./api/client";
import { useDebounced, useFeed } from "./api/useFeed";
import { FeedList, marketDay } from "./components/FeedList";
import { Filters, type FilterState } from "./components/Filters";
import { WatchlistRail } from "./components/WatchlistRail";

export default function App() {
  const [filters, setFilters] = useState<FilterState>({
    minScore: 30,
    ticker: "",
    source: "",
  });
  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([]);

  // Debounced so a four-letter ticker is one request, not four.
  const ticker = useDebounced(filters.ticker);

  const query = useMemo(
    () => ({
      minScore: filters.minScore,
      ticker: ticker || undefined,
      source: filters.source || undefined,
    }),
    [filters.minScore, ticker, filters.source],
  );

  const { events, loading, error, live, fresh } = useFeed(query);

  useEffect(() => {
    void fetchWatchlist().then(setWatchlist).catch(() => setWatchlist([]));
  }, [events.length]);

  const today = marketDay(new Date());
  const todayCount = events.filter(
    (e) => marketDay(e.occurred_at) === today,
  ).length;

  // Green only when flags are being pushed. Amber means it still works, but by
  // polling -- worth knowing when you are waiting on something.
  const dotClass = error ? "err" : live ? "" : "stale";

  // "today" would be a lie on a feed of replayed filings, so it is only claimed
  // when the rows really are from today.
  // Below the threshold these are events on the record, not flags; calling a
  // routine option grant a "flag" would cheapen the word.
  const word = filters.minScore >= 30 ? "flag" : "event";
  const noun = events.length === 1 ? word : `${word}s`;
  const countLabel =
    todayCount > 0 && todayCount === events.length
      ? `${events.length} ${noun} today`
      : `${events.length} ${noun}`;

  return (
    <div className="page">
      <div className="shell">
        <header className="shell-head">
          <span className={`dot ${dotClass}`} />
          <span className="brand" title={live ? "live: flags are pushed" : "polling every 5s"}>
            Signals
          </span>
          <span className="meta mono">
            <b>{countLabel}</b> · <b>{watchlist.length}</b> watched · times ET
          </span>
          <span className="spacer" />
          <Filters value={filters} onChange={setFilters} />
        </header>

        <div className="split">
          <FeedList events={events} loading={loading} error={error} today={today} fresh={fresh} />
          <WatchlistRail
            entries={watchlist}
            selected={filters.ticker}
            onSelect={(t) => setFilters({ ...filters, ticker: t })}
          />
        </div>
      </div>

      <footer>Public data only. Finds signals; makes no decisions.</footer>
    </div>
  );
}
