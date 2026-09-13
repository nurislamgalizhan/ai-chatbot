import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import {
  createBrowserRouter,
  Navigate,
  Outlet,
  RouterProvider,
} from "react-router-dom";
import { getAccount } from "./api/auth";
import { ApiError } from "./api/client";
import Layout from "./components/Layout";
import AccountPage from "./pages/AccountPage";
import AgentPage from "./pages/AgentPage";
import AnalyticsPage from "./pages/AnalyticsPage";
import ChatsPage from "./pages/ChatsPage";
import CostsPage from "./pages/CostsPage";
import DashboardPage from "./pages/DashboardPage";
import KnowledgePage from "./pages/KnowledgePage";
import LimitsPage from "./pages/LimitsPage";
import LoginPage from "./pages/LoginPage";
import SystemPage from "./pages/SystemPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 15_000 },
  },
});

/** Redirects to /login when the session is missing or expired. */
function RequireAuth() {
  const { isPending, isError, error } = useQuery({
    queryKey: ["account"],
    queryFn: getAccount,
    retry: false,
  });
  if (isPending) return null;
  if (isError && error instanceof ApiError && error.status === 401) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      {
        element: <Layout />,
        children: [
          { path: "/", element: <DashboardPage /> },
          { path: "/chats", element: <ChatsPage /> },
          { path: "/costs", element: <CostsPage /> },
          { path: "/analytics", element: <AnalyticsPage /> },
          { path: "/agent", element: <AgentPage /> },
          { path: "/knowledge", element: <KnowledgePage /> },
          { path: "/limits", element: <LimitsPage /> },
          { path: "/system", element: <SystemPage /> },
          { path: "/account", element: <AccountPage /> },
        ],
      },
    ],
  },
]);

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
