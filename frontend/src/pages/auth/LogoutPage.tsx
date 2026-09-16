import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { authApi } from "@/shared/api";
import { useSession } from "@/shared/session/sessionContext";
import { FormCard } from "@/shared/ui/FormCard";

export function LogoutPage() {
  const navigate = useNavigate();
  const { setUser } = useSession();
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    document.title = "Logout";
  }, []);

  async function handleLogout() {
    setIsSubmitting(true);
    try {
      await authApi.logout();
    } finally {
      setUser(null);
      setIsSubmitting(false);
      navigate("/steins-gate");
    }
  }

  return (
    <FormCard title="Logout" maxWidthClass="max-w-md">
      <p className="mb-8 text-center text-sm text-zinc-500">Are you sure you want to log out?</p>
      <div className="text-center">
        <button
          type="button"
          onClick={handleLogout}
          disabled={isSubmitting}
          className="inline-flex items-center justify-center rounded-lg border border-red-900/60 bg-red-950/30 px-8 py-3 text-sm font-medium text-red-400 transition-all duration-200 hover:border-red-700 hover:bg-red-950/60 hover:text-red-300 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          Logout
        </button>
      </div>
    </FormCard>
  );
}
