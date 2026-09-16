import { request } from "./client";
import type { components } from "./types.gen";

type Schemas = components["schemas"];

export type UserOut = Schemas["UserOut"];
export type SessionOut = Schemas["SessionOut"];
export type MessageOut = Schemas["MessageOut"];
export type VerificationDeliveryOut = Schemas["VerificationDeliveryOut"];
export type AnimeStatsOut = Schemas["AnimeStatsOut"];
export type RatingOut = Schemas["RatingOut"];
export type CommentOut = Schemas["CommentOut"];
export type CommentPageOut = Schemas["CommentPageOut"];
export type ReactionOut = Schemas["ReactionOut"];
export type ProgressOut = Schemas["ProgressOut"];

export { ApiError } from "./client";

export const authApi = {
  session: (signal?: AbortSignal) => request<SessionOut>("/auth/session", { signal }),
  register: (payload: { username: string; email: string; password: string }) =>
    request<VerificationDeliveryOut>("/auth/register", { method: "POST", json: payload }),
  resendVerification: () =>
    request<VerificationDeliveryOut>("/auth/resend-verification", { method: "POST" }),
  verifyEmail: (code: string) =>
    request<SessionOut>("/auth/verify-email", { method: "POST", json: { code } }),
  login: (payload: { username: string; password: string }) =>
    request<SessionOut>("/auth/login", { method: "POST", json: payload }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
};

export const profileApi = {
  updateNickname: (nickname: string) =>
    request<UserOut>("/profile", { method: "PATCH", json: { nickname } }),
  uploadAvatar: (file: File) => {
    const form = new FormData();
    form.append("avatar", file);
    return request<UserOut>("/profile/avatar", { method: "POST", form });
  },
};

export const catalogApi = {
  stats: (slug: string, signal?: AbortSignal) =>
    request<AnimeStatsOut>(`/anime/${slug}`, { signal }),
  registerView: (slug: string) => request<void>(`/anime/${slug}/view`, { method: "POST" }),
  rate: (slug: string, rating: number) =>
    request<RatingOut>(`/anime/${slug}/rating`, { method: "POST", json: { rating } }),
};

export const commentsApi = {
  list: (slug: string, page: number, signal?: AbortSignal) =>
    request<CommentPageOut>(`/anime/${slug}/comments?page=${page}`, { signal }),
  create: (slug: string, text: string) =>
    request<CommentOut>(`/anime/${slug}/comments`, { method: "POST", json: { text } }),
  react: (commentId: number, isLike: boolean) =>
    request<ReactionOut>(`/comments/${commentId}/reaction`, {
      method: "POST",
      json: { is_like: isLike },
    }),
  remove: (commentId: number) =>
    request<void>(`/comments/${commentId}`, { method: "DELETE" }),
};

export const watchApi = {
  get: (slug: string, signal?: AbortSignal) =>
    request<ProgressOut>(`/anime/${slug}/progress`, { signal }),
  save: (slug: string, payload: { current_time: number; duration: number }) =>
    request<ProgressOut>(`/anime/${slug}/progress`, { method: "PUT", json: payload }),
};
