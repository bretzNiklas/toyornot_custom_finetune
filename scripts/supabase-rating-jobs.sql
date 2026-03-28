begin;

create extension if not exists pgcrypto;
create extension if not exists pgmq cascade;

create table if not exists public.rating_jobs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  score_log_persisted_at timestamptz,
  status text not null default 'queued' check (status in ('queued', 'processing', 'completed', 'failed')),
  user_id uuid,
  guest_id text,
  account_tier text not null default 'free' check (account_tier in ('free', 'premium')),
  request_id text not null,
  image_hash_sha256 char(64) not null,
  judgement_engine_id text not null,
  request_payload jsonb not null,
  result_payload jsonb,
  error_message text,
  constraint rating_jobs_owner_check check (
    ((user_id is not null)::integer + (guest_id is not null)::integer) = 1
  )
);

alter table public.rating_jobs
  add column if not exists score_log_persisted_at timestamptz;

alter table public.rating_scores
  add column if not exists source_job_id uuid;

create unique index if not exists uq_rating_scores_source_job_id
  on public.rating_scores (source_job_id)
  where source_job_id is not null;

create index if not exists idx_rating_jobs_status_created_at
  on public.rating_jobs (status, created_at asc);

create index if not exists idx_rating_jobs_user_created_at
  on public.rating_jobs (user_id, created_at desc)
  where user_id is not null;

create index if not exists idx_rating_jobs_guest_created_at
  on public.rating_jobs (guest_id, created_at desc)
  where guest_id is not null;

alter table if exists public.rating_jobs enable row level security;
revoke all on table public.rating_jobs from anon, authenticated;
grant all on table public.rating_jobs to service_role;

do $$
begin
  if not exists (
    select 1
    from pgmq.list_queues()
    where queue_name = 'rating_dispatch'
  ) then
    perform pgmq.create('rating_dispatch');
  end if;
exception
  when undefined_function then
    perform pgmq.create('rating_dispatch');
end;
$$;

create or replace function public.enqueue_rating_job(
  p_account_tier text,
  p_guest_id text,
  p_image_hash_sha256 text,
  p_judgement_engine_id text,
  p_request_id text,
  p_request_payload jsonb,
  p_user_id uuid
)
returns setof public.rating_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_job public.rating_jobs%rowtype;
begin
  insert into public.rating_jobs (
    account_tier,
    guest_id,
    image_hash_sha256,
    judgement_engine_id,
    request_id,
    request_payload,
    status,
    user_id
  )
  values (
    p_account_tier,
    p_guest_id,
    p_image_hash_sha256,
    p_judgement_engine_id,
    p_request_id,
    p_request_payload,
    'queued',
    p_user_id
  )
  returning * into v_job;

  perform pgmq.send(
    'rating_dispatch',
    jsonb_build_object(
      'jobId', v_job.id::text,
      'requestId', v_job.request_id
    )
  );

  perform pg_notify('rating_queue_wakeup', v_job.id::text);

  return next v_job;
end;
$$;

create or replace function public.claim_rating_job(
  p_job_id uuid,
  p_stale_after_seconds integer default 300
)
returns setof public.rating_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_job public.rating_jobs%rowtype;
begin
  update public.rating_jobs as job
  set
    status = 'processing',
    completed_at = null,
    error_message = null,
    started_at = now()
  where job.id = p_job_id
    and (
      job.status = 'queued'
      or (
        job.status = 'processing'
        and job.started_at is not null
        and job.started_at < now() - make_interval(secs => greatest(p_stale_after_seconds, 0))
      )
    )
  returning job.* into v_job;

  if not found then
    return;
  end if;

  return next v_job;
end;
$$;

