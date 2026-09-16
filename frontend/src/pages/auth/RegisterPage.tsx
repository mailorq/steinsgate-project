import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, authApi } from "@/shared/api";
import { FormCard } from "@/shared/ui/FormCard";
import { TextField } from "@/shared/ui/TextField";

interface RegisterErrors {
  username?: string;
  email?: string;
  password?: string;
  confirmPassword?: string;
}

export function RegisterPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [errors, setErrors] = useState<RegisterErrors>({});
  const [serverError, setServerError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    document.title = "Registration";
  }, []);

  function validate(): RegisterErrors {
    const next: RegisterErrors = {};
    if (!username.trim()) {
      next.username = "Введите логин";
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      next.email = "Введите корректный email";
    }
    if (password.length < 8) {
      next.password = "Пароль должен быть не короче 8 символов";
    }
    if (password !== confirmPassword) {
      next.confirmPassword = "Пароли не совпадают";
    }
    return next;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const next = validate();
    setErrors(next);
    if (Object.keys(next).length > 0) {
      return;
    }

    setIsSubmitting(true);
    setServerError(null);
    try {
      const result = await authApi.register({ username: username.trim(), email, password });
      navigate("/verify-email", { state: { resendAvailableIn: result.resend_available_in } });
    } catch (error) {
      setServerError(error instanceof ApiError ? error.message : "Не удалось выполнить запрос");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <FormCard
      title="Registration"
      subtitle="The Future Gadget Laboratory needs you. Rise and become a Lab Member!"
    >
      <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-6">
        {serverError && <div className="text-center text-sm text-red-500">{serverError}</div>}

        <TextField
          id="username"
          label="Login"
          type="text"
          placeholder="Enter login"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          error={errors.username}
        />
        <TextField
          id="email"
          label="Email"
          type="email"
          placeholder="Enter email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={errors.email}
        />
        <TextField
          id="password"
          label="Password"
          type="password"
          placeholder="Enter your password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={errors.password}
        />
        <TextField
          id="confirm-password"
          label="Confirm Password"
          type="password"
          placeholder="Repeat the password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          error={errors.confirmPassword}
        />

        <button
          type="submit"
          disabled={isSubmitting}
          className="w-full rounded-lg bg-amber-500 px-5 py-3.5 text-sm font-semibold text-zinc-950 transition-all duration-200 hover:bg-amber-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
        >
          Register
        </button>
      </form>

      <div className="mt-8 text-center">
        <p className="text-sm text-zinc-500">
          What? You’re already a Lab Member? Then step this way!{" "}
          <Link to="/login" className="text-amber-400 transition-colors hover:text-amber-300">
            Sign in
          </Link>
        </p>
      </div>
    </FormCard>
  );
}
