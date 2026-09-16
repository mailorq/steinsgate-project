import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { catalogApi } from "@/shared/api";
import type { AnimeStatsOut } from "@/shared/api";
import { findAnimeBySlug } from "@/shared/config/animes";
import type { AnimeInfo } from "@/shared/config/animes";

export function animeStatsKey(slug: string) {
  return ["anime-stats", slug] as const;
}

export interface AnimeEntity {
  info: AnimeInfo;
  stats: AnimeStatsOut | null;
  isStatsPending: boolean;
}

export function useAnimeStats(slug: string) {
  return useQuery({
    queryKey: animeStatsKey(slug),
    queryFn: () => catalogApi.stats(slug),
    enabled: slug.length > 0,
  });
}

export function useAnime(slug: string | undefined): AnimeEntity | null {
  const info = findAnimeBySlug(slug);
  const { data, isPending } = useAnimeStats(info?.slug ?? "");

  if (info === undefined) {
    return null;
  }

  return { info, stats: data ?? null, isStatsPending: isPending };
}

export function useRateAnime(slug: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (rating: number) => catalogApi.rate(slug, rating),
    onSuccess: (result) => {
      queryClient.setQueryData<AnimeStatsOut>(animeStatsKey(slug), (current) =>
        current
          ? { ...current, avg_rating: result.avg_rating, user_rating: result.user_rating }
          : current,
      );
    },
  });
}
