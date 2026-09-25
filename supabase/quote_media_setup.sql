-- Quote media setup. Run once in Supabase → SQL Editor.

-- 1. Storage bucket for re-hosted quote media (images / videos under random UUID names).
--    Public read so Netlify can proxy /media/<file>; only the service key can write.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('quote-media', 'quote-media', true, 52428800,
        array['image/jpeg', 'image/png', 'video/mp4', 'video/webm'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- 2. Opaque viewer links: /v/<token> -> vendor URL (for 360 viewer pages that can't be copied).
create table if not exists public.media_links (
  token      text primary key check (token ~ '^[a-z2-9]{8,32}$'),
  vendor_url text not null,
  created_at timestamptz not null default now()
);
create index if not exists media_links_vendor_url_idx on public.media_links (vendor_url);

-- Row Level Security on, and NO policies: the public (anon / authenticated) can't read or
-- write anything. Only the service key (Render app, Netlify functions) bypasses RLS.
alter table public.media_links enable row level security;
revoke all on public.media_links from anon, authenticated;

-- 3. Captured 360 frames (added later; safe to run again).
--    Frames are stored in the quote-media bucket as <spin_id>/000.jpg, 001.jpg, …
--    A /v/<token> link whose row has spin_id set opens our own spinner, never the vendor page.
alter table public.media_links add column if not exists spin_id text check (spin_id ~ '^[a-f0-9]{32}$');
alter table public.media_links add column if not exists spin_frames integer check (spin_frames between 2 and 720);

-- 4. The frame the 360 starts on (the stone's "top" view). Added later; safe to run again.
alter table public.media_links add column if not exists spin_top integer check (spin_top >= 0);

-- 5. Capture code version, and captures a re-capture replaced (added later; safe to run again).
--    Only captures with spin_version >= 2 (or a spin_top) are ever shown; the first build's
--    captures (none of these) fall back to the original viewer. Nothing is ever deleted.
alter table public.media_links add column if not exists spin_version integer;
alter table public.media_links add column if not exists old_spin_ids text[] not null default '{}';
create index if not exists media_links_spin_id_idx on public.media_links (spin_id);
