// Grafana is served same-origin at /grafana/, and the edge asks the api about
// every request there: only a signed-in admin gets through. Hiding this tab
// from everyone else is tidiness; the edge is the boundary.
const DASHBOARD = "/grafana/d/open-worlds";

export default function AdminMetricsSection() {
  return (
    <section className="admin-section">
      <div className="content-head">
        <h2>Metrics</h2>
        <div className="content-head-actions">
          {/* The full Grafana UI, Explore included. Read-only: panels can be
              edited in place but never saved. */}
          <a
            className="btn btn-secondary"
            href={DASHBOARD}
            target="_blank"
            rel="noopener"
          >
            Full screen ↗
          </a>
        </div>
      </div>
      <iframe
        className="metrics-frame"
        title="Platform metrics"
        src={`${DASHBOARD}?kiosk`}
      />
    </section>
  );
}
