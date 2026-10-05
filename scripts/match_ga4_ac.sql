-- Rebuild the GA4 <-> ActiveCampaign source mapping from the staging tables.
-- Tokens substituted before execution: {{WINDOW_DAYS}} (int), {{TOL_SECONDS}} (int).
-- Statements are separated by lines beginning with "-- @@" (split marker).
-- Idempotent: both output tables are fully rebuilt each run.

-- @@ rebuild per-contact rollup
truncate public.ga4_ac_contact_source;
-- @@
insert into public.ga4_ac_contact_source
  (contact_id, email, cdate, source, medium, campaign, match_method,
   confidence, candidate_count, matched_at, time_delta_seconds, updated_at)
with contacts_scope as (
  select id,
         lower(email) as email,
         cdate,
         count(*) over (partition by date_trunc('minute', cdate)) as smc
  from public.activecampaign_contacts
  where cdate >= now() - (interval '1 day' * {{WINDOW_DAYS}})
    and email is not null and email <> ''
),
-- Calendly: reliable email match; earliest booking per contact wins.
calendly_match as (
  select distinct on (c.id)
         c.id as contact_id,
         coalesce(nullif(b.utm_source, ''), '(calendly)') as source,
         coalesce(nullif(b.utm_medium, ''), 'booking')    as medium,
         coalesce(nullif(b.utm_campaign, ''), b.event_name) as campaign,
         b.booking_created_at as matched_at
  from public.calendly_bookings_staging b
  join contacts_scope c on c.email = lower(b.email)
  order by c.id, b.booking_created_at asc
),
-- GA4: time-proximity match. Exclude bulk-import bursts (>2 contacts in the minute).
ga4_cand as (
  select c.id as contact_id,
         g.source, g.medium, g.campaign, g.event_time_utc,
         abs(extract(epoch from (c.cdate - g.event_time_utc))) as delta
  from contacts_scope c
  join public.ga4_events_staging g
    on g.event_time_utc between c.cdate - (interval '1 second' * {{TOL_SECONDS}})
                            and c.cdate + (interval '1 second' * {{TOL_SECONDS}})
  where c.smc <= 2
),
cand_counts as (
  select contact_id, count(distinct source || '/' || medium) as cand_src
  from ga4_cand group by contact_id
),
ga4_ranked as (
  select *, row_number() over (partition by contact_id order by delta asc) as rn
  from ga4_cand
),
ga4_match as (
  select r.contact_id, r.source, r.medium, r.campaign, r.event_time_utc as matched_at,
         r.delta, cc.cand_src
  from ga4_ranked r
  join cand_counts cc using (contact_id)
  where r.rn = 1
),
combined as (
  select cs.id as contact_id, cs.email, cs.cdate,
         cm.source  as cm_source,  cm.medium as cm_medium, cm.campaign as cm_campaign,
         cm.matched_at as cm_at,
         gm.source  as gm_source,  gm.medium as gm_medium, gm.campaign as gm_campaign,
         gm.matched_at as gm_at, gm.delta as gm_delta, gm.cand_src as gm_cand
  from contacts_scope cs
  left join calendly_match cm on cm.contact_id = cs.id
  left join ga4_match     gm on gm.contact_id = cs.id
)
select
  contact_id,
  email,
  cdate,
  case when cm_source is not null then cm_source else gm_source end as source,
  case when cm_source is not null then cm_medium else gm_medium end as medium,
  case when cm_source is not null then cm_campaign else gm_campaign end as campaign,
  case when cm_source is not null then 'calendly_email'
       when gm_source is not null then 'ga4_time'
       else 'unmatched' end as match_method,
  case when cm_source is not null then 'high'
       when gm_source is not null then
         case when gm_cand > 1
              then case when gm_delta <= 90 then 'medium' else 'low' end
              else case when gm_delta <= 90 then 'high'   else 'medium' end
         end
       else 'none' end as confidence,
  case when cm_source is null and gm_source is not null then gm_cand end as candidate_count,
  coalesce(cm_at, gm_at) as matched_at,
  case when cm_source is null and gm_source is not null then round(gm_delta)::int end
       as time_delta_seconds,
  now()
from combined;

-- @@ rebuild per-event audit
truncate public.ga4_ac_event_matches;
-- @@ audit: GA4 form buckets -> the contact whose nearest match is this bucket (if any)
insert into public.ga4_ac_event_matches
  (event_time, event_kind, event_name, source, medium, campaign, event_count,
   matched_contact_id, matched_email, match_method, time_delta_seconds)
with contacts_scope as (
  select id, lower(email) as email, cdate,
         count(*) over (partition by date_trunc('minute', cdate)) as smc
  from public.activecampaign_contacts
  where cdate >= now() - (interval '1 day' * {{WINDOW_DAYS}})
    and email is not null and email <> ''
),
-- best (nearest) contact for each GA4 staging bucket, if within tolerance
pairs as (
  select g.ctid as gid, c.id as contact_id, c.email,
         abs(extract(epoch from (c.cdate - g.event_time_utc))) as delta
  from public.ga4_events_staging g
  join contacts_scope c
    on c.smc <= 2
   and g.event_time_utc between c.cdate - (interval '1 second' * {{TOL_SECONDS}})
                            and c.cdate + (interval '1 second' * {{TOL_SECONDS}})
),
best as (
  select distinct on (gid) gid, contact_id, email, delta
  from pairs
  order by gid, delta asc
)
select g.event_time_utc, 'ga4_form', g.event_name, g.source, g.medium, g.campaign,
       g.event_count,
       b.contact_id, b.email,
       case when b.contact_id is not null then 'ga4_time' end,
       case when b.contact_id is not null then round(b.delta)::int end
from public.ga4_events_staging g
left join best b on b.gid = g.ctid;
-- @@ audit: Calendly bookings -> AC contact by email (if any)
insert into public.ga4_ac_event_matches
  (event_time, event_kind, event_name, source, medium, campaign, event_count,
   matched_contact_id, matched_email, match_method, time_delta_seconds)
select b.booking_created_at, 'calendly', b.event_name,
       coalesce(nullif(b.utm_source, ''), '(calendly)'),
       coalesce(nullif(b.utm_medium, ''), 'booking'),
       coalesce(nullif(b.utm_campaign, ''), b.event_name),
       1,
       c.id, lower(b.email),
       case when c.id is not null then 'calendly_email' end,
       null
from public.calendly_bookings_staging b
left join public.activecampaign_contacts c on lower(c.email) = lower(b.email);
