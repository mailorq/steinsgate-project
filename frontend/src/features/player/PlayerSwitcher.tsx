import { useEffect, useRef, useState } from "react";
import type {
  KeyboardEvent,
  PointerEvent as ReactPointerEvent,
  WheelEvent,
} from "react";

import type { AnimeEpisode, AnimePlayer, EpisodeAnimePlayer } from "@/shared/config/animes";

interface PlayerSwitcherProps {
  animeSlug: string;
  players: AnimePlayer[];
}

interface SavedPlayerState {
  version: 1;
  playerId: string;
  episodes: Record<string, number>;
  episodeRailScrollLefts: Record<string, number>;
}

interface Selection {
  player: AnimePlayer;
  episode: AnimeEpisode | null;
}

interface RailScrollMetrics {
  scrollLeft: number;
  scrollWidth: number;
  clientWidth: number;
}

interface EpisodeNavigationProps {
  direction: "previous" | "next";
  disabled: boolean;
  isHidden: boolean;
  onClick: () => void;
}

const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;
const RAIL_SCROLL_SAVE_DEBOUNCE_MS = 150;

function cookieName(animeSlug: string): string {
  return `sg-player-state-${animeSlug}`;
}

function isNonNegativeIntegerRecord(value: unknown): value is Record<string, number> {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.values(value).every((item) => typeof item === "number" && Number.isInteger(item) && item >= 0)
  );
}

function readSavedState(animeSlug: string): SavedPlayerState | null {
  if (typeof document === "undefined") {
    return null;
  }

  const rawCookie = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith(`${cookieName(animeSlug)}=`));

  if (!rawCookie) {
    return null;
  }

  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(rawCookie.slice(rawCookie.indexOf("=") + 1)));
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("version" in parsed) ||
      !("playerId" in parsed) ||
      !("episodes" in parsed) ||
      parsed.version !== 1 ||
      typeof parsed.playerId !== "string" ||
      !isNonNegativeIntegerRecord(parsed.episodes)
    ) {
      return null;
    }

    // Cookies created before the horizontal rail did not contain its position.
    // They remain valid and acquire the field on the first interaction.
    const episodeRailScrollLefts =
      "episodeRailScrollLefts" in parsed && isNonNegativeIntegerRecord(parsed.episodeRailScrollLefts)
        ? parsed.episodeRailScrollLefts
        : {};

    return {
      version: 1,
      playerId: parsed.playerId,
      episodes: parsed.episodes,
      episodeRailScrollLefts,
    };
  } catch {
    return null;
  }
}

