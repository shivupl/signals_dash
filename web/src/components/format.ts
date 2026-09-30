// Market time is the default everywhere, because rendering in the viewer's own
// zone turns a 16:05 post-close 8-K into "13:05" on the west coast, which reads
// as mid-session. A viewer who knows that and wants their own clock anyway can
// pick one; see zone.tsx. The default stays ET.
export const MARKET_TZ = "America/New_York";

export function timeOf(iso: string, zone: string = MARKET_TZ): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: zone,
  });
}

export function dayOf(iso: string, zone: string = MARKET_TZ): string {
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: zone,
  });
}

/** The calendar day in market time -- what "today" has to mean here.
 *
 *  Takes no zone on purpose. This one decides which day a filing belongs to and
 *  builds the since/until the API is asked for, and a filing belongs to the
 *  session it was published into whoever happens to be reading. */
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

export type Tier = "critical" | "high" | "background" | "quiet";

/** The bands the feed colours by, in one place so the legend explaining those
 *  colours reads from the same definition that assigns them. */
export const TIERS: { tier: Tier; from: number; label: string; means: string }[] = [
  { tier: "critical", from: 85, label: "85+", means: "Bankruptcy, restatement, delisting, auditor gone" },
  { tier: "high", from: 60, label: "60–84", means: "Officer departure, activist stake, an unusually large sale" },
  { tier: "background", from: 30, label: "30–59", means: "Material agreement, insider buy, most halts" },
  { tier: "quiet", from: 0, label: "under 30", means: "Recorded and searchable, but not flagged" },
];

export function tierOf(score: number): Tier {
  for (const band of TIERS) if (score >= band.from) return band.tier;
  return "quiet";
}

/** Seconds as something you can read at a glance: "8s", "2m 10s", "—". */
export function duration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (seconds < 1) return "<1s";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}
