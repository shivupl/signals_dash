import { useEffect, useRef, useState } from "react";
import { fetchCompany, type CompanyPayload, type Option } from "../api/client";
import { toSearch, type Filters } from "../filters";
import { onInternalClick } from "../router";
import { FilterBar } from "./FilterBar";
import { EventRow } from "./FeedList";
import { dayOf, marketDay, money, pct } from "./format";
import { Timeline } from "./Timeline";

const CODE_MEANING: Record<string, string> = {
  P: "open-market purchase",
  S: "sale",
  A: "grant or award",
  M: "option exercise",
  F: "shares withheld for tax",
  G: "gift",
  C: "conversion",
};

export function CompanyPage({
  ticker,
  filters,
  onChange,
  onReset,
  sources,
  categories,
}: {
  ticker: string;
  filters: Filters;
  onChange: (next: Partial<Filters>) => void;
  onReset: () => void;
  sources: Option[];
  categories: Option[];
}) {
  const [data, setData] = useState<CompanyPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);
  const key = toSearch(filters);

  useEffect(() => {
    const sequence = ++request.current;
    fetchCompany(ticker, filters)
      .then((payload) => {
        if (sequence !== request.current) return;
        setData(payload);
        setError(null);
      })
      .catch((e) => sequence === request.current && setError(String(e.message ?? e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker, key]);

  if (error) {
    return (
      <div className="state err">
        {error} ·{" "}
        <a href="/" onClick={onInternalClick("/")}>
          back to the feed
        </a>
      </div>
    );
  }
  if (!data) return <div className="state">loading…</div>;

  const { company } = data;
  const today = marketDay(new Date());
  const buyers = data.insiders.filter((i) => i.bought_90d > 0).length;

  return (
    <div className="company">
      <div className="co-head">
        <a className="back mono" href="/" onClick={onInternalClick("/")}>
          ← feed
        </a>
        <span className="co-ticker mono">{company.ticker}</span>
        <span className="co-name">{company.name}</span>
        <span className="spacer" />
        <div className="co-stats mono">
          <span>
            <i>price</i>
            {company.last_price !== null ? company.last_price.toFixed(2) : "—"}
          </span>
          <span>
            <i>7d</i>
            {company.week_change !== null ? (
              <b className={company.week_change >= 0 ? "up" : "down"}>{pct(company.week_change)}</b>
            ) : (
              "—"
            )}
          </span>
          <span>
            <i>earnings</i>
            {company.next_earnings ? dayOf(`${company.next_earnings}T16:00:00Z`) : "—"}
          </span>
          <span>
            <i>flags · 30d</i>
            {company.flags_this_month}
          </span>
        </div>
      </div>

      <FilterBar
        filters={filters}
        onChange={onChange}
        onReset={onReset}
        sources={sources}
        categories={categories}
        companies={[]}
        showTickers={false}
        showSystem={false}
      />

      <Timeline prices={data.prices} events={data.events} />

      <section>
        <h3>
          Events <span className="count mono">{data.total}</span>
        </h3>
        {data.events.length === 0 ? (
          <div className="state">Nothing at this threshold. Lower the score to see routine filings.</div>
        ) : (
          <div className="feed flat">
            {data.events.map((e) => (
              <EventRow key={e.id} event={e} today={today} linkTicker={false} />
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>
          Insiders <span className="count mono">last 12 months · dollars are the last 90 days</span>
        </h3>
        {buyers >= 3 && (
          <p className="callout">
            <b>{buyers} insiders</b> bought on the open market in the last 90 days. Three distinct
            buyers inside 30 days is the cluster signal, worth +25 on each of their filings.
          </p>
        )}
        {data.insiders.length === 0 ? (
          <div className="state">No Form 4 filings on record for this company yet.</div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Insider</th>
                  <th>Role</th>
                  <th>Last</th>
                  <th>Date</th>
                  <th className="num">Bought</th>
                  <th className="num">Sold</th>
                </tr>
              </thead>
              <tbody>
                {data.insiders.map((i) => (
                  <tr key={i.cik}>
                    <td>
                      {i.last_url ? (
                        <a href={i.last_url} target="_blank" rel="noreferrer">
                          {i.name}
                        </a>
                      ) : (
                        i.name
                      )}
                      {i.in_cluster && <span className="tag high">cluster</span>}
                      {i.plan_10b5_1 && <span className="tag">10b5-1</span>}
                    </td>
                    <td className="soft">{i.role}</td>
                    <td
                      className="mono"
                      title={i.last_codes.map((c) => `${c}: ${CODE_MEANING[c] ?? "other"}`).join("\n")}
                    >
                      {i.last_codes.join(" ") || "—"}
                    </td>
                    <td className="mono soft">{dayOf(i.last_date)}</td>
                    <td className={`num mono${i.bought_90d ? " up" : " soft"}`}>{money(i.bought_90d)}</td>
                    <td className={`num mono${i.sold_90d ? " down" : " soft"}`}>{money(i.sold_90d)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {data.possible_aliases.length > 0 && (
        <section>
          <h3>
            Possibly related, unresolved <span className="count mono">{data.possible_aliases.length}</span>
          </h3>
          <p className="help">
            Filings that matched no company but whose filer name starts like this one — usually a
            subsidiary or financing vehicle. Matched on the name alone, so treat as a lead.
          </p>
          <div className="tbl-wrap">
            <table className="tbl">
              <tbody>
                {data.possible_aliases.map((a) => (
                  <tr key={a.raw_name}>
                    <td>{a.raw_name}</td>
                    <td className="num mono soft">
                      {a.filings} filing{a.filings === 1 ? "" : "s"}
                    </td>
                    <td className="num mono soft">{dayOf(a.last_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
