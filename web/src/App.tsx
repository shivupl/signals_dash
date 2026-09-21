import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchMeta,
  fetchSystem,
  fetchWatchlist,
  type Meta,
  type SignalEvent,
  type SystemEvent,
  type WatchlistEntry,
} from "./api/client";
import { useFeed } from "./api/useFeed";
import { CompanyPage } from "./components/CompanyPage";
import { FeedList } from "./components/FeedList";
import { FilterBar } from "./components/FilterBar";
import { marketDay } from "./components/format";
import { SettingsPanel } from "./components/SettingsPanel";
import { StatusStrip } from "./components/StatusStrip";
import { WatchlistRail } from "./components/WatchlistRail";
import { useFilters } from "./filters";
import { companyFromPath, onInternalClick, useLocation } from "./router";

const EMPTY_META: Meta = { categories: [], sources: [] };

export default function App() {
  const { path } = useLocation();
  const ticker = companyFromPath(path);
  const [filters, setFilters, resetFilters] = useFilters();

  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([]);
  const [system, setSystem] = useState<SystemEvent[]>([]);
  const [meta, setMeta] = useState<Meta>(EMPTY_META);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const refreshSystem = useCallback(() => {
    void fetchSystem().then(setSystem).catch(() => undefined);
  }, []);

  // A pushed system event belongs in the strip, not the feed.
  const onPush = useCallback(
    (event: SignalEvent) => {
      if (event.source === "system") refreshSystem();
    },
    [refreshSystem],
  );

  const { events, total, flags, loading, error, live, fresh } = useFeed(filters, onPush);

  useEffect(() => {
    void fetchMeta().then(setMeta).catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshSystem();
    const timer = setInterval(refreshSystem, 60_000);
    return () => clearInterval(timer);
  }, [refreshSystem]);

  // Same threshold as the header, so the two cannot disagree.
  const railKey = `${filters.minScore}|${filters.sources}|${filters.categories}`;
  useEffect(() => {
    void fetchWatchlist(filters).then(setWatchlist).catch(() => setWatchlist([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railKey, events.length]);

  const companies = useMemo(
    () =>
      watchlist
        .filter((w) => w.ticker)
        .map((w) => ({ ticker: w.ticker as string, name: w.name }))
        .sort((a, b) => a.ticker.localeCompare(b.ticker)),
    [watchlist],
  );

  const today = marketDay(new Date());
  // Green only when flags are being pushed. Amber still works, but by polling.
  const dotClass = error ? "err" : live ? "" : "stale";
  const routine = total - flags - (filters.system ? events.filter((e) => e.source === "system").length : 0);

  return (
    <div className="page">
      <div className="shell">
        <header className="shell-head">
          <span className={`dot ${dotClass}`} />
          <a className="brand" href="/" onClick={onInternalClick("/")}
             title={live ? "live: flags are pushed" : "polling every 5s"}>
            Signals
          </a>
          <span className="meta mono">
            {/* Feed-wide counts would be noise on a page about one company. */}
            {!ticker && (
              <>
                <b>
                  {flags} flag{flags === 1 ? "" : "s"}
                </b>
                {routine > 0 && <> · {routine} routine</>} ·{" "}
              </>
            )}
            <b>{watchlist.length}</b> watched · times ET
          </span>
          <span className="spacer" />
          <button className="gear" onClick={() => setSettingsOpen(true)} aria-label="Settings" title="Settings">
            ⚙
          </button>
        </header>

        <StatusStrip events={system} />

        {ticker ? (
          <CompanyPage
            ticker={ticker}
            filters={filters}
            onChange={setFilters}
            onReset={resetFilters}
            sources={meta.sources}
            categories={meta.categories}
          />
        ) : (
          <>
            <FilterBar
              filters={filters}
              onChange={setFilters}
              onReset={resetFilters}
              sources={meta.sources}
              categories={meta.categories}
              companies={companies}
            />
            <div className="split">
              <FeedList
                events={events}
                loading={loading}
                error={error}
                today={today}
                fresh={fresh}
                total={total}
              />
              <WatchlistRail entries={watchlist} />
            </div>
          </>
        )}
      </div>

      <footer>Public data only. Finds signals; makes no decisions.</footer>
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
