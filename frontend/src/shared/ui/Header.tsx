import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { ANIMES, findAnimeBySlug } from "@/shared/config/animes";
import { useSession } from "@/shared/session/SessionContext";
import { DivergenceMeter } from "@/shared/ui/DivergenceMeter";

const DEFAULT_WORLDLINE = "0.000000";

type TitleMode = "full" | "compact" | "hidden";

export function Header() {
  const { pathname } = useLocation();
  const { user } = useSession();
  const currentAnime = findAnimeBySlug(pathname.replace(/^\/|\/$/g, ""));
  const worldline = currentAnime?.worldline ?? DEFAULT_WORLDLINE;

  const [titleMode, setTitleMode] = useState<TitleMode>("full");
  const [isLinesOpen, setIsLinesOpen] = useState(false);
  const menuContainerRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const fullMeasureRef = useRef<HTMLSpanElement>(null);
  const compactMeasureRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    setIsLinesOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!isLinesOpen) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      if (menuContainerRef.current && !menuContainerRef.current.contains(event.target as Node)) {
        setIsLinesOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setIsLinesOpen(false);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isLinesOpen]);

  useLayoutEffect(() => {
    function measure() {
      const button = menuButtonRef.current;
      const fullMeasure = fullMeasureRef.current;
      const compactMeasure = compactMeasureRef.current;
      if (!button || !fullMeasure || !compactMeasure) {
        return;
      }

      const buttonRect = button.getBoundingClientRect();
      const arrowPadding = parseFloat(getComputedStyle(button).paddingRight) || 12;
      const screenCenter = window.innerWidth / 2;

      const fullGap = screenCenter - fullMeasure.offsetWidth / 2 - buttonRect.right;
      const compactGap = screenCenter - compactMeasure.offsetWidth / 2 - buttonRect.right;

      // Возврат к более длинной версии требует запаса больше любого скачка
      // порога на CSS-брейкпоинтах, иначе на границе проскакивает лишний кадр
      const hysteresis = 40;
      setTitleMode((previous) => {
        if (fullGap > arrowPadding + (previous === "full" ? 0 : hysteresis)) {
          return "full";
        }
        if (compactGap > arrowPadding + (previous === "hidden" ? hysteresis : 0)) {
          return "compact";
        }
        return "hidden";
      });
    }

    measure();
    window.addEventListener("resize", measure);
    const observer = new ResizeObserver(measure);
    if (menuButtonRef.current) {
      observer.observe(menuButtonRef.current);
    }
    return () => {
      window.removeEventListener("resize", measure);
      observer.disconnect();
    };
  }, []);

  return (
    <header className="fixed top-0 right-0 left-0 z-50 border-b border-zinc-800/70 bg-zinc-950/80 backdrop-blur-md">
      <div className="relative mx-auto flex max-w-6xl items-center justify-between gap-1 px-2 py-2.5 md:gap-3 md:px-6 md:py-3">
        <div className="flex shrink-0 items-center gap-1 md:gap-2">
          <DivergenceMeter value={worldline} />

          <div ref={menuContainerRef} className="group relative">
            <button
              ref={menuButtonRef}
              type="button"
              aria-expanded={isLinesOpen}
              aria-haspopup="true"
              onClick={() => setIsLinesOpen((previous) => !previous)}
              className="flex cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1.5 text-zinc-400 transition-colors hover:text-amber-400 md:px-3"
            >
              <span className="font-mono text-xs tracking-widest uppercase md:text-sm">Линии</span>
              <svg
                className={`h-2.5 w-2.5 opacity-60 transition-transform duration-200 md:h-3 md:w-3 ${
                  isLinesOpen ? "rotate-180" : ""
                } md:group-hover:rotate-180`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            <ul
              className={`absolute left-0 mt-2 w-52 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950/95 shadow-2xl shadow-black/50 backdrop-blur-md transition-all duration-200 md:w-60 ${
                isLinesOpen ? "visible opacity-100" : "invisible opacity-0"
              } md:group-hover:visible md:group-hover:opacity-100`}
            >
              {ANIMES.map((anime) => (
                <li key={anime.slug}>
                  <Link
                    to={`/${anime.slug}`}
                    onClick={() => setIsLinesOpen(false)}
                    className={`flex items-center justify-between px-4 py-2.5 transition-colors hover:bg-zinc-900 ${
                      currentAnime?.slug === anime.slug ? "bg-zinc-900/60" : ""
                    }`}
                  >
                    <span className="font-mono text-xs text-amber-500 md:text-sm">
                      {anime.worldline}
                    </span>
                    <span className="text-xs text-zinc-400 md:text-sm">{anime.menuLabel}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <Link
          to="/future-gadget-laboratory"
          aria-hidden={titleMode !== "full"}
          tabIndex={titleMode === "full" ? 0 : -1}
          className={`fixed left-1/2 -translate-x-1/2 text-sm font-semibold tracking-[0.25em] whitespace-nowrap text-zinc-200 uppercase transition-opacity duration-300 hover:text-amber-400 lg:text-base ${
            titleMode === "full" ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
        >
          Future Gadget Laboratory
        </Link>

        <Link
          to="/future-gadget-laboratory"
          aria-hidden={titleMode !== "compact"}
          tabIndex={titleMode === "compact" ? 0 : -1}
          className={`fixed left-1/2 -translate-x-1/2 text-xs font-semibold tracking-widest whitespace-nowrap text-zinc-200 uppercase transition-opacity duration-300 hover:text-amber-400 ${
            titleMode === "compact" ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
        >
          Laboratory
        </Link>

        <span
          ref={fullMeasureRef}
          aria-hidden
          className="invisible absolute text-base font-semibold tracking-[0.25em] whitespace-nowrap uppercase"
        >
          Future Gadget Laboratory
        </span>
        <span
          ref={compactMeasureRef}
          aria-hidden
          className="invisible absolute text-xs font-semibold tracking-widest whitespace-nowrap uppercase"
        >
          Laboratory
        </span>

        <Link
          to={user ? "/profile" : "/register"}
          className="shrink-0 rounded-lg px-1 py-1 transition-transform hover:scale-105 md:px-3"
        >
          <img
            src="/img/register_bin.webp"
            alt={user ? "Профиль" : "Регистрация"}
            className="h-11 w-auto md:h-12"
          />
        </Link>
      </div>
    </header>
  );
}
