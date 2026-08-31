import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { logout } from "../api/auth";

/* The menu grows as phases land; only working sections are rendered —
 * dead links are forbidden by SPEC §0.2. */
export default function Layout() {
  const navigate = useNavigate();

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
          Чаты
        </NavLink>
        <NavLink
          to="/account"
          className={({ isActive }) => (isActive ? "active" : "")}
        >
          Учётная запись
        </NavLink>
        <div className="spacer" />
        <a
          href="/login"
          onClick={(e) => {
            e.preventDefault();
            void onLogout();
          }}
        >
          Выйти
        </a>
      </nav>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
