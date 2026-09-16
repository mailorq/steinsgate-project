import { useState } from "react";

import { useAnimeStats, useRateAnime } from "@/entities/anime";
import { useSession } from "@/shared/session/sessionContext";

const STARS = [1, 2, 3, 4, 5];

interface RatingStarsProps {
  animeSlug: string;
}

export function RatingStars({ animeSlug }: RatingStarsProps) {
  const { user } = useSession();
  const [hovered, setHovered] = useState<number | null>(null);

  // тот же ключ, что у страницы. запрос уходит один
  const { data } = useAnimeStats(animeSlug);
  const rateMutation = useRateAnime(animeSlug);

  const avgRating = data?.avg_rating ?? null;
  const userRating = data?.user_rating ?? null;
  const totalViews = data?.total_views;

  return (
    <>
      <div className="mb-4 flex items-center gap-4">
        <div className="flex items-center gap-2">
          <span className="text-sm text-zinc-400">Рейтинг:</span>
          <div className="flex items-center gap-1">
            {avgRating === null ? (
              <span className="text-zinc-500">Нет оценок</span>
            ) : (
              <>
                {STARS.map((star) => (
                  <StarIcon key={star} filled={star <= Math.round(avgRating)} sizeClass="w-5 h-5" />
                ))}
                <span className="ml-2 text-zinc-300">{avgRating.toFixed(1)}</span>
              </>
            )}
          </div>
        </div>
        <div className="text-sm text-zinc-400">
          Просмотров: <span className="text-zinc-300">{totalViews ?? "—"}</span>
        </div>
      </div>

      {user && (
        <div className="mb-4">
          <span className="mb-2 block text-sm text-zinc-400">Ваша оценка:</span>
          <div className="flex gap-1">
            {STARS.map((star) => (
              <button
                key={star}
                type="button"
                onClick={() => rateMutation.mutate(star)}
                onMouseEnter={() => setHovered(star)}
                onMouseLeave={() => setHovered(null)}
                className="transition hover:scale-110"
              >
                <StarIcon filled={star <= (hovered ?? userRating ?? 0)} sizeClass="w-6 h-6" />
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function StarIcon({ filled, sizeClass }: { filled: boolean; sizeClass: string }) {
  return (
    <svg
      className={`${sizeClass} ${filled ? "text-amber-400" : "text-zinc-600"}`}
      fill="currentColor"
      viewBox="0 0 20 20"
    >
      <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z" />
    </svg>
  );
}
