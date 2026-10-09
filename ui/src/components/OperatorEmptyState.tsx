export function OperatorEmptyState({
  title,
  detail,
  established,
  missing,
}: {
  readonly title: string;
  readonly detail: string;
  readonly established: string;
  readonly missing: string;
}) {
  return (
    <section className="operator-empty-state" aria-label={title}>
      <p className="operator-empty-state__state">{established === "Unconfirmed" ? "Unconfirmed" : "Confirmed state"}</p>
      <h3>{title}</h3>
      <p>{detail}</p>
      <dl>
        <div><dt>Established</dt><dd>{established}</dd></div>
        <div><dt>Missing</dt><dd>{missing}</dd></div>
      </dl>
    </section>
  );
}
