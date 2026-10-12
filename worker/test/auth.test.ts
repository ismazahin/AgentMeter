import { describe, expect, it } from "vitest";
import { MAX_USERS } from "../src/auth";
import { hashPassword, verifyPassword, verifyToken } from "../src/crypto";
import { E, PW, accessToken, api, uname, user } from "./helpers";

describe("passwords", () => {
  it("PBKDF2 with a pepper: verifies, rejects wrong password and wrong pepper, 40k iterations", async () => {
    const h = await hashPassword(PW, E.PASSWORD_PEPPER, 40000);
    expect(h.iterations).toBe(40000);
    const u = { pw_hash: h.hash, pw_salt: h.salt, pw_iter: h.iterations };
    expect(await verifyPassword(PW, E.PASSWORD_PEPPER, u)).toBe(true);
    expect(await verifyPassword(PW + "x", E.PASSWORD_PEPPER, u)).toBe(false);
    expect(await verifyPassword(PW, "another-pepper-0123456789", u)).toBe(false);   // a DB dump alone is not enough
    const row = await E.DB.prepare("SELECT pw_iter FROM users LIMIT 1").first();
    if (row) expect(row.pw_iter).toBe(40000);
  });
});

describe("login, tokens, refresh, logout", () => {
  it("logs in, reads /api/me, and the access token expires after 15 minutes", async () => {
    const u = await user();
    const me = await api("/api/me", { token: u.token });
    expect(me.status).toBe(200);
    expect(me.data.user.username).toBe(u.name);
    const p = await verifyToken<any>(E.ACCESS_TOKEN_SECRET, u.token, "access");
    expect(p!.exp - p!.iat).toBe(15 * 60);
    expect(JSON.stringify(me.data)).not.toMatch(/pw_hash|pw_salt/);
  });

  it("rejects missing, forged, expired and wrong-type tokens", async () => {
    const u = await user();
    expect((await api("/api/me")).status).toBe(401);
    expect((await api("/api/me", { token: u.token.slice(0, -2) + "xx" })).status).toBe(401);
    const sid = (await verifyToken<any>(E.ACCESS_TOKEN_SECRET, u.token, "access"))!.sid;
    const expired = await accessToken({ sub: u.id, role: "user", sid, iat: 1, exp: 2 });
    expect((await api("/api/me", { token: expired })).data.code).toBe("token_expired");
    const { signToken } = await import("../src/crypto");
    const runTyp = await signToken(E.ACCESS_TOKEN_SECRET, { typ: "run", sub: u.id, sid, exp: 9e9 });
    expect((await api("/api/me", { token: runTyp })).status).toBe(401);
  });

  it("refresh rotates the token; reusing the old one revokes the session", async () => {
    const u = await user();
    const r1 = await api("/api/auth/refresh", { body: { refresh_token: u.refresh } });
    expect(r1.status).toBe(200);
    expect(r1.data.refresh_token).not.toBe(u.refresh);
    expect((await api("/api/me", { token: r1.data.access_token })).status).toBe(200);
    const reuse = await api("/api/auth/refresh", { body: { refresh_token: u.refresh } });
    expect(reuse.data.code).toBe("session_revoked");
    expect((await api("/api/me", { token: r1.data.access_token })).status).toBe(401);       // whole session ended
    expect((await api("/api/auth/refresh", { body: { refresh_token: r1.data.refresh_token } })).status).toBe(401);
  });

  it("logout revokes at once: the access token and the refresh token stop working", async () => {
    const u = await user();
    expect((await api("/api/auth/logout", { method: "POST", token: u.token })).status).toBe(200);
    expect((await api("/api/me", { token: u.token })).data.code).toBe("session_revoked");
    expect((await api("/api/auth/refresh", { body: { refresh_token: u.refresh } })).status).toBe(401);
  });

  it("login is throttled after 5 failures per username, and wrong user vs wrong password look the same", async () => {
    const u = await user();
    const nobody = await api("/api/auth/login", { body: { username: "nobody-here", password: "x".repeat(12) } });
    const wrong = await api("/api/auth/login", { body: { username: u.name, password: "x".repeat(12) } });
    expect(nobody.status).toBe(401);
    expect(wrong.data).toEqual(nobody.data);
    for (let i = 0; i < 4; i++) await api("/api/auth/login", { body: { username: u.name, password: "bad-" + i } });
    const blocked = await api("/api/auth/login", { body: { username: u.name, password: PW } });   // even the right one
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("Retry-After")).toBe("900");
  });

  it("change password needs the current one and logs out other devices", async () => {
    const u = await user();
    const other = await api("/api/auth/login", { body: { username: u.name, password: PW } });
    expect((await api("/api/me/password", { body: { current_password: "nope-nope-nope", new_password: "new-password-123" }, token: u.token })).status).toBe(403);
    expect((await api("/api/me/password", { body: { current_password: PW, new_password: "short" }, token: u.token })).data.code).toBe("weak_password");
    expect((await api("/api/me/password", { body: { current_password: PW, new_password: "new-password-123" }, token: u.token })).status).toBe(200);
    expect((await api("/api/me", { token: u.token })).status).toBe(200);
    expect((await api("/api/me", { token: other.data.access_token })).status).toBe(401);
    expect((await api("/api/auth/login", { body: { username: u.name, password: "new-password-123" } })).status).toBe(200);
  });
});

