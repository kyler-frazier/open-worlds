import { Navigate, Route, Routes } from "react-router";
import { useAuth } from "./auth";
import AdminPage from "./pages/AdminPage";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import PendingPage from "./pages/PendingPage";

function FullPageSpinner() {
  return (
    <div className="center-screen">
      <div className="spinner" />
    </div>
  );
}

export default function App() {
  const { me, loading } = useAuth();

  return (
    <Routes>
      <Route path="/pending" element={<PendingPage />} />
      <Route
        path="/"
        element={
          loading ? <FullPageSpinner /> : me ? <DashboardPage /> : <LoginPage />
        }
      />
      <Route
        path="/admin"
        element={
          loading ? (
            <FullPageSpinner />
          ) : me?.is_admin ? (
            <AdminPage />
          ) : (
            <Navigate to="/" replace />
          )
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
