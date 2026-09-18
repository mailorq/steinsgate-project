import { useEffect, useRef, useState } from "react";
import type { ClipboardEvent, FormEvent, KeyboardEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError, authApi } from "@/shared/api";
import { useSession } from "@/shared/session/sessionContext";
import { FormCard } from "@/shared/ui/FormCard";

const CODE_LENGTH = 6;

interface VerificationNavigationState {
  resendAvailableIn?: number;
}

function formatCooldown(seconds: number): string {
  if (seconds < 120) {
    return `${seconds} с`;
  }
  if (seconds < 7200) {
    return `${Math.ceil(seconds / 60)} мин`;
  }
  return `${Math.ceil(seconds / 3600)} ч`;
}

export function VerifyEmailPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { setUser } = useSession();
  const [digits, setDigits] = useState<string[]>(Array(CODE_LENGTH).fill(""));
  const [error, setError] = useState<string | null>(null);
  const [resendMessage, setResendMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isResending, setIsResending] = useState(false);
  const navigationState = location.state as VerificationNavigationState | null;
  const [resendCooldown, setResendCooldown] = useState(
    typeof navigationState?.resendAvailableIn === "number"
      ? Math.max(navigationState.resendAvailableIn, 0)
      : 0,
  );
  const inputsRef = useRef<(HTMLInputElement | null)[]>([]);

  const code = digits.join("");
  const isComplete = code.length === CODE_LENGTH && /^\d{6}$/.test(code);

  useEffect(() => {
    document.title = "Email Verification";
    inputsRef.current[0]?.focus();
  }, []);

  useEffect(() => {
    if (resendCooldown <= 0) {
      return;
    }
    const interval = window.setInterval(() => {
      setResendCooldown((remaining) => Math.max(remaining - 1, 0));
    }, 1_000);
    return () => window.clearInterval(interval);
  }, [resendCooldown]);

  function setDigit(index: number, value: string) {
    const digit = value.replace(/\D/g, "").charAt(0) ?? "";
    setDigits((current) => current.map((d, i) => (i === index ? digit : d)));
    if (digit && index < CODE_LENGTH - 1) {
      inputsRef.current[index + 1]?.focus();
    }
  }

  function handleKeyDown(index: number, event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Backspace" && !digits[index] && index > 0) {
      setDigits((current) => current.map((d, i) => (i === index - 1 ? "" : d)));
      inputsRef.current[index - 1]?.focus();
    }
  }

  function handlePaste(event: ClipboardEvent<HTMLInputElement>) {
    event.preventDefault();
    const pasted = event.clipboardData.getData("text").replace(/\D/g, "").slice(0, CODE_LENGTH);
    setDigits(
      Array(CODE_LENGTH)
        .fill("")
        .map((_, i) => pasted[i] ?? ""),
    );
    inputsRef.current[Math.min(pasted.length, CODE_LENGTH - 1)]?.focus();
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!isComplete) {
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      const session = await authApi.verifyEmail(code);
      setUser(session.user ?? null);
      navigate("/steins-gate");
    } catch (requestError) {
      setError(
        requestError instanceof ApiError ? requestError.message : "Не удалось выполнить запрос",
      );
      setDigits(Array(CODE_LENGTH).fill(""));
      inputsRef.current[0]?.focus();
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleResend() {
    if (isResending || resendCooldown > 0) {
      return;
    }

    setIsResending(true);
    setError(null);
    setResendMessage(null);
    try {
      const result = await authApi.resendVerification();
      setResendCooldown(result.resend_available_in);
      setResendMessage("Код отправлен. Проверьте входящие и папку «Спам».");
      setDigits(Array(CODE_LENGTH).fill(""));
      inputsRef.current[0]?.focus();
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.retryAfter) {
        setResendCooldown(requestError.retryAfter);
      }
      setResendMessage(
        requestError instanceof ApiError ? requestError.message : "Не удалось отправить код",
      );
    } finally {
      setIsResending(false);
    }
  }

  return (
    <FormCard title="Email Verification" maxWidthClass="max-w-[30rem]">
      <p className="mb-6 text-center text-sm leading-relaxed text-zinc-500">
        Enter the 6-digit code from the D-Mail. If it does not arrive, request another send below.
      </p>

      {error && <div className="mb-4 text-center text-sm text-red-500">{error}</div>}
      {resendMessage && <div className="mb-4 text-center text-sm text-zinc-400" role="status">{resendMessage}</div>}

      <form onSubmit={handleSubmit} className="flex flex-col gap-6">
        <div className="w-full">
          <label className="mb-3 block text-center text-xs font-medium tracking-widest text-zinc-400 uppercase">Verification Code</label>
          <div className="flex justify-center gap-3">
            {digits.map((digit, index) => (
              <input
                key={index}
                ref={(el) => {
                  inputsRef.current[index] = el;
                }}
                type="text"
                inputMode="numeric"
                maxLength={1}
                autoComplete="off"
                value={digit}
                onChange={(e) => setDigit(index, e.target.value)}
                onKeyDown={(e) => handleKeyDown(index, e)}
                onPaste={handlePaste}
                onFocus={(e) => e.target.select()}
                className="h-14 w-12 rounded-lg border border-zinc-800 bg-zinc-900/80 text-center text-2xl font-semibold text-amber-400 caret-transparent transition-colors focus:border-amber-500/70 focus:outline-none"
              />
            ))}
          </div>
        </div>

        <button
          type="submit"
          disabled={!isComplete || isSubmitting}
          className="w-full rounded-lg bg-amber-500 px-5 py-3.5 text-sm font-semibold text-zinc-950 transition-all duration-200 hover:bg-amber-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          Verify
        </button>
      </form>

      <div className="mt-6 text-center">
        <button
          type="button"
          disabled={isResending || resendCooldown > 0}
          onClick={handleResend}
          className="text-sm text-amber-400 transition-colors hover:text-amber-300 disabled:cursor-not-allowed disabled:text-zinc-600"
        >
          {isResending
            ? "Отправляем…"
            : resendCooldown > 0
              ? `Повторить через ${formatCooldown(resendCooldown)}`
              : "Отправить код повторно"}
        </button>
      </div>

      <div className="mt-6 text-center">
        <Link to="/register" className="text-sm text-amber-400 transition-colors hover:text-amber-300">
          Back to Registration
        </Link>
      </div>
    </FormCard>
  );
}
