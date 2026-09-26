import type { Universe } from "../filters";

const OPTIONS: { value: Universe; label: string; hint: string }[] = [
  { value: "core", label: "My 40", hint: "Your hand-picked watchlist, flags at 30+" },
  { value: "sp500", label: "S&P 500", hint: "Every index member, flags at 50+, earnings hidden" },
];

/** Which monitor you are looking through. Lives in the URL, like every filter. */
export function UniverseSwitch({
  value,
  onChange,
}: {
  value: Universe;
  onChange: (next: Universe) => void;
}) {
  return (
    <div className="seg universe" role="group" aria-label="Monitor">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? "on" : ""}
          title={option.hint}
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
