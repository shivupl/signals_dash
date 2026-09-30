import type { Universe } from "../filters";

/** Which monitor you are looking through. Lives in the URL, like every filter.
 *
 *  The first tab used to read "My 40", which was true of a 40-name watchlist and
 *  becomes a lie the day the list grows -- and it is meant to grow, towards the
 *  small companies nobody else is watching. The count belongs in the header,
 *  which reads it from the rail, so the tab just names the thing.
 */
export function UniverseSwitch({
  value,
  onChange,
  watched,
}: {
  value: Universe;
  onChange: (next: Universe) => void;
  /** Rows in the watchlist right now, for the tooltip. Zero before it loads. */
  watched: number;
}) {
  const options: { value: Universe; label: string; hint: string }[] = [
    {
      value: "core",
      label: "Watchlist",
      hint: watched
        ? `Your hand-picked list: ${watched} names, flags at 30+`
        : "Your hand-picked list, flags at 30+",
    },
    {
      value: "sp500",
      label: "S&P 500",
      hint: "Every index member, flags at 50+, earnings hidden",
    },
  ];

  return (
    <div className="seg universe" role="group" aria-label="Monitor">
      {options.map((option) => (
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
