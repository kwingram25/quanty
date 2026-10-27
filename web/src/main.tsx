import { MotionConfig } from "motion/react";
import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { Accounts } from "./components/Accounts";
import { ToastProvider } from "./components/Toasts";
import { FeedsPage } from "./pages/Feeds";
import { RunPage } from "./pages/Run";
import { HealthPage } from "./pages/Health";
import { RunsPage } from "./pages/Runs";
import { setZone, useZone, zoneName } from "./format";
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

/** Times in your own zone or UTC; the label names the zone in use. */
function ZoneToggle() {
  const z = useZone();
  const other = z === "utc" ? "your time zone" : "UTC";
  return (
    <button className="icon-btn zone" onClick={() => setZone(z === "utc" ? "local" : "utc")}
      title={`Times are in ${zoneName(Date.now(), true)}. Click for ${other}.`} aria-label={`Show times in ${other}`}>
      {zoneName()}
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

const REPO = "https://github.com/kwingram25/quanty";
// a hosted demo: hours between its fresh starts
const PUBLIC = document.querySelector<HTMLMetaElement>('meta[name="quanty-public"]')?.content;

function App() {
  useZone(); // re-render every page when the time zone changes
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
          <NavLink to="/health">Health</NavLink>
        </nav>
        <Accounts />
        <ZoneToggle />
        <ThemeToggle />
        {document.querySelector('meta[name="quanty-auth"]') && (
          <form method="post" action="/logout"><button className="icon-btn signout" title="Sign out">Sign out</button></form>
        )}
      </div>
      {PUBLIC && (
        <DemoBanner>
          A demo on synthetic markets, shared by everyone viewing it: the controls work, and it starts over
          every {PUBLIC} hours. <a href={REPO} target="_blank" rel="noreferrer">quanty on GitHub</a>
        </DemoBanner>
      )}
      <Routes>
        <Route path="/" element={<RunsPage />} />
        <Route path="/feeds" element={<FeedsPage />} />
        <Route path="/health" element={<HealthPage />} />
        <Route path="/runs/:mode/:name" element={<RunPage />} />
        <Route path="*" element={<div className="page"><p className="empty">Nothing here. <Link to="/">Back to runs</Link></p></div>} />
      </Routes>
    </BrowserRouter>
  );
}

/** The hosted demo's note under the top bar. Pages sized to the window (a run's) leave room for it:
 *  its height, however many lines it wraps to, is --banner-h. */
function DemoBanner({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const root = document.documentElement.style;
    const size = new ResizeObserver(() => root.setProperty("--banner-h", `${el.offsetHeight}px`));
    size.observe(el);
    return () => { size.disconnect(); root.removeProperty("--banner-h"); };
  }, []);
  return <p className="demo-banner" ref={ref}>{children}</p>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MotionConfig reducedMotion="user">
      <ToastProvider><App /></ToastProvider>
    </MotionConfig>
  </StrictMode>,
);
