import { useMemo, useState } from "react";
import type { SignalEvent } from "../api/client";
import { dayOf, marketDay } from "./format";

const W = 1000;
const H = 250;
const PAD = { l: 8, r: 58, t: 16, b: 26 };

const TIER_COLOR: Record<string, string> = {
  critical: "var(--critical)",
  high: "var(--high)",
  background: "var(--info)",
  quiet: "var(--ink-soft)",
};

/**
 * Price with every event pinned to the day it happened. Layoffs, insider selling
 * and a court order sitting next to each other on one axis is a story the feed
 * alone never tells -- though until Phase 2 brings more sources, most companies
 * have few markers to show.
 */
export function Timeline({
  prices,
  events,
}: {
  prices: { d: string; close: number }[];
  events: SignalEvent[];
}) {
  const [hover, setHover] = useState<SignalEvent | null>(null);

  const geo = useMemo(() => {
    if (prices.length < 2) return null;
    const times = prices.map((p) => new Date(`${p.d}T12:00:00Z`).getTime());
    const closes = prices.map((p) => p.close);
    const [t0, t1] = [times[0], times[times.length - 1]];
    const lo = Math.min(...closes);
    const hi = Math.max(...closes);
    const span = hi - lo || 1;
    const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0 || 1)) * (W - PAD.l - PAD.r);
    const y = (c: number) => PAD.t + (1 - (c - lo) / span) * (H - PAD.t - PAD.b);
    const byDay = new Map(prices.map((p) => [p.d, p.close]));

    // The close on an event's market day, or the nearest earlier one.
    const closeOn = (day: string): number => {
      if (byDay.has(day)) return byDay.get(day)!;
      const earlier = prices.filter((p) => p.d <= day);
      return (earlier[earlier.length - 1] ?? prices[0]).close;
    };

    const path = prices.map((p, i) => `${i ? "L" : "M"}${x(times[i]).toFixed(1)},${y(p.close).toFixed(1)}`).join(" ");
    const markers = events
      .map((e) => {
        const day = marketDay(e.occurred_at);
        const t = new Date(`${day}T12:00:00Z`).getTime();
        if (t < t0 || t > t1) return null;
        return { e, cx: x(t), cy: y(closeOn(day)) };
      })
      .filter((m): m is { e: SignalEvent; cx: number; cy: number } => m !== null)
      // Draw quiet ones first so a flag is never hidden under a routine filing.
      .sort((a, b) => a.e.score - b.e.score);

    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => prices[Math.round(f * (prices.length - 1))]);
    return { path, markers, lo, hi, y, x, times, ticks };
  }, [prices, events]);

  if (!geo) {
    return <div className="state">Not enough price history yet to draw a timeline.</div>;
  }

  return (
    <div className="timeline">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Price with event markers">
        {[geo.lo, (geo.lo + geo.hi) / 2, geo.hi].map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(v)} y2={geo.y(v)} className="grid" />
            <text x={W - PAD.r + 8} y={geo.y(v) + 4} className="axis">
              {v.toFixed(2)}
            </text>
          </g>
        ))}
        {geo.ticks.map((p, i) => (
          <text
            key={p.d}
            x={geo.x(new Date(`${p.d}T12:00:00Z`).getTime())}
            y={H - 6}
            className="axis"
            // The end labels anchor inward, or they are clipped by the viewBox.
            textAnchor={i === 0 ? "start" : i === geo.ticks.length - 1 ? "end" : "middle"}
          >
            {dayOf(`${p.d}T16:00:00Z`)}
          </text>
        ))}
        <path d={geo.path} className="price" />
        {geo.markers.map(({ e, cx, cy }) => (
          <a key={e.id} href={e.url ?? undefined} target="_blank" rel="noreferrer">
            <circle
              cx={cx}
              cy={cy}
              r={e.score >= 30 ? 6.5 : 3.5}
              fill={TIER_COLOR[e.tier]}
              className={`marker${hover?.id === e.id ? " on" : ""}`}
              onMouseEnter={() => setHover(e)}
              onMouseLeave={() => setHover(null)}
            >
              <title>{`${e.headline} — ${e.score}`}</title>
            </circle>
          </a>
        ))}
      </svg>
      <div className="timeline-note mono">
        {hover ? (
          <>
            <b style={{ color: TIER_COLOR[hover.tier] }}>{hover.score}</b> {dayOf(hover.occurred_at)} ·{" "}
            {hover.headline} · {hover.detail}
          </>
        ) : (
          `${geo.markers.length} event${geo.markers.length === 1 ? "" : "s"} on the chart · hover for detail, click to open the filing`
        )}
      </div>
    </div>
  );
}
