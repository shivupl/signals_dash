import type { ActiveEntry } from "../api/client";
import { onInternalClick } from "../router";
import { tierOf } from "./format";

/**
 * The index rail: the busiest names, not five hundred rows.
 *
 * No price column. Index names are not on the worker's 15-minute refresh, so a
 * "this week" figure would be mostly blank -- ranking by flag count says
 * something true instead of something half-empty.
 */
export function ActiveRail({ entries }: { entries: ActiveEntry[] }) {
  return (
    <aside className="rail">
      <h2>Most active</h2>
      {entries.length === 0 && <div className="rail-note">No flags in this window.</div>}
      {entries.map((entry) => {
        const ticker = entry.ticker ?? "";
        const tone = { critical: "crit", high: "high", background: "info", quiet: "none" }[
          tierOf(entry.top_score)
        ];
        return (
          <a
            className="rail-row"
            key={entry.company_id}
            href={`/company/${ticker}`}
            onClick={onInternalClick(`/company/${ticker}`)}
            title={entry.name}
          >
            <span className="tk mono">{ticker || "—"}</span>
            <span className="n mono">{entry.flags || ""}</span>
            <span className={`chg mono ${tone}`}>{entry.top_score || ""}</span>
          </a>
        );
      })}
    </aside>
  );
}
