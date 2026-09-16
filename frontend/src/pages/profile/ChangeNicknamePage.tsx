import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { ApiError, profileApi } from "@/shared/api";
import type { UserOut } from "@/shared/api";
import { useSession } from "@/shared/session/sessionContext";
import { FormCard } from "@/shared/ui/FormCard";
import { TextField } from "@/shared/ui/TextField";

const MAX_NICKNAME_LENGTH = 50;

export function ChangeNicknamePage() {
  const { user, isLoading } = useSession();

  if (isLoading) {
    return null;
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <ChangeNicknameForm user={user} />;
}

function ChangeNicknameForm({ user }: { user: UserOut }) {
  const navigate = useNavigate();
  const { setUser } = useSession();
  const [nickname, setNickname] = useState(user.nickname);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    document.title = "Change Nickname";
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = nickname.trim();
    if (!trimmed || trimmed.length > MAX_NICKNAME_LENGTH) {
      setError(`Никнейм должен быть от 1 до ${MAX_NICKNAME_LENGTH} символов`);
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      const updated = await profileApi.updateNickname(trimmed);
      setUser(updated);
      navigate("/profile");
    } catch (requestError) {
      setError(
        requestError instanceof ApiError ? requestError.message : "Не удалось выполнить запрос",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <FormCard title="Change Nickname" maxWidthClass="max-w-md">
      <p className="mb-6 text-center text-sm leading-relaxed text-zinc-500">
        Every Lab Member deserves a proper codename. Choose wisely.
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-6">
        <TextField
          id="nickname"
          label="New Nickname"
          type="text"
          placeholder="Enter new nickname"
          maxLength={150}
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          error={error ?? undefined}
        />

        <button
          type="submit"
          disabled={isSubmitting}
          className="w-full rounded-lg bg-amber-500 px-5 py-3.5 text-sm font-semibold text-zinc-950 transition-all duration-200 hover:bg-amber-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          Save Nickname
        </button>
      </form>

      <div className="mt-6 text-center">
        <Link to="/profile" className="text-sm text-amber-400 transition-colors hover:text-amber-300">
          Back to Settings
        </Link>
      </div>
    </FormCard>
  );
}
