// Web Crypto primitives: base64url, HMAC tokens, PBKDF2 password hashing with a pepper.
//
// Token format (access, run): "v1.<base64url(JSON payload)>.<base64url(HMAC-SHA256)>", the MAC
// computed over "v1.<payload>". Access tokens use ACCESS_TOKEN_SECRET (Worker only); run tokens
// use RUN_TOKEN_SECRET, shared with the GPU backend, which verifies them the same way
// (agentmeter/server/runtoken.py). Refresh tokens are opaque random bytes, stored hashed.
//
// Passwords: PBKDF2-SHA256(HMAC-SHA256(PASSWORD_PEPPER, password), 16-byte salt, iterations).
// Iterations default to 40,000 — measured at ~6 ms in workerd, inside the 10 ms CPU budget of
// the Workers free plan (100,000 measured ~15-20 ms). OWASP recommends 600,000 for
// PBKDF2-SHA256; the gap is covered by the secret pepper (a stolen database alone cannot be
// brute-forced) and the login throttle. See docs/DEPLOY.md.
const enc = new TextEncoder();
const dec = new TextDecoder();

export function b64url(bytes: ArrayBuffer | Uint8Array): string {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64url(s: string): Uint8Array {
  const p = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(p);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function randomToken(bytes = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export function randomHex(bytes = 16): string {
  return [...crypto.getRandomValues(new Uint8Array(bytes))].map((x) => x.toString(16).padStart(2, "0")).join("");
}

export async function sha256Hex(data: string | ArrayBuffer | Uint8Array): Promise<string> {
  const buf = typeof data === "string" ? enc.encode(data) : data;
  const h = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

const keyCache = new Map<string, Promise<CryptoKey>>();
function hmacKey(secret: string): Promise<CryptoKey> {
  let k = keyCache.get(secret);
  if (!k) {
    k = crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
    keyCache.set(secret, k);
  }
  return k;
}

export async function hmac(secret: string, data: string | Uint8Array): Promise<Uint8Array> {
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), typeof data === "string" ? enc.encode(data) : data);
  return new Uint8Array(sig);
}

export async function hmacHex(secret: string, data: string): Promise<string> {
  return [...(await hmac(secret, data))].map((x) => x.toString(16).padStart(2, "0")).join("");
}

export function timingSafeEqual(a: Uint8Array | string, b: Uint8Array | string): boolean {
  const x = typeof a === "string" ? enc.encode(a) : a;
  const y = typeof b === "string" ? enc.encode(b) : b;
  if (x.length !== y.length) return false;
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

export async function signToken(secret: string, payload: Record<string, unknown>): Promise<string> {
  const body = "v1." + b64url(enc.encode(JSON.stringify(payload)));
  return body + "." + b64url(await hmac(secret, body));
}

/** Verify signature + expiry; returns the payload or null. */
export async function verifyToken<T extends Record<string, unknown>>(secret: string, token: string, typ: string): Promise<T | null> {
  const parts = (token || "").split(".");
  if (parts.length !== 3 || parts[0] !== "v1") return null;
  const body = parts[0] + "." + parts[1];
  let mac: Uint8Array;
  try {
    mac = unb64url(parts[2]);
  } catch {
    return null;
  }
  if (!timingSafeEqual(mac, await hmac(secret, body))) return null;
  let payload: T;
  try {
    payload = JSON.parse(dec.decode(unb64url(parts[1]))) as T;
  } catch {
    return null;
  }
  if (payload.typ !== typ) return null;
  if (typeof payload.exp !== "number" || payload.exp < Math.floor(Date.now() / 1000)) return null;
  return payload;
}

export async function hashPassword(password: string, pepper: string, iterations: number, saltB64?: string) {
  const salt = saltB64 ? unb64url(saltB64) : crypto.getRandomValues(new Uint8Array(16));
  const peppered = await hmac(pepper, password);
  const key = await crypto.subtle.importKey("raw", peppered, "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, 256);
  return { hash: b64url(bits), salt: b64url(salt), iterations };
}

export async function verifyPassword(password: string, pepper: string, user: { pw_hash: string; pw_salt: string; pw_iter: number }) {
  const { hash } = await hashPassword(password, pepper, user.pw_iter, user.pw_salt);
  return timingSafeEqual(hash, user.pw_hash);
}