describe("users: admin only, max 5, roles", () => {
  it("only an admin can manage users; a user gets 403", async () => {
    const plain = await user("user");
    expect((await api("/api/admin/users", { token: plain.token })).status).toBe(403);
    expect((await api("/api/admin/users", { body: { username: uname(), password: PW }, token: plain.token })).status).toBe(403);
    expect((await api("/api/admin/limits", { token: plain.token })).status).toBe(403);
    expect((await api("/api/admin/credentials", { token: plain.token })).status).toBe(403);
  });

  it("enforces at most 5 users server-side (disabled users count; deleting one frees a slot)", async () => {
    const admin = await user("admin");
    let count = (await E.DB.prepare("SELECT COUNT(*) AS n FROM users").first()).n as number;
    const made: string[] = [];
    while (count < MAX_USERS) {
      const r = await api("/api/admin/users", { body: { username: uname("m"), password: PW, role: "user" }, token: admin.token });
      expect(r.status).toBe(201);
      expect(r.data.user.must_change_pw).toBe(true);
      made.push(r.data.user.id);
      count++;
    }
    const sixth = await api("/api/admin/users", { body: { username: uname("x"), password: PW }, token: admin.token });
    expect(sixth.status).toBe(409);
    expect(sixth.data.code).toBe("max_users");
    const victim = made[made.length - 1];
    expect((await api(`/api/admin/users/${victim}`, { method: "DELETE", token: admin.token })).data.code).toBe("not_disabled");
    expect((await api(`/api/admin/users/${victim}`, { method: "PATCH", body: { disabled: true }, token: admin.token })).data.user.disabled).toBe(true);
    expect((await api("/api/admin/users", { body: { username: uname("x"), password: PW }, token: admin.token })).status).toBe(409);
    expect((await api(`/api/admin/users/${victim}`, { method: "DELETE", token: admin.token })).status).toBe(200);
    expect((await api("/api/admin/users", { body: { username: uname("y"), password: PW }, token: admin.token })).status).toBe(201);
  });

  it("disable ends the user's sessions; reset forces a password change; the last admin is protected", async () => {
    const admin = await user("admin");
    const u = await user("user");
    expect((await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { password: "reset-password-1" }, token: admin.token })).data.user.must_change_pw).toBe(true);
    expect((await api("/api/me", { token: u.token })).status).toBe(401);
    const back = await api("/api/auth/login", { body: { username: u.name, password: "reset-password-1" } });
    expect(back.data.user.must_change_pw).toBe(true);
    expect((await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { disabled: true }, token: admin.token })).status).toBe(200);
    expect((await api("/api/me", { token: back.data.access_token })).status).toBe(401);
    expect((await api("/api/auth/login", { body: { username: u.name, password: "reset-password-1" } })).status).toBe(401);
    expect((await api(`/api/admin/users/${admin.id}`, { method: "PATCH", body: { disabled: true }, token: admin.token })).data.code).toMatch(/self|last_admin/);
    const r = await api(`/api/admin/users/${admin.id}`, { method: "PATCH", body: { role: "user" }, token: admin.token });
    const admins = (await E.DB.prepare("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0").first()).n;
    if (admins === 1) expect(r.data.code).toBe("last_admin");
  });

  it("rejects bad usernames, weak passwords, duplicate users and unknown roles", async () => {
    const admin = await user("admin");
    expect((await api("/api/admin/users", { body: { username: "a b", password: PW }, token: admin.token })).data.code).toBe("bad_username");
    expect((await api("/api/admin/users", { body: { username: uname(), password: "short" }, token: admin.token })).data.code).toBe("weak_password");
    expect((await api("/api/admin/users", { body: { username: admin.name, password: PW }, token: admin.token })).data.code).toMatch(/user_exists|max_users/);
    expect((await api("/api/admin/users", { body: { username: uname(), password: PW, role: "root" }, token: admin.token })).data.code).toMatch(/bad_role|max_users/);
  });
});

