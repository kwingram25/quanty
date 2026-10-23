import { MotionConfig } from "motion/react";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { Accounts } from "./components/Accounts";
import { ToastProvider } from "./components/Toasts";
import { FeedsPage } from "./pages/Feeds";
import { RunPage } from "./pages/Run";
import { RunsPage } from "./pages/Runs";
import "./styles.css";

function ThemeToggle() {
  const [dark, setDark] = useState(() => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  });
  const flip = () => {
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
    setDark(!dark);
  };
  return (
    <button className="icon-btn" onClick={flip} aria-label={`Switch to ${dark ? "light" : "dark"} theme`}>
      {dark ? "☀" : "☾"}
    </button>
  );
}

/** g then r / f: go to runs / feeds. */
function Shortcuts() {
  const nav = useNavigate();
  useEffect(() => {
    let g = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey) return;
      if (e.key === "g") { g = Date.now(); return; }
      if (Date.now() - g < 800 && (e.key === "r" || e.key === "f")) nav(e.key === "r" ? "/" : "/feeds");
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [nav]);
  return null;
}

function App() {
  return (
    <BrowserRouter>
      <Shortcuts />
      <div className="topbar">
        <Link to="/" className="brand" aria-label="quanty home">
          <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
            <path d="M1 12 H5 V8 H9 V10 H15" fill="none" stroke="currentColor" strokeWidth="2.2" />
          </svg>quanty
        </Link>
        <nav>
          <NavLink to="/" end>Runs</NavLink>
          <NavLink to="/feeds">Feeds</NavLink>
        </nav>
        <Accounts />
        <ThemeToggle />
      </div>
      <Routes>
        <Route path="/" element={<RunsPage />} />
        <Route path="/feeds" element={<FeedsPage />} />
        <Route path="/runs/:mode/:name" element={<RunPage />} />
        <Route path="*" element={<div className="page"><p className="empty">Nothing here. <Link to="/">Back to runs</Link></p></div>} />
      </Routes>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <ToastProvider><App /></ToastProvider>
    </MotionConfig>
  </StrictMode>,
);
