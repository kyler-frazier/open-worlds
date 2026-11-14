import { useNavigate } from "react-router";
import { api } from "../api";
import { useAuth } from "../auth";
import { confirmDialog } from "../confirmService";
import UserMenu from "./UserMenu";

export default function Header() {
  const { me, logout, logoutEverywhere } = useAuth();
  const navigate = useNavigate();

  const signOut = async () => {
    await logout();
    navigate("/");
  };

  // Plain sign-out only clears this browser's cookie; any other copy stays
  // valid for the rest of its week. This ends all of them.
  const signOutEverywhere = async () => {
    const ok = await confirmDialog({
      title: "Sign out everywhere",
      message:
        "This signs you out of every browser and device where you're signed in, including this one. Use it if you signed in on a shared computer or think your session may have leaked.",
      confirmLabel: "Sign out everywhere",
    });
    if (!ok) return;
    await logoutEverywhere();
    navigate("/");
  };

  return (
    <header className="app-header">
      <div className="brand" onClick={() => navigate("/")}>
        <img className="logo-sm" src="/favicon-192.png" alt="" />
        <span>Open Worlds</span>
      </div>
      <div className="header-actions">
        {me && !me.drive_linked && (
          <a className="btn btn-secondary" href={api.driveLinkUrl()}>
            Link Google Drive
          </a>
        )}
        {me && (
          <UserMenu label={me.email ?? me.display_name ?? "Account"}>
            {(close) => (
              <>
                <div className="user-menu-who muted small">
                  {me.display_name && <div>{me.display_name}</div>}
                  <div className="mono">{me.email}</div>
                  {/* Status about the account, so it belongs with the account
                      rather than competing with the brand for header width.
                      The "Link Google Drive" prompt stays up top: that one is
                      an action, and an action behind a menu is easy to miss. */}
                  {me.drive_linked && (
                    <div className="user-menu-drive">
                      <span className="pill pill-ok">Drive linked</span>
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  className="user-menu-item"
                  role="menuitem"
                  onClick={() => {
                    close();
                    void signOut();
                  }}
                >
                  Sign out
                </button>
                <button
                  type="button"
                  className="user-menu-item user-menu-item-danger"
                  role="menuitem"
                  title="End your session in every browser, including this one"
                  onClick={() => {
                    close();
                    void signOutEverywhere();
                  }}
                >
                  Sign out everywhere
                </button>
              </>
            )}
          </UserMenu>
        )}
      </div>
    </header>
  );
}
