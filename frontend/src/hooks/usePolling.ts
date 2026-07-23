import { useCallback, useEffect, useRef, useState } from 'react';

interface PollingState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
}

interface PollingResult<T> extends PollingState<T> {
  /** Fetch immediately, outside the regular interval (e.g. right after a
   * mutation like stop/restart) and reset the interval clock from there. */
  refetch: () => void;
}

/** Fetches immediately, then on a fixed interval. Keeps the last-known-good
 * `data` visible across a failed poll rather than clearing it, so a
 * transient backend hiccup doesn't blank out the dashboard. */
export function usePolling<T>(fetchFn: () => Promise<T>, intervalMs: number): PollingResult<T> {
  const [state, setState] = useState<PollingState<T>>({ data: null, error: null, loading: true });
  const fetchFnRef = useRef(fetchFn);
  fetchFnRef.current = fetchFn;
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const data = await fetchFnRef.current();
        if (!cancelled) setState({ data, error: null, loading: false });
      } catch (error) {
        if (!cancelled) setState((prev) => ({ data: prev.data, error, loading: false }));
      }
    }

    poll();
    const id = setInterval(poll, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs, reloadToken]);

  const refetch = useCallback(() => setReloadToken((t) => t + 1), []);

  return { ...state, refetch };
}
