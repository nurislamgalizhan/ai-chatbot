import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { getAccount, logout } from "../api/auth";
import Icon from "./icons";

const PAGE_TITLES: Record<string, string> = {
  "/": "Чаты",
  "/account": "Учётная запись",
};

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? "").join("") || "А";
}

/* The menu grows as phases land; only working sections are rendered —
 * dead links are forbidden by SPEC §0.2. */
export default function Layout() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // Same query key as RequireAuth: served from cache, no extra request.
  const account = useQuery({ queryKey: ["account"], queryFn: getAccount });

  async function onLogout() {
    try {
      await logout();
    } finally {
      navigate("/login", { replace: true });
    }
  }

  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">Админ-панель</div>
        <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="chat" /> Чаты
        </NavLink>
        <NavLink
          to="/account"
          className={({ isActive }) => (isActive ? "active" : "")}
        >
          <Icon name="user" /> Учётная запись
        </NavLink>
        <div className="spacer" />
        <a
          href="/login"
          onClick={(e) => {
            e.preventDefault();
            void onLogout();
          }}
        >
          <Icon name="logout" /> Выйти
        </a>
      </nav>
      <div className="main-column">
        <header className="topbar">
          <h1>{PAGE_TITLES[pathname] ?? "Админ-панель"}</h1>
          {account.data && (
            <div className="user-pill">
              <span className="avatar">{initials(account.data.full_name)}</span>
              <span className="who">
                <span className="name">{account.data.full_name}</span>
                <span className="sub">{account.data.phone}</span>
              </span>
            </div>
          )}
        </header>
        <main className={pathname === "/" ? "page page--full" : "page"}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
