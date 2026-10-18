// Model catalog state (admin): enabled / download at start-up / added models / 4-bit verification.
import { describe, expect, it } from "vitest";
import { api, backendCall, register, user } from "./helpers";

const M = "Qwen/Qwen2.5-3B-Instruct";

describe("model catalog state", () => {
  it("only admins read and change it", async () => {
    const u = await user();
    expect((await api("/api/admin/models", { token: u.token })).status).toBe(403);
    expect((await api("/api/admin/models", { method: "PUT", body: {}, token: u.token })).status).toBe(403);
  });

  it("admin enables/disables, toggles download at start-up, adds and removes models", async () => {
    const a = await user("admin");
    let r = await api("/api/admin/models", { method: "PUT", token: a.token, body: { set: { [M]: { enabled: false, download_at_start: true } } } });
    expect(r.status).toBe(200);
    expect(r.data.state.models[M]).toEqual({ enabled: false, download_at_start: true });
    r = await api("/api/admin/models", { method: "PUT", token: a.token, body: { add: [{ id: "ibm-granite/granite-3.3-8b-instruct", note: "newer Granite" }] } });
    expect(r.data.state.added).toEqual([{ id: "ibm-granite/granite-3.3-8b-instruct", note: "newer Granite" }]);
    expect(r.data.state.models["ibm-granite/granite-3.3-8b-instruct"].enabled).toBe(false);          // added: off until enabled
    expect((await api("/api/admin/models", { method: "PUT", token: a.token, body: { add: [{ id: "TheBloke/Llama-2-7B-GPTQ" }] } })).data.code).toBe("bad_model");
    expect((await api("/api/admin/models", { method: "PUT", token: a.token, body: { add: [{ id: "not a model" }] } })).data.code).toBe("bad_model");
    expect((await api("/api/admin/models", { method: "PUT", token: a.token, body: { set: { [M]: { enabled: "yes" } } } })).status).toBe(400);
    expect((await api("/api/admin/models", { method: "PUT", token: a.token, body: { remove: [M] } })).data.code).toBe("not_added");   // listed in the YAML: disable instead
    r = await api("/api/admin/models", { method: "PUT", token: a.token, body: { remove: ["ibm-granite/granite-3.3-8b-instruct"] } });
    expect(r.data.state.added).toEqual([]);
    expect((await api("/api/admin/models", { token: a.token })).data.state.models[M].download_at_start).toBe(true);
  });

  it("the backend receives it on register/heartbeat and reports verification results (signed)", async () => {
    const a = await user("admin");
    await api("/api/admin/models", { method: "PUT", token: a.token, body: { set: { [M]: { enabled: true } } } });
    const reg = await register();
    expect(reg.data.model_state.models[M].enabled).toBe(true);
    const hb = await backendCall("POST", "/api/backend/heartbeat", { jobs: [] });
    expect(hb.data.model_state?.models?.[M]?.enabled, JSON.stringify(hb.data)).toBe(true);
    expect((await backendCall("GET", "/api/backend/model-state", null, { raw: "" })).data.state.models[M].enabled).toBe(true);
    expect((await api("/api/backend/models/verified", { body: { model: M, result: { status: "ok" } } })).status).toBe(401);   // unsigned
    const v = await backendCall("POST", "/api/backend/models/verified",
      { model: M, result: { status: "ok", transformers: "4.46.0", gpu: "NVIDIA L4", weights_mb: 2100.5, seconds: 41, by: "isma" } });
    expect(v.status).toBe(200);
    const st = (await api("/api/admin/models", { token: a.token })).data.state.models[M];
    expect(st.enabled).toBe(true);
    expect(st.verified).toMatchObject({ status: "ok", transformers: "4.46.0", gpu: "NVIDIA L4", weights_mb: 2100.5, by: "isma" });
    expect((await backendCall("POST", "/api/backend/models/verified", { model: M, result: { status: "maybe" } })).status).toBe(400);
  });

  it("verify run tokens are admin only and name the model", async () => {
    await register();
    const u = await user(), a = await user("admin");
    expect((await api("/api/runs/authorize", { token: u.token, body: { kind: "verify", model: M } })).status).toBe(403);
    expect((await api("/api/runs/authorize", { token: a.token, body: { kind: "verify" } })).status).toBe(400);
    const r = await api("/api/runs/authorize", { token: a.token, body: { kind: "verify", model: M } });
    expect(r.status).toBe(200);
    const payload = JSON.parse(atob(r.data.token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    expect(payload).toMatchObject({ kind: "verify", model: M, admin: true });
  });
});
