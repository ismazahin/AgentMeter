// Large files of a session (session_results.json over the inline limit, report.pdf, manifest.json,
// the prepared set's features.csv / labels.csv, flows.json): Supabase Storage, private bucket.
// The routes use get / put / delete by key; this file has the Supabase Storage client (REST,
// service-role key: server-side only) and an in-memory store for the tests and the dev server.

export interface StoredObject {
  body: ReadableStream<Uint8Array>;
  size: number | null;
  text(): Promise<string>;
  json<T = unknown>(): Promise<T>;
}

export interface ObjectStore {
  get(key: string): Promise<StoredObject | null>;
  put(key: string, body: Uint8Array | string, contentType: string): Promise<void>;
  delete(keys: string[]): Promise<void>;
  /** A URL the GPU backend PUTs one file to directly (it never holds a Storage key); null = not supported. */
  signedUploadUrl(key: string): Promise<string | null>;
  /** Size of a stored object, or null when it is not there. */
  size(key: string): Promise<number | null>;
}

function fromResponse(r: Response): StoredObject {
  const len = r.headers.get("content-length");
  return { body: r.body!, size: len ? Number(len) : null, text: () => r.text(), json: () => r.json() };
}

export class SupabaseStore implements ObjectStore {
  constructor(private url: string, private serviceKey: string, private bucket: string, private f: typeof fetch = fetch) {
    this.url = url.replace(/\/+$/, "");
  }

  private h(extra: Record<string, string> = {}) {
    return { Authorization: `Bearer ${this.serviceKey}`, apikey: this.serviceKey, ...extra };
  }

  private obj(key: string) {
    return `${this.url}/storage/v1/object/${this.bucket}/${key.split("/").map(encodeURIComponent).join("/")}`;
  }

  async get(key: string): Promise<StoredObject | null> {
    const r = await this.f(this.obj(key), { headers: this.h() });
    if (r.status === 404 || r.status === 400) { await r.body?.cancel(); return null; }   // Storage says 400 "not_found"
    if (!r.ok) throw new Error(`storage get ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return fromResponse(r);
  }

  async put(key: string, body: Uint8Array | string, contentType: string): Promise<void> {
    const r = await this.f(this.obj(key), { method: "POST", headers: this.h({ "content-type": contentType, "x-upsert": "true" }), body: body as BodyInit });
    if (!r.ok) throw new Error(`storage put ${r.status}: ${(await r.text()).slice(0, 200)}`);
    await r.body?.cancel();
  }

  async delete(keys: string[]): Promise<void> {
    if (!keys.length) return;
    const r = await this.f(`${this.url}/storage/v1/object/${this.bucket}`, {
      method: "DELETE", headers: this.h({ "content-type": "application/json" }), body: JSON.stringify({ prefixes: keys }) });
    if (!r.ok) throw new Error(`storage delete ${r.status}: ${(await r.text()).slice(0, 200)}`);
    await r.body?.cancel();
  }

  async signedUploadUrl(key: string): Promise<string | null> {
    const path = key.split("/").map(encodeURIComponent).join("/");
    const r = await this.f(`${this.url}/storage/v1/object/upload/sign/${this.bucket}/${path}`, {
      method: "POST", headers: this.h({ "content-type": "application/json", "x-upsert": "true" }), body: "{}" });
    if (!r.ok) throw new Error(`storage sign ${r.status}: ${(await r.text()).slice(0, 200)}`);
    const d = await r.json() as { url?: string };
    if (!d.url) throw new Error("storage sign: no url");
    return `${this.url}/storage/v1${d.url}`;
  }

  async size(key: string): Promise<number | null> {
    const r = await this.f(`${this.url}/storage/v1/object/info/${this.bucket}/${key.split("/").map(encodeURIComponent).join("/")}`, { headers: this.h() });
    if (!r.ok) { await r.body?.cancel(); return null; }
    const d = await r.json() as { size?: number; metadata?: { size?: number } };
    return d.size ?? d.metadata?.size ?? null;
  }
}

/** In memory (tests; the dev server). `uploadBase` makes signed upload URLs that the dev server serves. */
export class MemoryStore implements ObjectStore {
  readonly objects = new Map<string, { bytes: Uint8Array; type: string }>();
  readonly tokens = new Map<string, string>();
  constructor(private uploadBase: string | null = null) {}

  async get(key: string): Promise<StoredObject | null> {
    const o = this.objects.get(key);
    if (!o) return null;
    const r = new Response(o.bytes.slice());
    return fromResponse(new Response(r.body, { headers: { "content-length": String(o.bytes.length) } }));
  }

  async put(key: string, body: Uint8Array | string, contentType: string): Promise<void> {
    this.objects.set(key, { bytes: typeof body === "string" ? new TextEncoder().encode(body) : body.slice(), type: contentType });
  }

  async delete(keys: string[]): Promise<void> { for (const k of keys) this.objects.delete(k); }

  async signedUploadUrl(key: string): Promise<string | null> {
    if (!this.uploadBase) return null;
    const token = crypto.randomUUID();
    this.tokens.set(token, key);
    return `${this.uploadBase}/upload/${token}`;
  }

  /** The dev server's PUT /upload/<token> handler. */
  async acceptUpload(token: string, bytes: Uint8Array, contentType: string): Promise<boolean> {
    const key = this.tokens.get(token);
    if (!key) return false;
    this.tokens.delete(token);
    await this.put(key, bytes, contentType);
    return true;
  }

  async size(key: string): Promise<number | null> { return this.objects.get(key)?.bytes.length ?? null; }
}
