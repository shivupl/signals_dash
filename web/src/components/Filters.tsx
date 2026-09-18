export interface FilterState {
  minScore: number;
  ticker: string;
  source: string;
}

const SOURCES: { value: string; label: string }[] = [
  { value: "", label: "All sources" },
  { value: "edgar_8k", label: "8-K" },
  { value: "edgar_form4", label: "Form 4" },
  { value: "edgar_13dg", label: "13D/G" },
  { value: "halts", label: "Halts" },
];

const SCORES = [0, 30, 60, 85];

/**
 * Two pills that cycle rather than two dropdowns. There are only four options
 * behind each, and at this size a click beats opening a menu.
 */
export function Filters({
  value,
  onChange,
}: {
  value: FilterState;
  onChange: (next: FilterState) => void;
}) {
  const sourceIndex = Math.max(
    0,
    SOURCES.findIndex((s) => s.value === value.source),
  );
  const scoreIndex = Math.max(0, SCORES.indexOf(value.minScore));

  return (
    <>
      <input
        className="pill pill-input"
        placeholder="ticker"
        value={value.ticker}
        onChange={(e) => onChange({ ...value, ticker: e.target.value.toUpperCase() })}
        aria-label="Filter by ticker"
      />
      <button
        className={`pill${value.source ? " on" : ""}`}
        onClick={() =>
          onChange({ ...value, source: SOURCES[(sourceIndex + 1) % SOURCES.length].value })
        }
      >
        {SOURCES[sourceIndex].label}
      </button>
      <button
        className={`pill${value.minScore > 0 ? " on" : ""}`}
        onClick={() => onChange({ ...value, minScore: SCORES[(scoreIndex + 1) % SCORES.length] })}
      >
        {value.minScore === 0 ? "Any score" : `Score ${value.minScore}+`}
      </button>
    </>
  );
}
