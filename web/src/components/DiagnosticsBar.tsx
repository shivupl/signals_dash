import { useState } from "react";
import type { Option, SourceLatency, SourceVolume, Stats } from "../api/client";
import { duration, TIERS } from "./format";

/** One source, as the table needs it: the vocabulary joined to the measurements. */
interface SourceLine {
  value: string;
  label: string;
  hint: string | null;
  volume: SourceVolume | null;
  latency: SourceLatency | null;
  /** Producing events the filter bar does not offer as a source -- which is
   *  itself worth seeing, so these are listed rather than dropped. */
  unlisted: boolean;
}

function join(stats: Stats, sources: Option[]): SourceLine[] {
  const volume = new Map(stats.volume.map((v) => [v.source, v]));
  const latency = new Map(stats.latency.map((l) => [l.source, l]));
  const listed = new Set(sources.map((s) => s.value));
  const lines: SourceLine[] = sources.map((s) => ({
    value: s.value,
    label: s.label,
    hint: s.hint ?? null,
    volume: volume.get(s.value) ?? null,
    latency: latency.get(s.value) ?? null,
    unlisted: false,
  }));
  for (const v of stats.volume) {
    if (listed.has(v.source)) continue;
    lines.push({
      value: v.source,
      label: v.source,
      hint: null,
      volume: v,
      latency: latency.get(v.source) ?? null,
      unlisted: true,
    });
  }
  return lines;
}

/** Below this many events, a "percentile" is just one filing wearing a hat.
 *
 *  Production made the case: 13D/G had a single event that arrived through the
 *  reconciliation sweep five hours late, and it alone turned the headline into
 *  "35s–5h 12m" while the three busy sources were all sitting at forty seconds.
 *  Thin sources stay in the table, where the sample column says how thin. */
const MIN_SAMPLE = 5;

/** Sources whose measurement is worth putting in the headline. Falls back to
 *  everything measured rather than to nothing, so a young install still reports. */
function headline(stats: Stats): SourceLatency[] {
  const solid = stats.latency.filter((l) => l.events >= MIN_SAMPLE);
  return solid.length > 0 ? solid : stats.latency;
}

/** The p50 range across those sources. One number for a whole pipeline would be
 *  an average of unlike things; a range is honest and as short. */
function lagRange(stats: Stats): string {
  const p50s = headline(stats)
    .map((l) => l.p50_seconds)
    .filter((v): v is number => v !== null);
  if (p50s.length === 0) return "—";
  const low = Math.min(...p50s);
  const high = Math.max(...p50s);
  return low === high ? duration(low) : `${duration(low)}–${duration(high)}`;
}

function worstP95(stats: Stats): string {
  const p95s = headline(stats)
    .map((l) => l.p95_seconds)
    .filter((v): v is number => v !== null);
  return p95s.length ? duration(Math.max(...p95s)) : "—";
}

function Num({ value, tone }: { value: string; tone?: string }) {
  return <td className={`num${tone ? ` ${tone}` : ""}`}>{value}</td>;
}

function Sources({ lines, windowDays }: { lines: SourceLine[]; windowDays: number }) {
  return (
    <div className="dg-block wide">
      <h4>Sources</h4>
      <div className="dg-scroll">
      <table className="tbl dg-tbl">
        <thead>
          <tr>
            <th>Form</th>
            <th className="num">{windowDays}d</th>
            <th className="num">usual/wk</th>
            <th className="num">lag p50</th>
            <th className="num">p95</th>
            <th className="num">sample</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => {
            // Too few events for the figures beside them to mean much.
            const thin = line.latency !== null && line.latency.events < MIN_SAMPLE;
            return (
            <tr key={line.value} className={line.volume?.quiet ? "quiet-src" : ""}>
              <td title={thin ? "Too few events for the latency figures to mean much" : undefined}>
                <span className="dg-name mono">{line.label}</span>
                {line.volume?.quiet && (
                  <span className="tag high" title="Answering, but producing nothing">
                    quiet
                  </span>
                )}
                {line.unlisted && (
                  <span className="tag" title="Producing events, but not offered as a filter">
                    unlisted
                  </span>
                )}
                {line.hint && <span className="dg-hint">{line.hint}</span>}
              </td>
              <Num
                value={line.volume ? String(line.volume.recent) : "—"}
                tone={line.volume?.quiet ? "warn" : undefined}
              />
              <Num value={line.volume ? line.volume.per_week.toFixed(1) : "—"} tone="soft" />
              <Num value={duration(line.latency?.p50_seconds ?? null)} tone={thin ? "soft" : undefined} />
              <Num value={duration(line.latency?.p95_seconds ?? null)} tone="soft" />
              <Num value={line.latency ? String(line.latency.events) : "—"} tone="soft" />
            </tr>
            );
          })}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function Legend({ categories }: { categories: Option[] }) {
  const explained = categories.filter((c) => c.hint);
  return (
    <div className="dg-block">
      <h4>Event types</h4>
      <dl className="dg-defs">
        {explained.map((c) => (
          <div key={c.value}>
            <dt>{c.label}</dt>
            <dd>{c.hint}</dd>
          </div>
        ))}
      </dl>
      <p className="dg-note">
        The rest say what they are: insider buy, insider sell, officer change, material
        agreement, earnings, halt.
      </p>
    </div>
  );
}

