import type { SignalEvent } from "../api/client";

function timeOf(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function pct(value: number): string {
  // A real minus sign, not a hyphen: it lines up with the plus in a mono column.
  const sign = value >= 0 ? "+" : "−";
  return `${sign}${Math.abs(value).toFixed(1)}%`;
}

function dayOf(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function FeedList({
  events,
  loading,
  error,
  today,
  fresh,
}: {
  events: SignalEvent[];
  loading: boolean;
  error: string | null;
  today: string;
  fresh: Set<number>;
}) {
  if (error) return <div className="state err">{error}</div>;
  if (loading && events.length === 0) return <div className="state">loading…</div>;
  if (events.length === 0) {
    return (
      <div className="state">
        <div className="state-head">Nothing flagged.</div>
        <p className="state-body">
          A quiet feed is the normal state — the target is under twenty flags a day. If a source
          stops polling, that shows up here as a flag of its own. Lower the score filter to see
          routine filings.
        </p>
      </div>
    );
  }

  return (
    <div className="feed">
      {events.map((event) => {
        const isToday = new Date(event.occurred_at).toDateString() === today;
        return (
          <a
            key={event.id}
            className={`row ${event.tier}${fresh.has(event.id) ? " fresh" : ""}`}
            href={event.url ?? undefined}
            target="_blank"
            rel="noreferrer"
            title={Object.entries(event.score_parts)
              .map(([k, v]) => `${k}: ${v > 0 ? "+" : ""}${v}`)
              .join("\n")}
          >
            <span className="bar" />
            <span className="time mono">{timeOf(event.occurred_at)}</span>
            {/* The date only earns its place when the row is not from today. */}
            {!isToday && <span className="date mono">{dayOf(event.occurred_at)}</span>}
            <span className="head">
              <span className="tk mono">
                {event.ticker ?? (event.source === "system" ? "SYS" : "MKT")}
              </span>
              <span className="headline">{event.headline}</span>
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
            <span className="score mono">{event.score}</span>
          </a>
        );
      })}
    </div>
  );
}
