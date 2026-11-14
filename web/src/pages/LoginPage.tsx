import { api } from "../api";
import WebviewNotice from "../components/WebviewNotice";

export default function LoginPage() {
  return (
    <div className="center-screen">
      <div className="card login-card">
        <img className="logo" src="/favicon-512.png" alt="" />
        <h1>Open Worlds</h1>
        <p className="muted">
          Spin up and manage your own game servers. Sign in to continue.
        </p>
        <WebviewNotice />
        <a className="btn btn-primary btn-block" href={api.loginUrl()}>
          Sign in with Google
        </a>
        {/* Local-only bypass (api/src/open_worlds/api/dev_login.py). Vite drops
            this block, URLs included, from production builds, and the api only
            serves the route in local dev. */}
        {import.meta.env.DEV && (
          <>
            <a
              className="btn btn-secondary btn-block"
              href="/api/auth/dev/login?role=admin"
            >
              Dev login: admin
            </a>
            <a
              className="btn btn-secondary btn-block"
              href="/api/auth/dev/login?role=user"
            >
              Dev login: user
            </a>
          </>
        )}
        <p className="fineprint muted">
          Access is invite-only. After signing in you'll receive an ID to send
          to an admin for approval.
        </p>
      </div>
    </div>
  );
}
