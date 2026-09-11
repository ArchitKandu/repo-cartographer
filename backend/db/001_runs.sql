-- One row per mapping run, owned by whoever asked for it.
--
-- Every file in db/ is applied in filename order by `scripts/apply_migrations.py`,
-- on every run, with no record of what has already been applied. That is a
-- deliberate choice rather than a missing feature — a tracking table is one more
-- thing to get out of step with reality — but it buys simplicity with a rule that
-- has to hold: **every statement here must be safe to execute twice.** Hence
-- `if not exists` throughout, and `drop policy if exists` before each `create
-- policy`, which is the only way to make a policy idempotent (Postgres has no
-- `create policy if not exists`, even at 17).

create table if not exists public.runs (
    id          uuid primary key default gen_random_uuid(),

    -- The owner. `on delete cascade` because a deleted account should not leave
    -- its mapping history behind: this row carries the question the person asked
    -- and a pointer to the guide it produced.
    user_id     uuid not null references auth.users (id) on delete cascade,

    -- The LangGraph thread this run lives in — the join to `langgraph.checkpoints`
    -- and the id `run_config(thread_id)` is given when the run is resumed after an
    -- approval. Deliberately not a foreign key: `checkpoints` holds many rows per
    -- thread and none of them is a parent, and checkpoints are prunable while this
    -- row is the durable record that the run happened.
    thread_id   text not null unique,

    repo        text not null,
    question    text not null,

    -- `awaiting_approval` is the state that makes this table worth having. It is
    -- how the frontend knows to render an approve/reject control for a run whose
    -- process may be long gone, without asking LangGraph to enumerate threads.
    status      text not null default 'running'
                check (status in ('running', 'awaiting_approval', 'done', 'failed')),

    -- Object path in Supabase Storage, not the guide itself. A guide is a document
    -- served to a browser; keeping it out of the row keeps this table small enough
    -- that listing a user's runs stays cheap, and keeps the 500 MB the free tier
    -- gives the database for state rather than content.
    guide_path  text,

    -- Set when status becomes 'failed'. Null otherwise.
    error       text,

    created_at  timestamptz not null default now(),
    finished_at timestamptz
);

-- The only query the frontend makes: this user's runs, newest first. `user_id`
-- leads, so this index also covers the foreign key above — an unindexed FK makes
-- every `delete from auth.users` scan this table.
create index if not exists runs_user_id_created_at_idx
    on public.runs (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
--
-- Not optional here. This table is in `public`, which Supabase exposes through
-- the Data API, so the moment `authenticated` is granted access below, every
-- signed-in user of this project can address it over HTTPS. RLS is what decides
-- which rows come back.

alter table public.runs enable row level security;

-- Read-only, and only your own rows. There is deliberately no insert, update or
-- delete policy: with RLS enabled, no policy means no access, so those verbs are
-- denied to `authenticated` by omission rather than by a rule that has to stay
-- correct. Nothing is lost — the backend writes these rows over the direct
-- Postgres connection as `postgres`, which owns the table and bypasses RLS, and
-- the browser has no reason to author a run row itself.
--
-- `(select auth.uid())` rather than a bare `auth.uid()`: the subquery form is
-- hoisted to an InitPlan and evaluated once per statement, where the bare call is
-- re-evaluated per row. On a listing query that is the difference between one
-- call and one per run the user has ever made.
drop policy if exists runs_select_own on public.runs;
create policy runs_select_own on public.runs
    for select
    to authenticated
    using ((select auth.uid()) = user_id);

-- Revoke first, then grant back exactly one verb.
--
-- The revoke is not defensive tidiness, and writing `grant select` alone here
-- would have been a mistake: Supabase ships `alter default privileges` rules that
-- give `anon` and `authenticated` **all** privileges on every new table in
-- `public`. Inspected right after `create table`, this one had already handed
-- both roles DELETE, INSERT, REFERENCES, SELECT, TRIGGER, TRUNCATE and UPDATE. A
-- `grant select` on top of that grants nothing new and restricts nothing.
--
-- RLS does hold the line for most of those — with no matching policy an UPDATE or
-- DELETE quietly affects zero rows rather than erroring, which is why a test that
-- only watches for exceptions will report a table as locked down when it is not.
-- But TRUNCATE is the exception that makes this worth doing properly: Postgres
-- does not apply row security policies to TRUNCATE at all. A role holding that
-- privilege can empty the table regardless of any policy written above.
--
-- So the grants are reset to the access model this table actually has: everyone
-- signed in may read (rows filtered by the policy), nobody may write, and `anon`
-- gets nothing at all — there is no such thing as an anonymous run.
revoke all on public.runs from anon, authenticated;
grant select on public.runs to authenticated;
