do $$
begin
    if not exists (
        select 1
        from pg_publication
        where pubname = 'supabase_realtime'
    ) then
        create publication supabase_realtime;
    end if;

    if not exists (
        select 1
        from pg_publication p
        join pg_publication_rel pr on pr.prpubid = p.oid
        join pg_class c on c.oid = pr.prrelid
        join pg_namespace n on n.oid = c.relnamespace
        where p.pubname = 'supabase_realtime'
          and n.nspname = 'public'
          and c.relname = 'judge_api_jobs'
    ) then
        alter publication supabase_realtime add table public.judge_api_jobs;
    end if;
end
$$;
