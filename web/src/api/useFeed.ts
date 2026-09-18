import { useCallback, useEffect, useRef, useState } from "react";
import { fetchFeed, type FeedQuery, type SignalEvent } from "./client";

const FAST_POLL_MS = 5_000; // socket down: polling is all there is
const SLOW_POLL_MS = 60_000; // socket up: a safety net, not the delivery path

function matches(event: SignalEvent, query: FeedQuery): boolean {
  if (event.score < query.minScore) return false;
  if (query.ticker && (event.ticker ?? "").toUpperCase() !== query.ticker.toUpperCase()) return false;
  if (query.source && event.source !== query.source) return false;
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
 * Pub/sub underneath is lossy -- a message published while this tab was
 * disconnected is simply gone. That is safe only because every (re)connect
 * refetches over HTTP, and pushed messages are applied on top of that.
 *
 * Responses are sequence-guarded: typing a ticker fires overlapping requests
 * that can return out of order, and a stale one must not overwrite a fresh one.
 */
export function useFeed(query: FeedQuery) {
  const [events, setEvents] = useState<SignalEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [live, setLive] = useState(false);
  const [fresh, setFresh] = useState<Set<number>>(new Set());

  const byId = useRef(new Map<number, SignalEvent>());
  const latestRequest = useRef(0);
  const queryRef = useRef(query);
  queryRef.current = query;

  const load = useCallback(async () => {
    const sequence = ++latestRequest.current;
    try {
      const rows = await fetchFeed(queryRef.current);
      if (sequence !== latestRequest.current) return;
      byId.current = new Map(rows.map((r) => [r.id, r]));
      setEvents(rows);
      setError(null);
      setUpdatedAt(new Date());
    } catch (err) {
      if (sequence !== latestRequest.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (sequence === latestRequest.current) setLoading(false);
    }
  }, []);

  // One path for both event.new and event.updated. An "update" can be for an
  // event this client never held -- a promoted insider buy that was below the
  // score filter a minute ago -- so it must be able to insert, not only replace.
  const upsert = useCallback((event: SignalEvent) => {
    if (matches(event, queryRef.current)) byId.current.set(event.id, event);
    else byId.current.delete(event.id);
    setEvents(sorted(byId.current));
    setUpdatedAt(new Date());
    setFresh((prev) => new Set(prev).add(event.id));
    setTimeout(() => {
      setFresh((prev) => {
        const next = new Set(prev);
        next.delete(event.id);
        return next;
      });
    }, 6000);
  }, []);

  // refetch whenever the filters change
  useEffect(() => {
    void load();
  }, [load, query.minScore, query.ticker, query.source]);

  // polling: fast when the socket is down, slow when it is up
  useEffect(() => {
    const timer = setInterval(() => void load(), live ? SLOW_POLL_MS : FAST_POLL_MS);
    return () => clearInterval(timer);
  }, [load, live]);

  // the socket, with reconnect backoff
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
        void load(); // catch up on anything published while disconnected
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

  return { events, error, loading, updatedAt, live, fresh, reload: load };
}

/** Delay a fast-changing value, so typing does not fire a request per keystroke. */
export function useDebounced<T>(value: T, delayMs = 250): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}