function Scores({ threshold }: { threshold: number }) {
  return (
    <div className="dg-block">
      <h4>Score</h4>
      <dl className="dg-defs">
        {TIERS.map((band) => (
          <div key={band.tier}>
            <dt>
              <span className={`dg-swatch ${band.tier}`} />
              <span className="mono">{band.label}</span>
            </dt>
            <dd>{band.means}</dd>
          </div>
        ))}
      </dl>
      <p className="dg-note">
        The colour is the bar down the left of each row. Flags are events at or above the
        threshold, <span className="mono">{threshold}</span> — the bands are fixed, the
        threshold moves.
      </p>
    </div>
  );
}

/**
 * The machine's own vitals and vocabulary, at the foot of the page.
 *
 * The strip at the top is for things that are wrong. This is for the numbers that
 * are true whether or not anything is wrong -- how fast each source is noticed,
 * whether it is still producing, and what the forms and scores mean. Collapsed it
 * is one line; open it is the answer to "is this thing working, and what am I
 * looking at".
 *
 * There is deliberately no "last polled" here. That lives in the worker's memory
 * and never reaches the API, so the panel reports what the database can prove and
 * says as much rather than implying it watched the poll.
 */
export function DiagnosticsBar({
  stats,
  error,
  sources,
  categories,
}: {
  stats: Stats | null;
  error: string | null;
  sources: Option[];
  categories: Option[];
}) {
  const [open, setOpen] = useState(false);
  const quiet = stats?.volume.filter((v) => v.quiet) ?? [];
  const labels = new Map(sources.map((s) => [s.value, s.label]));
  const producing = (stats?.volume.length ?? 0) - quiet.length;

  return (
    <div className={`dg${open ? " open" : ""}`}>
      <button className="dg-line" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="dg-chev mono">{open ? "−" : "+"}</span>
        <span className="dg-label">Diagnostics</span>
        {error ? (
          <span className="dg-kv err">unavailable · {error}</span>
        ) : !stats ? (
          <span className="dg-kv soft">loading</span>
        ) : (
          <>
            {stats.latency.length === 0 ? (
              // Two dashes side by side read as broken. One field, saying why.
              <span className="dg-kv soft" title={stats.latency_note}>
                <i>lag</i>
                <span>nothing measured yet</span>
              </span>
            ) : (
              <>
                <span
                  className="dg-kv"
                  title="Median seconds from filing accepted to on screen, per source"
                >
                  <i>lag</i>
                  <b className="mono">{lagRange(stats)}</b>
                </span>
                <span className="dg-kv" title="Slowest 95th percentile of any source">
                  <i>p95</i>
                  <b className="mono">{worstP95(stats)}</b>
                </span>
              </>
            )}
            {quiet.length > 0 ? (
              <span className="dg-kv warn" title="Answering every poll, producing nothing">
                <i>quiet</i>
                <b>{quiet.map((v) => labels.get(v.source) ?? v.source).join(", ")}</b>
              </span>
            ) : (
              <span
                className="dg-kv"
                title="Sources with a history to judge by, none of which has gone quiet"
              >
                <i>sources</i>
                <b className="mono">{producing}</b>
                <span className="dg-unit">producing</span>
              </span>
            )}
            <span className="dg-kv">
              <i>threshold</i>
              <b className="mono">{stats.flag_threshold}</b>
            </span>
            {stats.unresolved > 0 && (
              <span className="dg-kv warn" title="Filings whose company could not be identified">
                <i>unresolved</i>
                <b className="mono">{stats.unresolved}</b>
              </span>
            )}
          </>
        )}
        <span className="dg-more mono">{open ? "hide" : "legend, latency"}</span>
      </button>

      {open && stats && (
        <div className="dg-body">
          <Sources lines={join(stats, sources)} windowDays={stats.volume_window_days} />
          <div className="dg-cols">
            <Legend categories={categories} />
            <Scores threshold={stats.flag_threshold} />
          </div>
          <p className="dg-foot">
            {stats.latency_note} The headline lag ignores sources with fewer than{" "}
            {MIN_SAMPLE} measured events, since a percentile over one filing is just that
            filing; the table above shows them anyway, with the sample they rest on.
            {stats.excluded_from_latency > 0 && (
              <> {stats.excluded_from_latency} events in this window are excluded for that reason.</>
            )}{" "}
            Unresolved filings: <span className="mono">{stats.unresolved}</span>. “Quiet” compares
            the last {stats.volume_window_days} days against each source’s own history, which is
            the same test the watchdog alarms on. Last-polled times are not shown: they live in the
            worker and never reach this page.
          </p>
        </div>
      )}
    </div>
  );
}
