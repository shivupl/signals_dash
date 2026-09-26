import { useCallback, useMemo } from "react";
import { navigate, useLocation } from "./router";

export type DateRange = "" | "today" | "7d" | "30d" | "custom";
/** Which monitor: the hand-picked watchlist, or the S&P 500. */
export type Universe = "core" | "sp500";

export interface Filters {
  universe: Universe;
  tickers: string[];
  sources: string[];
  categories: string[];
  /** Categories are excluded rather than included. The index view uses this to
   *  hide earnings 8-Ks without hiding everything else. */
  categoriesExclude: boolean;
  /** Display threshold. Per-viewer and unsaved -- but sent to the server, because
   *  the feed is limited to N rows and filtering after the limit drops matches. */
  minScore: number;
  range: DateRange;
  since: string; // yyyy-mm-dd, only when range === "custom"
  until: string;
  system: boolean;
}

export const DEFAULTS: Filters = {
  universe: "core",
  tickers: [],
  sources: [],
  categories: [],
  categoriesExclude: false,
  minScore: 30,
  range: "",
  since: "",
  until: "",
  system: false,
};

const list = (value: string | null): string[] =>
  value ? value.split(",").map((v) => v.trim()).filter(Boolean) : [];

export function parseFilters(search: string): Filters {
  const q = new URLSearchParams(search);
  const score = Number(q.get("min"));
  const range = (q.get("range") ?? "") as DateRange;
  return {
    universe: q.get("universe") === "sp500" ? "sp500" : "core",
    tickers: list(q.get("ticker")).map((t) => t.toUpperCase()),
    sources: list(q.get("source")),
    categories: list(q.get("category")),
    categoriesExclude: q.get("exclude") === "1",
    minScore: q.has("min") && Number.isFinite(score) ? Math.min(100, Math.max(0, score)) : 30,
    range: ["today", "7d", "30d", "custom"].includes(range) ? range : "",
    since: q.get("since") ?? "",
    until: q.get("until") ?? "",
    system: q.get("system") === "1",
  };
}

/** Only non-default values are written, so the common URL stays clean. */
export function toSearch(f: Filters): string {
  const q = new URLSearchParams();
  if (f.universe !== DEFAULTS.universe) q.set("universe", f.universe);
  if (f.tickers.length) q.set("ticker", f.tickers.join(","));
  if (f.sources.length) q.set("source", f.sources.join(","));
  if (f.categories.length) q.set("category", f.categories.join(","));
  if (f.categoriesExclude) q.set("exclude", "1");
  if (f.minScore !== DEFAULTS.minScore) q.set("min", String(f.minScore));
  if (f.range) q.set("range", f.range);
  if (f.range === "custom") {
    if (f.since) q.set("since", f.since);
    if (f.until) q.set("until", f.until);
  }
  if (f.system) q.set("system", "1");
  const text = q.toString().replace(/%2C/g, ",");
  return text ? `?${text}` : "";
}

/** The same filters as the API understands them. */
export function toApiParams(f: Filters, extra: Record<string, string> = {}): URLSearchParams {
  const q = new URLSearchParams({ min_score: String(f.minScore), ...extra });
  q.set("universe", f.universe);
  if (f.tickers.length) q.set("ticker", f.tickers.join(","));
  if (f.sources.length) q.set("source", f.sources.join(","));
  if (f.categories.length) {
    q.set(f.categoriesExclude ? "exclude_category" : "category", f.categories.join(","));
  }
  if (f.range && f.range !== "custom") q.set("range", f.range);
  if (f.range === "custom") {
    // A calendar day in market time: midnight to midnight, Eastern.
    if (f.since) q.set("since", `${f.since}T00:00:00-04:00`);
    if (f.until) q.set("until", `${f.until}T23:59:59-04:00`);
  }
  if (f.system) q.set("system", "true");
  return q;
}

/** Switching monitors changes what a sensible default view is.
 *
 *  The index files roughly seventy events a day, so threshold 30 would bury the
 *  feed in routine press releases; and every member reports earnings once a
 *  quarter, which for three weeks a quarter is ~25 flags a day of news you
 *  already expected. Both are display state written to the URL -- the server
 *  applies no per-universe defaults, so the URL always describes the screen.
 */
export function universeDefaults(universe: Universe): Partial<Filters> {
  if (universe === "sp500") {
    return { universe, minScore: 50, categories: ["earnings"], categoriesExclude: true };
  }
  return {
    universe,
    minScore: DEFAULTS.minScore,
    categories: [],
    categoriesExclude: false,
  };
}

export function isDefault(f: Filters): boolean {
  return toSearch(f) === "";
}

/** Filter state lives in the URL: shareable, and it survives a refresh. */
export function useFilters(): [Filters, (next: Partial<Filters>) => void, () => void] {
  const { path, search } = useLocation();
  const filters = useMemo(() => parseFilters(search), [search]);
  const update = useCallback(
    (next: Partial<Filters>) =>
      navigate(path + toSearch({ ...parseFilters(window.location.search), ...next }), {
        replace: true,
      }),
    [path],
  );
  const reset = useCallback(() => navigate(path, { replace: true }), [path]);
  return [filters, update, reset];
}
