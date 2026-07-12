-- Zion Jobs — schema Fase 2 (Supabase como backend real)
-- Rode isso no SQL Editor do projeto https://lnupezzcozlccigqzfsw.supabase.co
-- (Table Editor / SQL Editor > New query > cola tudo > Run)

create extension if not exists pgcrypto;

create table if not exists empresas (
  id uuid primary key default gen_random_uuid(),
  auth_user_id uuid references auth.users(id),
  nome text not null,
  cnpj text,
  email text not null,
  tel text,
  area text,
  endereco text,
  cidade text,
  estado text,
  logo_url text,
  verificado boolean not null default false,
  plano text,
  expira_em timestamptz,
  criado_em timestamptz not null default now()
);

create table if not exists vagas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid references empresas(id),          -- null para vagas raspadas
  empresa_nome text,                                  -- denormalizado p/ renderizar sem join
  empresa_logo_url text,
  empresa_verificado boolean not null default false,
  titulo text not null,
  descricao text,
  tipo text,                -- CLT | Freela | Estágio | Parceria | Mentoria
  modalidade text,          -- Presencial | Remoto | Híbrido
  setor text,
  cidade text,
  estado text,
  pais text default 'BR',   -- 'BR' | 'global' | 'US' | 'EU' | 'CA' | 'CN' | 'AU'
  salario text,
  cota_pcd boolean not null default false,
  cursos_necessarios text[] default '{}',
  tags text[] default '{}',
  origem text not null,     -- 'empresa' | 'scraper'
  fonte text,
  link_externo text,
  external_id text unique,  -- dedupe do scraper (ex: 'remoteok-123')
  criado_em timestamptz not null default now()
);

create table if not exists candidaturas (
  id uuid primary key default gen_random_uuid(),
  vaga_id uuid not null references vagas(id) on delete cascade,
  user_id uuid not null references auth.users(id),
  nome text,
  email text,
  whatsapp text,
  criado_em timestamptz not null default now(),
  unique (vaga_id, user_id)
);

alter table empresas enable row level security;
alter table vagas enable row level security;
alter table candidaturas enable row level security;

-- ─────────────────────────────────────────
-- EMPRESAS
-- qualquer usuário autenticado pode se cadastrar como empresa;
-- só enxerga o próprio registro. verificado/plano/expira_em só
-- mudam pelo Table Editor (service role ignora RLS) — esse é o admin.
-- ─────────────────────────────────────────
create policy "empresa insert" on empresas
  for insert with check (auth.uid() = auth_user_id);

create policy "empresa select own" on empresas
  for select using (auth.uid() = auth_user_id);

-- ─────────────────────────────────────────
-- VAGAS
-- leitura pública (feed aberto pra todo mundo);
-- escrita só por empresa verificada dona da vaga.
-- ─────────────────────────────────────────
create policy "vagas select public" on vagas
  for select using (true);

create policy "vagas insert empresa verificada" on vagas
  for insert with check (
    origem = 'empresa' and exists (
      select 1 from empresas e
      where e.id = empresa_id and e.auth_user_id = auth.uid() and e.verificado = true
    )
  );

create policy "vagas update dono" on vagas
  for update using (
    exists (select 1 from empresas e where e.id = empresa_id and e.auth_user_id = auth.uid())
  );

create policy "vagas delete dono" on vagas
  for delete using (
    exists (select 1 from empresas e where e.id = empresa_id and e.auth_user_id = auth.uid())
  );

-- ─────────────────────────────────────────
-- CANDIDATURAS
-- usuário cria a própria candidatura; enxerga a própria e a
-- empresa dona da vaga enxerga as candidaturas dela.
-- ─────────────────────────────────────────
create policy "candidatura insert" on candidaturas
  for insert with check (auth.uid() = user_id);

create policy "candidatura select" on candidaturas
  for select using (
    auth.uid() = user_id or exists (
      select 1 from vagas v join empresas e on e.id = v.empresa_id
      where v.id = vaga_id and e.auth_user_id = auth.uid()
    )
  );

-- ─────────────────────────────────────────
-- Índices úteis pro feed público
-- ─────────────────────────────────────────
create index if not exists idx_vagas_criado_em on vagas (criado_em desc);
create index if not exists idx_vagas_origem on vagas (origem);
create index if not exists idx_candidaturas_vaga on candidaturas (vaga_id);

-- ─────────────────────────────────────────
-- MIGRAÇÃO — rodar de novo se a tabela `vagas` já existia sem a coluna `pais`
-- (idempotente: pode rodar o arquivo inteiro de novo sem problema)
-- ─────────────────────────────────────────
alter table vagas add column if not exists pais text default 'BR';
create index if not exists idx_vagas_pais on vagas (pais);

alter table vagas add column if not exists idioma text default 'pt';  -- 'pt' | 'en'
