import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { NavLink, Navigate, Route, HashRouter, Routes } from "react-router-dom";

import Graph from "./pages/Graph";
import Inbox from "./pages/Inbox";
import Insights from "./pages/Insights";
import ItemDetail from "./pages/ItemDetail";
import Ops from "./pages/Ops";
import Review from "./pages/Review";
import Search from "./pages/Search";
import "./styles.css";

function App() {
  return (
    <div className="app">
      <nav className="nav">
        <h1>Catchment</h1>
        <NavLink to="/" end>Inbox</NavLink>
        <NavLink to="/search">Search</NavLink>
        <NavLink to="/graph">Tag graph</NavLink>
        <NavLink to="/insights">Insights</NavLink>
        <NavLink to="/review">Review</NavLink>
        <NavLink to="/ops">Failures &amp; ops</NavLink>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Inbox />} />
          <Route path="/items/:id" element={<ItemDetail />} />
          <Route path="/search" element={<Search />} />
          {/* The seed tag lives in the path so a particular view is a link
              worth keeping, not a state you have to rebuild by hand. */}
          <Route path="/graph" element={<Graph />} />
          <Route path="/graph/:tagId" element={<Graph />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/review" element={<Review />} />
          <Route path="/ops" element={<Ops />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* HashRouter: served as static files behind the loopback API, with no
        server-side rewrite rules to configure. */}
    <HashRouter>
      <App />
    </HashRouter>
  </StrictMode>,
);
