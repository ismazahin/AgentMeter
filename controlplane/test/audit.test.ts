// The audit log: admin actions, session deletions and password changes are recorded (who, what,
// on what, when), never with a password; only admins can read it.
import { describe, expect, it } from "vitest";
import { api, user, uname, PW } from "./helpers.ts";

describe("audit log", () => {
  it("records admin actions without secrets and shows them to admins only, newest first", async () => {
    const admin = await user("admin"), plain = await user();
    const name = uname("x");
    const c = await api("/api/admin/users", { method: "POST", token: admin.token, body: { username: name, password: "a-new-long-password-1", role: "user" } });
    expect(c.status).toBe(201);
    const id = c.data.user.id;
    expect((await api(`/api/admin/users/${id}`, { method: "PATCH", token: admin.token, body: { role: "admin" } })).status).toBe(200);
    expect((await api(`/api/admin/users/${id}`, { method: "PATCH", token: admin.token, body: { password: "another-long-password-2" } })).status).toBe(200);
    expect((await api("/api/admin/limits", { method: "PUT", token: admin.token, body: { sessions_per_hour: 7 } })).status).toBe(200);
    expect((await api("/api/me/password", { method: "POST", token: plain.token, body: { current_password: PW, new_password: "plain-new-password-3" } })).status).toBe(200);

    expect((await api("/api/admin/audit", { token: plain.token })).status).toBe(403);
    const r = await api("/api/admin/audit?limit=50", { token: admin.token });
    expect(r.status).toBe(200);
    const mine = r.data.entries.filter((e: any) => e.target === name || (e.action === "limits.update" && e.actor === admin.name) || e.target === plain.name);
    expect(mine.map((e: any) => e.action)).toEqual(["password.change", "limits.update", "user.update", "user.update", "user.create"]);
    expect(mine.find((e: any) => e.action === "user.create")).toMatchObject({ actor: admin.name, detail: { role: "user" } });
    expect(mine.filter((e: any) => e.action === "user.update").map((e: any) => e.detail)).toEqual([{ password_reset: true }, { role: "admin" }]);
    expect(JSON.stringify(r.data)).not.toMatch(/long-password|plain-new-password/);
    const ids = r.data.entries.map((e: any) => e.id);
    expect([...ids].sort((a: number, b: number) => b - a)).toEqual(ids);
  });
});
