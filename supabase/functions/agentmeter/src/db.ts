// The control plane's database: PostgreSQL (Supabase). The routes keep the small statement API
// they were written against (prepare / bind / first / all / run, and batch = one transaction);
// this file runs it on any Postgres client:
//   - the Edge Function: postgres.js over Supabase's connection pooler (index.ts)
//   - the tests and the local dev server: PGlite, Postgres compiled to WebAssembly (no server)
// SQL is written with "?" placeholders; they become $1, $2, ... here (never inside a quoted string).

/** What a Postgres client must offer. */
export interface Querier {
  query(text: string, params: unknown[]): Promise<{ rows: Record<string, unknown>[]; count: number }>;
  transaction<T>(fn: (q: Querier) => Promise<T>): Promise<T>;
}

export interface RunResult { meta: { changes: number } }

/** "?" -> "$n", outside single-quoted literals. */
export function numbered(sql: string): string {
  let out = "", n = 0, quoted = false;
  for (const ch of sql) {
    if (ch === "'") quoted = !quoted;
    out += ch === "?" && !quoted ? "$" + ++n : ch;
  }
  return out;
}

export class Statement {
  constructor(private readonly db: Db, readonly sql: string, readonly params: unknown[] = []) {}

  bind(...params: unknown[]): Statement {
    return new Statement(this.db, this.sql, params.map((p) => (p === undefined ? null : p)));
  }

  exec(q: Querier) { return q.query(numbered(this.sql), this.params); }

  async first<T = Record<string, any>>(): Promise<T | null> {
    const r = await this.exec(this.db.q);
    return (r.rows[0] as T) ?? null;
  }

  async all<T = Record<string, any>>(): Promise<{ results: T[] }> {
    return { results: (await this.exec(this.db.q)).rows as T[] };
  }

  async run(): Promise<RunResult> {
    return { meta: { changes: (await this.exec(this.db.q)).count } };
  }
}

export class Db {
  constructor(readonly q: Querier) {}

  prepare(sql: string): Statement { return new Statement(this, sql); }

  /** Every statement in one transaction: all or none. */
  async batch(stmts: Statement[]): Promise<RunResult[]> {
    return this.q.transaction(async (tx) => {
      const out: RunResult[] = [];
      for (const s of stmts) out.push({ meta: { changes: (await s.exec(tx)).count } });
      return out;
    });
  }
}

// COUNT(*) is BIGINT and SUM() NUMERIC in Postgres; the routes compare plain numbers.
export const NUMBER_TYPES = [20, 1700];

/** postgres.js (the Edge Function; also the dev server with a DATABASE_URL). */
export function postgresJs(sql: any): Querier {
  const wrap = (s: any): Querier => ({
    async query(text, params) {
      const r = await s.unsafe(text, params);
      return { rows: Array.from(r), count: typeof r.count === "number" ? r.count : r.length };
    },
    transaction: (fn) => s.begin((tx: any) => fn(wrap(tx))),
  });
  return wrap(sql);
}

/** PGlite (tests and the local dev server without a database server). */
export function pglite(db: any): Querier {
  const wrap = (s: any, top: boolean): Querier => ({
    async query(text, params) {
      const r = await s.query(text, params);
      return { rows: r.rows, count: r.affectedRows ?? r.rows.length };
    },
    transaction: (fn) => (top ? db.transaction((tx: any) => fn(wrap(tx, false))) : fn(wrap(s, false))),
  });
  return wrap(db, true);
}
