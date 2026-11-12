-- Who can see what. Re-run safely after every pipeline rebuild.
-- Public (anon): only the teaser views. Members (signed-in, email on the members list): the full pipeline.

create table if not exists members (
  email_hash text primary key,          -- sha256 of the lower-cased email (keeps emails out of the public repo)
  note text,
  added_at timestamptz default now());
alter table members enable row level security;
insert into members (email_hash, note) values
  ('05eb1d5eeff45c863f1b9a25799520d8c0a3c630167b4c47bc710e9c0e496b52', 'owner'),
  ('75a70b0ffc526998adbe6e8f36e242f01b3bfa354d5708431383ea2067dddfc9', 'member (added by owner 2026-11-12)')
  on conflict (email_hash) do nothing;

create or replace function public.is_member() returns boolean
  language sql stable security definer set search_path = public as $$
  select exists (select 1 from members
                 where email_hash = encode(sha256(convert_to(lower(coalesce(auth.jwt() ->> 'email', '')), 'UTF8')), 'hex'))
$$;
revoke all on function public.is_member() from public;
grant execute on function public.is_member() to anon, authenticated;

drop policy if exists members_read_pipeline on pipeline;
create policy members_read_pipeline on pipeline for select to authenticated using (public.is_member());
drop policy if exists members_read_projects on projects;
create policy members_read_projects on projects for select to authenticated using (public.is_member());
drop policy if exists members_read_munis on municipalities;
create policy members_read_munis on municipalities for select to authenticated using (public.is_member());

-- Public teaser: latest notable projects, no address / parcel / owner / applicant / documents.
create or replace view public.pipeline_teaser as
  select id, municipality, name, category, stage, first_seen, last_seen, mentions
  from pipeline
  where category not in ('residential minor', 'property maintenance', 'other', 'historic review', 'zoning text or map amendment', 'infrastructure/utility')
    and last_seen <= current_date + 60
  order by last_seen desc nulls last
  limit 15;
create or replace view public.pipeline_stats as
  select (select count(*) from pipeline) as projects,
         (select count(distinct municipality_key) from pipeline) as municipalities,
         (select count(*) from documents) as documents,
         (select count(*) from pipeline where last_seen >= current_date - 30) as active_30d,
         (select max(loaded_at) from documents) as updated_at;
revoke all on public.pipeline_teaser, public.pipeline_stats from anon, authenticated;
grant select on public.pipeline_teaser, public.pipeline_stats to anon, authenticated;

-- Login gate before launch: the login page only sends a sign-in link to emails on the members list.
create or replace function public.can_login(email text) returns boolean
  language sql stable security definer set search_path = public as $$
  select exists (select 1 from members
                 where email_hash = encode(sha256(convert_to(lower(trim(coalesce(email, ''))), 'UTF8')), 'hex'))
$$;
revoke all on function public.can_login(text) from public;
grant execute on function public.can_login(text) to anon, authenticated;
