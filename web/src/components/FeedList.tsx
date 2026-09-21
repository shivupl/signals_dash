import type { SignalEvent } from "../api/client";
import { onInternalClick } from "../router";
import { dayOf, marketDay, pct, timeOf } from "./format";

export function EventRow({
  event,
  today,
  fresh,
  linkTicker = true,
}: {
  event: SignalEvent;
  today: string;
  fresh?: boolean;
  linkTicker?: boolean;
}) {
  const isToday = marketDay(event.occurred_at) === today;
  const label = event.ticker ?? (event.source === "system" ? "SYS" : "MKT");
  const breakdown = Object.entries(event.score_parts)
    .map(([k, v]) => `${k}: ${v > 0 ? "+" : ""}${v}`)
    .join("\n");

  return (
    <div className={`row ${event.tier}${fresh ? " fresh" : ""}`}>
      <span className="bar-accent" />
      <span className="time mono">{timeOf(event.occurred_at)}</span>
      {/* The date only earns its place when the row is not from today. */}
      {!isToday && <span className="date mono">{dayOf(event.occurred_at)}</span>}
      <span className="head">
        {event.ticker && linkTicker ? (
          <a
            className="tk mono"
            href={`/company/${event.ticker}`}
            onClick={onInternalClick(`/company/${event.ticker}`)}
            title={event.company ?? undefined}
          >
            {label}
          </a>
        ) : (
          <span className="tk mono">{label}</span>
        )}
        {event.url ? (
          <a className="headline" href={event.url} target="_blank" rel="noreferrer">
            {event.headline}
          </a>
        ) : (
          <span className="headline">{event.headline}</span>
        )}
      </span>
      <span className="detail">
        {event.detail}
        {event.change_since !== null && (
          <span className={event.change_since >= 0 ? "up" : "down"}>
            {" · "}
            {pct(event.change_since)} since
          </span>
        )}
      </span>
      <span className="score mono" title={breakdown}>
        {event.score}
      </span>
    </div>
  );
}

export function FeedList({
  events,
  loading,
  error,
  today,
  fresh,
  total,
}: {
  events: SignalEvent[];
  loading: boolean;
  error: string | null;
  today: string;
  fresh: Set<number>;
  total: number;
}) {
  if (error) return <div className="state err">{error}</div>;
  if (loading && events.length === 0) return <div className="state">loading…</div>;
  if (events.length === 0) {
    return (
      <div className="state">
        <div className="state-head">Nothing matches.</div>
        <p className="state-body">
          A quiet feed is the normal state — the target is under twenty flags a day. If a source
          stops polling, that shows up in the status strip above. Lower the score or widen the
          filters to see routine filings.
        </p>
      </div>
    );
  }
  return (
    <div className="feed">
      {events.map((event) => (
        <EventRow key={event.id} event={event} today={today} fresh={fresh.has(event.id)} />
      ))}
      {total > events.length && (
        <div className="more mono">
          showing {events.length} of {total} — narrow the filters to see the rest
        </div>
      )}
    </div>
  );
}
