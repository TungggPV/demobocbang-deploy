import { useCallback, useEffect, useRef, useState } from "react";
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: React.DependencyList) {
  const [data, setData] = useState<T>(); const [error, setError] = useState<Error>(); const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const controller = useRef<AbortController | undefined>(undefined);
  const run = useCallback(async () => {
    const current = ++sequence.current;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setLoading(true);
    setError(undefined);
    try {
      const result = await fn(request.signal);
      if (!request.signal.aborted && current === sequence.current) setData(result);
    } catch (cause) {
      if (!request.signal.aborted && current === sequence.current) setError(cause as Error);
    } finally {
      if (controller.current === request) controller.current = undefined;
      if (!request.signal.aborted && current === sequence.current) setLoading(false);
    }
  }, deps);
  useEffect(() => {
    void run();
    return () => {
      controller.current?.abort();
      sequence.current += 1;
    };
  }, [run]);
  return { data, error, loading, reload: run, setData };
}
