import type { WatchlistEntry } from "../api/client";
import { onInternalClick } from "../router";
import { pct, tierOf } from "./format";

/** Earnings inside two weeks change how every other flag on that name reads. */
function earningsSoon(iso: string | null): boolean {
  if (!iso) return false;
  const days = (new Date(iso).getTime() - Date.now()) / 86_400_000;
  return days >= -1 && days <= 14;
}

function Row({ entry, dim }: { entry: WatchlistEntry; dim?: boolean }) {
  const ticker = entry.ticker ?? "";
  const tone = { critical: "crit", high: "high", background: "info", quiet: "none" }[
    tierOf(entry.top_score)
  ];
  return (
    <a
      className={`rail-row${dim ? " dim" : ""}`}
      href={`/company/${ticker}`}
      onClick={onInternalClick(`/company/${ticker}`)}
      title={entry.name}
    >
      <span className="tk mono">
        {ticker || "—"}
        {earningsSoon(entry.next_earnings) && (
          <span className="earn" title={`earnings ${entry.next_earnings}`}>
            E
          </span>
        )}
      </span>
      <span className="n mono">{entry.flags || ""}</span>
      {entry.week_change !== null ? (
        <span className={`chg mono ${entry.week_change >= 0 ? "up" : "down"}`}>
          {pct(entry.week_change)}
        </span>
      ) : (
        <span className={`chg mono ${tone}`}>{entry.top_score || ""}</span>
      )}
    </a>
  );
}

/**
 * Watched companies ranked by how much happened this week. Counts use the same
 * score threshold as the header, so the two cannot disagree.
 */
export function WatchlistRail({ entries }: { entries: WatchlistEntry[] }) {
  const active = entries.filter((e) => e.flags > 0);
  // On a quiet morning the flagged list is empty, which reads as "broken". A big
  // move with no filing behind it is its own kind of question.
  const movers = entries
    .filter((e) => e.flags === 0 && e.week_change !== null)
    .sort((a, b) => Math.abs(b.week_change ?? 0) - Math.abs(a.week_change ?? 0))
    .slice(0, Math.max(0, 8 - active.length));
  const quiet = entries.length - active.length - movers.length;

  return (
    <aside className="rail">
      <h2>This week</h2>
      {active.map((e) => (
        <Row entry={e} key={e.company_id} />
      ))}
      {active.length === 0 && <div className="rail-note">No flags this week.</div>}
      {movers.length > 0 && <h2 className="sub">Moving, no flags</h2>}
      {movers.map((e) => (
        <Row entry={e} key={e.company_id} dim />
      ))}
      {quiet > 0 && <div className="rail-note">{quiet} others quiet</div>}
    </aside>
  );
}
