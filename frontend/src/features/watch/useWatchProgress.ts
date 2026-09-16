import { useEffect, useRef } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { watchApi } from "@/shared/api";

const PLAYER_ORIGINS = ["kodikplayer.com", "anilibria.tv"];
const SAVE_DEBOUNCE_MS = 5000;
const POLL_INTERVAL_MS = 10000;
const MIN_SAVE_SECONDS = 10;

function isPlayerOrigin(origin: string): boolean {
  try {
    const host = new URL(origin).hostname;
    return PLAYER_ORIGINS.some((domain) => host === domain || host.endsWith(`.${domain}`));
  } catch {
    return false;
  }
}

function playerFrames(): HTMLIFrameElement[] {
  return Array.from(document.querySelectorAll<HTMLIFrameElement>("iframe.player-embed"));
}

function postToPlayer(frame: HTMLIFrameElement, message: object): void {
  try {
    frame.contentWindow?.postMessage(message, new URL(frame.src).origin);
  } catch {
    // the iframe may be removed or have an invalid url while the page switches players
  }
}

export function useWatchProgress(animeSlug: string, enabled: boolean) {
  const queryClient = useQueryClient();
  const saveTimeoutRef = useRef<number | null>(null);

  const { data: progress } = useQuery({
    queryKey: ["progress", animeSlug],
    queryFn: ({ signal }) => watchApi.get(animeSlug, signal),
    enabled,
  });

  const saveMutation = useMutation({
    mutationFn: (payload: { current_time: number; duration: number }) =>
      watchApi.save(animeSlug, payload),
    onSuccess: (result) => {
      queryClient.setQueryData(["progress", animeSlug], result);
    },
  });

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const onMessage = (event: MessageEvent) => {
      if (!isPlayerOrigin(event.origin)) {
        return;
      }
      const currentTime = event.data?.currentTime;
      if (typeof currentTime !== "number" || currentTime < MIN_SAVE_SECONDS) {
        return;
      }
      const duration = typeof event.data?.duration === "number" ? event.data.duration : 0;

      if (saveTimeoutRef.current !== null) {
        window.clearTimeout(saveTimeoutRef.current);
      }
      saveTimeoutRef.current = window.setTimeout(() => {
        saveMutation.mutate({ current_time: currentTime, duration });
      }, SAVE_DEBOUNCE_MS);
    };

    const poll = window.setInterval(() => {
      for (const frame of playerFrames()) {
        postToPlayer(frame, { action: "getTime" });
      }
    }, POLL_INTERVAL_MS);

    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
      window.clearInterval(poll);
      if (saveTimeoutRef.current !== null) {
        window.clearTimeout(saveTimeoutRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animeSlug, enabled]);

  function resume() {
    if (!progress || progress.current_time < MIN_SAVE_SECONDS) {
      return;
    }
    for (const frame of playerFrames()) {
      postToPlayer(frame, { action: "seek", time: progress.current_time });
    }
  }

  return { progress: progress ?? null, resume };
}

export function formatTime(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${pad(minutes)}:${pad(secs)}`;
  }
  return `${minutes}:${pad(secs)}`;
}
