// Market time, always. Rendering in the viewer's own zone turns a 16:05 post-close
// 8-K into "13:05" on the west coast, which reads as mid-session and is wrong in
// the way that matters.
export const MARKET_TZ = "America/New_York";

export function timeOf(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: MARKET_TZ,
  });
}

export function dayOf(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: MARKET_TZ,
  });
}

/** The calendar day in market time -- what "today" has to mean here. */
export function marketDay(value: Date | string): string {
  return new Date(value).toLocaleDateString("en-CA", { timeZone: MARKET_TZ });
}

export function pct(value: number): string {
  // A real minus sign, not a hyphen: it lines up with the plus in a mono column.
  const sign = value >= 0 ? "+" : "−";
  return `${sign}${Math.abs(value).toFixed(1)}%`;
}

export function money(value: number): string {
  if (!value) return "—";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `$${Math.round(value / 1_000)}K`;
  return `$${Math.round(value)}`;
}

export function tierOf(score: number): "critical" | "high" | "background" | "quiet" {
  if (score >= 85) return "critical";
  if (score >= 60) return "high";
  if (score >= 30) return "background";
  return "quiet";
}
