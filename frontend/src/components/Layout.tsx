import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { getAccount, logout } from "../api/auth";
import { getAiSwitch, setAiSwitch } from "../api/system";
import Icon from "./icons";

const PAGE_TITLES: Record<string, string> = {
  "/": "Чаты",
  "/knowledge": "База знаний",
  "/account": "Учётная запись",
};

function GlobalAiSwitch() {
  const queryClient = useQueryClient();
  const state = useQuery({
    queryKey: ["ai-switch"],
    queryFn: getAiSwitch,
    refetchInterval: 15000,
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setAiSwitch(enabled),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["ai-switch"] }),
  });
  if (!state.data) return null;
  const enabled = state.data.enabled;
  return (
    <button
      className={enabled ? "ai-global on" : "ai-global off"}
      disabled={toggle.isPending}
      onClick={() => toggle.mutate(!enabled)}
      title="Глобальный аварийный выключатель: глушит AI во всех чатах, не меняя режимы отдельных чатов"
    >
      <span className="dot" />
      {enabled ? "AI-агент активен" : "AI отключён глобально"}
    </button>
  );
}

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
          to="/knowledge"
          className={({ isActive }) => (isActive ? "active" : "")}
        >
          <Icon name="book" /> База знаний
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
          <div className="topbar-right">
            <GlobalAiSwitch />
            {account.data && (
            <div className="user-pill">
              <span className="avatar">{initials(account.data.full_name)}</span>
              <span className="who">
                <span className="name">{account.data.full_name}</span>
                <span className="sub">{account.data.phone}</span>
              </span>
            </div>
            )}
          </div>
        </header>
        <main className={pathname === "/" ? "page page--full" : "page"}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
