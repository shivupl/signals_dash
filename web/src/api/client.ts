import { toApiParams, type Filters } from "../filters";

export type Tier = "critical" | "high" | "background" | "quiet";

export interface SignalEvent {
  id: number;
  ticker: string | null;
  company: string | null;
  source: string;
  event_type: string;
  category: string | null;
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

export interface ActiveEntry {
  company_id: number;
  ticker: string | null;
  name: string;
  flags: number;
  top_score: number;
  last_event_at: string | null;
}

export interface SystemEvent {
  id: number;
  adapter: string | null;
  state: "open" | "resolved" | "info";
  event_type: string;
  headline: string;
  detail: string | null;
  occurred_at: string;
  minutes: number | null;
}

export interface Option {
  value: string;
  label: string;
}

export interface Meta {
  categories: Option[];
  sources: Option[];
}

export interface Insider {
  cik: string;
  name: string;
  role: string;
  last_codes: string[];
  last_date: string;
  last_url: string | null;
  bought_90d: number;
  sold_90d: number;
  filings: number;
  plan_10b5_1: boolean;
  in_cluster: boolean;
}

export interface CompanyPayload {
  company: {
    ticker: string;
    name: string;
    cik: string | null;
    watched: boolean;
    last_price: number | null;
    week_change: number | null;
    next_earnings: string | null;
    flags_this_month: number;
    in_universes: string[];
  };
  events: SignalEvent[];
  total: number;
  prices: { d: string; close: number }[];
  insiders: Insider[];
  possible_aliases: { raw_name: string; filings: number; last_seen: string }[];
}

export interface FeedPage {
  events: SignalEvent[];
  /** Matches before the row limit. */
  total: number;
  /** Company events scoring above zero. A row scoring 0 is not a flag. */
  flags: number;
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export async function fetchFeed(filters: Filters): Promise<FeedPage> {
  const response = await fetch(`/api/feed?${toApiParams(filters, { limit: "300" })}`);
  if (!response.ok) throw new Error(`feed: HTTP ${response.status}`);
  const events = (await response.json()) as SignalEvent[];
  return {
    events,
    total: Number(response.headers.get("X-Total-Count") ?? events.length),
    flags: Number(response.headers.get("X-Flag-Count") ?? events.length),
  };
}

/** Same threshold as the header, floored at 1, so the rail and header agree. */
export function fetchWatchlist(filters: Filters): Promise<WatchlistEntry[]> {
  const q = new URLSearchParams({
    min_score: String(Math.max(1, filters.minScore)),
    universe: filters.universe,
  });
  if (filters.sources.length) q.set("source", filters.sources.join(","));
  // An excluded category is not a rail filter: the rail counts what is there.
  if (filters.categories.length && !filters.categoriesExclude) {
    q.set("category", filters.categories.join(","));
  }
  return getJson<WatchlistEntry[]>(`/api/watchlist?${q}`);
}

/** The index rail. Ranked by flags, because index names carry no refreshed price. */
export function fetchActive(filters: Filters): Promise<ActiveEntry[]> {
  const q = new URLSearchParams({
    universe: filters.universe,
    min_score: String(Math.max(1, filters.minScore)),
    limit: "12",
  });
  if (filters.range && filters.range !== "custom") q.set("range", filters.range);
  if (filters.sources.length) q.set("source", filters.sources.join(","));
  return getJson<ActiveEntry[]>(`/api/active?${q}`);
}

export const fetchSystem = (): Promise<SystemEvent[]> => getJson<SystemEvent[]>("/api/system");
export const fetchMeta = (): Promise<Meta> => getJson<Meta>("/api/meta");

export function fetchCompany(ticker: string, filters: Filters): Promise<CompanyPayload> {
  const params = toApiParams({ ...filters, tickers: [], system: false });
  return getJson<CompanyPayload>(`/api/company/${encodeURIComponent(ticker)}?${params}`);
}

export interface SettingsPayload {
  flag_threshold: number;
  editable: boolean;
}

export const fetchSettings = (): Promise<SettingsPayload> =>
  getJson<SettingsPayload>("/api/settings");

export const saveSettings = (flag_threshold: number, token: string): Promise<SettingsPayload> =>
  getJson<SettingsPayload>("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Admin-Token": token },
    body: JSON.stringify({ flag_threshold }),
  });
