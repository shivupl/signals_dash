import { useEffect, useRef, useState } from "react";
import { localZone, MARKET_ZONE, useChooseZone, useZone, zoneAbbreviation } from "../zone";

/**
 * Which clock the feed is read in. Market time by default, because that is the
 * clock the filings are stamped with; the other two are here for anyone who has
 * to do the arithmetic in their head twenty times a day.
 *
 * The date beside a time follows the clock, so the two always agree. What does
 * not move is which day a filing belongs to: a filing belongs to the session it
 * was published into, so "today" and the date filters stay market time whatever
 * this says.
 */
export function TimeZonePicker() {
  const zone = useZone();
  const choose = useChooseZone();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const here = localZone();
  // Offering "your time" to somebody already in New York would be two names for
  // one clock, so it only appears when it is actually a different one.
  const options = [
    { value: MARKET_ZONE, label: "Market time", note: "ET, the clock filings are stamped with" },
    ...(here === MARKET_ZONE
      ? []
      : [{ value: here, label: "Your time", note: `${zoneAbbreviation(here)}, this browser` }]),
    { value: "UTC", label: "UTC", note: "no daylight saving" },
  ];

  return (
    <span className="tz" ref={ref}>
      <button
        className="tz-button mono"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title="Which clock times are shown in. “today” and the date filters stay market time."
      >
        times {zoneAbbreviation(zone)}
      </button>
      {open && (
        <div className="pop tz-pop" role="listbox">
          {options.map((option) => (
            <button
              key={option.value}
              className={`pop-row pick${option.value === zone ? " on" : ""}`}
              role="option"
              aria-selected={option.value === zone}
              onClick={() => {
                choose(option.value);
                setOpen(false);
              }}
            >
              <span className="nm zone-label">{option.label}</span>
              <span className="nm">{option.note}</span>
            </button>
          ))}
          <div className="pop-note">&ldquo;today&rdquo; and date filters stay market time</div>
        </div>
      )}
    </span>
  );
}
