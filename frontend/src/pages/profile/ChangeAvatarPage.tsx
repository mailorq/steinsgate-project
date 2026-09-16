import { useEffect, useState } from "react";
import type { ChangeEvent } from "react";
import Cropper from "react-easy-crop";
import type { Area, Point } from "react-easy-crop";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { cropImageToFile } from "@/features/avatar/cropImage";
import { ApiError, profileApi } from "@/shared/api";
import { useSession } from "@/shared/session/sessionContext";
import { Avatar } from "@/shared/ui/Avatar";
import { FormCard } from "@/shared/ui/FormCard";

const ALLOWED_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp"];
const MAX_SOURCE_SIZE = 20 * 1024 * 1024;

export function ChangeAvatarPage() {
  const navigate = useNavigate();
  const { user, isLoading, setUser } = useSession();
  const [imageSrc, setImageSrc] = useState<string | null>(null);
  const [crop, setCrop] = useState<Point>({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [croppedArea, setCroppedArea] = useState<Area | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    document.title = "Change Avatar";
  }, []);

  if (isLoading) {
    return null;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  function resetCropper() {
    setImageSrc(null);
    setCroppedArea(null);
    setCrop({ x: 0, y: 0 });
    setZoom(1);
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] ?? null;
    event.target.value = "";
    setError(null);

    if (!selected) {
      return;
    }
    if (selected.size > MAX_SOURCE_SIZE) {
      setError("Файл слишком большой. Максимум 20 МБ");
      return;
    }
    const extension = selected.name.slice(selected.name.lastIndexOf(".")).toLowerCase();
    if (!ALLOWED_EXTENSIONS.includes(extension)) {
      setError("Допустимые форматы: JPG, PNG, GIF, WEBP");
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      setCrop({ x: 0, y: 0 });
      setZoom(1);
      setImageSrc(reader.result as string);
    };
    reader.readAsDataURL(selected);
  }

  async function handleUpload() {
    if (!imageSrc || !croppedArea) {
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      const file = await cropImageToFile(imageSrc, croppedArea);
      const updated = await profileApi.uploadAvatar(file);
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
    <FormCard title="Change Avatar" maxWidthClass="max-w-md">
      <p className="mb-6 text-center text-sm leading-relaxed text-zinc-500">
        Update your Lab Member ID photo. Make it count.
      </p>

      {imageSrc ? (
        <div className="flex flex-col gap-5">
          <div className="relative h-72 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900">
            <Cropper
              image={imageSrc}
              crop={crop}
              zoom={zoom}
              aspect={1}
              cropShape="round"
              showGrid={false}
              onCropChange={setCrop}
              onZoomChange={setZoom}
              onCropComplete={(_, areaPixels) => setCroppedArea(areaPixels)}
            />
          </div>

          <label className="flex items-center gap-3 text-xs tracking-widest text-zinc-400 uppercase">
            Масштаб
            <input
              type="range"
              min={1}
              max={3}
              step={0.05}
              value={zoom}
              onChange={(e) => setZoom(Number(e.target.value))}
              className="flex-1 accent-amber-500"
            />
          </label>

          {error && <div className="text-center text-sm text-red-400">{error}</div>}

          <div className="flex gap-3">
            <button
              type="button"
              onClick={resetCropper}
              disabled={isSubmitting}
              className="flex-1 rounded-lg border border-zinc-700 px-5 py-3 text-sm font-medium text-zinc-300 transition-colors hover:border-zinc-500 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Отмена
            </button>
            <button
              type="button"
              onClick={handleUpload}
              disabled={isSubmitting || !croppedArea}
              className="flex-1 rounded-lg bg-amber-500 px-5 py-3 text-sm font-semibold text-zinc-950 transition-all duration-200 hover:bg-amber-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
            >
              Сохранить
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-6">
          <div className="flex justify-center">
            <Avatar
              avatarUrl={user.avatar_url}
              username={user.username}
              sizeClass="w-32 h-32"
              textSizeClass="text-5xl"
            />
          </div>

          <div className="w-full">
            <label
              className="mb-2 block text-xs font-medium tracking-widest text-zinc-400 uppercase"
              htmlFor="avatar"
            >
              Выберите изображение
            </label>
            <input
              type="file"
              id="avatar"
              accept="image/*"
              onChange={handleFileChange}
              className="w-full rounded-lg border border-zinc-800 bg-zinc-900/80 px-4 py-3 text-sm text-zinc-300 transition-colors file:mr-4 file:cursor-pointer file:rounded-lg file:border-0 file:bg-zinc-800 file:px-4 file:py-2 file:text-sm file:font-medium file:text-zinc-200 hover:border-zinc-700 hover:file:bg-zinc-700 focus:border-amber-500/70 focus:outline-none"
            />
            {error && <div className="mt-1 text-sm text-red-400">{error}</div>}
          </div>
        </div>
      )}

      <div className="mt-6 text-center">
        <Link to="/profile" className="text-sm text-amber-400 transition-colors hover:text-amber-300">
          Назад к профилю
        </Link>
      </div>
    </FormCard>
  );
}
