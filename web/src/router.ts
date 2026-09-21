import { useEffect, useState } from "react";

/**
 * A router small enough to read in one sitting. Two routes and a query string do
 * not need a dependency: the History API plus one custom event is the whole thing.
 */
const CHANGE = "signals:navigate";

export interface Location {
  path: string;
  search: string;
}

function current(): Location {
  return { path: window.location.pathname, search: window.location.search };
}

export function navigate(to: string, { replace = false } = {}): void {
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(CHANGE));
}

export function useLocation(): Location {
  const [location, setLocation] = useState(current);
  useEffect(() => {
    const update = () => setLocation(current());
    window.addEventListener("popstate", update);
    window.addEventListener(CHANGE, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(CHANGE, update);
    };
  }, []);
  return location;
}

/** `/company/AAPL` -> "AAPL"; anything else -> null. */
export function companyFromPath(path: string): string | null {
  const match = /^\/company\/([^/]+)\/?$/.exec(path);
  return match ? decodeURIComponent(match[1]).toUpperCase() : null;
}

/** Plain left-click only, so cmd/ctrl-click still opens a new tab. */
export function onInternalClick(to: string) {
  return (event: React.MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(to);
  };
}
