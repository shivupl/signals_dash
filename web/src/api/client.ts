export type Tier = "critical" | "high" | "background" | "quiet";

export interface SignalEvent {
  id: number;
  ticker: string | null;
  company: string | null;
  source: string;
  event_type: string;
  occurred_at: string;
  ingested_at: string;
  summary: string | null;
  headline: string;
  detail: string;
  score: number;
  tier: Tier;
  url: string | null;
  price_at: number | null;
  change_since: number | null;
  score_parts: Record<string, number>;
}

export interface WatchlistEntry {
  company_id: number;
  ticker: string | null;
  name: string;
  flags: number;
  top_score: number;
  last_event_at: string | null;
  last_price: number | null;
  week_change: number | null;
  next_earnings: string | null;
}

export interface Stats {
  latency: { source: string; events: number; p50_seconds: number | null; p95_seconds: number | null }[];
  unresolved: number;
  flag_threshold: number;
}

export interface FeedQuery {
  minScore: number;
  ticker?: string;
  source?: string;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

export function fetchFeed(query: FeedQuery): Promise<SignalEvent[]> {
  const params = new URLSearchParams({ min_score: String(query.minScore), limit: "200" });
  if (query.ticker) params.set("ticker", query.ticker);
  if (query.source) params.set("source", query.source);
  return getJson<SignalEvent[]>(`/api/feed?${params}`);
}

export const fetchWatchlist = (): Promise<WatchlistEntry[]> =>
  getJson<WatchlistEntry[]>("/api/watchlist?min_score=30");

export const fetchStats = (): Promise<Stats> => getJson<Stats>("/api/stats");
