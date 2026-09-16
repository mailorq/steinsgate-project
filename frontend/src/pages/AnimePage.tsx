import { useEffect, useRef } from "react";
import { Navigate, useParams } from "react-router-dom";

import { useAnime } from "@/entities/anime";
import { CommentsSection } from "@/features/comments/CommentsSection";
import { PlayerSwitcher } from "@/features/player/PlayerSwitcher";
import { RatingStars } from "@/features/rating/RatingStars";
import { WatchProgressBar } from "@/features/watch/WatchProgressBar";
import { useWatchProgress } from "@/features/watch/useWatchProgress";
import { catalogApi } from "@/shared/api";
import type { AnimeInfo } from "@/shared/config/animes";
import { useSession } from "@/shared/session/sessionContext";
import { Faq } from "@/shared/ui/Faq";

export function AnimePage() {
  const { slug } = useParams();
  const { user } = useSession();
  const anime = useAnime(slug);
  const info = anime?.info;
  const animeSlug = info?.slug;
  const trackedAnimeSlug = useRef<string | null>(null);
  const hasEpisodePlayer = info?.players.some((player) => player.type === "episodes") ?? false;
  const { progress, resume } = useWatchProgress(
    animeSlug ?? "",
    info !== undefined && user !== null && !hasEpisodePlayer,
  );

  useEffect(() => {
    if (info) {
      document.title = info.name;
    }
  }, [info]);

  useEffect(() => {
    if (!animeSlug) {
      return;
    }

    if (trackedAnimeSlug.current === animeSlug) {
      return;
    }

    trackedAnimeSlug.current = animeSlug;
    void catalogApi.registerView(animeSlug).catch(() => undefined);
  }, [animeSlug]);

  if (!info) {
    return <Navigate to="/steins-gate" replace />;
  }

  return (
    <>
      <AnimeDescription anime={info} />
      <PlayerSwitcher key={info.slug} animeSlug={info.slug} players={info.players} />
      {!hasEpisodePlayer && <WatchProgressBar progress={progress} onResume={resume} />}
      <Faq />
      <CommentsSection animeSlug={info.slug} />
    </>
  );
}

function AnimeDescription({ anime }: { anime: AnimeInfo }) {
  return (
    <div className="mx-auto mt-2 mb-10 max-w-5xl md:mb-14">
      <div className="flex flex-col gap-6 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-5 backdrop-blur-sm md:flex-row md:gap-8 md:p-7">
        <div className="mx-auto w-56 flex-shrink-0 md:mx-0 md:w-72 md:self-center">
          <img
            src={anime.poster}
            alt={anime.name}
            className="w-full rounded-xl shadow-2xl shadow-black/50 ring-1 ring-zinc-800"
          />
        </div>

        <div className="flex min-w-0 flex-col">
          <p className="font-mono text-xs tracking-[0.25em] text-amber-500/80 uppercase">
            Worldline {anime.worldline}
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight md:text-3xl">{anime.name}</h2>

          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1.5 text-sm text-zinc-400">
            <span>
              <span className="text-zinc-600">Сезон·</span> {anime.season}
            </span>
            <span>
              <span className="text-zinc-600">Тип·</span> {anime.type}
            </span>
          </div>
          <p className="mt-1.5 text-sm text-zinc-500">{anime.genres}</p>

          <div className="my-5 h-px w-full bg-zinc-800" />

          <RatingStars animeSlug={anime.slug} />

          <div className="my-5 h-px w-full bg-zinc-800" />

          <p className="text-sm leading-relaxed text-zinc-300 md:text-[15px]">{anime.description}</p>
        </div>
      </div>
    </div>
  );
}
