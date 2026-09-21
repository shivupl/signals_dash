import { useEffect, useMemo, useRef, useState } from "react";
import type { Option } from "../api/client";
import { DEFAULTS, isDefault, type DateRange, type Filters } from "../filters";

/** Close a popover on outside click or Escape. */
function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);
  return ref;
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: Option[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);

  const text =
    selected.length === 0
      ? label
      : selected.length === 1
        ? (options.find((o) => o.value === selected[0])?.label ?? selected[0])
        : `${label} · ${selected.length}`;

  return (
    <div className="ms" ref={ref}>
      <button className={`pill${selected.length ? " on" : ""}`} onClick={() => setOpen(!open)}>
        {text}
      </button>
      {open && (
        <div className="pop" role="listbox" aria-multiselectable>
          {options.map((o) => (
            <label className="pop-row" key={o.value}>
              <input
                type="checkbox"
                checked={selected.includes(o.value)}
                onChange={() => toggle(o.value)}
              />
              {o.label}
            </label>
          ))}
          {selected.length > 0 && (
            <button className="pop-clear" onClick={() => onChange([])}>
              clear
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function TickerPicker({
  companies,
  selected,
  onChange,
}: {
  companies: { ticker: string; name: string }[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const input = useRef<HTMLInputElement>(null);

  const matches = useMemo(() => {
    const q = text.trim().toUpperCase();
    const pool = companies.filter((c) => !selected.includes(c.ticker));
    if (!q) return pool.slice(0, 8);
    // Someone typing "M" wants MARA, META, MSFT -- not AMD because its name has an
    // M in it. Ticker prefixes first, then tickers containing it, then names.
    const rank = (c: { ticker: string; name: string }) =>
      c.ticker.startsWith(q) ? 0 : c.ticker.includes(q) ? 1 : c.name.toUpperCase().includes(q) ? 2 : 3;
    return pool
      .filter((c) => rank(c) < 3)
      .sort((a, b) => rank(a) - rank(b) || a.ticker.localeCompare(b.ticker))
      .slice(0, 8);
  }, [companies, selected, text]);

  const add = (ticker: string) => {
    onChange([...selected, ticker]);
    setText("");
    // Close until they type again: left open it sits on top of the very rows the
    // pick was meant to reveal.
    setOpen(false);
  };

  return (
    <div className="ms tickers" ref={ref}>
      {/* The whole pill is the click target, not just the narrow input inside it. */}
      <div
        className={`pill chips${selected.length ? " on" : ""}`}
        onClick={() => input.current?.focus()}
      >
        {selected.map((t) => (
          <span className="chip mono" key={t}>
            {t}
            <button
              aria-label={`remove ${t}`}
              onClick={() => onChange(selected.filter((s) => s !== t))}
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={input}
          className="mono"
          placeholder={selected.length ? "" : "ticker"}
          value={text}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && matches[0]) add(matches[0].ticker);
            if (e.key === "Backspace" && !text && selected.length)
              onChange(selected.slice(0, -1));
          }}
          aria-label="Filter by ticker"
        />
      </div>
      {open && matches.length > 0 && (
        <div className="pop">
          {matches.map((c) => (
            <button className="pop-row pick" key={c.ticker} onClick={() => add(c.ticker)}>
              <span className="mono tk">{c.ticker}</span>
              <span className="nm">{c.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const RANGES: { value: DateRange; label: string }[] = [
  { value: "", label: "All" },
  { value: "today", label: "Today" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
  { value: "custom", label: "Custom" },
];

export function FilterBar({
  filters,
  onChange,
  onReset,
  sources,
  categories,
  companies,
  showTickers = true,
  showSystem = true,
}: {
  filters: Filters;
  onChange: (next: Partial<Filters>) => void;
  onReset: () => void;
  sources: Option[];
  categories: Option[];
  companies: { ticker: string; name: string }[];
  showTickers?: boolean;
  showSystem?: boolean;
}) {
  // The slider moves locally and commits on release, so dragging is one request.
  const [score, setScore] = useState(filters.minScore);
  useEffect(() => setScore(filters.minScore), [filters.minScore]);

  return (
    <div className="bar">
      {showTickers && (
        <TickerPicker
          companies={companies}
          selected={filters.tickers}
          onChange={(tickers) => onChange({ tickers })}
        />
      )}
      <MultiSelect
        label="Source"
        options={showSystem ? sources : sources.filter((s) => s.value !== "system")}
        selected={filters.sources}
        onChange={(next) => onChange({ sources: next })}
      />
      <MultiSelect
        label="Event type"
        options={categories.filter((c) => showSystem || c.value !== "system")}
        selected={filters.categories}
        onChange={(next) => onChange({ categories: next })}
      />

      <label className="slider" title="Display threshold — yours alone, not saved">
        <span className="mono">score ≥ {score}</span>
        <input
          type="range"
          min={0}
          max={100}
          step={5}
          value={score}
          onChange={(e) => setScore(Number(e.target.value))}
          onMouseUp={() => onChange({ minScore: score })}
          onTouchEnd={() => onChange({ minScore: score })}
          onKeyUp={() => onChange({ minScore: score })}
        />
      </label>

      <div className="seg" role="group" aria-label="Date range">
        {RANGES.map((r) => (
          <button
            key={r.value}
            className={filters.range === r.value ? "on" : ""}
            onClick={() => onChange({ range: r.value })}
          >
            {r.label}
          </button>
        ))}
      </div>
      {filters.range === "custom" && (
        <span className="dates">
          <input
            type="date"
            value={filters.since}
            onChange={(e) => onChange({ since: e.target.value })}
            aria-label="from"
          />
          <span>→</span>
          <input
            type="date"
            value={filters.until}
            onChange={(e) => onChange({ until: e.target.value })}
            aria-label="to"
          />
        </span>
      )}

      {showSystem && (
        <label className="check">
          <input
            type="checkbox"
            checked={filters.system}
            onChange={(e) => onChange({ system: e.target.checked })}
          />
          system events
        </label>
      )}

      {!isDefault(filters) && (
        <button className="reset" onClick={onReset} title={`back to score ≥ ${DEFAULTS.minScore}`}>
          reset
        </button>
      )}
    </div>
  );
}
