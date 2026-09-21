import { useCallback, useEffect, useRef, useState } from "react";
import type { Filters } from "../filters";
import { toSearch } from "../filters";
import { fetchFeed, type SignalEvent } from "./client";

const FAST_POLL_MS = 5_000; // socket down: polling is all there is
const SLOW_POLL_MS = 60_000; // socket up: a safety net, not the delivery path

/** Whether a pushed event belongs in the current view. Date bounds are left to
 *  the next refetch: a pushed event is by definition "now". */
function matches(event: SignalEvent, f: Filters): boolean {
  if (event.source === "system" && !f.system && !f.sources.includes("system")) return false;
  if (event.score < f.minScore) return false;
  if (f.tickers.length && !f.tickers.includes((event.ticker ?? "").toUpperCase())) return false;
  if (f.sources.length && !f.sources.includes(event.source)) return false;
  if (f.categories.length && !f.categories.includes(event.category ?? "")) return false;
  return true;
}

function sorted(map: Map<number, SignalEvent>): SignalEvent[] {
  return [...map.values()].sort(
    (a, b) => b.occurred_at.localeCompare(a.occurred_at) || b.id - a.id,
  );
}

/**
 * The feed: HTTP as the source of truth, the websocket as an optimisation.
 *
 * Pub/sub underneath is lossy, which is safe only because every (re)connect
 * refetches over HTTP and pushed messages are applied on top. Responses are
 * sequence-guarded, since overlapping requests can return out of order.
 */
export function useFeed(filters: Filters, onPush?: (event: SignalEvent) => void) {
  const [events, setEvents] = useState<SignalEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [flags, setFlags] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [fresh, setFresh] = useState<Set<number>>(new Set());

  const byId = useRef(new Map<number, SignalEvent>());
  const latestRequest = useRef(0);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const onPushRef = useRef(onPush);
  onPushRef.current = onPush;
  const key = toSearch(filters);

  const load = useCallback(async () => {
    const sequence = ++latestRequest.current;
    try {
      const page = await fetchFeed(filtersRef.current);
      if (sequence !== latestRequest.current) return;
      byId.current = new Map(page.events.map((r) => [r.id, r]));
      setEvents(page.events);
      setTotal(page.total);
      setFlags(page.flags);
      setError(null);
    } catch (err) {
      if (sequence !== latestRequest.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (sequence === latestRequest.current) setLoading(false);
    }
  }, []);

  // An "update" can be for an event this client never held -- a promoted insider
  // buy that was below the score filter a minute ago -- so it must be able to
  // insert, not only replace.
  const upsert = useCallback(
    (event: SignalEvent) => {
      onPushRef.current?.(event);
      const known = byId.current.has(event.id);
      if (matches(event, filtersRef.current)) byId.current.set(event.id, event);
      else byId.current.delete(event.id);
      setEvents(sorted(byId.current));
      if (!known) void load(); // counts come from the server; refresh them
      setFresh((prev) => new Set(prev).add(event.id));
      setTimeout(() => {
        setFresh((prev) => {
          const next = new Set(prev);
          next.delete(event.id);
          return next;
        });
      }, 6000);
    },
    [load],
  );

  useEffect(() => {
    void load();
  }, [load, key]);

  useEffect(() => {
    const timer = setInterval(() => void load(), live ? SLOW_POLL_MS : FAST_POLL_MS);
    return () => clearInterval(timer);
  }, [load, live]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let closed = false;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws`);
      socket.onopen = () => {
        attempts = 0;
        setLive(true);
        void load();
      };
      socket.onmessage = (frame) => {
        try {
          const message = JSON.parse(frame.data as string);
          if (message.type === "event.new" || message.type === "event.updated") {
            upsert(message.data as SignalEvent);
          }
        } catch {
          /* a malformed frame is not worth tearing the socket down for */
        }
      };
      socket.onclose = () => {
        setLive(false);
        if (closed) return;
        attempts += 1;
        retry = setTimeout(connect, Math.min(1000 * 2 ** attempts, 30_000));
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, [load, upsert]);

  return { events, total, flags, error, loading, live, fresh, reload: load };
}

/** Delay a fast-changing value, so dragging a slider is one request, not fifty. */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}
