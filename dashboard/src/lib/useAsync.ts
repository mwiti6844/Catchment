import { useCallback, useEffect, useState } from "react";

/**
 * Minimal async state. Deliberately not a data-fetching library: this app has
 * five screens on localhost, and a cache layer would be more machinery than
 * the problem has.
 */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  const run = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .then((value) => !cancelled && (setData(value), setError(null)))
      .catch((err) => !cancelled && setError(err as Error))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(run, [run]);
  return { data, error, loading, reload: run };
}
