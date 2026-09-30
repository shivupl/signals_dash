import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchActive,
  fetchMeta,
  fetchStats,
  fetchSystem,
  fetchWatchlist,
  type ActiveEntry,
  type Meta,
  type SignalEvent,
  type Stats,
  type SystemEvent,
  type WatchlistEntry,
} from "./api/client";
import { useFeed } from "./api/useFeed";
import { ActiveRail } from "./components/ActiveRail";
import { CompanyPage } from "./components/CompanyPage";
import { DiagnosticsBar } from "./components/DiagnosticsBar";
import { FeedList } from "./components/FeedList";
import { FilterBar } from "./components/FilterBar";
import { marketDay } from "./components/format";
import { SettingsPanel } from "./components/SettingsPanel";
import { StatusStrip } from "./components/StatusStrip";
import { TimeZonePicker } from "./components/TimeZonePicker";
import { UniverseSwitch } from "./components/UniverseSwitch";
import { WatchlistRail } from "./components/WatchlistRail";
import { DEFAULTS, universeDefaults, useFilters } from "./filters";
import { companyFromPath, onInternalClick, useLocation } from "./router";

const EMPTY_META: Meta = { categories: [], sources: [] };

export default function App() {
  const { path } = useLocation();
  const ticker = companyFromPath(path);
  const [filters, setFilters, resetFilters] = useFilters();

  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([]);
  const [active, setActive] = useState<ActiveEntry[]>([]);
  const [system, setSystem] = useState<SystemEvent[]>([]);
  const [meta, setMeta] = useState<Meta>(EMPTY_META);
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
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

  // One request a minute, for both the diagnostics readout and the threshold the
  // Flags switch snaps to. Moves about as slowly as the status strip does.
  useEffect(() => {
    const load = () =>
      void fetchStats()
        .then((next) => {
          setStats(next);
          setStatsError(null);
        })
        .catch((e: unknown) => setStatsError(e instanceof Error ? e.message : String(e)));
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    refreshSystem();
    const timer = setInterval(refreshSystem, 60_000);
    return () => clearInterval(timer);
  }, [refreshSystem]);

  // Same threshold as the header, so the two cannot disagree.
  const railKey = `${filters.universe}|${filters.minScore}|${filters.sources}|${filters.categories}`;
  useEffect(() => {
    void fetchWatchlist(filters).then(setWatchlist).catch(() => setWatchlist([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railKey, events.length]);

  // The index rail ranks by flags, so it also moves when the date range does.
  useEffect(() => {
    if (filters.universe !== "sp500") return;
    void fetchActive(filters).then(setActive).catch(() => setActive([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railKey, filters.range, filters.since, filters.until, events.length]);

  const companies = useMemo(
    () =>
      watchlist
        .filter((w) => w.ticker)
        .map((w) => ({ ticker: w.ticker as string, name: w.name }))
        .sort((a, b) => a.ticker.localeCompare(b.ticker)),
    [watchlist],
  );

  const today = marketDay(new Date());
  // Until /stats answers, the default is the best available guess -- and it is the
  // value the server ships with, so the switch is usually right even then.
  const threshold = stats?.flag_threshold ?? DEFAULTS.minScore;
  // Narrowed means narrowed *past the default*. The default window and the rail
  // both cover a week, so they agree and the count needs no qualifier; a ticker
  // filter or a different range is what makes it the view's own number.
  const narrowed = filters.tickers.length > 0 || filters.range !== DEFAULTS.range;
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
          <UniverseSwitch
            value={filters.universe}
            watched={watchlist.length}
            onChange={(universe) => setFilters(universeDefaults(universe))}
          />
          <span className="meta mono">
            {/* Feed-wide counts would be noise on a page about one company. */}
            {!ticker && (
              <>
                <b>
                  {flags} flag{flags === 1 ? "" : "s"}
                  {/* The rail always covers the whole watchlist this week. When the
                      feed is narrowed by ticker or date, say the count is the view's. */}
                  {narrowed ? " in view" : ""}
                </b>
                {routine > 0 && <> · {routine} routine</>} ·{" "}
              </>
            )}
            {/* The rail's own row count, never a hard-coded 500: membership is
                whatever the snapshot says it is. */}
            <b>{watchlist.length}</b>{" "}
            {filters.universe === "sp500" ? "in S&P 500" : "watched"} ·{" "}
            <TimeZonePicker />
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
            threshold={threshold}
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
              threshold={threshold}
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
              {filters.universe === "sp500" ? (
                <ActiveRail entries={active} />
              ) : (
                <WatchlistRail entries={watchlist} />
              )}
            </div>
          </>
        )}

        <DiagnosticsBar
          stats={stats}
          error={statsError}
          sources={meta.sources}
          categories={meta.categories}
        />
      </div>

      <footer>Public data only. Finds signals; makes no decisions.</footer>
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
