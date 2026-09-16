import { createContext, useContext } from "react";

import type { UserOut } from "@/shared/api";

export const SESSION_QUERY_KEY = ["session"];

export interface SessionContextValue {
  user: UserOut | null;
  isLoading: boolean;
  setUser: (user: UserOut | null) => void;
}

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useSession must be used within SessionProvider");
  }
  return context;
}
