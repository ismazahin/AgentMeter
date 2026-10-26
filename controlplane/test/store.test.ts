// The Supabase Storage client (REST, service-role key) against a fake fetch: the exact requests
// it sends, and how a missing object reads (null, never an error).
import { describe, expect, it } from "vitest";
import { MemoryStore, SupabaseStore } from "../../supabase/functions/agentmeter/src/store.ts";

function fake(responses: Response[]) {
  const calls: { url: string; init: RequestInit }[] = [];
  const f = (async (url: string, init: RequestInit = {}) => { calls.push({ url, init }); return responses.shift()!; }) as unknown as typeof fetch;
  return { calls, f };
}

describe("Supabase Storage client", () => {
  it("put / get / delete / size use the bucket's REST paths with the service key", async () => {
    const { calls, f } = fake([new Response("{}"), new Response("hello", { headers: { "content-length": "5" } }),
                               new Response("[]"), new Response(JSON.stringify({ size: 5 }))]);
    const s = new SupabaseStore("https://ref.supabase.co/", "service-key", "agentmeter", f);
    await s.put("sessions/job_1/report.pdf", new Uint8Array([1, 2]), "application/pdf");
    expect(calls[0].url).toBe("https://ref.supabase.co/storage/v1/object/agentmeter/sessions/job_1/report.pdf");
    expect(calls[0].init).toMatchObject({ method: "POST", headers: { Authorization: "Bearer service-key", apikey: "service-key", "content-type": "application/pdf", "x-upsert": "true" } });
    const o = await s.get("sessions/job_1/report.pdf");
    expect([o!.size, await o!.text()]).toEqual([5, "hello"]);
    await s.delete(["a/b", "c"]);
    expect(calls[2]).toMatchObject({ url: "https://ref.supabase.co/storage/v1/object/agentmeter", init: { method: "DELETE", body: JSON.stringify({ prefixes: ["a/b", "c"] }) } });
    expect(await s.size("sessions/job_1/report.pdf")).toBe(5);
    expect(calls[3].url).toBe("https://ref.supabase.co/storage/v1/object/info/agentmeter/sessions/job_1/report.pdf");
  });

  it("a missing object is null; other errors throw; signed upload URLs are absolute", async () => {
    const { f } = fake([new Response('{"statusCode":"404","error":"not_found"}', { status: 400 }), new Response("boom", { status: 500 }),
                        new Response(JSON.stringify({ url: "/object/upload/sign/agentmeter/k?token=abc" }))]);
    const s = new SupabaseStore("https://ref.supabase.co", "k", "agentmeter", f);
    expect(await s.get("nope")).toBeNull();
    await expect(s.get("x")).rejects.toThrow("storage get 500");
    expect(await s.signedUploadUrl("k")).toBe("https://ref.supabase.co/storage/v1/object/upload/sign/agentmeter/k?token=abc");
  });

  it("the in-memory store (tests, dev server) accepts one upload per signed token", async () => {
    const m = new MemoryStore("http://127.0.0.1:9");
    const u = (await m.signedUploadUrl("sessions/x/flows.json"))!;
    const token = u.split("/upload/")[1];
    expect(await m.acceptUpload(token, new TextEncoder().encode("[1]"), "application/json")).toBe(true);
    expect(await m.acceptUpload(token, new Uint8Array(), "application/json")).toBe(false);          // used once
    expect(await (await m.get("sessions/x/flows.json"))!.json()).toEqual([1]);
    expect(await m.size("sessions/x/flows.json")).toBe(3);
  });
});
