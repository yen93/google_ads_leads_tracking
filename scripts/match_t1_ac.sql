-- Rebuild the "T1 Enquiries by channel" <-> ActiveCampaign mapping from staging.
-- Scoped to the 4 GA4 T1 conversion events:
--   calendly_form_submit, Form_submission_thankyou_1 (identity-bearing)
--   click_to_call, email_link_clicks              (anonymous -> unmappable)
-- Tokens substituted before execution: {{WINDOW_DAYS}} (int), {{TOL_SECONDS}} (int).
-- Statements are separated by lines beginning with "-- @@" (split marker).
-- Idempotent: both output tables are fully rebuilt each run.
-- Staging (loaded by map_t1_enquiries.py): public.ga4_events_staging holds all 4 T1
-- events this run; public.calendly_bookings_staging holds Calendly invitees.

-- @@ rebuild per-contact detail
truncate public.t1_enquiry_contacts restart identity;
-- @@
insert into public.t1_enquiry_contacts
  (contact_id, email, cdate, conversion_action, channel_source, channel_medium,
   match_method, confidence, time_delta_seconds, matched_at, updated_at)
with
-- AC contacts in the window, with same-minute-creation count (bulk-import guard).
contacts_scope as (
  select id,
         lower(email) as email,
         cdate,
         count(*) over (partition by date_trunc('minute', cdate)) as smc
  from public.activecampaign_contacts
  where cdate >= now() - (interval '1 day' * {{WINDOW_DAYS}})
    and email is not null and email <> ''
),
-- ---- CALENDLY: reliable identity by email; channel from nearest GA4 bucket ----
-- Nearest GA4 calendly_form_submit minute-bucket to each booking (for the channel).
cal_channel as (
  select b.ctid as bid,
         b.email, b.booking_created_at,
         b.utm_source, b.utm_medium,
         g.source as ga_source, g.medium as ga_medium,
         row_number() over (
           partition by b.ctid
           order by abs(extract(epoch from (b.booking_created_at - g.event_time_utc))) asc
         ) as rn
  from public.calendly_bookings_staging b
  left join public.ga4_events_staging g
    on g.event_name = 'calendly_form_submit'
   and g.event_time_utc between b.booking_created_at - (interval '1 second' * {{TOL_SECONDS}})
                            and b.booking_created_at + (interval '1 second' * {{TOL_SECONDS}})
),
-- Only bookings whose creation time aligns to an in-window GA4 calendly_form_submit
-- event are part of the dashboard's 27 (that event IS the counted enquiry, and it
-- carries the channel). Bookings with no in-window GA4 event are excluded.
cal_best as (
  select * from cal_channel where rn = 1 and ga_source is not null
),
calendly_contacts as (
  select distinct on (c.id, cb.ga_source, cb.ga_medium)
         c.id as contact_id,
         lower(cb.email) as email,
         c.cdate,
         'calendly_form_submit'::text as conversion_action,
         cb.ga_source as channel_source,
         cb.ga_medium as channel_medium,
         'calendly_email'::text as match_method,
         'high'::text as confidence,
         null::int as time_delta_seconds,
         cb.booking_created_at as matched_at
  from cal_best cb
  join public.activecampaign_contacts c on lower(c.email) = lower(cb.email)
  order by c.id, cb.ga_source, cb.ga_medium, cb.booking_created_at asc
),
-- ---- FORM: probabilistic identity by timestamp (same logic as match_ga4_ac.sql) ----
form_cand as (
  select c.id as contact_id, c.email, c.cdate,
         g.source, g.medium, g.event_time_utc,
         abs(extract(epoch from (c.cdate - g.event_time_utc))) as delta
  from contacts_scope c
  join public.ga4_events_staging g
    on g.event_name = 'Form_submission_thankyou_1'
   and g.event_time_utc between c.cdate - (interval '1 second' * {{TOL_SECONDS}})
                            and c.cdate + (interval '1 second' * {{TOL_SECONDS}})
  where c.smc <= 2
),
form_counts as (
  select contact_id, count(distinct source || '/' || medium) as cand_src
  from form_cand group by contact_id
),
form_ranked as (
  select *, row_number() over (partition by contact_id order by delta asc) as rn
  from form_cand
),
form_contacts as (
  select r.contact_id, r.email, r.cdate,
         'Form_submission_thankyou_1'::text as conversion_action,
         r.source as channel_source, r.medium as channel_medium,
         'ga4_time'::text as match_method,
         case when fc.cand_src > 1
              then case when r.delta <= 90 then 'medium' else 'low' end
              else case when r.delta <= 90 then 'high'   else 'medium' end
         end as confidence,
         round(r.delta)::int as time_delta_seconds,
         r.event_time_utc as matched_at
  from form_ranked r
  join form_counts fc using (contact_id)
  where r.rn = 1
)
select contact_id, email, cdate, conversion_action, channel_source, channel_medium,
       match_method, confidence, time_delta_seconds, matched_at, now()
from calendly_contacts
union all
select contact_id, email, cdate, conversion_action, channel_source, channel_medium,
       match_method, confidence, time_delta_seconds, matched_at, now()
from form_contacts;

-- @@ rebuild per-channel rollup
truncate public.t1_enquiry_channels restart identity;
-- @@
insert into public.t1_enquiry_channels
  (channel_source, channel_medium, calendly, form_submit, click_to_call, email_link,
   ga4_total, ac_contacts_matched, unmappable_count, updated_at)
with ga as (
  select source as channel_source, medium as channel_medium,
         coalesce(sum(event_count) filter (where event_name = 'calendly_form_submit'), 0)       as calendly,
         coalesce(sum(event_count) filter (where event_name = 'Form_submission_thankyou_1'), 0) as form_submit,
         coalesce(sum(event_count) filter (where event_name = 'click_to_call'), 0)              as click_to_call,
         coalesce(sum(event_count) filter (where event_name = 'email_link_clicks'), 0)          as email_link,
         sum(event_count) as ga4_total
  from public.ga4_events_staging
  group by source, medium
),
matched as (
  select channel_source, channel_medium, count(*) as ac_contacts_matched
  from public.t1_enquiry_contacts
  group by channel_source, channel_medium
)
select ga.channel_source, ga.channel_medium,
       ga.calendly, ga.form_submit, ga.click_to_call, ga.email_link,
       ga.ga4_total,
       coalesce(m.ac_contacts_matched, 0) as ac_contacts_matched,
       ga.click_to_call + ga.email_link   as unmappable_count,
       now()
from ga
left join matched m
  on m.channel_source is not distinct from ga.channel_source
 and m.channel_medium is not distinct from ga.channel_medium
order by ga.ga4_total desc;
