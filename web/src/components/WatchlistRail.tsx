import type { WatchlistEntry } from "../api/client";
import { pct } from "./FeedList";

/** Earnings inside two weeks change how every other flag on that name reads. */
function earningsSoon(iso: string | null): boolean {
  if (!iso) return false;
  const days = (new Date(iso).getTime() - Date.now()) / 86_400_000;
  return days >= -1 && days <= 14;
}

function tierClass(score: number): string {
  if (score >= 85) return "crit";
  if (score >= 60) return "high";
  if (score >= 30) return "info";
  return "none";
}

/**
 * Watched companies ranked by how much happened this week. A heat map of where
 * to look, not a portfolio view.
 *
 * The right-hand column is the week's price change. When no price is known yet it
 * falls back to the top score rather than inventing a percentage -- it is the one
 * number on screen you might act on.
 */
export function WatchlistRail({
  entries,
  selected,
  onSelect,
}: {
  entries: WatchlistEntry[];
  selected: string;
  onSelect: (ticker: string) => void;
}) {
  const active = entries.filter((e) => e.flags > 0);
  // On a quiet morning the flagged list is empty, which reads as "broken". The
  // week's largest moves among the unflagged names are context worth having --
  // a big move with no filing behind it is its own kind of question.
  const movers = entries
    .filter((e) => e.flags === 0 && e.week_change !== null)
    .sort((a, b) => Math.abs(b.week_change ?? 0) - Math.abs(a.week_change ?? 0))
    .slice(0, Math.max(0, 8 - active.length));
  const quiet = entries.length - active.length - movers.length;

  return (
    <aside className="rail">
      <h2>This week</h2>

      {active.map((entry) => {
        const ticker = entry.ticker ?? "";
        return (
          <button
            key={entry.company_id}
            className={`rail-row${selected === ticker ? " on" : ""}`}
            onClick={() => onSelect(selected === ticker ? "" : ticker)}
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
            <span className="n mono">{entry.flags}</span>
            {entry.week_change !== null ? (
              <span className={`chg mono ${entry.week_change >= 0 ? "up" : "down"}`}>
                {pct(entry.week_change)}
              </span>
            ) : (
              <span className={`chg mono ${tierClass(entry.top_score)}`}>{entry.top_score}</span>
            )}
          </button>
        );
      })}

      {active.length === 0 && <div className="rail-note">No flags this week.</div>}

      {movers.length > 0 && <h2 className="sub">Moving, no flags</h2>}
      {movers.map((entry) => {
        const ticker = entry.ticker ?? "";
        const change = entry.week_change ?? 0;
        return (
          <button
            key={entry.company_id}
            className={`rail-row dim${selected === ticker ? " on" : ""}`}
            onClick={() => onSelect(selected === ticker ? "" : ticker)}
            title={entry.name}
          >
            <span className="tk mono">
              {ticker}
              {earningsSoon(entry.next_earnings) && (
                <span className="earn" title={`earnings ${entry.next_earnings}`}>
                  E
                </span>
              )}
            </span>
            <span className="n mono" />
            <span className={`chg mono ${change >= 0 ? "up" : "down"}`}>{pct(change)}</span>
          </button>
        );
      })}

      {quiet > 0 && <div className="rail-note">{quiet} others quiet</div>}
    </aside>
  );
}
