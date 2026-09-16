import { useEffect } from "react";
import { Link, Navigate } from "react-router-dom";

import { useSession } from "@/shared/session/sessionContext";
import { Avatar } from "@/shared/ui/Avatar";

export function ProfilePage() {
  const { user, isLoading } = useSession();

  useEffect(() => {
    document.title = "Profile";
  }, []);

  if (isLoading) {
    return null;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="mx-auto mt-10 mb-16 max-w-xl">
      <p className="mb-4 px-1 font-mono text-xs tracking-[0.25em] text-amber-500/80 uppercase">
        Lab Member
      </p>

      <div className="overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-950/60 shadow-2xl shadow-black/40 backdrop-blur-sm">
        <div className="flex items-center gap-5 p-6 md:gap-6 md:p-8">
          <Avatar
            avatarUrl={user.avatar_url}
            username={user.username}
            sizeClass="w-24 h-24 md:w-28 md:h-28"
            textSizeClass="text-3xl md:text-4xl"
          />
          <div className="min-w-0">
            <h2 className="truncate text-2xl font-semibold tracking-tight md:text-3xl">
              {user.nickname}
            </h2>
            <p className="mt-1 truncate text-base text-zinc-500">@{user.username}</p>
            <p className="mt-2 truncate text-sm text-zinc-600">{user.email}</p>
          </div>
        </div>

        <div className="border-t border-zinc-800/80">
          <ProfileAction to="/profile/nickname" label="Изменить никнейм" hint={user.nickname} />
          <ProfileAction
            to="/profile/avatar"
            label="Изменить аватар"
            hint={user.avatar_url ? "загружен" : "не задан"}
          />
        </div>

        <div className="border-t border-zinc-800/80 p-3">
          <Link
            to="/logout"
            className="flex items-center justify-center rounded-xl px-4 py-3 text-base font-medium text-red-400/90 transition-colors hover:bg-red-950/30 hover:text-red-300"
          >
            Выйти из аккаунта
          </Link>
        </div>
      </div>
    </div>
  );
}

function ProfileAction({ to, label, hint }: { to: string; label: string; hint: string }) {
  return (
    <Link
      to={to}
      className="group flex items-center justify-between px-6 py-5 transition-colors hover:bg-zinc-900/60 md:px-8"
    >
      <span className="text-base text-zinc-200">{label}</span>
      <span className="flex items-center gap-3">
        <span className="max-w-[10rem] truncate text-sm text-zinc-600">{hint}</span>
        <span className="text-zinc-600 transition-all duration-200 group-hover:translate-x-0.5 group-hover:text-amber-500">
          →
        </span>
      </span>
    </Link>
  );
}
