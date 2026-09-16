import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppErrorBoundary } from "@/app/AppErrorBoundary";
import { Layout } from "@/app/Layout";
import { AnimePage } from "@/pages/AnimePage";
import { LabPage } from "@/pages/LabPage";
import { LoginPage } from "@/pages/auth/LoginPage";
import { LogoutPage } from "@/pages/auth/LogoutPage";
import { RegisterPage } from "@/pages/auth/RegisterPage";
import { VerifyEmailPage } from "@/pages/auth/VerifyEmailPage";
import { ChangeAvatarPage } from "@/pages/profile/ChangeAvatarPage";
import { ChangeNicknamePage } from "@/pages/profile/ChangeNicknamePage";
import { ProfilePage } from "@/pages/profile/ProfilePage";
import { SettingsPage } from "@/pages/profile/SettingsPage";
import { SessionProvider } from "@/shared/session/SessionProvider";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export function App() {
  return (
    <AppErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <BrowserRouter>
            <Routes>
              <Route element={<Layout />}>
                <Route path="/" element={<Navigate to="/steins-gate" replace />} />
                <Route path="/future-gadget-laboratory" element={<LabPage />} />

                <Route path="/register" element={<RegisterPage />} />
                <Route path="/login" element={<LoginPage />} />
                <Route path="/logout" element={<LogoutPage />} />
                <Route path="/verify-email" element={<VerifyEmailPage />} />

                <Route path="/profile" element={<ProfilePage />} />
                <Route path="/profile/settings" element={<SettingsPage />} />
                <Route path="/profile/nickname" element={<ChangeNicknamePage />} />
                <Route path="/profile/avatar" element={<ChangeAvatarPage />} />

                <Route path="/:slug" element={<AnimePage />} />
                <Route path="*" element={<Navigate to="/steins-gate" replace />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </SessionProvider>
      </QueryClientProvider>
    </AppErrorBoundary>
  );
}
