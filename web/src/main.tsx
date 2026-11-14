import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import App from "./App";
import { AuthProvider } from "./auth";
import ConfirmHost from "./components/ConfirmHost";
import { GamesProvider } from "./games";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <GamesProvider>
          <App />
          <ConfirmHost />
        </GamesProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
