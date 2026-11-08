import Link from "next/link";

export default function NotFound() {
  return (
    <div className="card center-block">
      <h1 className="card__title">Page not found</h1>
      <p className="muted">There is nothing at this address.</p>
      <Link href="/" className="button">
        Back to missions
      </Link>
    </div>
  );
}
