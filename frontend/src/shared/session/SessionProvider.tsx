import type { ReactNode } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { authApi } from "@/shared/api";

import { SESSION_QUERY_KEY, SessionContext } from "./sessionContext";
import type { SessionContextValue } from "./sessionContext";

export function SessionProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: authApi.session,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const value: SessionContextValue = {
    user: data?.user ?? null,
    isLoading,
    setUser: (user) => queryClient.setQueryData(SESSION_QUERY_KEY, { user }),
  };

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
