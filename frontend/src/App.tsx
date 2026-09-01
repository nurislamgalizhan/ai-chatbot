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
import ChatsPage from "./pages/ChatsPage";
import KnowledgePage from "./pages/KnowledgePage";
import LoginPage from "./pages/LoginPage";

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
          { path: "/", element: <ChatsPage /> },
          { path: "/knowledge", element: <KnowledgePage /> },
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
