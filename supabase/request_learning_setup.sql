-- Live Search "Read request": house rules, saved examples and the correction log.
-- Run once in Supabase → SQL Editor (safe to run again).

-- 1. Settings (the editable "Request rules" text lives under key 'request_rules').
create table if not exists public.request_settings (
  key        text primary key,
  value      text not null default '',
  updated_at timestamptz not null default now()
);

-- 2. Saved examples: the original request text plus the criteria as finally searched.
create table if not exists public.request_examples (
  id           uuid primary key default gen_random_uuid(),
  request_text text not null,
  criteria     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);

-- 3. Correction log: a field the parser filled that was then changed before searching.
create table if not exists public.request_corrections (
  id           uuid primary key default gen_random_uuid(),
  field        text not null,
  parsed_value text,
  final_value  text,
  request_text text,
  created_at   timestamptz not null default now()
);
create index if not exists request_corrections_field_idx on public.request_corrections (field);

-- Row Level Security on, and NO policies: only the service key (the Render app) can read or write.
alter table public.request_settings    enable row level security;
alter table public.request_examples    enable row level security;
alter table public.request_corrections enable row level security;
revoke all on public.request_settings, public.request_examples, public.request_corrections from anon, authenticated;
