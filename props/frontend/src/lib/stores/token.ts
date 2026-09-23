/** Admin token management for the API client and token fallback login. */
import { useSyncExternalStore } from "react";

const STORAGE_KEY = "props_admin_token";
let tokenRequired = !localStorage.getItem(STORAGE_KEY);
let authFailureHandled = false;
const listeners = new Set<() => void>();

function setTokenRequired(value: boolean): void {
  tokenRequired = value;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useNeedsToken(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => tokenRequired,
    () => true
  );
}

export function getToken(): string | null {
  return localStorage.getItem(STORAGE_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(STORAGE_KEY, token);
  setTokenRequired(false);
  authFailureHandled = false;
}

/** Mark the app authenticated through the server-side SSO session. */
export function markSessionAuthenticated(): void {
  setTokenRequired(false);
  authFailureHandled = false;
}

export function clearToken(): void {
  localStorage.removeItem(STORAGE_KEY);
  setTokenRequired(true);
}

/** Capture a token from the URL query and remove it from the visible address. */
export function captureTokenFromUrl(): void {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  if (!token) return;

  setToken(token);
  params.delete("token");
  const newSearch = params.toString();
  const newUrl = window.location.pathname + (newSearch ? `?${newSearch}` : "") + window.location.hash;
  history.replaceState(null, "", newUrl);
}

/** Signal that auth failed (401) so the app returns to token login. */
export function onAuthFailed(): void {
  if (authFailureHandled) return;
  authFailureHandled = true;
  clearToken();
}
