import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/** IANA name of the zone times are *displayed* in. */
export type Zone = string;

export const MARKET_ZONE = "America/New_York";
const KEY = "signals.zone";

/** The viewer's own zone, or market time if the browser will not say. */
export function localZone(): Zone {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || MARKET_ZONE;
  } catch {
    return MARKET_ZONE;
  }
}

/** "EDT", "PDT", "UTC" -- what to call a zone right now, rather than in January.
 *  Market time keeps the generic "ET": it is the name the filings use. */
export function zoneAbbreviation(zone: Zone, at: Date = new Date()): string {
  if (zone === MARKET_ZONE) return "ET";
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      timeZoneName: "short",
    }).formatToParts(at);
    return parts.find((p) => p.type === "timeZoneName")?.value ?? zone;
  } catch {
    return zone;
  }
}

function read(): Zone | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    // Private window, or site data blocked. The choice just is not remembered.
    return null;
  }
}

function write(zone: Zone): void {
  try {
    if (zone === MARKET_ZONE) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, zone);
  } catch {
    /* as above */
  }
}

const ZoneContext = createContext<Zone>(MARKET_ZONE);

/**
 * Which zone the clock is rendered in.
 *
 * Deliberately *not* in the URL, where every filter lives. A filter URL is meant
 * to be shared, and a timezone travelling with it would show the recipient your
 * evening rather than theirs. This is per-viewer, per-browser, like the admin
 * token: display state, not a description of the view.
 *
 * It changes what the clock reads and nothing else. `marketDay` stays in market
 * time, because it decides which day a filing belongs to and builds the
 * since/until the API is asked for -- a 16:05 ET filing belongs to that
 * afternoon whoever is looking at it, and from Los Angeles a viewer-zone
 * "today" would quietly file it under the wrong date.
 */
export function ZoneProvider({ children }: { children: ReactNode }) {
  const [zone, setZone] = useState<Zone>(() => read() ?? MARKET_ZONE);
  const choose = useCallback((next: Zone) => {
    write(next);
    setZone(next);
  }, []);
  const value = useMemo(() => ({ zone, choose }), [zone, choose]);
  return (
    <ZoneSetter.Provider value={value.choose}>
      <ZoneContext.Provider value={value.zone}>{children}</ZoneContext.Provider>
    </ZoneSetter.Provider>
  );
}

const ZoneSetter = createContext<(zone: Zone) => void>(() => undefined);

export const useZone = (): Zone => useContext(ZoneContext);
export const useChooseZone = (): ((zone: Zone) => void) => useContext(ZoneSetter);