create or replace function public.claim_next_rating_job()
returns setof public.rating_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_job public.rating_jobs%rowtype;
begin
  with next_job as (
    select id
    from public.rating_jobs
    where status = 'queued'
      or (
        status = 'processing'
        and started_at is not null
        and started_at < now() - interval '10 minutes'
      )
    order by
      case when status = 'queued' then 0 else 1 end asc,
      coalesce(started_at, created_at) asc,
      created_at asc
    limit 1
    for update skip locked
  )
  update public.rating_jobs as job
  set
    status = 'processing',
    completed_at = null,
    started_at = now()
  from next_job
  where job.id = next_job.id
  returning job.* into v_job;

  if not found then
    return;
  end if;

  return next v_job;
end;
$$;

create or replace function public.complete_rating_job(
  p_job_id uuid,
  p_result_payload jsonb,
  p_score_log_payload jsonb
)
returns setof public.rating_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_job public.rating_jobs%rowtype;
begin
  select *
  into v_job
  from public.rating_jobs
  where id = p_job_id
  for update;

  if not found then
    return;
  end if;

  update public.rating_jobs as job
  set
    completed_at = coalesce(job.completed_at, now()),
    error_message = null,
    result_payload = p_result_payload,
    status = 'completed'
  where job.id = p_job_id
  returning job.* into v_job;

  insert into public.rating_scores (
    total_score,
    lettering_style,
    color,
    composition,
    originality_concept,
    technique_complexity,
    category_scores,
    evidence,
    image_adequacy,
    uncertainty,
    critique,
    share_card_verdict,
    used_fallback_critique,
    critique_language,
    judgement_engine_id,
    rating_schema_version,
    image_mime_type,
    image_size_bytes,
    image_hash_sha256,
    model,
    request_id,
    source_job_id
  )
  values (
    (p_score_log_payload ->> 'total_score')::numeric,
    (p_score_log_payload ->> 'lettering_style')::smallint,
    (p_score_log_payload ->> 'color')::smallint,
    (p_score_log_payload ->> 'composition')::smallint,
    (p_score_log_payload ->> 'originality_concept')::smallint,
    (p_score_log_payload ->> 'technique_complexity')::smallint,
    p_score_log_payload -> 'category_scores',
    p_score_log_payload -> 'evidence',
    p_score_log_payload -> 'image_adequacy',
    (p_score_log_payload ->> 'uncertainty')::numeric,
    coalesce(p_score_log_payload ->> 'critique', ''),
    nullif(btrim(p_score_log_payload ->> 'share_card_verdict'), ''),
    coalesce((p_score_log_payload ->> 'used_fallback_critique')::boolean, false),
    p_score_log_payload ->> 'critique_language',
    p_score_log_payload ->> 'judgement_engine_id',
    coalesce(p_score_log_payload ->> 'rating_schema_version', 'v2'),
    p_score_log_payload ->> 'image_mime_type',
    (p_score_log_payload ->> 'image_size_bytes')::integer,
    null,
    p_score_log_payload ->> 'model',
    p_score_log_payload ->> 'request_id',
    p_job_id
  )
  on conflict (source_job_id) where source_job_id is not null do nothing;

  update public.rating_jobs as job
  set score_log_persisted_at = coalesce(job.score_log_persisted_at, now())
  where job.id = p_job_id
    and exists (
      select 1
      from public.rating_scores
      where source_job_id = p_job_id
    )
  returning job.* into v_job;

  return next v_job;
end;
$$;

revoke execute on function public.enqueue_rating_job(text, text, text, text, text, jsonb, uuid) from anon, authenticated;
grant execute on function public.enqueue_rating_job(text, text, text, text, text, jsonb, uuid) to service_role;

revoke execute on function public.claim_rating_job(uuid, integer) from anon, authenticated;
grant execute on function public.claim_rating_job(uuid, integer) to service_role;

revoke execute on function public.claim_next_rating_job() from anon, authenticated;
grant execute on function public.claim_next_rating_job() to service_role;

revoke execute on function public.complete_rating_job(uuid, jsonb, jsonb) from anon, authenticated;
grant execute on function public.complete_rating_job(uuid, jsonb, jsonb) to service_role;

commit;