describe("Remember me, inactivity logout, devices (profile)", () => {
  const login = (name: string, remember?: boolean, ua = "TestBrowser/1.0") =>
    api("/api/auth/login", { body: { username: name, password: PW, ...(remember === undefined ? {} : { remember }) }, headers: { "User-Agent": ua } });

  it("Remember me off (the default) gives a 12 h login with an idle limit; on gives 30 days and no idle limit", async () => {
    const u = await user();
    const off = await login(u.name);
    expect(off.data.session).toEqual({ remember: false, idle_minutes: 30 });
    const on = await login(u.name, true);
    expect(on.data.session).toEqual({ remember: true, idle_minutes: 0 });
    const rows = (await E.DB.prepare("SELECT remember, created_at, expires_at FROM auth_sessions WHERE user_id=? ORDER BY remember").bind(u.id).all()).results;
    const hours = (r: any) => (Date.parse(r.expires_at) - Date.parse(r.created_at)) / 3600e3;
    const offRow = rows.filter((r: any) => !r.remember).pop(), onRow = rows.find((r: any) => r.remember);
    expect(Math.round(hours(offRow))).toBe(12);
    expect(Math.round(hours(onRow))).toBe(30 * 24);
  });

  it("a Remember-me-off login that went quiet past the idle limit cannot refresh; a remembered one can", async () => {
    const u = await user();
    const off = await login(u.name), on = await login(u.name, true);
    const old = new Date(Date.now() - 60 * 60e3).toISOString();         // 60 min > 30 idle + 15 slack
    await E.DB.prepare("UPDATE auth_sessions SET last_used_at=? WHERE user_id=?").bind(old, u.id).run();
    const r = await api("/api/auth/refresh", { body: { refresh_token: off.data.refresh_token } });
    expect(r.status).toBe(401);
    expect(r.data.code).toBe("session_idle");
    expect((await api("/api/me", { token: off.data.access_token })).status).toBe(401);      // revoked, not just refused
    expect((await api("/api/auth/refresh", { body: { refresh_token: on.data.refresh_token } })).status).toBe(200);
  });

  it("the admin's session_idle_minutes applies; 0 turns the idle logout off", async () => {
    const admin = await user("admin"), u = await user();
    expect((await api("/api/admin/limits", { method: "PUT", token: admin.token, body: { session_idle_minutes: 0 } })).status).toBe(200);
    const off = await login(u.name);
    expect(off.data.session.idle_minutes).toBe(0);
    await E.DB.prepare("UPDATE auth_sessions SET last_used_at=? WHERE user_id=?").bind(new Date(Date.now() - 5 * 3600e3).toISOString(), u.id).run();
    expect((await api("/api/auth/refresh", { body: { refresh_token: off.data.refresh_token } })).status).toBe(200);
  });

  it("/api/me tells the role, this login and the previous one", async () => {
    const u = await user();                                       // first login (helper)
    const second = await login(u.name, true);
    const me = await api("/api/me", { token: second.data.access_token });
    expect(me.data.user.role).toBe("user");
    expect(me.data.session.remember).toBe(true);
    expect(me.data.session.logged_in_at).toBeTruthy();
    expect(me.data.previous_login_at).toBeTruthy();
    expect(JSON.stringify(me.data)).not.toMatch(/pw_hash|pw_salt|refresh_hash/);
  });

  it("lists my logged-in devices and logs one out; never someone else's", async () => {
    const u = await user(), other = await user();
    const phone = await login(u.name, true, "Phone/2.0");
    const list = await api("/api/me/devices", { token: u.token });
    expect(list.status).toBe(200);
    expect(list.data.devices.length).toBe(2);
    expect(list.data.devices.filter((d: any) => d.current).length).toBe(1);
    const ph = list.data.devices.find((d: any) => d.user_agent === "Phone/2.0");
    expect(ph.remember).toBe(true);
    expect(JSON.stringify(list.data)).not.toMatch(/refresh_hash|prev_hash/);
    // someone else cannot revoke it (looks like "no such device")
    expect((await api(`/api/me/devices/${ph.id}`, { method: "DELETE", token: other.token })).status).toBe(404);
    const del = await api(`/api/me/devices/${ph.id}`, { method: "DELETE", token: u.token });
    expect(del.status).toBe(200);
    expect(del.data.current).toBe(false);
    expect((await api("/api/me", { token: phone.data.access_token })).status).toBe(401);
    expect((await api("/api/auth/refresh", { body: { refresh_token: phone.data.refresh_token } })).status).toBe(401);
    expect((await api("/api/me/devices", { token: u.token })).data.devices.length).toBe(1);
    expect((await api("/api/me", { token: u.token })).status).toBe(200);                   // this device still logged in
  });
});