function writeState(animeSlug: string, state: SavedPlayerState): void {
  if (typeof document === "undefined") {
    return;
  }

  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${cookieName(animeSlug)}=${encodeURIComponent(JSON.stringify(state))}; Max-Age=${COOKIE_MAX_AGE_SECONDS}; Path=/; SameSite=Lax${secure}`;
}

function saveSelection(animeSlug: string, selection: Selection): void {
  const previous = readSavedState(animeSlug);
  const episodes = selection.episode
    ? { ...previous?.episodes, [selection.player.id]: selection.episode.number }
    : previous?.episodes ?? {};

  writeState(animeSlug, {
    version: 1,
    playerId: selection.player.id,
    episodes,
    episodeRailScrollLefts: previous?.episodeRailScrollLefts ?? {},
  });
}

function saveRailPosition(animeSlug: string, playerId: string, scrollLeft: number): void {
  const previous = readSavedState(animeSlug);
  if (!previous) {
    return;
  }

  writeState(animeSlug, {
    ...previous,
    episodeRailScrollLefts: {
      ...previous.episodeRailScrollLefts,
      [playerId]: Math.round(scrollLeft),
    },
  });
}

function getEpisode(player: EpisodeAnimePlayer, number: number | null): AnimeEpisode | null {
  if (player.episodes.length === 0) {
    return null;
  }

  return player.episodes.find((episode) => episode.number === number) ?? player.episodes[0];
}

function resolveSelection(players: AnimePlayer[], savedState: SavedPlayerState | null): Selection | null {
  const player = players.find((item) => item.id === savedState?.playerId) ?? players[0];
  if (!player) {
    return null;
  }

  return {
    player,
    episode: player.type === "episodes" ? getEpisode(player, savedState?.episodes[player.id] ?? null) : null,
  };
}

function Chevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg aria-hidden="true" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
      {direction === "left" ? <path d="m15 18-6-6 6-6" /> : <path d="m9 18 6-6-6-6" />}
    </svg>
  );
}

function EpisodeNavigation({ direction, disabled, isHidden, onClick }: EpisodeNavigationProps) {
  const isPrevious = direction === "previous";
  const label = isPrevious ? "Предыдущая" : "Следующая";

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      tabIndex={isHidden ? -1 : undefined}
      className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg border border-transparent px-3 text-sm text-zinc-400 transition-[background-color,border-color,color,box-shadow] duration-200 hover:border-zinc-700/70 hover:bg-zinc-800/70 hover:text-zinc-50 hover:shadow-[0_2px_12px_rgba(0,0,0,0.35)] focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:outline-none disabled:cursor-default disabled:border-transparent disabled:bg-transparent disabled:text-zinc-700 disabled:shadow-none disabled:hover:border-transparent disabled:hover:bg-transparent disabled:hover:text-zinc-700 disabled:hover:shadow-none sm:px-4"
    >
      {isPrevious && <Chevron direction="left" />}
      <span className="hidden sm:inline">{label}</span>
      <span className="sr-only sm:hidden">{label} серия</span>
      {!isPrevious && <Chevron direction="right" />}
    </button>
  );
}

export function PlayerSwitcher({ animeSlug, players }: PlayerSwitcherProps) {
  const [initialSelection] = useState(() => resolveSelection(players, readSavedState(animeSlug)));
  const [activePlayerId, setActivePlayerId] = useState(initialSelection?.player.id ?? "");
  const [activeEpisodeNumber, setActiveEpisodeNumber] = useState(initialSelection?.episode?.number ?? null);
  const [renderedSource, setRenderedSource] = useState(() => {
    if (!initialSelection) {
      return null;
    }
    return initialSelection.episode?.src ?? (initialSelection.player.type === "single" ? initialSelection.player.src : null);
  });
  const [isEpisodeRailOpen, setEpisodeRailOpen] = useState(false);
  const [railScrollMetrics, setRailScrollMetrics] = useState<RailScrollMetrics>({
    scrollLeft: 0,
    scrollWidth: 0,
    clientWidth: 0,
  });
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const episodeToggleRef = useRef<HTMLButtonElement | null>(null);
  const episodeControlsRef = useRef<HTMLDivElement | null>(null);
  const episodeRailRef = useRef<HTMLDivElement | null>(null);
  const scrollSaveTimeoutRef = useRef<number | null>(null);

  const activePlayer = players.find((player) => player.id === activePlayerId) ?? players[0];
  const episodePlayer = activePlayer?.type === "episodes" ? activePlayer : null;
  const activeEpisode = episodePlayer ? getEpisode(episodePlayer, activeEpisodeNumber) : null;
  const activeSource = activeEpisode?.src ?? (activePlayer?.type === "single" ? activePlayer.src : null);
  const activeEpisodeIndex = episodePlayer && activeEpisode ? episodePlayer.episodes.findIndex((episode) => episode.number === activeEpisode.number) : -1;
  const canGoPrevious = activeEpisodeIndex > 0;
  const canGoNext = episodePlayer !== null && activeEpisodeIndex < episodePlayer.episodes.length - 1;

  useEffect(() => {
    return () => {
      if (scrollSaveTimeoutRef.current !== null) {
        window.clearTimeout(scrollSaveTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!isEpisodeRailOpen || !episodePlayer) {
      return;
    }

    const rail = episodeRailRef.current;
    if (!rail) {
      return;
    }

    const savedScrollLeft = readSavedState(animeSlug)?.episodeRailScrollLefts[episodePlayer.id];
    const frame = window.requestAnimationFrame(() => {
      if (savedScrollLeft !== undefined) {
        rail.scrollTo({ left: savedScrollLeft, behavior: "smooth" });
        return;
      }
      rail.querySelector<HTMLElement>(`[data-episode="${activeEpisode?.number}"]`)?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [activeEpisode?.number, animeSlug, episodePlayer, isEpisodeRailOpen]);

  useEffect(() => {
    if (!isEpisodeRailOpen || !episodePlayer || !episodeRailRef.current) {
      return;
    }

    const rail = episodeRailRef.current;
    const updateMetrics = () => {
      setRailScrollMetrics({
        scrollLeft: rail.scrollLeft,
        scrollWidth: rail.scrollWidth,
        clientWidth: rail.clientWidth,
      });
    };
    const resizeObserver = new ResizeObserver(updateMetrics);
    resizeObserver.observe(rail);
    const frame = window.requestAnimationFrame(updateMetrics);

    return () => {
      window.cancelAnimationFrame(frame);
      resizeObserver.disconnect();
    };
  }, [episodePlayer, isEpisodeRailOpen]);

  useEffect(() => {
    if (!isEpisodeRailOpen || !episodePlayer) {
      return;
    }

    const closeOnOutsideLeftClick = (event: PointerEvent) => {
      if (event.button !== 0 || episodeControlsRef.current?.contains(event.target as Node)) {
        return;
      }

      const scrollLeft = episodeRailRef.current?.scrollLeft;
      if (scrollLeft !== undefined) {
        saveRailPosition(animeSlug, episodePlayer.id, scrollLeft);
      }
      setEpisodeRailOpen(false);
      episodeToggleRef.current?.blur();
    };

    document.addEventListener("pointerdown", closeOnOutsideLeftClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideLeftClick);
  }, [animeSlug, episodePlayer, isEpisodeRailOpen]);

  if (players.length === 0 || !activePlayer || !activeSource) {
    return null;
  }

  function changeSelection(player: AnimePlayer, episodeNumber: number | null = null) {
    if (player.id === activePlayer.id && episodeNumber === null) {
      return;
    }

    const savedEpisodeNumber = player.type === "episodes" ? readSavedState(animeSlug)?.episodes[player.id] ?? null : null;
    const episode = player.type === "episodes" ? getEpisode(player, episodeNumber ?? savedEpisodeNumber) : null;
    const source = episode?.src ?? (player.type === "single" ? player.src : null);

    if (!source) {
      return;
    }

    const selection = { player, episode };

    if (iframeRef.current) {
      iframeRef.current.src = "about:blank";
    }
    setRenderedSource(source);
    setActivePlayerId(player.id);
    setActiveEpisodeNumber(episode?.number ?? null);
    setEpisodeRailOpen(false);
    saveSelection(animeSlug, selection);
  }

  function openEpisodeRail() {
    if (episodePlayer) {
      setEpisodeRailOpen(true);
    }
  }

  function closeEpisodeRail() {
    if (episodePlayer && episodeRailRef.current) {
      saveRailPosition(animeSlug, episodePlayer.id, episodeRailRef.current.scrollLeft);
    }
    setEpisodeRailOpen(false);
  }

  function scheduleRailPositionSave() {
    if (!episodePlayer || !episodeRailRef.current) {
      return;
    }

    if (scrollSaveTimeoutRef.current !== null) {
      window.clearTimeout(scrollSaveTimeoutRef.current);
    }
    const playerId = episodePlayer.id;
    const scrollLeft = episodeRailRef.current.scrollLeft;
    setRailScrollMetrics({
      scrollLeft,
      scrollWidth: episodeRailRef.current.scrollWidth,
      clientWidth: episodeRailRef.current.clientWidth,
    });
    scrollSaveTimeoutRef.current = window.setTimeout(() => {
      saveRailPosition(animeSlug, playerId, scrollLeft);
      scrollSaveTimeoutRef.current = null;
    }, RAIL_SCROLL_SAVE_DEBOUNCE_MS);
  }

  function handleEpisodeRailWheel(event: WheelEvent<HTMLDivElement>) {
    const rail = episodeRailRef.current;
    if (!rail || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
      return;
    }

    event.preventDefault();
    rail.scrollBy({ left: event.deltaY, behavior: "smooth" });
  }

  function setRailScrollFromPointer(event: ReactPointerEvent<HTMLDivElement>) {
    const rail = episodeRailRef.current;
    if (!rail) {
      return;
    }

    const track = event.currentTarget.getBoundingClientRect();
    const maxScrollLeft = Math.max(rail.scrollWidth - rail.clientWidth, 0);
    if (maxScrollLeft === 0 || track.width === 0) {
      return;
    }

    const thumbWidth = Math.max((rail.clientWidth / rail.scrollWidth) * track.width, 24);
    const availableTrackWidth = Math.max(track.width - thumbWidth, 1);
    const nextScrollLeft = ((event.clientX - track.left - thumbWidth / 2) / availableTrackWidth) * maxScrollLeft;
    rail.scrollLeft = Math.min(Math.max(nextScrollLeft, 0), maxScrollLeft);
    scheduleRailPositionSave();
  }

  function handleRailScrollbarPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setRailScrollFromPointer(event);
  }

  function handleRailScrollbarPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      setRailScrollFromPointer(event);
    }
  }

  function handleRailScrollbarKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const rail = episodeRailRef.current;
    if (!rail) {
      return;
    }

    const pageDistance = event.shiftKey ? rail.clientWidth * 0.8 : 48;
    const targets: Record<string, number> = {
      Home: 0,
      End: rail.scrollWidth,
    };
    const target = targets[event.key];
    const direction = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    if (target === undefined && direction === 0) {
      return;
    }

    event.preventDefault();
    if (direction !== 0) {
      rail.scrollBy({ left: direction * pageDistance, behavior: "smooth" });
      return;
    }
    rail.scrollTo({ left: target, behavior: "smooth" });
  }

  const maxRailScrollLeft = Math.max(railScrollMetrics.scrollWidth - railScrollMetrics.clientWidth, 0);
  const railThumbWidth =
    railScrollMetrics.scrollWidth > 0 ? Math.max((railScrollMetrics.clientWidth / railScrollMetrics.scrollWidth) * 100, 8) : 100;
  const railThumbLeft =
    maxRailScrollLeft > 0 ? (railScrollMetrics.scrollLeft / maxRailScrollLeft) * (100 - railThumbWidth) : 0;

  return (
    <section className="mx-auto w-full max-w-5xl" aria-label="Плеер">
      {players.length > 1 && (
        <div className="relative mb-3 flex w-full select-none rounded-xl border border-zinc-800 bg-zinc-950/70 p-1" role="tablist" aria-label="Выбор плеера">
          <div
            className="absolute top-1 bottom-1 rounded-lg bg-zinc-800 transition-transform duration-200 ease-out motion-reduce:transition-none"
            style={{
              width: `calc(${100 / players.length}% - 4px)`,
              transform: `translateX(calc(${players.findIndex((player) => player.id === activePlayer.id) * 100}% + ${players.findIndex((player) => player.id === activePlayer.id) * 4}px))`,
              left: "4px",
            }}
          />
          {players.map((player) => (
            <button
              key={player.id}
              type="button"
              role="tab"
              aria-selected={player.id === activePlayer.id}
              onClick={() => changeSelection(player)}
              className={`z-10 min-h-11 flex-1 rounded-lg px-3 py-2 text-sm font-medium transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:outline-none ${
                player.id === activePlayer.id ? "text-amber-400" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {player.label}
            </button>
          ))}
        </div>
      )}

      <div className="relative w-full overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-950 shadow-2xl shadow-black/40">
        <div className="pb-[56.25%]" />
        {renderedSource ? (
          <iframe
            key={renderedSource}
            ref={iframeRef}
            src={renderedSource}
            title={activeEpisode ? `${activePlayer.label}: серия ${activeEpisode.number}` : activePlayer.label}
            allow="autoplay; fullscreen; picture-in-picture"
            referrerPolicy="strict-origin-when-cross-origin"
            className="player-embed absolute inset-0 h-full w-full"
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-zinc-500" aria-live="polite">
            Загрузка плеера…
          </div>
        )}
      </div>

      {episodePlayer && activeEpisode && (
        <div
          ref={episodeControlsRef}
          className="relative mt-3 h-14 overflow-hidden rounded-xl border border-zinc-800/80 bg-zinc-950/60"
        >
          <div
            className={`absolute inset-0 flex items-center transition-opacity duration-200 ease-out motion-reduce:transition-none ${
              isEpisodeRailOpen ? "pointer-events-none opacity-0" : "opacity-100"
            }`}
            aria-hidden={isEpisodeRailOpen}
          >
            <div className="grid h-full w-full grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 px-2 sm:px-3">
              <div className="flex justify-start">
                <EpisodeNavigation
                  direction="previous"
                  disabled={!canGoPrevious}
                  isHidden={isEpisodeRailOpen}
                  onClick={() => changeSelection(episodePlayer, episodePlayer.episodes[activeEpisodeIndex - 1].number)}
                />
              </div>
              <button
                ref={episodeToggleRef}
                type="button"
                onClick={openEpisodeRail}
                tabIndex={isEpisodeRailOpen ? -1 : undefined}
                className="h-9 min-w-36 rounded-lg px-4 text-[13px] font-medium tracking-[0.015em] text-zinc-200 transition-colors duration-200 hover:bg-zinc-800/70 hover:text-zinc-50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-amber-400 focus-visible:outline-none"
              >
                Серия <span className="font-semibold text-zinc-50">{activeEpisode.number}</span>{" "}
                <span className="font-normal tracking-normal text-zinc-500">из {episodePlayer.episodes.length}</span>
              </button>
              <div className="flex justify-end">
                <EpisodeNavigation
                  direction="next"
                  disabled={!canGoNext}
                  isHidden={isEpisodeRailOpen}
                  onClick={() => changeSelection(episodePlayer, episodePlayer.episodes[activeEpisodeIndex + 1].number)}
                />
              </div>
            </div>
          </div>

          <div
            className={`absolute inset-0 flex flex-col justify-center transition-opacity duration-200 ease-out motion-reduce:transition-none ${
              isEpisodeRailOpen ? "opacity-100" : "pointer-events-none opacity-0"
            }`}
            aria-hidden={!isEpisodeRailOpen}
          >
            <div
              ref={episodeRailRef}
              role="listbox"
              aria-label="Выбор серии"
              onKeyDown={(event) => event.key === "Escape" && closeEpisodeRail()}
              onWheel={handleEpisodeRailWheel}
              onScroll={scheduleRailPositionSave}
              className="episode-rail flex items-center gap-1 overflow-x-auto overflow-y-hidden overscroll-x-contain px-4"
            >
              {episodePlayer.episodes.map((episode) => (
                <button
                  key={episode.number}
                  data-episode={episode.number}
                  type="button"
                  role="option"
                  aria-selected={episode.number === activeEpisode.number}
                  tabIndex={isEpisodeRailOpen ? 0 : -1}
                  onClick={() => changeSelection(episodePlayer, episode.number)}
                  className={`h-9 min-w-11 shrink-0 rounded-md border px-2 text-sm font-medium transition-[background-color,border-color,color,box-shadow] duration-150 focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:outline-none ${
                    episode.number === activeEpisode.number
                      ? "border-amber-400/80 bg-amber-400/10 text-amber-400 shadow-[0_0_12px_rgba(245,158,11,0.1)]"
                      : "border-transparent text-zinc-500 hover:border-zinc-700/90 hover:bg-zinc-900 hover:text-zinc-100"
                  }`}
                >
                  {episode.number}
                </button>
              ))}
            </div>
            <div
              role="slider"
              aria-label="Прокрутка списка серий"
              aria-valuemin={0}
              aria-valuemax={Math.round(maxRailScrollLeft)}
              aria-valuenow={Math.round(railScrollMetrics.scrollLeft)}
              tabIndex={0}
              onPointerDown={handleRailScrollbarPointerDown}
              onPointerMove={handleRailScrollbarPointerMove}
              onKeyDown={handleRailScrollbarKeyDown}
              className="group mx-4 mt-0.5 flex h-4 cursor-pointer touch-none items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-amber-400"
            >
              <div className="relative h-1.5 w-full rounded-full bg-zinc-800/80 transition-colors group-hover:bg-zinc-700/90">
                <div
                  className="absolute top-0 h-full rounded-full bg-gradient-to-r from-amber-600 to-amber-400 shadow-[0_0_8px_rgba(245,158,11,0.38)] transition-[left,width] duration-150"
                  style={{ left: `${railThumbLeft}%`, width: `${railThumbWidth}%` }}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
