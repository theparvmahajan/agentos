"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="card center-block" role="alert">
      <h1 className="card__title">Something went wrong</h1>
      <p className="muted">The page hit an unexpected error.</p>
      <button type="button" className="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
