import { useState } from "react";
import type { SystemEvent } from "../api/client";
import { MARKET_TZ } from "./format";

function when(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: MARKET_TZ,
  });
}

/**
 * The pipeline talking about itself. It used to do that in the feed, where nine
 * alarms sat among two real flags; now it has one slim line of its own, and the
 * detail is a click away.
 */
export function StatusStrip({ events }: { events: SystemEvent[] }) {
  const [open, setOpen] = useState(false);
  if (events.length === 0) return null;

  const degraded = events.filter((e) => e.state === "open");
  const tone = degraded.length > 0 ? "bad" : "ok";
  const summary = events.slice(0, 4).map((e) => e.headline).join("  ·  ");

  return (
    <div className={`strip ${tone}`}>
      <button className="strip-line" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="strip-dot" />
        <span className="strip-text">{summary}</span>
        <span className="strip-more mono">
          {events.length > 4 ? `+${events.length - 4}  ` : ""}
          {open ? "hide" : "details"}
        </span>
      </button>
      {open && (
        <div className="strip-detail">
          {events.map((e) => (
            <div className={`strip-row ${e.state}`} key={e.id}>
              <span className="mono t">{when(e.occurred_at)}</span>
              <span className="h">{e.headline}</span>
              <span className="d">{e.detail?.replace(/^system · /, "")}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
