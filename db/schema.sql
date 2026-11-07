-- ZoningTrack database schema. Safe to re-run (idempotent).
create table if not exists municipalities (
  key          text primary key,
  name         text not null,
  county       text,
  updated_at   timestamptz default now()
);

create table if not exists documents (
  id             text primary key,              -- <municipality key>/<doc id>
  municipality_key text references municipalities(key),
  municipality   text,
  county         text,
  board          text,
  kind           text,                          -- agenda, minutes, packet, notice, agenda items, ...
  meeting_date   date,
  title          text,
  url            text,
  source_page    text,
  sha256         text,
  pages          int,
  chars          int,
  ocr            boolean,
  rolling        boolean,
  agenda_items   jsonb,
  meta           jsonb,                         -- every other field the collector recorded
  body           text,                          -- full extracted text
  collected_at   timestamptz,
  extracted_at   timestamptz,                   -- set when the AI step has processed it
  loaded_at      timestamptz default now()
);
create index if not exists documents_muni_date on documents (municipality_key, meeting_date desc);
create index if not exists documents_unextracted on documents (extracted_at) where extracted_at is null;

create table if not exists projects (
  id             bigserial primary key,
  document_id    text references documents(id) on delete cascade,
  municipality_key text references municipalities(key),
  board          text,
  meeting_date   date,
  plan_number    text,
  name           text,
  address        text,
  parcel         text,
  owner          text,
  applicant      text,
  request        text,
  zoning         text,
  size           text,
  use            text,
  stage          text,
  outcome        text,
  raw            jsonb,
  created_at     timestamptz default now()
);
create index if not exists projects_muni on projects (municipality_key, meeting_date desc);

-- Locked down: no public access. Only the service role (our collectors) can read or write.
alter table municipalities enable row level security;
alter table documents      enable row level security;
alter table projects       enable row level security;
alter table projects add column if not exists category text;
