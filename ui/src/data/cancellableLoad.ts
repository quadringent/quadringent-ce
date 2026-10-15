/**
 * Runs one asynchronous load and delivers its outcome only while it is still
 * the current one. Each call owns its flag and its `AbortController`, so a
 * slow response for a previous selection (pipeline A) can never overwrite the
 * state of the next selection (pipeline B): a shared "alive" ref cannot give
 * that guarantee because effect B re-arms it before the response of A lands.
 *
 * Use as the body of a `useEffect`: `return runCancellable(load, onOutcome);`
 */
export type LoadOutcome<T> =
  | { readonly status: 'ready'; readonly value: T }
  | { readonly status: 'failed'; readonly message: string; readonly error: unknown };

export function runCancellable<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deliver: (outcome: LoadOutcome<T>) => void,
): () => void {
  const controller = new AbortController();
  let current = true;
  load(controller.signal).then(
    (value) => {
      if (current) deliver({ status: 'ready', value });
    },
    (error: unknown) => {
      if (current) deliver({ status: 'failed', message: error instanceof Error ? error.message : String(error), error });
    },
  );
  return () => {
    current = false;
    controller.abort();
  };
}
